"""查询列表面板：物品排队、串行查询、展示所有商品明细、10分钟自动清理。"""
from datetime import datetime

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFormLayout, QGridLayout,
    QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox,
    QPlainTextEdit, QPushButton, QSplitter, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from core import query_queue_manager as qqm
from core.data_manager import load_items_from_main_csv, load_items
from gui.workers import QueryQueueWorker


QUEUE_COLS = ["状态", "物品名称", "Buff ID", "C5 哈希名",
              "样本数", "最低价", "最低磨损", "入队时间",
              "完成时间", "剩余保留"]

DETAIL_COLS = ["平台", "订单ID", "磨损值", "磨损等级", "价格", "paintseed", "assetid"]

# 状态显示文本
STATUS_TEXT = {
    qqm.STATUS_PENDING: "⏳ 等待",
    qqm.STATUS_RUNNING: "🔄 查询中",
    qqm.STATUS_DONE:    "✅ 完成",
    qqm.STATUS_ERROR:   "❌ 失败",
}


class AddQueueDialog(QDialog):
    """添加物品到查询队列的对话框。"""

    def __init__(self, parent=None, existing_items=None):
        super().__init__(parent)
        self.setWindowTitle("添加物品到查询队列")
        self.setMinimumWidth(460)
        self._all_items = existing_items or []

        form = QFormLayout(self)
        self.name_combo = QComboBox()
        self.name_combo.setEditable(True)
        self.name_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        for it in self._all_items:
            self.name_combo.addItem(it["item_name"], it)
        comp = self.name_combo.completer()
        comp.setCompletionMode(comp.CompletionMode.PopupCompletion)
        comp.setFilterMode(Qt.MatchFlag.MatchContains)
        self.name_combo.currentIndexChanged.connect(self._on_pick)
        form.addRow("选择物品 *", self.name_combo)

        self.buff_edit = QLineEdit()
        form.addRow("Buff goods_id", self.buff_edit)
        self.c5_edit = QLineEdit()
        form.addRow("C5 market_hash_name", self.c5_edit)
        self.c5_app_edit = QLineEdit("730")
        form.addRow("C5 app_id", self.c5_app_edit)

        btns = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        form.addRow(btns)

    def _on_pick(self, idx):
        data = self.name_combo.itemData(idx)
        if not data:
            return
        self.buff_edit.setText(data.get("buff_goods_id", ""))
        self.c5_edit.setText(data.get("c5_market_hash_name", ""))
        self.c5_app_edit.setText(data.get("c5_app_id", "730"))

    def get_values(self):
        data = self.name_combo.currentData()
        text = self.name_combo.currentText().strip()
        if data and data.get("item_name") == text:
            return {
                "item_name": data["item_name"],
                "buff_goods_id": data.get("buff_goods_id", ""),
                "c5_market_hash_name": data.get("c5_market_hash_name", ""),
                "c5_app_id": data.get("c5_app_id", "730"),
            }
        return {
            "item_name": text,
            "buff_goods_id": self.buff_edit.text().strip(),
            "c5_market_hash_name": self.c5_edit.text().strip(),
            "c5_app_id": self.c5_app_edit.text().strip() or "730",
        }


class QueryQueuePanel(QWidget):
    """查询列表标签页。"""

    def __init__(self):
        super().__init__()
        self._worker = None
        self._all_items = list(load_items_from_main_csv()) + list(load_items())
        # 启动时把上次中断的 running 重置为 pending
        qqm.reset_running_to_pending()
        # 立即清理一次过期记录
        qqm.cleanup_expired()
        # 定时器：每 60 秒清理一次过期记录 + 刷新表格剩余时间
        self._cleanup_timer = QTimer(self)
        self._cleanup_timer.timeout.connect(self._on_cleanup_tick)
        self._cleanup_timer.start(60 * 1000)
        self._build_ui()
        self._reload_queue()

    # ---------- UI ----------

    def _build_ui(self):
        root = QVBoxLayout(self)

        # --- 配置区 ---
        cfg_grp = QGroupBox("队列配置")
        grid = QGridLayout(cfg_grp)

        grid.addWidget(QLabel("查询平台:"), 0, 0)
        self.platform_combo = QComboBox()
        self.platform_combo.addItems(["全部", "仅Buff", "仅C5"])
        grid.addWidget(self.platform_combo, 0, 1)

        self.btn_start = QPushButton("▶ 开始处理队列")
        self.btn_start.clicked.connect(self._on_start)
        grid.addWidget(self.btn_start, 0, 2)

        self.btn_stop = QPushButton("⏸ 停止处理")
        self.btn_stop.clicked.connect(self._on_stop)
        self.btn_stop.setEnabled(False)
        grid.addWidget(self.btn_stop, 0, 3)

        root.addWidget(cfg_grp)

        # --- 操作按钮 ---
        btn_row = QHBoxLayout()
        self.btn_add = QPushButton("➕ 添加物品到队列")
        self.btn_add.clicked.connect(self._on_add)
        btn_row.addWidget(self.btn_add)

        self.btn_remove = QPushButton("➖ 移除选中")
        self.btn_remove.clicked.connect(self._on_remove)
        btn_row.addWidget(self.btn_remove)

        self.btn_clear_done = QPushButton("🗑 清除已完成")
        self.btn_clear_done.clicked.connect(self._on_clear_done)
        btn_row.addWidget(self.btn_clear_done)

        self.btn_clear_all = QPushButton("清空队列")
        self.btn_clear_all.clicked.connect(self._on_clear_all)
        btn_row.addWidget(self.btn_clear_all)

        btn_row.addStretch()
        self.lbl_count = QLabel("队列：0 项")
        btn_row.addWidget(self.lbl_count)
        root.addLayout(btn_row)

        # --- 队列表格 + 商品明细 ---
        splitter = QSplitter(Qt.Orientation.Vertical)

        # 上：队列状态表
        top_widget = QWidget()
        top_layout = QVBoxLayout(top_widget)
        top_layout.setContentsMargins(0, 0, 0, 0)
        top_layout.addWidget(QLabel("查询队列（点击行查看商品明细）:"))
        self.queue_table = QTableWidget(0, len(QUEUE_COLS))
        self.queue_table.setHorizontalHeaderLabels(QUEUE_COLS)
        self.queue_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch)
        self.queue_table.verticalHeader().setVisible(False)
        self.queue_table.setAlternatingRowColors(True)
        self.queue_table.setEditTriggers(
            QTableWidget.EditTrigger.NoEditTriggers)
        self.queue_table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows)
        self.queue_table.itemSelectionChanged.connect(self._on_queue_row_changed)
        top_layout.addWidget(self.queue_table)
        splitter.addWidget(top_widget)

        # 下：商品明细表
        bottom_widget = QWidget()
        bottom_layout = QVBoxLayout(bottom_widget)
        bottom_layout.setContentsMargins(0, 0, 0, 0)
        bottom_layout.addWidget(QLabel("商品明细（选中队列中已完成的物品）:"))
        self.detail_table = QTableWidget(0, len(DETAIL_COLS))
        self.detail_table.setHorizontalHeaderLabels(DETAIL_COLS)
        self.detail_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch)
        self.detail_table.verticalHeader().setVisible(False)
        self.detail_table.setAlternatingRowColors(True)
        self.detail_table.setEditTriggers(
            QTableWidget.EditTrigger.NoEditTriggers)
        bottom_layout.addWidget(self.detail_table)
        splitter.addWidget(bottom_widget)

        splitter.setSizes([300, 300])
        root.addWidget(splitter, 1)

        # --- 日志区 ---
        root.addWidget(QLabel("实时日志:"))
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumHeight(120)
        root.addWidget(self.log_view)

        # --- 状态栏 ---
        self.status = QLabel("就绪。")
        root.addWidget(self.status)

    # ---------- 队列管理 ----------

    def _reload_queue(self):
        items = qqm.list_items()
        self.queue_table.setRowCount(len(items))
        now = datetime.now()
        for r, it in enumerate(items):
            status = it["status"]
            status_text = STATUS_TEXT.get(status, status)
            status_item = QTableWidgetItem(status_text)
            status_item.setData(Qt.ItemDataRole.UserRole, it["id"])
            # 不同状态用不同颜色
            if status == qqm.STATUS_RUNNING:
                status_item.setForeground(Qt.GlobalColor.blue)
            elif status == qqm.STATUS_DONE:
                status_item.setForeground(Qt.GlobalColor.darkGreen)
            elif status == qqm.STATUS_ERROR:
                status_item.setForeground(Qt.GlobalColor.red)
            self.queue_table.setItem(r, 0, status_item)

            self.queue_table.setItem(r, 1, QTableWidgetItem(it["item_name"]))
            self.queue_table.setItem(r, 2, QTableWidgetItem(it.get("buff_goods_id") or ""))
            self.queue_table.setItem(r, 3, QTableWidgetItem(it.get("c5_market_hash_name") or ""))
            self.queue_table.setItem(
                r, 4, QTableWidgetItem(str(it.get("result_count") or 0)))

            mp = it.get("min_price")
            self.queue_table.setItem(
                r, 5, QTableWidgetItem(f"¥{mp:.2f}" if mp is not None else "-"))
            mw = it.get("min_wear")
            self.queue_table.setItem(
                r, 6, QTableWidgetItem(f"{mw:.17f}" if mw is not None else "-"))

            self.queue_table.setItem(r, 7, QTableWidgetItem(it.get("added_at") or "-"))
            self.queue_table.setItem(r, 8, QTableWidgetItem(it.get("finished_at") or "-"))

            # 剩余保留时间
            remain = "-"
            if status in (qqm.STATUS_DONE, qqm.STATUS_ERROR) and it.get("finished_at"):
                try:
                    finished = datetime.fromisoformat(it["finished_at"])
                    elapsed_sec = (now - finished).total_seconds()
                    remain_sec = qqm.AUTO_CLEANUP_MINUTES * 60 - elapsed_sec
                    if remain_sec > 0:
                        remain = f"{int(remain_sec // 60)} 分 {int(remain_sec % 60)} 秒"
                    else:
                        remain = "即将清理"
                except Exception:
                    remain = "-"
            self.queue_table.setItem(r, 9, QTableWidgetItem(remain))
        self.lbl_count.setText(f"队列：{len(items)} 项")
        # 保持当前选中行的明细
        self._refresh_detail_for_current()

    def _on_add(self):
        dlg = AddQueueDialog(self, existing_items=self._all_items)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        vals = dlg.get_values()
        if not vals["item_name"]:
            QMessageBox.warning(self, "提示", "物品名称不能为空。")
            return
        if not vals["buff_goods_id"] and not vals["c5_market_hash_name"]:
            QMessageBox.warning(self, "提示", "至少需要填一个平台 ID。")
            return
        qqm.add_item(vals)
        self._reload_queue()
        self._append_log(f"已添加到队列: {vals['item_name']}")

    def _on_remove(self):
        row = self.queue_table.currentRow()
        if row < 0:
            QMessageBox.information(self, "提示", "请先选中一行。")
            return
        item = self.queue_table.item(row, 0)
        if not item:
            return
        item_id = item.data(Qt.ItemDataRole.UserRole)
        if item_id is None:
            return
        qqm.remove_item(item_id)
        self._reload_queue()
        self._append_log(f"已移除队列项 id={item_id}")

    def _on_clear_done(self):
        qqm.clear_done()
        self._reload_queue()
        self._append_log("已清除所有已完成/失败的记录。")

    def _on_clear_all(self):
        if QMessageBox.question(
            self, "确认", "确定清空整个队列（包括正在查询的物品）？"
        ) != QMessageBox.StandardButton.Yes:
            return
        qqm.clear_all()
        self._reload_queue()
        self._append_log("已清空队列。")

    # ---------- 启停 ----------

    def _current_platforms(self):
        idx = self.platform_combo.currentIndex()
        if idx == 1:
            return ["buff"]
        if idx == 2:
            return ["c5"]
        return None

    def _on_start(self):
        if self._worker and self._worker.isRunning():
            QMessageBox.information(self, "提示", "队列处理已在运行中。")
            return
        platforms = self._current_platforms()
        self._worker = QueryQueueWorker(platforms=platforms)
        self._worker.progress.connect(self._on_progress)
        self._worker.item_started.connect(self._on_item_started)
        self._worker.item_finished.connect(self._on_item_finished)
        self._worker.queue_empty.connect(self._on_queue_empty)
        self._worker.stopped.connect(self._on_stopped)
        self._worker.start()
        self._set_running(True)
        self._append_log(
            f"▶ 开始处理队列，平台={platforms or '全部'}")

    def _on_stop(self):
        if self._worker and self._worker.isRunning():
            self._append_log("⏸ 正在停止，等待当前物品查询结束 ...")
            self._worker.stop()
        self.btn_stop.setEnabled(False)

    def _on_stopped(self):
        self._set_running(False)
        self._append_log("⏸ 队列处理已停止。")

    # ---------- 回调 ----------

    def _on_progress(self, msg):
        self.status.setText(msg)
        self._append_log(msg)

    def _on_item_started(self, item_id, item_name):
        self._reload_queue()

    def _on_item_finished(self, item_id, item_name, count, success, msg):
        self._reload_queue()

    def _on_queue_empty(self):
        # 队列空时不立刻停止，保持 worker 等待新任务
        pass

    # ---------- 明细表 ----------

    def _on_queue_row_changed(self):
        self._refresh_detail_for_current()

    def _refresh_detail_for_current(self):
        row = self.queue_table.currentRow()
        if row < 0:
            self.detail_table.setRowCount(0)
            return
        item = self.queue_table.item(row, 0)
        if not item:
            self.detail_table.setRowCount(0)
            return
        item_id = item.data(Qt.ItemDataRole.UserRole)
        if item_id is None:
            self.detail_table.setRowCount(0)
            return
        results = qqm.get_result(item_id)
        if not results:
            self.detail_table.setRowCount(0)
            return
        # 可能是错误信息 dict
        if isinstance(results, dict) and "error" in results:
            self.detail_table.setRowCount(1)
            self.detail_table.setColumnCount(1)
            self.detail_table.setHorizontalHeaderLabels(["错误信息"])
            self.detail_table.setItem(0, 0, QTableWidgetItem(results["error"]))
            return
        # 恢复列
        self.detail_table.setColumnCount(len(DETAIL_COLS))
        self.detail_table.setHorizontalHeaderLabels(DETAIL_COLS)
        self.detail_table.setRowCount(len(results))
        for r, order in enumerate(results):
            self.detail_table.setItem(
                r, 0, QTableWidgetItem(order.get("platform", "")))
            self.detail_table.setItem(
                r, 1, QTableWidgetItem(order.get("order_id", "")))
            wear = order.get("wear", 0)
            self.detail_table.setItem(
                r, 2, QTableWidgetItem(f"{float(wear):.17f}"))
            self.detail_table.setItem(
                r, 3, QTableWidgetItem(order.get("wear_name", "")))
            price = order.get("price", 0)
            self.detail_table.setItem(
                r, 4, QTableWidgetItem(f"¥{float(price):.2f}"))
            self.detail_table.setItem(
                r, 5, QTableWidgetItem(order.get("paintseed", "")))
            self.detail_table.setItem(
                r, 6, QTableWidgetItem(order.get("assetid", "")))

    # ---------- 定时清理 ----------

    def _on_cleanup_tick(self):
        deleted = qqm.cleanup_expired()
        if deleted > 0:
            self._append_log(f"⏰ 自动清理了 {deleted} 条过期记录。")
        self._reload_queue()

    # ---------- 工具 ----------

    def _append_log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_view.appendPlainText(f"[{ts}] {msg}")

    def _set_running(self, running: bool):
        self.btn_start.setEnabled(not running)
        self.btn_stop.setEnabled(running)
        self.btn_clear_all.setEnabled(not running)
        self.platform_combo.setEnabled(not running)

    def stop_if_running(self):
        if self._worker and self._worker.isRunning():
            self._worker.stop()
            self._worker.wait(3000)
