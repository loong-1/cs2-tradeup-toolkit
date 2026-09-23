"""购买列表管理：从查询结果加入商品，待用户确认后批量购买。"""
from datetime import datetime
from typing import List, Dict, Any

from core.data_manager import get_conn


# 状态
STATUS_PENDING = "pending"          # 待购买
STATUS_BOUGHT = "bought"            # 已购买
STATUS_FAILED = "failed"            # 购买失败
STATUS_REMOVED = "removed"          # 已移除


def add_item(order: Dict[str, Any]) -> int:
    """把一条查询结果加入购买列表。返回新记录 id。

    order 至少包含 platform, order_id, price, wear, wear_name。
    item_name 为可选（由调用方提供）。
    goods_id 为可选（Buff 购买需要）。
    """
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO buy_queue "
        "(item_name, platform, order_id, price, wear, wear_name, "
        "paintseed, assetid, status, added_at, goods_id, item_url) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (order.get("item_name", ""),
         order.get("platform", ""),
         order.get("order_id", ""),
         float(order.get("price", 0) or 0),
         float(order.get("wear", 0) or 0),
         order.get("wear_name", ""),
         order.get("paintseed", ""),
         order.get("assetid", ""),
         STATUS_PENDING,
         datetime.now().isoformat(timespec="seconds"),
         order.get("goods_id", ""),
         order.get("item_url", ""))
    )
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    return new_id


def list_items(status: str = None) -> List[Dict[str, Any]]:
    """返回购买列表中的物品（按加入顺序）。

    :param status: 仅返回指定状态；None 表示全部
    """
    conn = get_conn()
    if status:
        rows = conn.execute(
            "SELECT id, item_name, platform, order_id, price, wear, "
            "wear_name, paintseed, assetid, status, added_at, "
            "purchased_at, order_no, remark, goods_id, item_url "
            "FROM buy_queue WHERE status = ? ORDER BY id",
            (status,)).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, item_name, platform, order_id, price, wear, "
            "wear_name, paintseed, assetid, status, added_at, "
            "purchased_at, order_no, remark, goods_id, item_url "
            "FROM buy_queue ORDER BY id").fetchall()
    conn.close()
    return [{
        "id": r[0], "item_name": r[1], "platform": r[2],
        "order_id": r[3], "price": r[4], "wear": r[5],
        "wear_name": r[6], "paintseed": r[7], "assetid": r[8],
        "status": r[9], "added_at": r[10],
        "purchased_at": r[11], "order_no": r[12], "remark": r[13],
        "goods_id": r[14], "item_url": r[15],
    } for r in rows]


def list_pending() -> List[Dict[str, Any]]:
    """返回所有待购买物品。"""
    return list_items(STATUS_PENDING)


def remove_item(item_id: int) -> None:
    """从购买列表移除一条记录。"""
    conn = get_conn()
    conn.execute("DELETE FROM buy_queue WHERE id = ?", (item_id,))
    conn.commit()
    conn.close()


def clear_all() -> None:
    conn = get_conn()
    conn.execute("DELETE FROM buy_queue")
    conn.commit()
    conn.close()


def clear_bought() -> None:
    """清除所有已购买/失败的记录。"""
    conn = get_conn()
    conn.execute(
        "DELETE FROM buy_queue WHERE status IN (?, ?)",
        (STATUS_BOUGHT, STATUS_FAILED))
    conn.commit()
    conn.close()


def mark_bought(item_id: int, order_no: str = "", remark: str = "") -> None:
    conn = get_conn()
    conn.execute(
        "UPDATE buy_queue SET status = ?, purchased_at = ?, "
        "order_no = ?, remark = ? WHERE id = ?",
        (STATUS_BOUGHT, datetime.now().isoformat(timespec="seconds"),
         order_no, remark, item_id))
    conn.commit()
    conn.close()


def mark_failed(item_id: int, remark: str = "") -> None:
    conn = get_conn()
    conn.execute(
        "UPDATE buy_queue SET status = ?, remark = ? WHERE id = ?",
        (STATUS_FAILED, remark, item_id))
    conn.commit()
    conn.close()


def reset_to_pending(item_id: int) -> None:
    """将失败项重置为待购买（用于重试）。"""
    conn = get_conn()
    conn.execute(
        "UPDATE buy_queue SET status = ?, remark = '' WHERE id = ?",
        (STATUS_PENDING, item_id))
    conn.commit()
    conn.close()


def list_failed() -> List[Dict[str, Any]]:
    """返回所有失败物品。"""
    return list_items(STATUS_FAILED)
