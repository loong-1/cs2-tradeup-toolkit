"""收藏夹面板：分组（每组最多 10 个物品）+ 一键查询当前组最低价。"""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFormLayout, QGridLayout,
    QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox,
    QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout,
    QWidget,
)

from core import favorites_manager as fm
from core.data_manager import (
    load_items_from_main_csv, load_items, log_price_monitor)
from gui.workers import FavoritesQueryWorker


FAV_COLS = ["物品名称", "Buff ID", "C5 哈希名", "最低价", "最低磨损",
            "最低价平台", "最近检测时间", "操作"]


class AddFavoriteDialog(QDialog):
    """添加物品到收藏夹的对话框（从主CSV下拉或手动输入）。"""

    def __init__(self, parent=None, existing_items=None):
        super().__init__(parent)
        self.setWindowTitle("添加物品到收藏夹")
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
        # 优先使用下拉关联数据；否则用用户编辑后的值
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


class FavoritesPanel(QWidget):
    """收藏夹标签页。"""

    def __init__(self):
        super().__init__()
        self._worker = None
        self._all_items = list(load_items_from_main_csv()) + list(load_items())
        self._build_ui()
        self._refresh_groups()

    # ---------- UI ----------

    def _build_ui(self):
        root = QVBoxLayout(self)

        # --- 分组管理区 ---
        grp = QGroupBox("收藏夹分组（每组最多 10 个物品）")
        grid = QGridLayout(grp)

        grid.addWidget(QLabel("分组:"), 0, 0)
        self.group_combo = QComboBox()
        self.group_combo.setEditable(True)
        self.group_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.group_combo.currentIndexChanged.connect(self._on_group_changed)
        grid.addWidget(self.group_combo, 0, 1)

        self.btn_refresh_groups = QPushButton("刷新分组")
        self.btn_refresh_groups.clicked.connect(self._refresh_groups)
        grid.addWidget(self.btn_refresh_groups, 0, 2)

        self.btn_remove_group = QPushButton("删除当前分组")
        self.btn_remove_group.clicked.connect(self._on_remove_group)
        grid.addWidget(self.btn_remove_group, 0, 3)

        self.lbl_count = QLabel("当前组：0 / 10")
        grid.addWidget(self.lbl_count, 1, 0, 1, 4)
        root.addWidget(grp)

        # --- 操作按钮 ---
        btn_row = QHBoxLayout()
        self.btn_add = QPushButton("➕ 添加物品到当前组")
        self.btn_add.clicked.connect(self._on_add)
        btn_row.addWidget(self.btn_add)

        self.btn_remove = QPushButton("➖ 移除选中物品")
        self.btn_remove.clicked.connect(self._on_remove)
        btn_row.addWidget(self.btn_remove)

        self.btn_query = QPushButton("🔍 一键查询当前组最低价")
        self.btn_query.clicked.connect(self._on_query)
        btn_row.addWidget(self.btn_query)

        btn_row.addStretch()
        root.addLayout(btn_row)

        # --- 物品表格 ---
        self.table = QTableWidget(0, len(FAV_COLS))
        self.table.setHorizontalHeaderLabels(FAV_COLS)
        self.table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        root.addWidget(self.table, 1)

        # --- 状态栏 ---
        self.status = QLabel("就绪。")
        root.addWidget(self.status)

    # ---------- 分组管理 ----------

    def _refresh_groups(self):
        current_text = self.group_combo.currentText().strip()
        groups = fm.list_groups()
        self.group_combo.blockSignals(True)
        self.group_combo.clear()
        if groups:
            self.group_combo.addItems(groups)
            if current_text in groups:
                self.group_combo.setCurrentText(current_text)
            else:
                self.group_combo.setCurrentIndex(0)
        else:
            # 默认创建一个「默认分组」让用户能直接添加
            self.group_combo.addItem("默认分组")
            self.group_combo.setCurrentIndex(0)
        self.group_combo.blockSignals(False)
        self._on_group_changed()

    def _current_group(self) -> str:
        name = self.group_combo.currentText().strip()
        if not name:
            name = "默认分组"
        return name

    def _on_group_changed(self):
        self._reload_table()

    def _on_remove_group(self):
        name = self._current_group()
        if not name:
            return
        if QMessageBox.question(
            self, "确认", f"确定删除分组「{name}」及其所有物品？"
        ) != QMessageBox.StandardButton.Yes:
            return
        fm.remove_group(name)
        self._refresh_groups()

    # ---------- 物品增删 ----------

    def _on_add(self):
        group = self._current_group()
        if fm.count_items(group) >= fm.MAX_ITEMS_PER_GROUP:
            QMessageBox.warning(
                self, "提示",
                f"当前组已满 {fm.MAX_ITEMS_PER_GROUP} 个，"
                "请先移除部分物品或选择其他分组。")
            return
        dlg = AddFavoriteDialog(self, existing_items=self._all_items)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        vals = dlg.get_values()
        if not vals["item_name"]:
            QMessageBox.warning(self, "提示", "物品名称不能为空。")
            return
        if not vals["buff_goods_id"] and not vals["c5_market_hash_name"]:
            QMessageBox.warning(self, "提示", "至少需要填一个平台 ID。")
            return
        new_id = fm.add_item(group, vals)
        if new_id < 0:
            QMessageBox.warning(self, "提示", "分组已满。")
            return
        self._reload_table()

    def _on_remove(self):
        row = self.table.currentRow()
        if row < 0:
            QMessageBox.information(self, "提示", "请先选中一行。")
            return
        item_id_item = self.table.item(row, 0)
        if not item_id_item:
            return
        item_id = item_id_item.data(Qt.ItemDataRole.UserRole)
        if item_id is None:
            return
        fm.remove_item(item_id)
        self._reload_table()

    # ---------- 一键查询 ----------

    def _on_query(self):
        if self._worker and self._worker.isRunning():
            QMessageBox.information(self, "提示", "查询进行中，请稍候。")
            return
        items = fm.list_items(self._current_group())
        if not items:
            QMessageBox.warning(self, "提示", "当前组没有物品，请先添加。")
            return
        self._set_busy(True, f"正在并行查询 {len(items)} 个物品的最低价 ...")
        self._worker = FavoritesQueryWorker(items)
        self._worker.progress.connect(self.status.setText)
        self._worker.item_done.connect(self._on_item_done)
        self._worker.finished.connect(self._on_query_finished)
        self._worker.start()

    def _on_item_done(self, payload):
        item = payload["item"]
        info = payload.get("info")
        err = payload.get("error")
        item_id = item.get("id")
        if info is None:
            self.status.setText(f"查询失败: {item['item_name']} - {err}")
            return
        # 更新数据库缓存
        fm.update_check_result(
            item_id, info["min_price"], info["min_wear"],
            platform=info["min_price_platform"])
        # 记录到价格检测历史
        log_price_monitor(
            item["item_name"], info["min_price_platform"],
            info["min_price"], info["min_wear"],
            sample_count=info["count"],
            remark=f"收藏夹查询 - {self._current_group()}")
        # 刷新当前行（直接重载表格）
        self._reload_table()

    def _on_query_finished(self, ok, fail):
        self._set_busy(False, f"查询完成: 成功 {ok} 个, 失败 {fail} 个。")

    # ---------- 表格 ----------

    def _reload_table(self):
        group = self._current_group()
        items = fm.list_items(group)
        self.table.setRowCount(len(items))
        for r, it in enumerate(items):
            name_item = QTableWidgetItem(it["item_name"])
            name_item.setData(Qt.ItemDataRole.UserRole, it["id"])
            self.table.setItem(r, 0, name_item)
            self.table.setItem(r, 1, QTableWidgetItem(it["buff_goods_id"] or ""))
            self.table.setItem(r, 2, QTableWidgetItem(it["c5_market_hash_name"] or ""))
            price = it.get("min_price")
            self.table.setItem(
                r, 3, QTableWidgetItem(f"¥{price:.2f}" if price is not None else "-"))
            wear = it.get("min_wear")
            self.table.setItem(
                r, 4, QTableWidgetItem(f"{wear:.17f}" if wear is not None else "-"))
            # 平台列暂用空，查询后从缓存读不到，这里只显示"-"，下次查询时更新
            self.table.setItem(r, 5, QTableWidgetItem("-"))
            self.table.setItem(
                r, 6, QTableWidgetItem(it.get("last_checked") or "-"))
            self.table.setItem(r, 7, QTableWidgetItem("右键删除"))
        self.lbl_count.setText(f"当前组：{len(items)} / {fm.MAX_ITEMS_PER_GROUP}")
        if not self._worker or not self._worker.isRunning():
            self.status.setText(
                f"已加载 {len(items)} 个物品" if items else "当前组为空。")

    # ---------- 工具 ----------

    def _set_busy(self, busy, msg=""):
        self.btn_query.setEnabled(not busy)
        self.btn_add.setEnabled(not busy)
        self.btn_remove.setEnabled(not busy)
        self.btn_remove_group.setEnabled(not busy)
        if msg:
            self.status.setText(msg)

    def select_group_by_name(self, name: str):
        """供外部跳转使用：切到指定分组。"""
        idx = self.group_combo.findText(name)
        if idx >= 0:
            self.group_combo.setCurrentIndex(idx)
