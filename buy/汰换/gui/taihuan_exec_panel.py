"""「汰换」独立页面：按汰换方案在仓库中匹配最合理物品并执行点击。

流程（用户确认 2026-09-01）：
  1. 选择方案 → 「匹配库存」（自动先刷新仓库）
  2. 匹配报告：ok / 磨损偏差待定(>0.2 人工确认) / 缺口待定(等仓库到货)
  3. 用户确认后 → 「执行汰换」逐件点击（点击前一刻定位，asset_id 精确匹配）
  4. 汰换结束自动刷新仓库
"""
from __future__ import annotations

import logging
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QDoubleSpinBox, QFileDialog, QFormLayout,
    QGroupBox, QHBoxLayout, QHeaderView, QLabel, QListWidget,
    QListWidgetItem, QMessageBox, QPlainTextEdit, QProgressBar,
    QPushButton, QSplitter, QTableWidget, QVBoxLayout,
    QWidget,
)

from core import recipe_matcher
from gui.taihuan_worker import PlanMatchWorker, PlanExecuteWorker

logger = logging.getLogger(__name__)

_STATUS_TEXT = {
    "ok": "✓ 就绪",
    "wear_deviation": "⚠ 磨损偏差待定",
    "missing": "✗ 缺口待定",
    "pending": "… 待匹配",
}
_STATUS_COLOR = {
    "ok": "#2E7D32",
    "wear_deviation": "#E65100",
    "missing": "#C62828",
    "pending": "#757575",
}


class TaihuanExecPanel(QWidget):
    """「汰换」页：方案 → 匹配 → 确认 → 执行。"""

    # 本页刷新了库存 DB（汰换前/后）→ 主窗口转发给库存页重载，保持两个页面一致
    inventory_refreshed = Signal()

    def __init__(self):
        super().__init__()
        self._selected_plan: Optional[dict] = None
        self._plan_materials: list[dict] = []
        self._match_report = None            # taihuan_executor.MatchReport
        self._match_worker: Optional[PlanMatchWorker] = None
        self._exec_worker: Optional[PlanExecuteWorker] = None
        self._test_worker = None
        self._init_ui()

    # ------------------------------------------------------------
    def _init_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        split = QSplitter(Qt.Orientation.Horizontal)
        split.setHandleWidth(7)
        split.setChildrenCollapsible(False)
        split.addWidget(self._build_left_box())
        split.addWidget(self._build_right_box())
        split.setSizes([380, 760])
        root.addWidget(split, 1)

    # ---- 左：方案列表 + 操作 ----
    def _build_left_box(self) -> QWidget:
        panel = QWidget()
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)

        plan_box = QGroupBox("📋 汰换方案（来自「自动汰换」保存的方案）")
        plan_lay = QVBoxLayout(plan_box)
        self.lst_plans = QListWidget()
        self.lst_plans.itemClicked.connect(self._on_plan_clicked)
        plan_lay.addWidget(self.lst_plans)

        btn_row = QHBoxLayout()
        self.btn_reload_plans = QPushButton("🔄 刷新方案列表")
        self.btn_reload_plans.clicked.connect(self._reload_plans)
        btn_row.addWidget(self.btn_reload_plans)
        self.btn_import = QPushButton("📥 导入 CS2UP TXT")
        self.btn_import.clicked.connect(self._on_import_click)
        btn_row.addWidget(self.btn_import)
        self.btn_match = QPushButton("🔍 匹配库存（先刷新仓库）")
        self.btn_match.setStyleSheet(
            "QPushButton{background:#43A047;color:#fff;font-weight:bold;"
            "padding:6px 12px;}")
        self.btn_match.clicked.connect(self._on_match_click)
        btn_row.addWidget(self.btn_match)
        lay.addWidget(plan_box)
        lay.addLayout(btn_row)

        # 校准参数
        calib_box = QGroupBox("🎯 校准参数")
        calib_lay = QFormLayout(calib_box)
        self.spn_batch = QDoubleSpinBox()
        self.spn_batch.setRange(1, 200)
        self.spn_batch.setDecimals(0)
        self.spn_batch.setValue(5)
        self.spn_batch.setToolTip("每 N 格滚轮（1 格滚轮=2 行物品）后进行一次滑块上移修正")
        calib_lay.addRow("滚轮批大小:", self.spn_batch)
        self.spn_slider_px = QDoubleSpinBox()
        self.spn_slider_px.setRange(0, 500)
        self.spn_slider_px.setDecimals(0)
        self.spn_slider_px.setValue(1)
        calib_lay.addRow("滑块上移像素:", self.spn_slider_px)
        self.spn_delay = QDoubleSpinBox()
        self.spn_delay.setRange(0.1, 10.0)
        self.spn_delay.setDecimals(1)
        self.spn_delay.setSingleStep(0.1)
        self.spn_delay.setValue(1.0)
        self.spn_delay.setSuffix(" 秒")
        calib_lay.addRow("点击间隔:", self.spn_delay)
        lay.addWidget(calib_box)

        # 临时测试（滚轮/滑块像素标定）
        test_box = QGroupBox("🧪 临时测试（滚轮/滑块像素标定）")
        test_box.setStyleSheet(
            "QGroupBox{color:#6A1B9A;font-weight:bold;}")
        test_lay = QFormLayout(test_box)
        wheel_row = QHBoxLayout()
        self.spn_test_ticks = QDoubleSpinBox()
        self.spn_test_ticks.setRange(1, 100)
        self.spn_test_ticks.setDecimals(0)
        self.spn_test_ticks.setValue(8)
        self.spn_test_ticks.setSuffix(" 格")
        wheel_row.addWidget(self.spn_test_ticks)
        self.btn_test_wheel = QPushButton("🧪 滚轮测试")
        self.btn_test_wheel.setToolTip(
            "滚动指定格数滚轮，实测滑块位移像素（每格像素量）")
        self.btn_test_wheel.clicked.connect(
            lambda: self._start_temp_test("wheel"))
        wheel_row.addWidget(self.btn_test_wheel)
        test_lay.addRow("滚轮测试:", wheel_row)

        slider_row = QHBoxLayout()
        self.spn_test_px = QDoubleSpinBox()
        self.spn_test_px.setRange(1, 500)
        self.spn_test_px.setDecimals(0)
        self.spn_test_px.setValue(20)
        self.spn_test_px.setSuffix(" 像素")
        slider_row.addWidget(self.spn_test_px)
        self.btn_test_slider = QPushButton("🧪 滑块测试")
        self.btn_test_slider.setToolTip(
            "请求滑块上移指定像素，实测滑块真实位移")
        self.btn_test_slider.clicked.connect(
            lambda: self._start_temp_test("slider"))
        slider_row.addWidget(self.btn_test_slider)
        test_lay.addRow("滑块测试:", slider_row)

        self.btn_test_detect = QPushButton("📍 检测滑块当前位置")
        self.btn_test_detect.setToolTip("报告当前滑块屏幕坐标（不做任何操作）")
        self.btn_test_detect.clicked.connect(
            lambda: self._start_temp_test("detect"))
        test_lay.addRow("", self.btn_test_detect)
        lay.addWidget(test_box)

        # 执行
        exec_box = QGroupBox("▶ 执行汰换")
        exec_lay = QVBoxLayout(exec_box)
        exec_btn_row = QHBoxLayout()
        self.btn_execute = QPushButton("▶ 执行汰换（点击确认的物品）")
        self.btn_execute.setStyleSheet(
            "QPushButton{background:#2196F3;color:#fff;font-weight:bold;"
            "padding:8px 16px;}")
        self.btn_execute.setEnabled(False)
        self.btn_execute.clicked.connect(self._on_execute_click)
        exec_btn_row.addWidget(self.btn_execute)
        self.btn_stop = QPushButton("停止")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self._on_stop_click)
        exec_btn_row.addWidget(self.btn_stop)
        exec_lay.addLayout(exec_btn_row)
        self.progress = QProgressBar()
        self.progress.setFormat("执行进度 %p%")
        exec_lay.addWidget(self.progress)
        lay.addWidget(exec_box)

        lay.addStretch()
        return panel

    # ---- 右：匹配报告表 + 日志 ----
    def _build_right_box(self) -> QWidget:
        panel = QWidget()
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)

        report_box = QGroupBox("🧩 匹配报告（高磨损先用；磨损偏差>0.2 待定；缺口待定）")
        report_lay = QVBoxLayout(report_box)

        self.lbl_summary = QLabel("未匹配。选择方案后点击「匹配库存」。")
        self.lbl_summary.setStyleSheet(
            "font-weight:bold;color:#333;font-size:12px;")
        report_lay.addWidget(self.lbl_summary)

        self.tbl_match = QTableWidget(0, 7)
        self.tbl_match.setHorizontalHeaderLabels(
            ["槽位", "方案材料", "设定磨损区间", "状态",
             "选中物品(asset_id)", "实际磨损", "磨损差距"])
        hdr = self.tbl_match.horizontalHeader()
        hdr.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.tbl_match.setColumnWidth(0, 44)
        self.tbl_match.setColumnWidth(1, 170)
        self.tbl_match.setColumnWidth(2, 130)
        self.tbl_match.setColumnWidth(3, 110)
        self.tbl_match.setColumnWidth(4, 170)
        self.tbl_match.setColumnWidth(5, 90)
        self.tbl_match.setColumnWidth(6, 80)
        self.tbl_match.setEditTriggers(
            QTableWidget.EditTrigger.NoEditTriggers)
        self.tbl_match.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows)
        report_lay.addWidget(self.tbl_match, 1)

        confirm_row = QHBoxLayout()
        self.btn_confirm_deviation = QPushButton("⚠ 同意所有磨损偏差槽位")
        self.btn_confirm_deviation.setEnabled(False)
        self.btn_confirm_deviation.clicked.connect(
            self._on_confirm_deviation)
        confirm_row.addWidget(self.btn_confirm_deviation)
        self.chk_auto_agree = QCheckBox("磨损偏差自动同意（不推荐）")
        confirm_row.addWidget(self.chk_auto_agree)
        confirm_row.addStretch()
        report_lay.addLayout(confirm_row)
        lay.addWidget(report_box, 3)

        log_box = QGroupBox("🖥 系统日志（可选中复制，右键全选复制）")
        log_lay = QVBoxLayout(log_box)
        # QPlainTextEdit：支持鼠标选中复制 + Ctrl+A 全选 + 右键菜单
        self.txt_log = QPlainTextEdit()
        self.txt_log.setReadOnly(True)
        self.txt_log.setMaximumBlockCount(5000)   # 防止无限增长占内存
        self.txt_log.setStyleSheet(
            "QPlainTextEdit{font-family:Consolas,Menlo,monospace;"
            "font-size:11px;}")
        log_lay.addWidget(self.txt_log)
        lay.addWidget(log_box, 2)

        # 右键菜单追加"复制全部"
        from PySide6.QtGui import QAction
        self.txt_log.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu)
        def _log_ctx_menu(pos):
            menu = self.txt_log.createStandardContextMenu()
            menu.addSeparator()
            act_all = QAction("复制全部日志", menu)
            act_all.triggered.connect(
                lambda: QApplication.clipboard().setText(
                    self.txt_log.toPlainText()))
            menu.addAction(act_all)
            menu.exec(self.txt_log.mapToGlobal(pos))
        self.txt_log.customContextMenuRequested.connect(_log_ctx_menu)

        return panel

    # ------------------------------------------------------------
    # 方案加载
    # ------------------------------------------------------------
    def _reload_plans(self):
        self.lst_plans.clear()
        try:
            plans = recipe_matcher.list_saved_plans()
        except Exception as e:
            logger.warning("加载方案列表失败: %s", e)
            return
        for p in plans:
            if not isinstance(p, dict):
                continue
            mdata = p.get("materials_data") or {}
            mats = mdata.get("materials", []) if isinstance(mdata, dict) else []
            display = (f"[{p.get('id','?')}] {p.get('plan_name','')} "
                       f"(材料{len(mats)}件) - {p.get('status','')}")
            item = QListWidgetItem(display)
            item.setData(Qt.ItemDataRole.UserRole, p)
            self.lst_plans.addItem(item)

    def _on_import_click(self):
        """导入 CS2UP 一键炼金导出的 TXT 配方文件。"""
        path, _ = QFileDialog.getOpenFileName(
            self, "选择 CS2UP 导出的 TXT 配方文件",
            "", "文本文件 (*.txt);;所有文件 (*.*)")
        if not path:
            return
        try:
            n, lines = recipe_matcher.import_cs2up_txt(path)
        except Exception as e:
            logger.exception("导入失败")
            QMessageBox.critical(self, "导入失败", str(e))
            return
        if not n:
            QMessageBox.warning(self, "导入结果",
                                "未识别到任何配方（请确认是 CS2UP 一键炼金的导出格式）")
            return
        QMessageBox.information(
            self, "导入成功",
            f"成功导入 {n} 个配方：\n\n" + "\n".join(lines[:10])
            + ("\n..." if len(lines) > 10 else ""))
        self._reload_plans()

    def _on_plan_clicked(self, item: QListWidgetItem):
        plan = item.data(Qt.ItemDataRole.UserRole)
        if not plan:
            return
        self._selected_plan = plan
        mdata = plan.get("materials_data") or {}
        materials = mdata.get("materials", []) if isinstance(mdata, dict) else []
        self._plan_materials = [dict(m) for m in materials]
        self._log(f"📂 已选方案: {plan.get('plan_name')}"
                  f"（材料 {len(self._plan_materials)} 件）")
        self.btn_match.setEnabled(True)

    # ------------------------------------------------------------
    # 匹配
    # ------------------------------------------------------------
    def _on_match_click(self):
        if not self._plan_materials:
            QMessageBox.warning(self, "提示", "请先在左侧选择一个汰换方案")
            return
        self.btn_match.setEnabled(False)
        self.btn_execute.setEnabled(False)
        self._match_report = None
        self._log("🔍 开始匹配（汰换前先刷新仓库）...")
        self._match_worker = PlanMatchWorker(self._plan_materials)
        self._match_worker.log_message.connect(self._log)
        self._match_worker.match_done.connect(self._on_match_done)
        self._match_worker.error.connect(self._on_match_error)
        self._match_worker.start()

    def _on_match_done(self, report):
        self._match_report = report
        self.btn_match.setEnabled(True)
        self.inventory_refreshed.emit()   # 匹配前刷新了库存 → 通知库存页重载
        self.lbl_summary.setText(
            f"{report.summary}   （缺口槽位等仓库到货后重新匹配）")
        self._populate_match_table(report)
        has_deviation = bool(report.wear_deviation_slots)
        self.btn_confirm_deviation.setEnabled(has_deviation)
        # 全部 ok 且无需确认 → 可直接执行
        if report.can_execute:
            self.btn_execute.setEnabled(True)
            self._log("✓ 全部槽位就绪，可直接执行汰换")
        elif has_deviation and self.chk_auto_agree.isChecked():
            self._on_confirm_deviation()

    def _on_match_error(self, msg: str):
        self.btn_match.setEnabled(True)
        self._log(f"✗ {msg}")

    def _populate_match_table(self, report):
        tbl = self.tbl_match
        tbl.setRowCount(0)
        for sm in report.all_slots:
            r = tbl.rowCount()
            tbl.insertRow(r)
            tbl.setItem(r, 0, QLabel_item(str(sm.slot_index + 1)))
            tbl.setItem(r, 1, QLabel_item(sm.slot_name))
            tbl.setItem(r, 2, QLabel_item(
                f"{sm.wear_min:.4f} ~ {sm.wear_max:.4f}"))
            st = QLabel_item(_STATUS_TEXT.get(sm.status, sm.status))
            st.setForeground(Qt.GlobalColor.white)
            from PySide6.QtGui import QColor
            st.setBackground(QColor(_STATUS_COLOR.get(sm.status, "#757575")))
            tbl.setItem(r, 3, st)
            if sm.status == "ok":
                tbl.setItem(r, 4, QLabel_item(
                    f"{sm.chosen_hash_name} ({sm.chosen_asset_id[:10]}…)"))
                tbl.setItem(r, 5, QLabel_item(f"{sm.chosen_wear:.6f}"))
                tbl.setItem(r, 6, QLabel_item("—"))
            elif sm.status == "wear_deviation":
                tbl.setItem(r, 4, QLabel_item(
                    f"{sm.best_asset_id[:10]}…（待确认）"))
                tbl.setItem(r, 5, QLabel_item(f"{sm.best_wear:.6f}"))
                tbl.setItem(r, 6, QLabel_item(f"{sm.best_gap:.4f}"))
            else:
                tbl.setItem(r, 4, QLabel_item(sm.missing_reason))
                tbl.setItem(r, 5, QLabel_item("—"))
                tbl.setItem(r, 6, QLabel_item("—"))

    # ------------------------------------------------------------
    # 待定确认
    # ------------------------------------------------------------
    def _on_confirm_deviation(self):
        """人工同意所有磨损偏差槽位：best → chosen，状态改 ok。"""
        if not self._match_report:
            return
        n = 0
        for sm in self._match_report.wear_deviation_slots:
            # 重新按 asset_id 找库存信息填 chosen
            from core import taihuan_executor
            inv = {i["asset_id"]: i for i in
                   taihuan_executor.load_inventory_from_db()}
            best = inv.get(sm.best_asset_id)
            if not best:
                continue
            sm.status = "ok"
            sm.chosen_asset_id = sm.best_asset_id
            sm.chosen_hash_name = best["hash_name"]
            sm.chosen_wear = sm.best_wear
            n += 1
        if n:
            # 重算报告分类
            dev = [s for s in self._match_report.wear_deviation_slots
                   if s.status == "ok"]
            self._match_report.ok_slots.extend(dev)
            self._match_report.wear_deviation_slots = [
                s for s in self._match_report.wear_deviation_slots
                if s.status != "ok"]
            self._populate_match_table(self._match_report)
            self._log(f"✓ 已人工同意 {n} 个磨损偏差槽位")
            self.btn_confirm_deviation.setEnabled(False)
            if self._match_report.can_execute:
                self.btn_execute.setEnabled(True)
                self._log("✓ 全部槽位就绪，可执行汰换")

    # ------------------------------------------------------------
    # 执行
    # ------------------------------------------------------------
    def _on_execute_click(self):
        if not self._match_report:
            return
        confirmed = [sm for sm in self._match_report.all_slots
                     if sm.status == "ok"]
        if not confirmed:
            QMessageBox.warning(self, "提示", "没有已确认的槽位可执行")
            return
        if self._match_report.missing_slots:
            QMessageBox.warning(
                self, "提示",
                f"存在 {len(self._match_report.missing_slots)} 个缺口槽位，"
                "等仓库到货后重新匹配再执行")
            return
        names = "\n".join(
            f"  槽位{sm.slot_index+1}: {sm.chosen_hash_name}"
            f"（磨损 {sm.chosen_wear:.6f}）"
            for sm in confirmed)
        if QMessageBox.question(
                self, "确认执行",
                f"已确认 {len(confirmed)} 件物品：\n{names}\n\n"
                "流程：右键仓库最上物品 → 左键弹窗「汰换」按钮 →\n"
                "在 4 列过滤视图逐件左键添加，满 10 件自动开始汰换。\n"
                "执行期间请勿移动鼠标/切换窗口。确定？"
        ) != QMessageBox.StandardButton.Yes:
            return

        # 校准参数
        from utils.config import get_game_interact_config
        cfg = get_game_interact_config()
        cfg.BATCH_SIZE = int(self.spn_batch.value())
        cfg.SLIDER_ADJUST_PIXEL = int(self.spn_slider_px.value())

        from core import inventory_fetcher
        self.btn_execute.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.progress.setValue(0)
        self._exec_worker = PlanExecuteWorker(
            confirmed_slots=confirmed,
            csv_path=inventory_fetcher.INVENTORY_CSV_PATH,
            inter_item_delay=self.spn_delay.value(),
            # 完整流程（2026-09-02）：右键种子 → 左键弹窗「汰换」按钮
            # → 4 列过滤视图逐件左键添加，满 10 件自动开始汰换
            seed_pre_added=True,
        )
        self._exec_worker.item_start.connect(
            lambda n, i, t: self._log(f"  [{i}/{t}] {n}"))
        self._exec_worker.item_done.connect(
            lambda r: self._log(f"    {'✓' if r.success else '✗'} {r.message}"))
        self._exec_worker.scroll_progress.connect(
            lambda e, t: self.progress.setValue(int(e / max(t, 1) * 100)))
        self._exec_worker.log_message.connect(self._log)
        self._exec_worker.finished_ok.connect(self._on_exec_done)
        self._exec_worker.post_refresh_done.connect(self._on_post_refresh)
        self._exec_worker.error.connect(self._on_exec_error)
        self._exec_worker.start()

    def _on_post_refresh(self, count: int):
        self._log(f"📦 汰换后库存 {count} 件")
        self.inventory_refreshed.emit()   # 汰换后刷新了库存 → 通知库存页重载

    def on_inventory_changed_elsewhere(self):
        """库存页（或其他页面）刷新了库存 DB → 本页旧匹配报告作废。

        库存已变化，旧匹配的 asset_id 可能已不存在/位置已变，
        执行前必须重新「匹配库存」。
        """
        if self._exec_worker and self._exec_worker.isRunning():
            return   # 正在执行中不打扰（执行内部每件点击前都会重新定位）
        if self._match_report is not None:
            self._match_report = None
            self.btn_execute.setEnabled(False)
            self.btn_confirm_deviation.setEnabled(False)
            self.lbl_summary.setText(
                "⚠ 库存已在外部刷新，原匹配结果已作废——请重新「匹配库存」")
            self._log("⚠ 库存已更新（其他页面刷新），原匹配报告作废，"
                      "执行前请重新匹配")

    def _on_exec_done(self, results: list):
        self.btn_execute.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.progress.setValue(100)
        ok = sum(1 for r in results if r.success)
        self._log(f"✓ 汰换执行完成：成功 {ok}/{len(results)}")

    def _on_exec_error(self, msg: str):
        self.btn_execute.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self._log(f"✗ {msg}")
        QMessageBox.critical(self, "错误", msg)

    def _on_stop_click(self):
        if self._exec_worker and self._exec_worker.isRunning():
            self._exec_worker.stop()
            self._log("⏹ 已请求停止")
        if self._test_worker and self._test_worker.isRunning():
            self._test_worker.stop()
            self._log("⏹ 已请求停止临时测试")

    # ------------------------------------------------------------    
    def _start_temp_test(self, mode: str):
        """启动临时测试（滚轮/滑块像素标定），后台线程执行。"""
        if self._test_worker and self._test_worker.isRunning():
            self._log("⚠ 临时测试正在进行中，请等待完成")
            return
        from gui.taihuan_worker import TempTestWorker
        self._set_test_buttons_enabled(False)
        if mode == "wheel":
            self._log(f"🧪 滚轮测试：滚动 {int(self.spn_test_ticks.value())} 格"
                      "（请勿移动鼠标，观察游戏内仓库滚动）...")
        elif mode == "slider":
            self._log(f"🧪 滑块测试：请求上移 {int(self.spn_test_px.value())}"
                      " 像素（请勿移动鼠标）...")
        else:
            self._log("📍 检测滑块当前位置...")
        self._test_worker = TempTestWorker(
            mode,
            ticks=int(self.spn_test_ticks.value()),
            slider_px=int(self.spn_test_px.value()))
        self._test_worker.log_message.connect(self._log)
        self._test_worker.finished_ok.connect(
            lambda: self._set_test_buttons_enabled(True))
        self._test_worker.error.connect(self._on_temp_test_error)
        self._test_worker.start()

    def _set_test_buttons_enabled(self, enabled: bool):
        for btn in (self.btn_test_wheel, self.btn_test_slider,
                    self.btn_test_detect):
            btn.setEnabled(enabled)

    def _on_temp_test_error(self, msg: str):
        self._set_test_buttons_enabled(True)
        self._log(f"✗ {msg}")

    # ------------------------------------------------------------
    def _log(self, msg: str):
        # QPlainTextEdit 追加一行并滚动到底
        self.txt_log.appendPlainText(msg)

    def stop_if_running(self):
        for w in (getattr(self, "_match_worker", None),
                  getattr(self, "_exec_worker", None),
                  getattr(self, "_test_worker", None)):
            if w and w.isRunning():
                w.stop() if hasattr(w, "stop") else None
                w.wait(3000)

    def showEvent(self, event):
        super().showEvent(event)
        if self.lst_plans.count() == 0:
            self._reload_plans()


def QLabel_item(text: str):
    """构造只读单元格 item（模块内小工具）。"""
    from PySide6.QtWidgets import QTableWidgetItem
    it = QTableWidgetItem(text)
    it.setFlags(it.flags() & ~Qt.ItemFlag.ItemIsEditable)
    return it
