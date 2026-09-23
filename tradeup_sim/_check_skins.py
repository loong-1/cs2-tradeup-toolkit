# -*- coding: utf-8 -*-
"""检查优化器选中材料的皮肤范围。"""
import sys
sys.path.insert(0, 'f:/steamdt-project')
from tradeup_sim import database as db
from tradeup_sim.models import WEAR_GRADES

skins_to_check = [
    ("内格夫 | 酸葡萄", "攀升收藏品", "战痕累累"),
    ("Tec-9 | 束带-9", "寒带收藏品", "久经沙场"),
    ("AUG | 钢铁哨兵", "2025 列车停放站收藏品", "久经沙场"),
    ("SG 553 | 篮纹半调", "热辐射收藏品", "久经沙场"),
]

conn = db.get_conn()
for name, coll, grade in skins_to_check:
    row = conn.execute(
        "SELECT min_float, max_float FROM skins WHERE name=? AND collection=? AND is_stattrak=0",
        (name, coll)
    ).fetchone()
    if not row:
        print(f"{name} ({coll}) NOT FOUND")
        continue
    mn, mx = row["min_float"], row["max_float"]
    span = mx - mn
    # wear grade range
    for g, glo, ghi in WEAR_GRADES:
        if g == grade:
            alo = max(mn, glo)
            ahi = min(mx, ghi)
            n_lo = (alo - mn) / span
            n_hi = (ahi - mn) / span
            print(f"{name} ({coll}) [{grade}]")
            print(f"  skin: [{mn}, {mx}], span={span}")
            print(f"  grade range: [{alo}, {ahi}]")
            print(f"  n_lo={n_lo:.6f}, n_hi={n_hi:.6f}, room={n_hi-n_lo:.6f}")
            break
conn.close()
