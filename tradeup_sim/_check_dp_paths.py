# -*- coding: utf-8 -*-
"""检查所有 DP 路径的原始 sum(n_lo)。"""
import sys
sys.path.insert(0, 'f:/steamdt-project')
from tradeup_sim import database as db
from tradeup_sim import engine
from tradeup_sim.models import Skin
from tradeup_sim.engine import (_find_collections_with_skin, _material_options,
                                _diversify_candidates, _dp_topk_gpu)

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
req_avg_n = (target_wear - target.min_float) / (target.max_float - target.min_float)

material_quality = "军规级"
target_collections = _find_collections_with_skin(target, False)
target_set = set(target_collections)
all_collections = db.get_collections_with_quality(material_quality, False)
all_options = _material_options(material_quality, all_collections, False, live_prices=None)
for opt in all_options:
    opt["is_target_coll"] = opt["collection"] in target_set

opts = _diversify_candidates(all_options, 50)
paths = _dp_topk_gpu(opts, req_avg_n, 0.005, require_target=True, top_k=5, target_count=None)

print(f"target_sum = {req_avg_n*10:.6f}")
print(f"target+tol = {req_avg_n*10 + 0.005*10:.6f}")
print()

over_target = 0
for idx, path in enumerate(paths):
    sum_lo = sum(o["n_lo"] for o in path)
    sum_hi = sum(o["n_hi"] for o in path)
    if sum_lo > req_avg_n * 10 + 1e-9:
        over_target += 1
        if over_target <= 5:
            print(f"路径 {idx}: sum(n_lo)={sum_lo:.6f} > target={req_avg_n*10:.6f}")
            for o in path:
                print(f"    {o['skin'].name} ({o['grade']}): n_lo={o['n_lo']:.6f}")

print(f"\n超过 target 的路径数: {over_target} / {len(paths)}")
