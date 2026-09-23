"""价格检测面板：自定义频率 + 持续轮询监控目标的最低价。"""
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QGridLayout,
    QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox,
    QPlainTextEdit, QPushButton, QDoubleSpinBox, QSpinBox, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from core import price_monitor as pm
from core import favorites_manager as fm
from core.data_manager import (
    load_items_from_main_csv, load_items,
    query_price_monitor_history, get_price_history_summary,
    clear_price_monitor_history)
from gui.workers import PriceMonitorWorker


# 频率预设：显示文本 -> 秒数（-1 表示使用自定义输入框的值）
FREQ_PRESETS = [
    ("30 秒", 30),
    ("1 分钟", 60),
    ("2.5 分钟（推荐）", 150),
    ("5 分钟", 300),
    ("15 分钟", 900),
    ("30 分钟", 1800),
    ("1 小时", 3600),
    ("自定义", -1),
]

MONITOR_COLS = ["启用", "材料名称", "磨损区间", "阈值",
                "需求/已购", "当前最低价", "平台", "可买件数", "最近检测", "状态"]

# 列索引
COL_ENABLE, COL_NAME, COL_WEAR, COL_MAXPRICE, COL_NEED, \
    COL_PRICE, COL_PLATFORM, COL_AFFORD, COL_CHECKED, COL_STATUS = \
    range(10)

# 列 tooltip（表头悬停提示）
MONITOR_COLS_TOOLTIP = {
    COL_ENABLE:    "勾选后该目标参与检测；取消后本轮跳过",
    COL_NAME:      "皮肤名称（带磨损档位后缀）",
    COL_WEAR:      "仅检测此磨损区间内的在售（双击行编辑)",
    COL_MAXPRICE:  "价格阈值：在售价格 ≤ 此值时标记为可买\n双击行可编辑；显示「未设置」的材料不会触发自动购买",
    COL_NEED:      "已成功自动购买 / 目标需求件数\n双击行可设置需求量",
    COL_PRICE:     "本轮检测到的跨平台最低价",
    COL_PLATFORM:  "最低价所属平台（buff / c5 / eco）",
    COL_AFFORD:    "本轮检测中，价格 ≤ 阈值 的在售件数\n（同时满足磨损区间过滤）",
    COL_CHECKED:   "最近一次检测完成的时间",
    COL_STATUS:    "最近一次检测的结果（✅ 成功 / ❌ 错误原因）",
}


class AddMonitorDialog(QDialog):
    """添加监控目标的对话框。"""

    def __init__(self, parent=None, existing_items=None):
        super().__init__(parent)
        self.setWindowTitle("添加监控目标")
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

        # 2026-09-12 阈值囤货模式字段
        wear_row = QHBoxLayout()
        self.wear_min_spin = QDoubleSpinBox()
        self.wear_min_spin.setRange(0, 1.0)
        self.wear_min_spin.setDecimals(4)
        self.wear_min_spin.setSpecialValueText("不限")
        wear_row.addWidget(self.wear_min_spin)
        wear_row.addWidget(QLabel("~"))
        self.wear_max_spin = QDoubleSpinBox()
        self.wear_max_spin.setRange(0, 1.0)
        self.wear_max_spin.setDecimals(4)
        self.wear_max_spin.setSpecialValueText("不限")
        wear_row.addWidget(self.wear_max_spin)
        form.addRow("磨损区间:", wear_row)

        self.max_price_spin = QDoubleSpinBox()
        self.max_price_spin.setRange(0, 9999999.99)
        self.max_price_spin.setDecimals(2)
        self.max_price_spin.setSpecialValueText("不限制")
        self.max_price_spin.setPrefix("¥ ")
        form.addRow("价格阈值(≤):", self.max_price_spin)

        self.need_spin = QSpinBox()
        self.need_spin.setRange(0, 999)
        self.need_spin.setValue(0)
        self.need_spin.setSpecialValueText("不自动购买")
        form.addRow("需求件数:", self.need_spin)

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
        base = {
            "buff_goods_id": data.get("buff_goods_id", "") if data else
            self.buff_edit.text().strip(),
            "c5_market_hash_name": data.get("c5_market_hash_name", "") if data
            else self.c5_edit.text().strip(),
            "c5_app_id": (data.get("c5_app_id", "730") if data else
                          self.c5_app_edit.text().strip() or "730"),
        }
        base["item_name"] = (
            data["item_name"] if data and data.get("item_name") == text
            else text)
        # 阈值囤货字段
        wm = self.wear_min_spin.value()
        wx = self.wear_max_spin.value()
        base["wear_min"] = wm if wm > 0 else None
        base["wear_max"] = wx if wx > 0 else None
        mp = self.max_price_spin.value()
        base["max_price"] = mp if mp > 0 else None
        base["need_count"] = self.need_spin.value()
        return base


class EditTargetDialog(QDialog):
    """编辑监控目标（磨损区间/阈值/需求件数）的对话框。"""

    def __init__(self, target: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"编辑监控参数 - {target.get('item_name', '')}")
        self.setMinimumWidth(380)
        form = QFormLayout(self)

        wear_row = QHBoxLayout()
        self.wear_min_spin = QDoubleSpinBox()
        self.wear_min_spin.setRange(0, 1.0)
        self.wear_min_spin.setDecimals(4)
        self.wear_min_spin.setSpecialValueText("不限")
        self.wear_min_spin.setValue(float(target.get("wear_min") or 0))
        wear_row.addWidget(self.wear_min_spin)
        wear_row.addWidget(QLabel("~"))
        self.wear_max_spin = QDoubleSpinBox()
        self.wear_max_spin.setRange(0, 1.0)
        self.wear_max_spin.setDecimals(4)
        self.wear_max_spin.setSpecialValueText("不限")
        self.wear_max_spin.setValue(float(target.get("wear_max") or 0))
        wear_row.addWidget(self.wear_max_spin)
        form.addRow("磨损区间:", wear_row)

        self.max_price_spin = QDoubleSpinBox()
        self.max_price_spin.setRange(0, 9999999.99)
        self.max_price_spin.setDecimals(2)
        self.max_price_spin.setSpecialValueText("不限制")
        self.max_price_spin.setPrefix("¥ ")
        self.max_price_spin.setValue(float(target.get("max_price") or 0))
        form.addRow("价格阈值(≤):", self.max_price_spin)

        self.need_spin = QSpinBox()
        self.need_spin.setRange(0, 999)
        self.need_spin.setValue(int(target.get("need_count") or 0))
        self.need_spin.setSpecialValueText("不自动购买")
        form.addRow("需求件数:", self.need_spin)

        bought = pm.count_bought(target.get("id"))
        form.addRow(QLabel(f"已购 {bought} 件（修改需求量即可补齐/扩囤）"))

        btns = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        form.addRow(btns)

    def get_fields(self) -> dict:
        wm = self.wear_min_spin.value()
        wx = self.wear_max_spin.value()
        mp = self.max_price_spin.value()
        return {
            "wear_min": wm if wm > 0 else None,
            "wear_max": wx if wx > 0 else None,
            "max_price": mp if mp > 0 else None,
            "need_count": self.need_spin.value(),
        }


class PriceMonitorPanel(QWidget):
    """价格检测标签页。"""

    # 自动购买触发：把 best order dict + item_name 发送给购买列表
    auto_buy_triggered = Signal(dict)

    def __init__(self):
        super().__init__()
        self._worker = None          # C5/ECO API 检测线程
        self._worker_buff = None     # Buff 网页爬取检测线程
        # 按 item_name 累积各 worker 传回的平台最低价（合并 buff / c5+eco）
        self._plat_results: dict = {}
        self._all_items = list(load_items_from_main_csv()) + list(load_items())
        self._build_ui()
        self._load_auto_buy_config()
        self._reload_targets()

    # ---------- UI ----------

    def _build_ui(self):
        root = QVBoxLayout(self)

        # --- 检测配置区 ---
        cfg_grp = QGroupBox("检测配置（网页爬取与 API 检测频率独立）")
        grid = QGridLayout(cfg_grp)

        # 行0：Buff 网页爬取频率（Playwright 浏览器查询，慢 + 风控敏感）
        grid.addWidget(QLabel("Buff 网页爬取频率:"), 0, 0)
        self.freq_buff_combo = QComboBox()
        for label, _ in FREQ_PRESETS:
            self.freq_buff_combo.addItem(label)
        self.freq_buff_combo.setCurrentIndex(4)  # 默认 5 分钟（300s）
        self.freq_buff_combo.currentIndexChanged.connect(
            lambda: self._on_freq_changed(self.freq_buff_combo,
                                          self.custom_secs_buff))
        grid.addWidget(self.freq_buff_combo, 0, 1)

        grid.addWidget(QLabel("自定义秒数:"), 0, 2)
        self.custom_secs_buff = QSpinBox()
        self.custom_secs_buff.setRange(30, 86400)
        self.custom_secs_buff.setValue(300)
        self.custom_secs_buff.setEnabled(False)
        grid.addWidget(self.custom_secs_buff, 0, 3)

        # 行1：C5/ECO API 检测频率（接口快）
        grid.addWidget(QLabel("C5/ECO API 频率:"), 1, 0)
        self.freq_api_combo = QComboBox()
        for label, _ in FREQ_PRESETS:
            self.freq_api_combo.addItem(label)
        self.freq_api_combo.setCurrentIndex(2)  # 默认 2.5 分钟（150s）
        self.freq_api_combo.currentIndexChanged.connect(
            lambda: self._on_freq_changed(self.freq_api_combo,
                                          self.custom_secs_api))
        grid.addWidget(self.freq_api_combo, 1, 1)

        grid.addWidget(QLabel("自定义秒数:"), 1, 2)
        self.custom_secs_api = QSpinBox()
        self.custom_secs_api.setRange(5, 86400)
        self.custom_secs_api.setValue(150)
        self.custom_secs_api.setEnabled(False)
        grid.addWidget(self.custom_secs_api, 1, 3)

        grid.addWidget(QLabel("检测平台:"), 2, 0)
        plat_layout = QHBoxLayout()
        self.chk_buff = QCheckBox("BUFF")
        self.chk_c5 = QCheckBox("C5")
        self.chk_eco = QCheckBox("ECO")
        self.chk_buff.setChecked(True)
        self.chk_c5.setChecked(True)
        self.chk_eco.setChecked(True)
        plat_layout.addWidget(self.chk_buff)
        plat_layout.addWidget(self.chk_c5)
        plat_layout.addWidget(self.chk_eco)
        plat_layout.addStretch()
        grid.addLayout(plat_layout, 2, 1, 1, 3)

        self.btn_start = QPushButton("▶ 开始检测")
        self.btn_start.clicked.connect(self._on_start)
        grid.addWidget(self.btn_start, 3, 2)

        self.btn_stop = QPushButton("⏹ 停止检测")
        self.btn_stop.clicked.connect(self._on_stop)
        self.btn_stop.setEnabled(False)
        grid.addWidget(self.btn_stop, 3, 3)

        root.addWidget(cfg_grp)

        # --- 自动购买配置区 ---
        ab_grp = QGroupBox(
            "⚙ 自动购买（每材料阈值/需求量请双击目标表格行编辑；"
            "未设阈值的材料不会触发购买）")
        ab_grid = QGridLayout(ab_grp)

        self.ab_enabled = QCheckBox(
            "启用自动购买（检测到 ≤ 阈值的在售，一轮买齐至需求量）")
        self.ab_enabled.setToolTip(
            "勾选后：每轮检测会检查各材料的阈值，\n"
            "≤ 阈值的在售将自动批量购入（跨平台比价，最多到需求件数）。\n"
            "每材料的阈值/磨损区间/需求件数 → 双击表格行编辑。\n"
            "未设阈值的材料在自动购买时跳过（仅监控）。")
        ab_grid.addWidget(self.ab_enabled, 0, 0, 1, 3)

        self.ab_save_btn = QPushButton("💾 保存配置")
        self.ab_save_btn.clicked.connect(self._on_save_auto_buy)
        ab_grid.addWidget(self.ab_save_btn, 0, 3)
        root.addWidget(ab_grp)

        # --- 监控目标管理 ---
        target_row = QHBoxLayout()
        self.btn_add_target = QPushButton("➕ 添加监控目标")
        self.btn_add_target.clicked.connect(self._on_add_target)
        target_row.addWidget(self.btn_add_target)

        self.btn_remove_target = QPushButton("➖ 移除选中")
        self.btn_remove_target.clicked.connect(self._on_remove_target)
        target_row.addWidget(self.btn_remove_target)

        self.btn_import_fav = QPushButton("📥 从收藏夹导入")
        self.btn_import_fav.clicked.connect(self._on_import_from_favorites)
        target_row.addWidget(self.btn_import_fav)

        self.btn_import_plan = QPushButton("📥 从汰换方案导入（阈值囤货）")
        self.btn_import_plan.clicked.connect(self._on_import_from_plan)
        self.btn_import_plan.setStyleSheet(
            "QPushButton{color:#1a7f37;font-weight:bold;}")
        target_row.addWidget(self.btn_import_plan)

        self.btn_clear_targets = QPushButton("清空目标")
        self.btn_clear_targets.clicked.connect(self._on_clear_targets)
        target_row.addWidget(self.btn_clear_targets)

        self.btn_view_history = QPushButton("📊 查看选中历史")
        self.btn_view_history.clicked.connect(self._on_view_history)
        target_row.addWidget(self.btn_view_history)

        target_row.addStretch()
        self.lbl_target_count = QLabel("0 个目标")
        target_row.addWidget(self.lbl_target_count)
        root.addLayout(target_row)

        # --- 目标表格 ---
        self.table = QTableWidget(0, len(MONITOR_COLS))
        self.table.setHorizontalHeaderLabels(MONITOR_COLS)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        for col_idx, tip in MONITOR_COLS_TOOLTIP.items():
            header.setSectionResizeMode(col_idx,
                                        QHeaderView.ResizeMode.Stretch)
            header_item = self.table.horizontalHeaderItem(col_idx)
            if header_item is not None:
                header_item.setToolTip(tip)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.cellDoubleClicked.connect(self._on_edit_target)
        root.addWidget(self.table, 2)

        # --- 日志区 ---
        root.addWidget(QLabel("实时日志:"))
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumHeight(160)
        root.addWidget(self.log_view)

        # --- 状态栏 ---
        self.status = QLabel("就绪。")
        root.addWidget(self.status)

    # ---------- 频率配置 ----------

    def _on_freq_changed(self, combo=None, secs_spin=None):
        """频率下拉切换（兼容两组：网页爬取 / API）。"""
        if combo is None:
            return
        idx = combo.currentIndex()
        is_custom = (FREQ_PRESETS[idx][1] == -1)
        if secs_spin is not None:
            secs_spin.setEnabled(is_custom)

    def _current_interval_buff(self) -> float:
        idx = self.freq_buff_combo.currentIndex()
        secs = FREQ_PRESETS[idx][1]
        if secs == -1:
            secs = self.custom_secs_buff.value()
        return float(max(30, secs))   # 网页爬取最低 30s（节流+页面加载）

    def _current_interval_api(self) -> float:
        idx = self.freq_api_combo.currentIndex()
        secs = FREQ_PRESETS[idx][1]
        if secs == -1:
            secs = self.custom_secs_api.value()
        return float(max(5, secs))

    def _current_platforms(self):
        """根据三个复选框返回选中的平台列表；全选时返回 None（表示全部）。"""
        plats = []
        if self.chk_buff.isChecked():
            plats.append("buff")
        if self.chk_c5.isChecked():
            plats.append("c5")
        if self.chk_eco.isChecked():
            plats.append("eco")
        if not plats:
            return []
        if len(plats) == 3:
            return None
        return plats

    # ---------- 目标管理 ----------

    def _reload_targets(self):
        targets = pm.list_targets()
        self.table.setRowCount(len(targets))
        for r, t in enumerate(targets):
            enabled = bool(t.get("enabled", True))
            chk = QTableWidgetItem()
            chk.setFlags(chk.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            chk.setCheckState(
                Qt.CheckState.Checked if enabled else Qt.CheckState.Unchecked)
            chk.setData(Qt.ItemDataRole.UserRole, t["id"])
            self.table.setItem(r, COL_ENABLE, chk)

            name_item = QTableWidgetItem(t["item_name"])
            name_item.setToolTip(
                f"Buff: {t.get('buff_goods_id') or '-'}\n"
                f"C5: {t.get('c5_market_hash_name') or '-'}"
                + (f"\n来源方案: #{t.get('plan_id')}" if t.get("plan_id") else ""))
            self.table.setItem(r, COL_NAME, name_item)

            # 磨损区间
            wm, wx = t.get("wear_min"), t.get("wear_max")
            wear_text = (f"{wm:.4f}~{wx:.4f}"
                         if wm is not None and wx is not None else "不限")
            self.table.setItem(r, COL_WEAR, QTableWidgetItem(wear_text))

            # 阈值
            mp = t.get("max_price")
            if mp:
                self.table.setItem(
                    r, COL_MAXPRICE,
                    QTableWidgetItem(f"¥{mp:.2f}"))
            else:
                tip = QTableWidgetItem("未设置")
                tip.setForeground(Qt.GlobalColor.darkGray)
                tip.setToolTip(
                    "双击此行编辑价格阈值；"
                    "未设阈值时自动购买会跳过此材料（仅监控）")
                self.table.setItem(r, COL_MAXPRICE, tip)

            # 需求/已购
            need = t.get("need_count") or 0
            bought = pm.count_bought(t["id"])
            need_text = (f"{bought}/{need}" if need > 0 else "不自动购买")
            need_item = QTableWidgetItem(need_text)
            if need > 0 and bought >= need:
                need_item.setForeground(Qt.GlobalColor.green)
            self.table.setItem(r, COL_NEED, need_item)

            # 从历史表读取该物品最近一次检测结果，填充结果列
            rows = query_price_monitor_history(t["item_name"], limit=1)
            if rows:
                row = rows[0]
                # 列序：id, timestamp, item_name, platform, min_price,
                #       min_wear, sample_count, remark
                ts = row[1] or "-"
                platform = row[3] or "-"
                min_price = row[4]
                remark = row[7] or ""
                price_text = (f"¥{min_price:.2f}"
                              if min_price is not None else "-")
                self.table.setItem(r, COL_PRICE, QTableWidgetItem(price_text))
                self.table.setItem(r, COL_PLATFORM, QTableWidgetItem(platform))
                self.table.setItem(r, COL_CHECKED, QTableWidgetItem(ts))
                self.table.setItem(
                    r, COL_STATUS, QTableWidgetItem(remark[:30] or "-"))
            else:
                for c in (COL_PRICE, COL_PLATFORM, COL_AFFORD,
                          COL_CHECKED, COL_STATUS):
                    self.table.setItem(r, c, QTableWidgetItem("-"))
            self.table.setItem(r, COL_AFFORD, QTableWidgetItem("-"))
        self.lbl_target_count.setText(f"{len(targets)} 个目标")

    def _on_edit_target(self, row: int, col: int):
        """双击行 → 编辑该目标的磨损区间/阈值/需求件数。"""
        chk_item = self.table.item(row, COL_ENABLE)
        if not chk_item:
            return
        target_id = chk_item.data(Qt.ItemDataRole.UserRole)
        targets = [t for t in pm.list_targets() if t["id"] == target_id]
        if not targets:
            return
        dlg = EditTargetDialog(targets[0], self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        pm.update_target_fields(target_id, **dlg.get_fields())
        self._reload_targets()
        self._append_log("💾 已更新监控参数。")

    def _on_import_from_plan(self):
        """从汰换方案一键导入材料（阈值囤货模式）。"""
        plans = pm.list_taihuan_plans()
        if not plans:
            QMessageBox.information(
                self, "提示", "暂无保存的汰换方案（先在自动汰换页保存方案）。")
            return
        # 方案选择对话框（带套数选择）
        from PySide6.QtWidgets import QSpinBox as _SB
        dlg = QDialog(self)
        dlg.setWindowTitle("从汰换方案导入（阈值囤货）")
        dlg.setMinimumWidth(520)
        lay = QGridLayout(dlg)
        lay.addWidget(QLabel("选择方案:"), 0, 0)
        combo = QComboBox()
        for p in plans:
            label = f"#{p['id']} {p['plan_name'] or p['target_item']} [{p['status']}]"
            combo.addItem(label, p["id"])
        lay.addWidget(combo, 0, 1, 1, 2)
        lay.addWidget(QLabel("每槽位需求套数:"), 1, 0)
        sets_spin = _SB()
        sets_spin.setRange(1, 10)
        sets_spin.setValue(1)
        lay.addWidget(sets_spin, 1, 1)
        tip = QLabel("同名材料自动合并需求量；导入后双击表格行设置每材料价格阈值。")
        tip.setStyleSheet("color:#666;")
        lay.addWidget(tip, 2, 0, 1, 3)
        btns = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        lay.addWidget(btns, 3, 0, 1, 3)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        plan_id = combo.currentData()
        sets = sets_spin.value()
        # 从主 CSV 补充 buff_goods_id / c5_hash_name 元数据。
        # 主 CSV item_name 带 " - 档位" 后缀 → 按 (皮肤名, 档位) 精确映射：
        # 同皮肤各档位 buff_goods_id 与 C5 带后缀哈希名均不同（如 FN/BS 各一个
        # goods_id），必须按档位取，防止错档查询。
        _grades = ("崭新出厂", "略有磨损", "久经沙场", "破损不堪", "战痕累累")
        name_meta: dict = {}
        for it in (self._all_items or []):
            nm = (it.get("item_name") or "").strip()
            grade = ""
            if " - " in nm:
                base, _, suffix = nm.rpartition(" - ")
                if suffix in _grades:
                    nm, grade = base, suffix
            if not nm:
                continue
            gid = (it.get("buff_goods_id") or "").strip()
            c5n = (it.get("c5_market_hash_name") or "").strip()
            ent = name_meta.setdefault(
                nm, {"buff_goods_id": "", "gid_by_grade": {},
                     "c5_by_grade": {}})
            if grade:
                if gid:
                    ent["gid_by_grade"].setdefault(grade, gid)
                if c5n:
                    ent["c5_by_grade"].setdefault(grade, c5n)
            else:
                if gid and not ent["buff_goods_id"]:
                    ent["buff_goods_id"] = gid
                if c5n:
                    ent["c5_by_grade"].setdefault("", c5n)
        try:
            added = pm.import_from_taihuan_plan(
                plan_id, default_need=sets, name_meta=name_meta)
        except Exception as e:
            QMessageBox.warning(self, "导入失败", str(e))
            return
        self._reload_targets()

        # 触发一轮即时检测（跑完自动停），让用户立刻看到市场价
        if not self._is_any_worker_running():
            from PySide6.QtCore import QTimer
            QTimer.singleShot(100, self._run_one_round_and_stop)
            tip = (
                f"已从方案 #{plan_id} 导入 {added} 个材料目标"
                f"（套数 ×{sets}）。\n\n"
                f"✅ 已自动启动一轮检测，完成后会自动停止，"
                f"结果将实时填入表格。\n\n"
                f"下一步：双击表格行，为每个材料设置价格阈值。")
        else:
            tip = (
                f"已从方案 #{plan_id} 导入 {added} 个材料目标"
                f"（套数 ×{sets}）。\n\n"
                f"💡 检测正在运行中，新目标会在下一轮被自动覆盖。")
        QMessageBox.information(self, "导入完成", tip)

    def _is_any_worker_running(self) -> bool:
        """Buff 网页爬取或 C5/ECO API 任一通道正在跑？"""
        return bool(
            (self._worker and self._worker.isRunning())
            or (self._worker_buff and self._worker_buff.isRunning()))

    def _run_one_round_and_stop(self):
        """跑一轮检测后自动停止（给导入后即时看价用）。"""
        if self._is_any_worker_running():
            return
        platforms = self._current_platforms()
        buff_secs = self._current_interval_buff()
        api_secs = self._current_interval_api()

        # 单轮检测开始，清空上轮累积
        self._plat_results.clear()

        # Buff 网页爬取通道
        w_buff = None
        if platforms is None or "buff" in platforms:
            w_buff = PriceMonitorWorker(
                interval_seconds=buff_secs, platforms=["buff"])
            w_buff.progress.connect(self._on_progress)
            w_buff.item_done.connect(self._on_item_done)
            w_buff.round_finished.connect(self._on_round_finished)
            w_buff.stopped.connect(self._on_stopped)
            w_buff.start()
            self._worker_buff = w_buff

        # C5/ECO API 通道
        w_api = None
        if platforms is None or any(p in platforms for p in ("c5", "eco")):
            api_platforms = (["c5", "eco"] if platforms is None
                             else [p for p in platforms if p != "buff"])
            w_api = PriceMonitorWorker(
                interval_seconds=api_secs, platforms=api_platforms)
            w_api.progress.connect(self._on_progress)
            w_api.item_done.connect(self._on_item_done)
            w_api.round_finished.connect(self._on_round_finished)
            w_api.stopped.connect(self._on_stopped)
            w_api.start()
            self._worker = w_api

        self._set_running(True)
        self._append_log(
            "🔍 启动单轮检测（导入后即时看价），完成后自动停止。")

        # 等 Buff/ECO API 启动后，立即标记 stop → 本轮完成后自动停止
        from PySide6.QtCore import QTimer
        def _schedule_stop():
            for w in (self._worker_buff, self._worker):
                if w and w.isRunning():
                    w.stop()
        QTimer.singleShot(500, _schedule_stop)

    def _on_add_target(self):
        dlg = AddMonitorDialog(self, existing_items=self._all_items)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        vals = dlg.get_values()
        if not vals["item_name"]:
            QMessageBox.warning(self, "提示", "物品名称不能为空。")
            return
        if not vals["buff_goods_id"] and not vals["c5_market_hash_name"]:
            QMessageBox.warning(self, "提示", "至少需要填一个平台 ID。")
            return
        pm.add_target(vals)
        self._reload_targets()

    def _on_remove_target(self):
        row = self.table.currentRow()
        if row < 0:
            QMessageBox.information(self, "提示", "请先选中一行。")
            return
        chk_item = self.table.item(row, 0)
        if not chk_item:
            return
        target_id = chk_item.data(Qt.ItemDataRole.UserRole)
        if target_id is None:
            return
        pm.remove_target(target_id)
        self._reload_targets()

    def _on_clear_targets(self):
        if QMessageBox.question(
            self, "确认", "确定清空所有监控目标？"
        ) != QMessageBox.StandardButton.Yes:
            return
        pm.clear_targets()
        self._reload_targets()

    def _on_view_history(self):
        """弹出对话框展示选中物品的检测历史 + 价格趋势摘要。"""
        row = self.table.currentRow()
        if row < 0:
            QMessageBox.information(self, "提示", "请先选中一个监控目标。")
            return
        name_item = self.table.item(row, 1)
        if not name_item:
            return
        item_name = name_item.text()
        summary = get_price_history_summary(item_name)
        rows = query_price_monitor_history(item_name, limit=500)

        dlg = QDialog(self)
        dlg.setWindowTitle(f"检测历史 - {item_name}")
        dlg.resize(860, 560)
        layout = QVBoxLayout(dlg)

        # --- 价格趋势摘要 ---
        if summary:
            trend_widget = self._build_trend_summary(summary)
            layout.addWidget(trend_widget)
        else:
            layout.addWidget(QLabel(
                "📭 暂无检测历史。可启动检测或重新导入方案后，一轮检测结束即可查看。"))

        if not rows:
            btn_close = QPushButton("关闭")
            btn_close.clicked.connect(dlg.accept)
            layout.addWidget(btn_close)
            dlg.exec()
            return

        # --- 历史表格 ---
        from PySide6.QtWidgets import QTableWidget, QTableWidgetItem, QHeaderView
        hist_table = QTableWidget(len(rows), 6)
        hist_table.setHorizontalHeaderLabels(
            ["时间", "平台", "最低价", "最低磨损", "样本数", "备注"])
        hist_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch)
        hist_table.verticalHeader().setVisible(False)
        hist_table.setAlternatingRowColors(True)
        hist_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        for r, hr in enumerate(rows):
            # 列序: id, timestamp, item_name, platform, min_price,
            #       min_wear, sample_count, remark
            hist_table.setItem(r, 0, QTableWidgetItem(hr[1] or "-"))
            hist_table.setItem(r, 1, QTableWidgetItem(hr[3] or "-"))
            mp = hr[4]
            price_item = QTableWidgetItem(
                f"¥{mp:.2f}" if mp is not None else "失败")
            if mp is None:
                price_item.setForeground(Qt.GlobalColor.red)
            hist_table.setItem(r, 2, price_item)
            mw = hr[5]
            hist_table.setItem(
                r, 3, QTableWidgetItem(
                    f"{mw:.17f}" if mw is not None else "-"))
            hist_table.setItem(r, 4, QTableWidgetItem(str(hr[6] or 0)))
            hist_table.setItem(r, 5, QTableWidgetItem(hr[7] or "-"))
        layout.addWidget(hist_table)

        # --- 底部按钮 ---
        btn_row = QHBoxLayout()
        btn_clear = QPushButton("🗑 清空历史")
        btn_clear.setToolTip("仅清空此物品的历史快照（不影响全局）")
        btn_clear.clicked.connect(lambda: self._on_clear_history(
            item_name, dlg))
        btn_row.addWidget(btn_clear)
        btn_row.addStretch()
        btn_close = QPushButton("关闭")
        btn_close.clicked.connect(dlg.accept)
        btn_row.addWidget(btn_close)
        layout.addLayout(btn_row)
        dlg.exec()

    # ---------- 价格趋势 sparkline ----------

    @staticmethod
    def _build_trend_summary(summary: dict) -> QWidget:
        """构建一个带 sparkline 和统计摘要的小面板。"""
        w = QWidget()
        lay = QHBoxLayout(w)
        lay.setContentsMargins(6, 4, 6, 4)

        # 统计标签（左）
        stats = (
            f"📊 快照 {summary['count']} 次 · "
            f"最低 ¥{summary['min_price']:.2f} · "
            f"最高 ¥{summary['max_price']:.2f} · "
            f"均价 ¥{summary['avg_price']:.2f} "
            f"<span style='color:#888;'>（{summary['first_ts'][:16]} → {summary['last_ts'][:16]}）</span>"
        )
        lbl = QLabel(stats)
        lbl.setTextFormat(Qt.TextFormat.RichText)
        lay.addWidget(lbl)
        lay.addStretch()

        # Sparkline（右，Unicode 半高块字符绘制）
        spark = PriceMonitorPanel._make_sparkline(summary["prices"])
        spark_lbl = QLabel(f"<pre style='font-family:Consolas;'>{spark}</pre>")
        spark_lbl.setToolTip("价格趋势（↑涨 ↓跌 → 平；越高价格越高）")
        lay.addWidget(spark_lbl)
        return w

    @staticmethod
    def _make_sparkline(price_points, width=32) -> str:
        """用 Unicode 块字符画一个紧凑的 sparkline。

        :param price_points: [(ts_str, price_float), ...] 时间升序
        :param width: 字符宽度
        """
        if not price_points:
            return ""
        prices = [p for _, p in price_points]
        lo, hi = min(prices), max(prices)
        if hi <= lo:
            return "━" * width   # 一条直线（价格稳定）
        # 均匀采样到 width 个点
        n = len(prices)
        samples = [prices[min(int(i * (n - 1) / max(width - 1, 1)), n - 1)]
                   for i in range(width)]
        # 归一化到 0-7（8 个半高块等级）
        chars = " ▁▂▃▄▅▆▇█"   # 从空到满 8 级
        blocks = []
        for v in samples:
            r = (v - lo) / (hi - lo)
            idx = min(7, max(0, int(round(r * 7))))
            blocks.append(chars[idx])
        # 前后加一个箭头表达趋势
        first_half = price_points[:max(1, len(price_points) // 2)]
        second_half = price_points[len(price_points) // 2:]
        avg_first = sum(p for _, p in first_half) / len(first_half)
        avg_second = sum(p for _, p in second_half) / len(second_half)
        if avg_second > avg_first * 1.02:
            trend = " ↗"
        elif avg_second < avg_first * 0.98:
            trend = " ↘"
        else:
            trend = " →"
        return "".join(blocks) + trend

    def _on_clear_history(self, item_name: str, parent_dlg: QDialog):
        """清空该物品的历史快照。"""
        if QMessageBox.question(
            self, "确认",
            f"确定清空「{item_name}」的全部检测历史快照？"
        ) != QMessageBox.StandardButton.Yes:
            return
        clear_price_monitor_history(item_name)
        parent_dlg.accept()
        self._reload_targets()
        self._append_log(f"🗑 已清空 {item_name} 的检测历史。")

    def _on_import_from_favorites(self):
        """从收藏夹导入物品到监控目标。"""
        groups = fm.list_groups()
        if not groups:
            QMessageBox.information(self, "提示", "收藏夹还没有任何分组。")
            return
        # 弹一个简单的选择对话框
        from PySide6.QtWidgets import QInputDialog
        name, ok = QInputDialog.getItem(
            self, "选择分组", "从收藏夹哪个分组导入？",
            groups, 0, False)
        if not ok or not name:
            return
        items = fm.list_items(name)
        if not items:
            QMessageBox.information(self, "提示", f"分组「{name}」为空。")
            return
        added = 0
        for it in items:
            if not it.get("buff_goods_id") and not it.get("c5_market_hash_name"):
                continue
            pm.add_target({
                "item_name": it["item_name"],
                "buff_goods_id": it.get("buff_goods_id", ""),
                "c5_market_hash_name": it.get("c5_market_hash_name", ""),
                "c5_app_id": it.get("c5_app_id", "730"),
            })
            added += 1
        self._reload_targets()
        QMessageBox.information(
            self, "导入完成", f"已从「{name}」导入 {added} 个监控目标。")

    # ---------- 启停 ----------

    def _on_start(self):
        if ((self._worker and self._worker.isRunning())
                or (self._worker_buff and self._worker_buff.isRunning())):
            QMessageBox.information(self, "提示", "检测已在运行中。")
            return
        targets = pm.list_targets()
        if not targets:
            QMessageBox.warning(self, "提示", "请先添加监控目标。")
            return
        platforms = self._current_platforms()
        buff_secs = self._current_interval_buff()
        api_secs = self._current_interval_api()

        # 新一轮检测开始，清空上轮累积的平台价格
        self._plat_results.clear()

        # 通道1：Buff 网页爬取（独立频率）
        self._worker_buff = None
        if platforms is None or "buff" in platforms:
            w = PriceMonitorWorker(
                interval_seconds=buff_secs, platforms=["buff"])
            w.progress.connect(self._on_progress)
            w.item_done.connect(self._on_item_done)
            w.round_finished.connect(self._on_round_finished)
            w.stopped.connect(self._on_stopped)
            w.start()
            self._worker_buff = w
            self._append_log(
                f"▶ [Buff 网页爬取] 开始检测，间隔 {int(buff_secs)}s，"
                f"目标 {len(targets)} 个。")

        # 通道2：C5/ECO API（独立频率）
        self._worker = None
        if platforms is None or any(p in platforms for p in ("c5", "eco")):
            api_platforms = (["c5", "eco"] if platforms is None
                              else [p for p in platforms if p != "buff"])
            w2 = PriceMonitorWorker(
                interval_seconds=api_secs, platforms=api_platforms)
            w2.progress.connect(self._on_progress)
            w2.item_done.connect(self._on_item_done)
            w2.round_finished.connect(self._on_round_finished)
            w2.stopped.connect(self._on_stopped)
            w2.start()
            self._worker = w2
            self._append_log(
                f"▶ [{'/'.join(api_platforms).upper()} API] 开始检测，"
                f"间隔 {int(api_secs)}s，目标 {len(targets)} 个。")

        if not self._worker and not self._worker_buff:
            QMessageBox.warning(self, "提示", "请至少勾选一个检测平台。")
            return
        self._set_running(True)

    def _on_stop(self):
        for w in (self._worker, self._worker_buff):
            if w and w.isRunning():
                self._append_log("⏹ 正在停止，等待当前轮次结束 ...")
                w.stop()
        self.btn_stop.setEnabled(False)

    def _on_stopped(self):
        still_running = any(
            w and w.isRunning() for w in (self._worker, self._worker_buff))
        if not still_running:
            self._set_running(False)
            self._append_log("⏹ 检测已全部停止。")

    # ---------- 结果回调 ----------

    def _on_progress(self, msg):
        self.status.setText(msg)
        self._append_log(msg)

    def _on_item_done(self, result):
        target = result["target"]
        item_name = target["item_name"]
        # 找到对应行更新
        row = self._find_row_by_name(item_name)
        if row < 0:
            return
        if result["success"]:
            # 合并各 worker 的平台价格：Buff worker 只带 buff，API worker 带 c5+eco。
            # 若直接覆盖，后到的 buff-only 结果会冲掉 c5/eco 价格 → 表格看不到 ECO。
            # 这里按 item_name 累积 plat_min/plat_count（按平台替换最新值）。
            acc = self._plat_results.get(item_name, {"min": {}, "count": {}})
            cur_min = result.get("plat_min") or {}
            cur_cnt = result.get("plat_count") or {}
            for p, v in cur_min.items():
                try:
                    acc["min"][p] = float(v)
                except (TypeError, ValueError):
                    pass
            for p, n in cur_cnt.items():
                acc["count"][p] = int(n)
            self._plat_results[item_name] = acc

            # 从合并后的 plat_min 重算全局最低价
            plat_min = acc["min"]
            plat_count = acc["count"]
            best_plat = min(plat_min, key=plat_min.get)
            min_price = float(plat_min[best_plat])
            price_item = QTableWidgetItem(f"¥{min_price:.2f}")
            if plat_min:
                lines = []
                for plat in ("buff", "c5", "eco"):
                    if plat in plat_min:
                        p = plat_min[plat]
                        n = plat_count.get(plat, 0)
                        lines.append(f"{plat.upper()}: ¥{float(p):.2f}（{n} 件）")
                    else:
                        lines.append(f"{plat.upper()}: —")
                price_item.setToolTip("\n".join(lines))
            self.table.setItem(row, COL_PRICE, price_item)
            self.table.setItem(
                row, COL_PLATFORM, QTableWidgetItem(best_plat))
            afford_n = result.get("affordable_count")
            aff_text = ("-" if afford_n is None else str(afford_n))
            self.table.setItem(row, COL_AFFORD, QTableWidgetItem(aff_text))
            self.table.setItem(
                row, COL_CHECKED, QTableWidgetItem(result["checked_at"]))
            # 需求/已购列实时刷新（worker 购买后会再次 emit）
            need = target.get("need_count") or 0
            bought = pm.count_bought(target.get("id"))
            self.table.setItem(
                row, COL_NEED,
                QTableWidgetItem(f"{bought}/{need}" if need > 0
                                 else "不自动购买"))
            self.table.setItem(row, COL_STATUS, QTableWidgetItem("✅ 成功"))
        else:
            self.table.setItem(
                row, COL_CHECKED, QTableWidgetItem(result["checked_at"]))
            self.table.setItem(
                row, COL_STATUS, QTableWidgetItem(f"❌ {result['error']}"))

    # ---------- 自动购买 ----------

    def _load_auto_buy_config(self):
        """从数据库读取启用状态填充 UI（兜底价格已废弃）。"""
        cfg = pm.get_auto_buy_config()
        self.ab_enabled.setChecked(cfg.get("enabled", False))

    def _on_save_auto_buy(self):
        enabled = self.ab_enabled.isChecked()
        # max_price/max_wear 传 None（兜底已砍）
        pm.save_auto_buy_config(enabled, None, None)
        self._append_log(
            "💾 自动购买配置已保存: "
            f"{'启用' if enabled else '关闭'}")

        # 关键修复：启用自动购买 + 检测未在跑 → 自动启动持续检测
        if enabled and not self._is_any_worker_running():
            self._on_start()

    def _on_round_finished(self, round_idx, elapsed, ts):
        self._append_log(f"── 第 {round_idx} 轮完成，耗时 {elapsed}s，时间 {ts} ──")

    # ---------- 工具 ----------

    def _find_row_by_name(self, name: str) -> int:
        for r in range(self.table.rowCount()):
            it = self.table.item(r, 1)
            if it and it.text() == name:
                return r
        return -1

    def _append_log(self, msg: str):
        from datetime import datetime
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_view.appendPlainText(f"[{ts}] {msg}")

    def _set_running(self, running: bool):
        self.btn_start.setEnabled(not running)
        self.btn_stop.setEnabled(running)
        self.btn_add_target.setEnabled(not running)
        self.btn_remove_target.setEnabled(not running)
        self.btn_clear_targets.setEnabled(not running)
        self.btn_import_fav.setEnabled(not running)
        self.btn_import_plan.setEnabled(not running)
        self.freq_buff_combo.setEnabled(not running)
        self.custom_secs_buff.setEnabled(
            not running and
            FREQ_PRESETS[self.freq_buff_combo.currentIndex()][1] == -1)
        self.freq_api_combo.setEnabled(not running)
        self.custom_secs_api.setEnabled(
            not running and
            FREQ_PRESETS[self.freq_api_combo.currentIndex()][1] == -1)
        self.chk_buff.setEnabled(not running)
        self.chk_c5.setEnabled(not running)
        self.chk_eco.setEnabled(not running)

    # 外部可在面板卸载时调用
    def stop_if_running(self):
        for w in (self._worker, self._worker_buff):
            if w and w.isRunning():
                w.stop()
                w.wait(3000)
