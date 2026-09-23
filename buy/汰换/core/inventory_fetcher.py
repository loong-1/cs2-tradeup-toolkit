"""库存获取与持久化模块。

通过爬取 ECO 网页（https://www.ecosteam.cn/html/person/mysteam.html）
获取库存，保持网页显示顺序 → 写入 SQLite (inventory 表) 与
data/eco_inventory.csv（供 cs2滚动查询.py 兼容旧 CSV 路径）。

数据库 schema：
    inventory(
        asset_id TEXT PRIMARY KEY,    -- Steam 资产 ID（游戏内定位用）
        stock_id TEXT,                -- ECO 平台库存ID
        hash_name TEXT,               -- Steam 市场哈希名
        goods_name TEXT,              -- ECO 中文展示名
        paint_wear REAL,              -- 磨损值（float）
        paint_seed INTEGER,           -- 图案种子
        paint_index INTEGER,          -- 涂装索引
        tradable INTEGER,             -- 是否可交易
        stickers_json TEXT,           -- 贴纸列表 JSON
        keychains_json TEXT,           -- 挂件列表 JSON
        fetched_at TEXT               -- 抓取时间
    )
"""
import csv
import json
import logging
import sqlite3
from datetime import datetime
from typing import Callable, Optional

from core.data_manager import get_conn
from utils.config import DATA_DIR
import os

logger = logging.getLogger(__name__)

INVENTORY_CSV_PATH = os.path.join(DATA_DIR, "eco_inventory.csv")

# CSV 列顺序（兼容 cs2滚动查询.py 的旧 buff_inventory.csv 格式）
INVENTORY_CSV_COLUMNS = ["名称", "asset_id", "磨损", "paintseed"]


# ============================================================
# 数据库初始化
# ============================================================
def ensure_inventory_table():
    """确保 inventory 表存在（由 data_manager.init_db 调用，
    这里也提供幂等创建以防独立调用）。"""
    conn = get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS inventory (
            asset_id TEXT PRIMARY KEY,
            stock_id TEXT,
            hash_name TEXT,
            goods_name TEXT,
            paint_wear REAL,
            paint_seed INTEGER,
            paint_index INTEGER,
            tradable INTEGER,
            stickers_json TEXT,
            keychains_json TEXT,
            status INTEGER DEFAULT -1,
            fetched_at TEXT
        )
    """)
    # 汰换方案表
    conn.execute("""
        CREATE TABLE IF NOT EXISTS taihuan_plan (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            plan_name TEXT,
            target_item TEXT,
            materials_json TEXT,
            status TEXT DEFAULT 'pending',
            created_at TEXT NOT NULL,
            executed_at TEXT,
            remark TEXT
        )
    """)
    # 汰换执行日志
    conn.execute("""
        CREATE TABLE IF NOT EXISTS taihuan_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            plan_id INTEGER,
            timestamp TEXT NOT NULL,
            action TEXT,
            asset_id TEXT,
            item_name TEXT,
            status TEXT,
            remark TEXT
        )
    """)
    conn.commit()
    conn.close()


# ============================================================
# 拉取库存（通过 ECO 网页爬虫，保持网页显示顺序）
# ============================================================
def refresh_inventory(on_progress: Optional[Callable[[int, int], None]] = None,
                      steam_id: Optional[str] = None) -> int:
    """通过爬取 ECO 网页拉取完整库存并写入 SQLite + CSV。

    使用 Selenium + Edge 爬取 https://www.ecosteam.cn/html/person/mysteam.html，
    解析 <li class="card_item"> 卡片，保持网页显示顺序。
    翻页间隔 1.2s，需要 ECO_WEB_COOKIE 配置。

    Args:
        on_progress: 回调 (current_page, total_so_far) -> None
        steam_id: 未使用（保留兼容）

    Returns:
        写入的物品数量。
    """
    ensure_inventory_table()

    from core import eco_web_scraper, eco_web_api

    def progress_cb(page: int, total: int):
        logger.info("ECO 网页爬取：第 %d 页，累计 %d 件", page, total)
        if on_progress:
            on_progress(page, total)

    # ---- 统一 Cookie 通道（与 eco_web_api 所有接口一致）----
    # user_settings.json（GUI 手动保存 / Playwright 登录自动保存）优先，
    # config.ECO_WEB_COOKIE 兜底。爬虫不能直接读 config 常量——那是启动时
    # 硬编码的旧值，GUI 保存的新 cookie 永远拿不到。
    cookie_dict = eco_web_api._normalize_cookies(None)
    # loginToken 有效期短：爬取前先用 refreshToken 自动续期一次
    # （成功会写回 user_settings.json；失败/未过期则保持原值继续）。
    refreshed = eco_web_api.try_refresh_login_via_api(
        cookie_dict, save_to_user_settings=True)
    if refreshed:
        cookie_dict = refreshed
        logger.info("[库存爬取] loginToken 自动续期成功，使用新 Cookie 爬取。")
    try:
        eco_web_api._validate_eco_web_cookies(cookie_dict,
                                               context="ECO 库存爬取")
    except RuntimeError as e:
        raise RuntimeError(
            f"{e}\n"
            "  【推荐】购买列表页 → ECO Cookie 区 → 点「🌐 弹出浏览器登录自动抓取"
            "（推荐）」按钮，登录后自动保存，无需手动复制粘贴。") from e
    cookie_string = eco_web_api._dict_to_cookie_str(cookie_dict)

    items = eco_web_scraper.scrape_eco_inventory(
        cookie_string=cookie_string,
        headless=False,
        on_progress=progress_cb,
    )

    if not items:
        logger.warning("ECO 网页库存为空，可能 Cookie 过期或未登录。")
        _save_inventory_to_db([])
        _save_inventory_to_csv([])
        return 0

    count = _save_inventory_to_db(items)
    _save_inventory_to_csv(items)
    logger.info("库存已刷新：%d 件，写入 %s", count, INVENTORY_CSV_PATH)
    return count


def _save_inventory_to_db(items: list[dict]) -> int:
    """全量覆盖写入 inventory 表。"""
    conn = get_conn()
    conn.execute("DELETE FROM inventory")
    now = datetime.now().isoformat(timespec="seconds")
    rows = []
    for it in items:
        asset_id = it.get("AssetId") or ""
        if not asset_id:
            continue
        wear_raw = it.get("PaintWear") or "0"
        try:
            paint_wear = float(wear_raw)
        except (ValueError, TypeError):
            paint_wear = 0.0
        rows.append((
            asset_id,
            it.get("StockId", ""),
            it.get("HashName", ""),
            it.get("GoodsName", ""),
            paint_wear,
            int(it.get("PaintSeed") or 0),
            int(it.get("PaintIndex") or 0),
            1 if it.get("Tradable") else 0,
            json.dumps(it.get("Stickers") or [], ensure_ascii=False),
            json.dumps(it.get("Keychains") or [], ensure_ascii=False),
            int(it.get("Status") or -1),
            now,
        ))
    conn.executemany("""
        INSERT INTO inventory
            (asset_id, stock_id, hash_name, goods_name, paint_wear,
             paint_seed, paint_index, tradable, stickers_json,
             keychains_json, status, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, rows)
    conn.commit()
    conn.close()
    return len(rows)


def _save_inventory_to_csv(items: list[dict]) -> None:
    """写入 CSV，兼容 cs2滚动查询.py 的格式（名称, asset_id, 磨损, paintseed）。

    「名称」优先用 GoodsName（中文展示名），无则用 HashName。
    """
    with open(INVENTORY_CSV_PATH, "w", newline="",
              encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(INVENTORY_CSV_COLUMNS)
        for it in items:
            asset_id = it.get("AssetId") or ""
            if not asset_id:
                continue
            name = it.get("GoodsName") or it.get("HashName") or ""
            wear_raw = it.get("PaintWear") or "0"
            try:
                wear = float(wear_raw)
            except (ValueError, TypeError):
                wear = 0.0
            paint_seed = int(it.get("PaintSeed") or 0)
            writer.writerow([name, asset_id, wear, paint_seed])


# ============================================================
# 读取库存
# ============================================================
def load_inventory_from_db() -> list[dict]:
    """从数据库读取库存（按写入顺序=爬取顺序，不做重排）。"""
    ensure_inventory_table()
    conn = get_conn()
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT asset_id, stock_id, hash_name, goods_name, paint_wear,
               paint_seed, paint_index, tradable, status, fetched_at
        FROM inventory
        ORDER BY rowid
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_inventory_stats() -> dict:
    """返回库存统计信息。"""
    ensure_inventory_table()
    conn = get_conn()
    total = conn.execute("SELECT COUNT(*) FROM inventory").fetchone()[0]
    tradable = conn.execute(
        "SELECT COUNT(*) FROM inventory WHERE tradable=1").fetchone()[0]
    last_fetched = conn.execute(
        "SELECT MAX(fetched_at) FROM inventory").fetchone()[0]
    conn.close()
    return {
        "total": total,
        "tradable": tradable,
        "last_fetched": last_fetched or "未抓取",
    }
