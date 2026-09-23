# -*- coding: utf-8 -*-
"""验证磨损公式：用用户提供的实际数据反推 avg_n，看是否一致。"""
import sys
sys.path.insert(0, 'f:/steamdt-project')
from tradeup_sim import database as db

conn = db.get_conn()

# 第一个汰换的材料
mats1 = [
    ("AWP", "冥界之河", "运河水城收藏品", 0.190828),
    ("FAMAS", "Cry Me a River", "运河水城收藏品", 0.077579),
    ("AWP", "冥界之河", "运河水城收藏品", 0.170844),
    ("截短霰弹枪", "镀铜", "炙热沙城收藏品", 0.634514),
    ("FAMAS", "Cry Me a River", "运河水城收藏品", 0.107763),
    ("SG 553", "轻空", "运河水城收藏品", 0.176013),
    ("P2000", "升天", "运河水城收藏品", 0.086042),
    ("SSG 08", "橙黄镂刻", "运河水城收藏品", 0.340096),
    ("FAMAS", "Cry Me a River", "运河水城收藏品", 0.493173),
    ("SSG 08", "橙黄镂刻", "运河水城收藏品", 0.065215),
]

print("=== 第一个汰换 ===")
n_vals = []
for weapon, name, coll, wear in mats1:
    row = conn.execute(
        "SELECT min_float, max_float FROM skins WHERE name=? AND collection=? AND is_stattrak=0",
        (name, coll)
    ).fetchone()
    if not row:
        print(f"  {weapon} | {name}: NOT FOUND")
        continue
    mn, mx = row["min_float"], row["max_float"]
    n = (wear - mn) / (mx - mn) if mx > mn else 0
    n_vals.append(n)
    print(f"  {weapon} | {name}: wear={wear:.6f}, skin=[{mn}, {mx}], n={n:.6f}")

avg_n = sum(n_vals) / len(n_vals)
print(f"  avg_n = {avg_n:.6f}")

# 产物
outputs1 = [
    ("截短霰弹枪", "镀铜", "炙热沙城收藏品", 0.331202),
    ("AUG", "铜斑蛇", "炙热沙城收藏品", 0.166511),
    ("AWP", "蝮蛇迷彩", "炙热沙城收藏品", 0.285286),
    ("双持贝瑞塔", "翡翠色调", "运河水城收藏品", 0.039546),
    ("SSG 08", "橙黄镂刻", "运河水城收藏品", 0.247162),
    ("内格夫", "狮子鱼", "运河水城收藏品", 0.126169),
    ("SG 553", "轻空", "运河水城收藏品", 0.155337),
    ("P2000", "升天", "运河水城收藏品", 0.053078),
    ("SCAR-20", "绿色层压板", "运河水城收藏品", 0.133947),
]
print(f"\\n产物验证 (expected avg_n={avg_n:.6f}):")
for weapon, name, coll, wear in outputs1:
    row = conn.execute(
        "SELECT min_float, max_float FROM skins WHERE name=? AND collection=? AND is_stattrak=0",
        (name, coll)
    ).fetchone()
    if not row:
        print(f"  {weapon} | {name}: NOT FOUND")
        continue
    mn, mx = row["min_float"], row["max_float"]
    derived_n = (wear - mn) / (mx - mn) if mx > mn else 0
    expected_wear = mn + avg_n * (mx - mn)
    print(f"  {weapon} | {name}: wear={wear:.6f}, derived_n={derived_n:.6f}, expected_wear={expected_wear:.6f}, diff={abs(wear-expected_wear):.6f}")

conn.close()
