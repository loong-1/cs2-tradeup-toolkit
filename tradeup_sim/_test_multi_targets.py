# -*- coding: utf-8 -*-
"""测试多个目标皮肤的磨损预测准确性。"""
import sys
sys.path.insert(0, 'f:/steamdt-project')
from tradeup_sim import database as db
from tradeup_sim import engine
from tradeup_sim.models import Skin

# 用实际数据中的产物作为目标
targets = [
    ("截短霰弹枪 | 镀铜", "炙热沙城收藏品", 0.494324),
    ("AUG | 铜斑蛇", "炙热沙城收藏品", 0.248524),
    ("AWP | 蝮蛇迷彩", "炙热沙城收藏品", 0.425800),
    ("双持贝瑞塔 | 翡翠色调", "运河水城收藏品", 0.039546),
    ("SSG 08 | 橙黄镂刻", "运河水城收藏品", 0.247162),
    ("AK-47 | 寒翠", "寒带收藏品", 0.370743),
    ("新星 | 约克夏", "狩猎运动收藏品", 0.494324),
    ("M4A1消音版 | 多变迷彩", "炙热沙城 II 收藏品", 0.296595),
    ("PP-野牛 | 黄铜", "炙热沙城 II 收藏品", 0.494324),
    ("SG 553 | 大马士革钢", "炙热沙城 II 收藏品", 0.494324),
]

conn = db.get_conn()
for name, coll, wear in targets:
    row = conn.execute(
        "SELECT * FROM skins WHERE name=? AND collection=? AND is_stattrak=0",
        (name, coll)
    ).fetchone()
    if not row:
        print(f"{name} ({coll}) NOT FOUND")
        continue
    target = Skin(
        collection=row["collection"], name=row["name"], quality=row["quality"],
        min_float=row["min_float"], max_float=row["max_float"],
    )
    span = target.max_float - target.min_float
    if span <= 0:
        continue
    req_avg_n = (wear - target.min_float) / span
    if not (0 <= req_avg_n <= 1):
        print(f"{name}: req_avg_n={req_avg_n:.4f} 超出范围，跳过")
        continue
    try:
        result = engine.optimize_recipe(
            target, wear, is_stattrak=False,
            max_candidates=50, tolerance=0.005,
            strategy="max_profit", top_k=5,
        )
        err = result["wear_error"]
        status = "OK" if err < 0.001 else "FAIL"
        print(f"[{status}] {name}: target={wear:.6f}, pred={result['predicted_wear']:.6f}, err={err:.6f}")
    except Exception as e:
        print(f"[ERR] {name}: {e}")
conn.close()
