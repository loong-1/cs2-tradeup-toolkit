"""一键更新价格：C5 price/batch 批量 + ECO 补查 + SteamDT 兜底。"""
from __future__ import annotations

import os
import sys
import time
from typing import Callable, Dict, List, Optional, Tuple

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tradeup_sim import database as db
import tradeup_sim.steamdt_price as sdp

# 导入主软件的 C5 配置和磨损转换
_BUY_DIR = os.path.join(_PROJECT_ROOT, "buy", "汰换")
if _BUY_DIR not in sys.path:
    sys.path.insert(0, _BUY_DIR)
from core.data_manager import build_c5_market_hash_name
from core.price_fetcher import C5_APP_KEY, C5_APP_ID


def _query_c5_batch(market_hash_names: List[str], app_id: int = C5_APP_ID
                    ) -> Dict[str, float]:
    """调用 C5 price/batch 批量查询多个 marketHashName 的最低价。

    返回 { marketHashName: lowest_price }。
    """
    import requests
    if not market_hash_names:
        return {}
    try:
        resp = requests.post(
            "https://openapi.c5game.com/merchant/product/price/batch",
            params={"app-key": C5_APP_KEY},
            json={"appId": app_id, "marketHashNames": market_hash_names},
            timeout=15,
        )
        data = (resp.json() or {}).get("data") or {}
        result = {}
        if isinstance(data, dict):
            for name in market_hash_names:
                # 精确匹配或模糊匹配
                hit = None
                if name in data and isinstance(data[name], dict):
                    hit = data[name]
                else:
                    for k, v in data.items():
                        if not isinstance(v, dict):
                            continue
                        if k == name or name in k or k in name:
                            hit = v
                            break
                if hit:
                    for pk in ("price", "minPrice", "lowestPrice", "LowestPrice"):
                        raw = hit.get(pk)
                        if raw:
                            try:
                                p = float(raw)
                                if p > 0:
                                    result[name] = p
                                    break
                            except (TypeError, ValueError):
                                continue
        return result
    except Exception:
        return {}


def _query_eco_single(market_hash_name: str) -> Optional[float]:
    """查询单个 ECO 商品最低价。"""
    from core.price_fetcher import query_eco
    try:
        results = query_eco(market_hash_name, app_id=str(C5_APP_ID))
        if results:
            best = min(results, key=lambda x: float(x.get("price", 1e9)))
            return float(best["price"])
    except Exception:
        pass
    return None


def update_all_prices(
    progress_cb: Optional[Callable[[int, int, str], None]] = None,
    use_steamdt: bool = True,
    use_c5: bool = True,
    use_eco: bool = True,
    clear_first: bool = True,
    sync_to_csv: bool = True,
) -> Dict:
    """更新所有皮肤价格。

    策略：
    1. C5 price/batch 批量查询（每批最多 100 个带磨损后缀的 marketHashName）
    2. ECO 逐个补查 C5 未命中的
    3. SteamDT 批量补查剩余未命中的
    """
    t0 = time.time()

    if clear_first:
        n_cleared = db.clear_all_prices()
        if progress_cb:
            progress_cb(0, 0, f"已清空 {n_cleared} 条旧价格")

    targets = db.get_all_price_targets()
    total = len(targets)
    if total == 0:
        return {"total": 0, "updated_c5": 0, "updated_eco": 0,
                "updated_steamdt": 0, "failed": 0, "elapsed": 0}

    # 为每个 target 构建完整的 c5 market_hash_name（带英文磨损后缀）
    target_list = []
    for t in targets:
        c5_hash = build_c5_market_hash_name(t.get("market_hash", ""),
                                            t["wear_grade"])
        target_list.append({
            "name": t["name"],
            "wear_grade": t["wear_grade"],
            "is_stattrak": bool(t["is_stattrak"]),
            "c5_hash": c5_hash,
            "market_hash": t.get("market_hash", ""),
        })

    updated_c5 = 0
    updated_eco = 0
    updated_steamdt = 0
    failed = 0

    # ---- Step 1: C5 price/batch 批量查询 ----
    if use_c5:
        # 收集所有非空 c5_hash
        hash_to_targets: Dict[str, List[Dict]] = {}
        for t in target_list:
            if t["c5_hash"]:
                hash_to_targets.setdefault(t["c5_hash"], []).append(t)

        all_hashes = list(hash_to_targets.keys())
        batch_size = 100
        n_batches = (len(all_hashes) + batch_size - 1) // batch_size

        for bi in range(n_batches):
            batch = all_hashes[bi * batch_size:(bi + 1) * batch_size]
            prices = _query_c5_batch(batch)
            for h, price in prices.items():
                for t in hash_to_targets.get(h, []):
                    db.update_skin_price(t["name"], t["wear_grade"], price,
                                         t["is_stattrak"])
                    updated_c5 += 1
            # C5 节流：≥1 秒间隔
            if bi < n_batches - 1:
                time.sleep(1.0)
            if progress_cb:
                progress_cb(bi + 1, n_batches,
                            f"C5 批量 {bi + 1}/{n_batches} "
                            f"(已更新 {updated_c5} 条)")

    if progress_cb:
        progress_cb(1, 1, f"C5 完成：更新 {updated_c5} 条")

    # ---- Step 2: ECO 逐个补查 C5 未命中的（限制数量） ----
    if use_eco:
        missing = [t for t in target_list
                   if db.get_skin_price(t["name"], t["wear_grade"],
                                        t["is_stattrak"]) <= 0
                   and t["c5_hash"]]
        # 限制 ECO 补查数量：最多 500 条（约 2.5 分钟）
        ECO_MAX = 500
        n_total_missing = len(missing)
        if len(missing) > ECO_MAX:
            missing = missing[:ECO_MAX]
        n_missing = len(missing)
        for idx, t in enumerate(missing):
            price = _query_eco_single(t["c5_hash"])
            if price and price > 0:
                db.update_skin_price(t["name"], t["wear_grade"], price,
                                     t["is_stattrak"])
                updated_eco += 1
            else:
                failed += 1
            # ECO 节流
            time.sleep(0.3)
            if progress_cb and (idx + 1) % 20 == 0:
                progress_cb(idx + 1, n_missing,
                            f"ECO 补查 {idx + 1}/{n_missing} "
                            f"(共 {n_total_missing} 个未命中，"
                            f"限制 {ECO_MAX}，命中 {updated_eco})")

    # ---- Step 3: SteamDT 批量补查剩余未命中的（限制数量，避免频率限制） ----
    if use_steamdt:
        missing = []
        for t in target_list:
            price = db.get_skin_price(t["name"], t["wear_grade"],
                                      t["is_stattrak"])
            if price <= 0 and t["market_hash"]:
                missing.append((t["name"], t["market_hash"],
                                t["wear_grade"], t["is_stattrak"]))
        # 限制 SteamDT 补查数量：最多 200 条（2 批，约 2 分钟）
        # 超出的由配方优化时实时查询+缓存补上
        STEAMDT_MAX = 200
        n_total_missing = len(missing)
        if len(missing) > STEAMDT_MAX:
            missing = missing[:STEAMDT_MAX]
        if missing:
            if progress_cb:
                progress_cb(0, len(missing),
                            f"SteamDT 批量补查 {len(missing)}/{n_total_missing} 个"
                            f"（限制最多 {STEAMDT_MAX} 个，超出部分由实时查询补齐）...")
            steamdt_prices = sdp.query_skin_prices(missing)
            for name, mhash, grade, is_st in missing:
                key = (name, grade)
                if key in steamdt_prices and steamdt_prices[key] > 0:
                    db.update_skin_price(name, grade, steamdt_prices[key], is_st)
                    updated_steamdt += 1
                else:
                    failed += 1
            if progress_cb:
                progress_cb(len(missing), len(missing),
                            f"SteamDT 更新 {updated_steamdt}/{len(missing)}")

    # ---- Step 4: 同步价格到主 CSV 的 price_buff 列 ----
    csv_synced = 0
    if sync_to_csv:
        csv_synced = sync_prices_to_csv()
        if progress_cb:
            progress_cb(total, total, f"已同步 {csv_synced} 条价格到 CSV")

    elapsed = time.time() - t0
    if progress_cb:
        progress_cb(total, total,
                    f"完成！共 {total} 条，C5 {updated_c5}，"
                    f"ECO {updated_eco}，SteamDT {updated_steamdt}，"
                    f"CSV同步 {csv_synced}，失败 {failed}，"
                    f"耗时 {elapsed:.1f}s")

    return {
        "total": total,
        "updated_c5": updated_c5,
        "updated_eco": updated_eco,
        "updated_steamdt": updated_steamdt,
        "csv_synced": csv_synced,
        "failed": failed,
        "elapsed": round(elapsed, 1),
    }


def backfill_prices(
    progress_cb: Optional[Callable[[int, int, str], None]] = None,
    use_steamdt: bool = True,
    use_c5: bool = True,
    use_eco: bool = True,
    sync_to_csv: bool = True,
) -> Dict:
    """补查价格：只查询当前 price <= 0 的皮肤，不清空已有价格。

    策略同 update_all_prices，但只处理缺失价格的条目。
    """
    t0 = time.time()

    # 只取 price <= 0 的 target
    all_targets = db.get_all_price_targets()
    targets = []
    for t in all_targets:
        price = db.get_skin_price(t["name"], t["wear_grade"],
                                  bool(t["is_stattrak"]))
        if price <= 0:
            targets.append(t)
    total = len(targets)
    if total == 0:
        if progress_cb:
            progress_cb(0, 0, "所有皮肤已有价格，无需补查")
        return {"total": 0, "updated_c5": 0, "updated_eco": 0,
                "updated_steamdt": 0, "csv_synced": 0, "failed": 0,
                "elapsed": 0}

    if progress_cb:
        progress_cb(0, total, f"发现 {total} 条缺失价格，开始补查...")

    # 构建 c5_hash
    target_list = []
    for t in targets:
        c5_hash = build_c5_market_hash_name(t.get("market_hash", ""),
                                            t["wear_grade"])
        target_list.append({
            "name": t["name"],
            "wear_grade": t["wear_grade"],
            "is_stattrak": bool(t["is_stattrak"]),
            "c5_hash": c5_hash,
            "market_hash": t.get("market_hash", ""),
        })

    updated_c5 = updated_eco = updated_steamdt = failed = 0

    # Step 1: C5 批量
    if use_c5:
        hash_to_targets: Dict[str, List[Dict]] = {}
        for t in target_list:
            if t["c5_hash"]:
                hash_to_targets.setdefault(t["c5_hash"], []).append(t)
        all_hashes = list(hash_to_targets.keys())
        batch_size = 100
        n_batches = (len(all_hashes) + batch_size - 1) // batch_size
        for bi in range(n_batches):
            batch = all_hashes[bi * batch_size:(bi + 1) * batch_size]
            prices = _query_c5_batch(batch)
            for h, price in prices.items():
                for t in hash_to_targets.get(h, []):
                    db.update_skin_price(t["name"], t["wear_grade"], price,
                                         t["is_stattrak"])
                    updated_c5 += 1
            if bi < n_batches - 1:
                time.sleep(1.0)
            if progress_cb:
                progress_cb(bi + 1, n_batches,
                            f"C5 补查 {bi + 1}/{n_batches} (已更新 {updated_c5})")

    # Step 2: ECO 补查
    if use_eco:
        missing = [t for t in target_list
                   if db.get_skin_price(t["name"], t["wear_grade"],
                                        t["is_stattrak"]) <= 0
                   and t["c5_hash"]]
        ECO_MAX = 500
        if len(missing) > ECO_MAX:
            missing = missing[:ECO_MAX]
        for idx, t in enumerate(missing):
            price = _query_eco_single(t["c5_hash"])
            if price and price > 0:
                db.update_skin_price(t["name"], t["wear_grade"], price,
                                     t["is_stattrak"])
                updated_eco += 1
            else:
                failed += 1
            time.sleep(0.3)
            if progress_cb and (idx + 1) % 20 == 0:
                progress_cb(idx + 1, len(missing),
                            f"ECO 补查 {idx + 1}/{len(missing)} (命中 {updated_eco})")

    # Step 3: SteamDT 补查
    if use_steamdt:
        missing = []
        for t in target_list:
            price = db.get_skin_price(t["name"], t["wear_grade"],
                                      t["is_stattrak"])
            if price <= 0 and t["market_hash"]:
                missing.append((t["name"], t["market_hash"],
                                t["wear_grade"], t["is_stattrak"]))
        STEAMDT_MAX = 200
        if len(missing) > STEAMDT_MAX:
            missing = missing[:STEAMDT_MAX]
        if missing:
            if progress_cb:
                progress_cb(0, len(missing),
                            f"SteamDT 补查 {len(missing)} 个...")
            steamdt_prices = sdp.query_skin_prices(missing)
            for name, mhash, grade, is_st in missing:
                key = (name, grade)
                if key in steamdt_prices and steamdt_prices[key] > 0:
                    db.update_skin_price(name, grade, steamdt_prices[key], is_st)
                    updated_steamdt += 1
                else:
                    failed += 1

    # Step 4: 同步到 CSV
    csv_synced = 0
    if sync_to_csv:
        csv_synced = sync_prices_to_csv()

    elapsed = time.time() - t0
    if progress_cb:
        progress_cb(total, total,
                    f"补查完成！共 {total} 条，C5 {updated_c5}，"
                    f"ECO {updated_eco}，SteamDT {updated_steamdt}，"
                    f"CSV同步 {csv_synced}，失败 {failed}，"
                    f"耗时 {elapsed:.1f}s")

    return {
        "total": total,
        "updated_c5": updated_c5,
        "updated_eco": updated_eco,
        "updated_steamdt": updated_steamdt,
        "csv_synced": csv_synced,
        "failed": failed,
        "elapsed": round(elapsed, 1),
    }


def sync_prices_to_csv() -> int:
    """将 DB 中已更新的价格写回主 CSV 的 price_buff 列。

    匹配键：(皮肤名称, 磨损)。只更新 price > 0 的条目。
    写入前自动备份原 CSV 为 .bak 文件。
    返回更新的行数。
    """
    import csv as csv_mod
    import shutil

    from utils.config import MAIN_ITEMS_CSV

    if not MAIN_ITEMS_CSV or not os.path.exists(MAIN_ITEMS_CSV):
        return 0

    # 从 DB 读取所有价格，构建 (name, wear_grade) -> price 映射
    conn = db.get_conn()
    rows = conn.execute(
        "SELECT name, wear_grade, is_stattrak, price FROM skins WHERE price > 0"
    ).fetchall()
    conn.close()

    price_map: Dict[Tuple[str, str], float] = {}
    for r in rows:
        price_map[(r["name"], r["wear_grade"])] = r["price"]

    # 备份原 CSV
    backup_path = MAIN_ITEMS_CSV + ".bak"
    shutil.copy2(MAIN_ITEMS_CSV, backup_path)

    # 读取并更新
    with open(MAIN_ITEMS_CSV, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv_mod.DictReader(f)
        fieldnames = reader.fieldnames
        rows_out = []
        synced = 0
        for row in reader:
            name = (row.get("皮肤名称") or "").strip()
            wear = (row.get("磨损") or "").strip()
            key = (name, wear)
            if key in price_map:
                new_price = price_map[key]
                old_price = row.get("price_buff", "0")
                row["price_buff"] = str(round(new_price, 2))
                synced += 1
            rows_out.append(row)

    # 写回 CSV
    with open(MAIN_ITEMS_CSV, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv_mod.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows_out)

    return synced
