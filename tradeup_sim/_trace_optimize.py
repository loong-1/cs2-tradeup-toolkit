# -*- coding: utf-8 -*-
"""追踪优化器的完整流程，找出磨损分配问题。"""
import sys
sys.path.insert(0, 'f:/steamdt-project')
from tradeup_sim import database as db
from tradeup_sim import engine
from tradeup_sim.models import Skin

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
print(f"req_avg_n = {req_avg_n:.6f}")
print()

# 复刻 optimize_recipe 的准备阶段
from tradeup_sim.models import QUALITY_LEVEL, QUALITY_ORDER, next_quality
from tradeup_sim.engine import _find_collections_with_skin, _material_options, _diversify_candidates, _dp_topk_gpu, _local_improve, _chosen_to_materials

material_quality = "军规级"
target_collections = _find_collections_with_skin(target, False)
target_set = set(target_collections)
all_collections = db.get_collections_with_quality(material_quality, False)
all_options = _material_options(material_quality, all_collections, False, live_prices=None)
for opt in all_options:
    opt["is_target_coll"] = opt["collection"] in target_set

opts = _diversify_candidates(all_options, 50)
print(f"候选数: {len(opts)}")
print()

paths = _dp_topk_gpu(opts, req_avg_n, 0.005, require_target=True, top_k=5, target_count=None)
print(f"DP 路径数: {len(paths)}")
print()

# 检查每条路径的 sum(n_lo) 和 sum(n_hi)
for idx, path in enumerate(paths[:5]):
    sum_lo = sum(o["n_lo"] for o in path)
    sum_hi = sum(o["n_hi"] for o in path)
    avg_lo = sum_lo / 10
    avg_hi = sum_hi / 10
    print(f"路径 {idx}: sum(n_lo)={sum_lo:.4f} (avg={avg_lo:.4f}), sum(n_hi)={sum_hi:.4f} (avg={avg_hi:.4f})")
    print(f"  target_sum={req_avg_n*10:.4f}, 可达: {sum_lo - 1e-9 <= req_avg_n*10 <= sum_hi + 1e-9}")
print()

# 对第一条路径做 _local_improve + _chosen_to_materials
chosen = paths[0]
polished = _local_improve(chosen, opts, req_avg_n, passes=3)
print(f"_local_improve 后材料数: {len(polished)}")
sum_lo = sum(o["n_lo"] for o in polished)
sum_hi = sum(o["n_hi"] for o in polished)
print(f"sum(n_lo)={sum_lo:.6f}, sum(n_hi)={sum_hi:.6f}")
print(f"target_sum={req_avg_n*10:.6f}, deficit={req_avg_n*10 - sum_lo:.6f}")
print()

materials = _chosen_to_materials(polished, req_avg_n)
print("_chosen_to_materials 结果:")
n_sum = 0
for i, m in enumerate(materials):
    n = (m.wear - m.skin.min_float) / (m.skin.max_float - m.skin.min_float)
    n_sum += n
    print(f"  [{i}] {m.skin.name}: wear={m.wear:.6f}, n={n:.6f}")
print(f"avg_n = {n_sum/10:.6f}")
print(f"target avg_n = {req_avg_n:.6f}")
