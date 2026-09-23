"""自动汰换编排后台线程。

流程：
1. (可选) 汰换前刷新库存（调用 ECO API）
2. 搜索可执行汰换方案（库存 + 主 CSV）
3. 用户在 GUI 选择方案后，执行游戏内点击：
   对每件材料物品，定位+右键点击
4. (可选) 汰换后刷新库存（验证产出）

信号：
- progress(str, int, int): 阶段名 + 当前 + 总数
- log_message(str): 日志消息
- finished(list): ClickResult 列表
- error(str): 错误信息
"""
import logging
import math
import random
import time
from typing import Optional

from PySide6.QtCore import QThread, Signal

from core import inventory_fetcher, game_interact, recipe_matcher
from core import price_fetcher

logger = logging.getLogger(__name__)


class InventoryRefreshWorker(QThread):
    """后台刷新库存线程。"""
    progress = Signal(int, int)   # current_page, total_records
    log_message = Signal(str)
    finished_ok = Signal(int)     # item count
    error = Signal(str)

    def __init__(self, steam_id: Optional[str] = None, parent=None):
        super().__init__(parent)
        self.steam_id = steam_id

    def run(self):
        try:
            self.log_message.emit("开始爬取 ECO 网页库存...")
            count = inventory_fetcher.refresh_inventory(
                on_progress=lambda p, t: self.progress.emit(p, t),
                steam_id=self.steam_id,
            )
            self.log_message.emit(f"库存刷新完成，共 {count} 件")
            self.finished_ok.emit(count)
        except Exception as e:
            logger.exception("库存刷新失败")
            self.error.emit(f"库存刷新失败：{e}")


class PlanSearchWorker(QThread):
    """后台搜索汰换方案线程。"""
    log_message = Signal(str)
    finished_ok = Signal(list)   # list[TaihuanPlan]
    error = Signal(str)

    def __init__(self, target_collection: str = None,
                 target_quality: str = None,
                 include_stattrak: bool = False,
                 top_n: int = 10,
                 parent=None):
        super().__init__(parent)
        self.target_collection = target_collection
        self.target_quality = target_quality
        self.include_stattrak = include_stattrak
        self.top_n = top_n

    def run(self):
        try:
            self.log_message.emit("正在搜索可执行汰换方案...")
            plans = recipe_matcher.find_plans(
                target_collection=self.target_collection,
                target_quality=self.target_quality,
                include_stattrak=self.include_stattrak,
                top_n=self.top_n,
            )
            self.log_message.emit(f"找到 {len(plans)} 个方案")
            self.finished_ok.emit(plans)
        except Exception as e:
            logger.exception("方案搜索失败")
            self.error.emit(f"方案搜索失败：{e}")


class MaterialFindWorker(QThread):
    """后台查询各材料在固定磨损范围内最低价线程。

    对已选 10 件材料（主 CSV 材料行，含磨损范围）逐个调用
    price_fetcher.query_all（Buff/C5/ECO 并行），取该材料
    固定磨损范围内的最低在售价。

    信号：
    - item_start(int, int, str): 第几个 + 总数 + 材料名
    - item_done(int, dict): 第几个 + 结果 dict
    - finished_ok(list): 全部结果
    - error(str): 致命错误
    """
    log_message = Signal(str)
    item_start = Signal(int, int, str)
    item_done = Signal(int, object)     # idx(1based), result dict
    finished_ok = Signal(list)
    error = Signal(str)

    def __init__(self, materials: list, platforms=None, parent=None):
        """materials: recipe_matcher.material_query_config() 的输出列表。"""
        super().__init__(parent)
        self.materials = materials
        self.platforms = platforms

    def run(self):
        try:
            results = []
            total = len(self.materials)
            for i, mat in enumerate(self.materials, 1):
                name = mat.get("item_name") or mat.get("皮肤名称") or "未知"
                self.item_start.emit(i, total, name)
                self.log_message.emit(
                    f"[{i}/{total}] 查询 {name} "
                    f"磨损 {mat.get('wear_min') or 0:.4f}"
                    f"~{mat.get('wear_max') or 1:.4f} 最低价 ...")
                try:
                    rows, errors = price_fetcher.query_all(
                        {
                            "buff_goods_id": mat.get("buff_goods_id", ""),
                            "c5_market_hash_name": mat.get(
                                "c5_market_hash_name", ""),
                            "c5_app_id": mat.get("c5_app_id", "730"),
                        },
                        wear_min=mat.get("wear_min") or None,
                        wear_max=mat.get("wear_max") or None,
                        platforms=self.platforms,
                    )
                except Exception as e:
                    self.log_message.emit(f"    {name} 查询失败: {e}")
                    result = {
                        "name": name,
                        "wear_grade": mat.get("wear_grade", ""),
                        "wear_min": mat.get("wear_min"),
                        "wear_max": mat.get("wear_max"),
                        "min_price": None,
                        "platform": "",
                        "count": 0,
                        "error": str(e),
                        "items": [],
                    }
                    self.item_done.emit(i, result)
                    results.append(result)
                    continue

                best = min(rows, key=lambda r: r["price"]) if rows else None
                result = {
                    "name": name,
                    "wear_grade": mat.get("wear_grade", ""),
                    "wear_min": mat.get("wear_min"),
                    "wear_max": mat.get("wear_max"),
                    "min_price": best["price"] if best else None,
                    "platform": best["platform"] if best else "",
                    "count": len(rows),
                    "error": "",
                    # 完整在售商品列表（platform/order_id/goods_id/price/wear），
                    # 一键购买底价材料时按价格排序取前 N 件用
                    "items": rows,
                }
                if best:
                    self.log_message.emit(
                        f"    ✓ {name} 最低 ￥{best['price']:.2f}"
                        f"（{best['platform']}）")
                else:
                    self.log_message.emit(f"    {name} 未找到在售商品")
                self.item_done.emit(i, result)
                results.append(result)
            self.finished_ok.emit(results)
        except Exception as e:
            logger.exception("材料价格查询失败")
            self.error.emit(f"材料价格查询失败：{e}")


class MaterialBuyWorker(QThread):
    """一键购买底价材料线程（2026-09-11）。

    对「开始寻找材料」查询结果（每材料完整在售列表 items）按价格升序
    取前 N 件（N=套数，可跨平台混选最便宜），逐件走 buyer.buy_item
    统一购买入口（自动落 operations 表：时间/价格/平台/磨损/状态）。

    信号：
    - progress(str): 进度文本
    - item_done(int, int, bool, str, str): (第几件, 总数, 成功, 消息, 单号)
    - finished(int, int, str): (成功数, 失败数, 摘要)
    - error(str): 致命错误
    """
    progress = Signal(str)
    item_done = Signal(int, int, bool, str, str)
    finished = Signal(int, int, str)
    error = Signal(str)

    # 每单之间的额外间隔（秒）：购买是敏感写操作，防连击
    ORDER_GAP_BASE = 1.0
    ORDER_GAP_JITTER = 0.5

    def __init__(self, orders, parent=None):
        """orders: list[dict]，每项含 name(材料名) + buy_item 兼容字段
        （platform/order_id/goods_id/price/wear）。"""
        super().__init__(parent)
        self.orders = orders
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self):
        try:
            from core.buyer import buy_item  # 延迟 import，避免循环依赖
            total = len(self.orders)
            ok = fail = 0
            for i, od in enumerate(self.orders, 1):
                if self._stop:
                    break
                name = od.get("name") or od.get("item_name") or "未知材料"
                self.progress.emit(
                    f"[{i}/{total}] 购买 {name} "
                    f"（{od.get('platform')} ¥{od.get('price', 0):.2f}）...")
                try:
                    success, msg, order_id = buy_item(od, name)
                except Exception as e:
                    success, msg, order_id = False, f"购买异常: {e}", ""
                if success:
                    ok += 1
                else:
                    fail += 1
                self.item_done.emit(i, total, bool(success), str(msg), str(order_id or ""))
                # 单间间隔：1.0 + 0~0.5s 随机
                if i < total and not self._stop:
                    time.sleep(self.ORDER_GAP_BASE + random.uniform(0.0, self.ORDER_GAP_JITTER))
            summary = f"一键购买完成：成功 {ok} 件 / 失败 {fail} 件 / 共 {total} 件"
            self.finished.emit(ok, fail, summary)
        except Exception as e:
            logger.exception("一键购买材料失败")
            self.error.emit(f"一键购买材料失败：{e}")


# ============================================================
# 方案执行 Worker（新「汰换」页用）：匹配 → 待定确认 → 点击 → 汰换后刷新
# ============================================================
class PlanMatchWorker(QThread):
    """按方案匹配库存：汰换前刷新库存 → taihuan_executor 匹配 → 出报告。"""
    log_message = Signal(str)
    match_done = Signal(object)      # MatchReport
    error = Signal(str)

    def __init__(self, plan_materials: list, parent=None):
        super().__init__(parent)
        self.plan_materials = plan_materials
        self.refresh_inventory_first = True   # 汰换前必须刷新库存

    def run(self):
        try:
            if self.refresh_inventory_first:
                self.log_message.emit("🔄 汰换前刷新库存（ECO 网页爬取）...")
                try:
                    count = inventory_fetcher.refresh_inventory()
                    self.log_message.emit(f"✓ 库存已刷新：{count} 件")
                except Exception as e:
                    self.error.emit(f"汰换前刷新库存失败：{e}")
                    return

            from core import taihuan_executor
            inv = taihuan_executor.load_inventory_from_db()
            self.log_message.emit(f"📦 当前库存 {len(inv)} 件，开始匹配方案...")
            report = taihuan_executor.match_plan_to_inventory(
                self.plan_materials, inv)
            for sm in report.all_slots:
                if sm.status == "ok":
                    self.log_message.emit(
                        f"  ✓ 槽位{sm.slot_index+1} {sm.slot_name} "
                        f"→ {sm.chosen_hash_name}（磨损 {sm.chosen_wear:.6f}）")
                elif sm.status == "wear_deviation":
                    self.log_message.emit(
                        f"  ⚠ 槽位{sm.slot_index+1} {sm.slot_name} 磨损偏差 "
                        f"{sm.best_gap:.3f} > 0.2（最佳候选 {sm.best_wear:.6f}"
                        f"，区间 [{sm.wear_min:.4f},{sm.wear_max:.4f}]）→ 待定")
                else:
                    self.log_message.emit(
                        f"  ✗ 槽位{sm.slot_index+1} {sm.slot_name} "
                        f"缺口：{sm.missing_reason}")
            self.log_message.emit(f"匹配完成：{report.summary}")
            self.match_done.emit(report)
        except Exception as e:
            logger.exception("方案匹配失败")
            self.error.emit(f"方案匹配失败：{e}")


class PlanExecuteWorker(QThread):
    """汰换合同完整执行流程（2026-09-02 用户梳理）：

      1. 由上到下排序已确认物品，右键最上面的第一件（种子物品）
      2. 左键右键弹窗中的「汰换」按钮（模板匹配 taihuan_popup_btn.png）
      3. 游戏切换为 4 列过滤视图（只显示与种子同品质的物品）
      4. 逐件左键添加材料到汰换栏（2026-09-02 用户确认：从上向下找货）：
         - 假设种子物品已被自动放入汰换栏（不再出现在过滤视图）
         - 过滤视图顺序 = 库存 CSV 顺序按品质过滤（排除种子）
         - 点击顺序按位置升序（先点靠上的，从上向下依次添加）
         - 每点一件后方物品前移补缺 → 后续目标实际位置 = 原始位置
           - 已点击件数（升序下已点击件均在当前目标上方）
         - 超出可见行时向下滚动滚轮
      5. 满足 10 件后游戏自动开始汰换
    """
    item_start = Signal(str, int, int)   # name, idx_1based, total
    item_done = Signal(object)           # ClickResult
    scroll_progress = Signal(int, int)
    log_message = Signal(str)
    finished_ok = Signal(list)           # list[ClickResult]
    post_refresh_done = Signal(int)      # 汰换后刷新库存条数
    error = Signal(str)

    def __init__(self, confirmed_slots: list, csv_path: str,
                 inter_item_delay: float = 1.0,
                 seed_pre_added: bool = True, parent=None):
        """confirmed_slots: list[SlotMatch]（status 均为 ok）。

        seed_pre_added: 点击弹窗汰换按钮后种子是否已自动入汰换栏
        （True=过滤视图不含种子，只需点击剩余 N-1 件）。
        """
        super().__init__(parent)
        self.confirmed_slots = confirmed_slots
        self.csv_path = csv_path
        self.inter_item_delay = inter_item_delay
        self.seed_pre_added = seed_pre_added
        self._stop = False

    def stop(self):
        self._stop = True

    # ------------------------------------------------------------
    def _click_popup_button(self):
        """等待并左键右键弹窗中的「汰换」按钮（模板匹配，重试）。"""
        from core import game_interact as gi
        from utils.config import TAIHUAN_POPUP_BTN
        for attempt in range(8):
            if self._stop:
                return None
            time.sleep(0.5)
            pos = gi.find_template_on_screen(TAIHUAN_POPUP_BTN,
                                             threshold=0.8)
            if pos:
                gi.click_left_screen(*pos)
                return pos
            if attempt == 3:
                self.log_message.emit("⚠ 尚未匹配到汰换按钮，继续等待...")
        return None

    def _compute_filtered_positions(self, seed_asset_id: str,
                                    seed_name: str, quality: str):
        """计算过滤视图：同品质物品（CSV 顺序），可选排除种子。

        Returns:
            (filtered_rows, pos_by_asset) —
            filtered_rows: 过滤后的 CSV 行列表；
            pos_by_asset: asset_id -> 过滤视图位置下标
        """
        from core import game_interact as gi
        from core.taihuan_executor import get_quality_by_name
        rows = gi.load_inventory_rows(self.csv_path)
        filtered = []
        for row in rows:
            asset = (row.get("asset_id") or row.get("AssetId")
                     or "").strip()
            if not asset:
                continue
            if self.seed_pre_added and asset == str(seed_asset_id):
                continue
            name = (row.get("名称") or row.get("goods_name")
                    or row.get("hash_name") or "").strip()
            if get_quality_by_name(name) != quality:
                continue
            filtered.append((asset, name))
        pos_by_asset = {a: i for i, (a, _n) in enumerate(filtered)}
        return filtered, pos_by_asset

    # ------------------------------------------------------------
    def run(self):
        try:
            from core import game_interact as gi
            from core.taihuan_executor import get_quality_by_name
            from utils.config import get_game_interact_config
            cfg = get_game_interact_config()

            deps = gi._check_deps()
            if deps is not True:
                raise RuntimeError(
                    f"依赖缺失：{deps} 未安装，请 pip install 对应包")
            hwnd = gi.find_cs2_window()
            if hwnd is None:
                raise RuntimeError("未找到 CS2 窗口，请确保游戏已运行")

            from core.game_interact import ClickResult
            results = []

            # ---- 1) 由上到下：右键种子（最上物品） ----
            located = []
            for sm in self.confirmed_slots:
                try:
                    idx, _row, _cx, _total = gi.get_item_info_by_asset_id(
                        self.csv_path, sm.chosen_asset_id, cfg)
                    located.append((idx, sm))
                except ValueError as e:
                    self.log_message.emit(f"⚠ 定位失败 {sm.slot_name}: {e}")
            located.sort(key=lambda t: t[0])
            if not located:
                raise RuntimeError("所有物品均无法在库存中定位，执行中止")

            total = len(located)
            seed = located[0][1]
            self.log_message.emit(
                f"▶ 1/3 右键种子物品（仓库最上）：{seed.chosen_hash_name}")
            self.item_start.emit(seed.slot_name, 1, total)
            ok = gi.locate_and_click_item_by_asset_id(
                hwnd, self.csv_path, seed.chosen_asset_id, cfg,
                on_progress=lambda e, t: self.scroll_progress.emit(e, t))
            results.append(ClickResult(
                seed.slot_name, ok, "成功" if ok else "定位失败"))
            self.item_done.emit(results[-1])
            if not ok:
                raise RuntimeError("种子物品右键失败，执行中止")
            time.sleep(self.inter_item_delay)

            if self._stop:
                self.log_message.emit("⏹ 用户请求停止")
                self.finished_ok.emit(results)
                return

            # ---- 2) 左键弹窗「汰换」按钮 ----
            self.log_message.emit("▶ 2/3 等待右键弹窗，模板匹配「汰换」按钮...")
            btn = self._click_popup_button()
            if btn is None:
                raise RuntimeError(
                    "未找到汰换弹窗按钮（模板 taihuan_popup_btn.png），"
                    "请确认右键后弹窗已出现")
            self.log_message.emit(f"  ✓ 已左键汰换按钮 ({btn[0]},{btn[1]})")

            if self._stop:
                self.finished_ok.emit(results)
                return

            # ---- 3) 过滤视图逐件左键添加 ----
            quality = get_quality_by_name(seed.chosen_hash_name)
            if not quality:
                raise RuntimeError(
                    f"无法确定种子品质：{seed.chosen_hash_name}，"
                    "主 CSV 中未找到该物品")
            time.sleep(1.5)   # 等待过滤视图切换
            filtered, pos_by_asset = self._compute_filtered_positions(
                seed.chosen_asset_id, seed.chosen_hash_name, quality)
            self.log_message.emit(
                f"▶ 3/3 过滤视图（品质={quality}）预计 {len(filtered)} 件"
                "（种子已排除），请对照游戏内数量；如不一致位置会偏移")

            # 剩余目标 → 过滤视图位置，按位置升序（从上向下找货，
            # 用户 2026-09-02 确认：右键汰换后从上向下依次添加）
            targets = []
            for _idx, sm in located[1:]:
                p = pos_by_asset.get(str(sm.chosen_asset_id))
                if p is None:
                    self.log_message.emit(
                        f"⚠ {sm.chosen_hash_name} 不在过滤视图列表中，跳过")
                    continue
                targets.append((p, sm))
            targets.sort(key=lambda t: t[0])
            if not targets:
                raise RuntimeError("无剩余物品可在过滤视图中定位")

            # 保险：过滤视图滚回顶部（刚进入时本就在顶部，
            # 多滚几格向上在顶部是 no-op，可清除残留滚动状态）
            gi.scroll_filtered_rows(hwnd, -20, cfg)
            time.sleep(0.8)

            vis = cfg.FILTERED_VISIBLE_ROWS
            rpn = cfg.FILTERED_ROWS_PER_NOTCH   # 0.505 行/格（小数）
            first_row = 0.0   # 当前可视区第一行对应的过滤视图行号
            clicked = 0       # 已点击件数（均位于当前目标上方）
            for i, (orig_pos, sm) in enumerate(targets, 2):
                if self._stop:
                    self.log_message.emit("⏹ 用户请求停止")
                    break
                # 每点一件移入汰换栏后，其后方物品前移补缺；
                # 升序点击下已点击件均在当前目标上方，
                # 故实际位置 = 原始位置 - 已点击数
                pos = orig_pos - clicked
                t_row, t_col = pos // cfg.FILTERED_COLS, \
                    pos % cfg.FILTERED_COLS
                # 需要滚动时：把目标行滚入可视区（循环修正滚轮整格误差）
                # 判据用四舍五入后的 visual_row（first_row 带小数时，
                # 浮点边界判断会漏掉 round 后越界的情况）
                # rpn 为小数（96px/格 ÷ 190px/行），notches 必须 int
                # （scroll_filtered_rows 内部 range(abs(notches)) 不接受 float）
                visual_row = int(round(t_row - first_row))
                guard = 0
                while not (0 <= visual_row < vis) and guard < 8:
                    if visual_row >= vis:
                        need = visual_row - vis + 1   # 需下滚的行数
                        notches = max(1, math.ceil(need / rpn))
                        gi.scroll_filtered_rows(hwnd, notches, cfg)
                        first_row += notches * rpn
                    else:
                        need = -visual_row             # 需上滚的行数
                        notches = max(1, math.ceil(need / rpn))
                        gi.scroll_filtered_rows(hwnd, -notches, cfg)
                        first_row = max(0.0, first_row - notches * rpn)
                    time.sleep(0.6)
                    guard += 1
                    visual_row = int(round(t_row - first_row))
                    self.log_message.emit(
                        f"  滚动后可视首行={first_row:.2f}"
                        f"（目标行 {t_row}）")
                if not (0 <= visual_row < vis):
                    self.log_message.emit(
                        f"⚠ 目标行 {t_row} 滚动后仍未进入可视区"
                        f"（首行 {first_row}），跳过 {sm.chosen_hash_name}")
                    results.append(ClickResult(
                        sm.slot_name, False, "滚动定位失败"))
                    self.item_done.emit(results[-1])
                    continue
                self.item_start.emit(sm.slot_name, i, total)
                self.log_message.emit(
                    f"  [{i}/{total}] 左键 {sm.chosen_hash_name}"
                    f"（原位置 {orig_pos} → 现位置 {pos} = "
                    f"行{t_row}列{t_col}，可视行 {visual_row}）")
                gi.click_filtered_cell(hwnd, visual_row, t_col, cfg)
                clicked += 1
                results.append(ClickResult(sm.slot_name, True, "已添加"))
                self.item_done.emit(results[-1])
                time.sleep(self.inter_item_delay)

            ok_n = sum(1 for r in results if r.success)
            self.log_message.emit(
                f"✓ 添加完成：成功 {ok_n}/{len(results)}"
                "（含种子），满 10 件游戏将自动开始汰换")
            self.finished_ok.emit(results)
            # 注：不自动刷新库存（浏览器会抢游戏焦点）；
            # 汰换完成后请手动刷新或重新「匹配库存」
        except Exception as e:
            logger.exception("方案执行失败")
            self.error.emit(f"方案执行失败：{e}")


class GameClickWorker(QThread):
    """后台执行游戏内点击线程。"""
    item_start = Signal(str, int, int)  # name, idx_1based, total
    item_done = Signal(object)          # ClickResult
    scroll_progress = Signal(int, int)  # executed, total
    log_message = Signal(str)
    finished_ok = Signal(list)          # list[ClickResult]
    error = Signal(str)

    def __init__(self, item_names: list[str], csv_path: str,
                 inter_item_delay: float = 1.0, parent=None):
        super().__init__(parent)
        self.item_names = item_names
        self.csv_path = csv_path
        self.inter_item_delay = inter_item_delay
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self):
        try:
            self.log_message.emit(
                f"开始游戏内点击：共 {len(self.item_names)} 件材料")
            results = game_interact.batch_click_items(
                self.item_names,
                csv_path=self.csv_path,
                on_item_start=lambda n, i, t: self.item_start.emit(n, i, t),
                on_item_done=lambda r: self.item_done.emit(r),
                on_scroll_progress=lambda e, t: self.scroll_progress.emit(e, t),
                inter_item_delay=self.inter_item_delay,
            )
            self.log_message.emit(
                f"点击完成：成功 {sum(1 for r in results if r.success)}/"
                f"{len(results)}")
            self.finished_ok.emit(results)
        except Exception as e:
            logger.exception("游戏点击失败")
            self.error.emit(f"游戏点击失败：{e}")


# ============================================================
# ECO 网页自定义饰品 Worker（customBatchAdd / UserCustomList + CustomDelete）
# ============================================================

class EcoCustomApiWorker(QThread):
    """后台执行 ECO 自定义饰品的同步/清空动作，避免 UI 阻塞。"""
    log_message = Signal(str)
    finished_ok = Signal(object)    # 根据 action 不同返回 dict / list
    error = Signal(str)

    def __init__(self, action: str,
                 custom_items: list | None = None,
                 parent=None):
        """
        action:
          - 'sync_batch_add'     : custom_items -> POST customBatchAdd
          - 'list_then_delete_all': GET UserCustomList -> 逐条 CustomDelete
          - 'list_only'          : 仅 GET UserCustomList
        """
        super().__init__(parent)
        self.action = action
        self.custom_items = custom_items or []

    def run(self):
        try:
            from core import eco_web_api

            if self.action == "sync_batch_add":
                if not self.custom_items:
                    raise ValueError("customBatchAdd 缺少 custom_items")
                self.log_message.emit(
                    f"📋 写入 {len(self.custom_items)} 件自定义饰品到 ECO ...")
                result = eco_web_api.custom_batch_add(self.custom_items)
                self.log_message.emit("✓ customBatchAdd 成功。")
                self.finished_ok.emit(
                    {"items": self.custom_items, "raw": result})
                return

            if self.action in ("list_only", "list_then_delete_all"):
                self.log_message.emit("🔍 读取 ECO 自定义饰品列表 ...")
                lst = eco_web_api.user_custom_list(use_cache=False)
                items = lst if isinstance(lst, list) else (
                    lst.get("List") if isinstance(lst, dict) else [])
                if not isinstance(items, list):
                    items = []
                self.log_message.emit(f"✓ 共 {len(items)} 件自定义饰品。")

                if self.action == "list_only":
                    self.finished_ok.emit({"items": items, "raw": lst})
                    return

                # list_then_delete_all
                if not items:
                    self.finished_ok.emit({"deleted_count": 0})
                    return
                ids = []
                for it in items:
                    if not isinstance(it, dict):
                        continue
                    for k in ("CustomID", "CustomId", "Id", "ID", "id"):
                        v = it.get(k)
                        if v not in (None, ""):
                            ids.append(v)
                            break
                # 去重，保留顺序
                seen = set()
                unique_ids = []
                for v in ids:
                    if v in seen:
                        continue
                    seen.add(v)
                    unique_ids.append(v)
                self.log_message.emit(
                    f"🗑 将删除 {len(unique_ids)} 件自定义饰品 ...")
                # 批量 CustomDelete（ECO 支持数组或单条；接口实现里统一塞数组）
                result = eco_web_api.custom_delete(unique_ids)
                self.finished_ok.emit({
                    "deleted_count": len(unique_ids),
                    "raw": result,
                })
                return

            raise ValueError(f"未知 action: {self.action!r}")
        except Exception as e:
            logger.exception("ECO 自定义饰品操作失败")
            self.error.emit(f"ECO 自定义饰品操作失败：{e}")


# ============================================================
# ECO 官方汰换模拟 + (可选) 保存配方 Worker
# ============================================================

class EcoSimulationWorker(QThread):
    """后台：StartSimulation 计算，或 SaveSimulationResult。

    - save_formula_payload=None: 执行一次 start_simulation_web(api_materials)
      → 调 recipe_matcher.simulate_outputs_from_eco_result(** 只做后处理不发第二次 HTTP **)
      → 并发查三平台最低价 + 核算实时材料成本，
      返回 result_dict = {source, output_details, material_details, material_price_list,
                         groups, cost, ref_value, profit, keep_rate, ..., sim_result_raw, outputs}
    - save_formula_payload != None: 不做模拟，直接 save_simulation_result
      保存为 ECO 我的配方，返回 ResultData dict（含 FormulaID）。

    Args:
        api_materials: 10 件 HTTP 请求体（HashName/WearValue/MaterialSource/Sort）
        gui_materials: 10 件 GUI 层完整材料字典（含 wear_grade/buff_goods_id/wear_min/wear_max
                       /c5_market_hash_name/collection/quality）；不传则退化为 api_materials
                       （recipe_matcher._normalize_materials 的 HTTP 模式会反查主 CSV 兜底，
                       但明显传完整字典更鲁棒）
        save_formula_payload: 若存在则走"保存配方"分支；否则走"模拟"分支
        save_formula_name: 保存配方的显示名
    """
    log_message = Signal(str)
    finished_ok = Signal(dict)
    error = Signal(str)

    def __init__(self,
                 api_materials: list,
                 gui_materials: list | None = None,
                 save_formula_payload=None,
                 save_formula_name: str | None = None,
                 probe_lower_tiers: bool = False,
                 parent=None):
        super().__init__(parent)
        self.api_materials = api_materials or []
        self.gui_materials = gui_materials      # NEW：GUI 层完整 10 件材料元数据
        self.save_formula_payload = save_formula_payload
        self.save_formula_name = save_formula_name
        self.probe_lower_tiers = probe_lower_tiers   # 替换模式：低档更便宜标红提示

    def run(self):
        try:
            from core import eco_web_api, recipe_matcher

            if self.save_formula_payload is not None:
                self.log_message.emit(
                    f"💾 保存配方: {self.save_formula_name or '(自动)'}")
                result = eco_web_api.save_simulation_result(
                    self.save_formula_payload,
                    formula_name=self.save_formula_name)
                self.finished_ok.emit(
                    result if isinstance(result, dict) else {})
                return

            if len(self.api_materials) != 10:
                raise ValueError(
                    f"StartSimulation 需要正好 10 件材料，实际 "
                    f"{len(self.api_materials)} 件")

            # --- 唯一一次 StartSimulation HTTP 请求 ---
            self.log_message.emit("🧪 调用 ECO 官方 StartSimulation ...")
            sim_result = eco_web_api.start_simulation_web(self.api_materials)
            outputs = eco_web_api.simulation_to_output_skeleton(sim_result)
            self.log_message.emit(
                f"✓ 官方模拟返回 {len(outputs)} 个可能产出，"
                f"现在并发查三平台最低价并重算指标 ...")

            # --- 核心修复：不再调第二次 simulate_outputs（否则很容易 ResultCode=1），
            #     改用公开 API simulate_outputs_from_eco_result，直接把刚才成功的
            #     sim_result + GUI 完整材料元数据喂进去做纯后处理。
            #     gui_materials 优先；没传就退化为 api_materials（HTTP 模式 normalize 兜底）。
            mats_for_post = (list(self.gui_materials)
                             if isinstance(self.gui_materials, list)
                                and len(self.gui_materials) == 10
                             else [dict(m) for m in self.api_materials])
            sim = recipe_matcher.simulate_outputs_from_eco_result(
                sim_result,
                mats_for_post,
                platforms=None,                # 默认三平台 buff+c5+eco
                use_live_material_cost=True,   # 实时查 10 件材料做真实成本
                error_fallback=True,           # 后处理若失败才降级（但正常用户流程不会触发）
                probe_lower_tiers=self.probe_lower_tiers,  # 替换模式：低档探查标红
            )
            result_dict = dict(sim) if isinstance(sim, dict) else {}
            # 让 GUI 能把原始 ResultData 保存成配方
            result_dict["sim_result_raw"] = sim_result
            # 为旧代码兼容：result_dict["outputs"] 也塞一份 skeleton 原始列表
            # （新版 GUI 主要读 output_details，但有些 log/面板可能仍读 outputs）
            if "outputs" not in result_dict or not result_dict.get("outputs"):
                result_dict["outputs"] = outputs
            self.finished_ok.emit(result_dict)
        except Exception as e:
            logger.exception("ECO 官方模拟/保存失败")
            self.error.emit(f"ECO 官方模拟/保存失败：{e}")


# ============================================================
# 自动汰换面板资源加载（解决卡顿第 1 点）
# ============================================================

class LoadTaihuanAssetsWorker(QThread):
    """后台一次性加载主 CSV + 收藏品/品质下拉 + 保存方案列表。"""
    log_message = Signal(str)
    finished_ok = Signal(dict)
    error = Signal(str)

    def run(self):
        try:
            from core import recipe_matcher
            from core.data_manager import (
                load_items_from_main_csv_full,
                get_unique_collections,
                get_unique_rarities,
            )
            self.log_message.emit("📚 后台解析主 CSV（材料卡片）...")
            rows = load_items_from_main_csv_full()
            self.log_message.emit(f"  · 共 {len(rows)} 行材料。")
            self.log_message.emit("📚 后台解析收藏品/品质下拉 ...")
            cols = get_unique_collections(rows)
            rars = get_unique_rarities(rows)
            self.log_message.emit("📚 后台读取已保存方案列表 ...")
            plans = recipe_matcher.list_saved_plans()
            self.finished_ok.emit({
                "main_csv_rows": rows,
                "collections": cols,
                "rarities": rars,
                "saved_plans": plans,
            })
        except Exception as e:
            logger.exception("自动汰换资源加载失败")
            self.error.emit(f"自动汰换资源加载失败：{e}")


# ============================================================
# 历史记录后台查询（解决卡顿第 2 点）
# ============================================================

class HistoryQueryWorker(QThread):
    """后台按筛选条件 query_history。"""
    finished_ok = Signal(list)       # list[tuple] 数据行
    error = Signal(str)

    def __init__(self,
                 date_from: str, date_to: str,
                 name: str | None,
                 platform: str | None,
                 op_type: str | None,
                 parent=None):
        super().__init__(parent)
        self.date_from = date_from
        self.date_to = date_to
        self.name = name
        self.platform = platform
        self.op_type = op_type

    def run(self):
        try:
            from core.data_manager import query_history
            rows = query_history(
                self.date_from, self.date_to,
                self.name, self.platform, self.op_type)
            self.finished_ok.emit(list(rows))
        except Exception as e:
            logger.exception("历史记录查询失败")
            self.error.emit(f"历史记录查询失败：{e}")


# ============================================================
# RealReplaceResult 真实结果对比（step 5）
# ============================================================

class RealReplaceCompareWorker(QThread):
    """按 FormulaID：
      1) 读 FormulaDetail 拿 10 件材料
      2) 读 RealReplaceResult 真实执行结果
      3) 调用 recipe_matcher.real_replace_compare() 重算成本与期望
    返回归一化 dict。
    """
    log_message = Signal(str)
    finished_ok = Signal(dict)
    error = Signal(str)

    def __init__(self, formula_id: str, parent=None):
        super().__init__(parent)
        self.formula_id = (formula_id or "").strip()

    def run(self):
        try:
            if not self.formula_id:
                raise ValueError("FormulaID 为空")
            from core import recipe_matcher
            self.log_message.emit(
                f"🔍 读取 ECO FormulaDetail: {self.formula_id}")
            self.log_message.emit(
                "🔍 拉取 RealReplaceResult 并重算成本/期望 ...")
            result = recipe_matcher.real_replace_compare(self.formula_id)
            self.log_message.emit(
                f"✓ 真实结果对比完成："
                f"共 {len(result.get('real_rows', []))} 次真实汰换，"
                f"成本￥{result.get('material_cost', 0.0):.2f}，"
                f"真实期望￥{result.get('real_expected_value', 0.0):.2f}")
            self.finished_ok.emit(dict(result))
        except Exception as e:
            logger.exception("真实结果对比失败")
            self.error.emit(f"真实结果对比失败：{e}")


class TempTestWorker(QThread):
    """临时测试：滚轮/滑块像素量标定线程。

    通过检测滑块位置的前后差值，实测像素量：
    - "wheel"  模式：滚 N 格滚轮 → 滑块实际位移（每格像素量）
    - "slider" 模式：请求滑块上移 N 像素 → 实际位移
    - "detect" 模式：仅报告当前滑块屏幕坐标
    """
    log_message = Signal(str)
    finished_ok = Signal()
    error = Signal(str)

    def __init__(self, mode: str, ticks: int = 0, slider_px: int = 0,
                 parent=None):
        super().__init__(parent)
        self.mode = mode              # "wheel" | "slider" | "detect"
        self.ticks = int(ticks)
        self.slider_px = int(slider_px)
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self):
        try:
            import pyautogui
            import win32gui
            from core import game_interact as gi
            from utils.config import get_game_interact_config
            cfg = get_game_interact_config()

            deps = gi._check_deps()
            if deps is not True:
                raise RuntimeError(f"依赖缺失：{deps} 未安装")
            hwnd = gi.find_cs2_window()
            if hwnd is None:
                raise RuntimeError(
                    "未找到 CS2 窗口，请确保游戏已运行且仓库界面可见")

            def detect():
                pos = gi.get_slider_position(cfg)
                if pos is None:
                    raise RuntimeError(
                        "滑块检测失败：请确保 CS2 仓库界面可见、"
                        "滑块轨道区域（屏幕右侧 x≈1815）未被遮挡")
                return pos

            if self.mode == "detect":
                x, y = detect()
                self.log_message.emit(f"📍 当前滑块位置：x={x}, y={y}")
                self.finished_ok.emit()
                return

            pos0 = detect()
            self.log_message.emit(f"📍 测试前滑块位置：y={pos0[1]}")

            done = 0
            if self.mode == "wheel":
                # 聚焦游戏窗口滚动区域（与 scroll_to_row 相同的定位方式）
                rect = win32gui.GetWindowRect(hwnd)
                pyautogui.moveTo(rect[0] + cfg.SCROLL_MOUSE_X,
                                 rect[1] + cfg.SCROLL_MOUSE_Y, duration=0.1)
                pyautogui.click()
                time.sleep(0.1)
                for i in range(self.ticks):
                    if self._stop:
                        self.log_message.emit(
                            f"⏹ 用户请求停止（已滚动 {i} 格）")
                        break
                    pyautogui.scroll(-cfg.SCROLL_STEP)
                    time.sleep(cfg.SCROLL_DELAY)
                    done = i + 1
                time.sleep(0.3)
            elif self.mode == "slider":
                if self._stop:
                    self.log_message.emit("⏹ 用户请求停止")
                    self.finished_ok.emit()
                    return
                if not gi.adjust_slider_up(cfg, pixels=self.slider_px):
                    raise RuntimeError("滑块上移失败（无法获取滑块位置）")
                done = 1
                time.sleep(0.3)

            if done == 0:
                self.finished_ok.emit()
                return

            pos1 = detect()
            dy = pos1[1] - pos0[1]      # 负=滑块上移，正=滑块下移
            self.log_message.emit(f"📍 测试后滑块位置：y={pos1[1]}")
            if self.mode == "wheel":
                per = dy / done if done else 0.0
                self.log_message.emit(
                    f"🧪 滚轮 {done} 格 → 滑块位移 {dy:+d} 像素"
                    f"（平均每格 {per:+.1f} 像素；"
                    f"当前 SCROLL_STEP={cfg.SCROLL_STEP}，"
                    f"PIXELS_PER_ROW={cfg.PIXELS_PER_ROW}）")
            else:
                self.log_message.emit(
                    f"🧪 请求滑块上移 {self.slider_px} 像素 → "
                    f"实际位移 {dy:+d} 像素"
                    f"（{'向上' if dy < 0 else '向下' if dy > 0 else '未移动'}，"
                    f"|Δ|={abs(dy)}）")
            self.finished_ok.emit()
        except Exception as e:
            logger.exception("临时测试失败")
            self.error.emit(f"临时测试失败：{e}")

