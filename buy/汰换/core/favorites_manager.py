"""收藏夹管理：分组（每组最多 10 个物品）+ 最低价/最低磨损值缓存。"""
from datetime import datetime
from typing import List, Dict, Any

from core.data_manager import get_conn


MAX_ITEMS_PER_GROUP = 10  # 每组最多 10 个物品


def list_groups() -> List[str]:
    """返回所有分组名（按字母序）。"""
    conn = get_conn()
    rows = conn.execute(
        "SELECT DISTINCT group_name FROM favorites ORDER BY group_name"
    ).fetchall()
    conn.close()
    return [r[0] for r in rows]


def list_items(group_name: str) -> List[Dict[str, Any]]:
    """返回指定分组下所有物品。"""
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, group_name, item_name, buff_goods_id, c5_market_hash_name, "
        "c5_app_id, min_price, min_wear, last_checked, added_at "
        "FROM favorites WHERE group_name = ? ORDER BY id",
        (group_name,)
    ).fetchall()
    conn.close()
    return [{
        "id": r[0], "group_name": r[1], "item_name": r[2],
        "buff_goods_id": r[3], "c5_market_hash_name": r[4],
        "c5_app_id": r[5], "min_price": r[6], "min_wear": r[7],
        "last_checked": r[8], "added_at": r[9],
    } for r in rows]


def count_items(group_name: str) -> int:
    """统计分组内物品数。"""
    conn = get_conn()
    row = conn.execute(
        "SELECT COUNT(*) FROM favorites WHERE group_name = ?",
        (group_name,)
    ).fetchone()
    conn.close()
    return row[0] if row else 0


def add_item(group_name: str, item: Dict[str, Any]) -> int:
    """添加物品到分组。若分组已满（>=10）返回 -1。

    :return: 新记录 id；分组已满返回 -1。
    """
    if count_items(group_name) >= MAX_ITEMS_PER_GROUP:
        return -1
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO favorites "
        "(group_name, item_name, buff_goods_id, c5_market_hash_name, "
        "c5_app_id, min_price, min_wear, last_checked, added_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (group_name, item.get("item_name", ""),
         item.get("buff_goods_id", ""),
         item.get("c5_market_hash_name", ""),
         item.get("c5_app_id", "730"),
         item.get("min_price"),
         item.get("min_wear"),
         item.get("last_checked"),
         datetime.now().isoformat(timespec="seconds"))
    )
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    return new_id


def remove_item(item_id: int) -> None:
    """按 id 删除单个物品。"""
    conn = get_conn()
    conn.execute("DELETE FROM favorites WHERE id = ?", (item_id,))
    conn.commit()
    conn.close()


def remove_group(group_name: str) -> None:
    """删除整个分组。"""
    conn = get_conn()
    conn.execute("DELETE FROM favorites WHERE group_name = ?", (group_name,))
    conn.commit()
    conn.close()


def update_check_result(item_id: int, min_price: float, min_wear: float,
                        platform: str = "") -> None:
    """更新物品最近一次查询的最低价/最低磨损值缓存。"""
    conn = get_conn()
    conn.execute(
        "UPDATE favorites SET min_price = ?, min_wear = ?, "
        "last_checked = ? WHERE id = ?",
        (min_price, min_wear,
         datetime.now().isoformat(timespec="seconds"), item_id)
    )
    conn.commit()
    conn.close()
