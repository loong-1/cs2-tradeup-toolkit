"""库存→可执行汰换方案匹配器。

核心逻辑（来自 pachon/taihuan_bendi.py）：
1. 汰换规则：10 件同收藏品 + 同品质（次级）皮肤 → 合成 1 件上一级品质的皮肤
2. 输出磨损 = avg(材料实际磨损) × (目标皮肤 max_f - min_f) + min_f
3. 输出品质分级：≤0.07 崭新 / ≤0.15 略磨 / ≤0.38 久经 / ≤0.45 破损 / >0.45 战痕

数据来源：
- 主 CSV（物品箱子磨损对照表1_有效磨损及goods_id.csv）：
  收藏品名称/皮肤名称/品质/磨损区间/磨损/buff_goods_id/市场哈希名称/price_buff
- ECO 库存（inventory 表）：asset_id, goods_name, paint_wear

匹配策略：
对每个收藏品×目标品质，从库存中找同收藏品+次级品质的物品，
枚举 10 件组合，按预期产出磨损落在目标磨损区间内、
ROI（目标售价 - 材料成本）最高的方案排序。
由于库存物品成本可视为沉没成本，这里 ROI 用
  目标预估售价 / 0（即只看产出磨损是否落在想要的品质区间）
或
  目标预估售价 - 10 × 单件平均估价
本项目简化：只看产出磨损落在目标磨损区间内即可，profit 暂用 price_buff 估算。
"""
import json
import logging
import re
import sqlite3
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, asdict
from typing import Optional

from core.data_manager import (
    get_conn, load_items_from_main_csv_full,
    build_c5_market_hash_name, C5_APP_ID,
)
from utils.config import ECO_WEB_USE_OFFICIAL_SIMULATION
from core.inventory_fetcher import load_inventory_from_db, ensure_inventory_table
from core import eco_web_api
from core import price_fetcher

logger = logging.getLogger(__name__)

# 磨损阈值（来自 pachon/taihuan_bendi.py）
WEAR_THRESHOLDS = {
    "崭新出厂": 0.07,
    "略有磨损": 0.15,
    "久经沙场": 0.38,
    "破损不堪": 0.45,
    "战痕累累": 1.0,
}

QUALITY_LEVELS = {
    "消费级": 0,
    "工业级": 1,
    "军规级": 2,
    "受限级": 3,
    "保密级": 4,
    "隐秘级": 5,
    "稀有特殊物品": 6,
}


def get_next_quality(quality: str) -> Optional[str]:
    """获取上一级品质（汰换目标 = 上一级）。"""
    level = QUALITY_LEVELS.get(quality)
    if level is None or level >= 5:
        return None
    for q, lv in QUALITY_LEVELS.items():
        if lv == level + 1:
            return q
    return None


def get_wear_grade(float_val: float) -> str:
    """根据磨损值返回中文磨损等级。

    官方档位为【左闭右开】区间：边界整点属于更高磨损档
    （0.07→略有磨损、0.15→久经沙场、0.38→破损不堪、0.45→战痕累累）。
    """
    if float_val < 0.07:
        return "崭新出厂"
    elif float_val < 0.15:
        return "略有磨损"
    elif float_val < 0.38:
        return "久经沙场"
    elif float_val < 0.45:
        return "破损不堪"
    else:
        return "战痕累累"


def get_wear_range(grade: str, skin_min: float, skin_max: float):
    """根据目标磨损等级，裁剪皮肤的磨损区间。"""
    if grade not in WEAR_THRESHOLDS:
        return skin_min, skin_max
    upper = WEAR_THRESHOLDS[grade]
    prev_upper = 0.0
    for g, val in WEAR_THRESHOLDS.items():
        if g == grade:
            break
        prev_upper = val
    low = max(skin_min, prev_upper)
    high = min(skin_max, upper)
    if low > high:
        return skin_min, skin_max
    return low, high


# ============================================================
# 数据结构
# ============================================================
@dataclass
class Material:
    """汰换材料（库存中可用的物品）。"""
    asset_id: str
    goods_name: str
    paint_wear: float
    target_skin_name: str   # 库存物品对应的皮肤名称（从 hash_name 解析）
    collection: str
    quality: str


@dataclass
class TaihuanPlan:
    """一个可执行的汰换方案。"""
    target_collection: str
    target_quality: str
    target_skin_name: str
    target_price: float
    materials: list  # list[Material]
    material_cost: float
    output_float: float
    output_wear_grade: str
    profit: float
    roi: float
    # 以下为 v2 可选扩展字段（向后兼容：GUI 未感知也不会报错）
    source: str = "local"            # "eco_official"=来自 StartSimulation 官方结果；
                                     # "local"=本地估算兜底
    output_probs: list = field(default_factory=list)  # 匹配到的每个候选产出 (name,prob,wear)
    ref_value: float = 0.0           # 加权期望参考价值（所有产出 × 概率）
    keep_rate: float = 0.0           # 保本率（ref_value / cost * 100，封顶 100）


# ============================================================
# 主 CSV 解析（提供收藏品/品质/磨损区间/售价）
# ============================================================
def _parse_main_csv():
    """解析主 CSV，返回 dict:
    {
        (collection, quality, is_stattrak): [
            {
                "name": 皮肤名称,
                "min_f": float, "max_f": float,
                "wear": 磨损等级,
                "goods_id": int,
                "price_buff": float,
                "market_hash_name": str,
            }, ...
        ]
    }
    """
    rows = load_items_from_main_csv_full()
    index = defaultdict(list)
    for row in rows:
        collection = row.get("收藏品名称", "").strip()
        skin_name = row.get("皮肤名称", "").strip()
        quality = row.get("品质", "").strip()
        if not collection or not skin_name or not quality:
            continue
        wear_range_raw = row.get("磨损区间", "").replace("~", "").split()
        if len(wear_range_raw) >= 2:
            try:
                min_f = float(wear_range_raw[0])
                max_f = float(wear_range_raw[1])
            except ValueError:
                continue
        else:
            continue
        is_stattrak = "StatTrak" in skin_name or "StatTrak™" in skin_name
        try:
            goods_id = int(row.get("buff_goods_id") or 0)
        except ValueError:
            goods_id = 0
        try:
            price = float(row.get("price_buff") or 0)
        except ValueError:
            price = 0.0
        index[(collection, quality, is_stattrak)].append({
            "name": skin_name,
            "min_f": min_f,
            "max_f": max_f,
            "wear": row.get("磨损", "").strip(),
            "goods_id": goods_id,
            "price_buff": price,
            "market_hash_name": row.get("市场哈希名称", "").strip(),
        })
    return index


# ============================================================
# 反向索引 & 价格缓存（v2 官方模拟 + 实时查价用）
# ============================================================

def _canonical(s: str) -> str:
    """强归一化：大小写/空格/括号/分隔符/™ 全部统一。

    用于 hash_name / 皮肤名称的匹配兜底，保证 ECO 官方返回的 HashName / SPName
    与主 CSV 构建的键只要语义一致就能命中。
    """
    if not s:
        return ""
    t = str(s).lower()
    # 去特殊 unicode 空格 / 全角空格
    for ch in ("\u3000", "\xa0", "\t", "\r", "\n"):
        t = t.replace(ch, " ")
    # 统一括号：中文（） → 英文 ()
    t = t.replace("（", "(").replace("）", ")")
    # 统一分隔符：全角 ｜ / ｜ 各种变体 → 半角 |
    t = t.replace("\uff5c", "|").replace("\u2502", "|").replace("｜", "|")
    # 去™ / Ⓣ 等标记（stattrak™ → stattrak）
    t = t.replace("\u2122", "").replace("™", "")
    # 统一 stattrak (xxx) / stattrak™ / xxx（stattrak） → stattrak xxx
    # 先把 "(stattrak)" 去掉
    t = t.replace("(stattrak)", "")
    # 把 "weapon (stattrak) | skin" → "stattrak weapon | skin"
    if "| " in t and ") |" in t and "(stattrak" in t:
        t = "stattrak " + t.replace("(stattrak)", "").replace("(stattrak ", "")
    # 压缩多空格
    while "  " in t:
        t = t.replace("  ", " ")
    # 去掉 "| " 前后多余空格
    t = t.replace(" |", "|").replace("| ", "|")
    return t.strip()


def _build_full_hash_to_row_map():
    """基于主 CSV 生成 HashName / 皮肤名 → CSV 行的多键索引。

    索引分两类（第一类 3 种键、第二类 2 种键）：
      A. by_hash（完整英文磨损市场哈希名 → row）：
         - 原始键：build_c5_market_hash_name(市场哈希名称, 磨损)
         - 规范化键：_canonical(原始键)
         - 基础名键：_canonical(市场哈希名称)    （不带磨损后缀）
      B. by_name_wear（(皮肤名称, 磨损等级) → row）：
         - 原始键：(皮肤名称, 磨损)
         - 规范化键：(_canonical(皮肤名称), 磨损)

    这样只要 ECO 官方返回的 HashName / SPName 和主 CSV "语义相同"，
    哪怕差了空格/括号/StatTrak 写法/分隔符，也一定能命中 buff_goods_id。
    """
    rows = load_items_from_main_csv_full()
    by_hash = {}      # 完整英文磨损 hash_name -> row
    by_name_wear = {} # (皮肤名称, 磨损等级) -> row
    for row in rows:
        wear_cn = (row.get("磨损") or "").strip()
        base = (row.get("市场哈希名称") or "").strip()
        skin_name = (row.get("皮肤名称") or "").strip()
        if not skin_name and not base:
            continue
        if base and wear_cn:
            full_hash = build_c5_market_hash_name(base, wear_cn)
            if full_hash:
                if full_hash not in by_hash:
                    by_hash[full_hash] = row
                c_full = _canonical(full_hash)
                if c_full and c_full not in by_hash:
                    by_hash[c_full] = row
        if base:
            c_base = _canonical(base)
            if c_base and c_base not in by_hash:
                by_hash[c_base] = row
        if wear_cn and skin_name:
            key = (skin_name, wear_cn)
            if key not in by_name_wear:
                by_name_wear[key] = row
            c_key = (_canonical(skin_name), wear_cn.strip())
            if c_key not in by_name_wear:
                by_name_wear[c_key] = row
    return by_hash, by_name_wear


# 同一件（hash_name 唯一标识）物品的最低在售价 LRU 缓存（进程内）。
# 缓存键 -> (min_price, best_row_dict_or_None, errors_dict, rows_by_platform)
#   rows_by_platform: {"buff": row|None, "c5": row|None, "eco": row|None}
# GUI 方案搜索阶段大量 combo 会共享相同目标皮肤，能大幅减少爬取次数。
_price_query_cache: dict = {}
_price_query_cache_hits = 0
_price_query_cache_max = 2048


def _rows_by_platform(rows: list) -> dict:
    """把 price_fetcher.query_all 返回的归一化多行按 platform 折叠为「该平台最低价行」。"""
    d = {"buff": None, "c5": None, "eco": None}
    if not rows:
        return d
    for r in rows:
        if not isinstance(r, dict):
            continue
        p = str(r.get("platform") or "").lower()
        if p not in d:
            continue
        try:
            price = float(r.get("price"))
        except (TypeError, ValueError):
            continue
        cur = d[p]
        if cur is None:
            d[p] = r
            continue
        try:
            cur_price = float(cur.get("price"))
        except (TypeError, ValueError):
            d[p] = r
            continue
        if price < cur_price:
            d[p] = r
    return d


def _get_cached_min_price(cache_key: str):
    """从缓存读一件物品的最低价；未命中返回 None。"""
    global _price_query_cache_hits
    hit = _price_query_cache.get(cache_key)
    if hit is not None:
        _price_query_cache_hits += 1
    return hit


def _put_cached_min_price(cache_key: str, value):
    """写入缓存，超过上限时淘汰最早的条目（简易 FIFO）。"""
    if cache_key in _price_query_cache:
        _price_query_cache[cache_key] = value
        return
    if len(_price_query_cache) >= _price_query_cache_max:
        # 直接弹出最早插入的一条（Python dict 3.7+ 保持插入顺序）
        try:
            _price_query_cache.pop(next(iter(_price_query_cache)))
        except StopIteration:
            pass
    _price_query_cache[cache_key] = value


def _lookup_main_csv_row_by_output(hash_name: str, name_cn: str,
                                   wear_cn: str,
                                   hash_map: dict, name_wear_map: dict):
    """为一件产出皮肤找主 CSV 行（6 级匹配，命中即返回，保证 99%+ 命中率）。

    匹配优先级（自上而下，命中即返回）：
      1) hash_name 原始值完整命中（最常用 100% 路径）
      2) _canonical(hash_name) 命中 hash_map 中的规范化键 / 基础名键
      3) (name_cn, wear_cn) 原始 tuple 命中 name_wear_map
      4) (_canonical(name_cn), wear_cn) 命中 name_wear_map
      5) 退化：canonical(name_cn) 子串 + wear_cn 单条遍历
      6) 再退化：wear_cn 单条下按 market_hash_name canonical 子串匹配
    找不到返回 {}（空 dict）。
    """
    hn_raw = (hash_name or "").strip()
    nc_raw = (name_cn or "").strip()
    wc_raw = (wear_cn or "").strip()
    hn_c = _canonical(hn_raw)
    nc_c = _canonical(nc_raw)

    # 1) 精确 hash
    if hn_raw and hn_raw in hash_map:
        return hash_map[hn_raw]
    # 2) canonical hash / canonical base hash
    if hn_c and hn_c in hash_map:
        return hash_map[hn_c]
    # 3) (name_cn, wear_cn) 原始命中
    if nc_raw and wc_raw:
        row = name_wear_map.get((nc_raw, wc_raw))
        if row:
            return row
    # 4) canonical (name_cn, wear_cn) 命中
    if nc_c and wc_raw:
        row = name_wear_map.get((nc_c, wc_raw))
        if row:
            return row
    # 5) 退化：canonical 子串 + wear_cn（单条匹配才返回）
    if nc_c and wc_raw:
        cand = None
        for (n, w), r in name_wear_map.items():
            if w != wc_raw:
                continue
            nc = _canonical(n)
            if nc and (nc_c in nc or nc in nc_c):
                if cand is None:
                    cand = r
                else:
                    # 多条可能 -> 子串匹配取精确长度差最小
                    cand = None
                    break
        if cand:
            return cand
    # 6) 再退化：wear_cn 下按 hash_name canonical 与 base_canonical 子串
    if hn_c and wc_raw:
        cand = None
        for (n, w), r in name_wear_map.items():
            if w != wc_raw:
                continue
            base_c = _canonical((r.get("市场哈希名称") or "").strip())
            full_c = _canonical(build_c5_market_hash_name(
                (r.get("市场哈希名称") or "").strip(), w))
            if base_c and (hn_c == base_c or hn_c in base_c or base_c in hn_c):
                if cand is None:
                    cand = r
                else:
                    cand = None
                    break
            elif full_c and (hn_c == full_c or hn_c in full_c or full_c in hn_c):
                if cand is None:
                    cand = r
                else:
                    cand = None
                    break
        if cand:
            return cand
    return {}


def _material_lookup_meta(goods_name: str, skin_name: str = "",
                          wear_cn: str = "", hash_name: str = "") -> tuple:
    """补全材料的 收藏品/品质/基础market_hash/buff_goods_id/磨损中文名。

    :param wear_cn: 材料的磨损档位中文名（如 "破损不堪"）。若提供，优先返回
        该档位对应的 buff_goods_id；否则返回第一条匹配（通常是 FN 档）。
    :param hash_name: 材料的英文市场哈希名（如 "AUG | Syd Mead"），用于中文名
        不一致时通过 CSV「市场哈希名称」列兜底匹配。

    返回 (collection, quality, base_hash, buff_goods_id, wear_cn, min_f, max_f)
    查不到时对应位置为 "" 或 0。
    """
    rows = load_items_from_main_csv_full()
    key = (skin_name or goods_name or "").strip().lower()
    hash_key = (hash_name or "").strip().lower()
    # hash_name 可能带磨损后缀（如 "AUG | Syd Mead (Minimal Wear)"），去掉再匹配
    if hash_key:
        paren = hash_key.rfind("(")
        if paren > 0:
            hash_key_base = hash_key[:paren].strip()
        else:
            hash_key_base = hash_key
    else:
        hash_key_base = ""
    if not key and not hash_key_base:
        return "", "", "", "", "", 0.0, 1.0
    # 收集所有同名皮肤的行：匹配「皮肤名称」（中文）或「市场哈希名称」（英文 base）
    all_matched = []
    for row in rows:
        name = (row.get("皮肤名称") or "").strip().lower()
        hash_base = (row.get("市场哈希名称") or "").strip().lower()
        hit = False
        if name and key and (name in key or key in name):
            hit = True
        if hash_base and hash_key_base and (
                hash_base == hash_key_base
                or hash_base in hash_key_base
                or hash_key_base in hash_base):
            hit = True
        if hit:
            all_matched.append(row)
    if not all_matched:
        return "", "", "", "", "", 0.0, 1.0

    # 如果有磨损档位，优先匹配该档位的行
    matched = None
    if wear_cn:
        w = str(wear_cn).strip()
        for row in all_matched:
            row_wear = (row.get("磨损") or "").strip()
            if row_wear == w:
                matched = row
                break
    # 没指定档位或没找到该档位 → 用第一条（通常是 FN 档）。
    # 【档位安全 2026-09-11】指定了档位但 CSV 无该档行时，绝不能把第一条
    # （FN 档）的 buff_goods_id 返回给调用方 —— 否则 WW/BS 材料拿着 FN 的
    # goods_id 去查 Buff，磨损过滤后必然 0 条（DB 旧方案错档的根源）。
    # 元数据（收藏品/品质/哈希名）同皮肤跨档一致，可继续用第一条的；
    # 仅 goods_id 清空，调用方按"无 gid"降级处理。
    if not matched:
        matched = dict(all_matched[0])
        if wear_cn:
            matched["buff_goods_id"] = ""

    wear_range_raw = (matched.get("磨损区间") or "").replace("~", "").split()
    if len(wear_range_raw) >= 2:
        try:
            min_f = float(wear_range_raw[0])
            max_f = float(wear_range_raw[1])
        except ValueError:
            min_f, max_f = 0.0, 1.0
    else:
        min_f, max_f = 0.0, 1.0
    # CSV 常见"磨损区间=0~0.6"这种全档位默认值，用 matched 的中文磨损精确范围覆盖
    if (max_f - min_f) >= 0.45:
        off = _official_wear_range(matched.get("磨损") or "")
        if off:
            min_f, max_f = off
    return (
        (matched.get("收藏品名称") or "").strip(),
        (matched.get("品质") or "").strip(),
        (matched.get("市场哈希名称") or "").strip(),
        (matched.get("buff_goods_id") or "").strip(),
        (matched.get("磨损") or "").strip(),
        min_f, max_f,
    )


def _official_wear_range(wear_cn: str) -> tuple[float, float] | None:
    """中文磨损名 → CS 官方 5 档磨损范围（FN/MW/FT/WW/BS）。

    用于兜底：主 CSV 磨损区间写成默认的 0~0.6 / 0~1 宽范围时，
    使用 wear_grade 官方精确范围，避免 ECO StartSimulation 报 2001。
    """
    w = str(wear_cn).strip().lower()
    if not w:
        return None
    if ("崭新" in w) or (w == "factory new"):
        return 0.00, 0.07
    if ("略有" in w) or ("略磨" in w) or (w == "minimal wear"):
        return 0.07, 0.15
    if ("久经" in w) or (w == "field-tested"):
        return 0.15, 0.38
    if ("破损" in w) or (w == "well-worn"):
        return 0.38, 0.45
    if ("战痕" in w) or (w == "battle-scarred"):
        return 0.45, 1.00
    return None


# ---- 替换模式（低档探查）：磨损档位顺序 + 更低档查找 ----
# 规则：物品只允许"向上替换"（换成更低磨损档 = 品质更好），绝不向下。
_WEAR_TIER_ORDER = ["崭新出厂", "略有磨损", "久经沙场", "破损不堪", "战痕累累"]


def _wear_tier_index(wear_cn: str) -> int:
    """中文磨损档 → 档位序号（0=崭新 … 4=战痕）；无法识别返回 -1。"""
    w = str(wear_cn or "").strip()
    if not w:
        return -1
    for i, t in enumerate(_WEAR_TIER_ORDER):
        # 双向子串兼容（"略有磨损"/"略磨" 等简写）
        if t in w or w in t or (t == "略有磨损" and "略磨" in w):
            return i
    return -1


def _find_lower_tier_buff_rows(base_hash: str, current_wear_cn: str) -> list[dict]:
    """主 CSV 里找同基础名（市场哈希名称）、磨损档位严格更低的行。

    返回 [{wear_cn, buff_goods_id, tier}]（tier 为档位序号）。
    崭新出厂没有更低档、档位无法识别、基础名为空 → 返回空列表。
    """
    base = str(base_hash or "").strip().lower()
    if not base:
        return []
    cur_idx = _wear_tier_index(current_wear_cn)
    if cur_idx <= 0:
        return []
    rows = load_items_from_main_csv_full()
    out: list[dict] = []
    seen_tiers: set[int] = set()
    for r in rows:
        if (r.get("市场哈希名称") or "").strip().lower() != base:
            continue
        wcn = (r.get("磨损") or "").strip()
        tidx = _wear_tier_index(wcn)
        if tidx < 0 or tidx >= cur_idx or tidx in seen_tiers:
            continue
        gid = str(r.get("buff_goods_id") or "").strip()
        if not gid:
            continue
        seen_tiers.add(tidx)
        out.append({"wear_cn": wcn, "buff_goods_id": gid, "tier": tidx})
    return out


# ---- C2: 统一的"查价磨损边界"计算（材料 & 产出都走这个）----
# 优先级（完全对齐 GUI helper material_wear_min_max，用户要求）：
#   1. user_wear_min/user_wear_max（用户右键自定义的真实搜索边界；最高优先级）
#   2. 中文磨损等级 → 官方 5 档精确范围
#     - 材料侧：用 _wear_cn 查 _official_wear_range
#     - 产出侧：用 sk.wear_grade_cn 查 _official_wear_range
#   3. 主 CSV 皮肤自带磨损区间（一般 0~0.6/0~1，兜底）
#   4. 若该物品有确切 paint_wear / wf（单物品浮点数）：wear_max 夹紧为
#      min(当前 mx, paint_wear)，并保证 min<max（否则精确点 ±1e-6 兜底）
#      （用户原话："按填入的磨损作为最高磨损往前搜索"）
def _resolve_wear_bounds(mat: dict, *,
                         raw_min_f=None, raw_max_f=None,
                         wear_cn: str | None = None,
                         paint_wear: float | None = None) -> tuple[float, float]:
    """统一查价边界。

    Args:
        mat: 材料字典（_normalize_materials 输出、GUI 材料字典、主 CSV 行都可）。
             函数内部会自动读取：user_wear_min/max、_min_f/_max_f、
             wear_min/wear_max、paint_wear、磨损/wear_grade 等字段。
        raw_min_f: 优先使用的 min（供调用方显式传入，例如产出 case 想传 None 就传）
        raw_max_f: 优先使用的 max
        wear_cn: 优先使用的中文磨损等级名（例如产出 skeleton.wear_grade_cn）
        paint_wear: 优先使用的确切浮点数磨损值（例如 skeleton.wear_float）
    Returns:
        (wear_min_f, wear_max_f) 用于 price_fetcher 过滤；范围一定合法（0 ≤ a < b ≤ 1）。
    """
    mat = mat if isinstance(mat, dict) else {}

    # ---- Step 1: 显式传入优先（产出 case 常用：paint_wear/wear_cn 明确给）----
    a = None
    b = None
    if raw_min_f is not None:
        try: a = float(raw_min_f)
        except (TypeError, ValueError): a = None
    if raw_max_f is not None:
        try: b = float(raw_max_f)
        except (TypeError, ValueError): b = None

    # ---- Step 2: 用户自定义（GUI 右键 user_wear_*）----
    if a is None and "user_wear_min" in mat:
        try: a = float(mat["user_wear_min"])
        except (TypeError, ValueError): a = None
    if b is None and "user_wear_max" in mat:
        try: b = float(mat["user_wear_max"])
        except (TypeError, ValueError): b = None
    if a is None and "磨损_min" in mat:
        try: a = float(mat["磨损_min"])
        except (TypeError, ValueError): a = None
    if b is None and "磨损_max" in mat:
        try: b = float(mat["磨损_max"])
        except (TypeError, ValueError): b = None

    # ---- Step 3: 归一化字段拿 min/max（后处理引擎 normalized dict 常用 _min_f/_max_f；
    #           其它场景读 wear_min/wear_max/min_f/max_f/MinFloat/MaxFloat）----
    if a is None:
        for k in ("_min_f", "wear_min", "min_f", "MinFloat", "minFloat"):
            if mat.get(k) not in (None, ""):
                try:
                    a = float(mat[k])
                    break
                except (TypeError, ValueError):
                    continue
    if b is None:
        for k in ("_max_f", "wear_max", "max_f", "MaxFloat", "maxFloat"):
            if mat.get(k) not in (None, ""):
                try:
                    b = float(mat[k])
                    break
                except (TypeError, ValueError):
                    continue

    # ---- Step 4: wear_cn → 官方精确档 ----
    cn = str(wear_cn).strip() if isinstance(wear_cn, str) else ""
    if not cn:
        cn = str((mat.get("_wear_cn") or mat.get("磨损") or mat.get("wear_grade")
                  or mat.get("wear_name") or "")).strip()
    off = _official_wear_range(cn) if cn else None
    if off is not None:
        off_mn, off_mx = off
        # 若 a/b 原本就是主 CSV 宽范围（差值≥0.45），或压根没值，就直接用官方档
        a_none = a is None or not isinstance(a, (int, float))
        b_none = b is None or not isinstance(b, (int, float))
        if a_none or b_none or (abs(float(b) - float(a)) >= 0.45):
            a, b = off_mn, off_mx
        else:
            # 已经是正常小范围：保持用户/原值，但限制在官方档里（避免越界查价）
            a = max(float(a), off_mn)
            b = min(float(b), off_mx)

    # ---- Step 5: paint_wear（单物品确切浮点数）夹紧 wear_max（用户 C2 核心要求）
    pw_candidates = []
    if paint_wear is not None:
        pw_candidates.append(paint_wear)
    for k_pw in ("paint_wear", "PaintWear", "wear", "wear_float",
                 "wear_value_str", "WearValue"):
        if mat.get(k_pw) not in (None, ""):
            pw_candidates.append(mat.get(k_pw))
    pwf = None
    for v in pw_candidates:
        try:
            fv = float(v)
            if 0.0 <= fv <= 1.0:
                pwf = fv
                break
        except (TypeError, ValueError):
            continue
    # pwf=0 是 normalize 的"未填 paint_wear"默认值，不是真实磨损：
    # 必须完全跳过本分支（既不夹紧、也不做越档重推断）。
    # 【曾经的大坑】：越档保护 `if not (a <= pwf <= b)` 没排除 pwf=0 ——
    #   GUI 材料（无 paint_wear 字段）pwf=0.0 落在久经沙场 [0.15,0.38] 之外 →
    #   误判越档 → get_wear_grade(0.0)=崭新出厂 → 查询区间被偷换成 [0.0,0.07] →
    #   Buff/ECO 按错误磨损过滤 → 0 条命中 → 材料 Buff/ECO 价格大面积 None。
    if pwf is not None and pwf > 0:
        a = 0.0 if a is None else float(a)
        b = 1.0 if b is None else float(b)
        # 【越档保护】：产出侧偶尔会出现 "ECO 官方磨损等级(Exterior)=战痕累累"
        # 但 WearValue=0.3（实际是久经沙场档位值）这种不一致。此时如果继续
        # 用战痕累累 [0.45, 1.00] + paint_wear=0.3 夹紧 → b=min(1.0,0.3)=0.3 →
        # a(=0.45)>=b → 退化成单点 [0.3±1e-6] → 查询区间窄到搜不到任何在售。
        # 修复：若 pwf 落在 [a,b] 之外 → 按 pwf 实际值重新推断官方精确档覆盖，
        # 再把 b 夹紧到 pwf（用户 C2 核心要求：按填入的确切磨损值为上限往前搜）。
        if not (a <= pwf <= b):
            alt = _official_wear_range(get_wear_grade(pwf))
            if alt:
                a, b = alt
            # 否则保持 a/b 不变（可能是 GUI 自定义区间，不做破坏性越界自动改）
        # "按填入的磨损值 pwf 作为最高磨损往前搜索"
        # （pwf > 0 已在进入分支时保证，这里无需再判断）
        b = min(float(b), pwf)
        # 保持范围合法：夹紧后若 a >= b（通常是 paint_wear 极其接近档下限），
        # 把 a 拉回 [a=b-1e-6] 而不是单点 ±1e-6（单点搜索太窄，容易整条没结果）
        if not (a < b):
            # [官方档下限, pwf] 不越过 [0,1]：避免单点±1e-6 搜索窄到搜不到
            alt = _official_wear_range(get_wear_grade(pwf)) or (0.0, pwf)
            lo_cand = min(alt[0] if alt else 0.0, pwf - 1e-6)
            a = max(0.0, lo_cand)
            b = min(1.0, pwf)

    # ---- Step 6: 终局兜底（确保 a/b 永远合法，不给 price_fetcher 传 None）----
    a = float(a) if isinstance(a, (int, float)) else 0.0
    b = float(b) if isinstance(b, (int, float)) else 1.0
    a = max(0.0, min(a, 0.999999))
    b = max(a + 1e-9, min(1.0, b))

    # ---- Step 7: 官方档右边界按【左闭右开】处理 ----
    # CS 官方磨损档是左闭右开区间：wear 恰为 0.07/0.15/0.38/0.45 整点的物品
    # 属于【更高磨损】那一档（如 0.15 是久经沙场，不是略有磨损）。
    # 查询上限若包含边界点，会捞到更便宜的上一档物品污染本档最低价
    # （例：略磨 [0.07,0.15] 含 0.15 → 混入便宜的久经 0.15 物品）。
    # 例外：上限来自 paint_wear（该物品确切磨损值）夹紧时保持含端点——
    # 那是在找磨损恰好等于该值的具体物品本身。
    for _bd in (0.07, 0.15, 0.38, 0.45):
        if abs(b - _bd) < 1e-12:
            _from_pwf = (pwf is not None and pwf > 0
                         and abs(float(pwf) - _bd) < 1e-12)
            if not _from_pwf:
                b = max(a + 1e-9, b - 1e-6)
            break
    return a, b


def _normalize_materials(materials: list) -> list:
    """把各种形态的 10 件材料（dict / Material dataclass / 混合）统一成 dict。

    每个输出 dict 保证有这些字段：
        paint_wear (float)
        goods_name (str)
        hash_name  (str, 可选，但补齐优先)
        _skin_name (str)
        _collection (str)
        _quality (str)
        _base_hash (str, 主 CSV market_hash_name 基础名)
        _buff_goods_id (str)
        _wear_cn (str)
        _source (int): eco_web_api.SOURCE_STOCK(默认库存) / SOURCE_MARKET / SOURCE_CUSTOM
    """
    normalized = []
    for i, m in enumerate(materials):
        # 1) 从 Material dataclass 或 dict 抽公共字段
        if isinstance(m, Material):
            d = {
                "asset_id": m.asset_id,
                "goods_name": m.goods_name,
                "paint_wear": float(m.paint_wear or 0),
                "_skin_name": m.target_skin_name or "",
                "_collection": m.collection or "",
                "_quality": m.quality or "",
                "hash_name": "",
            }
        elif isinstance(m, dict):
            pw = m.get("paint_wear")
            try:
                pw_f = float(pw) if pw not in (None, "") else 0.0
            except (TypeError, ValueError):
                pw_f = 0.0
            # ======== HTTP 请求体模式（Worker EcoSimulationWorker 误传精简 api_materials）========
            # 特征：有 HashName（大写）/WearValue/MaterialSource/Sort；但没有 wear_grade / buff_goods_id / item_name
            if ("HashName" in m or "hashname" in m
                    or "WearValue" in m or "wearvalue" in m) \
                    and not (m.get("wear_grade") or m.get("item_name")
                             or m.get("buff_goods_id") or m.get("c5_market_hash_name")):
                hn_http = (m.get("HashName") or m.get("hashname") or m.get("hash_name") or "").strip()
                wv_http = m.get("WearValue") or m.get("wearvalue") or m.get("paint_wear") or 0
                src_http = m.get("MaterialSource") or m.get("materialsource") or m.get("_source") or eco_web_api.SOURCE_STOCK
                try:
                    pw_f = float(wv_http)
                except (TypeError, ValueError):
                    pw_f = 0.0
                # 用主 CSV 反查：优先按 c5_market_hash_name；ECO 返回 HashName 是英文名，要和主 CSV build_c5_market_hash_name 的「英文名 + 英文磨损」匹配，或和「中文名 + 英文磨损」匹配（因为 build_c5_market_hash_name 把 market_hash_base（即 CSV 的"市场哈希名称"字段，英文 base）+ 中文磨损翻译成英文磨损），但 CSV 的皮肤名称是中文。这里先按 build_c5_market_hash_name 结果匹配，匹配不到再按"市场哈希名称"模糊。
                rows_all = load_items_from_main_csv_full()
                looked = None
                if hn_http:
                    hn_http_low = hn_http.lower()
                    # 1) build_c5_market_hash_name(CSV 皮肤名称, CSV 磨损) -> 中文名+英文磨损？不，看函数实现：
                    #    build_c5_market_hash_name(market_hash_base: str, wear_cn: str) -> base + "(" + WEAR_CN_TO_EN[wear_cn] + ")"
                    #    其中 market_hash_base 应该是英文 base（CSV 的"市场哈希名称"列），不是中文名。
                    # 所以规范化后 hash_name 应该和 HTTP 请求体 HashName（Safety Net (Minimal Wear) 等）完全一致。
                    for r in rows_all:
                        got = build_c5_market_hash_name(
                            r.get("市场哈希名称") or r.get("皮肤名称") or "",
                            r.get("磨损") or "")
                        if got and got.lower() == hn_http_low:
                            looked = r; break
                    if looked is None:
                        # 2) 退而求其次：HashName 的英文 base（去掉 ' (FN/MW/...)' 尾）等于 CSV"市场哈希名称"
                        paren = hn_http.rfind("(")
                        base_http = hn_http[:paren].strip() if paren > 0 else hn_http
                        for r in rows_all:
                            if (r.get("市场哈希名称") or "").strip().lower() == base_http.lower():
                                looked = r; break
                if looked is not None:
                    # 把 looked 的元数据注入（不丢失 HashName/WearValue）
                    d = dict(m)
                    d["paint_wear"] = pw_f
                    d["hash_name"] = hn_http
                    d["goods_name"] = (looked.get("皮肤名称") or "").strip()
                    d["_skin_name"] = d["goods_name"]
                    d["_collection"] = (looked.get("收藏品名称") or "").strip()
                    d["_quality"]    = (looked.get("品质") or "").strip()
                    d["_base_hash"]  = (looked.get("市场哈希名称") or "").strip()
                    d["buff_goods_id"] = (looked.get("buff_goods_id") or "").strip()
                    d["c5_market_hash_name"] = hn_http
                    d["c5_app_id"] = str(C5_APP_ID)
                    d["wear_grade"] = (looked.get("磨损") or "").strip()
                    d["item_name"]  = d["goods_name"]
                    wr_raw = (looked.get("磨损区间") or "").replace("~", "").split()
                    if len(wr_raw) >= 2:
                        try:
                            d["wear_min"] = float(wr_raw[0]); d["wear_max"] = float(wr_raw[1])
                        except ValueError:
                            pass
                    # 官方磨损兜底（0~0.6 / 0~1 宽范围 → 精确档）
                    if (float(d.get("wear_max") or 1) - float(d.get("wear_min") or 0)) >= 0.45:
                        off = _official_wear_range(d.get("wear_grade") or "")
                        if off: d["wear_min"], d["wear_max"] = off
                    d["_min_f"] = float(d.get("wear_min") or 0)
                    d["_max_f"] = float(d.get("wear_max") or 1)
                    d["_wear_cn"] = d.get("wear_grade") or ""
                    d["_source"] = src_http if isinstance(src_http, int) else eco_web_api.SOURCE_STOCK
                    d["asset_id"] = m.get("asset_id") or ""
                else:
                    # lookup 失败：最小兜底
                    d = dict(m)
                    d["paint_wear"] = pw_f
                    d["hash_name"] = hn_http
                    d["goods_name"] = hn_http
                    d["_skin_name"] = hn_http
                    d["_collection"] = m.get("_collection") or ""
                    d["_quality"]    = m.get("_quality") or ""
                    d["_base_hash"]  = ""
                    d["_wear_cn"]    = get_wear_grade(pw_f) if pw_f else ""
                    off = _official_wear_range(d["_wear_cn"]) if d["_wear_cn"] else None
                    d["_min_f"] = off[0] if off else 0.0
                    d["_max_f"] = off[1] if off else 1.0
                    d["wear_min"] = d["_min_f"]; d["wear_max"] = d["_max_f"]
                    d["_source"]  = src_http if isinstance(src_http, int) else eco_web_api.SOURCE_STOCK
                    d["asset_id"] = m.get("asset_id") or ""
                # HTTP 模式走专属分支，跳过下面常规 dict 段逻辑（会再次 setdefault 造成污染）
                normalized.append(_finish_normalize_one(d))
                continue
            pw = m.get("paint_wear")
            try:
                pw_f = float(pw) if pw not in (None, "") else 0.0
            except (TypeError, ValueError):
                pw_f = 0.0
            d = dict(m)  # 拷贝保留所有原字段
            d["paint_wear"] = pw_f
            # 兼容 material_query_config 返回的 item_name / wear_grade 字段
            fallback_name = (
                m.get("hash_name")
                or m.get("item_name")
                or m.get("skin_name")
                or m.get("target_skin_name")
                or ""
            )
            d.setdefault("goods_name", fallback_name)
            d.setdefault("asset_id", m.get("asset_id") or "")
            d.setdefault(
                "_skin_name",
                (m.get("target_skin_name")
                 or m.get("item_name")
                 or m.get("skin_name")
                 or ""),
            )
            d.setdefault("_collection", m.get("_collection") or m.get("collection") or "")
            d.setdefault("_quality", m.get("_quality") or m.get("quality") or "")
            # c5_market_hash_name 是 material_query_config 独有的字段 → 视作 hash_name 兜底
            d.setdefault(
                "hash_name",
                m.get("hash_name") or m.get("c5_market_hash_name") or "",
            )
            # wear_grade 是中文磨损，给 _wear_cn 兜底（lookup 命中时会被覆盖，但 lookup 查不到时避免全空）
            if m.get("wear_grade") and not d.get("_wear_cn"):
                d["_wear_cn"] = m["wear_grade"]
        else:
            raise TypeError(
                f"simulate_outputs materials[{i}] 必须是 dict 或 Material，"
                f"实际 {type(m).__name__}")

        # 2) 缺失 metadata 时反查主 CSV 补全
        d = _finish_normalize_one(d)
        normalized.append(d)
    return normalized


def _finish_normalize_one(d: dict) -> dict:
    """normalize dict 的后半段：metadata 补齐 + 官方磨损兜底 + 缺 hash_name 拼装 + 来源默认。

    被两条路径复用：①常规 dict 分支末尾；②HTTP 请求体分支末尾。
    """
    need_lookup = not (d.get("_collection") and d.get("_quality") and
                       str(d.get("_buff_goods_id", "") or "").strip() != "")
    # 先提取已有磨损档位，传给 lookup 以便返回对应档位的 buff_goods_id
    existing_wear = (
        d.get("_wear_cn")
        or d.get("wear_grade")
        or d.get("磨损")
        or ""
    )
    if need_lookup:
        col, q, base_hash, gid, wear_cn, mnf, mxf = _material_lookup_meta(
            d.get("goods_name") or "",
            d.get("_skin_name") or "",
            wear_cn=existing_wear,
            hash_name=d.get("hash_name") or d.get("c5_market_hash_name") or "")
        if not d.get("_collection"):
            d["_collection"] = col
        if not d.get("_quality"):
            d["_quality"] = q
        if not d.get("_base_hash"):
            d["_base_hash"] = base_hash
        # _base_hash / _buff_goods_id：
        #   - wear_cn 有值时，_material_lookup_meta 已返回对应档位的 goods_id（gid），
        #     优先用它，因为材料自带的 buff_goods_id（own_gid）可能来自错档匹配
        #     （例如 by_cn.setdefault 只保留第一条=FN 档）。
        #   - wear_cn 为空或 lookup 没找到该档位时，回退到 own_gid。
        own_gid = (d.get("buff_goods_id")
                   if isinstance(d.get("buff_goods_id"), str)
                   else "")
        if existing_wear and str(gid or "").strip():
            d["_buff_goods_id"] = str(gid).strip()
        else:
            d["_buff_goods_id"] = str(own_gid).strip() or str(gid or "").strip()
        # 关键：已有 wear_grade（中文磨损，来自 material_query_config）不要被 lookup 的
        # "搜到的第一条 = 崭新出厂" 覆盖；user_wear / wear_min/wear_max 也保留。
        d["_wear_cn"] = existing_wear or wear_cn
        # 只在 dict 完全没自定义 wear_min/wear_max 时用 lookup 值
        has_own = any(k in d for k in ("wear_min", "wear_max",
                                       "user_wear_min", "user_wear_max",
                                       "磨损_min", "磨损_max"))
        if not has_own:
            d["_min_f"] = mnf
            d["_max_f"] = mxf
        else:
            d.setdefault("_min_f", 0.0)
            d.setdefault("_max_f", 1.0)
    else:
        # 已补齐集合+品质，但 wear 基础信息可能仍需填
        d.setdefault("_base_hash", "")
        existing_wear = (
            d.get("_wear_cn")
            or d.get("wear_grade")
            or d.get("磨损")
            or ""
        )
        # 【goods_id 档位校验】即使 _buff_goods_id 已有值，也要验证它是否匹配
        # 当前磨损档位。库存匹配/CSV 导入可能写入错档的 goods_id（全是 FN）。
        # 有 wear_cn 时重新 lookup 拿对应档位的 gid 覆盖。
        if existing_wear:
            d["_wear_cn"] = existing_wear
            _col, _q, _bh, _gid, _wc, _mnf, _mxf = _material_lookup_meta(
                d.get("goods_name") or "",
                d.get("_skin_name") or "",
                wear_cn=existing_wear,
                hash_name=d.get("hash_name") or d.get("c5_market_hash_name") or "")
            if str(_gid or "").strip():
                d["_buff_goods_id"] = str(_gid).strip()
            if not d.get("_base_hash"):
                d["_base_hash"] = _bh
        else:
            d.setdefault("_wear_cn", "")
            if not str(d.get("_buff_goods_id") or "").strip():
                d.setdefault("_buff_goods_id", "")
        d.setdefault("_min_f", 0.0)
        d.setdefault("_max_f", 1.0)

    # 2b) 官方磨损范围兜底：如果 _min_f/_max_f 差值 >= 0.45，判定 CSV 默认宽值（'0~0.6'/'0~1'），
    #     用 wear_grade 对应的官方范围覆盖；否则 StartSimulation 会因"磨损值越界"报 ResultCode=2001。
    try:
        span = float(d.get("_max_f", 1.0)) - float(d.get("_min_f", 0.0))
    except (TypeError, ValueError):
        span = 1.0
    if span >= 0.45:
        wear_cn_fixed = d.get("_wear_cn") or d.get("wear_grade") or ""
        off = _official_wear_range(wear_cn_fixed)
        if off:
            d["_min_f"] = off[0]
            d["_max_f"] = off[1]
    # 同理：用户 GUI 传进来的 wear_min/wear_max 如果也是 CSV 默认宽范围，也按官方磨损校准。
    for prefix in ("wear_", "磨损_"):
        mink = f"{prefix}min"; maxk = f"{prefix}max"
        if mink in d and maxk in d:
            try:
                s = float(d[maxk]) - float(d[mink])
            except (TypeError, ValueError):
                continue
            if s >= 0.45:
                wear_cn_fixed = (d.get("_wear_cn") or d.get("wear_grade") or "")
                off = _official_wear_range(wear_cn_fixed)
                if off:
                    d[mink] = off[0]; d[maxk] = off[1]

    # 3) 若 hash_name 缺失，用基础名 + 中文磨损（若 paint_wear 可推导等级）拼
    if not d.get("hash_name") and d.get("_base_hash"):
        wear_cn = d.get("_wear_cn") or ""
        if not wear_cn:
            wear_cn = get_wear_grade(d.get("paint_wear") or 0)
            d["_wear_cn"] = wear_cn
        full = build_c5_market_hash_name(d["_base_hash"], wear_cn)
        if full:
            d["hash_name"] = full

    # 4) MaterialSource：默认按「来源字段」判；一般库存物品默认 2，带 _price=市场价格 视为 1
    if d.get("_source") not in (1, 2, 3):
        if d.get("_price"):
            d["_source"] = eco_web_api.SOURCE_MARKET
        else:
            d["_source"] = eco_web_api.SOURCE_STOCK
    return d


def _build_api_materials(normalized_materials: list) -> list:
    """把 _normalize_materials 的 10 件结果 → StartSimulation Materials 数组。

    每个元素：{HashName, MaterialSource, WearValue(str), Sort(0..9)}
    """
    api_mats = []
    for idx, nm in enumerate(normalized_materials):
        hn = (nm.get("hash_name") or "").strip()
        if not hn:
            # 最差兜底：用基础名 + 中文磨损重新拼一次
            base = nm.get("_base_hash") or ""
            wear_cn = nm.get("_wear_cn") or get_wear_grade(nm.get("paint_wear", 0))
            hn = build_c5_market_hash_name(base, wear_cn)
        if not hn:
            raise RuntimeError(
                f"材料[{idx}] 无法生成 ECO StartSimulation 所需的 HashName"
                f"（goods_name={nm.get('goods_name')!r} "
                f"skin_name={nm.get('_skin_name')!r}）"
                f"请确保主 CSV 有该皮肤的 市场哈希名称/磨损 字段。")
        # WearValue：优先用材料真实 paint_wear（库存物品）；否则用 wear 范围中值。
        # 注意：不允许用 0 越界（比如 MW 档位下限 0.07 → 传 0 会 ResultCode=2001 材料磨损值错误）
        try:
            paint_f = float(nm.get("paint_wear") or 0)
        except (TypeError, ValueError):
            paint_f = 0.0
        if paint_f > 0:
            wear_value = paint_f
        else:
            try:
                mn = float(nm.get("wear_min") if nm.get("wear_min") not in (None, "") else (nm.get("_min_f") or 0))
                mx = float(nm.get("wear_max") if nm.get("wear_max") not in (None, "") else (nm.get("_max_f") or 1))
            except (TypeError, ValueError):
                mn, mx = 0.0, 1.0
            if 0.0 <= mn < mx <= 1.0:
                wear_value = (mn + mx) / 2.0
            else:
                # 最后兜底：用 wear_grade 的官方中值
                off = _official_wear_range(nm.get("_wear_cn") or nm.get("wear_grade") or "")
                wear_value = (off[0] + off[1]) / 2.0 if off else 0.5
        # 确保不越界（浮点）
        wear_value = max(0.0, min(1.0, float(wear_value)))
        wv = f"{wear_value:.8f}"
        src = int(nm.get("_source") or eco_web_api.SOURCE_STOCK)
        api_mats.append({
            "HashName": hn,
            "MaterialSource": src,
            "WearValue": wv,
            "Sort": idx,
        })
    return api_mats


def _query_min_price(item_config: dict, wear_min=None, wear_max=None,
                     platforms=None, cache_buster: str = "") -> tuple:
    """查一件物品的最低价（优先缓存，未命中则调 price_fetcher.query_all）。

    Returns:
        (min_price_or_None, best_row_dict_or_None, errors_dict, rows_by_platform)
          rows_by_platform = {"buff": row|None, "c5": row|None, "eco": row|None}
          每个平台取该平台返回的最低价条目（用于 GUI 明细表格显示分平台价格）。
    """
    buff_gid = str(item_config.get("buff_goods_id") or "")
    c5_name = (item_config.get("c5_market_hash_name") or "").strip()
    c5_app_id = item_config.get("c5_app_id") or str(C5_APP_ID)
    cache_key = f"{buff_gid}#{c5_name}#{wear_min}#{wear_max}#" \
                f"{sorted(platforms or [])}#{cache_buster}"
    cached = _get_cached_min_price(cache_key)
    if cached is not None and len(cached) >= 4:
        return cached[0], cached[1], cached[2], cached[3]
    if cached is not None and len(cached) == 3:
        # 兼容旧缓存格式：(min, best, errors) → 分平台置空
        return cached[0], cached[1], cached[2], _rows_by_platform([])

    rows, errors = price_fetcher.query_all(
        {
            "buff_goods_id": buff_gid,
            "c5_market_hash_name": c5_name,
            "c5_app_id": str(c5_app_id),
        },
        wear_min=wear_min,
        wear_max=wear_max,
        platforms=platforms,
    )
    best = min(rows, key=lambda r: r["price"]) if rows else None
    min_price = best["price"] if best else None
    by_platform = _rows_by_platform(rows)
    _put_cached_min_price(cache_key, (min_price, best, errors, by_platform))
    return min_price, best, errors, by_platform


def _calc_keep_rate(outputs: list, cost: float) -> float:
    """[统一算法] 保本率 = Σ(产出最低售价 ≥ 总成本 的概率) × 100%。

    规则（2026-09-03 明确，与 EV 必须共用同一套价格/磨损，否则虚高）：
      - price: 统一用"三平台实时最低价（在 wear_min/wear_max 夹紧区间内）"
      - prob: 官方 OutputProbability（原始值，不是 percent）
      - 找不到 price 的条目（=0 或 None）不计入"保本"一方
      - cost ≤ 0 时直接 0%（避免除零或无意义数字）

    outputs[i] 兼容两种结构：
      - output_details 风格：含 "probability"/"prob"/"min_price"/"price"/"count"
      - groups[].outputs 风格：同上；以及真实对比模式的 count/prob。

    Returns:
        百分比数值（0.0 ~ 100.0）。例如 40.23。
    """
    if not isinstance(outputs, list) or not outputs:
        return 0.0
    if not isinstance(cost, (int, float)) or cost <= 0:
        return 0.0
    try:
        cost_f = float(cost)
    except (TypeError, ValueError):
        return 0.0
    keep = 0.0
    total_weight = 0.0
    for o in outputs:
        if not isinstance(o, dict):
            continue
        # 价格读取：min_price / price 任一存在（None/0 视作"价格缺失，不保"）
        price = None
        for key in ("min_price", "price"):
            try:
                v = o.get(key)
                if v is not None and v != "":
                    fv = float(v)
                    if fv > 0:
                        price = fv
                        break
            except (TypeError, ValueError):
                continue
        # 权重：count > 0 优先（真实执行次数），否则 prob/probability（官方概率）
        weight = None
        try:
            c = o.get("count")
            if c is not None and int(c) > 0:
                weight = float(int(c))
        except (TypeError, ValueError):
            pass
        if weight is None:
            for key in ("probability", "prob"):
                try:
                    v = o.get(key)
                    if v is not None and v != "":
                        fv = float(v)
                        if fv > 0:
                            weight = fv
                            break
                except (TypeError, ValueError):
                    continue
        if weight is None or weight <= 0:
            continue
        total_weight += weight
        if price is not None and price >= cost_f:
            keep += weight
    if total_weight <= 0:
        return 0.0
    # 归一化：真实对比模式 count 权重可能总和 > 1（需要除以 total）
    #         ECO StartSimulation 官方概率总和≈1（也归一下避免 1.0001 异常）
    ratio = keep / total_weight
    if ratio < 0.0:
        ratio = 0.0
    if ratio > 1.0:
        ratio = 1.0
    return ratio * 100.0


def _materials_cost_via_price_fetcher(normalized_materials: list,
                                      platforms=None,
                                      probe_lower_tiers: bool = False) -> tuple:
    """对 10 件材料并行实时查价，求和得到成本。

    Args:
        normalized_materials: _normalize_materials 结果
        platforms: price_fetcher.query_all 的 platforms 参数
        probe_lower_tiers: 替换模式 —— 主查询全部完成后，串行逐个探查每件材料
            更低磨损档（同基础名）的 Buff 全局最低价；若有低档比当前档最低价
            更便宜，记入 detail["lower_tier_cheaper"]（GUI 标红提示，仅展示
            不改成本/不改模拟输入）。低档探查只查 Buff，每档 1 页
            （price.asc 第 1 条即该档全局最低价），排队串行执行不并发。

    Returns:
        (total_cost, price_per_material_list, detail_per_material_list)
        price_per_material_list[i] = 该材料的最低实时售价（元），无结果=0.0
        detail_per_material_list[i] = {
            "item_config": {...},
            "wear_min": float|None, "wear_max": float|None,
            "buff_price": float|None, "buff_row": dict|None,
            "c5_price":   float|None, "c5_row":   dict|None,
            "eco_price":  float|None, "eco_row":  dict|None,
            "min_price":  float,
            "best_platform": str,
            "errors": dict,
        }
    """
    def _platform_price(row):
        try:
            return float(row.get("price")) if row else None
        except (TypeError, ValueError):
            return None

    # 已有 _price 的不直接跳过——用户在 GUI 表格里要看三平台各自的价（buff/c5/eco），
    #   必须走 live 查询拿 by_platform 明细。_price 只做两个用途：
    #     1) 初始化价格列表（最后在 live 查询结果汇总后再覆盖/兜底）
    #     2) 如果 live 查询该条 min_price=0（平台报错/无结果），回退用 _price 计成本
    #   这样即使之前「开始寻找材料」GUI 面板给过 price_buff，ECO 汰换结果表格里
    #   仍能看到 buff/c5/eco 三平台各自最新最低价（而不是三列全 None）。
    items_to_query = []  # [(idx, item_config, wear_min, wear_max)]
    price_list = [0.0] * len(normalized_materials)
    detail_list = [None] * len(normalized_materials)
    # 记录有 _price 预价的 idx，live 查价后如果 min=0 就用这个兜底
    fallback_price_by_idx: dict[int, float] = {}
    for i, nm in enumerate(normalized_materials):
        p = nm.get("_price")
        try:
            pv = float(p) if p not in (None, "") else None
        except (TypeError, ValueError):
            pv = None
        if pv and pv > 0:
            fallback_price_by_idx[i] = float(pv)
            # 仍然放入 items_to_query 做 live 查询，不跳过
        # 构造 item_config：buff_goods_id + c5_market_hash_name(hash_name 或补全)
        c5_hash = (nm.get("hash_name") or "").strip()
        if not c5_hash:
            base = (nm.get("_base_hash") or "").strip()
            wear_cn = nm.get("_wear_cn") or get_wear_grade(nm.get("paint_wear", 0))
            c5_hash = build_c5_market_hash_name(base, wear_cn)
        ic = {
            "buff_goods_id": str(nm.get("_buff_goods_id") or ""),
            "c5_market_hash_name": c5_hash,
            "c5_app_id": str(C5_APP_ID),
        }
        # C2: 用统一 helper 计算查价磨损边界（完全对齐 GUI material_wear_min_max）：
        #   user_wear_* 优先 → 中文磨损档官方精确 → paint_wear/wear 夹紧上限 → 兜底
        #   不再用手写的 `nm.get("_min_f") or None`（会错误把 FN 档 wear_min=0 判空）。
        wmin, wmax = _resolve_wear_bounds(nm)
        items_to_query.append((i, ic, wmin, wmax))

    if not items_to_query:
        return float(sum(price_list)), price_list, detail_list

    # 并发查询材料价格（最多 10 个，线程池 10 线程即可）
    with ThreadPoolExecutor(max_workers=min(10, max(1, len(items_to_query)))) as pool:
        futs = {
            pool.submit(_query_min_price, ic, wmin, wmax, platforms): (idx, ic, wmin, wmax)
            for idx, ic, wmin, wmax in items_to_query
        }
        for fut in as_completed(futs):
            idx, ic, wmin, wmax = futs[fut]
            try:
                mp, best, errors, by_plat = fut.result()
            except Exception as e:
                logger.debug(
                    "材料[%d]实时查价失败，按 0 计成本: %s", idx, e)
                price_list[idx] = 0.0
                detail_list[idx] = {
                    "index": idx,
                    "skin_name": (normalized_materials[idx].get("_skin_name")
                                  or normalized_materials[idx].get("goods_name")
                                  or normalized_materials[idx].get("hash_name") or "").strip(),
                    "hash_name": (normalized_materials[idx].get("hash_name") or "").strip(),
                    "item_config": ic,
                    "wear_min": wmin, "wear_max": wmax,
                    "buff_price": None, "buff_row": None,
                    "c5_price": None,   "c5_row": None,
                    "eco_price": None,  "eco_row": None,
                    "min_price": 0.0,
                    "best_platform": "",
                    "errors": {"__all__": str(e)},
                }
                continue
            mp = float(mp) if mp else 0.0
            # 兜底：若 live 查询这条 min_price=0（平台无结果/报错/限流），
            #   但 GUI 预查过 _price（fallback_price_by_idx 里有值），就用预价兜底计成本
            #   （不覆盖分平台明细，明细仍保留 None + errors，便于用户看到问题）
            if mp <= 0 and idx in fallback_price_by_idx:
                fb = float(fallback_price_by_idx[idx])
                if fb > 0:
                    mp = fb
                    errors = dict(errors or {})
                    errors.setdefault(
                        "fallback_from_gui_price",
                        f"Live 查无有效价，已回退 GUI 预价￥{fb:.2f}计成本")
            price_list[idx] = mp
            buff_row = by_plat.get("buff") if isinstance(by_plat, dict) else None
            c5_row   = by_plat.get("c5")   if isinstance(by_plat, dict) else None
            eco_row  = by_plat.get("eco")  if isinstance(by_plat, dict) else None
            detail_list[idx] = {
                "index": idx,
                "skin_name": (normalized_materials[idx].get("_skin_name")
                              or normalized_materials[idx].get("goods_name")
                              or normalized_materials[idx].get("hash_name") or "").strip(),
                "hash_name": (normalized_materials[idx].get("hash_name") or "").strip(),
                "item_config": ic,
                "wear_min": wmin, "wear_max": wmax,
                "buff_price": _platform_price(buff_row), "buff_row": buff_row,
                "c5_price":   _platform_price(c5_row),   "c5_row":   c5_row,
                "eco_price":  _platform_price(eco_row),  "eco_row":  eco_row,
                "min_price": mp,
                "best_platform": (best.get("platform") or "") if best else (
                    "fixed_fallback" if idx in fallback_price_by_idx and mp > 0 and
                    not (isinstance(best, dict) and best.get("platform")) else ""),
                "best_row": best,
                "errors": errors or {},
            }

    # ============================================================
    # 替换模式（低档探查）：主查询全部完成后才执行，**单线程排队串行**，
    # 不与主查询并发（避免同时打开多个"网页"引发 429）。
    #   - 每个低档 goods_id 只查一次（跨材料去重；10 件同款 → 只 2 个请求）；
    #   - 每档 1 页：query_buff 带 [0,1] 全区间磨损过滤 → 首条必命中 →
    #     早停机制保证只发 1 个请求，price.asc 第 1 条 = 该档全局最低价；
    #   - 比当前档 min_price（三平台最低）便宜才记录，仅展示不改成本。
    # ============================================================
    if probe_lower_tiers:
        _probe_cache: dict[str, float | None] = {}   # buff_goods_id -> 该档全局最低价
        lower_rows_by_idx: dict[int, list[dict]] = {}
        probe_gids: list[str] = []                   # 保持发现顺序，串行执行
        for i, nm in enumerate(normalized_materials):
            cur_wear = (nm.get("_wear_cn") or nm.get("wear_grade")
                        or nm.get("磨损") or "")
            lowers = _find_lower_tier_buff_rows(
                nm.get("_base_hash") or nm.get("hash_name") or "", cur_wear)
            if not lowers:
                continue
            lower_rows_by_idx[i] = lowers
            for lr in lowers:
                gid = lr["buff_goods_id"]
                if gid not in _probe_cache and gid not in probe_gids:
                    probe_gids.append(gid)
        # 串行逐个探查（全局节流 gate 仍会保证 ≥Buff 间隔，如 5.1s）
        for gid in probe_gids:
            try:
                probe_rows = price_fetcher.query_buff(
                    gid, wear_min=0.0, wear_max=1.0)
                _prices = [float(r.get("price"))
                           for r in (probe_rows or [])
                           if r.get("price") not in (None, "")]
                _probe_cache[gid] = min(_prices) if _prices else None
            except Exception as e:
                logger.debug("替换模式低档探查 buff_goods_id=%s 失败: %s", gid, e)
                _probe_cache[gid] = None
        # 标注：每件材料取"价格 < 当前档最低价"里最便宜的低档
        for i, nm in enumerate(normalized_materials):
            lowers = lower_rows_by_idx.get(i)
            if not lowers:
                continue
            d = detail_list[i]
            if not isinstance(d, dict):
                continue
            try:
                mp = float(d.get("min_price") or 0)
            except (TypeError, ValueError):
                mp = 0.0
            if mp <= 0:
                continue   # 当前档本身无价，比较无意义
            skin_name = (nm.get("_skin_name") or nm.get("goods_name")
                         or nm.get("hash_name") or "").strip()
            best_lower = None
            for lr in lowers:
                p = _probe_cache.get(lr["buff_goods_id"])
                if p is None or float(p) >= mp:
                    continue
                if best_lower is None or float(p) < best_lower["price"]:
                    best_lower = {
                        "skin_name": skin_name,
                        "wear_cn": lr["wear_cn"],
                        "price": float(p),
                        "buff_goods_id": lr["buff_goods_id"],
                    }
            if best_lower is not None:
                d["lower_tier_cheaper"] = best_lower

    return float(sum(price_list)), price_list, detail_list


def _match_inventory_to_collection(inventory, main_index):
    """将库存物品映射到主 CSV 的 (collection, quality)。

    返回 dict: {(collection, quality, is_stattrak): [Material, ...]}

    匹配策略：通过 hash_name（库存的 HashName）匹配主 CSV 的 market_hash_name。
    若主 CSV 无 market_hash_name，则用皮肤名称子串匹配 hash_name。
    """
    ensure_inventory_table()
    # 构造 hash_name -> main_csv_row 反向索引
    hash_to_row = {}
    name_to_rows = defaultdict(list)
    for (collection, quality, is_stattrak), items in main_index.items():
        for item in items:
            mhn = item.get("market_hash_name", "")
            if mhn:
                hash_to_row[mhn.lower()] = (collection, quality, is_stattrak, item)
            name_to_row_key = item["name"].lower()
            name_to_rows[name_to_row_key].append(
                (collection, quality, is_stattrak, item))

    result = defaultdict(list)
    for inv in inventory:
        hash_name = (inv.get("hash_name") or "").strip()
        goods_name = (inv.get("goods_name") or "").strip()
        paint_wear = inv.get("paint_wear") or 0.0
        asset_id = inv.get("asset_id") or ""

        # 优先用 hash_name 精确匹配
        matched = None
        if hash_name:
            matched = hash_to_row.get(hash_name.lower())
        # 退化：用 goods_name 子串匹配皮肤名
        if not matched and goods_name:
            for skin_name_key, rows in name_to_rows.items():
                if skin_name_key and skin_name_key in goods_name.lower():
                    matched = rows[0]
                    break
        if not matched:
            continue

        collection, quality, is_stattrak, item = matched
        material = Material(
            asset_id=asset_id,
            goods_name=goods_name or hash_name,
            paint_wear=float(paint_wear),
            target_skin_name=item["name"],
            collection=collection,
            quality=quality,
        )
        result[(collection, quality, is_stattrak)].append(material)
    return result


# ============================================================
# 汰换方案搜索
# ============================================================
def calculate_output_float(materials, target_min_f: float,
                            target_max_f: float) -> float:
    """计算产出磨损 = avg(材料实际磨损) × (max-min) + min。

    materials 支持两种数据形式：
    - Material dataclass（含 paint_wear 属性）
    - dict（含 "paint_wear" 键）
    """
    def _wear(m):
        if isinstance(m, dict):
            return float(m.get("paint_wear") or 0)
        return float(m.paint_wear or 0)

    avg_input = sum(_wear(m) for m in materials) / 10.0
    return avg_input * (target_max_f - target_min_f) + target_min_f


def query_skin_wear_range(target_name: str):
    """按目标物品名称从主 CSV 查询皮肤磨损区间。

    从主 CSV 中匹配名称包含关键字（子串匹配，忽略大小写）的皮肤，
    返回匹配到的皮肤列表（含 min_f/max_f/品质/磨损等级/收藏品/售价）。

    Args:
        target_name: 目标物品名称关键字（如「混沌点阵」/「AK-47 | 混沌点阵」）

    Returns:
        list[dict]: 匹配皮肤 [{name, min_f, max_f, wear, quality, collection,
                               price_buff, market_hash_name}, ...]
    """
    if not target_name or not target_name.strip():
        return []
    keyword = target_name.strip().lower()
    rows = load_items_from_main_csv_full()
    result = []
    seen = set()
    for row in rows:
        skin_name = (row.get("皮肤名称") or "").strip()
        collection = (row.get("收藏品名称") or "").strip()
        quality = (row.get("品质") or "").strip()
        wear_range_raw = (row.get("磨损区间") or "").replace("~", "").split()
        if len(wear_range_raw) < 2:
            continue
        try:
            min_f = float(wear_range_raw[0])
            max_f = float(wear_range_raw[1])
        except ValueError:
            continue
        if keyword in skin_name.lower() or keyword in collection.lower():
            # 去重：同一 皮肤+磨损区间 只保留一次
            key = (collection, skin_name, min_f, max_f)
            if key in seen:
                continue
            seen.add(key)
            if not skin_name or not quality:
                continue
            result.append({
                "name": skin_name,
                "collection": collection,
                "quality": quality,
                "min_f": min_f,
                "max_f": max_f,
                "wear": (row.get("磨损") or "").strip(),
                "price_buff": (row.get("price_buff") or "0"),
                "market_hash_name": (row.get("市场哈希名称") or "").strip(),
            })
    return result


def simulate_outputs_local(materials: list, stock_price_map: dict = None) -> dict:
    """[v1 本地估算兜底版本] ECO 风格的汰换模拟。

    纯本地估算：n/10 × 1/k 分摊概率 + 线性磨损公式插值，价格用主 CSV price_buff。
    新版 simulate_outputs 在 ECO StartSimulation 调用失败（error_fallback=True）时
    会回退到本函数。

    为了让 GUI 展示结构统一，即便回退到本地模式，也会构造：
      - material_price_list / material_details（没有分平台实时价，min_price 用传入价格或 0）
      - output_details（每件产出 price / buff_price=price_buff 估算、c5/eco 置空）
    """
    def _groups(materials):
        groups = []
        pool = list(materials)
        for m in pool:
            if isinstance(m, dict):
                if not m.get("_collection"):
                    m["_collection"], m["_quality"] = _lookup_skin_meta(
                        m.get("goods_name") or m.get("hash_name") or "",
                        m.get("_skin_name") or "")
        from collections import OrderedDict
        by_col = OrderedDict()
        for m in pool:
            col = m.get("_collection") or "未匹配收藏品"
            by_col.setdefault(col, []).append(m)
        for col, ms in by_col.items():
            groups.append({"collection": col, "materials": ms})
        return groups

    def _resolve_material_price(m) -> float:
        price = None
        if isinstance(m, dict):
            price = m.get("_price")
            if price is None and stock_price_map:
                skin = (m.get("_skin_name") or "").lower()
                price = stock_price_map.get(skin)
            if price is None:
                price = stock_price_map.get(
                    ((m.get("goods_name") or m.get("hash_name")) or "").lower()) \
                    if stock_price_map else None
        try:
            return float(price or 0.0)
        except (TypeError, ValueError):
            return 0.0

    main_index = _parse_main_csv()
    groups_data = []
    output_details = []
    for g in _groups(materials):
        col = g["collection"]
        ms = g["materials"]
        count = len(ms)
        q = ms[0].get("_quality") or ""
        target_q = get_next_quality(q) if q else None
        outputs = []
        if target_q:
            target_key = (col, target_q, False)
            skin_rows = main_index.get(target_key, [])
            dedup = []
            seen_names = set()
            for skin in skin_rows:
                if skin["name"] in seen_names:
                    continue
                seen_names.add(skin["name"])
                dedup.append(skin)
            for skin in dedup:
                prob = (count / 10.0) / max(len(dedup), 1)
                out_float = calculate_output_float(
                    ms, skin["min_f"], skin["max_f"])
                price_val = float(skin.get("price_buff") or 0)
                entry = {
                    "name": skin["name"],
                    "hash_name": (skin.get("market_hash_name") or "").strip() or None,
                    "price": price_val,
                    "reference_price": 0.0,
                    "prob": prob,
                    "wear_float": out_float,
                    "wear_grade": get_wear_grade(out_float),
                    "rarity": (skin.get("品质") or "").strip() or None,
                    "platform_best": "buff_est",
                    "image": None,
                    "buff_price": price_val,
                    "buff_row": None,
                    "c5_price": None,
                    "c5_row": None,
                    "eco_price": None,
                    "eco_row": None,
                    # ===== 与 _official_simulate_outputs.output_details 字段对齐（GUI 直接消费）=====
                    "skin_name": skin["name"],
                    "probability": prob,
                    "wear_value": out_float,
                    # C2: local 路径也按官方档 + out_float 夹紧 wear_max（对齐后处理引擎）
                    #    skin dict 里有 wear_grade 就传入；否则就按 out_float 反推也能拿到
                    "wear_min": _resolve_wear_bounds(
                        skin, wear_cn=skin.get("wear_grade"), paint_wear=out_float)[0],
                    "wear_max": _resolve_wear_bounds(
                        skin, wear_cn=skin.get("wear_grade"), paint_wear=out_float)[1],
                    "min_price": price_val,
                    "best_platform": "buff_est",
                    "group_name": col or "",
                    "rarity_up": target_q or "",
                }
                outputs.append(entry)
                output_details.append(entry)
        groups_data.append({
            "collection": col,
            "count": count,
            "prob": count / 10.0,
            "materials": ms,
            "outputs": outputs,
        })

    cost = 0.0
    material_price_list = []
    material_details = []
    for idx, m in enumerate(materials):
        pv = _resolve_material_price(m)
        cost += pv
        material_price_list.append(pv)
        if isinstance(m, dict):
            # C2: local 路径也走统一 helper（避免 FN 档 wear_min=0 被 or 链误判为空，
            #    同时保证官方精确档 / paint_wear 夹紧 / user_wear_* 优先也生效）
            wmin_f, wmax_f = _resolve_wear_bounds(m)
        else:
            wmin_f, wmax_f = 0.0, 1.0
        material_details.append({
            "index": idx,
            "skin_name": ((m.get("_skin_name") if isinstance(m, dict) else "")
                          or (m.get("goods_name") if isinstance(m, dict) else "")
                          or (m.get("hash_name") if isinstance(m, dict) else "")
                          or getattr(m, "goods_name", "") or "").strip(),
            "hash_name": ((m.get("hash_name") if isinstance(m, dict) else "")
                          or getattr(m, "hash_name", "") or "").strip(),
            "wear_min": wmin_f, "wear_max": wmax_f,
            "buff_price": None, "buff_row": None,
            "c5_price": None,   "c5_row": None,
            "eco_price": None,  "eco_row": None,
            "min_price": pv,
            "best_platform": "local_est",
            "errors": {},
        })

    ref_value = 0.0
    all_output_entries: list[dict] = []
    for g in groups_data:
        for o in g["outputs"]:
            ref_value += o["price"] * o["prob"]
            all_output_entries.append(o)

    keep_rate = _calc_keep_rate(all_output_entries, cost)
    return {
        "groups": groups_data,
        "cost": cost,
        "ref_value": ref_value,
        "profit": ref_value - cost,
        "keep_rate": keep_rate,
        "source": "local",
        "material_price_list": material_price_list,
        "material_details": material_details,
        "output_details": output_details,
    }


def _call_eco_start_simulation(normalized_materials: list,
                               use_cache: bool = True) -> dict:
    """[helper A] 构建 HTTP 请求体 + 调 ECO 官方 StartSimulation，返回原始 result_data。

    说明：单独拆出是为了让调用方（GUI Worker）可以先拿到 sim_result，
    再把 **完整材料元数据** 传给后处理阶段，避免第二次 HTTP 触发 ResultCode=1
    「请不要重复请求」或「无元数据降级」。
    """
    api_materials = _build_api_materials(normalized_materials)
    return eco_web_api.start_simulation_web(api_materials, use_cache=use_cache)


def _process_eco_sim_result(result_data: dict,
                            normalized_materials: list,
                            platforms,
                            use_live_material_cost: bool,
                            stock_price_map: dict = None,
                            probe_lower_tiers: bool = False) -> dict:
    """[helper B] 把 ECO StartSimulation 返回的原始 result_data 加工成最终完整 dict。

    包含：skeleton 展平 → 主 CSV 反向索引 → 产出并发查价（三平台）→ 按收藏品分组 →
    材料成本（实时 or 传入价格）→ 顶部 6 指标 + material_details/output_details/material_price_list
    + ECO 官方展示字段（EstimatedCost/CapitalPreservationRate/SimulationAllPrice）。

    **不发任何 HTTP 请求**，纯后处理。调用方先自行拿到 sim_result 即可喂本函数。
    """
    # 1) 把 ECO 结果展平为 outputs 骨架
    skeleton = eco_web_api.simulation_to_output_skeleton(result_data)

    # 4) 准备主 CSV 反向索引 & 并发查价所有产出皮肤的最低价
    hash_map, name_wear_map = _build_full_hash_to_row_map()

    # 为每件产出皮肤构造 (index, item_config, wear_float, wear_cn)
    query_tasks = []  # list[tuple(idx, item_config)]
    for idx, sk in enumerate(skeleton):
        hn = sk.get("hash_name") or ""
        cn = sk.get("name_cn") or ""
        wear_cn = sk.get("wear_grade_cn") or ""
        # 磨损唯一真值来源：ECO 官方原始字符串（16 位小数）；float() 仅在计算瞬间转换
        wf = float(sk.get("wear_value_str")
                   or sk.get("wear_float") or 0.0)
        # 主 CSV 反向拿 buff_goods_id + 磨损区间
        main_row = _lookup_main_csv_row_by_output(hn, cn, wear_cn,
                                                  hash_map, name_wear_map)
        buff_gid = str(main_row.get("buff_goods_id") or "").strip() \
            if main_row else ""
        # C2: 产出查价边界——按用户要求"按填入的磨损值作为最高磨损往前搜索"：
        #   wear 下限 = 官方档最小值（例如 FN=0），
        #   wear 上限 = min(官方档最大值, ECO 官方给出的确切 wear_float(wf))
        #   例子：M4A4｜变频器｜崭新出厂 wf=0.0140 → 搜索 [0.00, 0.0140]
        #     （只找 ≤0.0140 的崭新出厂，不会错拿 0.0140~0.0700 间更贵/更差成色的）
        main_row2 = main_row if isinstance(main_row, dict) else {}
        # 优先把 wf（ECO 官方确切磨损）作为 paint_wear 传入夹紧上限
        wmin, wmax = _resolve_wear_bounds(main_row2, wear_cn=wear_cn, paint_wear=wf)
        ic = {
            "buff_goods_id": buff_gid,
            # C5 用完整英文市场哈希名（带英文磨损后缀）：和 hash_name 一致
            "c5_market_hash_name": hn,
            "c5_app_id": str(C5_APP_ID),
            # ECO 用 ECO 官方返回的真实 HashName（StartSimulation 产出 sk.hash_name）
            # 避免复用 C5 推断名造成「命名不匹配 → TotalRecord=0 → 价格 None」
            "eco_market_hash_name": hn,
        }
        query_tasks.append((idx, ic, wmin, wmax, sk, main_row, buff_gid, hn))

    # 并发查询所有产出皮肤的最低价（最多 N 线程）
    min_prices = [0.0] * len(query_tasks)
    best_rows = [None] * len(query_tasks)
    platform_rows = [None] * len(query_tasks)     # 每件产出的 Buff/C5/ECO 分平台最低价行
    errors_by_idx: dict[int, dict] = {}           # 每件产出 query_all 返回的 errors（关键诊断）
    work_min_by_idx: dict[int, float] = {}
    work_max_by_idx: dict[int, float] = {}
    work_items = []
    for idx, ic, wmin, wmax, sk, mr, _bg, _hn in query_tasks:
        work_items.append((idx, ic, wmin, wmax))
        work_min_by_idx[idx] = wmin
        work_max_by_idx[idx] = wmax
    if work_items:
        with ThreadPoolExecutor(max_workers=min(16, max(1, len(work_items)))) as pool:
            futs = {
                pool.submit(_query_min_price, ic, wmin, wmax, platforms): idx
                for idx, ic, wmin, wmax in work_items
            }
            for fut in as_completed(futs):
                idx = futs[fut]
                try:
                    mp, best, errors_dict, by_plat = fut.result()
                    min_prices[idx] = float(mp) if mp else 0.0
                    best_rows[idx] = best
                    platform_rows[idx] = by_plat
                    errors_by_idx[idx] = errors_dict or {}
                    if errors_dict:
                        has_no_buff = bool(errors_dict.get("buff"))
                        has_no_eco  = bool(errors_dict.get("eco"))
                        if has_no_buff or has_no_eco:
                            _bg, _hn = query_tasks[idx][6], query_tasks[idx][7]
                            logger.info(
                                "[OUTPUT_PRICE_ISSUE] idx=%d buff_goods_id=%r eco_hash_name=%r "
                                "→ errors=%s", idx, _bg, _hn, errors_dict)
                except Exception as e:
                    logger.debug(
                        "产出皮肤[%d]实时查价失败：%s，记 0", idx, e)
                    min_prices[idx] = 0.0
                    best_rows[idx] = None
                    platform_rows[idx] = None
                    errors_by_idx[idx] = {
                        "exception": f"{type(e).__name__}: {e}"}

    # 5) 按 collection_name 分组（与 simulate_outputs_local 的 groups 保持兼容）
    #    注意：官方接口会按 WeaponBox(收藏品) 把 10 件材料归入不同集合 → 再给 SimulationResults
    #    我们从 skeleton 的 collection_hash/collection_name 分组即可还原 group
    from collections import OrderedDict
    by_col = OrderedDict()
    total_ref = 0.0
    # query_tasks 结构：(idx, ic, wmin, wmax, sk, mr, buff_gid, hn)
    for i, tup in enumerate(query_tasks):
        sk = tup[4]
        mr = tup[5]
        col = sk.get("collection_name") or sk.get("collection_hash") or ""
        by_col.setdefault(col, []).append((i, sk, mr))
    # 每组的材料件数：从 MaterialFromWeaponBoxs 拿真实件数；拿不到则按 0 处理不影响展示
    mat_boxs = result_data.get("MaterialFromWeaponBoxs") or []
    col_to_count = {}
    for box in mat_boxs:
        if not isinstance(box, dict):
            continue
        n = 0
        fms = box.get("FormulaMaterials") or []
        if isinstance(fms, list):
            n = len(fms)
        col_to_count[(box.get("WeaponBoxName") or box.get("WeaponBoxHashName") or "").strip()] \
            = n

    def _platform_price(row):
        try:
            return float(row.get("price")) if row else None
        except (TypeError, ValueError):
            return None

    output_details = []  # 产出分平台明细（按 query_tasks 顺序，GUI 展示用）
    groups_data = []
    # 所有产出概率之和（有些收藏品组的总概率需要再次归一化展示？实际上
    # 官方 SimulationResults 每条的 OutputProbability 已自带收藏品占比权重，
    # 所以跨组求和就是 1.0。我们保持原值传递。）
    for col, entries in by_col.items():
        outputs_list = []
        for (i, sk, _mr_entry) in entries:
            live_price = min_prices[i]
            best = best_rows[i]
            by_plat = platform_rows[i] if isinstance(platform_rows[i], dict) else {}
            buff_row = by_plat.get("buff")
            c5_row   = by_plat.get("c5")
            eco_row  = by_plat.get("eco")
            # query_tasks 结构：(idx, ic, wmin, wmax, sk, mr_main_csv_row, buff_gid, hn)
            # 这里 mr_main_csv_row = tup[5]（主 CSV 反向 lookup 的结果，含 buff_goods_id 等），
            # buff_gid = tup[6]，hn = tup[7]；entry 直接带进去，GUI 就能显示「该产出的
            # Buff goods_id 是什么」，以及断言脚本不再是 69/69 全空。
            _ic = query_tasks[i][1]
            _mr_csv = query_tasks[i][5] if isinstance(query_tasks[i][5], dict) else {}
            _bgid = (str(query_tasks[i][6]).strip()
                     if query_tasks[i][6] is not None else
                     (str(_mr_csv.get("buff_goods_id") or "").strip()
                      if _mr_csv else ""))
            _hn_eco = str(query_tasks[i][7] or "").strip()
            _hn_c5  = str(_ic.get("c5_market_hash_name") or _hn_eco or "").strip()
            entry = {
                "name": sk.get("name_cn") or sk.get("hash_name"),
                "hash_name": sk.get("hash_name"),
                "price": live_price,       # 实时最低售价（Buff/C5/ECO 三平台比价）
                "reference_price": float(sk.get("reference_price") or 0),  # ECO 官方参考价，仅供展示
                "prob": float(sk.get("prob") or 0),                        # 官方 OutputProbability
                # 官方磨损一律携带 ECO 原始 16 位小数字符串（不再转 float 存储，
                # 消费端需要数值时自行 float() 瞬时转换，避免精度丢失）
                "wear_float": sk.get("wear_value_str")
                               or repr(float(sk.get("wear_float") or 0)),
                "wear_value_str": sk.get("wear_value_str") or "",          # 官方原始 16 位小数字符串（GUI 展示用）
                "wear_grade": sk.get("wear_grade_cn") or get_wear_grade(float(sk.get("wear_float") or 0)),
                "rarity": sk.get("rarity"),
                "platform_best": best["platform"] if best else "",
                "image": sk.get("image"),
                # 分平台价格 + 对应原始行（GUI 表格展示 Buff/C5/ECO 各自最低价）
                "buff_price": _platform_price(buff_row),
                "buff_row": buff_row,
                "c5_price": _platform_price(c5_row),
                "c5_row": c5_row,
                "eco_price": _platform_price(eco_row),
                "eco_row": eco_row,
                # ===== 主 CSV 反向索引元数据（修复『产出 buff_goods_id 拿不到 → GUI BUFF 栏全空』）=====
                #   - buff_goods_id: 主 CSV lookup 成功后的值（直接传给 Buff 接口的 goods_id）
                #   - c5_market_hash_name / eco_market_hash_name: 产出的市场哈希全名
                #   - skin_name_cn / wear_cn: 中文皮肤名 / 中文磨损（避免下游再次 split / 归一化）
                #   - collection_name / quality: 主 CSV 该行的收藏品名称 / 品质
                "buff_goods_id":    _bgid,
                "c5_market_hash_name": _hn_c5,
                "eco_market_hash_name": _hn_eco,
                "skin_name_cn":    sk.get("name_cn") or (_mr_csv.get("皮肤名称") if _mr_csv else "") or "",
                "wear_cn":         sk.get("wear_grade_cn")
                                   or (get_wear_grade(float(sk.get("wear_float") or 0)))
                                   or (_mr_csv.get("磨损") if _mr_csv else "")
                                   or "",
                "collection_name": col or (sk.get("collection_name") if isinstance(sk, dict) else "") or "",
                "quality":         (sk.get("rarity") or "") if True else
                                   ((_mr_csv.get("品质") if _mr_csv else "") or ""),
                # ===== 与 simulate_outputs_local.output_details 字段对齐（GUI 直接消费）=====
                "skin_name":   sk.get("name_cn") or sk.get("hash_name"),
                "probability": float(sk.get("prob") or 0),
                "wear_value":  sk.get("wear_value_str")
                               or repr(float(sk.get("wear_float") or 0)),
                "wear_min":    work_min_by_idx.get(i, sk.get("min_f")),
                "wear_max":    work_max_by_idx.get(i, sk.get("max_f")),
                "min_price":   live_price,
                "best_platform": best["platform"] if best else "",
                "best_row":    best,
                "group_name":  col or "",
                "rarity_up":   sk.get("rarity") or "",
                # ===== 关键：为什么这个产出的某平台是 None？把 errors 也带进去 =====
                "errors": errors_by_idx.get(i, {}),
            }
            outputs_list.append(entry)
            output_details.append(entry)
            total_ref += entry["price"] * entry["prob"]
        # 计算该组在 10 件材料中的件数：优先 MaterialFromWeaponBoxs 真值；
        # 拿不到就数 normalized_materials 里属于该收藏品的数量
        mat_count = col_to_count.get(col, 0)
        if mat_count == 0:
            # 通过材料元数据的 _collection 再统计一次
            mat_count = sum(1 for nm in normalized_materials
                            if (nm.get("_collection") or "") == col)
        groups_data.append({
            "collection": col,
            "count": mat_count,
            "prob": mat_count / 10.0 if mat_count >= 0 else 0.0,
            "materials": normalized_materials,  # 对外接口完整保留，GUI 若需按组分可再次切片
            "outputs": outputs_list,
        })

    # 6) 材料成本：实时查价 or 沿用库存/传入价格
    material_price_list = []
    material_details    = []
    if use_live_material_cost:
        cost, material_price_list, material_details = \
            _materials_cost_via_price_fetcher(normalized_materials, platforms,
                                              probe_lower_tiers=probe_lower_tiers)
    else:
        # 与 simulate_outputs_local 一致：_price 优先，其次 stock_price_map 按名映射
        # 这里为了简化，直接调 _materials_cost_via_price_fetcher 但跳过有 _price 的；
        # 没有 _price 的就按 stock_price_map 名称模糊找，找不到记 0
        cost = 0.0
        for idx, nm in enumerate(normalized_materials):
            p = nm.get("_price")
            try:
                pv = float(p) if p not in (None, "") else None
            except (TypeError, ValueError):
                pv = None
            # C2: wear_min/wear_max 统一走 _resolve_wear_bounds（避免 "_min_f or 0.0"
            #    把 FN 档 wear_min=0.0 误判成空，也保证和 GUI 显示 & live 查价边界一致）
            wmin, wmax = _resolve_wear_bounds(nm)
            if pv:
                cost += pv
                material_price_list.append(float(pv))
                material_details.append({
                    "index": idx,
                    "skin_name": (nm.get("_skin_name") or nm.get("goods_name") or nm.get("hash_name") or "").strip(),
                    "hash_name": (nm.get("hash_name") or "").strip(),
                    "wear_min": wmin,
                    "wear_max": wmax,
                    "buff_price": None, "buff_row": None,
                    "c5_price": None,   "c5_row": None,
                    "eco_price": None,  "eco_row": None,
                    "min_price": float(pv),
                    "best_platform": "fixed",
                    "errors": {},
                })
                continue
            used = 0.0
            detail = {
                "index": idx,
                "skin_name": (nm.get("_skin_name") or nm.get("goods_name") or nm.get("hash_name") or "").strip(),
                "hash_name": (nm.get("hash_name") or "").strip(),
                "wear_min": wmin,
                "wear_max": wmax,
                "buff_price": None, "buff_row": None,
                "c5_price": None,   "c5_row": None,
                "eco_price": None,  "eco_row": None,
                "min_price": 0.0,
                "best_platform": "stock_map",
                "errors": {},
            }
            if stock_price_map:
                key_options = [
                    (nm.get("_skin_name") or "").lower(),
                    (nm.get("goods_name") or "").lower(),
                    (nm.get("hash_name") or "").lower(),
                ]
                for k in key_options:
                    if k and k in stock_price_map:
                        used = float(stock_price_map[k])
                        break
            cost += used
            detail["min_price"] = used
            material_price_list.append(used)
            material_details.append(detail)
    ref_value = float(total_ref)
    keep_rate = _calc_keep_rate(output_details, float(cost))
    return {
        "groups": groups_data,
        "cost": float(cost),
        "ref_value": ref_value,
        "profit": ref_value - float(cost),
        "keep_rate": keep_rate,
        "source": "eco_official",
        # 新增：供 GUI 分平台明细表格展示
        "material_price_list": material_price_list,   # list[float]，与 10 件材料一一对应
        "material_details": material_details,         # list[dict]，每件材料 buff/c5/eco/min/errors...
        "output_details": output_details,             # list[dict]，每件产出 buff/c5/eco/min...
        "estimated_cost": result_data.get("EstimatedCost"),          # ECO 官方展示字段（仅供展示）
        "capital_preservation_rate": result_data.get("CapitalPreservationRate"),  # 同上（不作为计算依据）
        "simulation_all_price": result_data.get("SimulationAllPrice"),  # 同上
    }


def _official_simulate_outputs(normalized_materials: list,
                               platforms,
                               use_live_material_cost: bool,
                               stock_price_map: dict = None) -> dict:
    """[v2 官方模拟核心] 调用 StartSimulation + 对每件产出皮肤实时查价。

    内部简化为：A) _call_eco_start_simulation 发 HTTP 拿 result_data；
    B) _process_eco_sim_result 纯后处理。
    返回结构与 simulate_outputs_local 基本一致，额外带 source="eco_official"。
    """
    result_data = _call_eco_start_simulation(normalized_materials, use_cache=True)
    return _process_eco_sim_result(result_data, normalized_materials,
                                   platforms, use_live_material_cost, stock_price_map)


def simulate_outputs_from_eco_result(api_call_result: dict,
                                     materials: list,
                                     platforms: list[str] | None = None,
                                     use_live_material_cost: bool = True,
                                     stock_price_map: dict = None,
                                     error_fallback: bool = True,
                                     probe_lower_tiers: bool = False) -> dict:
    """[公开 API] 利用**已经成功的** ECO StartSimulation 原始结果进行后处理。

    专为 GUI Worker 场景设计：Worker 先自行调 eco_web_api.start_simulation_web()
    （打印"官方模拟返回 N 个可能产出"等日志），然后把 **原始 api_call_result**
    连同 **完整材料元数据字典 materials（10 件，建议传 GUI 槽位的完整字典，
    不要传精简 HTTP 请求体 api_materials）** 一起喂给本函数。

    本函数**不发任何 HTTP**，只做：normalize → skeleton → 产出并发查价 →
    材料成本核算 → 返回与 simulate_outputs 完全相同的 dict schema。
    这样可彻底避免「第二次 StartSimulation 触发 ResultCode=1 重复请求 → 降级 local」
    的架构级 Bug。

    Args:
        api_call_result: eco_web_api.start_simulation_web() 返回的原始 dict
        materials: 10 件材料，优先传 GUI 层 material_query_config 完整字典；
                   退而求其次传 HTTP 请求体（只有 HashName/WearValue/MaterialSource）
                   也可以，_normalize_materials HTTP 模式会反查主 CSV 补齐
        platforms: 查价平台列表，默认 ["buff","c5","eco"]
        use_live_material_cost: True=材料成本走 price_fetcher 实时；False=沿用传入价格
        stock_price_map: 皮肤名(lower)->单价（use_live=False 或 fallback local 时用）
        error_fallback: True=后处理抛异常降级 simulate_outputs_local；False=原样抛出

    Returns:
        与 simulate_outputs 完全相同 schema 的 dict。
    """
    if platforms is None:
        platforms = ["buff", "c5", "eco"]
    try:
        normalized = _normalize_materials(materials)
        return _process_eco_sim_result(api_call_result, normalized,
                                       platforms, use_live_material_cost,
                                       stock_price_map,
                                       probe_lower_tiers=probe_lower_tiers)
    except (RuntimeError, ValueError, TypeError, AttributeError,
            IndexError, KeyError) as e:
        if not error_fallback:
            raise
        logger.warning(
            "[FALLBACK] 利用 ECO sim_result 后处理失败，降级本地估算：%s", e)
        fallback_reason = (
            f"官方产出后处理失败：{type(e).__name__}: {e}，已降级本地估算。"
            f"（若您刚看到「官方模拟返回 N 产出」，请把完整材料字典而非 api_materials 传入"
            f" simulate_outputs_from_eco_result，或刷新 ECO_WEB_COOKIE 后重试）"
        )
        try:
            normalized_local = _normalize_materials(materials)
            r = simulate_outputs_local(normalized_local, stock_price_map)
            r["fallback_reason"] = fallback_reason
            return r
        except Exception as e2:
            logger.warning(
                "[FALLBACK] normalized 失败，直接用原始 materials：%s", e2)
            try:
                r2 = simulate_outputs_local(materials, stock_price_map)
                r2["fallback_reason"] = fallback_reason + f"；且本地补齐也失败：{type(e2).__name__}: {e2}"
                return r2
            except Exception as e3:
                # 兜底：返回一个空结构，保证 GUI 不崩
                return {
                    "groups": [], "cost": 0.0, "ref_value": 0.0,
                    "profit": 0.0, "keep_rate": 0.0, "source": "local",
                    "material_price_list": [0.0]*10,
                    "material_details": [], "output_details": [],
                    "fallback_reason": (
                        f"{fallback_reason}；本地估算也失败：{type(e3).__name__}: {e3}。"
                        f"请检查 10 件材料是否属于同一升级品质（工业→军规 等）。"
                    ),
                }


def simulate_outputs(materials: list, stock_price_map: dict = None,
                     use_eco_official: bool | None = None,
                     platforms: list[str] | None = None,
                     error_fallback: bool = True,
                     use_live_material_cost: bool = True) -> dict:
    """汰换模拟（v2 默认优先走 ECO 官方 StartSimulation 权威计算）。

    产出磨损和概率：
      - 默认用 ECO 官方 StartSimulation 的 OutputProbability/WearValue
        （use_eco_official=None 读取 config.ECO_WEB_USE_OFFICIAL_SIMULATION 默认 True）
      - 官方接口不可用时（error_fallback=True）自动降级为 v1 本地估算版本，
        并在返回中加 source="local" 字段让调用方感知。
    价格：
      - 所有产出皮肤的售价：**不再**用主 CSV price_buff / ECO ReferencePrice /
        EstimatedCost，统一并发调用 price_fetcher.query_all(Buff/C5/ECO) 取最低在售价。
      - 材料成本（use_live_material_cost=True，默认）：10 件材料实时查价求和。
        若为 False 则沿用传入的 _price 字段 / stock_price_map / 主 CSV 粗略估算，
        用于 GUI 已预先批量查完材料价的场景避免重复爬取。

    Args:
        materials: 10 件，支持 list[Material dataclass] / list[dict] / 混合
        stock_price_map: 皮肤名(lower) -> 单价（仅降级/非实时模式或 use_live_material_cost=False 时用）
        use_eco_official: True=强制官方；False=强制本地；None=读取配置 ECO_WEB_USE_OFFICIAL_SIMULATION
        platforms: 查价平台列表，默认 ["buff","c5","eco"]
        error_fallback: True=官方失败时自动回退本地估算（保证 GUI 不中断）；
                        False=官方失败直接抛 RuntimeError（调用方处理错误文案）
        use_live_material_cost: True=材料成本走 price_fetcher 实时；
                                False=沿用 material._price / stock_price_map

    Returns:
        dict: {
            "groups": [{collection, count, prob, materials, outputs:[{name,price,prob,wear_float,...}]}],
            "cost",          # 材料成本（元，实时查价 or 传入价格求和）
            "ref_value",     # 加权期望售价 = Σ min_price × prob
            "profit",        # ref_value - cost
            "keep_rate",     # min(100, ref_value / cost × 100)
            "source",        # "eco_official" 或 "local"（告知调用方是否成功走官方）
            # source=eco_official 时额外展示字段（**都不作为计算依据**）：
            "estimated_cost", "capital_preservation_rate", "simulation_all_price"
        }
    """
    if len(materials) != 10:
        raise ValueError(
            f"simulate_outputs 材料数必须为 10 件，实际 {len(materials)} 件")

    if platforms is None:
        platforms = ["buff", "c5", "eco"]

    if use_eco_official is None:
        use_eco_official = bool(ECO_WEB_USE_OFFICIAL_SIMULATION)

    # 统一材料形态
    try:
        normalized = _normalize_materials(materials)
    except Exception as e:
        if error_fallback:
            logger.warning("材料规范化失败，降级本地估算: %s", e)
            return simulate_outputs_local(materials, stock_price_map)
        raise
    if not use_eco_official:
        # 强制本地估算（但价格字段仍用主 CSV——若要实时价格需要调用方另行换算）
        # 注意：为避免行为混淆，明确的 use_eco_official=False 只返回 v1 算法
        return simulate_outputs_local(normalized, stock_price_map)

    try:
        return _official_simulate_outputs(
            normalized, platforms=platforms,
            use_live_material_cost=use_live_material_cost,
            stock_price_map=stock_price_map)
    except (RuntimeError, ValueError, TypeError) as e:
        if not error_fallback:
            raise RuntimeError(
                f"ECO 官方汰换模拟失败（未启用降级）：{e}") from e
        msg = str(e)
        if "用户未登录" in msg or "未登录" in msg:
            tip = "（ECO 网页 Cookie 过期或缺失 → 登录 ECO 官网后复制最新 Cookie 写入 utils/config.py 的 ECO_WEB_COOKIE）"
        elif "请不要重复请求" in msg:
            tip = "（相同材料短时间内被 ECO 去重 → 稍后再试，或确保 use_cache=True 命中缓存）"
        elif "Timeout" in msg or "timed out" in msg or "Connection" in msg:
            tip = "（网络问题 → 检查代理/防火墙，或调大 ECO_WEB_START_SIM_TIMEOUT）"
        elif "HashName" in msg and "无法生成" in msg:
            tip = "（材料主CSV 缺失 市场哈希名称/磨损 字段 → 补齐主 CSV）"
        else:
            tip = ""
        logger.warning(
            "[ECO_API_FALLBACK] StartSimulation 失败 → 降级本地估算：%s %s",
            msg, tip)
        return simulate_outputs_local(normalized, stock_price_map)


def _lookup_skin_meta(goods_name: str, skin_name: str = "") -> tuple:
    """根据物品名反查主 CSV，返回 (收藏品, 品质)；查不到返回 ("", "")。"""
    rows = load_items_from_main_csv_full()
    key = (skin_name or goods_name or "").strip().lower()
    if not key:
        return "", ""
    for row in rows:
        name = (row.get("皮肤名称") or "").strip().lower()
        if name and (name in key or key in name):
            return (row.get("收藏品名称") or "").strip(), \
                   (row.get("品质") or "").strip()
    return "", ""


def find_plans(target_collection: str = None,
               target_quality: str = None,
               include_stattrak: bool = False,
               max_budget: float = 1e9,
               top_n: int = 10,
               candidate_limit: int = 15,
               max_combinations: int = 5000,
               require_output_in_target_wear: bool = True,
               use_eco_official: bool | None = None,
               platforms: list[str] | None = None,
               error_fallback: bool = True) -> list:
    """从当前库存搜索可执行的汰换方案（v2：默认优先 ECO 官方模拟 + 实时查价）。

    相比 v1 的改动：
      - 对每个 10 件 combo：先调 simulate_outputs(use_eco_official)，
        用官方 OutputProbability/WearValue 作为权威产出分布（失败按 error_fallback 降级）。
      - 筛选条件：在 sim_result.groups[*].outputs[*] 里，找皮肤名匹配 target_skin["name"]
        且 wear_float 在 target_skin 的 [min_f, max_f] 内
        （若 require_output_in_target_wear=True 并且目标磨损等级有值，再额外命中 wear_grade）。
      - 价格：
        * target_price：target_skin 若在 outputs 里匹配成功 → 用其 outputs[i].price（price_fetcher 最低价）
          否则退化 → 主 CSV price_buff
        * material_cost → sim_result.cost（实时 10 件材料查价和，或降级版本地成本）
        * profit → sim_result.profit（Σ(min_price×prob) − 成本）
        * roi    → profit / material_cost * 100（material_cost=0 时 0）
      - 排序依旧按 profit 降序取 top_n。

    Args:
        target_collection: 指定收藏品名称，None 遍历所有
        target_quality: 指定目标品质，None 遍历所有
        include_stattrak: 是否匹配 StatTrak 物品
        max_budget: 材料总成本上限（材料 cost 超过则丢弃该方案；默认 1e9=无约束）
        top_n: 最终返回前 N 个方案（全收集齐全后排序）
        candidate_limit: 每组候选材料限制（避免组合爆炸）
        max_combinations: 单组组合枚举上限（ECO 官方模式下建议 100~300 以免 API 压力过大）
        require_output_in_target_wear: True=只保留产出磨损落在目标皮肤 [min_f,max_f] 且等级匹配的
        use_eco_official: True=强制官方 / False=强制本地估算 / None=读配置
        platforms: 查价平台列表，默认 None → ["buff","c5","eco"]
        error_fallback: True=ECO 官方失败时降级本地估算，不抛出；False=抛出由 GUI 捕获弹错

    Returns:
        list[TaihuanPlan]（Plan 含 source / output_probs / ref_value / keep_rate 新字段）
    """
    if platforms is None:
        platforms = ["buff", "c5", "eco"]
    if use_eco_official is None:
        use_eco_official = bool(ECO_WEB_USE_OFFICIAL_SIMULATION)

    # ECO 官方模式组合数自动降档（每个 combo 至少 1 次 HTTP StartSimulation +
    # N 次价格爬取，5000 组合会跑很久；这里做软上限，日志提示）
    if use_eco_official and max_combinations > 200:
        logger.info(
            "find_plans 官方模式下 max_combinations=%d 过大，自动降档为 200"
            "，避免长时间等待 / 触发 ECO 限流。"
            "（可通过 max_combinations 参数显式提高）", max_combinations)
        max_combinations = 200

    main_index = _parse_main_csv()
    inventory = load_inventory_from_db()
    if not inventory:
        logger.warning("库存为空，请先调用 refresh_inventory 抓取。")
        return []

    inv_by_key = _match_inventory_to_collection(inventory, main_index)

    # 构建主 CSV 反向索引，用于 combo 之外目标皮肤独立查价
    hash_map, name_wear_map = _build_full_hash_to_row_map()

    plans = []
    # 遍历每个 (collection, quality) 作为材料品质
    for (collection, material_quality, is_st), materials in inv_by_key.items():
        if is_st != include_stattrak:
            continue
        if target_collection and collection != target_collection:
            continue
        # 目标品质 = 材料品质 + 1
        target_q = get_next_quality(material_quality)
        if not target_q:
            continue
        if target_quality and target_q != target_quality:
            continue

        # 目标皮肤列表（同收藏品 + 上一级品质 + StatTrak 一致）
        target_key = (collection, target_q, is_st)
        target_skins = main_index.get(target_key, [])
        if not target_skins:
            continue

        # 材料数量检查
        if len(materials) < 10:
            logger.debug("收藏品 %s 品质 %s 库存不足（%d 件，需 10）",
                         collection, material_quality, len(materials))
            continue

        # 按磨损升序（磨损越低产出越好）
        candidates = sorted(materials, key=lambda m: m.paint_wear)
        if len(candidates) > candidate_limit:
            candidates = candidates[:candidate_limit]

        # 枚举 10 件组合（加组合数上限保护，避免组合爆炸）
        from itertools import combinations
        from math import comb
        total_combos = comb(len(candidates), 10)
        use_sampling = total_combos > max_combinations

        # 采样或枚举 combo 迭代器：所有目标皮肤共享一套 combo，避免每款目标皮肤重复枚举
        if use_sampling:
            import random
            indices_pool = list(range(len(candidates)))
            seen = set()
            sample_size = min(max_combinations, total_combos)
            while len(seen) < sample_size:
                combo_idx = tuple(sorted(random.sample(indices_pool, 10)))
                seen.add(combo_idx)
            combo_list = [tuple(candidates[i] for i in ci) for ci in seen]
        else:
            combo_list = list(combinations(candidates, 10))

        logger.info(
            "find_plans: 收藏品=%s 材料品质=%s → 目标品质=%s 候选皮肤=%d 个，"
            "combo 数量=%d（官方模拟=%s）",
            collection, material_quality, target_q, len(target_skins),
            len(combo_list), "ON" if use_eco_official else "OFF")

        for combo in combo_list:
            combo_dicts = list(combo)  # Material dataclass 列表，_normalize_materials 已兼容
            # 每个 combo 只算一次模拟（所有目标皮肤共享：产出分布是全集合给出的）
            try:
                sim_result = simulate_outputs(
                    combo_dicts,
                    stock_price_map=None,
                    use_eco_official=use_eco_official,
                    platforms=platforms,
                    error_fallback=error_fallback,
                    use_live_material_cost=True,
                )
            except RuntimeError as e:
                # error_fallback=False 时抛出；这里不吞，保持语义
                logger.error("find_plans simulate_outputs 失败（未降级）：%s", e)
                raise

            sim_source = sim_result.get("source", "local")
            sim_cost = float(sim_result.get("cost", 0))
            sim_ref_value = float(sim_result.get("ref_value", 0))
            sim_profit = float(sim_result.get("profit", 0))
            sim_keep = float(sim_result.get("keep_rate", 0))

            if sim_cost > max_budget:
                continue

            # 展平所有 outputs，便于 per-target_skin 匹配
            all_outputs = []
            for g in (sim_result.get("groups") or []):
                for o in (g.get("outputs") or []):
                    all_outputs.append(o)

            # 对每款目标皮肤，判断是否匹配（磨损区间 + 磨损等级）
            for target_skin in target_skins:
                target_name = target_skin["name"]
                target_min_f = float(target_skin.get("min_f") or 0)
                target_max_f = float(target_skin.get("max_f") or 1)
                target_wear_grade = (target_skin.get("wear") or "").strip()

                # 找到 outputs 中皮肤名匹配目标的条目
                # 匹配规则：主 CSV 皮肤名子串包含 outputs.name_cn 或互反包含
                matches = []
                for o in all_outputs:
                    oname = (o.get("name") or "").strip().lower()
                    tname = target_name.strip().lower()
                    if not oname:
                        continue
                    if oname == tname:
                        matches.append(o)
                    elif (oname in tname) or (tname in oname):
                        matches.append(o)
                if not matches:
                    # 若没有任何匹配的产出条目，说明该 combo 的分布不含目标皮肤 → 跳过
                    continue

                # 对匹配到的所有条目，筛选磨损在 [target_min_f, target_max_f] 的
                qualified = []
                for o in matches:
                    wf = float(o.get("wear_float") or 0)
                    if not (target_min_f <= wf <= target_max_f):
                        continue
                    if require_output_in_target_wear and target_wear_grade:
                        og = (o.get("wear_grade") or "").strip()
                        if og and og != target_wear_grade:
                            continue
                    qualified.append(o)
                if not qualified:
                    continue

                # 取磨损最接近目标区间中点的那一条（作为代表项的 output_float / wear_grade / target_price）
                mid = (target_min_f + target_max_f) / 2.0
                best = min(qualified, key=lambda o: abs(float(o.get("wear_float") or 0) - mid))

                # target_price：优先 best.price（price_fetcher 最低价）→ 退化主 CSV price_buff
                try:
                    target_price = float(best.get("price") or 0)
                except (TypeError, ValueError):
                    target_price = 0.0
                if target_price <= 0:
                    try:
                        target_price = float(target_skin.get("price_buff") or 0)
                    except (TypeError, ValueError):
                        target_price = 0.0

                out_float = float(best.get("wear_float") or 0)
                out_grade = (best.get("wear_grade") or get_wear_grade(out_float) or "").strip()

                # material_cost / profit / roi 统一走 simulate_outputs 给出的整体值
                material_cost = sim_cost
                profit = sim_profit
                roi = (profit / material_cost * 100) if material_cost > 0 else 0.0

                # output_probs：记录该 combo 全部候选产出（供 GUI 展示该方案的全貌）
                output_probs_snapshot = [
                    {
                        "name": o.get("name"),
                        "hash_name": o.get("hash_name"),
                        "prob": float(o.get("prob") or 0),
                        "wear_float": float(o.get("wear_float") or 0),
                        "wear_grade": o.get("wear_grade") or get_wear_grade(float(o.get("wear_float") or 0)),
                        "min_price": float(o.get("price") or 0),
                    } for o in all_outputs
                ]

                plans.append(TaihuanPlan(
                    target_collection=collection,
                    target_quality=target_q,
                    target_skin_name=target_name,
                    target_price=target_price,
                    materials=list(combo),
                    material_cost=material_cost,
                    output_float=out_float,
                    output_wear_grade=out_grade,
                    profit=profit,
                    roi=roi,
                    source=sim_source,
                    output_probs=output_probs_snapshot,
                    ref_value=sim_ref_value,
                    keep_rate=sim_keep,
                ))

                # 提前达到 max_budget 的不需要重复
                if material_cost > max_budget:
                    continue

    # 按 profit 降序
    plans.sort(key=lambda p: p.profit, reverse=True)
    result = plans[:top_n]
    logger.info(
        "find_plans 完成：候选 %d 个方案，取 top %d。"
        "（官方模拟方案=%d，本地降级方案=%d）",
        len(plans), len(result),
        sum(1 for p in result if p.source == "eco_official"),
        sum(1 for p in result if p.source != "eco_official"))
    return result


# ============================================================
# 持久化方案到 SQLite
# ============================================================
def save_plan_to_db(plan: TaihuanPlan, plan_name: str = "") -> int:
    """保存方案到 taihuan_plan 表，返回 plan_id。"""
    ensure_inventory_table()
    conn = get_conn()
    from datetime import datetime
    now = datetime.now().isoformat(timespec="seconds")
    materials_json = json.dumps([
        {
            "asset_id": m.asset_id,
            "goods_name": m.goods_name,
            "paint_wear": m.paint_wear,
            "skin_name": m.target_skin_name,
            "collection": m.collection,
            "quality": m.quality,
        } for m in plan.materials
    ], ensure_ascii=False)
    plan_name = plan_name or f"{plan.target_collection}_{plan.target_quality}_{plan.target_skin_name}"
    cur = conn.execute("""
        INSERT INTO taihuan_plan
            (plan_name, target_item, materials_json, status, created_at)
        VALUES (?, ?, ?, 'pending', ?)
    """, (plan_name, plan.target_skin_name, materials_json, now))
    plan_id = cur.lastrowid
    conn.commit()
    conn.close()
    return plan_id


def save_custom_plan_to_db(plan_name: str, target_item: str,
                           target_wear_min: float, target_wear_max: float,
                           materials: list, remark: str = "") -> int:
    """保存自定义方案到数据库。

    Args:
        plan_name: 方案名称
        target_item: 目标物品名称
        target_wear_min/max: 期望产出磨损区间
        materials: list[dict] 每个含 asset_id/goods_name/paint_wear/skin_name
        remark: 可选备注
    """
    ensure_inventory_table()
    conn = get_conn()
    from datetime import datetime
    now = datetime.now().isoformat(timespec="seconds")
    plan_data = {
        "target_item": target_item,
        "target_wear_min": target_wear_min,
        "target_wear_max": target_wear_max,
        "materials": materials,
    }
    materials_json = json.dumps(plan_data, ensure_ascii=False)
    cur = conn.execute("""
        INSERT INTO taihuan_plan
            (plan_name, target_item, materials_json, status, created_at, remark)
        VALUES (?, ?, ?, 'pending', ?, ?)
    """, (plan_name, target_item, materials_json, now, remark))
    plan_id = cur.lastrowid
    conn.commit()
    conn.close()
    return plan_id


def update_custom_plan(plan_id: int, plan_name: str, target_item: str,
                       target_wear_min: float, target_wear_max: float,
                       materials: list, remark: str = "") -> None:
    """更新已有方案的内容。"""
    ensure_inventory_table()
    conn = get_conn()
    plan_data = {
        "target_item": target_item,
        "target_wear_min": target_wear_min,
        "target_wear_max": target_wear_max,
        "materials": materials,
    }
    materials_json = json.dumps(plan_data, ensure_ascii=False)
    conn.execute("""
        UPDATE taihuan_plan
        SET plan_name = ?, target_item = ?, materials_json = ?, remark = ?
        WHERE id = ?
    """, (plan_name, target_item, materials_json, remark, plan_id))
    conn.commit()
    conn.close()


def save_material_plan_to_db(plan_name: str, materials: list,
                             remark: str = "") -> int:
    """保存纯材料汰换方案（无目标产出）到数据库。

    Args:
        plan_name: 方案名称
        materials: list[dict] 主 CSV 材料行（含 皮肤名称/磨损/磨损区间/
                   buff_goods_id/市场哈希名称/price_buff 等字段）
        remark: 可选备注

    Returns:
        int: 新方案的 plan_id
    """
    ensure_inventory_table()
    conn = get_conn()
    from datetime import datetime
    now = datetime.now().isoformat(timespec="seconds")
    plan_data = {"materials": materials}
    materials_json = json.dumps(plan_data, ensure_ascii=False)
    cur = conn.execute("""
        INSERT INTO taihuan_plan
            (plan_name, target_item, materials_json, status, created_at, remark)
        VALUES (?, ?, ?, 'pending', ?, ?)
    """, (plan_name, "", materials_json, now, remark))
    plan_id = cur.lastrowid
    conn.commit()
    conn.close()
    return plan_id


def update_material_plan(plan_id: int, plan_name: str, materials: list,
                         remark: str = "") -> None:
    """更新纯材料方案的内容（材料清单 + 名称）。"""
    ensure_inventory_table()
    conn = get_conn()
    plan_data = {"materials": materials}
    materials_json = json.dumps(plan_data, ensure_ascii=False)
    conn.execute("""
        UPDATE taihuan_plan
        SET plan_name = ?, target_item = '', materials_json = ?, remark = ?
        WHERE id = ?
    """, (plan_name, materials_json, remark, plan_id))
    conn.commit()
    conn.close()


def material_query_config(material: dict) -> dict:
    """把主 CSV 材料行转成查价配置（磨损范围 + 各平台 ID）。

    磨损范围优先级（高→低）：
      1) 材料 dict 自带 user_wear_min / user_wear_max（用户在槽位自定义）
      2) 材料 dict 自带 磨损_min / 磨损_max（方案保存时写入）
      3) 主 CSV 行的「磨损区间」解析值

    Returns:
        dict: {item_name, wear_grade, wear_min, wear_max, buff_goods_id,
               c5_market_hash_name, c5_app_id, price_buff,
               user_wear_enabled(bool)}
    """
    wear_range_raw = (material.get("磨损区间") or "").replace("~", "").split()
    wear_min, wear_max = 0.0, 1.0
    if len(wear_range_raw) >= 2:
        try:
            wear_min = float(wear_range_raw[0])
            wear_max = float(wear_range_raw[1])
        except ValueError:
            pass
    # CSV 兜底：磨损区间写成 '0~0.6'/'0~1' 这种默认宽范围时，用 wear_grade 官方精确范围
    if (wear_max - wear_min) >= 0.45:
        off = _official_wear_range(material.get("磨损") or "")
        if off:
            wear_min, wear_max = off
    user_enabled = False
    # 方案保存字段（兼容先前版本/其它模块写入）
    if "磨损_min" in material and "磨损_max" in material:
        try:
            wmin = float(material["磨损_min"])
            wmax = float(material["磨损_max"])
            if 0.0 <= wmin < wmax <= 1.0:
                wear_min, wear_max = wmin, wmax
                user_enabled = True
        except (TypeError, ValueError):
            pass
    # GUI 用户自定义覆盖（优先级最高）
    if "user_wear_min" in material and "user_wear_max" in material:
        try:
            wmin = float(material["user_wear_min"])
            wmax = float(material["user_wear_max"])
            if 0.0 <= wmin < wmax <= 1.0:
                wear_min, wear_max = wmin, wmax
                user_enabled = True
        except (TypeError, ValueError):
            pass

    hash_base = (material.get("市场哈希名称") or "").strip()
    wear_cn = (material.get("磨损") or "").strip()
    try:
        price = float(material.get("price_buff") or 0)
    except (TypeError, ValueError):
        price = 0.0
    # 【goods_id 档位校验/自愈 2026-09-11】DB 旧方案或库存匹配可能保存
    # 错档的 buff_goods_id（典型：WW/BS 材料存了 FN 档 id → Buff 磨损过滤
    # 后 0 条）。有 wear_grade 时用 (皮肤名+档位) 复合索引重查一遍，
    # 不一致自动用正确档位的 gid（旧方案载入即自愈，无需重建方案）。
    own_gid = str(material.get("_buff_goods_id")
                  or material.get("buff_goods_id") or "").strip()
    buff_gid = own_gid
    if wear_cn:
        _col, _q, _bh, gid_fixed, _wc, _mnf, _mxf = _material_lookup_meta(
            material.get("皮肤名称") or material.get("item_name") or "",
            material.get("_skin_name") or "",
            wear_cn=wear_cn,
            hash_name=material.get("hash_name")
            or material.get("c5_market_hash_name") or "")
        gid_fixed = str(gid_fixed or "").strip()
        if gid_fixed:
            if own_gid and own_gid != gid_fixed:
                logger.warning(
                    "[GID-TIER-FIX] %s（%s）buff_goods_id 档位错配 "
                    "%s → %s（已自动校正）",
                    material.get("皮肤名称") or material.get("item_name"),
                    wear_cn, own_gid, gid_fixed)
            buff_gid = gid_fixed
    return {
        "item_name": (material.get("皮肤名称") or material.get("item_name") or "").strip(),
        "wear_grade": wear_cn,
        "wear_min": wear_min,
        "wear_max": wear_max,
        # 优先用规范化后的 _buff_goods_id（已按磨损档校正），回退到原始 buff_goods_id
        "buff_goods_id": buff_gid,
        "c5_market_hash_name": build_c5_market_hash_name(hash_base, wear_cn),
        "c5_app_id": str(C5_APP_ID),
        "price_buff": price,
        "user_wear_enabled": user_enabled,
    }


def update_plan_materials(plan_id: int, materials: list) -> None:
    """仅更新方案的材料清单（匹配后同步）。"""
    ensure_inventory_table()
    conn = get_conn()
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT materials_json FROM taihuan_plan WHERE id = ?",
        (plan_id,)).fetchone()
    if not row:
        conn.close()
        return
    try:
        plan_data = json.loads(row["materials_json"] or "{}")
    except (json.JSONDecodeError, TypeError):
        plan_data = {}
    plan_data["materials"] = materials
    materials_json = json.dumps(plan_data, ensure_ascii=False)
    conn.execute(
        "UPDATE taihuan_plan SET materials_json = ? WHERE id = ?",
        (materials_json, plan_id))
    conn.commit()
    conn.close()


def list_saved_plans() -> list:
    """返回所有保存的方案（含自定义）。"""
    ensure_inventory_table()
    conn = get_conn()
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT id, plan_name, target_item, materials_json,
               status, created_at, executed_at, remark
        FROM taihuan_plan ORDER BY id DESC
    """).fetchall()
    conn.close()
    result = []
    for r in rows:
        d = dict(r)
        try:
            d["materials_data"] = json.loads(d.get("materials_json") or "{}")
        except (json.JSONDecodeError, TypeError):
            d["materials_data"] = {}
        result.append(d)
    return result


def delete_plan(plan_id: int) -> None:
    """删除指定方案。"""
    ensure_inventory_table()
    conn = get_conn()
    conn.execute("DELETE FROM taihuan_plan WHERE id = ?", (plan_id,))
    conn.execute("DELETE FROM taihuan_log WHERE plan_id = ?", (plan_id,))
    conn.commit()
    conn.close()


# ============================================================
# 从 CS2UP TXT 批量导入配方
# ============================================================
# 官方 5 档磨损区间（精确值，匹配过滤视图）
_OFFICIAL_RANGES = [
    ("崭新出厂", 0.00, 0.07),
    ("略有磨损", 0.07, 0.15),
    ("久经沙场", 0.15, 0.38),
    ("破损不堪", 0.38, 0.45),
    ("战痕累累", 0.45, 1.00),
]


def _wear_grade(w: float):
    for g, lo, hi in _OFFICIAL_RANGES:
        if lo <= w < hi:
            return g, lo, hi
    return "战痕累累", 0.45, 1.00


def _strip_wear(name: str) -> str:
    return re.sub(r"\s*\([^()]*\)\s*$", "", name).strip()


def import_cs2up_txt(txt_path: str) -> tuple[int, list[str]]:
    """从 CS2UP 一键炼金导出的 TXT 文件批量导入方案。

    格式：12 行 / 配方（标题 + 成本行 + 10 件材料），空行分隔。

    Returns:
        (导入数量, 每条的一行摘要 [f"[{pid}] 名称 ..."])
    """
    from core.data_manager import load_items_from_main_csv_full

    with open(txt_path, "r", encoding="utf-8-sig") as f:
        content = f.read()

    # 构建查询索引
    main_rows = load_items_from_main_csv_full()
    by_cn, by_base = {}, {}
    # 额外建 (皮肤名, 磨损档) 索引，避免同名皮肤取到第一条（FN 档）的 goods_id
    by_cn_wear = {}
    for r in main_rows:
        cn = (r.get("皮肤名称") or "").strip()
        if cn:
            by_cn.setdefault(cn, r)
            by_base.setdefault(_strip_wear(cn), r)
            wear = (r.get("磨损") or "").strip()
            if wear:
                by_cn_wear.setdefault((_strip_wear(cn), wear), r)

    def _resolve(name: str, wear: float) -> dict:
        # CS2UP 给出的 wear 是"最高磨损上限"——实际物品磨损必须 ≤ wear，
        # 区间取 [档下界, wear]（夹紧档上界）。例如：
        #   冰原迷彩 磨损 0.49 → 档=战痕累累(0.45~1.00) → 实际区间 [0.45, 0.49]
        g, lo, _hi = _wear_grade(wear)
        hi = wear   # 最高磨损上限 = CS2UP 给的值（不取档上界）
        # 极端值保护：避免 wear 恰等于档边界时 hi <= lo（如 0.07 反查崭新出厂）
        if not (lo < hi <= 1.0):
            # wear 恰等于档上界 → 退化为完整档区间
            _, lo, hi = _wear_grade(wear - 1e-9)
        named = f"{name} ({g})"
        # 优先按 (皮肤名, 磨损档) 精确匹配，避免取到 FN 档的 goods_id
        row = (by_cn_wear.get((_strip_wear(name), g))
               or by_cn.get(named) or by_cn.get(name)
               or by_base.get(_strip_wear(name)))
        # 若档反查得到 lo 但 wear 比 lo 还小（罕见：磨损值落档边界附近时）
        if wear <= lo:
            # 退回前一档（更精确的夹紧）
            prev = [(p_lo, p_hi) for _, p_lo, p_hi in _OFFICIAL_RANGES
                    if p_hi <= lo]
            if prev:
                lo, _ = prev[-1]
            hi = max(lo + 1e-6, wear)
        if row is not None:
            return {
                "皮肤名称": row.get("皮肤名称") or named,
                "市场哈希名称": row.get("市场哈希名称") or "",
                "磨损": g,
                "品质": row.get("品质") or "",
                "磨损_min": lo,
                "磨损_max": hi,
                "磨损区间": f"{lo:.10f}~{hi:.10f}",
                "涂装编号": row.get("涂装编号") or "",
                "buff_goods_id": row.get("buff_goods_id") or "",
                "c5_market_hash_name": row.get("市场哈希名称") or "",
                "price_buff": 0.0,
                "collection": row.get("所属收藏") or "",
                "wear_min": lo,
                "wear_max": hi,
                "user_wear_min": lo,
                "user_wear_max": hi,
                "paint_wear": None,
            }
        return {
            "皮肤名称": named,
            "市场哈希名称": "",
            "磨损": g,
            "品质": "",
            "磨损_min": lo,
            "磨损_max": hi,
            "磨损区间": f"{lo:.10f}~{hi:.10f}",
            "涂装编号": "",
            "buff_goods_id": "",
            "c5_market_hash_name": "",
            "price_buff": 0.0,
            "collection": "",
            "wear_min": lo,
            "wear_max": hi,
            "user_wear_min": lo,
            "user_wear_max": hi,
            "paint_wear": None,
        }

    summaries = []
    counters = {}
    count = 0
    for block in re.split(r"\n\s*\n", content.strip()):
        lines = [ln.strip() for ln in block.strip().splitlines() if ln.strip()]
        if len(lines) < 12 or not lines[0].startswith("CS2UP 一键炼金："):
            continue
        title = lines[0][len("CS2UP 一键炼金："):].strip()
        m = re.match(
            r"成本\s*¥([\d.,]+)｜EV\s*¥([\d.,]+)｜ROI\s*([+-]?\d+\.?\d*)%｜保本率\s*(\d+\.?\d*)%",
            lines[1])
        if m:
            cost, roi, brk = (float(m.group(1).replace(",", "")),
                              float(m.group(3)), float(m.group(4)))
        else:
            cost, roi, brk = 0.0, 0.0, 0.0
        mats = []
        ok = True
        for ln in lines[2:12]:
            mm = re.match(
                r"\d+\.\s*(.+?)｜磨损\s*(\d+\.\d+)｜¥([\d.,]+)", ln)
            if not mm:
                ok = False
                break
            d = _resolve(mm.group(1).strip(), float(mm.group(2)))
            d["cs2up_price"] = float(mm.group(3).replace(",", ""))
            mats.append(d)
        if not ok:
            continue
        counters[title] = counters.get(title, 0) + 1
        n = counters[title]
        if title == "undefined":
            plan_name = f"导入配方 {len(summaries)+1}"
        else:
            plan_name = title if n == 1 else f"{title} 变体{n}"
        remark = (f"CS2UP导入 成本¥{cost:.2f} "
                  f"ROI{roi:+.1f}% 保本率{brk:.1f}%")
        unresolved = sum(1 for m in mats if not m.get("品质"))
        pid = save_material_plan_to_db(plan_name, mats, remark=remark)
        unres_hint = (f"，{unresolved}件未解析品质" if unresolved else "")
        summaries.append(
            f"[{pid}] {plan_name}（10件，{10-unresolved}件命中主CSV{unres_hint}）"
            f" {remark}")
        count += 1
    return count, summaries


# ============================================================
# 真实汰换结果对比（RealReplaceResult）
# ============================================================

def _real_result_items_to_normalized(real_rows_raw) -> list[dict]:
    """把 RealReplaceResult ResultData（可能 list / 包一层 dict）统一成 list[dict]。

    真实 ECO 返回字段名未在编译期 100% 确认，这里兼容多种常见命名：
      - HashName / SPName / OutputHashName / OutputSPName
      - WearValue / OutputWearValue / MinFloat / MaxFloat
      - OutputProbability / Probability / Count
    """
    if isinstance(real_rows_raw, dict):
        # 常见形态：{Rows: [...] / List: [...] / Items: [...]}
        for k in ("Rows", "List", "Items", "Data", "ResultRows",
                  "RealReplaceResults", "Records"):
            if isinstance(real_rows_raw.get(k), list):
                return _real_result_items_to_normalized(real_rows_raw[k])
        return []
    if not isinstance(real_rows_raw, list):
        return []
    out: list[dict] = []
    for r in real_rows_raw:
        if not isinstance(r, dict):
            continue
        hash_name = ""
        for k in ("HashName", "OutputHashName", "GoodsHashName",
                  "MarketHashName", "FullHashName"):
            if r.get(k):
                hash_name = str(r[k]).strip()
                break
        name_cn = ""
        for k in ("SPName", "OutputSPName", "GoodsName", "Name", "SkinName"):
            if r.get(k):
                name_cn = str(r[k]).strip()
                break
        wear_value = None
        for k in ("WearValue", "OutputWearValue", "PaintWear"):
            if r.get(k) not in (None, ""):
                try:
                    wear_value = float(r[k])
                    break
                except (TypeError, ValueError):
                    continue
        wmin = None
        for k in ("MinFloat", "WearMin", "MinWear"):
            if r.get(k) not in (None, ""):
                try:
                    wmin = float(r[k])
                    break
                except (TypeError, ValueError):
                    continue
        wmax = None
        for k in ("MaxFloat", "WearMax", "MaxWear"):
            if r.get(k) not in (None, ""):
                try:
                    wmax = float(r[k])
                    break
                except (TypeError, ValueError):
                    continue
        prob = None
        for k in ("OutputProbability", "Probability", "Ratio", "Rate"):
            if r.get(k) not in (None, ""):
                try:
                    prob = float(r[k])
                    break
                except (TypeError, ValueError):
                    continue
        count = None
        for k in ("Count", "Times", "TotalCount", "ExecuteCount"):
            if r.get(k) not in (None, ""):
                try:
                    count = int(r[k])
                    break
                except (TypeError, ValueError):
                    continue
        out.append({
            "hash_name": hash_name,
            "name_cn": name_cn,
            "wear_float": wear_value,
            "wear_min": wmin,
            "wear_max": wmax,
            "prob": prob,
            "count": count,
            "raw": r,
        })
    return out


def _real_replace_ref_value(real_items: list[dict],
                            *,
                            platforms=None,
                            thread_workers: int = 10) -> tuple[float, list[dict]]:
    """对 RealReplaceResult 的每件产出皮肤查三平台最低价，并按次数/概率加权为真实期望。

    若 items 有 count（实际执行次数）则按 count 加权；
    否则按 prob（官方/历史概率）加权。

    Returns:
        (expected_value, priced_items)
        expected_value: 真实期望售价（元）
        priced_items[i]: 合并了 min_price / best_row / buff_goods_id / c5_market_hash_name
    """
    if not real_items:
        return 0.0, []

    by_hash, by_name_wear = _build_full_hash_to_row_map()
    priced: list[dict] = []

    # 对每件产出构造 (index, item_config, wear_min, wear_max)，并发查价
    items_to_query = []
    for i, item in enumerate(real_items):
        hash_name = (item.get("hash_name") or "").strip()
        name_cn = (item.get("name_cn") or "").strip()
        wear_cn = ""
        wear_f = item.get("wear_float")
        if wear_f is not None:
            wear_cn = get_wear_grade(float(wear_f))
        if not wear_cn and hash_name:
            wear_cn = eco_web_api.exterior_from_hash_name(hash_name)
        row = _lookup_main_csv_row_by_output(
            hash_name, name_cn, wear_cn, by_hash, by_name_wear)
        ic = {
            "buff_goods_id": str(row.get("buff_goods_id") or ""),
            "c5_market_hash_name": hash_name or build_c5_market_hash_name(
                (row.get("市场哈希名称") or "").strip(),
                (row.get("磨损") or "").strip() or wear_cn,
            ),
            "c5_app_id": str(C5_APP_ID),
        }
        wmin = item.get("wear_min")
        wmax = item.get("wear_max")
        if (not wmin or not wmax or wmin >= wmax) and wear_f is not None:
            pw = float(wear_f)
            wmin = max(0.0, pw - 1e-6)
            wmax = min(1.0, pw + 1e-6)
        items_to_query.append((i, ic, wmin, wmax))
        merged = dict(item)
        merged["_buff_goods_id"] = ic["buff_goods_id"]
        merged["_c5_market_hash_name"] = ic["c5_market_hash_name"]
        priced.append(merged)

    with ThreadPoolExecutor(max_workers=min(thread_workers,
                                            max(1, len(items_to_query)))) as pool:
        futs = {
            pool.submit(_query_min_price, ic, wmin, wmax, platforms): idx
            for idx, ic, wmin, wmax in items_to_query
        }
        for fut in as_completed(futs):
            idx = futs[fut]
            try:
                mp, best, _ = fut.result()
            except Exception as e:
                logger.warning("真实产出[%s] 查价失败: %s", idx, e)
                mp, best = None, None
            priced[idx]["min_price"] = float(mp) if mp else None
            priced[idx]["best_row"] = best

    total_count = 0
    total_weight = 0.0
    expected_value = 0.0
    for it in priced:
        p = it.get("min_price") or 0.0
        count = it.get("count")
        prob = it.get("prob")
        if count and count > 0:
            total_count += int(count)
            weight = int(count)
        elif prob is not None and prob > 0:
            weight = float(prob)
            total_weight += weight
        else:
            weight = 0
        expected_value += p * weight

    if total_count > 0:
        expected_value = expected_value / total_count
    elif total_weight > 0 and abs(total_weight - 1.0) > 1e-6:
        expected_value = expected_value / total_weight

    return expected_value, priced


def _detail_result_to_material_dicts(detail: dict) -> list[dict]:
    """从 FormulaDetail ResultData（dict）取出 10 件材料，归一化成可传
    material_query_config / _normalize_materials 的 list[dict] 形式。

    兼容：
      - detail["Materials"]: list[{HashName, SPName, MinFloat, MaxFloat, ...}]
      - detail["FormulaMaterials"] / list
      - 若没有直接字段，再退回 detail 嵌套
    """
    if not isinstance(detail, dict):
        return []
    mats_raw = None
    for k in ("Materials", "FormulaMaterials", "InputMaterials",
              "ReplaceMaterials"):
        v = detail.get(k)
        if isinstance(v, list):
            mats_raw = v
            break
    if mats_raw is None:
        # 可能包一层
        for vk in ("Result", "Data", "FormulaInfo", "Formula"):
            sub = detail.get(vk)
            if isinstance(sub, dict):
                mats_raw = _detail_result_to_material_dicts(sub)
                if mats_raw:
                    return mats_raw
        return []
    by_hash, by_name_wear = _build_full_hash_to_row_map()
    out = []
    for m in mats_raw:
        if not isinstance(m, dict):
            continue
        hn = ""
        for k in ("HashName", "FullHashName", "MarketHashName",
                  "OutputHashName"):
            if m.get(k):
                hn = str(m[k]).strip()
                break
        name_cn = ""
        for k in ("SPName", "Name", "SkinName", "GoodsName"):
            if m.get(k):
                name_cn = str(m[k]).strip()
                break
        wear_cn = eco_web_api.exterior_from_hash_name(hn) or ""
        row = _lookup_main_csv_row_by_output(
            hn, name_cn, wear_cn, by_hash, by_name_wear)
        wmin = None
        for k in ("MinFloat", "MinWear", "WearMin"):
            if m.get(k) not in (None, ""):
                try:
                    wmin = float(m[k])
                    break
                except (TypeError, ValueError):
                    continue
        wmax = None
        for k in ("MaxFloat", "MaxWear", "WearMax"):
            if m.get(k) not in (None, ""):
                try:
                    wmax = float(m[k])
                    break
                except (TypeError, ValueError):
                    continue
        if (wmin is None or wmax is None) and row:
            # 主 CSV 行兜底：磨损档中文 → 官方档位范围（_parse_wear_range
            # 已不存在，此前调用会在运行到这里时 NameError）
            off = _official_wear_range(
                (row.get("磨损") or "") if isinstance(row, dict) else "")
            if off:
                if wmin is None:
                    wmin = off[0]
                if wmax is None:
                    wmax = off[1]
        merged = dict(row) if isinstance(row, dict) else {}
        merged.update({
            "市场哈希名称": (row.get("市场哈希名称") if isinstance(row, dict) else "") or "",
            "皮肤名称": (row.get("皮肤名称") if isinstance(row, dict) else "") or name_cn or "",
            "磨损": (row.get("磨损") if isinstance(row, dict) else "") or wear_cn or "",
            "磨损_min": float(wmin) if wmin is not None else 0.0,
            "磨损_max": float(wmax) if wmax is not None else 1.0,
            "HashName": hn,
            "SPName": name_cn,
        })
        # 强制 material_query_config 走 磨损_min/max
        merged["user_wear_min"] = merged["磨损_min"]
        merged["user_wear_max"] = merged["磨损_max"]
        out.append(merged)
    return out


def real_replace_compare(formula_id: str, *,
                         platforms=None,
                         use_cache: bool = True) -> dict:
    """编排：FormulaDetail → RealReplaceResult → 实时查价，输出真实成本/期望。

    Args:
        formula_id: ECO 平台保存配方后的 FormulaID
        platforms: price_fetcher.query_all 平台列表（None=三平台）
        use_cache: 是否使用 eco_web_api 各自接口的 LRU

    Returns:
        dict: {
          formula_id,
          detail: FormulaDetail 原始 ResultData,
          materials: list[dict] 从 detail 归一化后的 10 件材料,
          material_cost: 10 件材料 Σ实时最低价,
          material_price_list: list[float] 每件材料最低售价,
          real_rows_raw: RealReplaceResult 原始结果,
          real_rows: list[dict] 归一化真实产出（含 min_price）,
          real_expected_value: 按真实执行次数/概率加权的期望售价,
          profit: real_expected_value - material_cost,
          keep_rate: 保本率（≥1 为稳赚）,
        }
    """
    from core import eco_web_api

    fid = (formula_id or "").strip()
    if not fid:
        raise ValueError("real_replace_compare formula_id 不能为空")

    detail = eco_web_api.formula_detail(fid, use_cache=use_cache) or {}
    materials = _detail_result_to_material_dicts(detail)

    # --- 材料成本：复用实时查价 ---
    material_cost = 0.0
    material_price_list: list[float] = []
    material_details: list[dict] = []
    if materials:
        normalized = _normalize_materials(materials)
        material_cost, material_price_list, material_details = \
            _materials_cost_via_price_fetcher(normalized, platforms=platforms)

    # --- 真实汰换结果 ---
    real_raw = eco_web_api.real_replace_result(fid, use_cache=use_cache)
    real_items = _real_result_items_to_normalized(real_raw)

    real_ev, priced_items = _real_replace_ref_value(
        real_items, platforms=platforms)

    keep_rate = _calc_keep_rate(priced_items, material_cost)
    profit = real_ev - material_cost

    return {
        "formula_id": fid,
        "detail": detail,
        "materials": materials,
        "material_cost": float(material_cost),
        "material_price_list": material_price_list,
        "material_details": material_details,
        "real_rows_raw": real_raw,
        "real_rows": priced_items,
        "real_expected_value": float(real_ev),
        "profit": float(profit),
        "keep_rate": float(keep_rate),
    }


def match_materials_from_inventory(target_item_name: str,
                                    target_wear_min: float,
                                    target_wear_max: float,
                                    top_n: int = 10) -> list:
    """从库存中匹配符合目标物品名称和磨损区间的材料。

    用于自定义方案：用户指定目标物品名称 + 磨损范围，
    从库存中找出名称匹配（子串）且磨损在区间内的物品。

    Returns:
        list[dict]: 匹配到的库存物品，含 asset_id/goods_name/paint_wear/hash_name
    """
    ensure_inventory_table()
    conn = get_conn()
    conn.row_factory = sqlite3.Row
    # 名称模糊匹配 + 磨损区间过滤
    keyword = f"%{target_item_name}%"
    rows = conn.execute("""
        SELECT asset_id, goods_name, hash_name, paint_wear, paint_seed
        FROM inventory
        WHERE (goods_name LIKE ? OR hash_name LIKE ?)
          AND paint_wear >= ? AND paint_wear <= ?
        ORDER BY paint_wear ASC
        LIMIT ?
    """, (keyword, keyword, target_wear_min, target_wear_max, top_n)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def log_taihuan_action(plan_id: int, action: str, asset_id: str = "",
                       item_name: str = "", status: str = "", remark: str = ""):
    """记录一条汰换执行日志。"""
    ensure_inventory_table()
    conn = get_conn()
    from datetime import datetime
    now = datetime.now().isoformat(timespec="seconds")
    conn.execute("""
        INSERT INTO taihuan_log
            (plan_id, timestamp, action, asset_id, item_name, status, remark)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (plan_id, now, action, asset_id, item_name, status, remark))
    conn.commit()
    conn.close()


def plan_to_dict(plan: TaihuanPlan) -> dict:
    """把 TaihuanPlan 转成可序列化的 dict（含 materials 列表）。"""
    d = asdict(plan)
    d["materials"] = [
        {
            "asset_id": m.asset_id,
            "goods_name": m.goods_name,
            "paint_wear": m.paint_wear,
            "skin_name": m.target_skin_name,
        } for m in plan.materials
    ]
    return d
