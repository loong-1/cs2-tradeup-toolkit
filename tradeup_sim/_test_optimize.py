# -*- coding: utf-8 -*-
"""测试优化器的磨损预测是否正确。"""
import sys
sys.path.insert(0, 'f:/steamdt-project')

from tradeup_sim import database as db
from tradeup_sim import engine
from tradeup_sim.models import Skin

# 用实际数据中的一个产物作为目标
# 材料是受限级，产物是保密级
# 选 AK-47 | 寒翠 (寒带收藏品)，实际磨损 0.370743
target_name = "AK-47 | 寒翠"
target_coll = "寒带收藏品"
target_wear = 0.370743

conn = db.get_conn()
row = conn.execute(
    "SELECT * FROM skins WHERE name=? AND collection=? AND is_stattrak=0",
    (target_name, target_coll)
).fetchone()
conn.close()

if not row:
    print(f"未找到目标皮肤: {target_name}")
    sys.exit(1)

target = Skin(
    collection=row["collection"],
    name=row["name"],
    quality=row["quality"],
    min_float=row["min_float"],
    max_float=row["max_float"],
    is_stattrak=bool(row["is_stattrak"]),
)

print(f"目标皮肤: {target.name} ({target.collection})")
print(f"稀有度: {target.quality}")
print(f"磨损区间: [{target.min_float}, {target.max_float}]")
print(f"目标磨损: {target_wear}")
print(f"所需 avg_n: {(target_wear - target.min_float)/(target.max_float - target.min_float):.6f}")
print()

try:
    result = engine.optimize_recipe(
        target, target_wear, is_stattrak=False,
        max_candidates=50, tolerance=0.005,
        strategy="max_profit", top_k=5,
    )
    print(f"优化成功! 材料成本: ¥{result['material_cost']:.2f}")
    print(f"预测磨损: {result['predicted_wear']:.6f} [{result['predicted_wear_grade']}]")
    print(f"目标磨损: {result['target_wear']:.6f}")
    print(f"磨损误差: {result['wear_error']:.6f}")
    print(f"目标概率: {result['probability']*100:.2f}%")
    print(f"理论 EV: ¥{result['theoretical_ev']:.2f}")
    print(f"利润: ¥{result['profit']:.2f}")
    print(f"ROI: {result['roi_pct']:.2f}%")
    print()
    print("材料列表:")
    for i, m in enumerate(result["materials"]):
        n = (m.wear - m.skin.min_float) / (m.skin.max_float - m.skin.min_float)
        print(f"  {i+1}. {m.skin.name} | {m.skin.collection} | "
              f"wear={m.wear:.6f} | n={n:.6f} | ¥{m.price:.2f}")

    # 验证：手动计算 avg_n
    n_sum = sum((m.wear - m.skin.min_float)/(m.skin.max_float - m.skin.min_float)
                for m in result["materials"])
    actual_avg_n = n_sum / len(result["materials"])
    pred = target.min_float + actual_avg_n * (target.max_float - target.min_float)
    print(f"\n验证: actual_avg_n = {actual_avg_n:.6f}")
    print(f"验证: predicted_wear = {pred:.6f}")
    print(f"验证: 与目标差 = {abs(pred - target_wear):.6f}")

except Exception as e:
    import traceback
    print(f"优化失败: {e}")
    traceback.print_exc()
