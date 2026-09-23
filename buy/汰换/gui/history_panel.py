"""历史记录面板：按条件筛选 + 导出 CSV。

异步化改造：查询全部走 HistoryQueryWorker，首屏构造函数不再立刻查库，
而是通过 QTimer.singleShot(0, ...) 投递到下一轮事件循环中再启动，
避免 main_window 构造时实例化 HistoryPanel 导致主线程卡 SQLite。
"""
from typing import Optional

from PySide6.QtCore import QDate, QTimer
from PySide6.QtWidgets import (
    QComboBox, QDateEdit, QFileDialog, QGridLayout, QGroupBox,
    QHeaderView, QLabel, QLineEdit, QMessageBox,
    QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from core.data_manager import export_history_csv

HISTORY_COLS = ["ID", "时间戳", "物品名称", "磨损", "平台", "价格",
               "数量", "操作类型", "状态", "订单ID", "备注"]


class HistoryPanel(QWidget):

    def __init__(self):
        super().__init__()
        self._query_worker: Optional[object] = None
        self._build_ui()
        # 首屏不直接查库；放到下一轮事件循环里用异步 Worker 完成
        QTimer.singleShot(0, self._on_query)

    # ============================================================
    # UI
    # ============================================================
    def _build_ui(self):
        root = QVBoxLayout(self)

        # --- 筛选区 ---
        grp = QGroupBox("筛选条件")
        grid = QGridLayout(grp)

        grid.addWidget(QLabel("起始日期:"), 0, 0)
        self.date_from = QDateEdit()
        self.date_from.setCalendarPopup(True)
        self.date_from.setDate(QDate.currentDate().addMonths(-1))
        self.date_from.setDisplayFormat("yyyy-MM-dd")
        grid.addWidget(self.date_from, 0, 1)

        grid.addWidget(QLabel("结束日期:"), 0, 2)
        self.date_to = QDateEdit()
        self.date_to.setCalendarPopup(True)
        self.date_to.setDate(QDate.currentDate())
        self.date_to.setDisplayFormat("yyyy-MM-dd")
        grid.addWidget(self.date_to, 0, 3)

        grid.addWidget(QLabel("物品名称:"), 1, 0)
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("模糊匹配，留空不限")
        grid.addWidget(self.name_edit, 1, 1)

        grid.addWidget(QLabel("平台:"), 1, 2)
        self.platform_combo = QComboBox()
        self.platform_combo.addItems(["全部", "buff", "c5", "全部(Buff/C5)"])
        grid.addWidget(self.platform_combo, 1, 3)

        grid.addWidget(QLabel("操作类型:"), 2, 0)
        self.op_combo = QComboBox()
        self.op_combo.addItems(["全部", "查询", "购买"])
        grid.addWidget(self.op_combo, 2, 1)

        self.btn_query = QPushButton("🔍 查询")
        self.btn_query.clicked.connect(self._on_query)
        grid.addWidget(self.btn_query, 2, 2)

        btn_export = QPushButton("💾 导出CSV")
        btn_export.clicked.connect(self._on_export)
        grid.addWidget(btn_export, 2, 3)

        root.addWidget(grp)

        # --- 结果表格 ---
        self.table = QTableWidget(0, len(HISTORY_COLS))
        self.table.setHorizontalHeaderLabels(HISTORY_COLS)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        root.addWidget(self.table, 1)

        self.status = QLabel("就绪。")
        root.addWidget(self.status)

    # ============================================================
    # 查询
    # ============================================================
    def _on_query(self):
        self.refresh()

    def refresh(self):
        """供外部调用：按当前筛选条件刷新历史记录表（异步）。"""
        date_from = self.date_from.date().toString("yyyy-MM-dd") + " 00:00:00"
        date_to = self.date_to.date().toString("yyyy-MM-dd") + " 23:59:59"
        name = self.name_edit.text().strip() or None
        plat_idx = self.platform_combo.currentIndex()
        platform = None if plat_idx in (0, 3) else self.platform_combo.currentText()
        op_idx = self.op_combo.currentIndex()
        op_type = None if op_idx == 0 else self.op_combo.currentText()

        self.btn_query.setEnabled(False)
        self.btn_query.setText("🔍 查询中…")
        self.status.setText("正在查询历史记录…")

        from gui.taihuan_worker import HistoryQueryWorker
        # 若上一次查询还在跑，先 terminate 掉（避免表格被两次回填冲突）
        if getattr(self, "_query_worker", None) is not None:
            w = self._query_worker
            if getattr(w, "isRunning", lambda: False)():
                try:
                    w.terminate()
                except Exception:
                    pass
                try:
                    w.wait(1500)
                except Exception:
                    pass

        self._query_worker = HistoryQueryWorker(
            date_from, date_to, name, platform, op_type, self)
        self._query_worker.finished_ok.connect(self._on_query_results)
        self._query_worker.error.connect(self._on_query_error)
        self._query_worker.start()

    def _on_query_results(self, rows: list):
        self.btn_query.setEnabled(True)
        self.btn_query.setText("🔍 查询")
        # 批量填表格时禁用更新，减少百次 setItem 重绘
        self.table.setUpdatesEnabled(False)
        try:
            self.table.setRowCount(len(rows))
            for r, row in enumerate(rows):
                for c, val in enumerate(row):
                    display = "" if val is None else str(val)
                    if c == 5 and val is not None:  # 价格列
                        try:
                            display = f"¥{float(val):.2f}" if val else ""
                        except (TypeError, ValueError):
                            pass
                    self.table.setItem(r, c, QTableWidgetItem(display))
        finally:
            self.table.setUpdatesEnabled(True)
        self.status.setText(f"共 {len(rows)} 条记录。")

    def _on_query_error(self, msg: str):
        self.btn_query.setEnabled(True)
        self.btn_query.setText("🔍 查询")
        self.status.setText(f"查询失败：{msg}")
        QMessageBox.critical(self, "错误", f"查询失败: {msg}")

    # ============================================================
    # 导出
    # ============================================================
    def _on_export(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "导出历史记录", "history.csv", "CSV 文件 (*.csv)")
        if not path:
            return
        # 从表格读取当前数据
        rows = []
        for r in range(self.table.rowCount()):
            row = []
            for c in range(self.table.columnCount()):
                item = self.table.item(r, c)
                row.append(item.text() if item else "")
            rows.append(row)
        try:
            export_history_csv(rows, path)
            QMessageBox.information(self, "导出成功", f"已保存到:\n{path}")
        except Exception as e:
            QMessageBox.critical(self, "导出失败", str(e))
