# -*- coding: utf-8 -*-
"""测试：用实际材料数据验证磨损算法。"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tradeup_sim import database as db
from tradeup_sim.models import WEAR_GRADES

# 实际材料数据（从 HTML 解析）
materials_data = [
    ("AK-47", "丛林涂装", "雨林遗迹收藏品", "破损不堪"),
    ("AK-47", "丛林涂装", "雨林遗迹收藏品", "破损不堪"),
    ("AK-47", "巴洛克之紫", "运河水城收藏品", "战痕累累"),
    ("AK-47", "捕食者", "炙热沙城收藏品", "破损不堪"),
    ("AK-47", "狩猎网格", "炙热沙城 II 收藏品", "破损不堪"),
    ("AK-47", "灰变迷彩", "寒带收藏品", "久经沙场"),
    ("AK-47", "橄榄迷彩", "狩猎运动收藏品", "久经沙场"),
    ("AUG", "众枪之的", "安全处所收藏品", "久经沙场"),
    ("AUG", "辐射危机", "死城之谜收藏品", "久经沙场"),
    ("AUG", "朽木", "2021 炙热沙城 II 收藏品", "久经沙场"),
]

def grade_range(grade):
    for name, lo, hi in WEAR_GRADES:
        if name == grade:
            return lo, hi
    return 0.0, 1.0

def get_skin(name, collection):
    conn = db.get_conn()
    row = conn.execute(
        "SELECT min_float, max_float FROM skins WHERE name=? AND collection=?",
        (name, collection)
    ).fetchone()
    conn.close()
    if row:
        return row["min_float"], row["max_float"]
    return None, None

print("=" * 100)
print(f"{'皮肤':<20} {'收藏':<16} {'档位':<8} {'min':>8} {'max':>8} {'n_lo':>8} {'n_mid':>8} {'n_hi':>8}")
print("-" * 100)

n_lo_list, n_mid_list, n_hi_list = [], [], []

for weapon, skin_sub, coll, grade in materials_data:
    skin_name = f"{weapon} | {skin_sub}"
    mn, mx = get_skin(skin_name, coll)
    if mn is None:
        print(f"{skin_name} {coll} NOT FOUND")
        continue
    span = mx - mn
    glo, ghi = grade_range(grade)
    alo = max(mn, glo)
    ahi = min(mx, ghi)
    n_lo = (alo - mn) / span
    n_mid = ((alo + ahi) / 2 - mn) / span
    n_hi = (ahi - mn) / span
    n_lo_list.append(n_lo)
    n_mid_list.append(n_mid)
    n_hi_list.append(n_hi)
    print(f"{skin_name:<20} {coll:<16} {grade:<8} {mn:>8.4f} {mx:>8.4f} {n_lo:>8.4f} {n_mid:>8.4f} {n_hi:>8.4f}")

print("-" * 100)
print(f"{'avg_n_lo':<20} = {sum(n_lo_list)/len(n_lo_list):.6f}")
print(f"{'avg_n_mid':<20} = {sum(n_mid_list)/len(n_mid_list):.6f}")
print(f"{'avg_n_hi':<20} = {sum(n_hi_list)/len(n_hi_list):.6f}")
print(f"{'actual avg_n':<20} = 0.494324")
print()

# 尝试不同的代表值策略
strategies = {
    "wear_grade_midpoint (标准档位中点)": n_mid_list,
    "skin_range_midpoint (皮肤区间中点)": [0.5]*10,
}

# 还有：如果用皮肤区间中点，norm 恒为 0.5
print("尝试不同代表值策略:")
for name, vals in strategies.items():
    avg = sum(vals)/len(vals)
    print(f"  {name}: avg_n = {avg:.6f}")

# 反推：实际 avg_n = 0.494324
# 如果 9 个材料用皮肤中点(0.5)，第10个需要多少？
print()
print("反推分析:")
target = 0.494324
# 如果用 n_mid，avg = 0.487424，差 0.0069
mid_avg = sum(n_mid_list)/len(n_mid_list)
print(f"  用档位中点 avg_n = {mid_avg:.6f}, 与实际差 {target - mid_avg:+.6f}")
skin_avg = 0.5
print(f"  用皮肤中点 avg_n = {skin_avg:.6f}, 与实际差 {target - skin_avg:+.6f}")
