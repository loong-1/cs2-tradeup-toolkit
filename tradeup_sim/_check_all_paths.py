# -*- coding: utf-8 -*-
"""检查所有 DP 路径的可行性，找出磨损分配失败的路径。"""
import sys
sys.path.insert(0, 'f:/steamdt-project')
from tradeup_sim import database as db
from tradeup_sim import engine
from tradeup_sim.models import Skin
from tradeup_sim.engine import (_find_collections_with_skin, _material_options,
                                _diversify_candidates, _dp_topk_gpu, _local_improve,
                                _chosen_to_materials, _calc_recipe_ev)

target_name = "AK-47 | 寒翠"
target_coll = "寒带收藏品"
target_wear = 0.370743

conn = db.get_conn()
row = conn.execute(
    "SELECT * FROM skins WHERE name=? AND collection=? AND is_stattrak=0",
    (target_name, target_coll)
).fetchone()
conn.close()

target = Skin(
    collection=row["collection"], name=row["name"], quality=row["quality"],
    min_float=row["min_float"], max_float=row["max_float"],
)
span = target.max_float - target.min_float
req_avg_n = (target_wear - target.min_float) / span

material_quality = "军规级"
target_collections = _find_collections_with_skin(target, False)
target_set = set(target_collections)
all_collections = db.get_collections_with_quality(material_quality, False)
all_options = _material_options(material_quality, all_collections, False, live_prices=None)
for opt in all_options:
    opt["is_target_coll"] = opt["collection"] in target_set

opts = _diversify_candidates(all_options, 50)
paths = _dp_topk_gpu(opts, req_avg_n, 0.005, require_target=True, top_k=5, target_count=None)

print(f"共 {len(paths)} 条路径")
print(f"target_sum = {req_avg_n*10:.6f}")
print()

bad_paths = []
for idx, path in enumerate(paths):
    sum_lo = sum(o["n_lo"] for o in path)
    sum_hi = sum(o["n_hi"] for o in path)
    feasible = sum_lo - 1e-9 <= req_avg_n * 10 <= sum_hi + 1e-9

    polished = _local_improve(path, opts, req_avg_n, passes=3)
    materials = _chosen_to_materials(polished, req_avg_n)
    n_sum = sum((m.wear - m.skin.min_float)/(m.skin.max_float - m.skin.min_float) for m in materials)
    actual_avg_n = n_sum / 10
    error = abs(actual_avg_n - req_avg_n)

    if error > 0.001:
        bad_paths.append((idx, actual_avg_n, error, polished))

print(f"磨损误差 > 0.001 的路径数: {len(bad_paths)}")
for idx, avg_n, err, polished in bad_paths[:5]:
    print(f"\n路径 {idx}: actual_avg_n={avg_n:.6f}, error={err:.6f}")
    sum_lo = sum(o["n_lo"] for o in polished)
    sum_hi = sum(o["n_hi"] for o in polished)
    print(f"  sum(n_lo)={sum_lo:.6f}, sum(n_hi)={sum_hi:.6f}, target_sum={req_avg_n*10:.6f}")
    print(f"  deficit={req_avg_n*10 - sum_lo:.6f}")
    for i, o in enumerate(polished):
        print(f"    [{i}] {o['skin'].name} ({o['grade']}): n_lo={o['n_lo']:.4f}, n_hi={o['n_hi']:.4f}")
