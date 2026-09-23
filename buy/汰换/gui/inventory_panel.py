"""库存面板：独立标签页，展示完整库存（一行9个，可滚动，按爬取顺序）。"""
import logging
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
    QScrollArea, QGridLayout, QFrame, QMessageBox, QComboBox, QLineEdit,
)

from core import inventory_fetcher
from gui.taihuan_worker import InventoryRefreshWorker

logger = logging.getLogger(__name__)

# 每行显示的库存物品数（与游戏内 9 列一致）
INVENTORY_COLS = 9

# hash_name(去磨损档后缀) -> 品质 的懒加载映射（主 CSV）
_QUALITY_BY_BASENAME = None


class InventoryCard(QFrame):
    """单个库存物品卡片。"""

    def __init__(self, item: dict, index: int, parent=None):
        super().__init__(parent)
        self.item = item
        self.index = index
        self.setFrameShape(QFrame.Shape.Box)
        self.setFixedHeight(95)
        self.setStyleSheet(
            "QFrame{border:1px solid #ccc; border-radius:4px; "
            "background:#fafafa; padding:2px;}"
            "QFrame:hover{border-color:#2196F3; background:#e3f2fd;}")
        self._build()

    def _build(self):
        cl = QVBoxLayout(self)
        cl.setContentsMargins(4, 4, 4, 4)
        cl.setSpacing(1)

        name = (self.item.get("goods_name")
                or self.item.get("hash_name") or "?")
        full_name = name
        if len(name) > 22:
            name = name[:20] + ".."

        wear = self.item.get("paint_wear") or 0.0
        try:
            wear = float(wear)
        except (TypeError, ValueError):
            wear = 0.0

        # 序号
        lbl_idx = QLabel(f"#{self.index + 1}")
        lbl_idx.setStyleSheet("font-size:8px; color:#aaa;")
        cl.addWidget(lbl_idx)

        lbl_name = QLabel(name)
        lbl_name.setWordWrap(True)
        lbl_name.setStyleSheet("font-size:10px; color:#333; font-weight:bold;")
        lbl_name.setToolTip(full_name)
        cl.addWidget(lbl_name)

        lbl_wear = QLabel(f"磨损: {wear:.17f}")
        lbl_wear.setStyleSheet("font-size:9px; color:#666;")
        cl.addWidget(lbl_wear)

        asset = self.item.get("asset_id") or ""
        lbl_asset = QLabel(
            f"ID: ...{asset[-6:]}" if len(asset) > 6 else f"ID: {asset}")
        lbl_asset.setStyleSheet("font-size:8px; color:#999;")
        cl.addWidget(lbl_asset)


class InventoryPanel(QWidget):
    """库存标签页。"""

    # 通知其他面板库存已刷新
    inventory_refreshed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._refresh_worker: Optional[InventoryRefreshWorker] = None
        self._build_ui()
        self._update_stats()
        self._load_grid()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)

        # 顶部操作栏
        top_row = QHBoxLayout()
        self.btn_refresh = QPushButton("🔄 刷新库存（ECO 网页爬取）")
        self.btn_refresh.setStyleSheet(
            "QPushButton{background:#4CAF50;color:white;padding:6px 16px;"
            "font-weight:bold;}")
        self.btn_refresh.clicked.connect(self._on_refresh)
        top_row.addWidget(self.btn_refresh)

        self.lbl_stats = QLabel("库存：未加载")
        self.lbl_stats.setStyleSheet("color:#555; font-size:13px;")
        top_row.addWidget(self.lbl_stats, 1)

        # 搜索过滤
        top_row.addWidget(QLabel("筛选:"))
        self.edit_filter = QLineEdit()
        self.edit_filter.setPlaceholderText("输入名称/AssetId 过滤...")
        self.edit_filter.setFixedWidth(200)
        self.edit_filter.textChanged.connect(self._on_filter_changed)
        top_row.addWidget(self.edit_filter)

        # 品质过滤（从主 CSV 反查品质；data 值与主 CSV「品质」列取值一致）
        top_row.addWidget(QLabel("品质:"))
        self.combo_quality = QComboBox()
        self.combo_quality.addItem("全部", None)
        self.combo_quality.addItem("消费级", "消费级")
        self.combo_quality.addItem("工业级", "工业级")
        self.combo_quality.addItem("军规级", "军规级")
        self.combo_quality.addItem("受限级", "受限级")
        self.combo_quality.addItem("保密级", "保密级")
        self.combo_quality.addItem("隐秘级", "隐秘级")
        self.combo_quality.currentIndexChanged.connect(
            self._on_quality_filter_changed)
        top_row.addWidget(self.combo_quality)

        layout.addLayout(top_row)

        # 库存网格（滚动区域）
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self.grid_container = QWidget()
        self.grid_layout = QGridLayout(self.grid_container)
        self.grid_layout.setSpacing(4)
        self.scroll.setWidget(self.grid_container)
        layout.addWidget(self.scroll, 1)

        self._all_items: list = []

    # ---------- 数据加载 ----------
    _WEAR_SUFFIXES = (
        "(崭新出厂)", "(略有磨损)", "(久经沙场)", "(破损不堪)", "(战痕累累)",
        "(Factory New)", "(Minimal Wear)", "(Field-Tested)",
        "(Well-Worn)", "(Battle-Scarred)",
    )

    @classmethod
    def _strip_wear_suffix(cls, name: str) -> str:
        """去掉名称末尾的磨损档后缀（中/英文档位都可能出现）。"""
        for suffix in cls._WEAR_SUFFIXES:
            if name.endswith(suffix):
                return name[: -len(suffix)].strip()
        return name

    @staticmethod
    def _get_quality_for_item(item: dict) -> str:
        """按名称（去磨损档后缀）从主 CSV 反查品质。

        库存 hash_name/goods_name 为中文名（如 "SSG 08 | 绿陶 (略有磨损)"），
        与主 CSV「皮肤名称」格式一致；同时索引英文「市场哈希名称」兜底。
        查不到返回空串（非皮肤类物品：武器箱/印花等，会被
        "全部"以外的品质过滤器排除）。
        """
        name = ((item.get("hash_name") or "").strip()
                or (item.get("goods_name") or "").strip())
        if not name:
            return ""
        base = InventoryPanel._strip_wear_suffix(name)
        # 懒加载 名称(去后缀) -> 品质 映射
        global _QUALITY_BY_BASENAME
        if _QUALITY_BY_BASENAME is None:
            mapping = {}
            try:
                from core.data_manager import load_items_from_main_csv_full
                for row in load_items_from_main_csv_full():
                    quality = (row.get("品质") or "").strip()
                    if not quality:
                        continue
                    # 主键：中文皮肤名称（与库存名称格式一致）
                    skin_cn = (row.get("皮肤名称") or "").strip()
                    if skin_cn:
                        b = InventoryPanel._strip_wear_suffix(skin_cn)
                        mapping.setdefault(b, quality)
                    # 兜底：英文市场哈希名称
                    mhn = (row.get("市场哈希名称") or "").strip()
                    if mhn:
                        b = InventoryPanel._strip_wear_suffix(mhn)
                        mapping.setdefault(b, quality)
            except Exception as e:
                logger.warning("品质映射加载失败: %s", e)
            _QUALITY_BY_BASENAME = mapping
        return _QUALITY_BY_BASENAME.get(base, "")

    def _load_grid(self, filter_text: str = ""):
        """渲染库存网格。"""
        try:
            items = inventory_fetcher.load_inventory_from_db()
        except Exception as e:
            logger.warning("加载库存失败: %s", e)
            items = []
        self._all_items = items

        # 品质过滤
        quality_filter = (self.combo_quality.currentData()
                          if hasattr(self, "combo_quality") else None)
        if quality_filter:
            items = [it for it in items
                     if self._get_quality_for_item(it) == quality_filter]

        # 名称/AssetId 过滤
        if filter_text:
            ft = filter_text.lower()
            items = [it for it in items
                     if ft in (it.get("goods_name") or "").lower()
                     or ft in (it.get("hash_name") or "").lower()
                     or ft in (it.get("asset_id") or "").lower()]

        # 清除旧卡片
        while self.grid_layout.count():
            child = self.grid_layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()

        # 渲染
        for i, item in enumerate(items):
            row = i // INVENTORY_COLS
            col = i % INVENTORY_COLS
            card = InventoryCard(item, i)
            self.grid_layout.addWidget(card, row, col)

    def _update_stats(self):
        try:
            stats = inventory_fetcher.get_inventory_stats()
            self.lbl_stats.setText(
                f"库存：共 {stats['total']} 件，可交易 {stats['tradable']} 件，"
                f"最后抓取 {stats['last_fetched']}")
        except Exception as e:
            self.lbl_stats.setText(f"库存统计失败：{e}")

    # ---------- 刷新 ----------
    def _on_refresh(self):
        if self._refresh_worker and self._refresh_worker.isRunning():
            QMessageBox.warning(self, "提示", "库存刷新正在进行中")
            return
        self.btn_refresh.setEnabled(False)
        self._refresh_worker = InventoryRefreshWorker()
        self._refresh_worker.progress.connect(
            lambda p, t: self.lbl_stats.setText(
                f"抓取中... 第 {p} 页，总计 {t} 件"))
        self._refresh_worker.finished_ok.connect(self._on_refresh_done)
        self._refresh_worker.error.connect(self._on_refresh_error)
        self._refresh_worker.start()

    def _on_refresh_done(self, count: int):
        self.btn_refresh.setEnabled(True)
        self._update_stats()
        self._load_grid(self.edit_filter.text().strip())
        self.inventory_refreshed.emit()

    def _on_refresh_error(self, msg: str):
        self.btn_refresh.setEnabled(True)
        self._update_stats()
        QMessageBox.critical(self, "错误", msg)

    def _on_filter_changed(self, text: str):
        self._load_grid(text.strip())

    def _on_quality_filter_changed(self):
        self._load_grid(self.edit_filter.text().strip())

    # ---------- 对外 ----------
    def stop_if_running(self):
        if self._refresh_worker and self._refresh_worker.isRunning():
            self._refresh_worker.terminate()
            self._refresh_worker.wait(2000)
