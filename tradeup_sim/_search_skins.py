# -*- coding: utf-8 -*-
import sys
sys.path.insert(0, 'f:/steamdt-project')
from tradeup_sim import database as db
conn = db.get_conn()
# 搜索相关皮肤
for kw in ['镀铜', '冥界之河', '铜斑蛇', 'Cry Me', '轻空', '升天', '橙黄镂刻', '蝮蛇迷彩', '翡翠色调', '狮子鱼']:
    rows = conn.execute(
        "SELECT name, collection, min_float, max_float FROM skins WHERE name LIKE ? LIMIT 5",
        (f'%{kw}%',)
    ).fetchall()
    print(f"--- {kw} ---")
    for r in rows:
        print(f"  {r['name']} | {r['collection']} | [{r['min_float']}, {r['max_float']}]")
conn.close()
