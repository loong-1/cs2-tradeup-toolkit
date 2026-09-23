"""数据持久化：SQLite 操作 + 物品映射表读写 + 历史导出。"""
import csv
import os
import sqlite3
from datetime import datetime

from utils.config import DB_PATH, ITEMS_CSV_PATH, MAIN_ITEMS_CSV, C5_APP_ID


# ============ 磨损等级 中文 -> 英文 ============
WEAR_CN_TO_EN = {
    "崭新出厂": "Factory New",
    "略有磨损": "Minimal Wear",
    "久经沙场": "Field-Tested",
    "破损不堪": "Well-Worn",
    "战痕累累": "Battle-Scarred",
}

# 磨损等级 英文 -> 中文（反向映射）
WEAR_EN_TO_CN = {v: k for k, v in WEAR_CN_TO_EN.items()}


def wear_en_to_cn(wear_en: str) -> str:
    """将英文磨损等级（Steam market_hash_name 后缀）转换为中文。

    无法识别时返回空串。
    """
    if not wear_en:
        return ""
    return WEAR_EN_TO_CN.get(wear_en.strip(), "")


def wear_cn_to_en(wear_cn: str) -> str:
    """将中文磨损等级转换为 Steam 市场哈希名所用的英文后缀。

    无法识别时返回空串（调用方需自行判断是否拼接）。
    """
    if not wear_cn:
        return ""
    return WEAR_CN_TO_EN.get(wear_cn.strip(), "")


def build_c5_market_hash_name(market_hash_base: str, wear_cn: str) -> str:
    """根据 CSV 中的「市场哈希名称」基础名 + 中文磨损等级，
    生成 C5 API 所需的完整 market_hash_name（带英文磨损后缀）。

    例：('AWP | Wildfire', '战痕累累') -> 'AWP | Wildfire (Battle-Scarred)'
    若磨损等级无法识别或基础名为空，则只返回基础名（不拼后缀）。
    """
    base = (market_hash_base or "").strip()
    if not base:
        return ""
    en = wear_cn_to_en(wear_cn)
    if not en:
        return base
    return f"{base} ({en})"


# ============ 数据库操作 ============

def get_conn():
    return sqlite3.connect(DB_PATH)


def init_db():
    conn = get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS operations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            item_name TEXT,
            wear REAL,
            platform TEXT,
            price REAL,
            quantity INTEGER,
            op_type TEXT,
            status TEXT,
            order_id TEXT,
            remark TEXT
        )
    """)
    # 收藏夹分组表：每组最多 10 个物品
    conn.execute("""
        CREATE TABLE IF NOT EXISTS favorites (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_name TEXT NOT NULL,
            item_name TEXT NOT NULL,
            buff_goods_id TEXT,
            c5_market_hash_name TEXT,
            c5_app_id TEXT,
            min_price REAL,
            min_wear REAL,
            last_checked TEXT,
            added_at TEXT NOT NULL
        )
    """)
    # 价格检测历史表：每次检测一条记录
    conn.execute("""
        CREATE TABLE IF NOT EXISTS price_monitor_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            item_name TEXT NOT NULL,
            platform TEXT,
            min_price REAL,
            min_wear REAL,
            sample_count INTEGER,
            remark TEXT
        )
    """)
    # 查询列表（排队查询）：物品加入后串行查询，查询完成10分钟后自动删除
    conn.execute("""
        CREATE TABLE IF NOT EXISTS query_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_name TEXT NOT NULL,
            buff_goods_id TEXT,
            c5_market_hash_name TEXT,
            c5_app_id TEXT,
            status TEXT DEFAULT 'pending',
            added_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT,
            result_count INTEGER DEFAULT 0,
            min_price REAL,
            min_wear REAL,
            result_json TEXT
        )
    """)
    # 购买列表：从查询结果加入，待用户确认后购买
    conn.execute("""
        CREATE TABLE IF NOT EXISTS buy_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_name TEXT NOT NULL,
            platform TEXT,
            order_id TEXT,
            price REAL,
            wear REAL,
            wear_name TEXT,
            paintseed TEXT,
            assetid TEXT,
            status TEXT DEFAULT 'pending',
            added_at TEXT NOT NULL,
            purchased_at TEXT,
            order_no TEXT,
            remark TEXT,
            goods_id TEXT
        )
    """)
    # 兼容已有数据库：若 goods_id 列不存在则添加
    try:
        conn.execute("SELECT goods_id FROM buy_queue LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE buy_queue ADD COLUMN goods_id TEXT")
    # 兼容已有数据库：若 item_url 列不存在则添加
    try:
        conn.execute("SELECT item_url FROM buy_queue LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE buy_queue ADD COLUMN item_url TEXT")
    conn.commit()
    conn.close()


def log_operation(item_name, wear, platform, price, quantity,
                  op_type, status, order_id="", remark=""):
    """记录一条操作（查询/购买）。"""
    conn = get_conn()
    conn.execute("""
        INSERT INTO operations
            (timestamp, item_name, wear, platform, price, quantity,
             op_type, status, order_id, remark)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (datetime.now().isoformat(timespec="seconds"),
          item_name, wear, platform, price, quantity,
          op_type, status, order_id, remark))
    conn.commit()
    conn.close()


def query_history(date_from=None, date_to=None, item_name=None,
                   platform=None, op_type=None):
    """按条件查询历史记录，返回 row 列表。"""
    conn = get_conn()
    sql = "SELECT * FROM operations WHERE 1=1"
    params = []
    if date_from:
        sql += " AND timestamp >= ?"
        params.append(date_from)
    if date_to:
        sql += " AND timestamp <= ?"
        params.append(date_to)
    if item_name:
        sql += " AND item_name LIKE ?"
        params.append(f"%{item_name}%")
    if platform:
        sql += " AND platform = ?"
        params.append(platform)
    if op_type:
        sql += " AND op_type = ?"
        params.append(op_type)
    sql += " ORDER BY timestamp DESC"
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return rows


def export_history_csv(rows, filepath):
    """将查询结果导出为 CSV。"""
    with open(filepath, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(
            ["ID", "时间戳", "物品名称", "磨损", "平台", "价格",
             "数量", "操作类型", "状态", "订单ID", "备注"])
        writer.writerows(rows)


# ---------- 价格历史：限流快照记录 ----------

# 同 item_name 的快照间隔（秒）：低于此间隔且价格变化 < 阈值时跳过
_PRICE_MONITOR_SNAPSHOT_INTERVAL = 300   # 5 分钟
_PRICE_MONITOR_CHANGE_THRESHOLD = 0.01   # 价格变化 < 1% 视为未变
_PRICE_MONITOR_RETENTION_DAYS = 7        # 历史自动保留天数
_PRICE_MONITOR_MAX_PER_ITEM = 200        # 每个物品最多保留的历史条数


def log_price_monitor(item_name, platform, min_price, min_wear,
                     sample_count=0, remark=""):
    """智能快照记录：非必要不写入，避免历史无限膨胀。

    写入条件（满足任一）：
    1. 距上次该 item_name 记录超过 _PRICE_MONITOR_SNAPSHOT_INTERVAL 秒
    2. 价格变化超过 _PRICE_MONITOR_CHANGE_THRESHOLD（1%）
    3. 本次是失败记录（min_price 为 None）
    """
    now_ts = datetime.now()
    conn = get_conn()
    try:
        # 查该 item_name 最近一条
        row = conn.execute(
            "SELECT min_price, timestamp FROM price_monitor_history "
            "WHERE item_name = ? ORDER BY id DESC LIMIT 1",
            (item_name,)).fetchone()

        should_write = False
        if row is None:
            should_write = True   # 首次记录
        elif min_price is None:
            should_write = True   # 失败记录总是记
        else:
            try:
                last_price = float(row[0] or 0)
                last_ts = datetime.fromisoformat(row[1])
                price_delta = abs(float(min_price) - last_price)
                price_rel = (price_delta / last_price) if last_price > 0 else 0
                time_gap = (now_ts - last_ts).total_seconds()
                # 满足任一条件才写：时间够久 或 价格变了
                if time_gap >= _PRICE_MONITOR_SNAPSHOT_INTERVAL \
                        or price_rel >= _PRICE_MONITOR_CHANGE_THRESHOLD:
                    should_write = True
            except (ValueError, TypeError):
                should_write = True

        if should_write:
            conn.execute("""
                INSERT INTO price_monitor_history
                    (timestamp, item_name, platform, min_price, min_wear,
                     sample_count, remark)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (now_ts.isoformat(timespec="seconds"),
                  item_name, platform, min_price, min_wear,
                  sample_count, remark))

            # 每个物品只保留最近 N 条（防单物品爆冲）
            conn.execute("""
                DELETE FROM price_monitor_history WHERE item_name = ?
                AND id NOT IN (
                    SELECT id FROM price_monitor_history
                    WHERE item_name = ? ORDER BY id DESC
                    LIMIT ?
                )
            """, (item_name, item_name, _PRICE_MONITOR_MAX_PER_ITEM))

            # 定期清理全局过期记录
            cutoff = (now_ts
                      - __import__("datetime").timedelta(
                          days=_PRICE_MONITOR_RETENTION_DAYS))
            conn.execute(
                "DELETE FROM price_monitor_history WHERE timestamp < ?",
                (cutoff.isoformat(timespec="seconds"),))

            conn.commit()
    finally:
        conn.close()


def query_price_monitor_history(item_name=None, limit=200, success_only=False):
    """查询价格检测历史，返回 row 列表（最新在前）。

    :param item_name: 精确匹配（不再用 LIKE，item_name 带档位后缀
                      如「XXX（战痕累累）」，精确匹配更安全）
    :param success_only: True 时只返回 min_price 不为空的成功记录
    """
    conn = get_conn()
    sql = "SELECT * FROM price_monitor_history WHERE 1=1"
    params = []
    if item_name:
        sql += " AND item_name = ?"
        params.append(item_name)
    if success_only:
        sql += " AND min_price IS NOT NULL"
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return rows


def get_price_history_summary(item_name: str) -> dict:
    """取某物品历史快照的统计摘要（给面板显示「价格趋势」小提示用）。

    返回 dict: {
        'first_ts', 'last_ts', 'count',
        'min_price', 'max_price', 'avg_price',
        'prices': [(ts_str, price_float), ...]  # 时间升序
    }
    或 None（无数据）。
    """
    rows = query_price_monitor_history(item_name, limit=500, success_only=True)
    if not rows:
        return None
    prices = []
    for r in reversed(rows):   # 时间升序
        if r[4] is not None:
            prices.append((r[1], float(r[4])))
    if not prices:
        return None
    only = [p for _, p in prices]
    return {
        "first_ts": prices[0][0],
        "last_ts": prices[-1][0],
        "count": len(prices),
        "min_price": min(only),
        "max_price": max(only),
        "avg_price": sum(only) / len(only),
        "prices": prices,
    }


def clear_price_monitor_history(item_name: str | None = None):
    """清空价格检测历史（全部 or 指定物品）。"""
    conn = get_conn()
    if item_name:
        conn.execute(
            "DELETE FROM price_monitor_history WHERE item_name = ?",
            (item_name,))
    else:
        conn.execute("DELETE FROM price_monitor_history")
    conn.commit()
    conn.close()


# ============ 物品映射表读写 ============

def load_items():
    """读取 items.csv，返回 dict 列表。"""
    if not ITEMS_CSV_PATH or not os.path.exists(ITEMS_CSV_PATH):
        return []
    items = []
    with open(ITEMS_CSV_PATH, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            items.append({
                "item_name": row.get("item_name", "").strip(),
                "buff_goods_id": row.get("buff_goods_id", "").strip(),
                "c5_market_hash_name": row.get("c5_market_hash_name", "").strip(),
                "c5_app_id": row.get("c5_app_id", "730").strip() or "730",
            })
    return items


def save_items(items):
    """全量覆盖写入 items.csv。"""
    with open(ITEMS_CSV_PATH, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(
            f, fieldnames=["item_name", "buff_goods_id",
                           "c5_market_hash_name", "c5_app_id"])
        writer.writeheader()
        for row in items:
            writer.writerow(row)


def load_items_from_main_csv():
    """从「物品箱子磨损对照表1_有效磨损及goods_id.csv」加载物品列表。

    每行对应一个「皮肤名称 + 磨损等级」的 Buff goods_id 条目。
    显示名格式为「皮肤名称 - 磨损等级」。
    返回 dict 列表，字段与 load_items() 一致。
    """
    if not MAIN_ITEMS_CSV or not os.path.exists(MAIN_ITEMS_CSV):
        return []
    items = []
    with open(MAIN_ITEMS_CSV, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            skin_name = (row.get("皮肤名称") or "").strip()
            wear_level = (row.get("磨损") or "").strip()
            goods_id = (row.get("buff_goods_id") or "").strip()
            market_hash = (row.get("市场哈希名称") or "").strip()
            if not skin_name or not goods_id:
                continue
            display = f"{skin_name} - {wear_level}" if wear_level else skin_name
            # C5 API 需要带英文磨损后缀的完整 market_hash_name
            c5_hash = build_c5_market_hash_name(market_hash, wear_level)
            items.append({
                "item_name": display,
                "buff_goods_id": goods_id,
                "c5_market_hash_name": c5_hash,
                "c5_app_id": str(C5_APP_ID),
            })
    return items


# 主 CSV 原始列头顺序（物品总览表格用）
MAIN_CSV_COLUMNS = [
    "收藏品名称", "皮肤名称", "品质", "磨损区间", "磨损",
    "buff_goods_id", "收藏品英文名称", "品质代码", "市场哈希名称",
    "crate_id", "price_buff",
]


def load_items_from_main_csv_full():
    """读取主 CSV，返回完整字段的 dict 列表（用于表格展示/筛选）。"""
    if not MAIN_ITEMS_CSV or not os.path.exists(MAIN_ITEMS_CSV):
        return []
    rows = []
    with open(MAIN_ITEMS_CSV, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            cleaned = {k: (v or "").strip() for k, v in row.items()}
            rows.append(cleaned)
    return rows


def get_unique_rarities(rows=None):
    """返回品质值去重列表（按出现频次降序），首项为「全部」。"""
    if rows is None:
        rows = load_items_from_main_csv_full()
    counter = {}
    for r in rows:
        v = r.get("品质", "")
        if not v:
            continue
        counter[v] = counter.get(v, 0) + 1
    ordered = sorted(counter.items(), key=lambda x: -x[1])
    return ["全部"] + [k for k, _ in ordered]


def get_unique_collections(rows=None):
    """返回收藏品名称去重列表（按出现频次降序），首项为「全部」。"""
    if rows is None:
        rows = load_items_from_main_csv_full()
    counter = {}
    for r in rows:
        v = r.get("收藏品名称", "")
        if not v:
            continue
        counter[v] = counter.get(v, 0) + 1
    ordered = sorted(counter.items(), key=lambda x: -x[1])
    return ["全部"] + [k for k, _ in ordered]



