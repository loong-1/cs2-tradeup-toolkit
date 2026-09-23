"""自动汰换面板（材料方案版）。

流程：
1. 左面板：直接使用「物品总览」（主 CSV）的数据作为可汰换材料，
   支持关键字/收藏品/品质/磨损筛选，点击卡片加入材料（最多 10 件）
2. 右面板：10 个材料槽位 + 参考总价 + 「开始寻找材料」
3. 方案管理：命名保存/载入/更新/删除 10 材料方案
4. 「开始寻找材料」：后台逐个查询每个材料在固定磨损范围内的
   最低价（Buff/C5/ECO 并行），在结果表中展示
5. 底部：校准参数 & 在 CS2 中点击材料（原有逻辑保留）

注：目标产出物品在本面板已弃用，方案只由 10 件材料组成。
"""
import logging
from typing import Optional

from PySide6.QtCore import Qt, Signal, QThread, QTimer
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QLineEdit,
    QComboBox, QScrollArea, QGridLayout, QFrame, QSpinBox, QDoubleSpinBox,
    QProgressBar, QTextEdit, QSplitter, QMessageBox, QListWidget,
    QListWidgetItem, QGroupBox, QAbstractItemView, QTableWidget,
    QTableWidgetItem, QHeaderView, QFormLayout, QDialog, QDialogButtonBox,
    QTabWidget,
)
from PySide6.QtGui import QColor, QBrush

from core import recipe_matcher
from core import user_settings as _us
from core import price_fetcher as _pfetcher
from core.data_manager import load_items_from_main_csv_full
from gui.taihuan_worker import GameClickWorker, MaterialFindWorker

# GUI 推荐默认值（帮助文本里提示）
_BUFF_THROTTLE_RECOMMENDED_S = 1.0
_BUFF_THROTTLE_MAX_PAGES, _ = _pfetcher.get_buff_scrape_config()  # 复用，不改动现有页数 UI

logger = logging.getLogger(__name__)

# 磨损等级显示颜色（参考 ECO 网页的 color 值）
WEAR_COLORS = {
    "崭新出厂": "#488b48",
    "略有磨损": "#488b48",
    "久经沙场": "#f1ad4d",
    "破损不堪": "#b7625f",
    "战痕累累": "#b7625f",
}

# 品质等级顺序（用于下拉）
QUALITY_ORDER = ["消费级", "工业级", "军规级", "受限级", "保密级", "隐秘级", "稀有特殊物品"]


def _wear_grade(w: float) -> str:
    return recipe_matcher.get_wear_grade(w)


def _parse_wear_range(row: dict):
    """解析主 CSV 行的「磨损区间」为 (min_f, max_f)。"""
    raw = (row.get("磨损区间") or "").replace("~", "").split()
    if len(raw) >= 2:
        try:
            return float(raw[0]), float(raw[1])
        except ValueError:
            pass
    return 0.0, 1.0


def _material_name(m: dict) -> str:
    return m.get("皮肤名称") or m.get("item_name") or m.get("goods_name") or "未知"


# ---- C2b: 官方精确 5 档磨损（与 recipe_matcher._official_wear_range 保持一致）----
_OFFICIAL_WEAR_RANGES_CN = [
    # (wear_cn 匹配关键字子串, 官方法定 wear_min, wear_max)
    # NOTE: "略有磨损"中间夹"有"字，匹配用"略有"
    ("崭新出厂",                    0.00, 0.07),
    ("略有磨损",                    0.07, 0.15),
    ("略磨",                        0.07, 0.15),   # 常见简称兜底
    ("久经沙场",                    0.15, 0.38),
    ("破损不堪",                    0.38, 0.45),
    ("战痕累累",                    0.45, 1.00),
]


def official_wear_range(wear_cn: str) -> tuple[float, float] | None:
    """给定中文磨损等级（或含该词的字符串）返回官方精确磨损区间 (min,max)。
    未命中返回 None（调用方再退而求其次用皮肤自带的宽范围）。"""
    if not isinstance(wear_cn, str):
        return None
    w = wear_cn.strip()
    if not w:
        return None
    for key, mn, mx in _OFFICIAL_WEAR_RANGES_CN:
        if key in w:
            return mn, mx
    # 英文字符串兜底
    low = w.lower()
    if "factory new" in low or low.endswith("fn"):
        return 0.00, 0.07
    if "minimal wear" in low or low.endswith("mw"):
        return 0.07, 0.15
    if "field-tested" in low or low.endswith("ft"):
        return 0.15, 0.38
    if "well-worn" in low or low.endswith("ww"):
        return 0.38, 0.45
    if "battle-scarred" in low or low.endswith("bs"):
        return 0.45, 1.00
    return None


def material_wear_display(material: dict) -> tuple[str, str, bool]:
    """返回 GUI 上用于"磨损区间"显示的 (title, range_text, is_custom)。

    规则（对齐 C2b 用户要求）：
      - 若该材料存在 user_wear_*（用户右键自定义过）→ 显示自定义区间，并 is_custom=True
        （这是用户特意指定的搜索边界，优先级最高）
      - 否则：若存在 磨损_min/磨损_max（方案保存/导出写入的夹紧区间，非宽范围）
        → 用该区间，磨损档名取"磨损"字段
      - 否则：若"磨损"（中文磨损等级）能命中官方 5 档 → 显示官方精确档
        崭新出厂:0~0.07 / 略有磨损:0.07~0.15 / 久经沙场:0.15~0.38
        / 破损不堪:0.38~0.45 / 战痕累累:0.45~1.0
      - 都没有：退回 _parse_wear_range 得到的皮肤宽范围（一般是 0~0.6 / 0~1）。
    """
    # 1) 用户自定义最高优先级
    if "user_wear_min" in material and "user_wear_max" in material:
        try:
            a = float(material["user_wear_min"])
            b = float(material["user_wear_max"])
            if 0.0 <= a < b <= 1.0:
                return (
                    f"自定义 {a:.4f}~{b:.4f}",
                    f"{a:.6f}~{b:.6f}",
                    True,
                )
        except (TypeError, ValueError):
            pass
    # 1.5) 方案保存/导出写入的 磨损_min/磨损_max（夹紧到皮肤范围的精确区间）
    #      与 _resolve_wear_bounds 保持一致：差值<0.45 才算有效精确区间
    wear_cn = (material.get("磨损") or "").strip()
    if "磨损_min" in material and "磨损_max" in material:
        try:
            a = float(material["磨损_min"])
            b = float(material["磨损_max"])
            if 0.0 <= a < b <= 1.0 and (b - a) < 0.45:
                return (
                    f"{wear_cn or '区间'} {a:.4f}~{b:.4f}",
                    f"{a:.6f}~{b:.6f}",
                    False,
                )
        except (TypeError, ValueError):
            pass
    # 2) 官方 5 档精确值
    if not wear_cn:
        # 拿 wmin 推中文磨损档
        a, b = _parse_wear_range(material)
        wmid = (a + b) / 2.0
        wear_cn = _wear_grade(wmid)
    rng = official_wear_range(wear_cn)
    if rng is not None:
        mn, mx = rng
        return (
            f"{wear_cn} {mn:.2f}~{mx:.2f}",
            f"{mn:.6f}~{mx:.6f}",
            False,
        )
    # 3) 退回皮肤自带宽范围
    a, b = _parse_wear_range(material)
    return (
        f"{wear_cn or '未知'} {a:.2f}~{b:.2f}",
        f"{a:.6f}~{b:.6f}",
        False,
    )


def _wear_max_right_open(mx: float, pwf=None) -> float:
    """官方档右边界按【左闭右开】处理：0.07/0.15/0.38/0.45 整点属于更高磨损档
    （0.15 是久经沙场而非略有磨损），查询/传参上限剔除边界点。
    例外：上限来自 paint_wear（物品确切磨损）夹紧时保持含端点。"""
    for bd in (0.07, 0.15, 0.38, 0.45):
        if abs(float(mx) - bd) < 1e-12:
            from_pwf = (pwf is not None and abs(float(pwf) - bd) < 1e-12)
            if not from_pwf:
                return float(mx) - 1e-6
            break
    return float(mx)


def material_wear_min_max(material: dict) -> tuple[float, float]:
    """给查价 / ECO StartSimulation 请求体 / GUI 总览表取 (wear_min, wear_max)。
    严格按 C2 / C2b 用户要求的优先级（与 _resolve_wear_bounds 对齐）：
      1. user_wear_*（用户自定义；这是用户明确要求的搜索边界，查价就按这个范围搜）
      2. 磨损_min/磨损_max（方案保存/导出写入的夹紧区间，差值<0.45 才生效）
      3. 中文磨损档 → 官方 5 档精确范围
      4. 主 CSV 皮肤宽范围（兜底）
      5. 若 paint_wear 存在（单物品确切磨损值），wear_max 要夹紧到 paint_wear：
         即 wear_max = min(wear_max_official_or_custom, paint_wear)
         用户原话「按照填入的磨损作为最高磨损往前搜索」。
    """
    pw = material.get("paint_wear")
    pwf = None
    try:
        pwf = float(pw)
        if not (0.0 <= pwf <= 1.0):
            pwf = None
    except (TypeError, ValueError):
        pwf = None

    # 1) 用户自定义
    if "user_wear_min" in material and "user_wear_max" in material:
        try:
            a = float(material["user_wear_min"])
            b = float(material["user_wear_max"])
            if 0.0 <= a < b <= 1.0:
                mn, mx = a, b
                if pwf is not None:
                    # "按填入的磨损(paint_wear)作为最高磨损往前搜索"
                    mx = min(mx, pwf)
                    if not (mn < mx):
                        mn = max(0.0, pwf - 1e-6)
                        mx = min(1.0, pwf + 1e-6)
                mx = _wear_max_right_open(mx, pwf)
                if mx <= mn:
                    mx = min(1.0, mn + 1e-6)
                return mn, mx
        except (TypeError, ValueError):
            pass
    # 2) 磨损_min/磨损_max（方案保存/导出写入的夹紧区间）
    if "磨损_min" in material and "磨损_max" in material:
        try:
            a = float(material["磨损_min"])
            b = float(material["磨损_max"])
            if 0.0 <= a < b <= 1.0 and (b - a) < 0.45:
                mn, mx = a, b
                if pwf is not None:
                    mx = min(mx, pwf)
                    if not (mn < mx):
                        mn = max(0.0, pwf - 1e-6)
                        mx = min(1.0, pwf + 1e-6)
                mx = _wear_max_right_open(mx, pwf)
                if mx <= mn:
                    mx = min(1.0, mn + 1e-6)
                return max(0.0, mn), min(1.0, mx)
        except (TypeError, ValueError):
            pass
    # 3) 官方 5 档精确值 / 皮肤宽范围
    wear_cn = (material.get("磨损") or "").strip()
    if not wear_cn:
        a, b = _parse_wear_range(material)
        wmid = (a + b) / 2.0
        wear_cn = _wear_grade(wmid)
    rng = official_wear_range(wear_cn)
    if rng is not None:
        mn, mx = rng
    else:
        mn, mx = _parse_wear_range(material)
    # 4) paint_wear 夹紧上限
    if pwf is not None:
        mx = min(mx, pwf)
        if not (mn < mx):
            mn = max(0.0, pwf - 1e-6)
            mx = min(1.0, pwf + 1e-6)
    # 右开处理：档位边界整点属于更高磨损档（0.15=久经而非略磨，以此类推），
    # 上限为 0.07/0.15/0.38/0.45 时剔除端点（paint_wear 精确夹紧除外）
    mx = _wear_max_right_open(mx, pwf)
    if mx <= mn:
        mx = min(1.0, mn + 1e-6)
    return max(0.0, mn), min(1.0, mx)


class WearEditDialog(QDialog):
    """材料磨损区间编辑弹窗。双击槽位或右键弹出。"""

    def __init__(self, material_name: str,
                 default_min: float, default_max: float, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"自定义磨损 - {material_name}")
        form = QFormLayout(self)

        self.spn_min = QDoubleSpinBox()
        self.spn_min.setRange(0.0, 1.0)
        self.spn_min.setDecimals(6)
        self.spn_min.setSingleStep(0.0001)
        self.spn_min.setValue(float(default_min))

        self.spn_max = QDoubleSpinBox()
        self.spn_max.setRange(0.0, 1.0)
        self.spn_max.setDecimals(6)
        self.spn_max.setSingleStep(0.0001)
        self.spn_max.setValue(float(default_max))

        self.chk_reset = QPushButton("↩ 还原为该皮肤默认磨损区间")
        self.chk_reset.clicked.connect(self._on_reset_default)

        form.addRow("最低磨损 (Min):", self.spn_min)
        form.addRow("最高磨损 (Max):", self.spn_max)
        form.addRow("", self.chk_reset)

        self._default_min = float(default_min)
        self._default_max = float(default_max)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self._on_accept)
        btns.rejected.connect(self.reject)
        form.addRow(btns)

    def _on_reset_default(self):
        self.spn_min.setValue(self._default_min)
        self.spn_max.setValue(self._default_max)

    def _on_accept(self):
        if self.spn_min.value() >= self.spn_max.value():
            QMessageBox.warning(
                self, "提示", "最低磨损必须小于最高磨损。")
            return
        self.accept()

    def values(self):
        return self.spn_min.value(), self.spn_max.value()


class MaterialSlot(QFrame):
    """单个材料槽位（10 格之一）。

    操作：
      - 左键点击槽位（空槽/有物品都算）：有物品则删除；空槽无操作。
      - 右键点击槽位（必须有物品）：弹出 WearEditDialog 修改该材料磨损区间。
    （v2 已移除双击修改磨损：避免与常见"双击确认"肌肉记忆冲突。）
    """

    slot_clicked = Signal(int)
    slot_edit_requested = Signal(int)

    def __init__(self, index: int, parent=None):
        super().__init__(parent)
        self.index = index
        self.item = None
        self.setFixedSize(136, 92)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self._apply_empty_style()

        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 3, 4, 3)
        lay.setSpacing(2)

        self.lbl_tag = QLabel("用户自定义")
        self.lbl_tag.setStyleSheet(
            "color:#fff;background:#f1ad4d;"
            "font-size:8px;padding:0 2px;border-radius:2px;")
        self.lbl_tag.setVisible(False)
        lay.addWidget(self.lbl_tag, 0,
                      Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)

        self.lbl_wear = QLabel("—")
        self.lbl_wear.setStyleSheet("color:#999;font-size:9px;")
        self.lbl_name = QLabel(f"空槽 {index + 1}")
        self.lbl_name.setStyleSheet("color:#bbb;font-size:9px;")
        self.lbl_name.setWordWrap(True)
        lay.addStretch()
        lay.addWidget(self.lbl_wear)
        lay.addWidget(self.lbl_name)
        lay.addStretch()

    def _apply_empty_style(self):
        self.setStyleSheet(
            "MaterialSlot{background:#f7f8fa;border:1px dashed #c8cdd4;"
            "border-radius:6px;}")

    def _apply_normal_style(self):
        self.setStyleSheet(
            "MaterialSlot{background:#eef4ee;border:1px solid #488b48;"
            "border-radius:6px;}")

    def _apply_custom_style(self):
        self.setStyleSheet(
            "MaterialSlot{background:#fff7ec;border:2px solid #f1ad4d;"
            "border-radius:6px;}")

    @staticmethod
    def _material_effective_wear_range(material: dict):
        """返回 (wmin, wmax, is_user_custom)，用于槽位显示与编辑默认值。
        注意：为了 WearEditDialog 默认值与搜索边界一致，这里直接走 material_wear_min_max
        （官方档精确 / 用户自定义优先 / paint_wear 夹紧 wear_max）。
        is_user_custom 由 user_wear_* 是否存在判断。
        """
        wmin, wmax = material_wear_min_max(material)
        custom = False
        if "user_wear_min" in material and "user_wear_max" in material:
            try:
                a = float(material["user_wear_min"])
                b = float(material["user_wear_max"])
                if 0.0 <= a < b <= 1.0:
                    custom = True
            except (TypeError, ValueError):
                pass
        if (not custom) and "磨损_min" in material and "磨损_max" in material:
            try:
                a = float(material["磨损_min"])
                b = float(material["磨损_max"])
                if 0.0 <= a < b <= 1.0:
                    custom = True
            except (TypeError, ValueError):
                pass
        return wmin, wmax, custom

    def set_item(self, material: dict):
        self.item = material
        title, rng_txt, is_custom = material_wear_display(material)
        grade = material.get("磨损") or ""
        if not grade:
            # title 里已经包含等级字样了，就直接取第一个空格前字串
            grade = title.split()[0] if title else "未知"
        color = WEAR_COLORS.get(grade, "#333")
        name = _material_name(material)
        if len(name) > 14:
            name = name[:14] + "…"
        # 前缀：自定义带 ★
        prefix = "★ " if is_custom else ""
        self.lbl_wear.setText(f"{prefix}{title}")
        self.lbl_wear.setStyleSheet(f"color:{color};font-size:9px;")
        self.lbl_wear.setToolTip(f"默认搜索边界（≤最高磨损往前搜）：{rng_txt}")
        try:
            price = float(material.get("price_buff") or 0)
        except (TypeError, ValueError):
            price = 0.0
        price_txt = f"参考￥{price:.2f}" if price else ""
        self.lbl_name.setText(f"{name}\n{price_txt}")
        self.lbl_name.setStyleSheet("color:#333;font-size:9px;font-weight:bold;")
        self.lbl_tag.setVisible(is_custom)
        if is_custom:
            self._apply_custom_style()
        else:
            self._apply_normal_style()

    def clear_item(self):
        self.item = None
        self.lbl_wear.setText("—")
        self.lbl_wear.setStyleSheet("color:#999;font-size:9px;")
        self.lbl_name.setText(f"空槽 {self.index + 1}")
        self.lbl_name.setStyleSheet("color:#bbb;font-size:9px;")
        self.lbl_tag.setVisible(False)
        self._apply_empty_style()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.slot_clicked.emit(self.index)
        elif event.button() == Qt.MouseButton.RightButton:
            if self.item is not None:
                self.slot_edit_requested.emit(self.index)
        super().mouseReleaseEvent(event)


class StockCard(QFrame):
    """左侧主 CSV 材料卡片（点击加入材料）。"""

    card_clicked = Signal(dict)

    def __init__(self, material: dict, parent=None):
        super().__init__(parent)
        self.item = material
        self.setFixedSize(132, 84)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet(
            "StockCard{background:#fff;border:1px solid #e3e6ea;"
            "border-radius:6px;} "
            "StockCard:hover{background:#eef4ee;border-color:#488b48;}")

        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 4, 6, 4)
        lay.setSpacing(2)

        wmin, wmax = material_wear_min_max(material)
        grade = material.get("磨损") or _wear_grade(wmin) or "未知"
        color = WEAR_COLORS.get(grade, "#333")
        self.lbl_grade = QLabel(grade)
        self.lbl_grade.setStyleSheet(f"color:{color};font-size:9px;")
        lay.addWidget(self.lbl_grade)

        self.lbl_wear = QLabel(f"{wmin:.4f}~{wmax:.4f}")
        self.lbl_wear.setStyleSheet("color:#666;font-size:9px;")
        self.lbl_wear.setToolTip(
            f"官方精确档/自定义磨损搜索范围（≤最高磨损往前搜）：{wmin:.6f} ~ {wmax:.6f}")
        lay.addWidget(self.lbl_wear)

        name = _material_name(material)
        if len(name) > 12:
            name = name[:12] + "…"
        self.lbl_name = QLabel(name)
        self.lbl_name.setStyleSheet(
            "color:#333;font-size:9px;font-weight:bold;")
        self.lbl_name.setToolTip(_material_name(material))
        lay.addWidget(self.lbl_name)

        try:
            price = float(material.get("price_buff") or 0)
        except (TypeError, ValueError):
            price = 0.0
        price_txt = f"参考￥{price:.2f}" if price else ""
        self.lbl_price = QLabel(price_txt)
        self.lbl_price.setStyleSheet("color:#f56c6c;font-size:9px;")
        lay.addWidget(self.lbl_price)
        lay.addStretch()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.card_clicked.emit(self.item)
        super().mouseReleaseEvent(event)


class AutoTaihuanPanel(QWidget):
    """自动汰换面板（材料方案版）。"""

    request_refresh_history = Signal()

    # 材料卡片单次渲染上限：几千张一次建会卡死 UI；超过时提示输入关键字
    _MAX_CARDS = 300

    def __init__(self, parent=None):
        super().__init__(parent)
        self._click_worker: Optional[GameClickWorker] = None
        self._find_worker: Optional[MaterialFindWorker] = None
        self._assets_worker: Optional[QThread] = None
        self._selected_materials: list = []   # 已选 10 件材料（主 CSV 行 dict）
        self._current_plan_id: Optional[int] = None
        # 「开始寻找材料」的完整查询结果（含每材料 items 在售列表）
        self._find_results: list = []
        self._material_buy_worker: Optional[QThread] = None
        # 缓存的主 CSV 行（异步加载后填充；主线程筛选不再重解析 CSV）
        self._main_csv_rows_cache: list = []
        self._assets_loaded: bool = False
        self._init_ui()
        self._start_async_assets_load()

    def _start_async_assets_load(self):
        """启动后台资源加载；加载完后回填下拉、卡片、方案列表。"""
        from gui.taihuan_worker import LoadTaihuanAssetsWorker
        self.lbl_inv_info.setText("⏳ 正在后台加载主 CSV / 方案列表 ...")
        self._assets_worker = LoadTaihuanAssetsWorker()
        if hasattr(self._assets_worker, "log_message"):
            self._assets_worker.log_message.connect(self.txt_log.append)
        self._assets_worker.finished_ok.connect(self._on_assets_loaded)
        self._assets_worker.error.connect(self._on_assets_load_error)
        self._assets_worker.start()

    def _on_assets_loaded(self, payload: dict):
        self._main_csv_rows_cache = list(payload.get("main_csv_rows") or [])
        self._fill_filter_dropdowns(
            collections=payload.get("collections"),
            rarities=payload.get("rarities"),
        )
        self._reload_saved_plans(plans=payload.get("saved_plans"))
        self._refresh_material_cards()
        self._assets_loaded = True
        self.txt_log.append(
            f"✓ 自动汰换面板资源加载完成：{len(self._main_csv_rows_cache)} 件材料；"
            f"{len(payload.get('saved_plans') or [])} 个已保存方案。")

    def _on_assets_load_error(self, msg: str):
        self.lbl_inv_info.setText("❌ 资源加载失败，重试请切换面板或重启。")
        self.txt_log.append(f"✗ 自动汰换资源加载失败：{msg}")
        QMessageBox.critical(self, "资源加载失败", msg)

    # ============================================================
    # UI 初始化
    # ============================================================
    def _init_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        # 整页改用【垂直 QSplitter】：三大功能块（上半材料区 / ECO 结果区 /
        # 底部日志+执行）高度均可拖拽分割条自由调整，窗口缩放时按当前比例伸缩。
        # 水平方向两个 QSplitter（左筛选|右槽位、日志|执行）本来就支持拖拽，
        # 由此形成完整的可自由调整占地的网格布局。
        root_split = QSplitter(Qt.Orientation.Vertical)
        root_split.setHandleWidth(7)
        root_split.setChildrenCollapsible(False)   # 不允许拖到 0 隐藏

        # 1) 上半：原左侧筛选+右侧槽位+方案
        # 右面板内容刚性较高（固定尺寸槽位卡+节流框+结果/方案区），不套滚动区的
        # 话它的最小高度会顶住 Splitter，导致上下拖不动 → 套 QScrollArea 让
        # 最小高度塌缩，面板拖小时内部出滚动条而不是卡死。
        split = QSplitter(Qt.Orientation.Horizontal)
        split.setHandleWidth(7)
        split.setChildrenCollapsible(False)
        split.addWidget(self._build_left_panel())
        right_scroll = QScrollArea()
        right_scroll.setWidgetResizable(True)
        right_scroll.setFrameShape(QFrame.Shape.NoFrame)
        right_scroll.setStyleSheet("QScrollArea{border:none;}")
        right_scroll.setWidget(self._build_right_panel())
        split.addWidget(right_scroll)
        split.setSizes([560, 340])
        split.setMinimumHeight(150)
        root_split.addWidget(split)

        # 2) 中：ECO 模拟结果展示区（固定框）—— 就是你要的"把结果放到固定的地方"
        eco_box = self._build_eco_result_box()
        eco_box.setMinimumHeight(160)   # 允许被拖到较小高度
        root_split.addWidget(eco_box)

        # 3) 下：系统日志 + 执行汰换（执行区同样套滚动区，允许压得很矮）
        log_exec_split = QSplitter(Qt.Orientation.Horizontal)
        log_exec_split.setHandleWidth(7)
        log_exec_split.setChildrenCollapsible(False)
        log_exec_split.addWidget(self._build_log_box())
        exec_scroll = QScrollArea()
        exec_scroll.setWidgetResizable(True)
        exec_scroll.setFrameShape(QFrame.Shape.NoFrame)
        exec_scroll.setStyleSheet("QScrollArea{border:none;}")
        exec_scroll.setWidget(self._build_exec_box())
        log_exec_split.addWidget(exec_scroll)
        log_exec_split.setSizes([620, 280])
        log_exec_split.setMinimumHeight(90)
        root_split.addWidget(log_exec_split)

        root_split.setSizes([300, 380, 200])
        root.addWidget(root_split)

    # ---- 左下：系统日志框（独立、可滚动） ----
    def _build_log_box(self):
        box = QGroupBox("系统日志（查价 / 模拟 / 保存配方 / 真实对比）")
        lay = QVBoxLayout(box)
        lay.setContentsMargins(6, 4, 6, 4)
        self.txt_log = QTextEdit()
        self.txt_log.setReadOnly(True)
        self.txt_log.setStyleSheet(
            "QTextEdit{font-family:Consolas,Menlo,monospace;font-size:11px;}")
        lay.addWidget(self.txt_log, 1)
        return box

    # ---- 左侧：筛选 + 主 CSV 材料卡片 ----
    def _build_left_panel(self):
        panel = QWidget()
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)

        # 筛选栏
        filter_row = QHBoxLayout()
        filter_row.setSpacing(4)

        self.edit_search = QLineEdit()
        self.edit_search.setPlaceholderText("搜索皮肤/收藏品关键字")
        # 防抖 300ms：每敲一个字符就全量重建卡片网格会卡死 UI，
        # 攒 300ms 停顿后一次性刷新
        self._search_debounce = QTimer(self)
        self._search_debounce.setSingleShot(True)
        self._search_debounce.setInterval(300)
        self._search_debounce.timeout.connect(self._refresh_material_cards)
        self.edit_search.textChanged.connect(
            lambda _: self._search_debounce.start())
        filter_row.addWidget(self.edit_search, 1)

        self.combo_collection = QComboBox()
        self.combo_collection.currentIndexChanged.connect(
            lambda _: self._refresh_material_cards())
        filter_row.addWidget(self.combo_collection)

        self.combo_quality = QComboBox()
        self.combo_quality.currentIndexChanged.connect(
            lambda _: self._refresh_material_cards())
        filter_row.addWidget(self.combo_quality)

        self.combo_wear = QComboBox()
        self.combo_wear.currentIndexChanged.connect(
            lambda _: self._refresh_material_cards())
        filter_row.addWidget(self.combo_wear)

        lay.addLayout(filter_row)
        self._fill_filter_dropdowns()

        self.lbl_inv_info = QLabel("- 件材料可选")
        self.lbl_inv_info.setStyleSheet("color:#888;font-size:10px;")
        lay.addWidget(self.lbl_inv_info)

        # 卡片网格（可滚动）
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea{border:1px solid #e3e6ea;}")
        self.grid_widget = QWidget()
        self.grid_layout = QGridLayout(self.grid_widget)
        self.grid_layout.setSpacing(4)
        self.grid_layout.setContentsMargins(4, 4, 4, 4)
        self.grid_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        scroll.setWidget(self.grid_widget)
        lay.addWidget(scroll, 1)

        return panel

    # ---- 右侧：材料槽位 + 查价 + 方案管理 ----
    def _build_right_panel(self):
        panel = QWidget()
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(4, 0, 0, 0)
        lay.setSpacing(6)

        # 头部信息
        header = QHBoxLayout()
        self.lbl_mat_count = QLabel("材料数量： 0 / 10")
        self.lbl_mat_count.setStyleSheet(
            "font-size:11px;font-weight:bold;color:#333;")
        header.addWidget(self.lbl_mat_count)
        header.addStretch()
        self.lbl_ref_total = QLabel("参考总价：￥0.00")
        self.lbl_ref_total.setStyleSheet(
            "color:#f5a623;font-size:11px;font-weight:bold;")
        header.addWidget(self.lbl_ref_total)
        lay.addLayout(header)

        # 10 个槽位网格（2 行 × 5 列）
        slot_wrap = QWidget()
        slot_grid = QGridLayout(slot_wrap)
        slot_grid.setSpacing(4)
        self.slots: list[MaterialSlot] = []
        for i in range(10):
            slot = MaterialSlot(i)
            slot.slot_clicked.connect(self._on_slot_clicked)
            slot.slot_edit_requested.connect(self._on_slot_edit_requested)
            self.slots.append(slot)
            slot_grid.addWidget(slot, i // 5, i % 5)
        lay.addWidget(slot_wrap)

        # ---------------- 【新增：槽位旁边 → BUFF 查询节流（防 429/价格 None）绿色配置框】---------------
        throttle_box = QGroupBox("🐌 BUFF 查询节流（防止 429 限流 → 部分价格 None）")
        throttle_box.setStyleSheet(
            "QGroupBox {"
            "  border: 1.5px solid #43A047; border-radius: 6px;"
            "  margin-top: 6px; padding-top: 4px;"
            "  font-weight: bold; color: #2E7D32;"
            "}"
            "QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; }"
        )
        tb_lay = QFormLayout(throttle_box)
        tb_lay.setContentsMargins(8, 10, 8, 6)
        tb_lay.setSpacing(6)

        tip = QLabel(
            "10 件材料并发查价时，如果 Buff 出现 <b style=\"color:#C62828\">429 触发限流</b>"
            " 导致表格里 BUFF 列部分 <b>None / ￥0.00</b>：把下面的值调大。<br>"
            "默认 1.0s 对普通账号比较稳；<b>0</b> = 完全不靠主动节流（仅靠指数退避重试，429 风险高）；"
            "<b>≥2.0</b> 超稳但更慢。")
        tip.setWordWrap(True)
        tip.setStyleSheet("color:#555;font-size:10px;font-weight:normal;")
        tb_lay.addRow(tip)

        row1 = QHBoxLayout()
        row1.setSpacing(6)
        lbl_interval = QLabel("每条 BUFF 查询前等待：")
        lbl_interval.setStyleSheet("font-weight:bold;color:#2E7D32;")
        row1.addWidget(lbl_interval)

        self.spn_buff_throttle = QDoubleSpinBox()
        self.spn_buff_throttle.setRange(0.0, 10.0)
        self.spn_buff_throttle.setSingleStep(0.1)
        self.spn_buff_throttle.setDecimals(1)
        self.spn_buff_throttle.setSuffix(" 秒 / 请求")
        self.spn_buff_throttle.setToolTip(
            "范围 0.0 ~ 10.0 秒。每个 Buff 平台 HTTP 请求（10件并发里每件单独发）前 sleep。\n"
            "如果仍 429 → 调到 1.5s 或更大；如果查询速度太慢又没 429 → 调到 0.3~0.6s。\n"
            "保存后立即生效（user_settings 持久化，下次启动自动读）。")
        # 初始值：读取 user_settings 覆盖 or config 默认
        from utils.config import BUFF_REQUEST_INTERVAL as _CFG_INTERVAL
        init_val = _us.get_buff_request_interval(_CFG_INTERVAL)
        self.spn_buff_throttle.setValue(init_val)
        row1.addWidget(self.spn_buff_throttle, 1)

        btn_save = QPushButton("💾 保存（立即生效）")
        btn_save.setStyleSheet(
            "QPushButton { background-color: #43A047; color: white; padding: 5px 12px;"
            " font-weight: bold; border-radius: 3px; }"
            "QPushButton:hover { background-color: #388E3C; }")
        btn_save.clicked.connect(self._on_save_buff_throttle)
        row1.addWidget(btn_save)

        btn_reset = QPushButton("♻️ 重置为 1.0s 推荐值")
        btn_reset.setToolTip("把间隔重置为建议值 1.0 秒（对普通账号 429 概率较低）")
        btn_reset.clicked.connect(lambda: (
            self.spn_buff_throttle.setValue(_BUFF_THROTTLE_RECOMMENDED_S),
            self._on_save_buff_throttle(),
        ))
        row1.addWidget(btn_reset)

        btn_clear = QPushButton("🧹 回退 config 默认值")
        btn_clear.setToolTip("删除 GUI 覆盖值，使用 utils/config.py 的 BUFF_REQUEST_INTERVAL（不修改 config.py 源码）")
        btn_clear.clicked.connect(self._on_clear_buff_throttle)
        row1.addWidget(btn_clear)
        row1.addStretch()
        tb_lay.addRow(row1)

        self.lbl_buff_throttle_status = QLabel(self._buff_throttle_status_text(init_val, source="loaded"))
        self.lbl_buff_throttle_status.setWordWrap(True)
        self.lbl_buff_throttle_status.setStyleSheet("font-size:10px;color:#555;")
        tb_lay.addRow(self.lbl_buff_throttle_status)
        lay.addWidget(throttle_box)
        # ------------------------------------------------------------------------------

        # ECO 自定义饰品 / 官方模拟 按钮行
        eco_btn_row = QHBoxLayout()
        # 替换模式开关：材料查价后额外串行探查更低磨损档的 Buff 最低价，
        # 更便宜则在材料表备注列标红提示（仅提示，不改成本/模拟输入）
        self.btn_replace_mode = QPushButton("🔄 替换模式：开")
        self.btn_replace_mode.setCheckable(True)
        self.btn_replace_mode.setChecked(
            bool(_us.get(_us.REPLACE_PROBE_ENABLED_KEY, True)))
        self._update_replace_mode_btn()
        self.btn_replace_mode.clicked.connect(self._on_toggle_replace_mode)
        self.btn_replace_mode.setToolTip(
            "开启：材料价格查完后，排队串行查询每件材料【更低磨损档】的 Buff 全局最低价\n"
            "（每档 1 页；如久经→查略磨+崭新）。若某低档更便宜，在 10 件材料表的备注列\n"
            "标红显示『物品名称 档位名 价格』，供手动向上替换参考。\n"
            "关闭：不额外查询（速度最快）。开关状态会保存。")
        eco_btn_row.addWidget(self.btn_replace_mode)
        self.btn_sync_custom = QPushButton("📋 同步到 ECO 自定义饰品")
        self.btn_sync_custom.clicked.connect(self._on_sync_eco_custom)
        eco_btn_row.addWidget(self.btn_sync_custom)
        self.btn_clear_custom = QPushButton("🗑 清空 ECO 自定义饰品")
        self.btn_clear_custom.clicked.connect(self._on_clear_eco_custom)
        eco_btn_row.addWidget(self.btn_clear_custom)
        self.btn_official_sim = QPushButton("🧪 ECO 官方汰换模拟")
        self.btn_official_sim.setStyleSheet(
            "QPushButton{background:#6750A4;color:#fff;padding:6px 14px;"
            "font-weight:bold;}")
        self.btn_official_sim.clicked.connect(self._on_official_simulation)
        eco_btn_row.addWidget(self.btn_official_sim, 1)
        lay.addLayout(eco_btn_row)

        # 按钮：清空已选 / 开始寻找材料
        btn_row = QHBoxLayout()
        self.btn_clear = QPushButton("清空已选")
        self.btn_clear.clicked.connect(self._on_clear_selected)
        btn_row.addWidget(self.btn_clear)

        self.btn_find = QPushButton("🔍 开始寻找材料")
        self.btn_find.setStyleSheet(
            "QPushButton{background:#488b48;color:#fff;padding:7px 20px;"
            "font-weight:bold;}")
        self.btn_find.clicked.connect(self._on_find_materials)
        btn_row.addWidget(self.btn_find, 1)

        # ---- 一键购买底价材料（2026-09-11）：套数 N × 10 件 ----
        btn_row.addWidget(QLabel("套数:"))
        self.spin_buy_sets = QSpinBox()
        self.spin_buy_sets.setRange(1, 10)
        self.spin_buy_sets.setValue(1)
        self.spin_buy_sets.setToolTip(
            "购买套数：N 套 = 每个材料槽位买 N 件（共 N×10 件）。\n"
            "从查询结果的完整在售列表里按价格升序跨平台取最便宜的 N 件。")
        btn_row.addWidget(self.spin_buy_sets)
        self.btn_buy_materials = QPushButton("🛒 一键购买底价")
        self.btn_buy_materials.setStyleSheet(
            "QPushButton{background:#c0392b;color:#fff;padding:7px 20px;"
            "font-weight:bold;}")
        self.btn_buy_materials.setToolTip(
            "对当前方案 10 件材料，按查询到的底价跨平台购买。\n"
            "先点「🔍 开始寻找材料」查询最新价格，再点此按钮购买。")
        self.btn_buy_materials.clicked.connect(self._on_buy_materials)
        btn_row.addWidget(self.btn_buy_materials)

        # ---- 上传最低价到价格监控（2026-09-12 阈值囤货）----
        self.btn_upload_prices = QPushButton("📤 上传最低价到价格监控")
        self.btn_upload_prices.setStyleSheet(
            "QPushButton{color:#1a7f37;padding:7px 14px;font-weight:bold;}")
        self.btn_upload_prices.setToolTip(
            "把当前 10 件材料的查询最低价上传到「价格检测」页：\n"
            "每材料生成/更新一个监控目标（阈值=最低选用价、磨损区间=材料区间、\n"
            "需求件数=同材料槽位数×套数），配合自动购买实现分批囤货。")
        self.btn_upload_prices.clicked.connect(self._on_upload_min_prices)
        btn_row.addWidget(self.btn_upload_prices)
        lay.addLayout(btn_row)

        # 材料最低价结果
        result_box = QGroupBox("材料最低价（固定磨损范围内）")
        result_lay = QVBoxLayout(result_box)
        result_lay.setContentsMargins(6, 4, 6, 4)
        result_lay.setSpacing(4)
        self.lbl_find_status = QLabel(
            "未查询。点击「开始寻找材料」查询各材料在固定磨损范围内的最低价。")
        self.lbl_find_status.setStyleSheet("color:#888;font-size:10px;")
        self.lbl_find_status.setWordWrap(True)
        result_lay.addWidget(self.lbl_find_status)

        self.tbl_prices = QTableWidget(0, 5)
        self.tbl_prices.setHorizontalHeaderLabels(
            ["#", "材料", "磨损范围", "最低价", "平台"])
        self.tbl_prices.verticalHeader().setVisible(False)
        self.tbl_prices.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tbl_prices.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        self.tbl_prices.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch)
        for c, w in {0: 32, 2: 100, 3: 80, 4: 55}.items():
            self.tbl_prices.horizontalHeader().setSectionResizeMode(
                c, QHeaderView.ResizeMode.Fixed)
            self.tbl_prices.setColumnWidth(c, w)
        # 不再固定 max 高度：面板拉大时表格跟随伸展（可自由放大缩小占地）
        result_lay.addWidget(self.tbl_prices, 1)
        lay.addWidget(result_box, 1)

        # 方案管理
        plan_box = QGroupBox("方案管理")
        plan_lay = QVBoxLayout(plan_box)

        name_row = QHBoxLayout()
        self.edit_plan_name = QLineEdit()
        self.edit_plan_name.setPlaceholderText("方案名称")
        name_row.addWidget(self.edit_plan_name, 1)
        self.btn_save_plan = QPushButton("💾 保存方案")
        self.btn_save_plan.clicked.connect(self._on_save_plan)
        name_row.addWidget(self.btn_save_plan)
        plan_lay.addLayout(name_row)

        self.lst_plans = QListWidget()
        self.lst_plans.itemClicked.connect(self._on_plan_clicked)
        plan_lay.addWidget(self.lst_plans, 1)

        plan_btn_row = QHBoxLayout()
        self.btn_load_plan = QPushButton("📂 载入方案")
        self.btn_load_plan.clicked.connect(self._on_load_plan)
        plan_btn_row.addWidget(self.btn_load_plan)
        self.btn_update_plan = QPushButton("✏ 更新方案")
        self.btn_update_plan.clicked.connect(self._on_update_plan)
        plan_btn_row.addWidget(self.btn_update_plan)
        self.btn_delete_plan = QPushButton("🗑 删除方案")
        self.btn_delete_plan.clicked.connect(self._on_delete_plan)
        plan_btn_row.addWidget(self.btn_delete_plan)
        plan_btn_row.addStretch()
        plan_lay.addLayout(plan_btn_row)
        lay.addWidget(plan_box)

        lay.addStretch()
        return panel

    # ---- 底部：执行游戏点击 ----
    def _build_exec_box(self):
        box = QGroupBox("校准参数 & 执行汰换")
        exec_layout = QHBoxLayout(box)
        exec_col = QVBoxLayout()
        # 为 exec_col 包一个 widget 以便放进 HBox
        exec_wrap = QWidget()
        exec_wrap.setLayout(exec_col)

        calib_col = QFormLayout()
        self.spn_batch = QSpinBox()
        self.spn_batch.setRange(1, 50)
        self.spn_batch.setValue(5)
        self.spn_batch.setToolTip("每 N 格滚轮（1 格滚轮=2 行物品）后进行一次滑块上移修正")
        calib_col.addRow("滚轮批大小:", self.spn_batch)
        self.spn_slider_px = QSpinBox()
        self.spn_slider_px.setRange(1, 20)
        self.spn_slider_px.setValue(1)
        calib_col.addRow("滑块上移像素:", self.spn_slider_px)
        self.spn_delay = QDoubleSpinBox()
        self.spn_delay.setRange(0.1, 10.0)
        self.spn_delay.setSingleStep(0.1)
        self.spn_delay.setValue(1.0)
        calib_col.addRow("点击间隔(秒):", self.spn_delay)
        exec_layout.addLayout(calib_col)

        exec_col = QVBoxLayout()
        exec_btn_row = QHBoxLayout()
        self.btn_execute = QPushButton("▶ 在 CS2 中点击材料")
        self.btn_execute.setStyleSheet(
            "QPushButton{background:#2196F3;color:#fff;padding:8px 16px;"
            "font-weight:bold;}")
        self.btn_execute.clicked.connect(self._on_execute_click)
        exec_btn_row.addWidget(self.btn_execute)
        self.btn_stop = QPushButton("停止")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self._on_stop_click)
        exec_btn_row.addWidget(self.btn_stop)
        exec_btn_row.addStretch()
        exec_col.addLayout(exec_btn_row)

        self.progress = QProgressBar()
        exec_col.addWidget(self.progress)

        # 日志现在在左下独立大框，这里不再重复塞 txt_log
        self.progress.setFormat("执行进度 %p%")

        exec_layout.addWidget(exec_wrap, 1)
        return box

    # ============================================================
    # ECO 模拟结果固定展示区（中间大框）
    # ============================================================
    def _make_metric_card(self, title: str, color: str, key_name: str) -> QFrame:
        """构建 1 张指标卡：Label title / Label value / self._eco_metric_labels[key_name]。"""
        card = QFrame()
        card.setFrameShape(QFrame.Shape.StyledPanel)
        card.setStyleSheet(
            f"QFrame{{border:1px solid #e3e6ea;border-radius:4px;"
            f"background:#ffffff;padding:4px;}}")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(8, 4, 8, 4)
        lay.setSpacing(0)
        t = QLabel(title)
        t.setStyleSheet("font-size:10px;color:#888;")
        lay.addWidget(t)
        val = QLabel("—")
        val.setStyleSheet(f"font-size:14px;font-weight:bold;color:{color};")
        lay.addWidget(val)
        self._eco_metric_labels[key_name] = val
        return card

    def _configure_result_table(
        self, tbl: QTableWidget, headers: list[str],
        stretch_col: int, fixed_widths: dict[int, int]
    ):
        tbl.setHorizontalHeaderLabels(headers)
        tbl.verticalHeader().setVisible(False)
        tbl.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        tbl.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        tbl.setAlternatingRowColors(True)
        tbl.setWordWrap(False)
        tbl.horizontalHeader().setSectionResizeMode(
            stretch_col, QHeaderView.ResizeMode.Stretch)
        for c, w in fixed_widths.items():
            tbl.horizontalHeader().setSectionResizeMode(
                c, QHeaderView.ResizeMode.Fixed)
            tbl.setColumnWidth(c, w)
        tbl.verticalHeader().setDefaultSectionSize(22)

    def _build_eco_result_box(self):
        """固定结果展示框：指标卡 + Tab(模拟结果/真实结果对比)。"""
        self._eco_metric_labels: dict[str, QLabel] = {}

        box = QGroupBox("ECO 汰换结果（🧪 官方模拟 / 💾 配方 / 真实结果对比）")
        lay = QVBoxLayout(box)
        lay.setContentsMargins(6, 4, 6, 4)
        lay.setSpacing(4)

        # ---- 1) 顶部指标卡（成本 / 期望 / 利润 / 保本率 / Source / FormulaID） ----
        metric_row = QHBoxLayout()
        metric_row.setSpacing(6)
        metric_row.addWidget(self._make_metric_card("💰 材料总成本", "#333",       "cost"), 1)
        metric_row.addWidget(self._make_metric_card("📈 加权期望售价", "#6750A4",  "ref_value"), 1)
        metric_row.addWidget(self._make_metric_card("💵 利润（期望-成本）", "#f5a623", "profit"), 1)
        metric_row.addWidget(self._make_metric_card("🛡 保本率", "#488b48",         "keep_rate"), 1)
        metric_row.addWidget(self._make_metric_card("🔎 数据来源", "#555",          "source"), 1)
        metric_row.addWidget(self._make_metric_card("🆔 FormulaID", "#2196F3",     "formula_id"), 1)
        lay.addLayout(metric_row)

        # ---- 2) Tab：官方模拟 vs 真实结果对比 ----
        self.eco_tabs = QTabWidget()

        # Tab A：官方模拟结果
        sim_tab = QWidget()
        sim_lay = QVBoxLayout(sim_tab)
        sim_lay.setContentsMargins(4, 2, 4, 2)
        sim_lay.setSpacing(3)

        # 产出明细表
        self.tbl_eco_outputs = QTableWidget(0, 9)
        self._configure_result_table(
            self.tbl_eco_outputs,
            ["#", "概率", "产出饰品", "磨损", "Buff(元)", "C5(元)",
             "ECO(元)", "三平台最低", "最优平台"],
            2,
            {0: 32, 1: 68, 3: 100, 4: 80, 5: 80, 6: 80, 7: 86, 8: 68}
        )
        # 允许被 Splitter 压得很矮（默认 min 提示会顶住，导致上下拖不动）
        self.tbl_eco_outputs.setMinimumHeight(36)
        self.tbl_eco_materials = QTableWidget(0, 9)
        self._configure_result_table(
            self.tbl_eco_materials,
            ["槽位", "材料饰品", "自定义磨损区间", "Buff(元)", "C5(元)",
             "ECO(元)", "最低选用价", "来源平台", "备注"],
            1,
            {0: 44, 2: 128, 3: 80, 4: 80, 5: 80, 6: 86, 7: 68, 8: 110}
        )
        self.tbl_eco_materials.setMinimumHeight(36)   # 同上：允许压矮

        # 产出表 / 材料表之间也用垂直 QSplitter：两表高度可自由拖拽调整
        sim_tbl_split = QSplitter(Qt.Orientation.Vertical)
        sim_tbl_split.setHandleWidth(6)
        sim_tbl_split.setChildrenCollapsible(False)

        def _wrap_out_sim():
            w = QWidget()
            vl = QVBoxLayout(w)
            vl.setContentsMargins(0, 0, 0, 0)
            vl.setSpacing(2)
            vl.addWidget(QLabel("🎯 可能产出（ECO StartSimulation 官方概率 + 磨损）："), 0)
            vl.addWidget(self.tbl_eco_outputs, 1)
            return w

        def _wrap_mat_sim():
            w = QWidget()
            vl = QVBoxLayout(w)
            vl.setContentsMargins(0, 0, 0, 0)
            vl.setSpacing(2)
            vl.addWidget(QLabel("📦 10 件材料（三平台实时最低价 + 自定义磨损）："), 0)
            vl.addWidget(self.tbl_eco_materials, 1)
            return w

        sim_tbl_split.addWidget(_wrap_out_sim())
        sim_tbl_split.addWidget(_wrap_mat_sim())
        sim_tbl_split.setSizes([240, 160])
        sim_lay.addWidget(sim_tbl_split, 1)

        self.eco_tabs.addTab(sim_tab, "🧪 官方模拟结果")

        # Tab B：真实结果对比
        real_tab = QWidget()
        real_lay = QVBoxLayout(real_tab)
        real_lay.setContentsMargins(4, 2, 4, 2)
        real_lay.setSpacing(3)

        topbar = QHBoxLayout()
        topbar.addWidget(QLabel("FormulaID:"))
        self.edit_formula_id = QLineEdit()
        self.edit_formula_id.setPlaceholderText(
            "粘贴保存配方返回的 FormulaID 后点击开始对比")
        topbar.addWidget(self.edit_formula_id, 1)
        self.btn_real_compare = QPushButton("📊 真实汰换结果对比")
        self.btn_real_compare.setStyleSheet(
            "QPushButton{background:#2196F3;color:#fff;padding:6px 14px;"
            "font-weight:bold;}")
        self.btn_real_compare.clicked.connect(self._on_real_compare_clicked)
        topbar.addWidget(self.btn_real_compare)
        self.btn_clear_results = QPushButton("🧹 清空结果")
        self.btn_clear_results.clicked.connect(self._clear_eco_results)
        topbar.addWidget(self.btn_clear_results)
        real_lay.addLayout(topbar)

        self.lbl_real_status = QLabel("未对比。保存配方获得 FormulaID 后再执行真实对比。")
        self.lbl_real_status.setStyleSheet("color:#888;font-size:10px;")
        self.lbl_real_status.setWordWrap(True)
        real_lay.addWidget(self.lbl_real_status)

        self.tbl_real_outputs = QTableWidget(0, 10)
        self._configure_result_table(
            self.tbl_real_outputs,
            ["#", "概率/次数", "真实产出饰品", "磨损", "Buff(元)", "C5(元)",
             "ECO(元)", "三平台最低", "最优平台", "权重贡献"],
            2,
            {0: 32, 1: 86, 3: 100, 4: 80, 5: 80, 6: 80, 7: 86, 8: 68, 9: 90}
        )
        self.tbl_real_outputs.setMinimumHeight(36)   # 允许被 Splitter 压矮

        self.tbl_real_materials = QTableWidget(0, 8)
        self._configure_result_table(
            self.tbl_real_materials,
            ["#", "配方材料", "磨损区间", "Buff(元)", "C5(元)",
             "ECO(元)", "最低选用价", "来源平台"],
            1,
            {0: 32, 2: 128, 3: 80, 4: 80, 5: 80, 6: 86, 7: 68}
        )
        self.tbl_real_materials.setMinimumHeight(36)   # 同上：允许压矮

        # 真实产出表 / 配方材料表之间用垂直 QSplitter：高度可自由拖拽调整
        real_tbl_split = QSplitter(Qt.Orientation.Vertical)
        real_tbl_split.setHandleWidth(6)
        real_tbl_split.setChildrenCollapsible(False)

        def _wrap_out_real():
            w = QWidget()
            vl = QVBoxLayout(w)
            vl.setContentsMargins(0, 0, 0, 0)
            vl.setSpacing(2)
            vl.addWidget(QLabel("📊 真实执行结果（RealReplaceResult） × 三平台实时查价："), 0)
            vl.addWidget(self.tbl_real_outputs, 1)
            return w

        def _wrap_mat_real():
            w = QWidget()
            vl = QVBoxLayout(w)
            vl.setContentsMargins(0, 0, 0, 0)
            vl.setSpacing(2)
            vl.addWidget(QLabel("📦 配方 10 件材料（从 FormulaDetail 读取 + 实时成本）："), 0)
            vl.addWidget(self.tbl_real_materials, 1)
            return w

        real_tbl_split.addWidget(_wrap_out_real())
        real_tbl_split.addWidget(_wrap_mat_real())
        real_tbl_split.setSizes([240, 160])
        real_lay.addWidget(real_tbl_split, 1)

        self.eco_tabs.addTab(real_tab, "📊 真实结果对比")
        lay.addWidget(self.eco_tabs, 1)

        return box

    # ============================================================
    # ECO 结果：表格填充 / 清空 / 点击跳转
    # ============================================================
    def _set_metric(self, key: str, text: str):
        lab = self._eco_metric_labels.get(key)
        if lab is not None:
            lab.setText(text)

    def _fmt_price(self, v) -> str:
        try:
            f = float(v)
        except (TypeError, ValueError):
            return "—"
        if f <= 0:
            return "—"
        return f"￥{f:.2f}"

    def _color_cell(self, tbl: QTableWidget, row: int, col: int,
                    value, fg: str = "#333", bg: str | None = None,
                    align=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                    bold: bool = False):
        txt = self._fmt_price(value) if isinstance(value, (int, float)) else str(value)
        item = QTableWidgetItem(txt)
        item.setTextAlignment(align)
        item.setForeground(QBrush(QColor(fg)))
        if bg:
            item.setBackground(QBrush(QColor(bg)))
        if bold:
            f = item.font()
            f.setBold(True)
            item.setFont(f)
        tbl.setItem(row, col, item)

    def _clear_eco_tables(self):
        for tbl in (self.tbl_eco_outputs, self.tbl_eco_materials,
                    self.tbl_real_outputs, self.tbl_real_materials):
            tbl.setUpdatesEnabled(False)
            try:
                tbl.setRowCount(0)
            finally:
                tbl.setUpdatesEnabled(True)

    def _clear_eco_results(self):
        self._clear_eco_tables()
        for k in ("cost", "ref_value", "profit", "keep_rate", "source", "formula_id"):
            self._set_metric(k, "—")
        self.lbl_real_status.setText(
            "未对比。保存配方获得 FormulaID 后再执行真实对比。")
        self.txt_log.append("🧹 已清空 ECO 结果展示区。")

    def _populate_output_table(
        self,
        tbl: QTableWidget,
        outputs: list[dict],
        *,
        prob_col: str = "prob",
        name_col: str = "name",
        hash_col: str = "hash_name",
        wear_col: str = "wear_float",
        extra_contrib: list[float] | None = None,
    ):
        """统一的产出明细填充：官方模拟 output_details / 真实对比 real_rows 都可用。"""
        tbl.setUpdatesEnabled(False)
        try:
            tbl.setRowCount(0)
            if not isinstance(outputs, list):
                outputs = []
            tbl.setRowCount(len(outputs))
            for i, o in enumerate(outputs):
                prob_v = o.get(prob_col)
                try:
                    prob_f = float(prob_v)
                except (TypeError, ValueError):
                    prob_f = 0.0
                # 显示：≥1 按"次数/N次"否则按百分比
                if prob_f >= 1:
                    prob_txt = f"{int(round(prob_f))} 次"
                elif prob_f > 0:
                    prob_txt = f"{prob_f*100:.2f}%"
                else:
                    prob_txt = "—"
                name_txt = (o.get(name_col)
                            or o.get("OutputHashName")
                            or o.get(hash_col)
                            or "未知").strip()
                wear_v = o.get(wear_col)
                try:
                    wear_f = float(wear_v)
                except (TypeError, ValueError):
                    wear_f = None
                # 优先展示 ECO 官方返回的原始磨损字符串（16 位小数全精度）；
                # 没有原始字符串的路径（如真实对比行）回退 .6f 显示
                wear_raw = str(o.get("wear_value_str") or "").strip()
                if wear_raw and wear_f is not None:
                    wear_txt = wear_raw
                elif wear_f is not None:
                    wear_txt = f"{wear_f:.6f}"
                else:
                    wear_txt = "—"
                buff_p = o.get("buff_price")
                c5_p   = o.get("c5_price")
                eco_p  = o.get("eco_price")
                min_p  = o.get("min_price")
                if min_p in (None, ""):
                    try:
                        min_p = o.get("price")
                    except Exception:
                        min_p = None
                best_p = (o.get("platform_best")
                          or o.get("platform")
                          or o.get("best_platform") or "")
                contrib_txt = "—"
                if extra_contrib is not None and i < len(extra_contrib):
                    try:
                        c = float(extra_contrib[i])
                    except (TypeError, ValueError):
                        c = 0.0
                    contrib_txt = f"￥{c:.2f}"

                self._color_cell(tbl, i, 0, str(i + 1),
                                 align=Qt.AlignmentFlag.AlignCenter)
                self._color_cell(tbl, i, 1, prob_txt,
                                 fg="#6750A4", bold=True,
                                 align=Qt.AlignmentFlag.AlignCenter)
                self._color_cell(tbl, i, 2, name_txt,
                                 align=Qt.AlignmentFlag.AlignLeft
                                 | Qt.AlignmentFlag.AlignVCenter)
                self._color_cell(tbl, i, 3, wear_txt)
                self._color_cell(tbl, i, 4, buff_p, "#333")
                self._color_cell(tbl, i, 5, c5_p, "#333")
                self._color_cell(tbl, i, 6, eco_p, "#333")
                try:
                    min_fv = float(min_p)
                except (TypeError, ValueError):
                    min_fv = 0.0
                self._color_cell(tbl, i, 7,
                                 min_p if min_fv > 0 else "—",
                                 fg="#f5a623", bold=True)
                best_txt = best_p or ("自动" if min_fv > 0 else "无在售")
                self._color_cell(tbl, i, 8, best_txt,
                                 align=Qt.AlignmentFlag.AlignCenter)
                # 第 9 列：贡献（真实对比用；官方模拟=￥prob*min_price）
                if tbl.columnCount() >= 10:
                    if extra_contrib is None:
                        try:
                            contrib_val = prob_f * min_fv
                        except Exception:
                            contrib_val = 0.0
                        contrib_txt = (f"￥{contrib_val:.2f}"
                                       if contrib_val > 0 else "—")
                    c_item = QTableWidgetItem(contrib_txt)
                    c_item.setTextAlignment(Qt.AlignmentFlag.AlignRight
                                            | Qt.AlignmentFlag.AlignVCenter)
                    tbl.setItem(i, 9, c_item)
        finally:
            tbl.setUpdatesEnabled(True)

    def _populate_material_table(self,
                                 tbl: QTableWidget,
                                 details: list[dict],
                                 selected_materials: list[dict] | None = None):
        """统一的材料成本明细填充。"""
        tbl.setUpdatesEnabled(False)
        try:
            tbl.setRowCount(0)
            if not isinstance(details, list):
                details = []
            # 确保与 10 件材料对齐
            if selected_materials and len(selected_materials) and len(details) == len(selected_materials):
                zipped = list(zip(details, selected_materials))
            else:
                zipped = [(d, None) for d in details]
            tbl.setRowCount(len(zipped))
            for i, (d, sm) in enumerate(zipped):
                if not isinstance(d, dict):
                    d = {}
                # 名称：优先用槽位选的（中文名+磨损描述），再用 detail 里的
                if isinstance(sm, dict):
                    name_a = _material_name(sm)
                    wear_grade = (sm.get("磨损") or "").strip()
                    if wear_grade:
                        name_txt = f"{name_a}（{wear_grade}）"
                    else:
                        name_txt = name_a
                else:
                    name_txt = (d.get("skin_name") or d.get("hash_name")
                                or d.get("name") or "未知").strip()
                # ---- C2b: 磨损区间显示严格按用户要求 4 级优先级：
                #   1) 有 sm（槽位原始 GUI 材料字典）→ material_wear_min_max(sm)
                #      （user_wear_* 最优先 → 中文磨损档官方 5 档精确 → 宽范围兜底 → paint_wear 夹紧 wear_max）
                #   2) detail(d) 里带 wear_min/wear_max → 用这个（后处理引擎已按官方档/paint_wear 算过）
                #   3) sm 自带 user_wear_* → 在 wear_txt 里追加 ★自定义
                wear_txt = "默认"
                if isinstance(sm, dict):
                    # 有 GUI 原始字典：直接用 GUI helper 计算（严格按官方档/自定义）
                    wmin, wmax = material_wear_min_max(sm)
                    wear_txt = f"{wmin:.4f} ~ {wmax:.4f}"
                    # 是否"自定义"：有 user_wear_* 就算
                    if ("user_wear_min" in sm and "user_wear_max" in sm) \
                            or sm.get("user_wear_enabled"):
                        wear_txt += "  ★自定义"
                    # Tooltip：把 sm 的中文磨损档/官方档/自定义一起写出来
                    _title, _rng, _c = material_wear_display(sm)
                    if isinstance(tbl, QTableWidget):
                        itm = tbl.item(i, 2)
                        tip_txt = f"显示/搜索边界规则：{_title}\n实际数值范围：{_rng}"
                        if itm:
                            itm.setToolTip(tip_txt)
                        else:
                            tbl.setItem(i, 2, QTableWidgetItem())
                            tbl.item(i, 2).setToolTip(tip_txt)
                elif isinstance(d, dict):
                    # 只有 detail（真实对比等没有原始 GUI 字典的场景）
                    wmin = wmax = None
                    for k in ("wear_min", "min_f", "MinFloat", "user_wear_min", "磨损_min"):
                        if wmin is None and d.get(k) not in (None, ""):
                            try: wmin = float(d[k])
                            except (TypeError, ValueError): pass
                    for k in ("wear_max", "max_f", "MaxFloat", "user_wear_max", "磨损_max"):
                        if wmax is None and d.get(k) not in (None, ""):
                            try: wmax = float(d[k])
                            except (TypeError, ValueError): pass
                    if wmin is None:
                        wear_txt = "默认"
                    elif wmax is None:
                        wear_txt = f"≥{wmin:.4f}"
                    else:
                        wear_txt = f"{wmin:.4f} ~ {wmax:.4f}"

                buff_p = d.get("buff_price")
                c5_p   = d.get("c5_price")
                eco_p  = d.get("eco_price")
                min_p  = d.get("min_price")
                best_p = d.get("best_platform") or d.get("platform_best") or ""
                remark = ""
                errs = d.get("errors") if isinstance(d.get("errors"), dict) else {}
                if errs:
                    # ============================================================
                    # 【GUI 备注列完整显示 errors 所有有价值信息】
                    #   之前只取 keys in (buff/c5/eco) → 导致 errors.buff_diag、
                    #   buff_no_results、fallback_from_gui_price 等诊断全被吞，
                    #   空备注列让用户无法判断 Buff 全 None 的原因。
                    #   现在按优先级拼接：
                    #     1) 三个主平台报错（buff/c5/eco 失败）
                    #     2) buff 专属诊断：buff_diag / buff_no_results
                    #     3) 回退/查询未发起：fallback_from_gui_price /
                    #        *_no_results / __all__
                    #     4) 其余未知键：都以 "键:值前120字" 形式加入
                    # ============================================================
                    parts: list[str] = []
                    # 优先级 1：主平台失败（键=buff/c5/eco，值是错误描述）
                    for p in ("buff", "c5", "eco"):
                        if p in errs and errs[p]:
                            try:
                                msg = str(errs[p])[:80]
                            except Exception:
                                msg = "失败"
                            parts.append(f"{p.upper()}失败:{msg}")
                    # 优先级 2：buff 诊断详情（从严格模式 0 结果写入）
                    for key in ("buff_diag", "buff_no_results"):
                        if key in errs and errs[key]:
                            try:
                                msg = str(errs[key])[:160]
                            except Exception:
                                msg = ""
                            if msg:
                                parts.append(msg)
                    # 优先级 3：回退/未发起（保持 80 字截断）
                    for key in ("fallback_from_gui_price",
                                 "c5_no_results", "eco_no_results", "__all__"):
                        if key in errs and errs[key]:
                            try:
                                msg = str(errs[key])[:80]
                            except Exception:
                                msg = ""
                            if msg:
                                parts.append(msg)
                    # 优先级 4：剩下的键（防御未来扩展）
                    already = {"buff","c5","eco","buff_diag","buff_no_results",
                               "fallback_from_gui_price","c5_no_results",
                               "eco_no_results","__all__"}
                    for k, v in errs.items():
                        if k in already or not v:
                            continue
                        try:
                            msg = f"{k}:{str(v)[:80]}"
                        except Exception:
                            msg = f"{k}"
                        parts.append(msg)
                    remark = "\n".join(parts)
                # 仍然保留 min=0 但没错误时的兜底提示（防止上面 parts 生成空串时看不到任何标识）
                if min_p in (None, 0, 0.0, "") and not remark:
                    remark = "无在售"
                # ---- 替换模式：更低磨损档更便宜 → 红字提示（物品名称 档位名 价格）----
                lower_txt = ""
                lower_info = d.get("lower_tier_cheaper")
                if isinstance(lower_info, dict):
                    try:
                        lower_txt = (f"↘ {lower_info.get('skin_name', '')} "
                                     f"{lower_info.get('wear_cn', '')} "
                                     f"¥{float(lower_info.get('price') or 0):.2f}")
                    except (TypeError, ValueError):
                        lower_txt = ""

                slot_txt = str(i + 1)
                if tbl is self.tbl_eco_materials:
                    # 真实对比表里没有槽位编号概念，就用 #
                    pass

                self._color_cell(tbl, i, 0, slot_txt,
                                 align=Qt.AlignmentFlag.AlignCenter)
                self._color_cell(tbl, i, 1, name_txt,
                                 align=Qt.AlignmentFlag.AlignLeft
                                 | Qt.AlignmentFlag.AlignVCenter)
                fg_wear = "#333"
                if "★自定义" in wear_txt:
                    fg_wear = "#b7625f"
                self._color_cell(tbl, i, 2, wear_txt, fg=fg_wear)
                self._color_cell(tbl, i, 3, buff_p)
                self._color_cell(tbl, i, 4, c5_p)
                self._color_cell(tbl, i, 5, eco_p)
                try:
                    min_fv = float(min_p)
                except (TypeError, ValueError):
                    min_fv = 0.0
                self._color_cell(tbl, i, 6,
                                 min_p if min_fv > 0 else "—",
                                 fg="#f5a623", bold=True)
                self._color_cell(tbl, i, 7, best_p or "—",
                                 align=Qt.AlignmentFlag.AlignCenter)
                # 第 8 列备注（低档更便宜的红字提示优先，其次查询失败/无在售）
                if tbl.columnCount() >= 9:
                    if lower_txt:
                        cell_txt = lower_txt + (f"\n{remark}" if remark else "")
                        self._color_cell(tbl, i, 8, cell_txt,
                                         fg="#C62828", bold=True,
                                         align=Qt.AlignmentFlag.AlignLeft
                                         | Qt.AlignmentFlag.AlignVCenter)
                    else:
                        self._color_cell(tbl, i, 8, remark,
                                         fg="#a66" if remark else "#888",
                                         align=Qt.AlignmentFlag.AlignLeft
                                         | Qt.AlignmentFlag.AlignVCenter)
        finally:
            tbl.setUpdatesEnabled(True)

    def _fill_filter_dropdowns(self, collections=None, rarities=None):
        """填充收藏品/品质/磨损等级下拉框。

        Args:
            collections: 若传入则不再主线程查主 CSV；None=用 get_unique_collections()。
            rarities:   同上。
        """
        if collections is None:
            from core.data_manager import get_unique_collections
            cols = get_unique_collections()
        else:
            cols = list(collections)
        self.combo_collection.blockSignals(True)
        self.combo_collection.clear()
        self.combo_collection.addItem("全部收藏品")
        for c in cols:
            if c != "全部":
                self.combo_collection.addItem(c)
        self.combo_collection.blockSignals(False)

        if rarities is None:
            from core.data_manager import get_unique_rarities
            rars = get_unique_rarities()
        else:
            rars = list(rarities)
        self.combo_quality.blockSignals(True)
        self.combo_quality.clear()
        self.combo_quality.addItem("全部品质")
        ordered = []
        for q in QUALITY_ORDER:
            if q in rars:
                ordered.append(q)
        for q in reversed(ordered):
            self.combo_quality.addItem(q)
        self.combo_quality.blockSignals(False)

        self.combo_wear.blockSignals(True)
        self.combo_wear.clear()
        self.combo_wear.addItems(
            ["全部磨损", "崭新出厂", "略有磨损", "久经沙场", "破损不堪", "战痕累累"])
        self.combo_wear.blockSignals(False)

    def _refresh_material_cards(self):
        """按筛选条件刷新左侧主 CSV 材料卡片网格。

        优先使用后台加载缓存的 _main_csv_rows_cache；否则兜底同步加载。
        """
        if self._main_csv_rows_cache:
            rows = self._main_csv_rows_cache
        else:
            try:
                rows = load_items_from_main_csv_full()
            except Exception as e:
                logger.warning("读取主 CSV 失败: %s", e)
                rows = []
            self._main_csv_rows_cache = list(rows)

        # 筛选
        kw = self.edit_search.text().strip().lower()
        col = self.combo_collection.currentText() if hasattr(self, "combo_collection") else "全部收藏品"
        ql = self.combo_quality.currentText() if hasattr(self, "combo_quality") else "全部品质"
        wr = self.combo_wear.currentText() if hasattr(self, "combo_wear") else "全部磨损"

        filtered = []
        for it in rows:
            skin = (it.get("皮肤名称") or "").lower()
            coll = (it.get("收藏品名称") or "").lower()
            if kw and kw not in skin and kw not in coll:
                continue
            if col != "全部收藏品" and it.get("收藏品名称") != col:
                continue
            if ql != "全部品质" and it.get("品质") != ql:
                continue
            if wr != "全部磨损" and it.get("磨损") != wr:
                continue
            filtered.append(it)

        # 已选材料高亮：记录 (皮肤名称, 磨损) 组合
        selected_keys = {
            (m.get("皮肤名称"), m.get("磨损")) for m in self._selected_materials}

        # 清空网格
        while self.grid_layout.count():
            item = self.grid_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

        self.lbl_inv_info.setText(
            f"{len(filtered)} 件材料可选（主 CSV）"
            + (f"，仅显示前 {self._MAX_CARDS} 张（输入关键字缩小范围）"
               if len(filtered) > self._MAX_CARDS else ""))

        # 批量创建卡片时禁止父级重绘，避免百次 addWidget 连续触发布局重排
        self.grid_widget.setUpdatesEnabled(False)
        try:
            COLUMNS = 4
            shown = filtered[:self._MAX_CARDS]   # 上限：几千张卡片一次渲染必卡
            for idx, it in enumerate(shown):
                card = StockCard(it)
                card.card_clicked.connect(self._on_card_clicked)
                if (it.get("皮肤名称"), it.get("磨损")) in selected_keys:
                    card.setStyleSheet(
                        "StockCard{background:#e8f2e8;border:1px solid #488b48;"
                        "border-radius:6px;}")
                self.grid_layout.addWidget(card, idx // COLUMNS, idx % COLUMNS)

            # 对齐顶部 + 空态提示
            if not filtered:
                tip = QLabel("没有搜索到结果，换一个关键词试一试")
                tip.setStyleSheet("color:#999;padding:20px;")
                self.grid_layout.addWidget(tip, 0, 0, 1, COLUMNS)
        finally:
            self.grid_widget.setUpdatesEnabled(True)

    # ============================================================
    # 卡片 → 槽位
    # ============================================================
    def _on_card_clicked(self, item: dict):
        if len(self._selected_materials) >= 10:
            QMessageBox.information(self, "提示", "材料已满 10 件，请先移除部分材料。")
            return
        if not item.get("buff_goods_id") and not item.get("市场哈希名称"):
            QMessageBox.warning(
                self, "提示",
                f"材料「{item.get('皮肤名称')}」缺少平台 ID，无法自动查价，"
                f"仍可加入（仅作方案记录）。")
        self._selected_materials.append(dict(item))
        self._sync_slots()
        self.txt_log.append(
            f"➕ 加入材料: {item.get('皮肤名称')}（{item.get('磨损')}）"
            f"（槽位 {len(self._selected_materials)}，允许重复）")

    def _on_slot_edit_requested(self, index: int):
        if index >= len(self._selected_materials):
            return
        mat = self._selected_materials[index]
        # 默认值=当前生效值；用户可手动改，或「还原默认」回到皮肤固定磨损区间
        cur_min, cur_max, _ = MaterialSlot._material_effective_wear_range(mat)
        default_min, default_max = _parse_wear_range(mat)
        dlg = WearEditDialog(
            _material_name(mat), default_min, default_max, self)
        dlg.spn_min.setValue(float(cur_min))
        dlg.spn_max.setValue(float(cur_max))
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        new_min, new_max = dlg.values()
        # 仅当与默认区间不一致时，写入 user_wear_* 字段；一致则清除
        if (abs(new_min - default_min) < 1e-9
                and abs(new_max - default_max) < 1e-9):
            mat.pop("user_wear_min", None)
            mat.pop("user_wear_max", None)
            tag = ""
        else:
            mat["user_wear_min"] = float(new_min)
            mat["user_wear_max"] = float(new_max)
            tag = "（自定义）"
        # 同步保存字段：保存方案时也能还原
        mat["磨损_min"] = float(new_min)
        mat["磨损_max"] = float(new_max)
        self._sync_slots()
        self.txt_log.append(
            f"✏️ 槽位 {index + 1} 磨损更新为 "
            f"{new_min:.6f}~{new_max:.6f}{tag}")

    def _on_slot_clicked(self, index: int):
        if index >= len(self._selected_materials):
            return
        removed = self._selected_materials.pop(index)
        self._sync_slots()
        self.txt_log.append(f"➖ 移除材料: {_material_name(removed)}")

    # ============================================================
    # ECO 自定义饰品 / 官方模拟
    # ============================================================
    def _current_10_materials_for_eco(self):
        """校验并返回 10 件材料元数据，用于 customBatchAdd / StartSimulation。

        Returns (ok, mats, msg)
          mats = list[dict]，每项含 slot_idx / HashName / name_cn /
                 wear_min / wear_max / remark / buff_goods_id /
                 collection / quality / _wear_cn / market_hash_name /
                 user_wear_min / user_wear_max / _price / 皮肤名称 / 磨损。
        【关键】：所有"price_fetcher 实时查价" & "_normalize_materials" 所需字段
                 （buff_goods_id / 中文磨损档 / 收藏品 / 品质 / user_wear_*）
                 都必须透传到 gui_mats，否则 _finish_normalize_one 的 lookup
                 会被 need_lookup 短路或反查失败 → buff_goods_id 空 → Buff 查价
                 无法按 goods_id 搜到 → 材料 min_price=0 → 总成本=0。
        """
        if len(self._selected_materials) != 10:
            return False, [], f"当前材料 {len(self._selected_materials)}/10，请凑满 10 件。"
        from core.data_manager import build_c5_market_hash_name
        mats = []
        for i, m in enumerate(self._selected_materials):
            hash_base = (m.get("市场哈希名称") or "").strip()
            wear_cn = (m.get("磨损") or "").strip()
            full_hash = build_c5_market_hash_name(hash_base, wear_cn)
            if not full_hash:
                return (False, [],
                        f"第 {i + 1} 件材料缺少 市场哈希名称/磨损，"
                        "无法构造 ECO 所需 HashName")
            wmin, wmax, _ = MaterialSlot._material_effective_wear_range(m)
            # 把 GUI 主 CSV 行的所有元数据都透传到 gui_mats（关键修复：buff_goods_id）
            # 以及用户右键自定义的 user_wear_min/user_wear_max、GUI 预查好的 _price
            mat_out = {
                # eco API / HTTP 所需
                "slot_idx": i,
                "HashName": full_hash,
                "name_cn": _material_name(m),
                "wear_min": float(wmin),
                "wear_max": float(wmax),
                "remark": f"自动汰换面板 槽位{i + 1}",
                # 给 _normalize_materials → price_fetcher.query_all 的关键字段
                "buff_goods_id": (m.get("buff_goods_id") or "").strip(),
                "hash_name": full_hash,
                "market_hash_name": full_hash,
                "c5_market_hash_name": full_hash,
                "皮肤名称": _material_name(m),
                "磨损": wear_cn,
                "wear_grade": wear_cn,
                "collection": (m.get("收藏品名称") or m.get("collection") or "").strip(),
                "quality":    (m.get("品质")       or m.get("quality")    or "").strip(),
                "_price":     (m.get("price_buff") or m.get("_price")  or 0) or None,
            }
            # 自定义磨损范围：GUI 用户右键改的 user_wear_min/max 最高优先级
            # user_wear_min/max 字段存在但值为 None（或空）表示"未启用自定义"
            # （新导入的 CS2UP 方案就全是 None），必须跳过而非直接 float
            if "user_wear_min" in m and "user_wear_max" in m \
                    and m.get("user_wear_min") is not None \
                    and m.get("user_wear_max") is not None \
                    and m["user_wear_min"] != "" and m["user_wear_max"] != "":
                try:
                    a, b = float(m["user_wear_min"]), float(m["user_wear_max"])
                    if 0.0 <= a < b <= 1.0:
                        mat_out["user_wear_min"] = a
                        mat_out["user_wear_max"] = b
                except (TypeError, ValueError):
                    pass
            # 兼容保存方案的「磨损_min / 磨损_max」
            if "磨损_min" in m and "磨损_max" in m \
                    and m.get("磨损_min") not in (None, "") \
                    and m.get("磨损_max") not in (None, ""):
                try:
                    a, b = float(m["磨损_min"]), float(m["磨损_max"])
                    if 0.0 <= a < b <= 1.0:
                        mat_out["磨损_min"] = a
                        mat_out["磨损_max"] = b
                except (TypeError, ValueError):
                    pass
            # 皮肤实际浮点区间（主 CSV「磨损区间」）：ECO StartSimulation 会校验
            # WearValue ∈ (皮肤区间)∩(档位区间) 开区间（如 Candy Apple 最高 0.30，
            # 传 0.38 → 2001 材料磨损值错误），传给 eco_web_api 做夹紧。
            smin, smax = _parse_wear_range(m)
            if 0.0 <= smin < smax <= 1.0:
                mat_out["skin_wear_min"] = float(smin)
                mat_out["skin_wear_max"] = float(smax)
            mats.append(mat_out)
        return True, mats, ""

    def _update_replace_mode_btn(self):
        """按当前开关状态刷新替换模式按钮的文字与配色。"""
        on = self.btn_replace_mode.isChecked()
        self.btn_replace_mode.setText(f"🔄 替换模式：{'开' if on else '关'}")
        self.btn_replace_mode.setStyleSheet(
            ("QPushButton{background:#43A047;color:#fff;padding:6px 12px;"
             "font-weight:bold;border-radius:3px;}")
            if on else
            ("QPushButton{background:#9E9E9E;color:#fff;padding:6px 12px;"
             "font-weight:bold;border-radius:3px;}")
        )

    def _on_toggle_replace_mode(self, checked: bool):
        """切换替换模式：持久化到 user_settings 并刷新按钮显示。"""
        try:
            _us.update(**{_us.REPLACE_PROBE_ENABLED_KEY: bool(checked)})
        except OSError:
            pass   # 持久化失败不阻断（下次启动回退默认开启）
        self._update_replace_mode_btn()
        self.txt_log.append(
            f"🔄 替换模式已{'开启' if checked else '关闭'}："
            + ("材料查价完成后将排队串行探查更低磨损档的 Buff 最低价，"
               "更便宜的在材料表备注列标红提示。" if checked else
               "不再额外查询更低档价格（速度最快）。"))

    def _on_sync_eco_custom(self):
        ok, mats, msg = self._current_10_materials_for_eco()
        if not ok:
            QMessageBox.warning(self, "提示", msg)
            return
        from gui.taihuan_worker import EcoCustomApiWorker
        payload = [
            {
                "HashName": m["HashName"],
                "SPName": m["name_cn"],
                "MinFloat": m["wear_min"],
                "MaxFloat": m["wear_max"],
                "Remark": m["remark"],
            } for m in mats
        ]
        self._eco_custom_worker = EcoCustomApiWorker(
            action="sync_batch_add", custom_items=payload)
        self.btn_sync_custom.setEnabled(False)
        self.btn_sync_custom.setText("📋 同步中…")
        self._eco_custom_worker.log_message.connect(self.txt_log.append)
        self._eco_custom_worker.finished_ok.connect(self._on_sync_custom_ok)
        self._eco_custom_worker.error.connect(self._on_eco_custom_error)
        self._eco_custom_worker.start()

    def _on_sync_custom_ok(self, result):
        self.btn_sync_custom.setEnabled(True)
        self.btn_sync_custom.setText("📋 同步到 ECO 自定义饰品")
        count = len(result.get("items", [])) if isinstance(result, dict) else 0
        self.txt_log.append(
            f"✓ 已同步 {count or len(self._selected_materials)} 件材料到 ECO 自定义饰品。"
            f"（MaterialSource=3 的 StartSimulation 可直接引用）")

    def _on_clear_eco_custom(self):
        if QMessageBox.question(
            self, "确认",
            "将先拉取 ECO 自定义饰品列表，再批量删除全部自定义项。\n"
            "是否继续？"
        ) != QMessageBox.StandardButton.Yes:
            return
        from gui.taihuan_worker import EcoCustomApiWorker
        self._eco_custom_clear_worker = EcoCustomApiWorker(
            action="list_then_delete_all")
        self.btn_clear_custom.setEnabled(False)
        self.btn_clear_custom.setText("🗑 清空中…")
        self._eco_custom_clear_worker.log_message.connect(self.txt_log.append)
        self._eco_custom_clear_worker.finished_ok.connect(
            self._on_clear_custom_ok)
        self._eco_custom_clear_worker.error.connect(self._on_eco_custom_error)
        self._eco_custom_clear_worker.start()

    def _on_clear_custom_ok(self, result):
        self.btn_clear_custom.setEnabled(True)
        self.btn_clear_custom.setText("🗑 清空 ECO 自定义饰品")
        deleted = (result.get("deleted_count") if isinstance(result, dict)
                   else None)
        if deleted is None:
            deleted = "（请查看上方日志）"
        self.txt_log.append(f"✓ ECO 自定义饰品清空完成，删除 {deleted} 项。")

    def _on_eco_custom_error(self, msg):
        # 兜底恢复所有 ECO 相关按钮状态（失败时）
        for btn, txt in (
            (getattr(self, "btn_sync_custom", None), "📋 同步到 ECO 自定义饰品"),
            (getattr(self, "btn_clear_custom", None), "🗑 清空 ECO 自定义饰品"),
            (getattr(self, "btn_official_sim", None), "🧪 ECO 官方汰换模拟"),
        ):
            if btn is not None:
                btn.setEnabled(True)
                btn.setText(txt)
        self.txt_log.append(f"✗ ECO 操作失败：{msg}")
        QMessageBox.critical(self, "ECO 操作失败", msg)

    def _on_official_simulation(self):
        """把当前 10 件材料发给 ECO StartSimulation，结果写入中间固定展示区。"""
        ok, mats, msg = self._current_10_materials_for_eco()
        if not ok:
            QMessageBox.warning(self, "提示", msg)
            return
        # gui_mats = mats = 完整 GUI 10 件材料字典（含 wear_grade/buff_goods_id/
        #                wear_min/wear_max/c5_market_hash_name/collection/quality）
        #            ↓ 直接传给 EcoSimulationWorker.gui_materials，
        #               让 simulate_outputs_from_eco_result 做纯后处理时**无需再反查主 CSV**，
        #               且不会再发第二次 StartSimulation（彻底避免 ResultCode=1 重复请求 → 降级）。
        gui_mats = list(mats)
        # 构建 HTTP 请求体精简版 api_materials（仅用于唯一一次 StartSimulation）
        api_materials = []
        for i, m in enumerate(gui_mats):
            # WearValue 用区间最大值：ECO 按最高磨损（最差品质）参与模拟，
            # 产出概率按最保守情况估算
            wear_max_float = float(m["wear_max"])
            api_materials.append({
                "HashName": m["HashName"],
                "MaterialSource": 3,  # 3=自定义（同步后可走自定义库）
                "WearValue": f"{wear_max_float:.16f}",
                "Sort": i,
                # 皮肤实际浮点区间：eco_web_api 会把 WearValue 夹进
                # (皮肤区间)∩(档位区间) 开区间，规避 2001「材料磨损值错误」
                "SkinMinFloat": m.get("skin_wear_min"),
                "SkinMaxFloat": m.get("skin_wear_max"),
            })
        # 先切到"官方模拟结果"Tab
        if getattr(self, "eco_tabs", None) is not None:
            self.eco_tabs.setCurrentIndex(0)
        from gui.taihuan_worker import EcoSimulationWorker
        # NEW：关键改动——同时传两份：api_materials 给唯一一次 HTTP 用；
        # gui_mats（完整元数据）给后处理 simulate_outputs_from_eco_result 用。
        self._official_sim_worker = EcoSimulationWorker(
            api_materials, gui_materials=gui_mats,
            probe_lower_tiers=self.btn_replace_mode.isChecked())
        self.btn_official_sim.setEnabled(False)
        self.btn_official_sim.setText("🧪 模拟中…")
        self._set_metric("source", "查询中…")
        self._official_sim_worker.log_message.connect(self.txt_log.append)
        self._official_sim_worker.finished_ok.connect(
            self._on_official_sim_ok)
        self._official_sim_worker.error.connect(self._on_eco_custom_error)
        self._official_sim_worker.start()

    def _apply_sim_result_to_ui(self, result: dict):
        """把 simulate_outputs 返回的结果 dict 写到固定展示区 + 日志摘要。"""
        # --- 指标卡 ---
        cost      = float(result.get("cost") or 0.0)
        ref_value = float(result.get("ref_value") or 0.0)
        profit    = float(result.get("profit") or 0.0)
        keep_rate = float(result.get("keep_rate") or 0.0)
        source    = (str(result.get("source") or "local")).strip()
        self._set_metric("cost",      f"￥{cost:.2f}")
        self._set_metric("ref_value", f"￥{ref_value:.2f}")
        profit_color = "#488b48" if profit >= 0 else "#b7625f"
        self._eco_metric_labels.get("profit").setStyleSheet(
            f"font-size:14px;font-weight:bold;color:{profit_color};")
        self._set_metric("profit",    f"￥{profit:+.2f}")
        self._set_metric("keep_rate", f"{keep_rate:.2f}%")
        source_map = {
            "eco_official": "ECO 官方 API（权威）",
            "local":        "本地估算（降级兜底）",
        }
        self._set_metric("source", source_map.get(source, source or "未知"))

        # --- 降级原因展示（source=local 且 result 带 fallback_reason 时触发）
        fallback_reason = (str(result.get("fallback_reason") or "")).strip()
        if source == "local" and fallback_reason:
            # 1) 指标卡"来源"下面追加一个醒目的红色副标题（若有 lbl_source_sub 控件）
            lbl_sub = self._eco_metric_sub_labels.get("source") \
                if hasattr(self, "_eco_metric_sub_labels") else None
            if lbl_sub is not None:
                lbl_sub.setText("⚠ " + fallback_reason[:120])
                lbl_sub.setStyleSheet("color:#b7625f;font-size:11px;")
                lbl_sub.setWordWrap(True)
                lbl_sub.setVisible(True)
            # 2) 给"数据来源"指标卡 tooltip，展示完整原因
            src_lbl_val = self._eco_metric_labels.get("source_value") \
                if hasattr(self, "_eco_metric_labels") else None
            if src_lbl_val:
                src_lbl_val.setToolTip(fallback_reason)
            # 3) 同时把完整 fallback_reason 打进系统日志（用户一定看得到）
            self.txt_log.append(
                "⚠ 已降级为本地估算，原因：" + fallback_reason)

        # --- 产出明细 ---
        outputs = result.get("output_details") or []
        if not isinstance(outputs, list) or len(outputs) == 0:
            # 兜底：把 groups 里的 outputs 展平；这种情况通常分平台价缺失，但仍要展示
            for g in (result.get("groups") or []):
                for o in (g.get("outputs") or []):
                    outputs.append(o)
        self._populate_output_table(self.tbl_eco_outputs, outputs)

        # --- 材料成本明细（对齐槽位顺序） ---
        material_details = (result.get("material_details")
                            if isinstance(result.get("material_details"), list) else [])
        if material_details:
            # _selected_materials 顺序与槽位一致 → 表格就能显示槽位 1..10
            selected = list(self._selected_materials)
            # 如果 simulate_outputs 内部会调 _normalize_materials 可能顺序变化，
            # 但这里我们走的是 api_materials -> _normalize_materials(list(zip 顺序不变)
            # 如果长度对不上就不传 selected_materials，避免错位
            align_flag = (selected
                          if len(selected) == len(material_details)
                          else None)
            self._populate_material_table(
                self.tbl_eco_materials, material_details, align_flag)
        else:
            # 兜底：用 material_price_list 生成伪 detail
            prices = (result.get("material_price_list")
                      if isinstance(result.get("material_price_list"), list)
                      else [])
            fallback = []
            for i, p in enumerate(prices):
                try:
                    pv = float(p)
                except (TypeError, ValueError):
                    pv = 0.0
                fallback.append({
                    "index": i, "min_price": pv, "best_platform": "汇总",
                    "skin_name": "",
                })
            if fallback:
                self._populate_material_table(
                    self.tbl_eco_materials, fallback,
                    (list(self._selected_materials)
                     if len(self._selected_materials) == len(fallback) else None))

        total_prob = sum(
            float(o.get("prob") or 0.0) for o in outputs
            if isinstance(o, dict))
        self.txt_log.append(
            f"✓ ECO 模拟完成（来源={source_map.get(source, source)}）："
            f"{len(outputs)} 个可能产出，Σprob≈{total_prob:.4f}；"
            f"成本=￥{cost:.2f}；期望=￥{ref_value:.2f}；"
            f"利润=￥{profit:+.2f}；保本率={keep_rate:.2f}%。"
            "完整明细请查看中间「ECO 汰换结果」区。")

    def _on_official_sim_ok(self, result: dict):
        self.btn_official_sim.setEnabled(True)
        self.btn_official_sim.setText("🧪 ECO 官方汰换模拟")
        self._apply_sim_result_to_ui(result)

        # 询问是否保存为 ECO 我的配方
        sim_result_raw = result.get("sim_result_raw") or {}
        if sim_result_raw and QMessageBox.question(
            self, "保存配方",
            "已得到 ECO 官方模拟结果。是否同步保存为 ECO 平台「我的配方」？"
        ) == QMessageBox.StandardButton.Yes:
            name, ok = self._prompt_save_formula_name()
            if not ok:
                return
            from gui.taihuan_worker import EcoSimulationWorker
            self._save_formula_worker = EcoSimulationWorker(
                [],
                save_formula_payload=sim_result_raw,
                save_formula_name=name,
            )
            self.btn_official_sim.setEnabled(False)
            self.btn_official_sim.setText("💾 保存配方中…")
            self._save_formula_worker.log_message.connect(self.txt_log.append)
            self._save_formula_worker.finished_ok.connect(
                self._on_save_formula_ok)
            self._save_formula_worker.error.connect(self._on_eco_custom_error)
            self._save_formula_worker.start()

    def _prompt_save_formula_name(self):
        """弹出简单输入框，给出默认配方名。返回 (name, ok_clicked)。"""
        from PySide6.QtWidgets import QInputDialog
        import datetime as _dt
        default = "AutoSave-" + _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        name, ok = QInputDialog.getText(
            self, "保存配方", "请输入配方名称：", text=default)
        name = (name or "").strip()
        if not ok or not name:
            return "", False
        return name, True

    def _on_save_formula_ok(self, result: dict):
        self.btn_official_sim.setEnabled(True)
        self.btn_official_sim.setText("🧪 ECO 官方汰换模拟")
        formula_id = (result.get("FormulaID")
                      if isinstance(result, dict) else None)
        if formula_id is None and isinstance(result, dict):
            # 可能字段叫 Id / formula_id
            for k in ("Id", "id", "formula_id", "FormulaId"):
                if k in result and result[k]:
                    formula_id = result[k]
                    break
        fid_txt = str(formula_id).strip() if formula_id is not None else ""
        # 写到两个地方：指标卡 + 真实对比 Tab 的输入框（选中）
        self._set_metric("formula_id", fid_txt or "（无）")
        if fid_txt and getattr(self, "edit_formula_id", None) is not None:
            self.edit_formula_id.setText(fid_txt)
            if getattr(self, "eco_tabs", None) is not None:
                self.eco_tabs.setCurrentIndex(1)  # 切到「真实结果对比」Tab
        self.txt_log.append(
            f"💾 已保存为 ECO 我的配方，FormulaID={fid_txt!r}。"
            f"（已自动填入中间 Tab B，可直接点击「真实汰换结果对比」）")
        QMessageBox.information(
            self, "保存成功",
            f"配方已保存至 ECO 我的配方。\nFormulaID = {fid_txt!r}\n"
            f"已自动填入中间「真实结果对比」Tab，可立即执行对比。")

    # ============================================================
    # 真实结果对比（RealReplaceCompareWorker）
    # ============================================================
    def _on_real_compare_clicked(self):
        fid = (self.edit_formula_id.text() if hasattr(self, "edit_formula_id") else "").strip()
        if not fid:
            QMessageBox.warning(
                self, "提示",
                "请先填写 FormulaID。\n"
                "可先在「官方模拟结果」Tab 保存配方，返回 FormulaID。")
            return
        from gui.taihuan_worker import RealReplaceCompareWorker
        self.btn_real_compare.setEnabled(False)
        self.btn_real_compare.setText("📊 对比中…")
        self.lbl_real_status.setText(
            f"正在读取 FormulaID={fid} 的 FormulaDetail + RealReplaceResult，"
            f"并并发查三平台价格…")
        self._real_compare_worker = RealReplaceCompareWorker(fid)
        self._real_compare_worker.log_message.connect(self.txt_log.append)
        self._real_compare_worker.finished_ok.connect(
            self._on_real_compare_ok)
        self._real_compare_worker.error.connect(self._on_real_compare_error)
        self._real_compare_worker.start()

    def _on_real_compare_error(self, msg: str):
        self.btn_real_compare.setEnabled(True)
        self.btn_real_compare.setText("📊 真实汰换结果对比")
        self.lbl_real_status.setText(f"✗ 失败：{msg}")
        self.lbl_real_status.setStyleSheet("color:#b7625f;font-size:10px;")
        self.txt_log.append(f"✗ 真实结果对比失败：{msg}")
        QMessageBox.critical(self, "真实结果对比失败", msg)

    def _on_real_compare_ok(self, result: dict):
        self.btn_real_compare.setEnabled(True)
        self.btn_real_compare.setText("📊 真实汰换结果对比")
        # --- 指标卡（切到真实视角：material_cost / real_expected_value / profit / keep_rate） ---
        mat_cost   = float(result.get("material_cost") or 0.0)
        real_ev    = float(result.get("real_expected_value") or 0.0)
        profit     = float(result.get("profit") or 0.0)
        keep_rate  = float(result.get("keep_rate") or 0.0)
        fid        = str(result.get("formula_id") or self.edit_formula_id.text()).strip()
        self._set_metric("formula_id", fid or "—")
        self._set_metric("cost",      f"￥{mat_cost:.2f}")
        self._set_metric("ref_value", f"￥{real_ev:.2f}")
        color = "#488b48" if profit >= 0 else "#b7625f"
        self._eco_metric_labels.get("profit").setStyleSheet(
            f"font-size:14px;font-weight:bold;color:{color};")
        self._set_metric("profit", f"￥{profit:+.2f}")
        self._set_metric("keep_rate", f"{keep_rate*100:.2f}%"
                         if keep_rate <= 1 else f"{keep_rate:.4f}")
        # 真实对比场景把 source 标出来，避免和模拟混淆
        self._set_metric("source", "Real Replace 真实")

        # --- 真实产出明细（real_rows 里应带 min_price / buff_price / c5_price / eco_price） ---
        real_rows = result.get("real_rows") or []
        contribs = [float(r.get("weighted_value") or 0.0) for r in real_rows] \
            if isinstance(real_rows, list) else None
        self._populate_output_table(
            self.tbl_real_outputs,
            real_rows if isinstance(real_rows, list) else [],
            prob_col="weight",
            name_col="name",
            hash_col="hash_name",
            wear_col="wear_float",
            extra_contrib=contribs,
        )

        # --- 配方材料成本明细 ---
        mat_details = result.get("material_details") or []
        mats_from_detail = result.get("materials") or []
        if isinstance(mat_details, list) and len(mat_details):
            aligned = (mats_from_detail
                       if isinstance(mats_from_detail, list)
                       and len(mats_from_detail) == len(mat_details)
                       else None)
            self._populate_material_table(
                self.tbl_real_materials, mat_details, aligned)

        status = (
            f"✓ 真实对比完成：共 {len(real_rows)} 条真实产出记录；"
            f"材料成本=￥{mat_cost:.2f}；真实期望=￥{real_ev:.2f}；"
            f"利润=￥{profit:+.2f}；保本率={float(keep_rate):.2f}%。"
        )
        self.lbl_real_status.setText(status)
        self.lbl_real_status.setStyleSheet("color:#488b48;font-size:10px;")
        self.txt_log.append(status)

    # ============================================================
    # BUFF 查询节流：状态文本生成 + 保存回调 + 回退回调
    # ============================================================

    def _buff_throttle_status_text(self, actual_interval: float,
                                   source: str = "saved") -> str:
        """生成状态框显示文本。

        source: "saved" | "loaded" | "cleared"
        """
        from utils.config import BUFF_REQUEST_INTERVAL as _CFG_DEF
        override = _us.get(_us.BUFF_REQ_INTERVAL_KEY)
        has_override = not (override is None or override == ""
                           or (isinstance(override, float) and False))
        try:
            _ = float(override)
            has_override = True
        except (TypeError, ValueError):
            has_override = False
        if source == "loaded":
            prefix = "📂 启动时加载："
        elif source == "cleared":
            prefix = "🧹 已清除 GUI 覆盖，"
        else:
            prefix = "✅ 已保存，"
        if has_override:
            src = (f"读取自 GUI 覆盖（user_settings.json "
                   f"buff_request_interval_s={override!r}），"
                   f"优先级 > config.py BUFF_REQUEST_INTERVAL={_CFG_DEF}s")
        else:
            src = f"读取自 config.py 默认值 BUFF_REQUEST_INTERVAL={_CFG_DEF}s。"
        return (f"{prefix}当前每条 BUFF 请求前 sleep "
                f"<b>{actual_interval:.1f} 秒</b>。  {src}")

    def _on_save_buff_throttle(self):
        """💾 保存（立即生效）：把 spinbox 的值写到 user_settings → 刷新状态 + 日志。"""
        seconds = float(self.spn_buff_throttle.value())
        try:
            _actual = _pfetcher.set_runtime_buff_req_interval(seconds)
        except OSError as e:
            QMessageBox.critical(
                self, "保存 BUFF 节流失败",
                f"写入 data/user_settings.json 失败：\n{e}")
            return
        status = self._buff_throttle_status_text(_actual, source="saved")
        # 写状态：绿色加粗
        self.lbl_buff_throttle_status.setStyleSheet(
            "font-size:10px;color:#2E7D32;font-weight:bold;")
        self.lbl_buff_throttle_status.setText(status)
        self.txt_log.append(
            f"[BUFF 节流] 💾 已保存 GUI 覆盖值：每请求前 sleep {_actual:.1f}s。"
            " 下次「开始寻找材料」/「ECO 官方模拟」材料查价立即生效。")

    def _on_clear_buff_throttle(self):
        """🧹 清除 GUI 覆盖：回退到 config.py 的 BUFF_REQUEST_INTERVAL 默认值。"""
        reply = QMessageBox.question(
            self, "确认清除 BUFF 查询节流 GUI 覆盖？",
            "清除后，将重新使用 utils/config.py 里的 BUFF_REQUEST_INTERVAL 值作为默认，"
            "不再使用本次 spinbox 的设置。\n\n（不会修改 config.py 源码本身）确认继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        try:
            _actual = _pfetcher.set_runtime_buff_req_interval(None)
        except OSError as e:
            QMessageBox.critical(
                self, "清除 BUFF 节流失败",
                f"更新 data/user_settings.json 失败：\n{e}")
            return
        # Spinbox 同步显示实际回退后的值（config 默认）
        self.spn_buff_throttle.setValue(_actual)
        status = self._buff_throttle_status_text(_actual, source="cleared")
        self.lbl_buff_throttle_status.setStyleSheet(
            "font-size:10px;color:#555;")
        self.lbl_buff_throttle_status.setText(status)
        self.txt_log.append(
            f"[BUFF 节流] 🧹 已清除 GUI 覆盖，回退到 config.py 默认值 "
            f"（每请求前 sleep {_actual:.1f}s）。")

    def _on_clear_selected(self):
        self._selected_materials.clear()
        self._sync_slots()
        self.tbl_prices.setRowCount(0)
        self.lbl_find_status.setText(
            "未查询。点击「开始寻找材料」查询各材料在固定磨损范围内的最低价。")
        self.txt_log.append("🗑 已清空所选材料")

    def _sync_slots(self):
        for i, slot in enumerate(self.slots):
            if i < len(self._selected_materials):
                slot.set_item(self._selected_materials[i])
            else:
                slot.clear_item()
        self.lbl_mat_count.setText(
            f"材料数量： {len(self._selected_materials)} / 10")
        total = sum(
            float(m.get("price_buff") or 0) for m in self._selected_materials)
        self.lbl_ref_total.setText(f"参考总价：￥{total:.2f}")

    # ============================================================
    # 开始寻找材料（各材料固定磨损范围内最低价）
    # ============================================================
    def _on_find_materials(self):
        if len(self._selected_materials) < 10:
            QMessageBox.warning(
                self, "提示",
                f"您还需要从左侧选择 {10 - len(self._selected_materials)} "
                f"件材料。")
            return
        mats = [
            recipe_matcher.material_query_config(m)
            for m in self._selected_materials
        ]
        self.btn_find.setEnabled(False)
        self.btn_find.setText("🔍 查询中…")
        self.tbl_prices.setRowCount(0)
        self.lbl_find_status.setText("正在查询各材料最低价…")

        self._find_worker = MaterialFindWorker(mats)
        self._find_worker.item_start.connect(self._on_price_item_start)
        self._find_worker.item_done.connect(self._on_price_item_done)
        self._find_worker.log_message.connect(self.txt_log.append)
        self._find_worker.finished_ok.connect(self._on_find_done)
        self._find_worker.error.connect(self._on_find_error)
        self._find_worker.start()

    def _on_price_item_start(self, idx, total, name):
        self.lbl_find_status.setText(f"正在查询 [{idx}/{total}] {name} …")
        self.txt_log.append(
            f"🔍 [{idx}/{total}] 查询 {name} 固定磨损范围内最低价")

    def _on_price_item_done(self, idx, result):
        row = idx - 1
        self.tbl_prices.setRowCount(max(self.tbl_prices.rowCount(), idx))
        name = result.get("name", "")
        wmin = result.get("wear_min") or 0
        wmax = result.get("wear_max") or 1
        item0 = QTableWidgetItem(str(idx))
        item1 = QTableWidgetItem(name)
        item1.setToolTip(name)
        item2 = QTableWidgetItem(f"{wmin:.4f} ~ {wmax:.4f}")
        if result.get("min_price") is not None:
            item3 = QTableWidgetItem(f"￥{result['min_price']:.2f}")
            item3.setForeground(Qt.GlobalColor.red)
            item4 = QTableWidgetItem(result.get("platform", ""))
        else:
            item3 = QTableWidgetItem("—")
            item4 = QTableWidgetItem("无在售")
        for c, it in ((0, item0), (1, item1), (2, item2),
                      (3, item3), (4, item4)):
            if c in (0, 2, 3, 4):
                it.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.tbl_prices.setItem(row, c, it)

    def _on_find_done(self, results):
        self.btn_find.setEnabled(True)
        self.btn_find.setText("🔍 开始寻找材料")
        # 保留完整查询结果（含 items 在售列表）供「一键购买底价」使用
        self._find_results = results
        priced = [r for r in results if r.get("min_price") is not None]
        if priced:
            total = sum(r["min_price"] for r in priced)
            self.lbl_find_status.setText(
                f"查询完成：{len(priced)}/{len(results)} 个材料有报价，"
                f"合计最低成本 ￥{total:.2f}")
        else:
            self.lbl_find_status.setText("查询完成：未找到任何在售报价。")
        self.txt_log.append(
            f"✓ 材料查询完成：{len(priced)}/{len(results)} 个材料有报价")

    def _on_find_error(self, msg: str):
        self.btn_find.setEnabled(True)
        self.btn_find.setText("🔍 开始寻找材料")
        self.lbl_find_status.setText(f"查询失败：{msg}")
        self.txt_log.append(f"✗ {msg}")
        QMessageBox.critical(self, "错误", msg)

    # ============================================================
    # 上传最低价到价格监控（2026-09-12 阈值囤货）
    # ============================================================
    def _on_upload_min_prices(self):
        """把 10 件材料的当前最低选用价上传为价格检测的阈值。

        - 数据源：self._find_results（「开始寻找材料」查询结果）+
          self._selected_materials（槽位原始材料，含磨损档/ID 元数据）
        - 聚合：同 (皮肤名, 档位) 合并需求件数（槽位数 × 套数）
        - 目标名：'皮肤名（档位）'（不同档位可共存，Buff 查询靠
          goods_id + 磨损区间过滤，item_name 仅用于显示）
        - 幂等：同名目标重复上传 → 覆盖阈值/磨损/需求，不累加
        """
        if not getattr(self, "_find_results", None):
            QMessageBox.warning(
                self, "提示",
                "请先点击「🔍 开始寻找材料」查询最新价格，再上传阈值。")
            return
        mats = list(self._selected_materials)
        results = self._find_results
        if len(mats) != len(results):
            QMessageBox.warning(
                self, "提示",
                f"材料槽位({len(mats)})与查询结果({len(results)})不一致，"
                "请重新查询后再上传。")
            return

        from core import price_monitor as pm
        n_sets = self.spin_buy_sets.value()

        # 聚合 (皮肤名, 档位) -> 槽位材料 + 需求件数 + 最低价
        groups = {}
        for sm, r in zip(mats, results):
            skin = (sm.get("皮肤名称") or "").strip()
            grade = (sm.get("磨损") or "").strip()
            if not skin:
                continue
            key = (skin, grade)
            g = groups.setdefault(
                key, {"sm": sm, "need": 0, "price": None})
            g["need"] += 1
            mp = r.get("min_price")
            if mp is not None:
                if g["price"] is None or mp < g["price"]:
                    g["price"] = float(mp)

        if not groups:
            QMessageBox.warning(self, "提示", "没有可上传的材料。")
            return

        added = updated = skipped = 0
        for (skin, grade), g in groups.items():
            if g["price"] is None:
                skipped += 1
                continue
            disp_name = f"{skin}（{grade}）" if grade else skin
            # 磨损区间：优先用户自定义区间，其次官方档位区间
            try:
                wm, wx = material_wear_min_max(g["sm"])
            except Exception:
                wm = wx = None
            if wm is None or wx is None or wm <= 0 or wx <= 0:
                wm = wx = None
            need = g["need"] * n_sets
            existing = pm._find_target_by_name(disp_name)
            if existing:
                pm.update_target_fields(
                    existing["id"],
                    max_price=g["price"],
                    wear_min=wm, wear_max=wx,
                    need_count=need)      # 覆盖，不累加（幂等）
                updated += 1
            else:
                pm.add_target({
                    "item_name": disp_name,
                    "buff_goods_id": g["sm"].get("buff_goods_id", ""),
                    "c5_market_hash_name":
                        g["sm"].get("c5_market_hash_name", ""),
                    "c5_app_id": g["sm"].get("c5_app_id", "730"),
                    "wear_min": wm, "wear_max": wx,
                    "max_price": g["price"],
                    "need_count": need,
                })
                added += 1

        msg = (f"已上传到价格检测：新增 {added} 个目标，"
               f"更新 {updated} 个阈值"
               + (f"，{skipped} 个无报价已跳过" if skipped else "") + "。\n\n"
               f"到「价格检测」页开启自动购买 + ▶ 开始检测，\n"
               f"即可在价格 ≤ 阈值时分批自动囤货。")
        self.txt_log.append(
            f"📤 阈值上传完成：新增 {added} / 更新 {updated} / 跳过 {skipped}"
            f"（套数 ×{n_sets}）")
        QMessageBox.information(self, "上传完成", msg)

    # ============================================================
    # 一键购买底价材料（2026-09-11）：套数 N × 10 件
    # ============================================================
    def _on_buy_materials(self):
        """按查询结果的完整在售列表，每材料槽位跨平台取最便宜 N 件下单。"""
        if self._material_buy_worker is not None:
            QMessageBox.information(self, "提示", "已有购买任务在执行中。")
            return
        if not self._find_results:
            QMessageBox.warning(
                self, "提示",
                "请先点击「🔍 开始寻找材料」查询最新价格，再执行一键购买。")
            return

        n_sets = self.spin_buy_sets.value()
        # ---- 按材料聚合需求（2026-09-11 修复重复购买 bug）----
        # 方案里同一材料可能占多个槽位（如 Necro Jr. ×7 槽）。若按槽位各自
        # 取前 N 件，所有槽位会选中【同一批最便宜商品】→ 第 1 件买走后，
        # 其余订单全是同一商品必然失败（实测：同一 productId 反复 preview）。
        # 正确语义：同材料需求量 = 槽位数 × 套数，从该材料在售列表取前
        # 需求量件（列表内每件是不同商品，天然不重复）。
        demand = {}   # 材料名 -> [find_result, 需求件数]
        for r in self._find_results:
            items = r.get("items") or []
            if not items:
                continue
            nm = r.get("name", "")
            if nm not in demand:
                demand[nm] = [r, 0]
            demand[nm][1] += n_sets
        orders = []
        insufficient = []
        for nm, (r, need) in demand.items():
            # 价格升序，取前 need 件（跨平台混选最便宜的；磨损已在查询时过滤）
            ranked = sorted(r.get("items") or [], key=lambda x: x.get("price", 0))
            if len(ranked) < need:
                insufficient.append(
                    f"{nm}（在售 {len(ranked)} 件 < 需求 {need}）")
            for it in ranked[:need]:
                od = dict(it)
                od["name"] = nm
                orders.append(od)

        if not orders:
            QMessageBox.warning(
                self, "提示", "没有任何可购买的材料商品（查询结果里无在售条目）。")
            return
        if insufficient:
            self.txt_log.append(
                "⚠ 部分材料在售数量不足套数需求：\n  " + "\n  ".join(insufficient))

        # ---- 确认弹窗：订单明细表 + 总价 ----
        total = sum(o.get("price", 0) for o in orders)
        dlg = QDialog(self)
        dlg.setWindowTitle(f"🛒 确认一键购买（{n_sets} 套，共 {len(orders)} 件）")
        dlg.setMinimumSize(560, 420)
        dv = QVBoxLayout(dlg)
        tbl = QTableWidget(len(orders), 6)
        tbl.setHorizontalHeaderLabels(
            ["#", "材料", "平台", "价格(元)", "磨损", "商品ID"])
        tbl.verticalHeader().setVisible(False)
        tbl.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        tbl.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for row, od in enumerate(orders):
            wear = od.get("wear", 0)
            vals = [
                str(row + 1), od.get("name", ""), od.get("platform", ""),
                f"{od.get('price', 0):.2f}",
                f"{wear:.6f}" if wear else "—",
                str(od.get("order_id", "") or od.get("goods_id", "")),
            ]
            for c, v in enumerate(vals):
                it = QTableWidgetItem(v)
                if c in (0, 2, 3, 4):
                    it.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                tbl.setItem(row, c, it)
        dv.addWidget(tbl, 1)
        info = QLabel(
            f"共 {len(orders)} 件，总金额 ￥{total:.2f}（套数 {n_sets}）\n"
            f"购买将走统一入口：成功/失败均自动记录到「历史记录」页签（含价格与时间）。")
        info.setStyleSheet("color:#555;")
        info.setWordWrap(True)
        dv.addWidget(info)
        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.button(QDialogButtonBox.StandardButton.Ok).setText("确认购买")
        btns.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        dv.addWidget(btns)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            self.txt_log.append("已取消一键购买。")
            return

        # ---- 启动购买线程 ----
        from gui.taihuan_worker import MaterialBuyWorker
        self.btn_buy_materials.setEnabled(False)
        self.btn_buy_materials.setText("🛒 购买中…")
        self.txt_log.append(
            f"🛒 开始一键购买底价材料：{n_sets} 套共 {len(orders)} 件，"
            f"预计总金额 ￥{total:.2f}")
        self._material_buy_worker = MaterialBuyWorker(orders)
        self._material_buy_worker.progress.connect(
            lambda s: self.lbl_find_status.setText(s))
        self._material_buy_worker.item_done.connect(self._on_buy_item_done)
        self._material_buy_worker.finished.connect(self._on_buy_all_done)
        self._material_buy_worker.error.connect(self._on_buy_error)
        self._material_buy_worker.start()

    def _on_buy_item_done(self, idx, total, success, msg, order_id):
        mark = "✓" if success else "✗"
        extra = f"（单号 {order_id}）" if order_id else ""
        self.txt_log.append(f"{mark} [{idx}/{total}] {msg}{extra}")

    def _on_buy_all_done(self, ok, fail, summary):
        self.btn_buy_materials.setEnabled(True)
        self.btn_buy_materials.setText("🛒 一键购买底价")
        self.lbl_find_status.setText(summary)
        self.txt_log.append(
            f"✅ {summary}\n"
            f"   购买明细（价格/时间）已自动记录，可在「历史记录」页签"
            f"筛选「购买」查看。")
        self._material_buy_worker = None
        if fail:
            QMessageBox.warning(self, "购买完成（部分失败）", summary)
        else:
            QMessageBox.information(self, "购买完成", summary)

    def _on_buy_error(self, msg):
        self.btn_buy_materials.setEnabled(True)
        self.btn_buy_materials.setText("🛒 一键购买底价")
        self.lbl_find_status.setText(f"购买失败：{msg}")
        self.txt_log.append(f"✗ {msg}")
        QMessageBox.critical(self, "错误", msg)
        self._material_buy_worker = None

    # ============================================================
    # 方案管理
    # ============================================================
    def _on_save_plan(self):
        if len(self._selected_materials) < 10:
            QMessageBox.warning(self, "提示", "请先在左侧选择 10 件材料。")
            return
        plan_name = self.edit_plan_name.text().strip()
        if not plan_name:
            QMessageBox.warning(self, "提示", "请先为方案命名。")
            return
        materials = [dict(m) for m in self._selected_materials]
        try:
            plan_id = recipe_matcher.save_material_plan_to_db(
                plan_name, materials)
            self._current_plan_id = plan_id
            self.txt_log.append(f"✓ 方案已保存（id={plan_id}）: {plan_name}")
            self._reload_saved_plans()
        except Exception as e:
            QMessageBox.critical(self, "错误", f"保存方案失败：{e}")

    def _on_update_plan(self):
        if self._current_plan_id is None:
            QMessageBox.warning(self, "提示", "请先在列表中载入一个方案再更新")
            return
        if len(self._selected_materials) < 10:
            QMessageBox.warning(self, "提示", "请先在左侧选择 10 件材料。")
            return
        plan_name = self.edit_plan_name.text().strip()
        if not plan_name:
            QMessageBox.warning(self, "提示", "请先为方案命名。")
            return
        materials = [dict(m) for m in self._selected_materials]
        try:
            recipe_matcher.update_material_plan(
                self._current_plan_id, plan_name, materials)
            self.txt_log.append(
                f"✓ 方案已更新（id={self._current_plan_id}）")
            self._reload_saved_plans()
        except Exception as e:
            QMessageBox.critical(self, "错误", f"更新方案失败：{e}")

    def _reload_saved_plans(self, plans=None):
        """刷新方案列表。plans=None 时同步查库，否则直接用传入的列表。"""
        self.lst_plans.clear()
        if plans is None:
            try:
                plans = recipe_matcher.list_saved_plans()
            except Exception as e:
                logger.warning("加载方案列表失败: %s", e)
                plans = []
        try:
            for p in plans:
                if not isinstance(p, dict):
                    continue
                mdata = p.get("materials_data") or {}
                mats = mdata.get("materials", []) if isinstance(mdata, dict) else []
                mat_count = len(mats)
                display = (f"[{p.get('id','?')}] {p.get('plan_name','')} "
                           f"(材料{mat_count}件) - {p.get('status','')}")
                item = QListWidgetItem(display)
                item.setData(Qt.ItemDataRole.UserRole, p)
                self.lst_plans.addItem(item)
        except Exception as e:
            logger.warning("渲染方案列表失败: %s", e)

    def _on_plan_clicked(self, item: QListWidgetItem):
        pass

    def _on_load_plan(self):
        row = self.lst_plans.currentRow()
        if row < 0:
            QMessageBox.warning(self, "提示", "请先选中一个方案")
            return
        item = self.lst_plans.item(row)
        plan = item.data(Qt.ItemDataRole.UserRole)
        mdata = plan.get("materials_data") or {}
        materials = mdata.get("materials", []) if isinstance(mdata, dict) else []

        self._current_plan_id = plan.get("id")
        self.edit_plan_name.setText(plan.get("plan_name") or "")

        if not materials:
            QMessageBox.warning(self, "提示", "该方案没有材料数据。")
            return
        self._selected_materials = [dict(m) for m in materials]
        self._sync_slots()
        self.tbl_prices.setRowCount(0)
        self.lbl_find_status.setText(
            "未查询。点击「开始寻找材料」查询各材料在固定磨损范围内的最低价。")
        self.txt_log.append(
            f"📂 已载入方案: {plan.get('plan_name')} "
            f"（材料 {len(self._selected_materials)} 件）")

    def _on_delete_plan(self):
        row = self.lst_plans.currentRow()
        if row < 0:
            QMessageBox.warning(self, "提示", "请先选中一个方案")
            return
        item = self.lst_plans.item(row)
        plan = item.data(Qt.ItemDataRole.UserRole)
        plan_id = plan.get("id")
        if QMessageBox.question(
            self, "确认", f"确定删除方案 '{plan.get('plan_name')}'？"
        ) != QMessageBox.StandardButton.Yes:
            return
        recipe_matcher.delete_plan(plan_id)
        if self._current_plan_id == plan_id:
            self._current_plan_id = None
        self._reload_saved_plans()
        self.txt_log.append(f"🗑 已删除方案 id={plan_id}")

    # ============================================================
    # 执行游戏点击（原有逻辑保留）
    # ============================================================
    def _on_execute_click(self):
        if not self._selected_materials:
            QMessageBox.warning(self, "提示", "请先在右侧放入材料")
            return
        from utils.config import get_game_interact_config
        cfg = get_game_interact_config()
        cfg.BATCH_SIZE = self.spn_batch.value()
        cfg.SLIDER_ADJUST_PIXEL = self.spn_slider_px.value()

        item_names = [m.get("市场哈希名称") or m.get("皮肤名称") or ""
                      for m in self._selected_materials]
        from core import inventory_fetcher
        csv_path = inventory_fetcher.INVENTORY_CSV_PATH

        self.btn_execute.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.progress.setValue(0)
        self.txt_log.append(f"▶ 开始点击 {len(item_names)} 件材料")

        self._click_worker = GameClickWorker(
            item_names=item_names,
            csv_path=csv_path,
            inter_item_delay=self.spn_delay.value(),
        )
        self._click_worker.item_start.connect(
            lambda n, i, t: self.txt_log.append(f"  [{i}/{t}] {n}"))
        self._click_worker.item_done.connect(
            lambda r: self.txt_log.append(
                f"    {'✓' if r.success else '✗'} {r.message}"))
        self._click_worker.scroll_progress.connect(
            lambda e, t: self.progress.setValue(int(e / max(t, 1) * 100)))
        self._click_worker.log_message.connect(self.txt_log.append)
        self._click_worker.finished_ok.connect(self._on_click_done)
        self._click_worker.error.connect(self._on_click_error)
        self._click_worker.start()

    def _on_click_done(self, results: list):
        self.btn_execute.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.progress.setValue(100)
        ok = sum(1 for r in results if r.success)
        self.txt_log.append(f"✓ 点击完成：成功 {ok}/{len(results)}")
        try:
            for r in results:
                recipe_matcher.log_taihuan_action(
                    plan_id=self._current_plan_id or 0,
                    action="click",
                    item_name=r.item_name,
                    status="success" if r.success else "fail",
                    remark=r.message,
                )
        except Exception:
            pass

    def _on_click_error(self, msg: str):
        self.btn_execute.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.txt_log.append(f"✗ {msg}")
        QMessageBox.critical(self, "错误", msg)

    def _on_stop_click(self):
        if self._click_worker and self._click_worker.isRunning():
            self._click_worker.stop()
            self.txt_log.append("⏹ 已请求停止")

    def stop_if_running(self):
        workers = [
            getattr(self, "_click_worker", None),
            getattr(self, "_find_worker", None),
            getattr(self, "_assets_worker", None),
            getattr(self, "_eco_custom_worker", None),
            getattr(self, "_eco_custom_clear_worker", None),
            getattr(self, "_official_sim_worker", None),
            getattr(self, "_save_formula_worker", None),
        ]
        for w in workers:
            if w and getattr(w, "isRunning", lambda: False)():
                try:
                    w.terminate()
                except Exception:
                    pass
                try:
                    w.wait(2000)
                except Exception:
                    pass
