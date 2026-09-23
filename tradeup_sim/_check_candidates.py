# -*- coding: utf-8 -*-
"""检查优化器返回的所有候选配方。"""
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
req_avg_n = (target_wear - target.min_float) / (target.max_float - target.min_float)

result = engine.optimize_recipe(
    target, target_wear, is_stattrak=False,
    max_candidates=50, tolerance=0.005,
    strategy="max_profit", top_k=5,
)

print(f"目标 avg_n = {req_avg_n:.6f}")
print()

candidates = result["all_candidates"]
print(f"候选配方数: {len(candidates)}")
print()

bad = []
for i, cand in enumerate(candidates):
    mats = cand["materials"]
    n_sum = sum((m.wear - m.skin.min_float)/(m.skin.max_float - m.skin.min_float) for m in mats)
    avg_n = n_sum / 10
    err = abs(avg_n - req_avg_n)
    if err > 0.001:
        bad.append((i, avg_n, err, mats))

print(f"磨损异常的候选数: {len(bad)}")
for i, avg_n, err, mats in bad[:3]:
    print(f"\n候选 {i}: avg_n={avg_n:.6f}, error={err:.6f}")
    for j, m in enumerate(mats):
        n = (m.wear - m.skin.min_float)/(m.skin.max_float - m.skin.min_float)
        print(f"  [{j}] {m.skin.name} ({m.skin.collection}): wear={m.wear:.6f}, n={n:.6f}")
