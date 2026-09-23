"""SQLite 数据层：建表 + 从 CSV 导入皮肤数据 + 查询接口。

皮肤表 skins 设计：
  一个皮肤在不同磨损档有不同的 buff_goods_id/price，所以 (name, wear_grade) 是复合主键。
  min_float/max_float 是该皮肤本身的磨损上下限（所有磨损档相同）。
"""
from __future__ import annotations

import csv
import os
import re
import sqlite3
from typing import List, Optional, Dict, Tuple

from .models import Skin, QUALITY_LEVEL, QUALITY_ORDER, WEAR_GRADES

DB_PATH = os.path.join(os.path.dirname(__file__), "data", "tradeup.db")
CSV_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "数据集", "物品箱子磨损对照表1_有效磨损及goods_id.csv",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS skins (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    collection    TEXT NOT NULL,
    name          TEXT NOT NULL,
    quality       TEXT NOT NULL,
    min_float     REAL NOT NULL,
    max_float     REAL NOT NULL,
    is_stattrak   INTEGER NOT NULL DEFAULT 0,
    wear_grade    TEXT NOT NULL DEFAULT '',
    market_hash   TEXT DEFAULT '',
    buff_goods_id INTEGER DEFAULT 0,
    price         REAL DEFAULT 0.0,
    UNIQUE(name, wear_grade, is_stattrak)
);
CREATE INDEX IF NOT EXISTS idx_skins_coll_qual ON skins(collection, quality);
CREATE INDEX IF NOT EXISTS idx_skins_name ON skins(name);

CREATE TABLE IF NOT EXISTS history (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT NOT NULL,
    seed          INTEGER,
    batch_size    INTEGER DEFAULT 1,
    materials_json TEXT NOT NULL,
    result_json   TEXT NOT NULL
);
"""


def get_conn(db_path: str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


def _parse_wear_range(s: str) -> Tuple[float, float]:
    """解析 '0.01 ~ 0.7' → (0.01, 0.7)。"""
    m = re.findall(r"[\d.]+", s.replace(",", ""))
    if len(m) >= 2:
        return float(m[0]), float(m[1])
    return 0.0, 1.0


def import_csv(csv_path: str = CSV_PATH, db_path: str = DB_PATH,
               verbose: bool = True) -> int:
    """从 CSV 导入皮肤数据到 SQLite；返回导入行数。"""
    if not os.path.exists(csv_path):
        raise FileNotFoundError(f"CSV 不存在: {csv_path}")
    conn = get_conn(db_path)
    cur = conn.cursor()
    rows_inserted = 0
    with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            name = (row.get("皮肤名称") or "").strip()
            if not name:
                continue
            quality = (row.get("品质") or "").strip()
            if quality not in QUALITY_LEVEL:
                continue  # 跳过未知品质
            # 违禁品已从游戏移除，禁用
            if quality == "违禁品":
                continue
            is_st = 1 if "StatTrak" in name else 0
            min_f, max_f = _parse_wear_range(row.get("磨损区间", "0 ~ 1"))
            wear_grade = (row.get("磨损") or "").strip()
            try:
                gid = int(row.get("buff_goods_id") or 0)
            except ValueError:
                gid = 0
            try:
                price = float(row.get("price_buff") or 0)
            except ValueError:
                price = 0.0
            cur.execute(
                """INSERT OR REPLACE INTO skins
                   (collection, name, quality, min_float, max_float,
                    is_stattrak, wear_grade, market_hash, buff_goods_id, price)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (row.get("收藏品名称", "").strip(), name, quality,
                 min_f, max_f, is_st, wear_grade,
                 row.get("市场哈希名称", "").strip(), gid, price),
            )
            rows_inserted += 1
    conn.commit()
    conn.close()
    if verbose:
        print(f"[DB] 已导入 {rows_inserted} 条皮肤记录 → {db_path}")
    return rows_inserted


def ensure_data(db_path: str = DB_PATH, csv_path: str = CSV_PATH) -> None:
    """如果 skins 表为空，自动从 CSV 导入。"""
    conn = get_conn(db_path)
    cnt = conn.execute("SELECT COUNT(*) FROM skins").fetchone()[0]
    conn.close()
    if cnt == 0:
        import_csv(csv_path, db_path)


# ---------- 查询接口 ----------

def list_collections(db_path: str = DB_PATH) -> List[str]:
    conn = get_conn(db_path)
    rows = conn.execute(
        "SELECT DISTINCT collection FROM skins ORDER BY collection"
    ).fetchall()
    conn.close()
    return [r["collection"] for r in rows]


def list_qualities() -> List[str]:
    """返回可用的品质列表（排除消费级和违禁品）。"""
    return [q for q in QUALITY_ORDER if q not in ("消费级", "违禁品")]


def get_skins_by_collection_quality(
    collection: str, quality: str,
    is_stattrak: bool = False,
    db_path: str = DB_PATH,
) -> List[Skin]:
    """取某收藏品某品质的所有皮肤（去重：同名皮肤只取一行，min/max 相同）。"""
    conn = get_conn(db_path)
    rows = conn.execute(
        """SELECT DISTINCT name, collection, quality, min_float, max_float,
                  is_stattrak, market_hash, buff_goods_id, price
           FROM skins
           WHERE collection=? AND quality=? AND is_stattrak=?
           ORDER BY name""",
        (collection, quality, 1 if is_stattrak else 0),
    ).fetchall()
    conn.close()
    out: List[Skin] = []
    seen: set = set()
    for r in rows:
        if r["name"] in seen:
            continue
        seen.add(r["name"])
        out.append(Skin(
            collection=r["collection"], name=r["name"], quality=r["quality"],
            min_float=r["min_float"], max_float=r["max_float"],
            is_stattrak=bool(r["is_stattrak"]),
            market_hash_name=r["market_hash"] or "",
            buff_goods_id=r["buff_goods_id"] or 0,
            price=r["price"] or 0.0,
        ))
    return out


def get_skins_by_collections_quality(
    collections: List[str], quality: str,
    is_stattrak: bool = False,
    db_path: str = DB_PATH,
) -> List[Skin]:
    """取多个收藏品某品质的所有皮肤（去重）。"""
    if not collections:
        return []
    conn = get_conn(db_path)
    placeholders = ",".join("?" * len(collections))
    sql = (f"""SELECT DISTINCT name, collection, quality, min_float, max_float,
                  is_stattrak, market_hash, buff_goods_id, price
           FROM skins
           WHERE collection IN ({placeholders}) AND quality=? AND is_stattrak=?
           ORDER BY name""")
    params = list(collections) + [quality, 1 if is_stattrak else 0]
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    out: List[Skin] = []
    seen: set = set()
    for r in rows:
        if r["name"] in seen:
            continue
        seen.add(r["name"])
        out.append(Skin(
            collection=r["collection"], name=r["name"], quality=r["quality"],
            min_float=r["min_float"], max_float=r["max_float"],
            is_stattrak=bool(r["is_stattrak"]),
            market_hash_name=r["market_hash"] or "",
            buff_goods_id=r["buff_goods_id"] or 0,
            price=r["price"] or 0.0,
        ))
    return out


def get_skin_price(name: str, wear_grade: str,
                   is_stattrak: bool = False,
                   db_path: str = DB_PATH) -> float:
    """取某皮肤某磨损档的参考价。"""
    conn = get_conn(db_path)
    r = conn.execute(
        "SELECT price FROM skins WHERE name=? AND wear_grade=? AND is_stattrak=?",
        (name, wear_grade, 1 if is_stattrak else 0),
    ).fetchone()
    conn.close()
    return (r["price"] if r and r["price"] else 0.0)


def get_skin_market_ids(name: str, wear_grade: str,
                        is_stattrak: bool = False,
                        db_path: str = DB_PATH) -> Dict:
    """取某皮肤某磨损档的 buff_goods_id 和 c5_market_hash_name（供实时价格查询用）。"""
    conn = get_conn(db_path)
    r = conn.execute(
        "SELECT buff_goods_id, market_hash FROM skins "
        "WHERE name=? AND wear_grade=? AND is_stattrak=? LIMIT 1",
        (name, wear_grade, 1 if is_stattrak else 0),
    ).fetchone()
    conn.close()
    if not r:
        return {"buff_goods_id": 0, "c5_market_hash_name": ""}
    return {
        "buff_goods_id": r["buff_goods_id"] or 0,
        "c5_market_hash_name": r["market_hash"] or "",
    }


def clear_all_prices(db_path: str = DB_PATH) -> int:
    """将所有皮肤的 price 重置为 0。返回更新的行数。"""
    conn = get_conn(db_path)
    cur = conn.execute("UPDATE skins SET price=0.0")
    conn.commit()
    n = cur.rowcount
    conn.close()
    return n


def update_skin_price(name: str, wear_grade: str, price: float,
                      is_stattrak: bool = False,
                      db_path: str = DB_PATH):
    """更新某皮肤某磨损档的价格。"""
    conn = get_conn(db_path)
    conn.execute(
        "UPDATE skins SET price=? WHERE name=? AND wear_grade=? AND is_stattrak=?",
        (price, name, wear_grade, 1 if is_stattrak else 0),
    )
    conn.commit()
    conn.close()


def get_all_price_targets(db_path: str = DB_PATH) -> List[Dict]:
    """获取所有需要更新价格的 (name, wear_grade, market_hash, buff_goods_id, is_stattrak)。"""
    conn = get_conn(db_path)
    rows = conn.execute(
        "SELECT DISTINCT name, wear_grade, market_hash, buff_goods_id, is_stattrak "
        "FROM skins WHERE market_hash != '' OR buff_goods_id > 0"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_collections_with_quality(quality: str, is_stattrak: bool = False,
                                 db_path: str = DB_PATH) -> List[str]:
    """返回所有包含指定稀有度皮肤的收藏品列表。"""
    conn = get_conn(db_path)
    rows = conn.execute(
        "SELECT DISTINCT collection FROM skins "
        "WHERE quality=? AND is_stattrak=? ORDER BY collection",
        (quality, 1 if is_stattrak else 0),
    ).fetchall()
    conn.close()
    return [r["collection"] for r in rows]


def search_skins(keyword: str, quality: Optional[str] = None,
                 is_stattrak: bool = False,
                 db_path: str = DB_PATH, limit: int = 200) -> List[Skin]:
    """按名称搜索皮肤（用于材料选择）。"""
    conn = get_conn(db_path)
    sql = ("SELECT DISTINCT name, collection, quality, min_float, max_float, "
           "is_stattrak, market_hash FROM skins "
           "WHERE name LIKE ? AND is_stattrak=?")
    params: list = [f"%{keyword}%", 1 if is_stattrak else 0]
    if quality:
        sql += " AND quality=?"
        params.append(quality)
    sql += " ORDER BY name LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    seen: set = set()
    out: List[Skin] = []
    for r in rows:
        if r["name"] in seen:
            continue
        seen.add(r["name"])
        out.append(Skin(
            collection=r["collection"], name=r["name"], quality=r["quality"],
            min_float=r["min_float"], max_float=r["max_float"],
            is_stattrak=bool(r["is_stattrak"]),
            market_hash_name=r["market_hash"] or "",
        ))
    return out
