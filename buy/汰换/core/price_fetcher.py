"""比价查询：调用 found_buff / found_c5 / eco_client 脚本函数，归一化结果。"""
import logging
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Optional

import requests

from vendor import found_buff, found_c5
from core import eco_client

from utils.config import C5_APP_KEY, C5_APP_ID
from utils.config import BUFF_MAX_PAGES, BUFF_PAGE_DELAY, BUFF_RETRY_DELAY
from utils.config import BUFF_REQUEST_INTERVAL as _CFG_BUFF_REQ_INTERVAL
from utils.config import C5_REQUEST_INTERVAL as _CFG_C5_REQ_INTERVAL
from utils.config import C5_MAX_PAGES as C5_MAX_PAGES_CFG
from core import buff_auth

logger = logging.getLogger(__name__)
_BUFF_LOGGER = logging.getLogger("price_fetcher.query_buff")
_ECO_LOGGER  = logging.getLogger("price_fetcher.query_eco")
_C5_LOGGER   = logging.getLogger("price_fetcher.query_c5")
_QUERYALL_LOGGER = logging.getLogger("price_fetcher.query_all")
_BUFF_GATE_LOGGER = logging.getLogger("price_fetcher.buff_throttle_gate")
_C5_GATE_LOGGER  = logging.getLogger("price_fetcher.c5_throttle_gate")


def _ts() -> str:
    """毫秒级时间戳字符串（用于人类可读的日志对比）。"""
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


# ============================================================
# 【query_all 单飞去重 + 60s TTL 结果缓存】—— 解决重复请求风暴：
#   10 件久经小牛皮 MAC-10 会把同一收藏品的多件材料按 WeaponBox 分组，
#   同一 (hash_name, wear_min, wear_max) 组合在 query_tasks 里重复 10 次，
#   直接 ThreadPoolExecutor(16) 并发提交就会把 ECO 3 档降级×C5 翻页×Buff
#   查价 → 同一秒爆出 30+ 条请求，ECO=6001、C5=429、部分结果全空。
#
#   策略：
#     a. 每个归一化 cache key（buff_gid/c5_name/eco_name + 磨损/价格范围）
#        同时只能有 1 个线程在跑（用 threading.Event 做单飞栅栏）；
#        其他线程 wait 第一个跑完，直接共享结果。
#     b. 近 60 秒内相同 key 的成功结果直接复用缓存，再发都不用。
# ============================================================
class _SingleFlight:
    """简单单飞 + TTL 缓存实现（无第三方依赖，线程安全）。"""
    def __init__(self, ttl_seconds: float = 60.0):
        self.ttl = ttl_seconds
        self._lock = threading.Lock()
        # key -> (expire_monotonic_ts, results_tuple, errors_dict)
        self._cache: dict = {}
        # key -> threading.Event(已经有线程在跑，其他线程等它的结果写进 cache)
        self._inflight: dict = {}

    def _purge_expired_locked(self, now: float) -> None:
        expired = [k for k, (exp, _, _) in self._cache.items() if exp < now]
        for k in expired:
            self._cache.pop(k, None)

    def get(self, key):
        """返回 (HIT, cached_results_tuple_or_None, cached_errors_or_None)
        HIT=True: 结果在 TTL 内，可直接用。
        """
        with self._lock:
            now = time.monotonic()
            self._purge_expired_locked(now)
            item = self._cache.get(key)
            if item and item[0] >= now:
                return True, item[1], item[2]
            # 另一个线程在跑
            ev = self._inflight.get(key)
            if ev is not None:
                return "WAIT", ev, None
            # 没人在跑，当前 caller 应该自己发请求并把结果写回
            new_ev = threading.Event()
            self._inflight[key] = new_ev
            return False, new_ev, None

    def put(self, key, results_tuple, errors_dict) -> None:
        """写入缓存 + 释放所有在等这个 key 的 waiter。

        【失败不缓存】如果 errors_dict 包含平台级硬错误（键为 buff/c5/eco 的异常，
        如"请先登录"/429/网络错误），则不写入 60s TTL 缓存——避免用户修复问题
        （如刷新 Buff 登录态）后 60s 内仍命中旧的失败结果。
        "no_results"/"diag" 类诊断（真的无货）仍正常缓存，避免重复查无结果的区间。
        """
        hard_fail = False
        if isinstance(errors_dict, dict):
            for plat in ("buff", "c5", "eco"):
                v = errors_dict.get(plat)
                if v:
                    hard_fail = True
                    break
        with self._lock:
            ev = self._inflight.pop(key, None)
            if not hard_fail:
                exp = time.monotonic() + self.ttl
                self._cache[key] = (exp, results_tuple,
                                    dict(errors_dict) if isinstance(errors_dict, dict) else {})
        if isinstance(ev, threading.Event):
            ev.set()

    def wait_for(self, key, ev: threading.Event, timeout: float = 180.0):
        """另一个线程在跑时的等待；wait 超时则 caller 自己再发一次（fallback）。"""
        ok = ev.wait(timeout=timeout)
        if not ok:
            return False, None, None
        with self._lock:
            item = self._cache.get(key)
            if not item:
                return False, None, None
            return True, item[1], item[2]


_QUERYALL_SF = _SingleFlight(ttl_seconds=60.0)


def _qa_cache_key(item_config, wear_min, wear_max, price_min, price_max, platforms):
    """生成 query_all 归一化去重 key（和结果粒度对齐：同一批 platforms 下的相同参数 → 共享）"""
    def _s(v):
        if v is None: return ""
        if isinstance(v, float):
            # float 精度去抖：保留 9 位小数即可，避免 0.1500000001 和 0.15 看成不同 key
            return f"{v:.9f}"
        return str(v)
    platforms_sorted = sorted(list(platforms or [])) if platforms else []
    plat_tag = "|".join(platforms_sorted)
    bgid = (str(item_config.get("buff_goods_id") or "").strip() or "")
    c5n  = (str(item_config.get("c5_market_hash_name") or "").strip() or "")
    ecn  = (str(item_config.get("eco_market_hash_name") or "").strip() or "")
    # 不同 c5_app_id 不应该共享，因为 C5 查询时会有不同结果（概率极低，但严格一点）
    c5app = str(item_config.get("c5_app_id") or "")
    key_tup = (plat_tag, bgid, c5n, ecn, c5app,
               _s(wear_min), _s(wear_max), _s(price_min), _s(price_max))
    return "__".join(key_tup)


# ============================================================
# 【Buff 全局节流锁】—— 解决 Thundering Herd（惊群效应）：
#   之前在 query_buff 里每条线程自己 sleep 5s → 10 条同时 sleep 同时醒
#   → 毫秒级一起发 10 条请求 → Buff 瞬时流量封禁 429 → "只拿到 1 件数据"。
#   现在改成：**所有线程共享一把 Lock 才能发 1 条 safe_request**，且两条请求
#   的全局 monotonic 时间戳差必须 ≥ interval。
#   - interval=GUI user_settings 覆盖 > config.BUFF_REQUEST_INTERVAL 默认
#   - interval=0 时只互斥不同时发，不做额外 wait
# ============================================================
_BUFF_REQ_LOCK = threading.Lock()
_BUFF_LAST_REQ_TS: float = 0.0   # time.monotonic()，首次=0 表示第一条立即发


def _throttled_global_safe_request(url, headers, params) -> object | None:
    """跨线程全局串行化的 Buff safe_request（互斥 Lock + 严格 time gap）。

    只有拿到 _BUFF_REQ_LOCK 的线程才能走到 found_buff.safe_request；
    且距离上次发请求（全局 monotonic 时间）不足 interval 时先补 sleep。
    翻页请求也必须走这里（否则 10 件翻第 2 页又同时撞 Buff）。
    返回 found_buff.safe_request 的原始结果（requests.Response 或 None）。
    """
    global _BUFF_LAST_REQ_TS
    tname = threading.current_thread().name
    short_params = {k: params.get(k) for k in
                    ("goods_id", "page_num", "sort_by", "game",
                     "page_size", "min_paintwear", "max_paintwear")
                    if isinstance(params, dict) and k in params}
    gid = short_params.get("goods_id", "?")
    pagen = short_params.get("page_num", "?")
    t_acq0 = time.perf_counter()
    _BUFF_GATE_LOGGER.info(
        f"[{_ts()}] [BUFF-GATE] ⏳ thread={tname} goods_id={gid} page={pagen} "
        f"等待全局节流锁 ...（_BUFF_REQ_LOCK 被其他线程持有？）")
    with _BUFF_REQ_LOCK:
        t_acq = (time.perf_counter() - t_acq0) * 1000
        interval = _get_effective_buff_req_interval()
        now = time.monotonic()
        # 距离上一次发请求还剩多少要补 wait
        dt_since_last = now - _BUFF_LAST_REQ_TS
        wait_s = 0.0
        if interval and interval > 0:
            if _BUFF_LAST_REQ_TS == 0.0:
                wait_s = 0.0   # 第一条请求（进程刚启动）不用等
            else:
                wait_s = interval - dt_since_last
                if wait_s < 0:
                    wait_s = 0.0
        if wait_s > 0:
            # 0~0.5s 随机震荡：固定间隔的请求指纹容易被风控识别，叠加抖动更接近真人
            jitter = random.uniform(0.0, 0.5)
            w0 = time.perf_counter()
            _BUFF_GATE_LOGGER.info(
                f"[{_ts()}] [BUFF-GATE] 💤 thread={tname} goods_id={gid} page={pagen} "
                f"拿到锁后补 sleep wait={wait_s:.3f}s+jitter{ jitter:.3f}s（上一条 {dt_since_last:.3f}s 前，"
                f"需要 ≥interval={interval:.2f}s，lock 排队等了 {t_acq:.0f}ms）")
            time.sleep(wait_s + jitter)
            w_dt = (time.perf_counter() - w0) * 1000
            _BUFF_GATE_LOGGER.info(
                f"[{_ts()}] [BUFF-GATE] 💤 thread={tname} goods_id={gid} page={pagen} "
                f"补 sleep 完成，实际 {w_dt:.0f}ms（≈{(wait_s+jitter)*1000:.0f}ms）")
        else:
            _BUFF_GATE_LOGGER.info(
                f"[{_ts()}] [BUFF-GATE] ✔ thread={tname} goods_id={gid} page={pagen} "
                f"无需补 sleep（上一条 {dt_since_last:.3f}s 前 ≥ interval={interval:.2f}s，"
                f"lock 排队等了 {t_acq:.0f}ms）→ 立即发 HTTP")
        # ============ 发真实 HTTP ============
        try:
            resp = found_buff.safe_request(url, headers, params)
        finally:
            _BUFF_LAST_REQ_TS = time.monotonic()    # 无论成功/抛异常，都更新最后时间戳
        _BUFF_GATE_LOGGER.info(
            f"[{_ts()}] [BUFF-GATE] ↡ thread={tname} goods_id={gid} page={pagen} "
            f"HTTP 返回：resp is None?={resp is None}；status_code={getattr(resp,'status_code','N/A')}，"
            f"本线程 gate+HTTP 总耗时 {((time.perf_counter() - t_acq0) * 1000):.0f}ms → 释放锁")
        return resp

# ECO 查询页数上限
ECO_MAX_PAGES = 5

# ============================================================
# Buff 服务端磨损筛选（自定义模式 min_paintwear / max_paintwear）
#   2026-09-03 用户给的参考 URL：
#     https://buff.163.com/goods/1146810#tab=selling&page_num=1&min_paintwear=0.50&max_paintwear=0.63
#   这对应网页「磨损区间」下拉框里的「自定义」li（id=custom-float-range），
#   可以传任意 2 位小数上下界，不受 HTML 里那 5 个预设档位限制。
#   直接把 wear_min/wear_max 作为 HTTP 参数传给 sell_order 接口，
#   Buff 服务端就只返回该区间内的在售 → 不可能再出现"深翻 10 页也
#   0 条命中"的情况。而且客户端 filter_orders 仍保留作为第二道防线，
#   确保 API 接口某天行为变化时也不会把区间外的物品当最低价。
#
#   同时按用户要求：有磨损范围筛选时最多只翻 2 页（page_size=50，最多
# ============================================================
# 【2026-09-03 浏览器实证（Buff 1146810 在售 tab）：磨损筛选纯前端 DOM 过滤】
#   用真实浏览器操作了 li@value="0.50-0.63" 和 li@value="0.90-1.00" 两个预设，
#   对比前后 /api/market/goods/sell_order 的 Network 请求：
#     - 点预设前：初始化 GET 2 条（goods_id=1146810 page_num=1/2，参数仅
#       {game goods_id page_num sort_by=default mode allow_tradable_cooldown _}）
#     - 点预设后：新增 0 条 sell_order 请求（GET 和 POST 都 0）
#   → **Buff /api/market/goods/sell_order GET/POST 均不接受任何磨损子区间
#     服务端筛选参数**（float_range / paintwear_min / min_paintwear 都无效）。
#     网页里的磨损筛选完全由前端 JavaScript 读取 `custom_paintwear_val.value`
#     字符串后在已下载的 page 1+ page 2 数据上做 DOM 过滤。
#
#   因此：彻底撤掉所有"服务端磨损筛选 GET 参数"尝试代码，
#   回到纯客户端 filter_orders + price.asc + 翻页上限=10 路径。
#   产出、材料两边查价完全同构（同一函数 / 同参数集合 / 同 cap=10）。
#   page_size=50 × 10 页 = 最多 500 条在售（冷热门档足够，配合 price.asc
#   早停：热门磨损 1~3 页命中即停止，不会触发 429）。
# ============================================================
BUFF_WEAR_FILTER_MAX_PAGES = 10

# ============================================================
# 【Buff 服务端磨损筛选参数精度：最大 4 位小数】
#   2026-09-05 浏览器实证 + 用户确认：
#     goods/33906#tab=selling&min_paintwear=0.18&max_paintwear=0.2151
#   → sell_order API 接受 min_paintwear/max_paintwear，最大精度 4 位小数。
#   超过 4 位会被服务端截断（或忽略），所以 round 到 4 位后去末尾 0。
# ============================================================
BUFF_PAINTWEAR_MAX_DECIMALS = 4


def _buff_paintwear_str(value) -> Optional[str]:
    """把磨损值格式化为 Buff 服务端筛选接受的字符串（最大 4 位小数）。

    规则：
      1. round 到 4 位小数（消浮点误差，如 0.07*100）
      2. 去掉末尾多余的 0（0.2640 → "0.264"，0.2151 → "0.2151"，0.15 → "0.15"）
      3. None/空/非法/越界 → 返回 None（不传该参数，走不限）
    """
    if value in (None, ""):
        return None
    try:
        fv = float(value)
    except (TypeError, ValueError):
        return None
    if not (0.0 <= fv <= 1.0):
        return None
    # round 到 4 位，再去末尾 0
    rounded = round(fv, BUFF_PAINTWEAR_MAX_DECIMALS)
    s = f"{rounded:.{BUFF_PAINTWEAR_MAX_DECIMALS}f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    if s == "" or s == ".":
        s = "0"
    if "." not in s:
        s = s + ".0"
    return s


# 【Buff 单页条数】实测 sell_order 接口支持 page_size 参数（默认 10，最大 50+）。
#   一次拿 50 条最便宜订单：① 磨损过滤早停更容易在第 1 页命中；
#   ② 无货时深翻页请求量降到原来的 1/5（91 条在售 2 页扫完 vs 原来 10 页）。
BUFF_PAGE_SIZE = 50

# 运行时可覆盖的爬取参数（GUI 可调）
_buff_max_pages = BUFF_MAX_PAGES
_buff_page_delay = BUFF_PAGE_DELAY


def _get_effective_buff_req_interval() -> float:
    """当前实际生效的 Buff 请求前间隔（秒）。优先级：
    1) GUI 实时覆盖（user_settings.buff_request_interval_s）
    2) config.py 默认值（BUFF_REQUEST_INTERVAL）
    返回值范围 [0.0, 30.0]
    """
    from core import user_settings as _us   # 延迟 import，避免模块初始化循环
    return _us.get_buff_request_interval(_CFG_BUFF_REQ_INTERVAL)


def set_runtime_buff_req_interval(seconds: float | None) -> float:
    """GUI 调用：设置/清除运行时 Buff 请求间隔（持久化到 user_settings，下次启动自动生效）。

    返回实际生效的值。seconds = None 表示清除覆盖，回退到 config.py 默认值。
    """
    from core import user_settings as _us
    _us.set_buff_request_interval(seconds)
    return _get_effective_buff_req_interval()


# ============================================================
# 【C5 全局节流锁】—— 解决 openapi.c5game.com 429 Too Many Requests：
#   C5 合作方 IP 白名单对 merchant/* 请求有严格秒级限额；10 件同产出并发
#   query_c5 时很容易爆出 429 → 整条 C5 价格 None。
#   做法：所有需要调用 C5 openapi 的地方（requests.post batch、
#         found_c5.query_product_list、found_c5 内部的其他 HTTP）都先走
#         这里的 Lock+monotonic gate，两条请求时间差必须 ≥ interval。
# ============================================================
_C5_REQ_LOCK = threading.Lock()
_C5_LAST_REQ_TS: float = 0.0


def _get_effective_c5_req_interval() -> float:
    """C5 实际请求间隔（GUI user_settings 覆盖 > config.C5_REQUEST_INTERVAL 默认）。"""
    from core import user_settings as _us
    try:
        v = _us.get_c5_request_interval(_CFG_C5_REQ_INTERVAL)
    except Exception:
        try: v = float(_CFG_C5_REQ_INTERVAL)
        except (TypeError, ValueError): v = 1.0
    try:
        return max(0.0, min(30.0, float(v)))
    except (TypeError, ValueError):
        return 1.0


def _c5_gate_sleep(tag: str = "") -> None:
    """C5 请求前统一 sleep 一下（全局串行化 + monotonic 间隔）。

    传 tag 仅用于日志（哪一个 C5 入口在排队）。一般不对外暴露给调用者，
    C5 内部包装函数在真实 HTTP 前各调一次即可。返回后 caller 可以立刻发请求。
    """
    global _C5_LAST_REQ_TS
    tname = threading.current_thread().name
    t_acq0 = time.perf_counter()
    with _C5_REQ_LOCK:
        t_acq_ms = (time.perf_counter() - t_acq0) * 1000
        interval = _get_effective_c5_req_interval()
        now = time.monotonic()
        wait_s = 0.0
        if interval > 0 and _C5_LAST_REQ_TS > 0:
            wait_s = max(0.0, interval - (now - _C5_LAST_REQ_TS))
        if wait_s > 0:
            # 0~0.5s 随机震荡：避免固定间隔的请求指纹被风控识别
            jitter = random.uniform(0.0, 0.5)
            _C5_GATE_LOGGER.info(
                "[%s] [C5-GATE] 💤 thread=%s tag=%r 补 sleep=%.3fs+jitter%.3fs "
                "(上一条 %+.3fs 前, 需要 ≥%.2fs, 锁等待 %.0fms)",
                _ts(), tname, tag, wait_s, jitter, now - _C5_LAST_REQ_TS, interval, t_acq_ms)
            time.sleep(wait_s + jitter)
        else:
            _C5_GATE_LOGGER.debug(
                "[%s] [C5-GATE] ✔ thread=%s tag=%r 无需 sleep "
                "(上一条 %+.3fs 前, interval=%.2fs, 锁等待 %.0fms)",
                _ts(), tname, tag, now - _C5_LAST_REQ_TS, interval, t_acq_ms)
        _C5_LAST_REQ_TS = time.monotonic()


def set_buff_scrape_config(max_pages: int, page_delay: float):
    """运行时设置 Buff 爬取强度（由 GUI 调用）。"""
    global _buff_max_pages, _buff_page_delay
    _buff_max_pages = max(1, min(max_pages, 50))
    _buff_page_delay = max(0.1, min(page_delay, 30.0))
    # 同步覆盖 found_buff 的重试退避基础延迟
    found_buff.RETRY_DELAY = BUFF_RETRY_DELAY


def get_buff_scrape_config() -> tuple:
    """返回当前 (max_pages, page_delay)。"""
    return _buff_max_pages, _buff_page_delay


def _resolve_buff_cookies():
    """优先用 BuffAuthManager 的动态 session/csrf，找不到回退到 found_buff.py 常量。"""
    c = buff_auth.get_session_cookies(force_refresh=False, allow_playwright=False)
    if c and c.session and c.csrf_token:
        return c.session, c.csrf_token, c.source
    return found_buff.SESSION, found_buff.CSRF_TOKEN, "hardcoded(found_buff.py)"


def query_buff(goods_id, wear_min=None, wear_max=None,
               price_min=None, price_max=None):
    """查询 Buff 平台（最多 BUFF_MAX_PAGES 页），返回归一化商品列表。

    若 Buff 返回错误（如 Session 过期），直接抛出 RuntimeError 以便 UI 提示。
    """
    gid = int(goods_id)
    # ---- 参数类型强转（避免上游 paint_wear 传字符串 → 下游 :.6f 格式化 ValueError 被
    #      query_all except 吞掉 → 整条产出 BUFF/ECO 栏全空）----
    def _f(v):
        if v is None or v == "":
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None
    wear_min = _f(wear_min)
    wear_max = _f(wear_max)
    price_min = _f(price_min)
    price_max = _f(price_max)
    # ============================================================
    # 【2026-09-06 彻底废弃 Buff API，仅用浏览器 URL 爬取】
    #   用户明确要求：删除所有直接 requests 调 /api/market/goods/sell_order 的代码，
    #   只通过 Playwright 打开 goods/{id}#tab=selling&min_paintwear=&max_paintwear=
    #   URL，拦截前端 sell_order 响应获取数据。磨损筛选由前端 JS 处理。
    #   浏览器不可用 / 登录过期 / 查询失败 → 直接抛 RuntimeError，不再静默回退 API。
    # ============================================================
    from core import buff_browser_query
    browser_results = buff_browser_query.query_buff_via_browser(
        gid, wear_min, wear_max, price_min, price_max,
        max_pages=2)
    if browser_results is not None:
        _BUFF_LOGGER.info(
            f"[{_ts()}] [BUFF] ✅ 浏览器查询完成 goods_id={gid} "
            f"返回 {len(browser_results)} 条 wear=[{wear_min},{wear_max}]")
        return browser_results
    # browser_results is None → Playwright 未安装
    raise RuntimeError(
        f"Buff 查询失败：Playwright 未安装 (goods_id={gid})。"
        f"请执行 pip install playwright && playwright install msedge")


# —— 线程安全（query_buff → query_all 诊断传递）——
# 严格模式下 query_buff 返回空 list 时把一句话诊断写进下面 dict，
# query_all 拿到 results=[] 后从这里按线程名读出，塞进 errors.buff_diag。
import threading as _thr
_buf_note_lock = _thr.Lock()
_buf_note: dict[str, list[dict]] = {}


def _take_buff_diagnosis(tname: str, goods_id: "int|str|None") -> "list[str]":
    """取出当前线程 query_buff 留下的诊断（读取即删除，防止越积越大）。"""
    if not goods_id:
        return []
    try:
        gid = int(goods_id)
    except (TypeError, ValueError):
        return []
    with _buf_note_lock:
        lst = _buf_note.get(tname) or []
        out = [d["diagnosis"] for d in lst if d.get("goods_id") == gid]
        remain = [d for d in lst if d.get("goods_id") != gid]
        if remain:
            _buf_note[tname] = remain
        elif tname in _buf_note:
            del _buf_note[tname]
    return out


def _fmt_wear_and_price(wmin, wmax, pmin, pmax) -> str:
    """filter_orders 参数的摘要字符串。"""
    parts = []
    if wmin is not None or wmax is not None:
        parts.append(f"wear=[{wmin}, {wmax}]")
    if pmin is not None or pmax is not None:
        parts.append(f"price=[{pmin}, {pmax}]")
    return "(" + ", ".join(parts) + ")" if parts else ""


def query_c5(market_hash_name, app_id=C5_APP_ID,
             wear_min=None, wear_max=None,
             price_min=None, price_max=None,
             full_list=False):
    """查询 C5 平台最低价，优先走 price/batch 快速接口拿最低 itemId+price，
    无结果再翻页 merchant/market/v2/products/list（最多 C5_MAX_PAGES 页）。

    :param full_list: True=跳过 batch 快速路径，走翻页获取带 paintwear 的多条商品
                      （GUI 查询购买需要展示列表时用）；False=优先 batch 拿最低价
                      （模拟计算只需要最低价时用）。
    第 1 页就返回错误时抛出 RuntimeError，以便 UI 明确提示（如 IP 白名单等）。
    只有「当前页无数据 / 后续页无结果」属于正常结束。
    """
    def _f(v):
        if v is None or v == "":
            return None
        try: return float(v)
        except (TypeError, ValueError): return None
    wear_min = _f(wear_min)
    wear_max = _f(wear_max)
    price_min = _f(price_min)
    price_max = _f(price_max)
    try:
        app_id = int(app_id)
    except (TypeError, ValueError):
        app_id = C5_APP_ID
    results = []
    page = 1
    max_pages = C5_MAX_PAGES_CFG

    # ---------- 优化：先走 price/batch 接口拿该 marketHashName 的最低挂牌价 ----------
    # （这个接口 1 次 HTTP 就能返回最低价格 + itemId，比翻 5 页 products/list 快 10 倍；
    #   若 batch 里返回的 price 在 [wear_min, wear_max] 过滤后仍合法，就直接用。
    #   注意：batch 接口只给 "最低在售价 + itemId"，不给 paintwear，所以 wear 过滤只能
    #   交给后续翻页或"相信 batch 最低就是全档最低"—— 这里若 wear 过滤是官方整档
    #   （FN=[0,0.07] 等）直接信任 batch price；若用户自定义了严格 wear 范围（如
    #   [0.02, 0.03]）再走翻页产品列表拿精确 paintwear 做过滤。）
    #   full_list=True 时跳过 batch（GUI 需要带磨损的完整列表）。
    if not full_list:
        try:
            _c5_gate_sleep(tag="price/batch")
            batch_resp = requests.post(
                "https://openapi.c5game.com/merchant/product/price/batch",
                params={"app-key": C5_APP_KEY},
                json={"appId": app_id, "marketHashNames": [market_hash_name]},
                timeout=10,
            )
            batch_data = (batch_resp.json() or {}).get("data") or {}
            # 找匹配 key：batch 结果可能把传入名或不带括号等变体作为 key
            batch_hit = None
            if isinstance(batch_data, dict):
                for k, v in batch_data.items():
                    if k == market_hash_name or market_hash_name in k or k in market_hash_name:
                        batch_hit = v if isinstance(v, dict) else None
                        break
            if batch_hit:
                bp_raw = batch_hit.get("price") or batch_hit.get("minPrice") \
                         or batch_hit.get("lowestPrice") or batch_hit.get("LowestPrice")
                try:
                    bprice = float(bp_raw)
                except (TypeError, ValueError):
                    bprice = 0.0
                if bprice > 0:
                    # 判断 wear 过滤是否整档（官方 5 档之一）→ 整档就信任 batch 最低价
                    #   否则需要翻页拿具体 paintwear 做过滤（因为 batch 没返回 wear）
                    official_range = (
                        # (min, max) pair: 5 档官方精确磨损
                        (0.0, 0.07), (0.07, 0.15), (0.15, 0.38),
                        (0.38, 0.45), (0.45, 1.0),
                        # 无过滤
                        (None, None),
                    )
                    is_official_wear = False
                    for omin, omax in official_range:
                        if (omin == wear_min and omax == wear_max) or \
                           (omin is None and wear_min is None and wear_max is None):
                            is_official_wear = True
                            break
                    # 用户自定义磨损范围太窄 → 不信任 batch，必须翻页拿具体 paintwear
                    price_in_filter = (
                        (price_min is None or bprice >= price_min) and
                        (price_max is None or bprice <= price_max)
                    )
                    if price_in_filter and is_official_wear:
                        return [{
                            "platform": "c5",
                            "order_id": "",
                            "goods_id": str(batch_hit.get("itemId") or ""),
                            "price": float(bprice),
                            "wear": 0.0,   # batch 不返回 paintwear，置 0 不展示 float
                            "wear_name": "",  # 由调用方按整档 wear_min/wear_max 展示中文磨损档
                            "paintseed": "",
                            "assetid": "",
                            "_source": "c5_price_batch_fast",
                        }]
        except Exception:
            # batch 失败完全不影响，退回逐页翻
            pass

    while page <= max_pages:
        _c5_gate_sleep(tag=f"products/list?page={page}")
        resp = found_c5.query_product_list(
            app_key=C5_APP_KEY,
            market_hash_name=market_hash_name,
            app_id=app_id,
            page_num=page,
            page_size=50,
        )
        if not resp or not resp.get("success"):
            # 第 1 页就失败 → 抛出错误提示用户；后续页失败视为正常结束
            if page == 1:
                msg = ""
                if resp:
                    msg = resp.get("msg") or resp.get("message") or str(resp)
                raise RuntimeError(
                    f"C5 API 错误 (market_hash_name={market_hash_name!r})："
                    f"{msg or 'resp=None'}。"
                    f"常见原因：IP 不在白名单 / APP_KEY 错误。")
            break
        data = resp.get("data", {})
        product_list = data.get("list", [])
        if not product_list:
            break
        for product in product_list:
            asset = product.get("assetInfo", {})
            fw = asset.get("floatWear")
            try:
                wear = float(fw) if fw is not None else 0.0
            except (TypeError, ValueError):
                wear = 0.0
            try:
                price = float(product.get("price", 0))
            except (TypeError, ValueError):
                price = 0.0
            # 应用筛选
            if wear_min is not None and wear < wear_min:
                continue
            if wear_max is not None and wear > wear_max:
                continue
            if price_min is not None and price < price_min:
                continue
            if price_max is not None and price > price_max:
                continue
            results.append({
                "platform": "c5",
                "order_id": str(product.get("productId", "")),
                "goods_id": "",
                "price": price,
                "wear": wear,
                "wear_name": found_c5.get_wear_name(wear),
                "paintseed": str(asset.get("paintSeed", "")),
                "assetid": str(asset.get("assetId", "")),
            })
        if not data.get("hasMore", False):
            break
        page += 1
    return results


def _extract_base_hash_name(hash_name: str) -> str:
    """从 market_hash_name 去除末尾 (Factory New) 等英文磨损括号后缀，返回基础模板名。

    例："MAC-10 | Calf Skin (Factory New)" → "MAC-10 | Calf Skin"
    例："StatTrak™ AWP | Wildfire (Minimal Wear)" → "StatTrak™ AWP | Wildfire"
    """
    if not isinstance(hash_name, str):
        return ""
    s = hash_name.strip()
    if not s:
        return ""
    # 去掉末尾最后一对英文括号中的内容（如果内容里含常见磨损词）
    open_i = s.rfind("(")
    close_i = s.rfind(")")
    if open_i >= 0 and close_i > open_i and close_i == len(s) - 1:
        tail = s[open_i+1:close_i].strip()
        wear_kw = ("Factory New", "Minimal Wear", "Field-Tested",
                   "Well-Worn", "Battle-Scarred", "Not Painted")
        if any(k in tail for k in wear_kw):
            return s[:open_i].strip()
    return s


def _eco_cn_suffixed_name(base_name: str, wear_min, wear_max) -> str:
    """用 base_name + 中文磨损括号，拼「中文名+（磨损）」版本。

    例：base="MAC-10 | Calf Skin" wear=[0,0.07] → "MAC-10 | Calf Skin（崭新出厂）"
    """
    if not isinstance(base_name, str) or not base_name:
        return ""
    lo = float(wear_min) if wear_min is not None else 0.0
    hi = float(wear_max) if wear_max is not None else 0.0
    mid = (lo + hi) / 2.0
    wear_cn = _wear_to_name(mid) if (wear_min is not None or wear_max is not None) else ""
    if not wear_cn:
        return ""
    return f"{base_name.strip()}（{wear_cn}）"


def query_eco(hash_name, app_id="730",
              wear_min=None, wear_max=None,
              price_min=None, price_max=None):
    """查询 ECO 平台在售商品列表，返回归一化商品列表。

    ECO API 支持服务端磨损过滤（StartPaintWear/EndPaintWear），
    价格过滤在客户端进行。

    **自动降级重试 3 档**：
      Retry#1 原始 hash_name（如英文带磨损后缀）
      Retry#2 去磨损后缀的基础模板名（MAC-10 | Calf Skin）
      Retry#3 中文磨损后缀拼接版（MAC-10 | Calf Skin（崭新出厂））
    任一档 TotalRecord>0 即采用该档结果，无需用户手动切换命名风格。
    """
    # ---- 参数类型强转（同 query_buff：避免 :.5f/:.2f 炸 ValueError）
    def _f(v):
        if v is None or v == "":
            return None
        try: return float(v)
        except (TypeError, ValueError): return None
    wear_min = _f(wear_min)
    wear_max = _f(wear_max)
    price_min = _f(price_min)
    price_max = _f(price_max)
    try:
        app_id = str(int(app_id))
    except (TypeError, ValueError):
        app_id = "730"
    tname = threading.current_thread().name
    t0 = time.perf_counter()
    _ECO_LOGGER.info(
        f"[{_ts()}] [QUERY_ECO ▶ START] thread={tname} hash_name={hash_name!r} "
        f"app_id={app_id} wear=[{wear_min},{wear_max}] price=[{price_min},{price_max}]")

    if not hash_name:
        raise RuntimeError("ECO 查询需要 hash_name（市场哈希名）。")

    # ============== 构造 3 档 hash_name 变体 ==============
    variants = []  # (label, use_variant_hash_name, use_wear_params)
    v1_hash = str(hash_name).strip()
    variants.append(("① 原始 hash_name（英文带磨损后缀）", v1_hash, True))

    v2_base = _extract_base_hash_name(v1_hash)
    if v2_base and v2_base != v1_hash:
        # 基础名不带磨损 → 保留服务端 wear 过滤（更安全，不会把其他磨损档混进来）
        variants.append(("② 去磨损后缀（基础模板名）", v2_base, True))

    if wear_min is not None or wear_max is not None:
        v3_cn = _eco_cn_suffixed_name(v2_base or v1_hash, wear_min, wear_max)
        if v3_cn and v3_cn not in (v1_hash, v2_base):
            # 中文名本身带磨损 → 不再传 StartPaintWear（避免双过滤冲突）
            variants.append(("③ 中文磨损后缀（与 ECO 官网商品名一致）", v3_cn, False))

    # 去重（保持顺序，用标签即可）
    seen = set()
    unique_variants = []
    for lab, vn, uw in variants:
        if not vn: continue
        key = (vn, uw)
        if key in seen: continue
        seen.add(key)
        unique_variants.append((lab, vn, uw))
    if not unique_variants:
        unique_variants.append(("① 原始", v1_hash, True))

    # 磨损参数钳制（复用原逻辑，仅 use_wear_params=True 时才传给 ECO）
    def _clamp_wear(lo, hi):
        lo_out = lo if lo is not None else None
        hi_out = hi if hi is not None else None
        if lo_out is not None:
            try:
                fv = float(lo_out)
                if fv <= 0: lo_out = 1e-6
                elif fv > 1: lo_out = 1.0
            except (TypeError, ValueError): lo_out = None
        if hi_out is not None:
            try:
                fv = float(hi_out)
                if fv <= 0: hi_out = 1e-6
                elif fv > 1: hi_out = 1.0
            except (TypeError, ValueError): hi_out = None
        return lo_out, hi_out

    def _one_attempt(label, cur_hash, use_wear_params) -> tuple[list, str]:
        api_lo, api_hi = _clamp_wear(wear_min, wear_max) if use_wear_params else (None, None)
        suffix_conflict = False
        if isinstance(cur_hash, str) and cur_hash.count("(") >= 1:
            try:
                tail = cur_hash[cur_hash.rindex("("):cur_hash.rindex(")")] \
                    if ")" in cur_hash else ""
            except ValueError:
                tail = ""
            if tail and ("Wear" in tail or "Factory" in tail or "Minimal" in tail or
                         "Field" in tail or "Well" in tail or "Battle" in tail or
                         "崭新出厂" in tail or "略有磨损" in tail or
                         "久经沙场" in tail or "破损不堪" in tail or
                         "战痕累累" in tail):
                suffix_conflict = True
        if suffix_conflict and use_wear_params and (api_lo is not None or api_hi is not None):
            _ECO_LOGGER.warning(
                f"[{_ts()}] [QUERY_ECO ⚠ RETRY] thread={tname} variant={label!r} "
                f"hash_name={cur_hash!r} 已带磨损后缀，同时又传入服务端磨损过滤 "
                f"[StartPaintWear={api_lo}, EndPaintWear={api_hi}]。若 TotalRecord=0 "
                f"将自动切换下一档命名规则。")
        try:
            items = eco_client.fetch_all_sell_goods(
                hash_name=cur_hash,
                game_id=str(app_id),
                start_paint_wear=api_lo,
                end_paint_wear=api_hi,
                max_pages=ECO_MAX_PAGES,
            )
        except RuntimeError as e:
            _ECO_LOGGER.error(
                f"[{_ts()}] [QUERY_ECO ✗ SDK FAIL] thread={tname} "
                f"variant={label!r} hash_name={cur_hash!r}：{e}")
            raise
        except Exception as e:
            _ECO_LOGGER.error(
                f"[{_ts()}] [QUERY_ECO ✗ FAIL] thread={tname} "
                f"variant={label!r} hash_name={cur_hash!r}：{type(e).__name__}: {e}")
            raise RuntimeError(
                f"ECO 查询失败 variant={label!r} hash_name={cur_hash!r}：{e}") from e
        return items, label

    # ============== 3 档依次尝试，任何一档 items 非空即采用 ==============
    items: list = []
    used_label = ""
    used_hash = ""
    attempt_errors: list[str] = []
    for idx, (lab, vhash, uw) in enumerate(unique_variants, start=1):
        try:
            cand_items, _ = _one_attempt(lab, vhash, uw)
            if cand_items:
                items, used_label, used_hash = cand_items, lab, vhash
                _ECO_LOGGER.info(
                    f"[{_ts()}] [QUERY_ECO FALLBACK HIT ✔] thread={tname} "
                    f"attempt=#{idx}/{len(unique_variants)} variant={lab!r} "
                    f"hash_name={vhash!r} → 命中 {len(cand_items)} 条在售，"
                    f"跳过剩余 #{len(unique_variants)-idx} 档重试。")
                break
            else:
                attempt_errors.append(
                    f"#{idx}/{len(unique_variants)} {lab!r} hash={vhash!r} → TotalRecord=0")
                _ECO_LOGGER.info(
                    f"[{_ts()}] [QUERY_ECO FALLBACK TRY ⏭] thread={tname} "
                    f"attempt=#{idx}/{len(unique_variants)} variant={lab!r} "
                    f"hash_name={vhash!r} → 0 条在售，继续下一档。")
        except RuntimeError as e:
            attempt_errors.append(
                f"#{idx}/{len(unique_variants)} {lab!r} → ERROR: {str(e)[:200]}")
            continue

    # ============== 全 3 档空 → 聚合诊断 ==============
    if not items:
        tips = [
            f"全部 {len(unique_variants)} 档 hash_name 命名规则均返回 0 条或错误："
            + "； ".join(attempt_errors) + "。",
        ]
        tips.append(
            "可能原因：ECO 当前平台该商品确实无在售（已尝试三种主流命名规则）；"
            "或合作方未开放 SellGoodsList 访问权限（此时 ResultCode 非 0，上一条日志会写明）。")
        diag = " ".join(tips)
        _ECO_LOGGER.warning(
            f"[{_ts()}] [QUERY_ECO ⚠ ALL EMPTY] thread={tname} hash_name={hash_name!r}。{diag}")
        # 把聚合诊断透传给上层 query_all，避免 errors 里 eco_no_results 只写一句模糊文案
        try:
            errors_store
        except NameError:
            pass
    else:
        _ECO_LOGGER.info(
            f"[{_ts()}] [QUERY_ECO SDK END] thread={tname} used_variant={used_label!r} "
            f"used_hash={used_hash!r} raw_count={len(items)}"
            + (f" 前3个价格(PaintWear)=%s" %
               ", ".join(
                   f"(¥{float(it.get('SellingPrice') or 0):.2f}@w={float(it.get('PaintWear') or 0):.5f})"
                   for it in items[:3]) if items else ""))

    # 客户端二次过滤
    results = []
    wears_ok = []
    prices_ok = []
    n_skipped_wear = 0
    n_skipped_price = 0
    for item in items:
        try:
            wear = float(item.get("PaintWear", 0))
        except (TypeError, ValueError):
            wear = 0.0
        try:
            price = float(item.get("SellingPrice", 0))
        except (TypeError, ValueError):
            price = 0.0
        if wear_min is not None and wear < wear_min:
            n_skipped_wear += 1; continue
        if wear_max is not None and wear > wear_max:
            n_skipped_wear += 1; continue
        if price_min is not None and price < price_min:
            n_skipped_price += 1; continue
        if price_max is not None and price > price_max:
            n_skipped_price += 1; continue
        wear_name = _wear_to_name(wear)
        wears_ok.append(wear); prices_ok.append(price)
        results.append({
            "platform": "eco",
            "order_id": str(item.get("GoodsNum", "")),
            "goods_id": "",
            "price": price,
            "wear": wear,
            "wear_name": wear_name,
            "paintseed": str(item.get("PaintSeed", "")),
            "assetid": "",
            "_eco_variant_label": used_label,   # 诊断：哪档命名命中的
            "_eco_variant_hash": used_hash,
        })

    plat_prices = sorted(prices_ok)
    min_p = plat_prices[0] if plat_prices else None
    max_p = plat_prices[-1] if plat_prices else None
    wear_rng = (
        f"wear_ok=[{min(wears_ok):.5f}, {max(wears_ok):.5f}] n={len(wears_ok)}"
        if wears_ok else "wear_ok=0")
    dt_ms = (time.perf_counter() - t0) * 1000
    _ECO_LOGGER.info(
        f"[{_ts()}] [QUERY_ECO END] thread={tname} hash_name={hash_name!r} "
        f"used_variant={used_label!r} filtered_count={len(results)} {wear_rng} "
        f"price=[{('¥%.2f'%min_p) if min_p is not None else 'None'}, "
        f"{('¥%.2f'%max_p) if max_p is not None else 'None'}] "
        f"skip(wear={n_skipped_wear},price={n_skipped_price}) dt={dt_ms:.0f}ms")
    return results


def _wear_to_name(wear: float) -> str:
    """磨损值转中文磨损等级名称。"""
    if wear < 0.07:
        return "崭新出厂"
    elif wear < 0.15:
        return "略有磨损"
    elif wear < 0.38:
        return "久经沙场"
    elif wear < 0.45:
        return "破损不堪"
    else:
        return "战痕累累"


def query_all(item_config, wear_min=None, wear_max=None,
              price_min=None, price_max=None, platforms=None,
              c5_full_list=False):
    """并行查询多平台，按价格升序合并返回。

    :param item_config: dict，含 buff_goods_id / c5_market_hash_name / c5_app_id
    :param platforms: list 如 ['buff','c5','eco']，None 表示全部
    :param c5_full_list: True=C5 走翻页获取带磨损的完整列表（GUI 查询购买用）；
                         False=优先 batch 拿最低价（模拟计算用）。
    :return: (results, errors) results 为归一化列表，errors 为各平台错误信息
    """
    # ---- 参数类型强转（避免 wear/price 字符串型 :.2f 格式化 ValueError）----
    def _f(v):
        if v is None or v == "":
            return None
        try: return float(v)
        except (TypeError, ValueError): return None
    wear_min = _f(wear_min)
    wear_max = _f(wear_max)
    price_min = _f(price_min)
    price_max = _f(price_max)
    tname = threading.current_thread().name
    t0 = time.perf_counter()
    if platforms is None:
        platforms = ["buff", "c5", "eco"]

    buff_gid = item_config.get("buff_goods_id", "")
    c5_name = item_config.get("c5_market_hash_name", "")
    c5_app_id = int(item_config.get("c5_app_id", C5_APP_ID))
    # ECO hash_name 优先级：① item_config 自带 eco_market_hash_name（ECO 官方 StartSimulation
    # 返回的真实 HashName，产出/材料如果传了就优先用，避免复用 C5 推断名出现命名不一致）；
    # ② 回退到 c5_name（市场哈希名，C5/ECO 通用的兜底）。
    eco_own = (item_config.get("eco_market_hash_name") or "").strip()
    if eco_own and eco_own.lower() != (c5_name or "").lower():
        eco_name = eco_own
        _QUERYALL_LOGGER.info(
            f"[{_ts()}] [QUERY_ALL eco_name] thread={tname} 使用 item_config.eco_market_hash_name="
            f"{eco_own!r}（覆盖 c5_market_hash_name={c5_name!r}）")
    else:
        eco_name = c5_name
    _meta_tag = item_config.get("_meta_tag") or item_config.get("cn") or f"buff={buff_gid}"
    # 【查询名称 + 磨损范围日志】优先用 item_name（材料），回退 c5_market_hash_name（产出）
    _disp_name = (item_config.get("item_name")
                  or item_config.get("wear_grade")
                  or item_config.get("c5_market_hash_name")
                  or _meta_tag)
    _wear_grade = item_config.get("wear_grade") or ""

    # ============================================================
    # 【单飞去重】10 件同收藏同皮肤 → 同一 key 同时被 ThreadPoolExecutor(16) 并发提交
    # → 结果风暴触发 ECO 6001/C5 429/Buff 429 → 部分价格 None。
    # 这里先做单飞 + 60s TTL：a. TTL 命中直接取；b. 有线程在跑就等；c. 我是第一只，
    # 真正发请求后 put 把 cache 和 event 给 waiters。
    # ============================================================
    platforms_sorted_frozen = tuple(sorted(list(platforms))) if platforms else ()
    _qa_key = _qa_cache_key(
        item_config, wear_min, wear_max, price_min, price_max, list(platforms_sorted_frozen))
    _sf_state, _sf_x, _sf_err = _QUERYALL_SF.get(_qa_key)
    # CASE 1: TTL 内缓存命中 → 直接 return 缓存里的 (results, errors)
    if _sf_state is True:
        _cached_res = list(_sf_x) if isinstance(_sf_x, (list, tuple)) else []
        _cached_err = _sf_err if isinstance(_sf_err, dict) else {}
        _QUERYALL_LOGGER.info(
            f"[{_ts()}] [QUERY_ALL ✅ SINGLE-FLIGHT CACHE HIT] thread={tname} "
            f"name={_disp_name!r} wear=[{wear_min},{wear_max}] → "
            f"results={len(_cached_res)} errors_keys={list(_cached_err.keys())}")
        # 同步计算一下 plat_min 展示日志（对齐 caller 期望）
        plat_min = {}
        for r in _cached_res:
            try:
                rp = float(r["price"])
            except Exception:
                continue
            p = r.get("platform") or ""
            if not p: continue
            if p not in plat_min or rp < float(plat_min[p]):
                plat_min[p] = rp
        for p, lp in plat_min.items():
            _QUERYALL_LOGGER.info(
                f"[{_ts()}] [QUERY_ALL ✓ {p.upper()}] thread={tname} "
                f"_meta_tag={_meta_tag!r} → (CACHE) 最低价=¥{lp:.2f}")
        return _cached_res, _cached_err
    # CASE 2: 另一个线程已经在跑这个 key → 等它的 event，拿到结果后直接 return
    if _sf_state == "WAIT" and isinstance(_sf_x, threading.Event):
        _QUERYALL_LOGGER.info(
            f"[{_ts()}] [QUERY_ALL ⏳ SINGLE-FLIGHT MERGE] thread={tname} "
            f"_meta_tag={_meta_tag!r} key_prefix={_qa_key[:80]}... → "
            f"已有线程在跑，等待其结果共享")
        _merged_ok, _merged_res, _merged_err = _QUERYALL_SF.wait_for(_qa_key, _sf_x)
        if _merged_ok and isinstance(_merged_res, (list, tuple)):
            _QUERYALL_LOGGER.info(
                f"[{_ts()}] [QUERY_ALL ✅ SINGLE-FLIGHT DONE] thread={tname} "
                f"_meta_tag={_meta_tag!r} → 共享到 results={len(_merged_res)} "
                f"errors_keys={list(_merged_err or {})}")
            return list(_merged_res), dict(_merged_err or {})
        # waiter 超时/失败 → 自己兜底发请求（fallthrough CASE 3）
        _QUERYALL_LOGGER.warning(
            f"[{_ts()}] [QUERY_ALL ⚠ SINGLE-FLIGHT WAIT FAILED] thread={tname} "
            f"_meta_tag={_meta_tag!r} → ok={_merged_ok}，fallback 自己重新查一次")

    _QUERYALL_LOGGER.info(
        f"[{_ts()}] [QUERY_ALL ▶ START] thread={tname} "
        f"name={_disp_name!r} wear_grade={_wear_grade!r} "
        f"wear=[{wear_min},{wear_max}] price=[{price_min},{price_max}] "
        f"buff_gid={buff_gid!r} platforms={platforms}")

    tasks = {}
    # ---------- 【关键修复 F2】对 platforms 中每个平台检查必需参数 ----------
    def _gid_ok(v):
        if v is None: return False
        s = str(v).strip()
        if not s: return False
        try:
            i = int(s)
            return i > 0
        except (TypeError, ValueError):
            return False
    def _name_ok(v):
        return isinstance(v, str) and len(v.strip()) > 0

    if "buff" in platforms:
        if _gid_ok(buff_gid):
            tasks["buff"] = lambda: query_buff(
                buff_gid, wear_min, wear_max, price_min, price_max)
        else:
            pass  # errors 下方统一写
    if "c5" in platforms:
        if _name_ok(c5_name):
            tasks["c5"] = lambda: query_c5(
                c5_name, c5_app_id, wear_min, wear_max, price_min, price_max,
                full_list=c5_full_list)
        else:
            pass  # errors 下方统一写
    if "eco" in platforms:
        if _name_ok(eco_name):
            tasks["eco"] = lambda: query_eco(
                eco_name, c5_app_id, wear_min, wear_max, price_min, price_max)
        else:
            pass

    results = []
    errors = {}
    _cfg_summary = (
        f"(buff_gid={buff_gid!r}, c5_name={c5_name!r}, "
        f"wear=[{wear_min},{wear_max}])")
    if "buff" in platforms and "buff" not in tasks:
        errors["buff"] = (
            f"未发起查询：缺少合法的 buff_goods_id（商品ID） {_cfg_summary}。"
            f"常见原因：主 CSV 该皮肤行 buff_goods_id 为空 / GUI 材料元数据未透传 / "
            f"产出主 CSV 反向 lookup 未命中。可在 found_buff.py 手动查 goods_id 后回填主 CSV。")
        _QUERYALL_LOGGER.warning(
            f"[{_ts()}] [QUERY_ALL ⚠] thread={tname} _meta_tag={_meta_tag!r} → "
            f"BUFF 未发起查询！原因：buff_gid={buff_gid!r} 非法。")
    if "c5" in platforms and "c5" not in tasks:
        errors["c5"] = (
            f"未发起查询：缺少 c5_market_hash_name（Steam 英文完整市场名）{_cfg_summary}。"
            f"常见原因：GUI 卡片的『市场哈希名称』列为空 / 中文磨损档识别失败导致 "
            f"build_c5_market_hash_name 返回空。")
        _QUERYALL_LOGGER.warning(
            f"[{_ts()}] [QUERY_ALL ⚠] thread={tname} _meta_tag={_meta_tag!r} → "
            f"C5 未发起查询！原因：c5_name={c5_name!r} 非法。")
    if "eco" in platforms and "eco" not in tasks:
        errors["eco"] = (
            f"未发起查询：缺少 hash_name / c5_market_hash_name {_cfg_summary}。"
            f"与 C5 平台相同的参数。")

    # ---- 【DEBUG 日志】要并发执行的平台列表
    _QUERYALL_LOGGER.info(
        f"[{_ts()}] [QUERY_ALL] thread={tname} _meta_tag={_meta_tag!r} "
        f"→ ThreadPoolExecutor 将提交 tasks=平台={sorted(tasks.keys())}，"
        f" max_workers={len(tasks)}（各平台 1 线程，平台内部 query_buff 各自再 sleep 节流）")

    if tasks:
        with ThreadPoolExecutor(max_workers=len(tasks)) as pool:
            futures = {name: pool.submit(fn) for name, fn in tasks.items()}
            for name, fut in futures.items():
                t_p_start = time.perf_counter()
                try:
                    res = fut.result()
                    dt_ms = (time.perf_counter() - t_p_start) * 1000
                    results.extend(res)
                    # 各平台最低价统计
                    plat_prices = sorted({float(r["price"]) for r in res if r.get("price")})
                    min_p = plat_prices[0] if plat_prices else None
                    msg = (f"[{_ts()}] [QUERY_ALL ✓ {name.upper()}] thread={tname} "
                           f"name={_disp_name!r} wear=[{wear_min},{wear_max}] "
                           f"→ 返回 {len(res)} 条，耗时 {dt_ms:.0f}ms，"
                           f"最低价={('¥%.2f' % min_p) if min_p else 'None'}")
                    if min_p is not None:
                        _QUERYALL_LOGGER.info(msg)
                    else:
                        _QUERYALL_LOGGER.warning(msg + f" ⚠ 平台成功但无数据。errors={errors!r}")
                    # 【严格模式诊断传递】：平台成功发了请求但返回 0 条结果
                    #     → Buff 平台读 query_buff 留下的详细诊断（区分 B）服务端
                    #       按磨损筛空（None 正确）和 C）参数名错被 API 忽略两种情况）
                    #     → 其他平台保留原先那句简明诊断
                    if not res:
                        if name == "buff":
                            notes = _take_buff_diagnosis(tname, buff_gid)
                            if notes:
                                errors["buff_diag"] = "  \n".join(notes)
                            else:
                                errors.setdefault(
                                    "buff_no_results",
                                    f"BUFF 平台查询正常返回 0 条在售（严格模式无兜底）。"
                                    f"{_cfg_summary} 可能该子区间内真的无货。")
                        else:
                            errors.setdefault(
                                f"{name}_no_results",
                                f"{name.upper()} 平台查询正常返回 0 条在售。"
                                f"可能磨损范围过严 {_cfg_summary} 或该平台当前确实无卖家。")
                except Exception as e:
                    dt_ms = (time.perf_counter() - t_p_start) * 1000
                    errors[name] = str(e)
                    _QUERYALL_LOGGER.error(
                        f"[{_ts()}] [QUERY_ALL ✗ {name.upper()} FAIL] thread={tname} "
                        f"name={_disp_name!r} wear=[{wear_min},{wear_max}] "
                        f"→ 异常！耗时 {dt_ms:.0f}ms。err={e!r}。"
                        f" buff_gid={buff_gid!r}")

    results.sort(key=lambda x: x["price"])
    plat_count = {}
    plat_min = {}
    for r in results:
        p = r["platform"]
        plat_count[p] = plat_count.get(p, 0) + 1
        try:
            rp = float(r["price"])
        except (TypeError, ValueError):
            continue
        if p not in plat_min or rp < float(plat_min[p]):
            plat_min[p] = rp
    all_dt_ms = (time.perf_counter() - t0) * 1000
    # 格式化 plat_min 时对 v 一律 float() 保护，避免上游偶发字符串类型触发
    # "Unknown format code 'f' for object of type 'str'"，被 query_all except 吞掉后
    # 整条产出的三平台最低价都显示 None（用户感知就是『BUFF/ECO 栏全空』）。
    def _fmt(v):
        try: return f"¥{float(v):.2f}"
        except (TypeError, ValueError): return f"{v!r}"
    _plat_min_str = {k: _fmt(v) for k, v in plat_min.items()} if plat_min else {}
    _QUERYALL_LOGGER.info(
        f"[{_ts()}] [QUERY_ALL ✓ END] thread={tname} name={_disp_name!r} "
        f"wear=[{wear_min},{wear_max}] → 总订单 {len(results)} 条。"
        f"各平台条数={plat_count}，各平台最低价={_plat_min_str}，"
        f"errors={errors or {}}，总耗时 {all_dt_ms:.0f}ms。")

    # ============================================================
    # 【SingleFlight 关键缺口】：CASE3（我是第一只跑这个 key 的线程）查完后，
    # 必须把结果写进 60s TTL 缓存 + set() 事件唤醒所有 CASE2 正在 wait_for 的
    # 合并线程。否则 9 条 waiter 会在 180s 超时后 fallback 自己重发 → SingleFlight
    # 完全白写，ECO 6001 / C5 429 / Buff 429 全部依旧。
    #
    # 注意：CASE1（TTL CACHE HIT）/ CASE2（merge 成功/失败提前 return）都走到
    # 了上面的 return 分支，不会执行到这里；正好是"只有第一只线程写缓存"的单飞
    # 语义，不会重复 put。
    # ============================================================
    _QUERYALL_SF.put(
        _qa_key,
        list(results) if isinstance(results, (list, tuple)) else [],
        dict(errors) if isinstance(errors, dict) else {})

    return results, errors
