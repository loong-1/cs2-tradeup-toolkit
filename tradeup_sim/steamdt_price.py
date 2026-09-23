"""
使用 SteamDT 开放平台查询饰品价格（批量 + 单条混用）。

频率限制（来自官方文档）：
  - /open/cs2/v1/price/batch  : 每分钟 1 次（每批最多 100 个 marketHashName）
  - /open/cs2/v1/price/single : 每分钟 60 次

策略：
  1. 优先用批量接口（一次最多 100 个）。
  2. 批量失败（频率限制 / 部分无数据）时，用单条接口补齐。
  3. 内置内存缓存（TTL），同一会话内避免重复查询。

关于磨损区间：SteamDT 价格接口不支持按 float 区间过滤。
价格是按 marketHashName 返回的，而 marketHashName 已含磨损档
（如 "AWP | Wildfire (Field-Tested)"），即一个磨损档对应一个价格。
这正好符合汰换配方优化的需求（按磨损档取价）。
"""
import os
import sys
import time
from typing import Dict, List, Tuple, Optional

# steamdt client 在项目根目录 send_request.py
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from send_request import SteamDTClient, PlatformPriceVO  # noqa: E402

# 中文磨损档 -> 英文磨损档（Steam marketHashName 后缀）
WEAR_EN = {
    "崭新出厂": "Factory New",
    "略有磨损": "Minimal Wear",
    "久经沙场": "Field-Tested",
    "破损不堪": "Well-Worn",
    "战痕累累": "Battle-Scarred",
}

# ---------- 频率控制 ----------
# 批量: 每分钟 1 次 → 最小间隔 60s
# 单条: 每分钟 60 次 → 最小间隔 1s（留点余量用 1.1s）
_BATCH_MIN_INTERVAL = 60.0
_SINGLE_MIN_INTERVAL = 1.1

_last_batch_ts = 0.0
_last_single_ts = 0.0

# ---------- 缓存（内存 + 持久化 JSON） ----------
# { mhn: (price, timestamp) }
# 内存 TTL = 10 分钟，持久化 TTL = 24 小时
_CACHE: Dict[str, Tuple[float, float]] = {}
_CACHE_TTL = 600.0
_PERSISTENT_TTL = 86400.0  # 24 小时

# 持久化缓存文件路径
_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "data", "price_cache.json")


def _load_persistent_cache():
    """启动时从 JSON 文件加载缓存到内存。"""
    if not os.path.exists(_CACHE_FILE):
        return
    try:
        import json
        with open(_CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        now = time.time()
        for mhn, (price, ts) in data.items():
            if now - ts < _PERSISTENT_TTL:
                _CACHE[mhn] = (price, ts)
    except Exception:
        pass


def _save_persistent_cache():
    """将内存缓存写入 JSON 文件（只保留未过期的）。"""
    try:
        import json
        os.makedirs(os.path.dirname(_CACHE_FILE), exist_ok=True)
        now = time.time()
        data = {mhn: [price, ts] for mhn, (price, ts) in _CACHE.items()
                if now - ts < _PERSISTENT_TTL}
        with open(_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception:
        pass


# 启动时加载持久化缓存
_load_persistent_cache()


def _cache_get(mhn: str) -> Optional[float]:
    entry = _CACHE.get(mhn)
    if not entry:
        return None
    price, ts = entry
    if time.time() - ts > _CACHE_TTL:
        # 内存过期，但持久化可能还有效（24h）
        if time.time() - ts < _PERSISTENT_TTL:
            return price
        _CACHE.pop(mhn, None)
        return None
    return price


def _cache_set(mhn: str, price: float):
    _CACHE[mhn] = (price, time.time())
    # 持久化（异步或定期，这里直接写以保证简单）
    _save_persistent_cache()


def build_market_hash_name(market_hash: str, wear_grade_cn: str,
                           is_stattrak: bool = False,
                           is_souvenir: bool = False) -> str:
    """
    根据 DB 的 market_hash（英文名，不带磨损后缀）拼接 Steam marketHashName。

    例:
      market_hash="AWP | Wildfire", wear="崭新出厂"
      -> "AWP | Wildfire (Factory New)"
      StatTrak: -> "StatTrak\u2122 AWP | Wildfire (Factory New)"
    """
    en = WEAR_EN.get(wear_grade_cn, "")
    base = market_hash.strip()
    if is_souvenir:
        base = f"Souvenir {base}"
    elif is_stattrak:
        base = f"StatTrak\u2122 {base}"
    if en:
        return f"{base} ({en})"
    return base


def _extract_price(plats: List[PlatformPriceVO],
                   preferred_platforms: Optional[List[str]] = None
                   ) -> Optional[float]:
    """从平台价格列表中取最低价（在售 sellPrice）。"""
    prices = []
    for p in plats:
        if preferred_platforms and p.platform not in preferred_platforms:
            continue
        if p.sell_price and p.sell_price > 0:
            prices.append(float(p.sell_price))
    return min(prices) if prices else None


def _wait_for_batch():
    """批量接口频率控制：距上次调用不足 60s 则等待。"""
    global _last_batch_ts
    now = time.time()
    wait = _BATCH_MIN_INTERVAL - (now - _last_batch_ts)
    if wait > 0:
        time.sleep(wait)
    _last_batch_ts = time.time()


def _wait_for_single():
    """单条接口频率控制：距上次调用不足 1.1s 则等待。"""
    global _last_single_ts
    now = time.time()
    wait = _SINGLE_MIN_INTERVAL - (now - _last_single_ts)
    if wait > 0:
        time.sleep(wait)
    _last_single_ts = time.time()


def query_prices_mixed(market_hash_names: List[str],
                       preferred_platforms: Optional[List[str]] = None
                       ) -> Dict[str, float]:
    """
    批量 + 单条混用查询价格。

    流程：
      1. 查缓存，命中的直接返回。
      2. 剩余的用批量接口（每批 100 个，每分钟 1 次）。
      3. 批量未命中的（失败或无数据）用单条接口补齐（每分钟 60 次）。

    返回 { marketHashName: min_sell_price }
    """
    result: Dict[str, float] = {}
    pending: List[str] = []

    # 1. 缓存命中
    for mhn in market_hash_names:
        cached = _cache_get(mhn)
        if cached is not None:
            result[mhn] = cached
        else:
            pending.append(mhn)

    if not pending:
        return result

    client = SteamDTClient()

    # 2. 批量查询（每批 100）
    batch_failed: List[str] = []
    for i in range(0, len(pending), 100):
        batch = pending[i:i + 100]
        _wait_for_batch()
        try:
            data = client.batch_get_price(batch)
            for mhn in batch:
                plats = data.get(mhn, [])
                price = _extract_price(plats, preferred_platforms)
                if price is not None:
                    result[mhn] = price
                    _cache_set(mhn, price)
                else:
                    batch_failed.append(mhn)
        except Exception as e:
            msg = str(e)
            if "上限" in msg or "limit" in msg.lower():
                # 频率限制，剩余全部走单条
                batch_failed.extend(batch)
                print(f"[steamdt] 批量接口频率限制，改用单条查询")
                break
            else:
                print(f"[steamdt] 批量查询失败: {e}")
                batch_failed.extend(batch)

    # 3. 单条补齐（最多 50 条，避免触发频率限制）
    SINGLE_MAX = 50
    for idx, mhn in enumerate(batch_failed):
        if idx >= SINGLE_MAX:
            print(f"[steamdt] 单条查询达到上限 {SINGLE_MAX}，停止补齐")
            break
        _wait_for_single()
        try:
            resp = client.get_price(mhn)
            if not resp.success:
                err = resp.error_msg or resp.error_code_str or "未知错误"
                if "上限" in err or "limit" in err.lower():
                    print(f"[steamdt] 单条接口频率限制，停止查询")
                    break
                print(f"[steamdt] 单条查询失败 {mhn}: {err}")
                continue
            plats = resp.data or []
            price = _extract_price(plats, preferred_platforms)
            if price is not None:
                result[mhn] = price
                _cache_set(mhn, price)
        except Exception as e:
            msg = str(e)
            if "上限" in msg or "limit" in msg.lower():
                print(f"[steamdt] 单条接口频率限制，停止查询")
                break
            print(f"[steamdt] 单条查询异常 {mhn}: {e}")

    return result


def query_skin_prices(skin_grades: List[Tuple[str, str, str, bool]],
                      preferred_platforms: Optional[List[str]] = None
                      ) -> Dict[Tuple[str, str], float]:
    """
    便捷接口：传入 [(name, market_hash, wear_grade_cn, is_stattrak), ...]
    返回 {(name, wear_grade_cn): 最低价}。

    自动去重、缓存、批量+单条混用查询。
    """
    # 去重
    seen: Dict[Tuple[str, str, bool], str] = {}
    for name, mhash, grade, is_st in skin_grades:
        key = (name, grade, is_st)
        if key not in seen:
            seen[key] = build_market_hash_name(mhash, grade, is_st)

    mhn_list = list(seen.values())
    price_by_mhn = query_prices_mixed(mhn_list, preferred_platforms)

    out: Dict[Tuple[str, str], float] = {}
    for (name, grade, is_st), mhn in seen.items():
        if mhn in price_by_mhn:
            out[(name, grade)] = price_by_mhn[mhn]
    return out
