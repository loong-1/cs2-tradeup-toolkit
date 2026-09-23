"""主窗口：标签页容器。"""
from PySide6.QtWidgets import QMainWindow, QTabWidget

from gui.query_panel import QueryPanel
from gui.history_panel import HistoryPanel
from gui.items_overview_panel import ItemsOverviewPanel
from gui.favorites_panel import FavoritesPanel
from gui.price_monitor_panel import PriceMonitorPanel
from gui.query_queue_panel import QueryQueuePanel
from gui.buy_queue_panel import BuyQueuePanel
from gui.inventory_panel import InventoryPanel
from gui.auto_taihuan_panel import AutoTaihuanPanel
from gui.taihuan_exec_panel import TaihuanExecPanel
from gui.tradeup_sim_panel import TradeupSimPanel


class MainWindow(QMainWindow):

    def __init__(self):
        super().__init__()
        self.setWindowTitle("汰换 - CS饰品比价购买工具")
        self.resize(1280, 800)

        tabs = QTabWidget()
        self.query_panel = QueryPanel()
        self.overview_panel = ItemsOverviewPanel()
        self.history_panel = HistoryPanel()
        self.favorites_panel = FavoritesPanel()
        self.monitor_panel = PriceMonitorPanel()
        self.queue_panel = QueryQueuePanel()
        self.buy_queue_panel = BuyQueuePanel()
        self.inventory_panel = InventoryPanel()
        self.taihuan_panel = AutoTaihuanPanel()
        self.taihuan_exec_panel = TaihuanExecPanel()
        self.tradeup_sim_panel = TradeupSimPanel()
        tabs.addTab(self.overview_panel, "物品总览（品质筛选）")
        tabs.addTab(self.query_panel, "查询购买")
        tabs.addTab(self.favorites_panel, "收藏夹")
        tabs.addTab(self.queue_panel, "查询列表")
        tabs.addTab(self.monitor_panel, "价格检测")
        tabs.addTab(self.buy_queue_panel, "购买列表")
        tabs.addTab(self.inventory_panel, "库存")
        tabs.addTab(self.taihuan_panel, "自动汰换")
        tabs.addTab(self.taihuan_exec_panel, "汰换")
        tabs.addTab(self.tradeup_sim_panel, "炼金模拟")
        tabs.addTab(self.history_panel, "历史记录")
        # 默认打开「物品总览」以便用户先看数据筛选
        tabs.setCurrentIndex(0)
        self.setCentralWidget(tabs)
        self._tabs = tabs

        # 跳转信号连接
        self.overview_panel.request_goto_query.connect(self._goto_query_with)
        # 查询面板 -> 购买列表面板：加入购买列表
        self.query_panel.request_add_to_buy.connect(self._on_add_to_buy)
        # 购买列表面板 -> 历史记录面板：购买完成后刷新历史
        self.buy_queue_panel.request_refresh_history.connect(
            self.history_panel.refresh)
        # 「汰换」页刷新了库存 DB（匹配前/汰换后）→ 库存页同步重载，保持一致
        self.taihuan_exec_panel.inventory_refreshed.connect(
            self._on_inventory_refreshed_elsewhere)
        # 反向：库存页自己刷新 → 汰换页的匹配报告标记过期（库存已变，
        # 旧匹配作废，执行前需重新匹配）
        self.inventory_panel.inventory_refreshed.connect(
            self.taihuan_exec_panel.on_inventory_changed_elsewhere)

        self.statusBar().showMessage(
            "提示：物品总览支持品质/收藏品筛选，双击行可直接加入查询购买。"
            " 库存标签页通过 ECO API 抓取并按爬取顺序展示完整库存。"
            " 自动汰换面板：从物品总览数据选择 10 件材料→命名保存方案→"
            "开始寻找材料（查询各材料固定磨损范围内的最低价）。")

    def _goto_query_with(self, payload):
        """接收到物品总览的跳转请求：切换标签页 + 设置物品。"""
        self.query_panel.select_item_by_config(payload)
        self._tabs.setCurrentWidget(self.query_panel)

    def _on_add_to_buy(self, order):
        """查询面板请求把订单加入购买列表。"""
        self.buy_queue_panel.add_order(order)

    def _on_inventory_refreshed_elsewhere(self):
        """其他页面（汰换页）刷新了库存 DB → 库存页立即重载网格和统计。"""
        try:
            self.inventory_panel._update_stats()
            self.inventory_panel._load_grid(
                self.inventory_panel.edit_filter.text().strip())
        except Exception:
            pass

    def closeEvent(self, event):
        """窗口关闭时停止后台线程。"""
        for panel in (self.monitor_panel, self.queue_panel,
                      self.buy_queue_panel, self.inventory_panel,
                      self.taihuan_panel, self.taihuan_exec_panel):
            try:
                panel.stop_if_running()
            except Exception:
                pass
        super().closeEvent(event)
