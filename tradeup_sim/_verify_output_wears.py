# -*- coding: utf-8 -*-
"""验证优化器输出的配方中，所有产物的磨损计算是否正确。"""
import sys
sys.path.insert(0, 'f:/steamdt-project')
from tradeup_sim import database as db, engine
from tradeup_sim.models import Skin

conn = db.get_conn()
row = conn.execute(
    "SELECT * FROM skins WHERE name='AK-47 | 寒翠' AND collection='寒带收藏品' AND is_stattrak=0 LIMIT 1"
).fetchone()
target = Skin(
    collection=row["collection"], name=row["name"], quality=row["quality"],
    min_float=row["min_float"], max_float=row["max_float"],
)
target_wear = 0.370743
conn.close()

result = engine.optimize_recipe(
    target, target_wear, is_stattrak=False,
    max_candidates=50, tolerance=0.005,
    strategy="max_profit", top_k=5,
)

print(f"目标: {target.name}, wear={target_wear}")
print(f"预测磨损: {result['predicted_wear']:.6f}, 误差: {result['wear_error']:.6f}")
print(f"\n所有候选配方的产物磨损验证:")
print("=" * 100)

for i, cand in enumerate(result['all_candidates'][:3]):
    print(f"\n--- 候选 {i+1} (成本={cand['cost']:.2f}) ---")
    # 计算材料的 avg_n
    n_vals = []
    for m in cand['materials']:
        span = m.skin.max_float - m.skin.min_float
        n = (m.wear - m.skin.min_float) / span if span > 0 else 0
        n_vals.append(n)
    avg_n = sum(n_vals) / len(n_vals)
    print(f"  材料 avg_n = {avg_n:.6f}")
    
    # 验证每个产物的磨损
    for out in cand['outputs']:
        out_skin = out['skin']
        span = out_skin.max_float - out_skin.min_float
        expected_wear = out_skin.min_float + avg_n * span
        actual_wear = out.get('wear', 0)
        diff = abs(expected_wear - actual_wear)
        status = "✓" if diff < 0.001 else "✗"
        print(f"  {status} {out_skin.name}: expected={expected_wear:.6f}, actual={actual_wear:.6f}, diff={diff:.6f}")
