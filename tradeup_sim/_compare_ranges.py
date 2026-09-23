# -*- coding: utf-8 -*-
import sys
sys.path.insert(0, 'f:/steamdt-project')
from tradeup_sim import database as db
conn = db.get_conn()

# 从 HTML 中提取的实际 float 范围
actual_ranges = {
    "AK-47 | 丛林涂装": (0.06, 0.8),
    "AK-47 | 巴洛克之紫": (0.0, 1.0),
    "AK-47 | 捕食者": (0.06, 0.8),
    "AK-47 | 狩猎网格": (0.06, 0.8),
    "AK-47 | 灰变迷彩": (0.0, 0.671875),
    "AK-47 | 橄榄迷彩": (0.0, 0.5),
    "AUG | 众枪之的": (0.06, 0.8),
    "AUG | 辐射危机": (0.0, 0.55),
    "截短霰弹枪 | 镀铜": (0.0, 0.67),
    "AUG | 铜斑蛇": (0.0, 0.67),
}

for name, (actual_lo, actual_hi) in actual_ranges.items():
    rows = conn.execute(
        "SELECT name, collection, min_float, max_float, is_stattrak FROM skins WHERE name=? LIMIT 3",
        (name,)
    ).fetchall()
    print(f"\n{name}:")
    print(f"  实际范围: [{actual_lo}, {actual_hi}]")
    for r in rows:
        match = "✓" if abs(r['min_float']-actual_lo)<0.001 and abs(r['max_float']-actual_hi)<0.001 else "✗"
        print(f"  {match} DB: [{r['min_float']}, {r['max_float']}] (stattrak={r['is_stattrak']}, coll={r['collection']})")

conn.close()
