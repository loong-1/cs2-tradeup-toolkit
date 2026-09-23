"""物品总览面板：展示主 CSV 完整表格，支持品质/收藏品/磨损/关键字筛选 + 跳转 + 批量加监控。"""
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox, QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QMessageBox, QPushButton, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from core.data_manager import (
    MAIN_CSV_COLUMNS, load_items_from_main_csv_full,
    get_unique_rarities, get_unique_collections, C5_APP_ID,
    build_c5_market_hash_name,
)
from core import price_monitor as pm


# 磨损筛选预设：显示文本 -> (wear_zh 精确匹配或 None)
WEAR_FILTER_OPTIONS = [
    ("全部", None),
    ("崭新出厂", "崭新出厂"),
    ("略有磨损", "略有磨损"),
    ("久经沙场", "久经沙场"),
    ("破损不堪", "破损不堪"),
    ("战痕累累", "战痕累累"),
]


class ItemsOverviewPanel(QWidget):
    """物品总览标签页。"""

    # 请求跳转到「查询购买」标签页并装载指定物品配置
    request_goto_query = Signal(dict)

    def __init__(self):
        super().__init__()
        self._all_rows = load_items_from_main_csv_full()
        self._rarities = get_unique_rarities(self._all_rows)
        self._collections = get_unique_collections(self._all_rows)
        # 表格实际列 = 选择列 + MAIN_CSV_COLUMNS
        self._table_cols = ["选择"] + list(MAIN_CSV_COLUMNS)
        self._build_ui()
        self._apply_filter()

    # ---------- UI ----------

    def _build_ui(self):
        root = QVBoxLayout(self)

        # --- 筛选区 ---
        grp = QGroupBox("筛选条件（多条件取 AND）")
        grid = QGridLayout(grp)

        grid.addWidget(QLabel("品质:"), 0, 0)
        self.rarity_combo = QComboBox()
        self.rarity_combo.addItems(self._rarities)
        self.rarity_combo.currentIndexChanged.connect(self._apply_filter)
        grid.addWidget(self.rarity_combo, 0, 1)

        grid.addWidget(QLabel("收藏品:"), 0, 2)
        self.collection_combo = QComboBox()
        self.collection_combo.setEditable(True)
        self.collection_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        comp = self.collection_combo.completer()
        comp.setCompletionMode(comp.CompletionMode.PopupCompletion)
        comp.setFilterMode(Qt.MatchFlag.MatchContains)
        self.collection_combo.addItems(self._collections)
        self.collection_combo.currentIndexChanged.connect(self._apply_filter)
        grid.addWidget(self.collection_combo, 0, 3)

        grid.addWidget(QLabel("磨损:"), 1, 0)
        self.wear_combo = QComboBox()
        for label, _ in WEAR_FILTER_OPTIONS:
            self.wear_combo.addItem(label)
        self.wear_combo.setCurrentIndex(0)
        self.wear_combo.currentIndexChanged.connect(self._apply_filter)
        grid.addWidget(self.wear_combo, 1, 1)

        grid.addWidget(QLabel("关键字搜索:"), 1, 2)
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText(
            "皮肤名称 / 市场哈希名 / goods_id 模糊匹配")
        self.search_edit.textChanged.connect(self._on_search_debounced)
        grid.addWidget(self.search_edit, 1, 3)

        btn_row_filter = QHBoxLayout()
        btn_reset = QPushButton("重置筛选")
        btn_reset.clicked.connect(self._reset_filters)
        btn_row_filter.addWidget(btn_reset)
        btn_row_filter.addStretch()
        grid.addLayout(btn_row_filter, 2, 0, 1, 4)

        root.addWidget(grp)

        # --- 操作按钮 + 计数 ---
        op_row = QHBoxLayout()
        self.btn_select_all = QPushButton("☑ 全选当前筛选")
        self.btn_select_all.clicked.connect(self._on_select_all)
        op_row.addWidget(self.btn_select_all)

        self.btn_deselect_all = QPushButton("☐ 取消全选")
        self.btn_deselect_all.clicked.connect(self._on_deselect_all)
        op_row.addWidget(self.btn_deselect_all)

        self.lbl_count = QLabel("共 0 行")
        op_row.addSpacing(20)
        op_row.addWidget(self.lbl_count)
        op_row.addStretch()

        self.btn_add_monitor = QPushButton("📈 勾选加入价格检测")
        self.btn_add_monitor.clicked.connect(self._on_add_to_monitor)
        op_row.addWidget(self.btn_add_monitor)

        self.btn_jump = QPushButton("➡️ 选中行加入查询购买")
        self.btn_jump.clicked.connect(self._on_jump)
        op_row.addWidget(self.btn_jump)
        root.addLayout(op_row)

        # --- 表格 ---
        self.table = QTableWidget(0, len(self._table_cols))
        self.table.setHorizontalHeaderLabels(self._table_cols)
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        # 第 0 列（选择）固定较窄
        self.table.setColumnWidth(0, 50)
        # 关键列加宽（因为多了选择列，列索引全部 +1）
        widths = {
            "收藏品名称": 180, "皮肤名称": 240, "品质": 80,
            "磨损区间": 100, "磨损": 80, "buff_goods_id": 100,
            "市场哈希名称": 280, "price_buff": 90,
        }
        for i, col in enumerate(MAIN_CSV_COLUMNS):
            if col in widths:
                self.table.setColumnWidth(i + 1, widths[col])
        hdr.setStretchLastSection(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSortingEnabled(True)
        self.table.itemDoubleClicked.connect(self._on_double_click)
        root.addWidget(self.table, 1)

        self.status = QLabel("就绪。")
        root.addWidget(self.status)

    # ---------- 筛选逻辑 ----------

    def _reset_filters(self):
        self.rarity_combo.setCurrentIndex(0)
        self.collection_combo.setCurrentIndex(0)
        self.wear_combo.setCurrentIndex(0)
        self.search_edit.clear()
        self._apply_filter()

    def _on_search_debounced(self, _text):
        self._apply_filter()

    def _apply_filter(self):
        """按当前筛选条件过滤，AND 语义。"""
        rarity = self.rarity_combo.currentText().strip()
        collection = self.collection_combo.currentText().strip()
        kw = self.search_edit.text().strip().lower()
        wear_idx = self.wear_combo.currentIndex()
        wear_filter = WEAR_FILTER_OPTIONS[wear_idx][1]  # None 或 中文磨损名

        filtered = []
        for row in self._all_rows:
            if rarity and rarity != "全部" and row.get("品质", "") != rarity:
                continue
            if collection and collection != "全部" \
                    and row.get("收藏品名称", "") != collection:
                continue
            if wear_filter and row.get("磨损", "") != wear_filter:
                continue
            if kw:
                skin = row.get("皮肤名称", "").lower()
                hn = row.get("市场哈希名称", "").lower()
                gid = row.get("buff_goods_id", "").lower()
                if not (kw in skin or kw in hn or kw in gid):
                    continue
            filtered.append(row)

        self.table.setSortingEnabled(False)
        self.table.blockSignals(True)
        self.table.setRowCount(len(filtered))
        for r_idx, row in enumerate(filtered):
            # 列 0：勾选框
            chk = QTableWidgetItem()
            chk.setFlags(chk.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            chk.setCheckState(Qt.CheckState.Unchecked)
            chk.setData(Qt.ItemDataRole.UserRole, row)
            self.table.setItem(r_idx, 0, chk)
            # 其余列与 MAIN_CSV_COLUMNS 对应
            for c_idx, col in enumerate(MAIN_CSV_COLUMNS):
                cell = QTableWidgetItem(row.get(col, ""))
                self.table.setItem(r_idx, c_idx + 1, cell)
        self.table.blockSignals(False)
        self.table.setSortingEnabled(True)

        total = len(self._all_rows)
        cur = len(filtered)
        self.lbl_count.setText(f"显示 {cur} / 共 {total} 行")
        tags = []
        if rarity and rarity != "全部":
            tags.append(f"品质={rarity}")
        if wear_filter:
            tags.append(f"磨损={wear_filter}")
        self.status.setText(
            f"筛选完成。" + (f" [{', '.join(tags)}]" if tags else "") +
            (f" 匹配 {cur} 行" if kw else ""))

    # ---------- 勾选操作 ----------

    def _on_select_all(self):
        self.table.blockSignals(True)
        for r in range(self.table.rowCount()):
            chk = self.table.item(r, 0)
            if chk:
                chk.setCheckState(Qt.CheckState.Checked)
        self.table.blockSignals(False)

    def _on_deselect_all(self):
        self.table.blockSignals(True)
        for r in range(self.table.rowCount()):
            chk = self.table.item(r, 0)
            if chk:
                chk.setCheckState(Qt.CheckState.Unchecked)
        self.table.blockSignals(False)

    def _get_checked_rows(self):
        """返回当前勾选的原始行 dict 列表。"""
        checked = []
        for r in range(self.table.rowCount()):
            chk = self.table.item(r, 0)
            if chk and chk.checkState() == Qt.CheckState.Checked:
                row = chk.data(Qt.ItemDataRole.UserRole)
                if row:
                    checked.append(row)
        return checked

    def _on_add_to_monitor(self):
        rows = self._get_checked_rows()
        if not rows:
            QMessageBox.information(self, "提示", "请先勾选要加入的物品。")
            return
        added = 0
        for row in rows:
            skin = row.get("皮肤名称", "")
            wear = row.get("磨损", "")
            goods_id = row.get("buff_goods_id", "")
            hash_base = row.get("市场哈希名称", "")
            hash_name = build_c5_market_hash_name(hash_base, wear)
            if not goods_id and not hash_name:
                continue
            pm.add_target({
                "item_name": f"{skin} - {wear}" if wear else skin,
                "buff_goods_id": goods_id,
                "c5_market_hash_name": hash_name,
                "c5_app_id": str(C5_APP_ID),
            })
            added += 1
        QMessageBox.information(
            self, "加入成功",
            f"已加入 {added} / {len(rows)} 个物品到价格检测模块。"
            + (" （部分物品没有平台 ID，已跳过）" if added < len(rows) else ""))

    # ---------- 跳转 ----------

    def _on_double_click(self, _item):
        self._on_jump()

    def _on_jump(self):
        # 如果有勾选则跳到第一个勾选行；否则跳到当前选中行
        checked = self._get_checked_rows()
        if checked:
            row = checked[0]
        else:
            row_idx = self.table.currentRow()
            if row_idx < 0:
                QMessageBox.information(self, "提示", "请先选择一行。")
                return
            chk = self.table.item(row_idx, 0)
            row = (chk.data(Qt.ItemDataRole.UserRole) if chk else None)
            if not row:
                return
        skin_name = row.get("皮肤名称", "")
        wear = row.get("磨损", "")
        goods_id = row.get("buff_goods_id", "")
        hash_base = row.get("市场哈希名称", "")
        hash_name = build_c5_market_hash_name(hash_base, wear)
        display = f"{skin_name} - {wear}" if wear else skin_name
        payload = {
            "item_name": display,
            "buff_goods_id": goods_id,
            "c5_market_hash_name": hash_name,
            "c5_app_id": str(C5_APP_ID),
        }
        if not goods_id and not hash_name:
            QMessageBox.warning(self, "提示", "该行没有可查询的平台 ID。")
            return
        self.request_goto_query.emit(payload)
