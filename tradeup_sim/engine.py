"""CS2 汰换合同核心算法。

规则（严格遵循官方机制）：
  1. 常规汰换：10 件同稀有度材料 → 1 件高一级稀有度皮肤。
  2. 隐秘升刀：5 件隐秘级 → 1 件稀有特殊物品（刀/手套），可配置开关。
  3. 输出池 = 所有材料所属收藏品中，高一级稀有度的全部皮肤，去重。
     每个收藏品的概率质量 = 该收藏品材料数 / 总材料数，
     收藏品内的高一级皮肤平分该部分质量（非全局等概率）。
  4. 磨损：每件材料磨损归一化到 [0,1]（按该皮肤自身 min/max_float），
     10 件取平均，再线性映射到产物皮肤的 [min_float, max_float]。
  5. StatTrak：10 件全为 StatTrak → 产出 StatTrak；否则普通。
     纪念品可作材料但产出必为普通。
  6. 禁用：消费级（无下级可升）、违禁品。

边界处理：
  - 材料数量不足（≠10，隐秘升刀 ≠5）→ ValueError
  - 稀有度不一致 → ValueError
  - 混合 StatTrak → 产出普通（合法，不报错）
  - 无上级稀有度（如隐秘级未开升刀）→ ValueError
  - 输出池为空 → ValueError
"""
from __future__ import annotations

import random
from typing import List, Optional, Dict, Tuple

from .models import (
    Skin, Material, TradeUpResult, QUALITY_LEVEL, QUALITY_ORDER,
    next_quality, get_wear_grade, WEAR_GRADES,
)
from . import database as db


# ============================================================
# 校验
# ============================================================
def validate_materials(materials: List[Material],
                       allow_covert_knife: bool = True) -> str:
    """校验材料合法性；返回错误信息（空串=合法）。"""
    n = len(materials)
    if n == 0:
        return "材料为空"
    qualities = {m.quality for m in materials}
    if len(qualities) > 1:
        return f"材料稀有度不一致：{qualities}"
    q = next(iter(qualities))
    if q == "消费级":
        return "消费级材料无法汰换（无下级可升）"
    if q == "违禁品":
        return "违禁品已从游戏移除，禁用"
    # 数量校验
    if q == "隐秘级":
        if allow_covert_knife:
            if n != 5:
                return "隐秘升刀需要恰好 5 件材料"
        else:
            return "隐秘级已是常规汰换最高级（开启隐秘升刀可用 5 件换刀）"
    else:
        if n != 10:
            return f"{q} 材料需要恰好 10 件"
    return ""


# ============================================================
# 输出池
# ============================================================
def build_output_pool(materials: List[Material],
                      output_quality: str,
                      is_stattrak: bool) -> List[Skin]:
    """构建输出池：所有材料收藏品中，高一级稀有度的皮肤，去重。

    注意：概率不是全局等概率！每个收藏品的概率质量 = 该收藏品材料数 / 总材料数，
    该收藏品内的高一级皮肤平分这部分质量。
    详见 get_output_probabilities()。
    """
    collections = {m.collection for m in materials}
    pool: List[Skin] = []
    seen: set = set()
    for coll in collections:
        skins = db.get_skins_by_collection_quality(coll, output_quality, is_stattrak)
        for s in skins:
            if s.name in seen:
                continue
            seen.add(s.name)
            pool.append(s)
    return pool


def get_output_probabilities(materials: List[Material],
                             output_quality: str,
                             is_stattrak: bool) -> Dict[str, float]:
    """计算每个产物皮肤的精确概率（按收藏品材料数量加权）。

    CS2 汰换机制：每件材料贡献 1/N 概率质量给所属收藏品，
    该收藏品内的高一级皮肤平分这部分质量。
    N = 10（常规）或 5（隐秘升刀）。

    返回 { skin_name: probability }
    """
    from collections import Counter
    n = len(materials)
    coll_counts = Counter(m.collection for m in materials)
    probs: Dict[str, float] = {}
    for coll, count in coll_counts.items():
        skins = db.get_skins_by_collection_quality(coll, output_quality, is_stattrak)
        # 去重同名皮肤
        seen = set()
        unique_skins = []
        for s in skins:
            if s.name not in seen:
                seen.add(s.name)
                unique_skins.append(s)
        if not unique_skins:
            continue
        coll_mass = count / n
        per_skin = coll_mass / len(unique_skins)
        for s in unique_skins:
            probs[s.name] = probs.get(s.name, 0.0) + per_skin
    return probs


def build_knife_pool(is_stattrak: bool) -> List[Skin]:
    """隐秘升刀的输出池：所有稀有特殊物品（刀/手套）。"""
    conn = db.get_conn()
    rows = conn.execute(
        """SELECT DISTINCT name, collection, quality, min_float, max_float,
                  is_stattrak, market_hash, buff_goods_id, price
           FROM skins WHERE quality='稀有特殊物品' AND is_stattrak=?
           ORDER BY name""",
        (1 if is_stattrak else 0),
    ).fetchall()
    conn.close()
    out: List[Skin] = []
    seen: set = set()
    for r in rows:
        if r["name"] in seen:
            continue
        seen.add(r["name"])
        out.append(Skin(
            collection=r["collection"], name=r["name"], quality=r["quality"],
            min_float=r["min_float"], max_float=r["max_float"],
            is_stattrak=bool(r["is_stattrak"]),
            market_hash_name=r["market_hash"] or "",
            buff_goods_id=r["buff_goods_id"] or 0,
            price=r["price"] or 0.0,
        ))
    return out


# ============================================================
# 磨损计算
# ============================================================
def calculate_output_wear(materials: List[Material],
                          output_skin: Skin) -> float:
    """归一化 → 平均 → 映射到产物 [min, max]。

    每件材料：norm = (wear - skin.min_float) / (skin.max_float - skin.min_float)
    avg = mean(norm_i)
    output_wear = output.min_float + avg * (output.max_float - output.min_float)
    """
    norms = []
    for m in materials:
        span = m.skin.max_float - m.skin.min_float
        if span <= 0:
            norms.append(0.0)
        else:
            n = (m.wear - m.skin.min_float) / span
            norms.append(max(0.0, min(1.0, n)))
    avg = sum(norms) / len(norms)
    out = output_skin.min_float + avg * (output_skin.max_float - output_skin.min_float)
    return max(0.0, min(1.0, out))


# ============================================================
# 单次模拟
# ============================================================
def simulate(materials: List[Material],
             seed: Optional[int] = None,
             allow_covert_knife: bool = True) -> TradeUpResult:
    """执行一次汰换模拟，返回产出结果。"""
    err = validate_materials(materials, allow_covert_knife)
    if err:
        raise ValueError(err)

    rng = random.Random(seed)
    q = materials[0].quality

    # --- StatTrak 判定：全有或全无；纪念品强制普通 ---
    has_souvenir = any(m.is_souvenir for m in materials)
    all_st = all(m.is_stattrak for m in materials)
    output_is_st = (all_st and not has_souvenir)

    # --- 产出稀有度 ---
    if q == "隐秘级" and allow_covert_knife:
        output_quality = "稀有特殊物品"
    else:
        output_quality = next_quality(q)
        if output_quality is None:
            raise ValueError(f"{q} 没有更高稀有度")

    # --- 输出池 ---
    if output_quality == "稀有特殊物品":
        pool = build_knife_pool(output_is_st)
        probs = {s.name: 1.0 / len(pool) for s in pool}  # 刀池等概率
    else:
        pool = build_output_pool(materials, output_quality, output_is_st)
        probs = get_output_probabilities(materials, output_quality, output_is_st)
        if not pool:
            pool = build_output_pool(materials, output_quality, not output_is_st)
            if pool:
                output_is_st = not output_is_st
                probs = get_output_probabilities(materials, output_quality, output_is_st)
    if not pool:
        raise ValueError(
            f"输出池为空：材料所属收藏品中没有 {output_quality} 皮肤")

    # --- 按加权概率抽取产物 ---
    names = [s.name for s in pool]
    weights = [probs.get(n, 0.0) for n in names]
    total_w = sum(weights)
    if total_w <= 0:
        output_skin = rng.choice(pool)
        prob = 1.0 / len(pool)
    else:
        # 用权重做随机选择
        r = rng.random() * total_w
        cum = 0.0
        output_skin = pool[-1]
        for s, w in zip(pool, weights):
            cum += w
            if r <= cum:
                output_skin = s
                break
        prob = probs.get(output_skin.name, 0.0)

    # --- 磨损 ---
    out_wear = calculate_output_wear(materials, output_skin)

    # --- 成本与产出价 ---
    cost = sum(m.price for m in materials)
    out_wear_grade = get_wear_grade(out_wear)
    out_price = db.get_skin_price(output_skin.name, out_wear_grade, output_is_st)

    return TradeUpResult(
        output_skin=output_skin,
        output_wear=out_wear,
        probability=prob,
        material_cost=cost,
        output_price=out_price,
        is_stattrak=output_is_st,
        seed=seed,
    )


# ============================================================
# 批量模拟 + 统计
# ============================================================
def simulate_batch(materials: List[Material],
                   times: int = 1000,
                   base_seed: Optional[int] = None,
                   allow_covert_knife: bool = True) -> Dict:
    """批量模拟，返回结果列表 + 统计摘要。"""
    results: List[TradeUpResult] = []
    for i in range(times):
        seed = (base_seed + i) if base_seed is not None else None
        results.append(simulate(materials, seed=seed,
                                allow_covert_knife=allow_covert_knife))

    # 统计：按产物皮肤聚合次数、EV
    agg: Dict[str, Dict] = {}
    total_cost = results[0].material_cost if results else 0.0
    ev = 0.0
    for r in results:
        key = r.output_skin.name
        if key not in agg:
            agg[key] = {
                "skin": r.output_skin,
                "count": 0,
                "prob": 0.0,
                "avg_wear": 0.0,
                "avg_price": 0.0,
                "is_stattrak": r.is_stattrak,
            }
        agg[key]["count"] += 1
        agg[key]["avg_wear"] += r.output_wear
        agg[key]["avg_price"] += r.output_price
        ev += r.output_price
    ev = ev / len(results) if results else 0.0
    for k, v in agg.items():
        v["prob"] = v["count"] / len(results)
        v["avg_wear"] /= v["count"]
        v["avg_price"] /= v["count"]

    profit = ev - total_cost
    roi = (profit / total_cost * 100.0) if total_cost > 0 else 0.0

    return {
        "results": results,
        "summary": {
            "times": times,
            "material_cost": total_cost,
            "ev": round(ev, 2),
            "profit": round(profit, 2),
            "roi_pct": round(roi, 2),
            "output_pool_size": len(agg),
            "per_output": agg,
        },
    }


# ============================================================
# 概率分布（不抽随机，直接列全输出池及精确概率 + 理论 EV）
# ============================================================
def get_probability_distribution(materials: List[Material],
                                 allow_covert_knife: bool = True) -> Dict:
    """返回完整输出池的精确概率分布 + 理论 EV（不依赖随机抽样）。"""
    err = validate_materials(materials, allow_covert_knife)
    if err:
        raise ValueError(err)

    q = materials[0].quality
    has_souvenir = any(m.is_souvenir for m in materials)
    all_st = all(m.is_stattrak for m in materials)
    output_is_st = (all_st and not has_souvenir)

    if q == "隐秘级" and allow_covert_knife:
        output_quality = "稀有特殊物品"
    else:
        output_quality = next_quality(q)
        if output_quality is None:
            raise ValueError(f"{q} 没有更高稀有度")

    if output_quality == "稀有特殊物品":
        pool = build_knife_pool(output_is_st)
        probs = {s.name: 1.0 / len(pool) for s in pool}
    else:
        pool = build_output_pool(materials, output_quality, output_is_st)
        probs = get_output_probabilities(materials, output_quality, output_is_st)
        if not pool:
            pool = build_output_pool(materials, output_quality, not output_is_st)
            if pool:
                output_is_st = not output_is_st
                probs = get_output_probabilities(materials, output_quality, output_is_st)
    if not pool:
        raise ValueError(f"输出池为空：没有 {output_quality} 皮肤")

    cost = sum(m.price for m in materials)
    rows = []
    ev = 0.0
    for s in pool:
        est_wear = calculate_output_wear(materials, s)
        grade = get_wear_grade(est_wear)
        price = db.get_skin_price(s.name, grade, output_is_st)
        prob = probs.get(s.name, 0.0)
        ev += price * prob
        rows.append({
            "skin": s,
            "probability": prob,
            "est_wear": est_wear,
            "wear_grade": grade,
            "price": price,
            "is_stattrak": output_is_st,
        })
    rows.sort(key=lambda x: x["price"], reverse=True)
    profit = ev - cost
    roi = (profit / cost * 100.0) if cost > 0 else 0.0
    return {
        "output_quality": output_quality,
        "pool_size": len(pool),
        "material_cost": cost,
        "theoretical_ev": round(ev, 2),
        "profit": round(profit, 2),
        "roi_pct": round(roi, 2),
        "rows": rows,
    }


# ============================================================
# 配方优化器：给定目标产物 + 期望磨损，求成本最低的 10 件材料配方
# ============================================================
def _grade_wear_range(grade: str) -> Tuple[float, float]:
    """返回某磨损档的官方区间 [lo, hi)。"""
    for name, lo, hi in WEAR_GRADES:
        if name == grade:
            return lo, hi
    return 0.0, 1.0


def _material_options(material_quality: str, collections: List[str],
                      is_stattrak: bool,
                      live_prices: Optional[Dict] = None) -> List[Dict]:
    """枚举所有可选的（材料皮肤, 磨损档）组合及其价格与归一化磨损区间。

    :param live_prices: dict，key=(skin_name, grade) → 实时价格；
                        若提供则优先使用，否则回退到 DB 静态价格。
    返回列表每项：{skin, grade, n_lo, n_hi, price, wear_lo, wear_hi,
                   market_ids, is_target_coll}
    """
    out = []
    for coll in collections:
        skins = db.get_skins_by_collection_quality(coll, material_quality, is_stattrak)
        for s in skins:
            span = s.max_float - s.min_float
            if span <= 0:
                continue
            for grade, glo, ghi in WEAR_GRADES:
                alo = max(s.min_float, glo)
                ahi = min(s.max_float, ghi)
                if alo >= ahi:
                    continue  # 该皮肤无此磨损档
                n_lo = (alo - s.min_float) / span
                n_hi = (ahi - s.min_float) / span
                # 价格：优先实时价，回退 DB 静态价
                key = (s.name, grade)
                if live_prices and key in live_prices and live_prices[key] > 0:
                    price = live_prices[key]
                else:
                    price = db.get_skin_price(s.name, grade, is_stattrak)
                if price <= 0:
                    # 无价格数据：设为极大值参与 DP（不会被选中，但保证候选池不缩水）
                    price = 99999.0
                market_ids = db.get_skin_market_ids(s.name, grade, is_stattrak)
                out.append({
                    "skin": s, "grade": grade,
                    "n_lo": n_lo, "n_hi": n_hi,
                    "price": price,
                    "wear_lo": alo, "wear_hi": ahi,
                    "market_ids": market_ids,
                    "has_real_price": price < 99999.0,
                    "collection": coll,
                })
    return out


def prepare_options(target_skin: Skin,
                    is_stattrak: bool = False) -> Tuple[List[Dict], List[str], List[str]]:
    """返回目标皮肤的所有候选（材料皮肤, 磨损档）选项 + 目标收藏品列表 + 全部收藏品列表。

    GUI 可调用此函数获取候选列表，查询实时价格后，再把 live_prices
    传给 optimize_recipe。

    :return: (options, target_collections, all_collections)
    """
    target_q = target_skin.quality
    if target_q not in QUALITY_LEVEL:
        raise ValueError(f"未知稀有度: {target_q}")
    lvl = QUALITY_LEVEL[target_q]
    if lvl <= 0:
        raise ValueError(f"{target_q} 没有下级材料")
    material_quality = QUALITY_ORDER[lvl - 1]
    target_collections = _find_collections_with_skin(target_skin, is_stattrak)
    if not target_collections:
        raise ValueError(f"未找到包含 {target_skin.name} 的收藏品")
    # 全部有该材料稀有度的收藏品
    all_collections = db.get_collections_with_quality(material_quality, is_stattrak)
    options = _material_options(material_quality, all_collections, is_stattrak)
    # 标记是否来自目标收藏品
    target_set = set(target_collections)
    for opt in options:
        opt["is_target_coll"] = opt["collection"] in target_set
    return options, target_collections, all_collections


def _diversify_candidates(options: List[Dict], max_cands: int) -> List[Dict]:
    """混合最便宜和最低磨损的候选，且保证目标收藏品候选一定被包含。"""
    if len(options) <= max_cands:
        return options
    target_opts = [o for o in options if o.get("is_target_coll")]
    other_opts = [o for o in options if not o.get("is_target_coll")]

    # 保证至少保留一半目标候选（最多 max_cands//2）
    n_target_keep = min(len(target_opts), max(1, max_cands // 2))
    target_opts.sort(key=lambda o: (o["price"], o["skin"].name))
    chosen_target = target_opts[:n_target_keep]

    # 剩余名额给非目标候选（一半最便宜 + 一半最低磨损）
    remaining = max_cands - len(chosen_target)
    chosen_ids = {id(o) for o in chosen_target}

    by_price = sorted(other_opts, key=lambda o: (o["price"], o["skin"].name))
    by_float = sorted(other_opts,
                      key=lambda o: (o["n_lo"], o["price"]))
    chosen_other = {}
    half = max(1, remaining // 2)
    for o in by_price:
        if len(chosen_other) >= half:
            break
        if id(o) not in chosen_ids:
            chosen_other[id(o)] = o
    for o in by_float:
        if len(chosen_other) >= remaining:
            break
        if id(o) not in chosen_ids and id(o) not in chosen_other:
            chosen_other[id(o)] = o
    for o in by_price:
        if len(chosen_other) >= remaining:
            break
        if id(o) not in chosen_ids and id(o) not in chosen_other:
            chosen_other[id(o)] = o

    out = chosen_target + list(chosen_other.values())
    out.sort(key=lambda o: (o["price"],))
    return out


def _dp_topk(options: List[Dict], req_avg_n: float,
             tolerance: float = 0.005,
             require_target: bool = True,
             top_k: int = 5,
             target_count: Optional[int] = None) -> List[List[Dict]]:
    """Top-K DP（稠密数组版，正确实现）。

    每个 (target_count, count, float_bucket) 单元格保留前 K 条最便宜路径。
    target_count 跟踪已选目标收藏品材料数量（0~10）。

    :param target_count: 若指定，则只返回恰好含 target_count 件目标材料的配方；
                         None=不限制数量（只要 ≥1 即可）。
    """
    STEP = 0.005
    N_BUCKETS = int(10.0 / STEP) + 1  # 2001
    n = 10

    # dp[c][i][b] = list of (cost, path_tuple)，c = 目标材料数量
    dp = [[[[] for _ in range(N_BUCKETS)] for _ in range(n + 1)]
          for _ in range(n + 1)]
    dp[0][0][0] = [(0.0, ())]

    target_sum = req_avg_n * n
    target_bucket = int(round(target_sum / STEP))
    tol_buckets = max(1, int(round(tolerance * n / STEP)))

    # 用 n_lo（该磨损档的最小归一化磨损）作为分桶值。
    # 因为 _chosen_to_materials 会把每个材料夹取到 req_avg_n，
    # 材料实际可贡献 [n_lo, n_hi] 内任意值。用 n_lo 让 DP 知道
    # "最少贡献多少"，只要 sum(n_lo) ≤ target 就可调高达标，
    # 从而优先选更便宜的高磨损档（FT/WW/BS），避免不必要的 FN。
    opt_buckets = [int(round(o["n_lo"] / STEP)) for o in options]
    opt_n_hi = [o["n_hi"] for o in options]
    opt_is_target = [1 if o.get("is_target_coll") else 0 for o in options]
    opt_prices = [o["price"] for o in options]
    n_opts = len(options)

    def _offer(cell: list, cost: float, path: tuple):
        path_set = set(path)
        for ex_cost, ex_path in cell:
            if set(ex_path) == path_set:
                return
        cell.append((cost, path))
        cell.sort(key=lambda t: t[0])
        if len(cell) > top_k:
            del cell[top_k:]

    for i in range(n):
        for c in range(i + 1):  # 最多 i 件目标材料
            cur_layer = dp[c][i]
            for b in range(N_BUCKETS):
                paths = cur_layer[b]
                if not paths:
                    continue
                for oi in range(n_opts):
                    nb = b + opt_buckets[oi]
                    if nb >= N_BUCKETS:
                        continue
                    is_tgt = opt_is_target[oi]
                    nc = c + is_tgt
                    if nc > n:
                        continue
                    nxt = dp[nc][i + 1]
                    price = opt_prices[oi]
                    for cost, path in paths:
                        if oi in path:
                            continue
                        _offer(nxt[nb], cost + price, path + (oi,))

    # 收集：sum(n_lo) ≤ target（从下方可达），再过滤上限不可达的
    # 注意：sum(n_lo) 不能超过 target，因为 _chosen_to_materials 无法把磨损
    # 降到 n_lo 以下。tolerance 仅用于上限检查和分桶精度补偿。
    results = []
    lo = 0
    hi = min(N_BUCKETS - 1, target_bucket + 1)  # +1 补偿分桶舍入误差
    upper_min = target_sum - tolerance * n  # sum(n_hi) 至少要达到这个值
    # 确定要收集的 target_count 范围
    if target_count is not None:
        c_range = [target_count]
    elif require_target:
        c_range = range(1, n + 1)
    else:
        c_range = range(n + 1)
    for c in c_range:
        for b in range(lo, hi + 1):
            for cost, path in dp[c][n][b]:
                if len(path) == n:
                    # 下限检查：sum(n_lo) 必须 ≤ target（分桶可能有误差，精确验证）
                    sum_lo = sum(options[oi]["n_lo"] for oi in path)
                    if sum_lo > target_sum + 1e-6:
                        continue
                    # 上限可行性检查：所有材料的 n_hi 之和必须 ≥ target - tol
                    sum_hi = sum(opt_n_hi[oi] for oi in path)
                    if sum_hi >= upper_min:
                        results.append((cost, [options[oi] for oi in path]))
    # 按成本升序排列
    results.sort(key=lambda x: x[0])
    return [r[1] for r in results]


def _dp_topk_gpu(options: List[Dict], req_avg_n: float,
                 tolerance: float = 0.005,
                 require_target: bool = True,
                 top_k: int = 5,
                 target_count: Optional[int] = None) -> List[List[Dict]]:
    """Top-K DP 的 GPU 张量版（PyTorch）。

    与 _dp_topk 的语义差异：
      - 允许同一 (皮肤, 磨损档) 选项被重复使用（CS2 汰换合同允许重复材料），
        这使得 DP 可以用稠密张量高效计算。
      - 状态用 (top_k, target_count, bucket) 张量表示，转移用张量移位 +
        torch.topk 取前 K 小。
      - 回溯时记录每步所用的选项索引，还原 10 件材料。

    若无 CUDA，自动回退到 CPU 张量。
    """
    import sys
    import os
    # 支持项目目录下的隔离 torch 安装（沙箱环境下 site-packages 不可写）
    _torch_lib = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "_torch_lib")
    if os.path.isdir(_torch_lib) and _torch_lib not in sys.path:
        sys.path.insert(0, _torch_lib)
        # 自定义安装路径需手动注册 DLL 目录
        _torch_dll = os.path.join(_torch_lib, "torch", "lib")
        if os.path.isdir(_torch_dll):
            try:
                os.add_dll_directory(_torch_dll)
            except (OSError, AttributeError):
                pass
    import torch
    if torch.cuda.is_available():
        device = torch.device("cuda")
        print(f"[DP] GPU 加速启用: {torch.cuda.get_device_name(0)} (CUDA {torch.version.cuda})")
    else:
        device = torch.device("cpu")
        print(f"[DP] CUDA 不可用，使用 CPU 运行 PyTorch")

    STEP = 0.005
    N_BUCKETS = int(10.0 / STEP) + 1  # 2001
    n = 10
    INF = 1e18

    # 用 n_lo（最小归一化磨损）作为分桶值，理由同 _dp_topk：
    # 材料可贡献 [n_lo, n_hi] 内任意值，用 n_lo 让 DP 优先选便宜的高磨损档。
    opt_buckets = [int(round(o["n_lo"] / STEP)) for o in options]
    opt_n_hi = [o["n_hi"] for o in options]
    opt_is_target = [1 if o.get("is_target_coll") else 0 for o in options]
    opt_prices = [float(o["price"]) for o in options]
    n_opts = len(options)

    # 当前层 dp: (top_k, n+1, N_BUCKETS)
    cur = torch.full((top_k, n + 1, N_BUCKETS), INF, device=device, dtype=torch.float32)
    cur[0, 0, 0] = 0.0

    # 存储每层的父指针，用于回溯: parents[i] = (parent_opt, parent_k)
    # parent_opt[k, c, b] = 该格第 k 优解使用的选项索引
    # parent_k[k, c, b]  = 来自上一层的第几个优解
    parents_opt = []
    parents_k = []

    # 预计算选项的张量属性（用于向量化转移）
    bkt_tensor = torch.tensor(opt_buckets, device=device, dtype=torch.long)
    tgt_tensor = torch.tensor(opt_is_target, device=device, dtype=torch.long)
    prc_tensor = torch.tensor(opt_prices, device=device, dtype=torch.float32)
    arange_b = torch.arange(N_BUCKETS, device=device)
    arange_c = torch.arange(n + 1, device=device)

    for i in range(n):
        # 向量化：一次性为所有选项构造移位后的候选成本
        # cur: (top_k, n+1, N_BUCKETS)
        # 扩展为 (n_opts, top_k, n+1, N_BUCKETS)
        cur_exp = cur.unsqueeze(0).expand(n_opts, -1, -1, -1)

        # b 维移位索引: result[b] = cur[b - bkt_oi]
        b_idx = arange_b.unsqueeze(0) - bkt_tensor.unsqueeze(1)  # (n_opts, N_BUCKETS)
        b_mask = b_idx >= 0
        b_idx = b_idx.clamp(min=0)
        b_idx_exp = b_idx.unsqueeze(1).unsqueeze(1).expand(-1, top_k, n + 1, -1)

        # c 维移位索引
        c_idx = arange_c.unsqueeze(0) - tgt_tensor.unsqueeze(1)  # (n_opts, n+1)
        c_mask = c_idx >= 0
        c_idx = c_idx.clamp(min=0)
        c_idx_exp = c_idx.unsqueeze(1).unsqueeze(-1).expand(-1, top_k, -1, N_BUCKETS)

        # 先移 c 再移 b
        shifted = torch.gather(cur_exp, 2, c_idx_exp)
        shifted = torch.gather(shifted, 3, b_idx_exp)

        # 合并 mask: (n_opts, n+1, N_BUCKETS) -> (n_opts, 1, n+1, N_BUCKETS)
        mask = (c_mask.unsqueeze(2) & b_mask.unsqueeze(1)).unsqueeze(1)
        shifted = shifted.masked_fill(~mask, INF)

        # 加价格
        shifted = shifted + prc_tensor.view(n_opts, 1, 1, 1)

        # reshape 为 (n_opts * top_k, n+1, N_BUCKETS)
        cands = shifted.reshape(n_opts * top_k, n + 1, N_BUCKETS)

        # 沿 dim=0 取 top_k 最小
        topk_vals, topk_idx = torch.topk(cands, top_k, dim=0, largest=False)
        nxt = topk_vals
        p_opt = (topk_idx // top_k).to(torch.int32)
        p_k = (topk_idx % top_k).to(torch.int32)
        parents_opt.append(p_opt)
        parents_k.append(p_k)
        cur = nxt

    # 收集：sum(n_lo) ≤ target（从下方可达），再过滤上限不可达的
    # 注意：sum(n_lo) 不能超过 target，因为 _chosen_to_materials 无法把磨损
    # 降到 n_lo 以下。tolerance 仅用于上限检查和分桶精度补偿。
    target_sum = req_avg_n * n
    target_bucket = int(round(target_sum / STEP))
    lo = 0
    hi = min(N_BUCKETS - 1, target_bucket + 1)  # +1 补偿分桶舍入误差
    upper_min = target_sum - tolerance * n  # sum(n_hi) 下限

    if target_count is not None:
        c_range = [target_count]
    elif require_target:
        c_range = range(1, n + 1)
    else:
        c_range = range(n + 1)

    cur_cpu = cur.cpu().numpy()
    # 把父指针转成 numpy 数组，加速回溯时的索引
    import numpy as np
    po_np = [t.cpu().numpy() for t in parents_opt]
    pk_np = [t.cpu().numpy() for t in parents_k]

    # 先收集所有有效 (cost, c, b, k)，按成本排序后只回溯前 MAX_RESULTS 条
    # 避免回溯数千条非最优路径导致性能崩塌
    MAX_RESULTS = max(top_k * 20, 100)
    candidates = []
    for c in c_range:
        for b in range(lo, hi + 1):
            for k in range(top_k):
                cost = float(cur_cpu[k, c, b])
                if cost < INF:
                    candidates.append((cost, c, b, k))
    candidates.sort(key=lambda x: x[0])
    candidates = candidates[:MAX_RESULTS]

    results = []
    for cost, c, b, k in candidates:
        # 回溯
        path = []
        cc, bb, kk = c, b, k
        for layer in range(n - 1, -1, -1):
            oi = int(po_np[layer][kk, cc, bb])
            pk = int(pk_np[layer][kk, cc, bb])
            path.append(oi)
            cc -= opt_is_target[oi]
            bb -= opt_buckets[oi]
            kk = pk
        path.reverse()
        # 下限检查：sum(n_lo) 必须 ≤ target（分桶可能有误差，精确验证）
        sum_lo = sum(options[oi]["n_lo"] for oi in path)
        if sum_lo > target_sum + 1e-6:
            continue
        # 上限可行性检查
        sum_hi = sum(opt_n_hi[oi] for oi in path)
        if sum_hi >= upper_min:
            results.append((cost, [options[oi] for oi in path]))
    # 按成本升序排列
    results.sort(key=lambda x: x[0])
    return [r[1] for r in results]


def _local_improve(chosen: List[Dict], options: List[Dict],
                   req_avg_n: float, passes: int = 3) -> List[Dict]:
    """局部搜索微调：尝试用未使用的选项替换当前材料，保留更优解。

    评估标准：材料总成本更低（且保持至少一件目标收藏品材料）。
    """
    current = list(chosen)
    used_ids = {id(o) for o in current}
    unused = [o for o in options if id(o) not in used_ids]

    def _has_target(items):
        return any(o.get("is_target_coll") for o in items)

    def _wear_feasible(items):
        """检查是否可调磨损达到目标：sum(n_lo) ≤ target ≤ sum(n_hi)。"""
        n_lo_sum = sum(o["n_lo"] for o in items)
        n_hi_sum = sum(o["n_hi"] for o in items)
        m = len(items)
        return (n_lo_sum - 1e-9 <= req_avg_n * m <= n_hi_sum + 1e-9)

    def _score(items):
        # 分数 = -成本（越低越好），磨损可行性由 _wear_feasible 保证
        cost = sum(o["price"] for o in items)
        return -cost

    best_score = _score(current)
    for _ in range(passes):
        improved = False
        for i in range(len(current)):
            for j, cand in enumerate(unused):
                trial = list(current)
                trial[i] = cand
                if not _has_target(trial):
                    continue
                if not _wear_feasible(trial):
                    continue
                s = _score(trial)
                if s > best_score + 1e-9:
                    unused[j] = current[i]
                    current = trial
                    best_score = s
                    improved = True
                    break
            if improved:
                break
        if not improved:
            break
    return current


def _calc_recipe_ev(materials: List[Material], is_stattrak: bool,
                    output_prices: Optional[Dict] = None,
                    target_skin_name: str = "") -> Dict:
    """计算配方的输出池、EV、利润、保本率。

    :param output_prices: dict key=(skin_name, wear_grade) → 价格；
                          None 则用 DB 静态价。
    :param target_skin_name: 目标皮肤名，用于高亮标记。
    """
    q = materials[0].quality
    if q == "隐秘级":
        output_quality = "稀有特殊物品"
        pool = build_knife_pool(is_stattrak)
        probs = {s.name: 1.0 / len(pool) for s in pool}
    else:
        output_quality = next_quality(q)
        pool = build_output_pool(materials, output_quality, is_stattrak)
        probs = get_output_probabilities(materials, output_quality, is_stattrak)

    cost = sum(m.price for m in materials)
    rows = []
    ev = 0.0
    profit_prob = 0.0  # 按概率加权的盈利概率（Σ盈利皮肤的概率）
    for s in pool:
        est_wear = calculate_output_wear(materials, s)
        grade = get_wear_grade(est_wear)
        prob = probs.get(s.name, 0.0)
        key = (s.name, grade)
        if output_prices and key in output_prices and output_prices[key] > 0:
            price = output_prices[key]
        else:
            price = db.get_skin_price(s.name, grade, is_stattrak)
        if price <= 0:
            price = 0.0
        ev += price * prob
        if price >= cost:
            profit_prob += prob
        rows.append({
            "skin": s, "probability": prob,
            "est_wear": est_wear, "wear_grade": grade,
            "price": round(price, 2),
            "is_target": s.name == target_skin_name,
        })
    profit = ev - cost
    roi = (profit / cost * 100.0) if cost > 0 else 0.0
    break_even_rate = profit_prob
    target_prob = probs.get(target_skin_name, 0.0)
    return {
        "output_quality": output_quality,
        "pool_size": len(pool),
        "material_cost": round(cost, 2),
        "theoretical_ev": round(ev, 2),
        "profit": round(profit, 2),
        "roi_pct": round(roi, 2),
        "break_even_rate": round(break_even_rate, 4),
        "target_probability": round(target_prob, 6),
        "rows": rows,
    }


def _candidate_score(cand: Dict, strategy: str) -> float:
    """按策略计算候选配方的分数（越大越好）。"""
    s = cand["stats"]
    cost = s["material_cost"]
    if strategy == "min_cost":
        return -cost
    elif strategy == "max_ev_ratio":
        return s["theoretical_ev"] / cost if cost > 0 else 0
    else:  # max_profit
        return s["profit"]


def optimize_recipe(target_skin: Skin, target_wear: float,
                    is_stattrak: bool = False,
                    max_candidates: int = 50,
                    tolerance: float = 0.005,
                    live_prices: Optional[Dict] = None,
                    output_prices: Optional[Dict] = None,
                    strategy: str = "max_profit",
                    max_extra_collections: int = 3,
                    top_k: int = 5,
                    target_count: Optional[int] = None) -> Dict:
    """求产出目标皮肤的最优配方（兼顾成本、期望、保本率）。

    :param target_count: 目标收藏品材料数量（X打Y中的X）。
                         None=不限制（≥1即可）；1~9=精确指定主料数量。
    """
    target_q = target_skin.quality
    if target_q not in QUALITY_LEVEL:
        raise ValueError(f"未知稀有度: {target_q}")
    lvl = QUALITY_LEVEL[target_q]
    if lvl <= 0:
        raise ValueError(f"{target_q} 没有下级材料，无法汰换产出")
    material_quality = QUALITY_ORDER[lvl - 1]

    if not (target_skin.min_float <= target_wear <= target_skin.max_float):
        raise ValueError(
            f"目标磨损 {target_wear:.6f} 超出皮肤区间 "
            f"[{target_skin.min_float:.4f}, {target_skin.max_float:.4f}]")

    span = target_skin.max_float - target_skin.min_float
    if span <= 0:
        raise ValueError("目标皮肤磨损区间无效")
    req_avg_n = (target_wear - target_skin.min_float) / span

    target_collections = _find_collections_with_skin(target_skin, is_stattrak)
    if not target_collections:
        raise ValueError(f"未找到包含 {target_skin.name} 的收藏品")
    target_set = set(target_collections)

    # 所有收藏品的候选
    all_collections = db.get_collections_with_quality(material_quality, is_stattrak)
    all_options = _material_options(material_quality, all_collections, is_stattrak,
                                    live_prices=live_prices)
    for opt in all_options:
        opt["is_target_coll"] = opt["collection"] in target_set

    if not all_options:
        raise ValueError(f"无可用材料（{material_quality}，无价格数据）")

    # ---- 尝试不同的收藏品子集 ----
    # 基准：仅目标收藏品
    # 扩展：目标收藏品 + 最便宜的 N 个其他收藏品
    target_opts = [o for o in all_options if o["is_target_coll"]]
    extra_opts = [o for o in all_options if not o["is_target_coll"]]

    # 按收藏品分组，取每个收藏品最便宜的价格作为代表
    extra_coll_cost = {}
    for o in extra_opts:
        c = o["collection"]
        if c not in extra_coll_cost or o["price"] < extra_coll_cost[c]:
            extra_coll_cost[c] = o["price"]
    # 按最便宜材料价格升序排列非目标收藏品
    sorted_extra_colls = sorted(extra_coll_cost.keys(),
                                key=lambda c: extra_coll_cost[c])

    candidates_recipes = []

    # ---- 单次 DP 遍历全部候选 ----
    # 多样化候选（一半最便宜 + 一半最低磨损）
    opts = _diversify_candidates(all_options, max_candidates)
    # Top-K DP：优先用 GPU 张量版，失败/不可用时回退到 Python 版
    dp_used = "GPU"
    try:
        paths = _dp_topk_gpu(opts, req_avg_n, tolerance,
                             require_target=True, top_k=top_k,
                             target_count=target_count)
    except Exception as e:
        dp_used = "CPU"
        import traceback
        print(f"[DP] GPU 加速失败，回退 CPU: {type(e).__name__}: {e}")
        traceback.print_exc()
        paths = _dp_topk(opts, req_avg_n, tolerance,
                         require_target=True, top_k=top_k,
                         target_count=target_count)
    print(f"[DP] 使用 {dp_used} 加速，候选数={len(opts)}，结果数={len(paths)}")
    if not paths:
        raise ValueError("无法找到有效配方")

    # 对每条路径做局部搜索 + EV 评估，选最优
    for chosen in paths:
        polished = _local_improve(chosen, opts, req_avg_n, passes=3)
        materials = _chosen_to_materials(polished, req_avg_n)
        stats = _calc_recipe_ev(materials, is_stattrak, output_prices,
                                target_skin_name=target_skin.name)
        candidates_recipes.append({
            "materials": materials,
            "stats": stats,
            "collections_used": list({m.skin.collection for m in materials}),
        })

    # 按策略选择最优
    best = max(candidates_recipes, key=lambda r: _candidate_score(r, strategy))
    materials = best["materials"]
    stats = best["stats"]

    # 计算预测磨损
    n_sum = 0.0
    for m in materials:
        sp = m.skin.max_float - m.skin.min_float
        n_sum += (m.wear - m.skin.min_float) / sp if sp > 0 else 0.0
    actual_avg_n = n_sum / len(materials)
    predicted_wear = target_skin.min_float + actual_avg_n * span

    result = {
        "materials": materials,
        "material_cost": stats["material_cost"],
        "predicted_wear": round(predicted_wear, 6),
        "predicted_wear_grade": get_wear_grade(predicted_wear),
        "target_wear": target_wear,
        "wear_error": round(abs(predicted_wear - target_wear), 6),
        "probability": stats["target_probability"],
        "output_pool_size": stats["pool_size"],
        "collections": best["collections_used"],
        "material_quality": material_quality,
        "is_stattrak": is_stattrak,
        "target_skin": target_skin,
        # EV 相关
        "theoretical_ev": stats["theoretical_ev"],
        "profit": stats["profit"],
        "roi_pct": stats["roi_pct"],
        "break_even_rate": stats["break_even_rate"],
        "output_rows": stats["rows"],
        "strategy": strategy,
        "all_candidates": candidates_recipes,
    }
    return result


def _chosen_to_materials(chosen: List[Dict], req_avg_n: float) -> List[Material]:
    """将 DP 选中的 options 转为 Material 列表，精确分配磨损使归一化均值等于目标。

    策略：所有材料先设为 n_lo，再把"缺口"（target_sum - sum(n_lo)）
    按余量从大到小依次加到各材料上（不超过 n_hi），确保均值精确等于目标。
    """
    n = len(chosen)
    target_sum = req_avg_n * n

    # 初始化为 n_lo
    n_vals = [opt["n_lo"] for opt in chosen]
    current_sum = sum(n_vals)
    deficit = target_sum - current_sum

    if deficit > 1e-9:
        # 按可调余量 (n_hi - n_lo) 从大到小排序，优先加余量大的
        order = sorted(range(n), key=lambda i: chosen[i]["n_hi"] - chosen[i]["n_lo"],
                       reverse=True)
        for i in order:
            if deficit <= 1e-9:
                break
            room = chosen[i]["n_hi"] - n_vals[i]
            add = min(room, deficit)
            n_vals[i] += add
            deficit -= add

    materials: List[Material] = []
    for opt, nv in zip(chosen, n_vals):
        s = opt["skin"]
        span_m = s.max_float - s.min_float
        wear = s.min_float + nv * span_m
        wear = max(s.min_float, min(s.max_float, wear))
        materials.append(Material(skin=s, wear=wear, price=opt["price"],
                                  grade=opt.get("grade", "")))
    return materials


def _find_collections_with_skin(target_skin: Skin,
                                is_stattrak: bool) -> List[str]:
    """找到所有包含目标皮肤（指定稀有度、ST 状态）的收藏品。"""
    conn = db.get_conn()
    rows = conn.execute(
        "SELECT DISTINCT collection FROM skins "
        "WHERE name=? AND quality=? AND is_stattrak=?",
        (target_skin.name, target_skin.quality, 1 if is_stattrak else 0),
    ).fetchall()
    conn.close()
    return [r["collection"] for r in rows]


def export_recipe_to_taihuan(materials: List[Material],
                             target_skin: Skin,
                             plan_name: Optional[str] = None) -> int:
    """将配方导出到自动汰换库。

    :param materials: 10 件材料
    :param target_skin: 目标产物皮肤（用于生成配方名）
    :param plan_name: 配方名，None 则自动生成
    :return: 保存的 plan_id
    """
    import sys
    import os
    _buy_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "buy", "汰换")
    if _buy_dir not in sys.path:
        sys.path.insert(0, _buy_dir)
    from core import recipe_matcher as rm
    from core.data_manager import build_c5_market_hash_name

    # 构建材料列表（自动汰换库格式）
    mat_rows = []
    for m in materials:
        s = m.skin
        # 优先用 DP 选中时确定的磨损档，避免边界浮点误差
        grade = m.grade or get_wear_grade(m.wear)
        # 磨损档区间（夹紧到皮肤自身范围）
        glo, ghi = _grade_wear_range(grade)
        wlo = max(s.min_float, glo)
        whi = min(s.max_float, ghi)
        # 从 DB 取 buff_goods_id（按磨损档）
        ids = db.get_skin_market_ids(s.name, grade, s.is_stattrak)
        buff_gid = ids.get("buff_goods_id") or s.buff_goods_id
        c5_hash = build_c5_market_hash_name(s.market_hash_name, grade)
        mat_rows.append({
            "皮肤名称": s.name,
            "市场哈希名称": s.market_hash_name,
            "磨损": grade,
            "品质": s.quality,
            "磨损_min": round(wlo, 10),
            "磨损_max": round(whi, 10),
            "磨损区间": f"{wlo:.10f}~{whi:.10f}",
            "buff_goods_id": str(buff_gid),
            "c5_market_hash_name": c5_hash,
            "price_buff": round(m.price, 2),
            "collection": s.collection,
            "wear_min": round(wlo, 10),
            "wear_max": round(whi, 10),
        })

    if plan_name is None:
        plan_name = f"{target_skin.name} 配方"

    return rm.save_material_plan_to_db(plan_name, mat_rows,
                                        remark="炼金模拟导出")

