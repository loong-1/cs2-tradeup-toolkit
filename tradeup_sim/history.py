"""历史记录管理：SQLite 存储 + 导出 CSV/JSON。"""
from __future__ import annotations

import csv
import json
import os
import sqlite3
from datetime import datetime
from typing import List, Dict, Any, Optional

from .database import get_conn, DB_PATH


def save_history(materials: List[Dict], result: Dict,
                 seed: Optional[int] = None,
                 batch_size: int = 1,
                 db_path: str = DB_PATH) -> int:
    """保存一次模拟记录；返回记录 id。"""
    conn = get_conn(db_path)
    ts = datetime.now().isoformat(timespec="seconds")
    cur = conn.execute(
        "INSERT INTO history (ts, seed, batch_size, materials_json, result_json) "
        "VALUES (?,?,?,?,?)",
        (ts, seed, batch_size, json.dumps(materials, ensure_ascii=False),
         json.dumps(result, ensure_ascii=False)),
    )
    conn.commit()
    rid = cur.lastrowid
    conn.close()
    return rid


def list_history(limit: int = 200,
                 db_path: str = DB_PATH) -> List[Dict[str, Any]]:
    conn = get_conn(db_path)
    rows = conn.execute(
        "SELECT id, ts, seed, batch_size, materials_json, result_json "
        "FROM history ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    out = []
    for r in rows:
        out.append({
            "id": r["id"],
            "ts": r["ts"],
            "seed": r["seed"],
            "batch_size": r["batch_size"],
            "materials": json.loads(r["materials_json"]),
            "result": json.loads(r["result_json"]),
        })
    return out


def clear_history(db_path: str = DB_PATH) -> int:
    conn = get_conn(db_path)
    cur = conn.execute("DELETE FROM history")
    conn.commit()
    n = cur.rowcount
    conn.close()
    return n


# ---------- 导出 ----------

def export_history_csv(rows: List[Dict], path: str) -> int:
    """导出历史记录到 CSV；返回行数。"""
    fieldnames = ["id", "ts", "seed", "batch_size",
                  "output_skin", "output_wear", "output_wear_grade",
                  "is_stattrak", "probability", "material_cost",
                  "output_price", "profit", "roi_pct"]
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            res = r.get("result", {})
            w.writerow({
                "id": r.get("id"),
                "ts": r.get("ts"),
                "seed": r.get("seed"),
                "batch_size": r.get("batch_size"),
                "output_skin": res.get("output_skin", ""),
                "output_wear": res.get("output_wear", ""),
                "output_wear_grade": res.get("output_wear_grade", ""),
                "is_stattrak": res.get("is_stattrak", ""),
                "probability": res.get("probability", ""),
                "material_cost": res.get("material_cost", ""),
                "output_price": res.get("output_price", ""),
                "profit": res.get("profit", ""),
                "roi_pct": res.get("roi_pct", ""),
            })
    return len(rows)


def export_history_json(rows: List[Dict], path: str) -> None:
    """导出历史记录到 JSON。"""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)


def export_distribution_csv(dist: Dict, path: str) -> int:
    """导出概率分布到 CSV。"""
    fieldnames = ["skin", "collection", "probability", "est_wear",
                  "wear_grade", "price", "is_stattrak"]
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for row in dist.get("rows", []):
            s = row["skin"]
            w.writerow({
                "skin": s.name,
                "collection": s.collection,
                "probability": f"{row['probability']:.4f}",
                "est_wear": f"{row['est_wear']:.6f}",
                "wear_grade": row["wear_grade"],
                "price": f"{row['price']:.2f}",
                "is_stattrak": row["is_stattrak"],
            })
    return len(dist.get("rows", []))
