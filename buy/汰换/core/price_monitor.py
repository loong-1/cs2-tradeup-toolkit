"""价格检测核心模块：监控目标管理 + 单次查询逻辑 + 阈值自动购买。

2026-09-12 改造：从"全局单一价格上限"升级为"每材料单独阈值 + 需求量"
的汰换材料分批囤货模式：
- monitor_targets 表新增 wear_min/wear_max（磨损区间）、max_price（阈值）、
  need_count（需求量）、plan_id（来源方案）；
- 自动购买条件：在售价格 ≤ 该材料阈值 → 一轮内把所有 ≤阈值 的在售件
  （跨平台比价、最多到剩余需求量）全部买齐；
- 已购件数从 monitor_auto_buy_log 统计（success=1），防重复购买；
- 全局 monitor_auto_buy 的 max_price/max_wear 保留作为无阈值材料的兜底。
"""
import json
from datetime import datetime
from typing import List, Dict, Any, Optional

from core.data_manager import get_conn, build_c5_market_hash_name
from core.price_fetcher import query_all, _extract_base_hash_name


# ============ 监控目标表 CRUD ============

def _ensure_table():
    conn = get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS monitor_targets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_name TEXT NOT NULL,
            buff_goods_id TEXT,
            c5_market_hash_name TEXT,
            c5_app_id TEXT,
            enabled INTEGER DEFAULT 1,
            added_at TEXT NOT NULL
        )
    """)
    # 2026-09-12 列迁移（旧库升级）
    for col, ddl in [
        ("wear_min", "REAL"),
        ("wear_max", "REAL"),
        ("max_price", "REAL"),
        ("need_count", "INTEGER"),
        ("plan_id", "INTEGER"),
    ]:
        try:
            conn.execute(f"ALTER TABLE monitor_targets "
                         f"ADD COLUMN {col} {ddl}")
        except Exception:
            pass  # 已存在
    conn.execute("""
        CREATE TABLE IF NOT EXISTS monitor_auto_buy (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            enabled INTEGER DEFAULT 0,
            max_price REAL,
            max_wear REAL,
            updated_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS monitor_auto_buy_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            target_id INTEGER NOT NULL,
            item_name TEXT NOT NULL,
            platform TEXT,
            order_id TEXT,
            price REAL,
            wear REAL,
            purchased_at TEXT,
            success INTEGER,
            remark TEXT
        )
    """)
    row = conn.execute(
        "SELECT COUNT(*) FROM monitor_auto_buy").fetchone()
    if not row or row[0] == 0:
        conn.execute(
            "INSERT INTO monitor_auto_buy "
            "(enabled, max_price, max_wear, updated_at) VALUES (0, NULL, NULL, ?)",
            (datetime.now().isoformat(timespec="seconds"),))
    conn.commit()
    conn.close()


_TARGET_COLS = ("id, item_name, buff_goods_id, c5_market_hash_name, "
                "c5_app_id, enabled, added_at, "
                "wear_min, wear_max, max_price, need_count, plan_id")


def _row_to_target(r) -> Dict[str, Any]:
    return {
        "id": r[0], "item_name": r[1], "buff_goods_id": r[2],
        "c5_market_hash_name": r[3], "c5_app_id": r[4] or "730",
        "enabled": bool(r[5]), "added_at": r[6],
        "wear_min": r[7], "wear_max": r[8],
        "max_price": r[9],
        "need_count": r[10] if r[10] is not None else 0,
        "plan_id": r[11],
    }


def list_targets() -> List[Dict[str, Any]]:
    """返回所有监控目标。"""
    _ensure_table()
    conn = get_conn()
    rows = conn.execute(
        f"SELECT {_TARGET_COLS} FROM monitor_targets ORDER BY id"
    ).fetchall()
    conn.close()
    return [_row_to_target(r) for r in rows]


def add_target(item: Dict[str, Any]) -> int:
    """新增监控目标，返回新 id。"""
    _ensure_table()
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO monitor_targets "
        "(item_name, buff_goods_id, c5_market_hash_name, c5_app_id, "
        "enabled, added_at, wear_min, wear_max, max_price, need_count, plan_id) "
        "VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?)",
        (item.get("item_name", ""),
         item.get("buff_goods_id", ""),
         item.get("c5_market_hash_name", ""),
         item.get("c5_app_id", "730"),
         datetime.now().isoformat(timespec="seconds"),
         item.get("wear_min"),
         item.get("wear_max"),
         item.get("max_price"),
         item.get("need_count", 0) or 0,
         item.get("plan_id")))
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    return new_id


def update_target_fields(target_id: int, **fields) -> None:
    """更新监控目标的部分字段（wear_min/wear_max/max_price/need_count）。"""
    allowed = ("wear_min", "wear_max", "max_price", "need_count")
    sets, vals = [], []
    for k in allowed:
        if k in fields:
            sets.append(f"{k} = ?")
            vals.append(fields[k])
    if not sets:
        return
    vals.append(target_id)
    conn = get_conn()
    conn.execute(
        f"UPDATE monitor_targets SET {', '.join(sets)} WHERE id = ?", vals)
    conn.commit()
    conn.close()


def remove_target(target_id: int) -> None:
    conn = get_conn()
    conn.execute("DELETE FROM monitor_targets WHERE id = ?", (target_id,))
    conn.execute(
        "DELETE FROM monitor_auto_buy_log WHERE target_id = ?", (target_id,))
    conn.commit()
    conn.close()


def set_target_enabled(target_id: int, enabled: bool) -> None:
    conn = get_conn()
    conn.execute(
        "UPDATE monitor_targets SET enabled = ? WHERE id = ?",
        (1 if enabled else 0, target_id))
    conn.commit()
    conn.close()


def clear_targets() -> None:
    conn = get_conn()
    conn.execute("DELETE FROM monitor_targets")
    conn.execute("DELETE FROM monitor_auto_buy_log")
    conn.commit()
    conn.close()


# ============ 汰换方案导入 ============

def import_from_taihuan_plan(plan_id: int, plan_name: str = "",
                             default_need: int = 1,
                             wear_tier_map: Optional[Dict[str, tuple]] = None,
                             name_meta: Optional[Dict[str, dict]] = None) -> int:
    """从 taihuan_plan 方案导入材料为监控目标（同名材料合并需求量）。

    :param plan_id: 方案 id（写入 target.plan_id 便于溯源）
    :param plan_name: 方案名（日志用）
    :param default_need: 每个材料槽位默认需求件数（套数）
    :param wear_tier_map: {(皮肤名, 档位): (wear_min, wear_max)} 可选覆盖
    :param name_meta: {皮肤名: {buff_goods_id, gid_by_grade, c5_by_grade,
                                 c5_market_hash_name}} 可选主 CSV 元数据
    :return: 导入的目标数（新建数；重复导入为覆盖更新）
    """
    _ensure_table()
    conn = get_conn()
    row = conn.execute(
        "SELECT materials_json FROM taihuan_plan WHERE id = ?",
        (plan_id,)).fetchone()
    conn.close()
    if not row:
        raise ValueError(f"方案 {plan_id} 不存在")

    data = json.loads(row[0])
    if isinstance(data, dict):
        materials = data.get("materials", [])
    else:
        materials = data

    # 按 (皮肤名, 档位) 聚合槽位数（同皮肤不同档位是不同监控目标，
    # 且各档位 buff_goods_id / C5 带后缀名均不同）
    slots: Dict[tuple, int] = {}
    wears: Dict[tuple, tuple] = {}
    mat_meta: Dict[tuple, dict] = {}
    for m in materials:
        name = (m.get("皮肤名称") or m.get("skin_name")
                or m.get("goods_name") or "").strip()
        grade = (m.get("磨损") or m.get("wear_grade") or "").strip()
        if not name:
            continue
        key = (name, grade)
        slots[key] = slots.get(key, 0) + 1
        if key not in mat_meta:
            mat_meta[key] = {
                # 材料自带 gid（保存方案时按档位写入，档位正确）
                "buff_goods_id": str(m.get("buff_goods_id") or "").strip(),
                # 材料自带英文基础名（不带磨损后缀）
                "c5_base": ((m.get("c5_market_hash_name")
                             or m.get("市场哈希名称") or "") or "").strip(),
            }
        if key not in wears:
            # 磨损区间：方案材料自带 磨损_min/max（官方档区间）；缺失时
            # 用 paint_wear 单值兜底（后续双击行可编辑）
            try:
                wm = float(m.get("磨损_min"))
                wx = float(m.get("磨损_max"))
                if 0 < wm < wx:
                    wears[key] = (wm, wx)
            except (TypeError, ValueError):
                pass
            if key not in wears:
                pw = m.get("paint_wear")
                if isinstance(pw, (int, float)) and pw > 0:
                    wears[key] = (pw, pw)

    added = 0
    for (name, grade), count in slots.items():
        own = mat_meta.get((name, grade), {})
        meta = (name_meta or {}).get(name, {})
        gid_by_grade = meta.get("gid_by_grade") or {}
        c5_by_grade = meta.get("c5_by_grade") or {}
        # Buff gid：材料自带（档位正确）> 主 CSV 按档位 > 主 CSV 兜底
        buff_gid = (own.get("buff_goods_id")
                    or gid_by_grade.get(grade)
                    or meta.get("buff_goods_id", ""))
        # C5 哈希名（需带英文磨损后缀，如 "P90 | Wave Breaker (Well-Worn)"）：
        # 主 CSV 按档位 > 材料基础名+档位后缀 > 材料原始名 > 主 CSV 兜底
        c5_base = _extract_base_hash_name(own.get("c5_base", ""))
        c5_hash = (c5_by_grade.get(grade)
                   or (build_c5_market_hash_name(c5_base, grade)
                       if c5_base else "")
                   or own.get("c5_base", "")
                   or c5_by_grade.get("")
                   or meta.get("c5_market_hash_name", ""))
        wm, wx = wears.get((name, grade), (None, None))
        disp_name = f"{name}（{grade}）" if grade else name
        # 已存在同名目标 → 覆盖元数据 + 重设需求量（幂等，保留阈值）
        existing = _find_target_by_name(disp_name)
        if existing:
            conn = get_conn()
            conn.execute(
                "UPDATE monitor_targets SET buff_goods_id = ?, "
                "c5_market_hash_name = ?, wear_min = ?, wear_max = ?, "
                "need_count = ?, plan_id = ? WHERE id = ?",
                (buff_gid, c5_hash, wm, wx, count * default_need,
                 plan_id, existing["id"]))
            conn.commit()
            conn.close()
            continue
        add_target({
            "item_name": disp_name,
            "buff_goods_id": buff_gid,
            "c5_market_hash_name": c5_hash,
            "c5_app_id": meta.get("c5_app_id", "730"),
            "wear_min": wm,
            "wear_max": wx,
            "need_count": count * default_need,
            "plan_id": plan_id,
        })
        added += 1
    return added


def _find_target_by_name(name: str) -> Optional[Dict[str, Any]]:
    conn = get_conn()
    row = conn.execute(
        f"SELECT {_TARGET_COLS} FROM monitor_targets "
        "WHERE item_name = ? ORDER BY id LIMIT 1", (name,)).fetchone()
    conn.close()
    return _row_to_target(row) if row else None


def list_taihuan_plans() -> List[Dict[str, Any]]:
    """列出可导入的汰换方案（id/名称/目标/状态/时间）。"""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT id, plan_name, target_item, status, created_at "
            "FROM taihuan_plan ORDER BY id DESC").fetchall()
    except Exception:
        rows = []
    conn.close()
    return [{
        "id": r[0], "plan_name": r[1] or "", "target_item": r[2] or "",
        "status": r[3] or "", "created_at": r[4] or "",
    } for r in rows]


# ============ 单次检测逻辑 ============

def run_single_check(target: Dict[str, Any],
                     platforms=None) -> Dict[str, Any]:
    """对单个监控目标执行一次查询（带材料磨损区间 + 完整在售列表）。

    :return: dict {
        "target": target,
        "success": bool,
        "min_price": float,
        "min_price_platform": str,
        "min_wear": float,
        "min_wear_platform": str,
        "count": int,
        "best": order_dict,
        "items": [order_dict, ...],   # 全部在售（价格升序，跨平台）
        "error": str or None,
        "checked_at": str,
    }
    """
    checked_at = datetime.now().isoformat(timespec="seconds")
    try:
        wm = target.get("wear_min")
        wx = target.get("wear_max")
        kwargs = {}
        if wm is not None and wx is not None and wm > 0 and wx > 0:
            kwargs["wear_min"] = wm
            kwargs["wear_max"] = wx
        results, errors = query_all(target, platforms=platforms,
                                    c5_full_list=True, **kwargs)
        if not results:
            err_msg = ""
            if errors:
                err_parts = [f"{k}: {v}" for k, v in errors.items()]
                err_msg = "; ".join(err_parts)
            else:
                err_msg = "无在售"
            return {
                "target": target, "success": False,
                "min_price": None, "min_price_platform": "",
                "min_wear": None, "min_wear_platform": "",
                "count": 0, "best": None, "items": [],
                "error": err_msg, "checked_at": checked_at,
            }
        min_price_item = results[0]  # query_all 已按价格升序
        min_wear_item = min(results, key=lambda x: x.get("wear", 0))
        # 各平台最低价汇总（供 GUI tooltip 展示，用户可直观看到 buff/c5/eco 各自价格）
        plat_min = {}
        plat_count = {}
        for r in results:
            p = r.get("platform") or ""
            if not p:
                continue
            plat_count[p] = plat_count.get(p, 0) + 1
            try:
                rp = float(r["price"])
            except (TypeError, ValueError):
                continue
            if p not in plat_min or rp < float(plat_min[p]):
                plat_min[p] = rp
        return {
            "target": target, "success": True,
            "min_price": min_price_item["price"],
            "min_price_platform": min_price_item["platform"],
            "min_wear": min_wear_item["wear"],
            "min_wear_platform": min_wear_item["platform"],
            "count": len(results), "best": min_price_item,
            "items": results,
            "plat_min": plat_min, "plat_count": plat_count,
            "error": None, "checked_at": checked_at,
        }
    except Exception as e:
        return {
            "target": target, "success": False,
            "min_price": None, "min_price_platform": "",
            "min_wear": None, "min_wear_platform": "",
            "count": 0, "best": None, "items": [],
            "error": str(e), "checked_at": checked_at,
        }


# ============ 自动购买配置 CRUD ============

def get_auto_buy_config() -> Dict[str, Any]:
    """返回自动购买配置 dict：{enabled, max_price, max_wear}"""
    _ensure_table()
    conn = get_conn()
    row = conn.execute(
        "SELECT enabled, max_price, max_wear FROM monitor_auto_buy "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()
    if not row:
        return {"enabled": False, "max_price": None, "max_wear": None}
    return {
        "enabled": bool(row[0]),
        "max_price": row[1],
        "max_wear": row[2],
    }


def save_auto_buy_config(enabled: bool,
                         max_price: float | None,
                         max_wear: float | None) -> None:
    """保存自动购买配置（覆盖更新现有配置）。"""
    _ensure_table()
    conn = get_conn()
    conn.execute("DELETE FROM monitor_auto_buy")
    conn.execute(
        "INSERT INTO monitor_auto_buy "
        "(enabled, max_price, max_wear, updated_at) VALUES (?, ?, ?, ?)",
        (1 if enabled else 0, max_price, max_wear,
         datetime.now().isoformat(timespec="seconds")))
    conn.commit()
    conn.close()


def count_bought(target_id: int) -> int:
    """该监控目标已成功购买的件数（monitor_auto_buy_log success=1）。"""
    _ensure_table()
    conn = get_conn()
    row = conn.execute(
        "SELECT COUNT(*) FROM monitor_auto_buy_log "
        "WHERE target_id = ? AND success = 1", (target_id,)).fetchone()
    conn.close()
    return int(row[0] or 0)


def remaining_need(target: Dict[str, Any]) -> int:
    """剩余需求 = need_count - 已购成功件数（最小 0）。"""
    need = target.get("need_count") or 0
    if need <= 0:
        return 0
    return max(0, need - count_bought(target.get("id")))


def target_threshold(target: Dict[str, Any],
                      config: Dict[str, Any]) -> Optional[float]:
    """材料有效阈值：只取 target.max_price（每材料单独设）。

    不再使用全局兜底——没有设阈值的材料在自动购买时视为「跳过」，
    不会触发任何购买判定。
    """
    tp = target.get("max_price")
    if tp is not None and tp > 0:
        return float(tp)
    return None


def get_affordable_orders(result: Dict[str, Any],
                           target: Dict[str, Any],
                           config: Dict[str, Any],
                           limit: Optional[int] = None) -> List[Dict[str, Any]]:
    """从检测结果中取出所有 ≤阈值 的在售件（价格升序，最多 limit 件）。

    :param result: run_single_check 的返回（items 为完整在售列表）
    :param limit: 剩余需求量（None 表示不限制）
    """
    if not result.get("success"):
        return []
    threshold = target_threshold(target, config)
    if threshold is None:
        return []
    mw = target.get("wear_max")
    out = []
    for order in (result.get("items") or []):
        try:
            price = float(order.get("price", 0) or 0)
        except (TypeError, ValueError):
            continue
        if price <= 0 or price > threshold:
            continue
        wear = order.get("wear")
        if mw is not None and wear is not None:
            try:
                if float(wear) > float(mw):
                    continue
            except (TypeError, ValueError):
                pass
        out.append(order)
    out.sort(key=lambda o: float(o.get("price", 0) or 0))
    if limit is not None:
        out = out[:max(0, limit)]
    return out


def check_auto_buy_match(result: Dict[str, Any],
                         config: Dict[str, Any]) -> bool:
    """旧接口保留（判断 best 是否满足全局价格/磨损条件）。"""
    if not config.get("enabled"):
        return False
    if not result.get("success"):
        return False
    best = result.get("best")
    if not best:
        return False
    price = float(best.get("price", 0) or 0)
    wear = float(best.get("wear", 0) or 0)
    if config.get("max_price") is not None and price > config["max_price"]:
        return False
    if config.get("max_wear") is not None and wear > config["max_wear"]:
        return False
    return True


def log_auto_buy(target_id: int, item_name: str, platform: str,
                 order_id: str, price: float, wear: float,
                 success: bool, remark: str = "") -> None:
    """记录一次自动购买。"""
    _ensure_table()
    conn = get_conn()
    conn.execute(
        "INSERT INTO monitor_auto_buy_log "
        "(target_id, item_name, platform, order_id, price, wear, "
        "purchased_at, success, remark) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (target_id, item_name, platform, order_id, price, wear,
         datetime.now().isoformat(timespec="seconds"),
         1 if success else 0, remark))
    conn.commit()
    conn.close()
