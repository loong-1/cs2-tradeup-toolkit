"""汰换执行器：按方案槽位在库存中匹配最合理物品。

核心规则（用户确认 2026-09-01）：
  1. 高磨损先用：同槽位候选中按 paint_wear 降序，先消耗高磨损（差货先用）；
  2. 磨损偏差待定：候选最优磨损与槽位设定区间差距过大（默认 >0.2）→ 待定，
     等用户在 GUI 手动同意后再汰换；
  3. 缺口待定：库存中无满足名称+磨损区间的物品 → 待定，等仓库到货后重启；
  4. 全局去重：同一件库存物品（asset_id）只能被一个槽位选中；
  5. 点击定位最后计算：库存顺序会随汰换而变化，物品在点击前一刻才计算
     CSV 行列位置（交给 game_interact 逐件执行时处理）。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# 磨损差距阈值：|候选磨损 - 槽位设定值| > 该值 → 待定需人工确认
DEFAULT_WEAR_DEVIATION_THRESHOLD = 0.2


# ============================================================
# 数据结构
# ============================================================
@dataclass
class SlotMatch:
    """一个槽位的匹配结果。"""
    slot_index: int                    # 槽位序号 0-9
    slot_name: str                     # 槽位物品名（皮肤名称/市场哈希名）
    wear_min: float                    # 槽位设定磨损区间
    wear_max: float
    status: str = "pending"            # ok / wear_deviation / missing / pending
    # status=ok 时：选中的库存物品
    chosen_asset_id: str = ""
    chosen_hash_name: str = ""
    chosen_wear: float = 0.0
    # status=wear_deviation 时：最优候选（供人工确认）
    best_asset_id: str = ""
    best_wear: float = 0.0
    best_gap: float = 0.0              # 与区间最近端的距离
    # status=missing 时：缺口描述
    missing_reason: str = ""
    # 全部满足名称的候选数（诊断用）
    name_candidates: int = 0
    in_range_candidates: int = 0


@dataclass
class MatchReport:
    """整轮匹配报告。"""
    ok_slots: list[SlotMatch] = field(default_factory=list)
    wear_deviation_slots: list[SlotMatch] = field(default_factory=list)
    missing_slots: list[SlotMatch] = field(default_factory=list)
    all_slots: list[SlotMatch] = field(default_factory=list)

    @property
    def can_execute(self) -> bool:
        """全部槽位 ok 才可直接执行；有待定则不行。"""
        return (len(self.ok_slots) == len(self.all_slots)
                and len(self.all_slots) > 0)

    @property
    def summary(self) -> str:
        return (f"就绪 {len(self.ok_slots)} / 磨损偏差 {len(self.wear_deviation_slots)}"
                f" / 缺口 {len(self.missing_slots)} / 共 {len(self.all_slots)}")


# ============================================================
# 库存读取
# ============================================================
def load_inventory_from_db() -> list[dict]:
    """读取当前库存（inventory 表 → eco_inventory.csv 兜底）。"""
    rows: list[dict] = []
    try:
        from core.data_manager import get_conn
        conn = get_conn()
        cur = conn.execute(
            "SELECT asset_id, hash_name, goods_name, paint_wear, tradable, status "
            "FROM inventory ORDER BY rowid")
        for r in cur.fetchall():
            rows.append({
                "asset_id": r[0] or "",
                "hash_name": r[1] or "",
                "goods_name": r[2] or "",
                "paint_wear": float(r[3] or 0),
                "tradable": int(r[4] or 0),
                "status": int(r[5] if r[5] is not None else -1),
            })
        conn.close()
    except Exception as e:
        logger.warning("读取库存 DB 失败，回退 CSV：%s", e)
    if rows:
        return rows
    # 兜底：CSV
    try:
        import csv as _csv
        from core.inventory_fetcher import INVENTORY_CSV_PATH
        import os
        if not os.path.exists(INVENTORY_CSV_PATH):
            return []
        with open(INVENTORY_CSV_PATH, "r", encoding="utf-8-sig") as f:
            for row in _csv.DictReader(f):
                try:
                    wear = float(row.get("磨损") or 0)
                except (TypeError, ValueError):
                    wear = 0.0
                rows.append({
                    "asset_id": (row.get("asset_id") or "").strip(),
                    "hash_name": (row.get("名称") or "").strip(),
                    "goods_name": (row.get("名称") or "").strip(),
                    "paint_wear": wear,
                    "tradable": 1,
                    "status": -1,
                })
    except Exception as e:
        logger.warning("读取库存 CSV 失败：%s", e)
    return rows


# ============================================================
# 名称匹配（中英对照）
# ============================================================
# 库存来自 ECO 网页爬取：hash_name/goods_name 都是【中文】（如
# 'MP7 | 珊瑚佩斯利 (久经沙场)'）；而方案材料可能存的是主 CSV 的
# 【英文市场哈希名】（如 'MP7 | Coral Paisley'）或【中文皮肤名称】。
# → 从主 CSV 构建 中↔英 映射（皮肤名称 ↔ 市场哈希名称），
#   匹配时槽位名的中/英变体都对库存名做包含匹配。
_CN_EN_MAP: dict[str, str] = {}     # 中文皮肤名(小写) -> 英文基础名(小写)
_EN_CN_MAP: dict[str, str] = {}     # 英文基础名(小写) -> 中文皮肤名(小写)
_MAP_LOADED = False


def _ensure_name_map() -> None:
    """懒加载主 CSV 的中英皮肤名映射（进程内只加载一次）。"""
    global _MAP_LOADED
    if _MAP_LOADED:
        return
    _MAP_LOADED = True
    try:
        from core.data_manager import load_items_from_main_csv_full
        for row in load_items_from_main_csv_full():
            cn = (row.get("皮肤名称") or "").strip().lower()
            en = (row.get("市场哈希名称") or "").strip().lower()
            if cn and en:
                _CN_EN_MAP[cn] = en
                _EN_CN_MAP[en] = cn
    except Exception as e:
        logger.warning("加载中英名称映射失败：%s", e)


def _strip_wear_suffix(name: str) -> str:
    """去掉名称里的中文/英文磨损档后缀，返回基础名（小写）。

    'MP7 | 珊瑚佩斯利 (久经沙场)' -> 'mp7 | 珊瑚佩斯利'
    'MP7 | Coral Paisley (Field-Tested)' -> 'mp7 | coral paisley'
    """
    import re
    s = (name or "").strip().lower()
    s = re.sub(r"\s*\((崭新出厂|略有磨损|久经沙场|破损不堪|战痕累累|"
               r"Factory New|Minimal Wear|Field-Tested|Well-Worn|"
               r"Battle-Scarred)\)\s*$", "", s)
    return s


def _expand_variants(name: str) -> set[str]:
    """把一个名称展开成全部基础名变体（去档后缀 + 中英互译）。"""
    _ensure_name_map()
    base = _strip_wear_suffix(name)
    out = {(name or "").strip().lower(), base}
    if base in _CN_EN_MAP:
        out.add(_CN_EN_MAP[base])
    if base in _EN_CN_MAP:
        out.add(_EN_CN_MAP[base])
    out.discard("")
    return out


# 基础名(小写, 去档后缀) -> 品质 的懒加载映射（主 CSV）
_QUALITY_BY_BASENAME: dict[str, str] = {}


def get_quality_by_name(name: str) -> str:
    """按物品名（中文皮肤名/英文哈希名均可）从主 CSV 反查品质。

    自动去磨损档后缀；查不到返回空串（非皮肤类物品：武器箱/印花等）。
    供汰换合同过滤视图的顺序计算用（游戏按品质过滤仓库）。
    """
    global _QUALITY_BY_BASENAME
    if not _QUALITY_BY_BASENAME:
        try:
            from core.data_manager import load_items_from_main_csv_full
            for row in load_items_from_main_csv_full():
                q = (row.get("品质") or "").strip()
                if not q:
                    continue
                for key in ("皮肤名称", "市场哈希名称"):
                    n = (row.get(key) or "").strip()
                    if n:
                        _QUALITY_BY_BASENAME.setdefault(
                            _strip_wear_suffix(n), q)
        except Exception as e:
            logger.warning("品质映射加载失败：%s", e)
    return _QUALITY_BY_BASENAME.get(_strip_wear_suffix(name or ""), "")


def _name_matches(slot_name: str, inv_hash: str, inv_goods: str) -> bool:
    """槽位物品名与库存物品名匹配。

    策略：槽位名与库存名各自展开成基础名变体集合
    （原样/去档后缀/中英互译），任一变体互相包含即命中。
    """
    sn = (slot_name or "").strip()
    if not sn:
        return False
    slot_vars = _expand_variants(sn)
    if not slot_vars:
        return False

    for inv in (inv_hash, inv_goods):
        iv = (inv or "").strip().lower()
        if not iv:
            continue
        inv_vars = _expand_variants(iv)
        for v in slot_vars:
            for ivb in inv_vars:
                if v and ivb and (v in ivb or ivb in v):
                    return True
    return False


# ============================================================
# 匹配引擎
# ============================================================
def match_plan_to_inventory(
        plan_materials: list[dict],
        inventory: list[dict],
        wear_threshold: float = DEFAULT_WEAR_DEVIATION_THRESHOLD,
) -> MatchReport:
    """把方案槽位匹配到库存物品。

    Args:
        plan_materials: 方案材料列表（每项含 皮肤名称/市场哈希名称 +
            wear_min/wear_max 或 磨损_min/磨损_max）
        inventory: load_inventory_from_db() 的返回
        wear_threshold: 磨损偏差阈值（>该值 → wear_deviation 待定）

    匹配顺序：槽位按方案顺序；每个槽位在【未被占用的】库存里找
    名称匹配 + 磨损在区间内的候选，按磨损降序取第一件（高磨损先用）。
    """
    report = MatchReport()
    used_asset_ids: set[str] = set()

    for slot_idx, mat in enumerate(plan_materials):
        slot_name = (mat.get("市场哈希名称") or mat.get("皮肤名称") or "").strip()
        # 磨损区间：优先自定义区间，其次默认档区间
        try:
            wmin = float(mat.get("wear_min") if mat.get("wear_min") is not None
                         else mat.get("磨损_min", 0.0))
            wmax = float(mat.get("wear_max") if mat.get("wear_max") is not None
                         else mat.get("磨损_max", 1.0))
        except (TypeError, ValueError):
            wmin, wmax = 0.0, 1.0
        if wmax <= wmin:
            wmax = wmin + 1e-6

        sm = SlotMatch(slot_index=slot_idx, slot_name=slot_name,
                       wear_min=wmin, wear_max=wmax)

        # 名称匹配候选（排除已占用）
        name_cands = [inv for inv in inventory
                      if inv["asset_id"] not in used_asset_ids
                      and _name_matches(slot_name, inv["hash_name"],
                                        inv["goods_name"])]
        sm.name_candidates = len(name_cands)

        # 磨损区间内候选
        in_range = [inv for inv in name_cands if wmin <= inv["paint_wear"] <= wmax]
        sm.in_range_candidates = len(in_range)

        if in_range:
            # 高磨损先用（降序）
            in_range.sort(key=lambda x: x["paint_wear"], reverse=True)
            chosen = in_range[0]
            sm.status = "ok"
            sm.chosen_asset_id = chosen["asset_id"]
            sm.chosen_hash_name = chosen["hash_name"]
            sm.chosen_wear = chosen["paint_wear"]
            used_asset_ids.add(chosen["asset_id"])
            report.ok_slots.append(sm)
        elif name_cands:
            # 有同名但磨损都不在区间 → 磨损偏差待定
            best = min(
                name_cands,
                key=lambda x: min(abs(x["paint_wear"] - wmin),
                                  abs(x["paint_wear"] - wmax)))
            gap = min(abs(best["paint_wear"] - wmin),
                      abs(best["paint_wear"] - wmax))
            sm.best_asset_id = best["asset_id"]
            sm.best_wear = best["paint_wear"]
            sm.best_gap = gap
            if gap > wear_threshold:
                sm.status = "wear_deviation"
                report.wear_deviation_slots.append(sm)
            else:
                # 偏差小：直接放行（视为可接受的就近匹配）
                sm.status = "ok"
                sm.chosen_asset_id = best["asset_id"]
                sm.chosen_hash_name = best["hash_name"]
                sm.chosen_wear = best["paint_wear"]
                used_asset_ids.add(best["asset_id"])
                report.ok_slots.append(sm)
        else:
            sm.status = "missing"
            sm.missing_reason = (
                f"库存中无名称匹配 '{slot_name}' 的未占用物品")
            report.missing_slots.append(sm)

        report.all_slots.append(sm)

    logger.info("方案匹配：%s", report.summary)
    return report
