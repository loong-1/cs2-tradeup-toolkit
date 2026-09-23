import sys
sys.path.insert(0, 'f:/steamdt-project')
from tradeup_sim import database as db

conn = db.get_conn()

# 产物列表（名称, 收藏品, 预计磨损）
outputs = [
    ("截短霰弹枪 | 镀铜", "炙热沙城收藏品", 0.494324),
    ("AUG | 铜斑蛇", "炙热沙城收藏品", 0.248524),
    ("AWP | 蝮蛇迷彩", "炙热沙城收藏品", 0.425800),
    ("双持贝瑞塔 | 翡翠色调", "运河水城收藏品", 0.039546),
    ("SSG 08 | 橙黄镂刻", "运河水城收藏品", 0.247162),
    ("Tec-9 | 骨化之色", "雨林遗迹收藏品", 0.039546),
    ("MAC-10 | 核子花园", "死城之谜收藏品", 0.346027),
    ("XM1014 | 碾骨机", "死城之谜收藏品", 0.296595),
    ("MP9 | 落日", "死城之谜收藏品", 0.494324),
    ("加利尔AR | 渐变琥珀", "2021 炙热沙城 II 收藏品", 0.197730),
    ("G3SG1 | 碧藤青翠", "2021 炙热沙城 II 收藏品", 0.271878),
    ("SSG 08 | 渐变强酸", "安全处所收藏品", 0.014830),
    ("AK-47 | 寒翠", "寒带收藏品", 0.370743),
    ("SSG 08 | 芝诺悖论", "狩猎运动收藏品", 0.346027),
    ("USP消音版 | 阿尔卑斯迷彩", "狩猎运动收藏品", 0.336141),
    ("新星 | 约克夏", "狩猎运动收藏品", 0.494324),
    ("USP消音版 | 椰风花语", "寒带收藏品", 0.271878),
    ("M4A1消音版 | 多变迷彩", "炙热沙城 II 收藏品", 0.296595),
    ("法玛斯 | 摧枯拉朽", "安全处所收藏品", 0.296595),
    ("FN57 | 银白石英", "安全处所收藏品", 0.197730),
    ("PP-野牛 | 黄铜", "炙热沙城 II 收藏品", 0.494324),
    ("SG 553 | 大马士革钢", "炙热沙城 II 收藏品", 0.494324),
]

print(f"{'皮肤':<25} {'收藏':<20} {'min_f':>8} {'max_f':>8} {'range':>8} {'exp_wear':>10} {'avg_n':>8}")
print("-" * 110)
avg_ns = []
for name, coll, ew in outputs:
    row = conn.execute(
        "SELECT min_float, max_float FROM skins WHERE name=? AND collection=? AND is_stattrak=0",
        (name, coll)).fetchone()
    if row:
        mn, mx = row['min_float'], row['max_float']
        rng = mx - mn
        avg_n = (ew - mn) / rng if rng > 0 else 0
        avg_ns.append(avg_n)
        print(f"{name:<25} {coll:<20} {mn:>8.4f} {mx:>8.4f} {rng:>8.4f} {ew:>10.6f} {avg_n:>8.4f}")
    else:
        print(f"{name:<25} {coll:<20} {'NOT FOUND':>8}")

conn.close()
print(f"\navg_n 统计: min={min(avg_ns):.4f}, max={max(avg_ns):.4f}, mean={sum(avg_ns)/len(avg_ns):.4f}")
