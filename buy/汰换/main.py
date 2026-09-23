"""程序入口：初始化数据库 + 启动 GUI。"""
import sys

from PySide6.QtWidgets import QApplication

from core.data_manager import init_db
from core.inventory_fetcher import ensure_inventory_table
from gui.main_window import MainWindow


def main():
    init_db()
    ensure_inventory_table()  # inventory/taihuan_plan/taihuan_log 表
    app = QApplication(sys.argv)
    app.setApplicationName("汰换比价工具")
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
