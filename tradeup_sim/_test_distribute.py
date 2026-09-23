# -*- coding: utf-8 -*-
"""直接测试 _chosen_to_materials 的磨损分配。"""
import sys
sys.path.insert(0, 'f:/steamdt-project')
from tradeup_sim import database as db
from tradeup_sim import engine
from tradeup_sim.models import WEAR_GRADES, Skin

# 优化器选中的材料
chosen_data = [
    ("内格夫 | 酸葡萄", "攀升收藏品", "战痕累累", 0.08),
    ("Tec-9 | 束带-9", "寒带收藏品", "久经沙场", 0.11),
    ("内格夫 | 酸葡萄", "攀升收藏品", "战痕累累", 0.08),
    ("内格夫 | 酸葡萄", "攀升收藏品", "战痕累累", 0.08),
    ("内格夫 | 酸葡萄", "攀升收藏品", "战痕累累", 0.08),
    ("内格夫 | 酸葡萄", "攀升收藏品", "战痕累累", 0.08),
    ("AUG | 钢铁哨兵", "2025 列车停放站收藏品", "久经沙场", 0.09),
    ("内格夫 | 酸葡萄", "攀升收藏品", "战痕累累", 0.08),
    ("AUG | 钢铁哨兵", "2025 列车停放站收藏品", "久经沙场", 0.09),
    ("SG 553 | 篮纹半调", "热辐射收藏品", "久经沙场", 0.08),
]

conn = db.get_conn()
chosen = []
for name, coll, grade, price in chosen_data:
    row = conn.execute(
        "SELECT min_float, max_float FROM skins WHERE name=? AND collection=? AND is_stattrak=0",
        (name, coll)
    ).fetchone()
    mn, mx = row["min_float"], row["max_float"]
    span = mx - mn
    for g, glo, ghi in WEAR_GRADES:
        if g == grade:
            alo = max(mn, glo)
            ahi = min(mx, ghi)
            n_lo = (alo - mn) / span
            n_hi = (ahi - mn) / span
            break
    s = Skin(collection=coll, name=name, quality="军规级", min_float=mn, max_float=mx)
    chosen.append({
        "skin": s, "grade": grade,
        "n_lo": n_lo, "n_hi": n_hi,
        "price": price, "wear_lo": alo, "wear_hi": ahi,
        "collection": coll,
    })
conn.close()

req_avg_n = 0.494324
print(f"req_avg_n = {req_avg_n}")
print(f"target_sum = {req_avg_n * 10}")
print()

# 手动模拟 _chosen_to_materials
n = len(chosen)
target_sum = req_avg_n * n
n_vals = [opt["n_lo"] for opt in chosen]
current_sum = sum(n_vals)
deficit = target_sum - current_sum
print(f"sum(n_lo) = {current_sum:.6f}")
print(f"deficit = {deficit:.6f}")
print()

if deficit > 1e-9:
    order = sorted(range(n), key=lambda i: chosen[i]["n_hi"] - chosen[i]["n_lo"], reverse=True)
    print("分配顺序 (按 room 降序):")
    for i in order:
        room = chosen[i]["n_hi"] - chosen[i]["n_lo"]
        print(f"  [{i}] {chosen[i]['skin'].name} room={room:.6f}")
    print()
    for i in order:
        if deficit <= 1e-9:
            break
        room = chosen[i]["n_hi"] - n_vals[i]
        add = min(room, deficit)
        n_vals[i] += add
        deficit -= add
        print(f"  [{i}] {chosen[i]['skin'].name}: add={add:.6f}, n_val={n_vals[i]:.6f}, deficit={deficit:.6f}")

print()
print("最终 n_vals:")
for i, (opt, nv) in enumerate(zip(chosen, n_vals)):
    print(f"  [{i}] {opt['skin'].name}: n={nv:.6f}")

actual_avg = sum(n_vals) / len(n_vals)
print(f"\nactual avg_n = {actual_avg:.6f}")
print(f"target avg_n = {req_avg_n:.6f}")
print(f"error = {abs(actual_avg - req_avg_n):.6f}")

# 现在调用实际函数
print("\n--- 调用实际 _chosen_to_materials ---")
materials = engine._chosen_to_materials(chosen, req_avg_n)
for i, m in enumerate(materials):
    n = (m.wear - m.skin.min_float) / (m.skin.max_float - m.skin.min_float)
    print(f"  [{i}] {m.skin.name}: wear={m.wear:.6f}, n={n:.6f}")

n_sum = sum((m.wear - m.skin.min_float)/(m.skin.max_float - m.skin.min_float) for m in materials)
actual_avg_n = n_sum / len(materials)
print(f"\nactual avg_n = {actual_avg_n:.6f}")
print(f"target avg_n = {req_avg_n:.6f}")
print(f"error = {abs(actual_avg_n - req_avg_n):.6f}")
