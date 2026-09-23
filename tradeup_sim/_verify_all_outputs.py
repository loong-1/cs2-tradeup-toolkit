# -*- coding: utf-8 -*-
"""验证所有产物的 avg_n 是否一致。"""
import sys
sys.path.insert(0, 'f:/steamdt-project')
from tradeup_sim import database as db

conn = db.get_conn()

# 从 HTML 解析的产物数据
outputs = [
    ("截短霰弹枪 | 镀铜", "炙热沙城收藏品", 0.494324),
    ("AUG | 铜斑蛇", "炙热沙城收藏品", 0.248524),
    ("AWP | 蝮蛇迷彩", "炙热沙城收藏品", 0.4258),
    ("双持贝瑞塔 | 翡翠色调", "运河水城收藏品", 0.039546),
    ("SSG 08 | 橙黄镂刻", "运河水城收藏品", 0.247162),
    ("P90 | 巴洛克之红", "运河水城收藏品", 0.247162),
    ("G3SG1 | 穆拉诺之紫", "运河水城收藏品", 0.247162),
    ("Tec-9 | 骨化之色", "雨林遗迹收藏品", 0.039546),
    ("MAC-10 | 核子花园", "死城之谜收藏品", 0.346027),
    ("Tec-9 | 核子剧毒", "死城之谜收藏品", 0.346027),
    ("XM1014 | 碾骨机", "死城之谜收藏品", 0.296595),
    ("MP9 | 落日", "死城之谜收藏品", 0.494324),
    ("格洛克18型 | 核子反应", "死城之谜收藏品", 0.494324),
    ("P250 | 潜藏者", "2021 炙热沙城 II 收藏品", 0.247162),
    ("新星 | 流沙", "2021 炙热沙城 II 收藏品", 0.247162),
    ("加利尔AR | 渐变琥珀", "2021 炙热沙城 II 收藏品", 0.19773),
    ("G3SG1 | 碧藤青翠", "2021 炙热沙城 II 收藏品", 0.271878),
    ("SSG 08 | 渐变强酸", "安全处所收藏品", 0.01483),
    ("AK-47 | 寒翠", "寒带收藏品", 0.370743),
    ("SSG 08 | 芝诺悖论", "狩猎运动收藏品", 0.346027),
    ("USP消音版 | 阿尔卑斯迷彩", "狩猎运动收藏品", 0.336141),
    ("P250 | 随便玩玩", "狩猎运动收藏品", 0.346027),
    ("新星 | 约克夏", "狩猎运动收藏品", 0.494324),
    ("USP消音版 | 椰风花语", "寒带收藏品", 0.271878),
    ("M4A1消音版 | 多变迷彩", "炙热沙城 II 收藏品", 0.296595),
    ("法玛斯 | 摧枯拉朽", "安全处所收藏品", 0.296595),
    ("FN57 | 银白石英", "安全处所收藏品", 0.19773),
    ("PP-野牛 | 黄铜", "炙热沙城 II 收藏品", 0.494324),
    ("SG 553 | 大马士革钢", "炙热沙城 II 收藏品", 0.494324),
    ("MAC-10 | 白杨丛", "寒带收藏品", 0.336141),
    ("MP5-SD | 黄金榕", "寒带收藏品", 0.296595),
    ("XM1014 | 铜斑迷彩", "寒带收藏品", 0.296595),
]

print("验证产物 avg_n 是否一致 (期望 avg_n = 0.494324):")
print("-" * 80)
all_match = True
for name, coll, wear in outputs:
    # 去掉磨损等级后缀
    base_name = name.split(' (')[0] if '(' in name else name
    row = conn.execute(
        "SELECT min_float, max_float FROM skins WHERE name=? AND collection=? AND is_stattrak=0 LIMIT 1",
        (base_name, coll)
    ).fetchone()
    if not row:
        print(f"  {base_name}: NOT FOUND in DB")
        all_match = False
        continue
    mn, mx = row["min_float"], row["max_float"]
    span = mx - mn
    if span <= 0:
        print(f"  {base_name}: span=0")
        continue
    derived_n = (wear - mn) / span
    match = abs(derived_n - 0.494324) < 0.001
    if not match:
        all_match = False
    status = "✓" if match else "✗"
    print(f"  {status} {base_name}: wear={wear:.6f}, range=[{mn}, {mx}], avg_n={derived_n:.6f}")

print("-" * 80)
print(f"全部匹配: {'是' if all_match else '否'}")
conn.close()
