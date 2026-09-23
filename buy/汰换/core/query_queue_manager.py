"""查询列表管理：物品排队、串行查询、结果缓存、10分钟自动清理。"""
import json
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional

from core.data_manager import get_conn


# 完成后保留时长（分钟）
AUTO_CLEANUP_MINUTES = 10

# 队列状态
STATUS_PENDING = "pending"      # 等待中
STATUS_RUNNING = "running"      # 查询中
STATUS_DONE = "done"            # 查询完成
STATUS_ERROR = "error"          # 出错


def add_item(item: Dict[str, Any]) -> int:
    """添加物品到队列末尾，返回新记录 id。"""
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO query_queue "
        "(item_name, buff_goods_id, c5_market_hash_name, c5_app_id, "
        "status, added_at) VALUES (?, ?, ?, ?, ?, ?)",
        (item.get("item_name", ""),
         item.get("buff_goods_id", ""),
         item.get("c5_market_hash_name", ""),
         item.get("c5_app_id", "730"),
         STATUS_PENDING,
         datetime.now().isoformat(timespec="seconds"))
    )
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    return new_id


def list_items() -> List[Dict[str, Any]]:
    """返回队列中所有物品（按入队顺序）。"""
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, item_name, buff_goods_id, c5_market_hash_name, "
        "c5_app_id, status, added_at, started_at, finished_at, "
        "result_count, min_price, min_wear, result_json "
        "FROM query_queue ORDER BY id"
    ).fetchall()
    conn.close()
    return [{
        "id": r[0], "item_name": r[1], "buff_goods_id": r[2],
        "c5_market_hash_name": r[3], "c5_app_id": r[4] or "730",
        "status": r[5], "added_at": r[6],
        "started_at": r[7], "finished_at": r[8],
        "result_count": r[9], "min_price": r[10], "min_wear": r[11],
        "result_json": r[12],
    } for r in rows]


def list_pending() -> List[Dict[str, Any]]:
    """返回所有 pending 状态的物品（按 id 升序）。"""
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, item_name, buff_goods_id, c5_market_hash_name, "
        "c5_app_id, status, added_at FROM query_queue "
        "WHERE status = ? ORDER BY id",
        (STATUS_PENDING,)
    ).fetchall()
    conn.close()
    return [{
        "id": r[0], "item_name": r[1], "buff_goods_id": r[2],
        "c5_market_hash_name": r[3], "c5_app_id": r[4] or "730",
        "status": r[5], "added_at": r[6],
    } for r in rows]


def mark_running(item_id: int) -> None:
    conn = get_conn()
    conn.execute(
        "UPDATE query_queue SET status = ?, started_at = ? WHERE id = ?",
        (STATUS_RUNNING, datetime.now().isoformat(timespec="seconds"), item_id)
    )
    conn.commit()
    conn.close()


def save_result(item_id: int, results: List[Dict[str, Any]]) -> None:
    """保存查询结果，并更新汇总字段。"""
    result_json = json.dumps(results, ensure_ascii=False)
    count = len(results)
    if results:
        # results 已按 price 升序，首项即最低价
        min_price = float(results[0].get("price", 0))
        min_wear_item = min(results, key=lambda x: float(x.get("wear", 0)))
        min_wear = float(min_wear_item.get("wear", 0))
    else:
        min_price = None
        min_wear = None
    conn = get_conn()
    conn.execute(
        "UPDATE query_queue SET status = ?, finished_at = ?, "
        "result_count = ?, min_price = ?, min_wear = ?, result_json = ? "
        "WHERE id = ?",
        (STATUS_DONE, datetime.now().isoformat(timespec="seconds"),
         count, min_price, min_wear, result_json, item_id)
    )
    conn.commit()
    conn.close()


def mark_error(item_id: int, error_msg: str) -> None:
    conn = get_conn()
    conn.execute(
        "UPDATE query_queue SET status = ?, finished_at = ?, "
        "result_json = ? WHERE id = ?",
        (STATUS_ERROR, datetime.now().isoformat(timespec="seconds"),
         json.dumps({"error": error_msg}, ensure_ascii=False), item_id)
    )
    conn.commit()
    conn.close()


def get_result(item_id: int) -> Optional[List[Dict[str, Any]]]:
    """读取某物品的查询结果 JSON。"""
    conn = get_conn()
    row = conn.execute(
        "SELECT result_json FROM query_queue WHERE id = ?", (item_id,)
    ).fetchone()
    conn.close()
    if not row or not row[0]:
        return None
    try:
        return json.loads(row[0])
    except (ValueError, TypeError):
        return None


def remove_item(item_id: int) -> None:
    conn = get_conn()
    conn.execute("DELETE FROM query_queue WHERE id = ?", (item_id,))
    conn.commit()
    conn.close()


def clear_all() -> None:
    conn = get_conn()
    conn.execute("DELETE FROM query_queue")
    conn.commit()
    conn.close()


def clear_done() -> None:
    """仅清除已完成（done/error）的记录。"""
    conn = get_conn()
    conn.execute(
        "DELETE FROM query_queue WHERE status IN (?, ?)",
        (STATUS_DONE, STATUS_ERROR))
    conn.commit()
    conn.close()


def cleanup_expired(now: Optional[datetime] = None) -> int:
    """清理已完成超过 AUTO_CLEANUP_MINUTES 分钟的记录，返回删除条数。"""
    if now is None:
        now = datetime.now()
    threshold = now - timedelta(minutes=AUTO_CLEANUP_MINUTES)
    conn = get_conn()
    cur = conn.execute(
        "DELETE FROM query_queue "
        "WHERE status IN (?, ?) AND finished_at IS NOT NULL "
        "AND finished_at < ?",
        (STATUS_DONE, STATUS_ERROR,
         threshold.isoformat(timespec="seconds")))
    deleted = cur.rowcount
    conn.commit()
    conn.close()
    return deleted


def reset_running_to_pending() -> int:
    """将所有 running 状态的记录重置为 pending（程序重启时调用）。"""
    conn = get_conn()
    cur = conn.execute(
        "UPDATE query_queue SET status = ? WHERE status = ?",
        (STATUS_PENDING, STATUS_RUNNING))
    reset = cur.rowcount
    conn.commit()
    conn.close()
    return reset
