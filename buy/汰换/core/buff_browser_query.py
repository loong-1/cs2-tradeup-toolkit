"""Buff 浏览器查询：用 Playwright 打开带 min_paintwear/max_paintwear 的 URL，
让前端 JS 处理磨损筛选，拦截 sell_order API 响应数据。

核心思路（用户 2026-09-06 要求"从根本上改变 Buff 数据获取"）：
  - 直接 requests 调 sell_order API 时，min_paintwear/max_paintwear 参数可能
    因格式/精度/服务端识别等问题导致筛选失效 → 大量 None。
  - 改用 Playwright 打开真实网页 URL（如 goods/33906#tab=selling
    &min_paintwear=0.18&max_paintwear=0.2151），前端 JS 会把 fragment 里的
    磨损参数透传给 sell_order API，服务端返回的就是该磨损区间的在售条目。
  - 监听 page 的 response 事件，捕获 /market/goods/sell_order 的 JSON 响应，
    解析出 items 列表返回，完全等价于用户在浏览器里看到的结果。

【线程模型】Playwright 的 context 只能在创建它的线程里使用，而 query_buff
  在 ThreadPoolExecutor 的子线程中被调用。因此这里用一个**专用后台线程**
  运行 Playwright，所有查询通过 queue.Queue 把任务发给该线程，结果通过
  Event + 共享变量回传。这样保证所有 Playwright 操作都在同一线程。

依赖：playwright（pip install playwright && playwright install msedge）
      若未安装，query_buff 会自动回退到原 API 方式。
"""
from __future__ import annotations

import atexit
import logging
import queue
import random
import threading
import time
from typing import List, Optional

logger = logging.getLogger(__name__)

# 复用 buff_auth 的 Playwright user_data_dir（已登录态）
from core import buff_auth
from utils.config import (
    BUFF_BROWSER_QUERY_INTERVAL as _QUERY_INTERVAL,
    BUFF_BROWSER_QUERY_JITTER as _QUERY_JITTER,
)

# ============================================================
# 专用 Playwright 线程 + 任务队列
# ============================================================
_task_queue: "queue.Queue[tuple]" = queue.Queue()
_worker_thread: Optional[threading.Thread] = None
_worker_lock = threading.Lock()
_browser_ctx = None
_pw_instance = None
# 上次浏览器查询任务的 monotonic 时间戳：worker 线程串行执行，
# 任务间隔 = _QUERY_INTERVAL + random(0, _QUERY_JITTER)（默认 1.5 + 0~0.3s）
_BROWSER_QUERY_LAST_TS: float = 0.0
# 启动同步：worker 线程完成浏览器 launch（成功或失败）后 set。
# 解决多线程同时调用 _ensure_worker_thread 导致反复重启、about:blank 满天飞的问题。
_browser_startup_event = threading.Event()
_browser_startup_event.set()  # 初始为 set 状态（无启动进行中）
# 标记 worker 线程是否正在执行浏览器 launch（区分"启动中"和"已死待重启"）
# True = 线程正在 launch 浏览器，False = 线程在任务循环中
_launch_in_progress = False


def _playwright_available() -> bool:
    try:
        import playwright  # noqa: F401
        return True
    except ImportError:
        return False


# 缓存的"隐藏桌面" GUID（创建一次，复用）
_hidden_desktop_guid = None
_hidden_desktop_lock = threading.Lock()


def _get_hidden_desktop():
    """获取（懒创建）一个用于隐藏 Edge 的虚拟桌面 GUID。"""
    global _hidden_desktop_guid
    with _hidden_desktop_lock:
        if _hidden_desktop_guid is not None:
            return _hidden_desktop_guid
        try:
            from . import virtual_desktop
            _hidden_desktop_guid = virtual_desktop.create_desktop()
            logger.info("[BUFF-BROWSER] 已创建隐藏用虚拟桌面: %s",
                        _hidden_desktop_guid)

            # 退出时删除该桌面，避免累积空桌面
            def _cleanup():
                try:
                    if _hidden_desktop_guid is not None:
                        virtual_desktop.remove_desktop(_hidden_desktop_guid)
                except Exception:
                    pass

            atexit.register(_cleanup)
        except Exception as e:
            logger.warning("[BUFF-BROWSER] 创建虚拟桌面失败: %s", e)
            raise
        return _hidden_desktop_guid


def _find_edge_hwnds():
    """枚举所有标题含 'Microsoft Edge' 的顶层窗口 hwnd。"""
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    EnumWindowsProc = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    hwnds = []

    def _enum_cb(hwnd, _lparam):
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value
        if ("Microsoft Edge" in title
                or title.strip() == "Edge"
                or "about:blank" in title):
            hwnds.append(hwnd)
        return True

    user32.EnumWindows(EnumWindowsProc(_enum_cb), 0)
    return hwnds


def _hide_edge_window():
    """把 Playwright Edge 窗口移到独立虚拟桌面，用户完全看不到。

    方案：创建一个虚拟桌面 → 把 Edge 主窗口移过去。
    窗口仍"活着"（context 不关），但在当前桌面不可见、不抢焦点。
    守护线程持续把新出现的 Edge 窗口也移过去（防弹窗/captcha 弹窗）。
    """
    try:
        from . import virtual_desktop
        desktop_guid = _get_hidden_desktop()

        def _move_all_to_desktop():
            moved = 0
            for hwnd in _find_edge_hwnds():
                if virtual_desktop.move_window_to_desktop(hwnd, desktop_guid):
                    moved += 1
            return moved

        # 启动时 Edge 窗口可能还没渲染出来，轮询最多 5 秒
        for _ in range(10):
            if _move_all_to_desktop() > 0:
                break
            time.sleep(0.5)

        moved = _move_all_to_desktop()
        logger.info("[BUFF-BROWSER] 已将 %d 个 Edge 窗口移到隐藏虚拟桌面", moved)

        # 守护线程：持续把新出现的 Edge 窗口（如 captcha 弹窗）移过去
        def _daemon_loop():
            while _browser_ctx is not None:
                try:
                    _move_all_to_desktop()
                except Exception:
                    pass
                time.sleep(1.0)

        t = threading.Thread(
            target=_daemon_loop, name="BuffEdgeDesktopGuard", daemon=True)
        t.start()

    except Exception as _e:
        logger.warning("[BUFF-BROWSER] 虚拟桌面隐藏失败，回退到屏幕外方案: %s", _e)
        _hide_edge_window_offscreen()


def _hide_edge_window_offscreen():
    """兜底方案：SetWindowPos 把窗口移到 (-32000,-32000)。"""
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        SWP_NOSIZE = 0x0001
        SWP_NOZORDER = 0x0004
        SWP_NOACTIVATE = 0x0010
        SWP_SHOWWINDOW = 0x0040
        flags = SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_SHOWWINDOW
        for hwnd in _find_edge_hwnds():
            user32.SetWindowPos(hwnd, 0, -32000, -32000, 0, 0, flags)
        logger.info("[BUFF-BROWSER] 兜底：Edge 窗口已移到屏幕外")
    except Exception as _e:
        logger.debug("[BUFF-BROWSER] 屏幕外兜底也失败: %s", _e)


def _ensure_worker_thread():
    """确保 Playwright 专用线程已启动且浏览器上下文可用。

    用 _browser_startup_event 做启动同步：启动中的浏览器不会被并发线程
    重复杀掉重启（之前导致大量 about:blank + "Target page has been closed"）。
    """
    global _worker_thread, _browser_ctx, _browser_startup_event

    # 快速路径：线程活着 + 上下文有效
    if (_worker_thread is not None and _worker_thread.is_alive()
            and _browser_ctx is not None):
        return

    with _worker_lock:
        # 双重检查
        if (_worker_thread is not None and _worker_thread.is_alive()
                and _browser_ctx is not None):
            return

        # 有启动正在进行 → 等它完成，不要重启
        # 关键：用 _launch_in_progress 区分"真的在 launch"和"浏览器已死待重启"。
        #   - launch_in_progress=True → 线程正在执行 launch，等它完成
        #   - launch_in_progress=False 但 ctx=None → 浏览器已崩溃，立即重启（不等 60s）
        if (_worker_thread is not None and _worker_thread.is_alive()
                and not _browser_startup_event.is_set()
                and _launch_in_progress):
            logger.info("[BUFF-BROWSER] 检测到浏览器启动中，等待其就绪...")
            _browser_startup_event.wait(timeout=60)
            if _browser_ctx is not None:
                logger.info("[BUFF-BROWSER] 浏览器已就绪")
                return
            logger.warning("[BUFF-BROWSER] 启动等待超时或失败，准备重启")

        # 需要（重新）启动：先让旧线程退出
        if _worker_thread is not None and _worker_thread.is_alive():
            logger.warning("[BUFF-BROWSER] 浏览器上下文失效，重启 worker 线程...")
            try:
                _task_queue.put(None)  # 哨兵值
            except Exception:
                pass
            _worker_thread.join(timeout=5)

        _browser_ctx = None
        _browser_startup_event.clear()  # 标记"启动进行中"
        _worker_thread = threading.Thread(
            target=_browser_worker_loop,
            name="BuffBrowserWorker",
            daemon=True)
        _worker_thread.start()
        logger.info("[BUFF-BROWSER] 专用线程已启动，等待浏览器就绪...")

    # 锁外等待浏览器 launch 完成（避免长时间持锁阻塞其他无关操作）
    _browser_startup_event.wait(timeout=90)
    if _browser_ctx is None:
        raise RuntimeError("浏览器启动失败（请检查 Edge 是否已关闭、user_data_dir 是否被占用）")
    logger.info("[BUFF-BROWSER] 浏览器已就绪")


def _browser_worker_loop():
    """Playwright 专用线程主循环：从队列取任务 → 执行 → 回传结果。"""
    global _browser_ctx, _pw_instance, _browser_startup_event
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        logger.error("[BUFF-BROWSER] playwright 导入失败：%s", e)
        _browser_startup_event.set()  # 通知启动失败
        # 把所有待处理任务标记为失败
        while True:
            try:
                task = _task_queue.get_nowait()
            except queue.Empty:
                break
            _, result_dict, result_event = task
            result_dict["error"] = f"playwright 不可用：{e}"
            result_event.set()
        return

    try:
        global _launch_in_progress
        _launch_in_progress = True
        _pw_instance = sync_playwright().start()
        user_dir = str(buff_auth.PLAYWRIGHT_USER_DIR)
        buff_auth.PLAYWRIGHT_USER_DIR.mkdir(parents=True, exist_ok=True)
        _browser_ctx = _pw_instance.chromium.launch_persistent_context(
            user_data_dir=user_dir,
            headless=False,  # 有头模式：Buff 反爬会拦截 headless
            channel="msedge",  # 用户使用 Edge 浏览器
            args=[
                "--start-minimized",        # Edge 启动即最小化到任务栏
                "--disable-minimize-window",
            ],
        )
        _hide_edge_window()
        logger.info("[BUFF-BROWSER] Playwright 浏览器已启动并隐藏（persistent context）")
    except Exception as e:
        logger.error("[BUFF-BROWSER] Playwright 启动失败：%s", e)
        _browser_ctx = None
    finally:
        _launch_in_progress = False
        _browser_startup_event.set()  # 通知：launch 尝试完成（成功或失败）

    while True:
        try:
            task = _task_queue.get()
        except Exception:
            break
        if task is None:  # 哨兵值，退出线程
            break
        func, args, kwargs, result_dict, result_event = task
        try:
            if _browser_ctx is None:
                result_dict["error"] = "浏览器未启动"
            else:
                result_dict["result"] = func(*args, **kwargs)
        except Exception as e:
            logger.exception("[BUFF-BROWSER] 任务执行异常：%s", e)
            result_dict["error"] = str(e)
            # 浏览器/上下文已死 → 标记失效，下次 _ensure_worker_thread 会重启
            err_str = str(e).lower()
            if any(kw in err_str for kw in (
                    "has been closed", "target closed", "browser closed",
                    "context closed", "connection closed")):
                logger.warning("[BUFF-BROWSER] 检测到浏览器上下文已关闭，标记失效待重启")
                _browser_ctx = None
                _browser_startup_event.clear()
        result_event.set()

    # 清理
    if _browser_ctx is not None:
        try:
            _browser_ctx.close()
        except Exception:
            pass
        _browser_ctx = None
    if _pw_instance is not None:
        try:
            _pw_instance.stop()
        except Exception:
            pass
        _pw_instance = None


def _submit_to_worker(func, *args, **kwargs):
    """把任务提交给 Playwright 专用线程执行，等待结果返回。

    若返回错误为「浏览器未启动」或上下文已关闭，自动重启 worker 线程并重试一次。
    """
    global _browser_ctx, _browser_startup_event
    _ensure_worker_thread()
    result_dict: dict = {}
    result_event = threading.Event()
    _task_queue.put((func, args, kwargs, result_dict, result_event))
    # 等待结果（最多 120 秒）
    if not result_event.wait(timeout=120):
        raise RuntimeError("Buff 浏览器查询超时（120s）")

    err = result_dict.get("error")
    need_restart = (
        err == "浏览器未启动"
        or (err and any(kw in str(err).lower() for kw in (
            "has been closed", "target closed", "browser closed",
            "context closed", "connection closed")))
    )
    if need_restart:
        logger.warning("[BUFF-BROWSER] 浏览器不可用（%s），尝试重启后重试...", err)
        _browser_ctx = None
        _browser_startup_event.clear()
        _ensure_worker_thread()
        result_dict2: dict = {}
        result_event2 = threading.Event()
        _task_queue.put((func, args, kwargs, result_dict2, result_event2))
        if not result_event2.wait(timeout=120):
            raise RuntimeError("Buff 浏览器查询超时（120s）")
        if "error" in result_dict2:
            raise RuntimeError(result_dict2["error"])
        return result_dict2.get("result")
    if "error" in result_dict:
        raise RuntimeError(result_dict["error"])
    return result_dict.get("result")


def _build_url(goods_id, page_num, min_paintwear, max_paintwear) -> str:
    """构造 Buff 在售 tab URL，磨损参数放在 fragment 里（前端 JS 会透传给 API）。"""
    base = f"https://buff.163.com/goods/{goods_id}?from=market#tab=selling&page_num={page_num}"
    parts = []
    if min_paintwear is not None:
        parts.append(f"min_paintwear={min_paintwear}")
    if max_paintwear is not None:
        parts.append(f"max_paintwear={max_paintwear}")
    if parts:
        base += "&" + "&".join(parts)
    return base


def _infer_official_tier(wear_min, wear_max) -> tuple:
    """根据 wear_min/wear_max 推断所属官方磨损档位范围。

    用于窄区间 0 条时兜底：放宽到整个档位范围再 filter。
    返回 (tier_min, tier_max)，推断不出返回 (None, None)。
    """
    if wear_min is None and wear_max is None:
        return None, None
    # 用 wear_min 作为档位判断依据（材料/产出的 wear_min 通常落在档位内）
    ref = wear_min if wear_min is not None else wear_max
    try:
        ref = float(ref)
    except (TypeError, ValueError):
        return None, None
    if ref < 0.07:
        return 0.0, 0.07
    if ref < 0.15:
        return 0.07, 0.15
    if ref < 0.38:
        return 0.15, 0.38
    if ref < 0.45:
        return 0.38, 0.45
    return 0.45, 1.0


def _do_query(goods_id, wear_min, wear_max, price_min, price_max, max_pages):
    """在 Playwright 线程内执行实际查询（由 _submit_to_worker 调用）。"""
    from core.price_fetcher import _buff_paintwear_str

    s_min = _buff_paintwear_str(wear_min) if wear_min is not None else None
    s_max = _buff_paintwear_str(wear_max) if wear_max is not None else None

    # 带磨损过滤时服务端已筛选，第 1 页（10 条）就是价格最低且符合区间的，
    # 无需翻页——减少请求量，降低触发安全验证的概率。
    has_wear_filter = (s_min is not None) or (s_max is not None)
    if has_wear_filter:
        max_pages = 1

    all_items = []
    # 记录最后一次 API 响应的 code/error，用于诊断"0 条"到底是登录过期还是真没货
    last_api_code = None
    last_api_error = None
    last_api_items = 0
    login_required = False

    # ---- 查询任务间隔节流 ----
    # 全局 monotonic 时间戳控制：**每个 URL 请求之间**都必须间隔
    # 1.5s 基础 + 0~2.0s 随机。之前只在 _do_query 开头节流一次，翻页/重试
    # 的连续请求无间隔 → 瞬间打爆 Buff 触发安全验证。
    def _throttle(label=""):
        global _BROWSER_QUERY_LAST_TS
        _dt = time.monotonic() - _BROWSER_QUERY_LAST_TS
        _wait = (_QUERY_INTERVAL + random.uniform(0.0, _QUERY_JITTER)) - _dt
        if _wait > 0:
            logger.info(
                f"[BUFF-BROWSER] ⏳ 节流 {label}：距上次查询 {_dt:.2f}s "
                f"< 目标间隔，等待 {_wait:.2f}s ...")
            time.sleep(_wait)
        _BROWSER_QUERY_LAST_TS = time.monotonic()

    for page_num in range(1, max_pages + 1):
        _throttle(label=f"page{page_num}")
        url = _build_url(goods_id, page_num, s_min, s_max)
        logger.info(
            f"[BUFF-BROWSER] ▶ 打开 URL goods_id={goods_id} page={page_num} "
            f"wear=[{wear_min},{wear_max}] url={url}")
        try:
            page = _browser_ctx.new_page()
        except Exception as e:
            logger.error("[BUFF-BROWSER] 创建 page 失败：%s", e)
            # 必须向上抛（不能 return None）：
            #   - closed 类错误 → _submit_to_worker 检测后自动重启浏览器并重试一次
            #     （浏览器窗口被手动关闭 / crash 后自愈）；
            #   - return None 会被 query_buff_via_browser 误报成『登录态已过期』，
            #     且吞掉异常导致重启机制永远不触发（10 材料全挂的根因）。
            raise

        captured = []
        # 额外记录非 200 的 sell_order 响应（如 401 登录过期），之前只抓 200 导致
        # 登录过期被静默吞掉、最终返回 [] 被误判为"该磨损区间无货"。
        non200_responses = []
        captured_urls = []

        def _on_response(resp):
            try:
                rurl = resp.url
                if "/market/goods/sell_order" not in rurl:
                    return
                captured_urls.append((resp.status, rurl[:120]))
                if resp.status == 200:
                    j = resp.json()
                    captured.append(j)
                    # 记录每条 sell_order 响应的关键信息便于排查
                    try:
                        ic = len(j.get("data", {}).get("items", []) or [])
                        tc = j.get("data", {}).get("total_count", "?")
                        logger.info(
                            f"[BUFF-BROWSER]   ← sell_order captured status=200 "
                            f"items={ic} total_count={tc}")
                    except Exception:
                        pass
                else:
                    non200_responses.append((resp.status, rurl))
                    logger.warning(
                        f"[BUFF-BROWSER]   ← sell_order non-200 status={resp.status}")
            except Exception as e:
                # 之前这里 bare except 吞掉了所有错误，导致 resp.json() 失败时静默丢失响应
                logger.warning(
                    f"[BUFF-BROWSER]   ← sell_order 响应解析异常: {e!r} "
                    f"url={resp.url[:120]} status={resp.status}")

        page.on("response", _on_response)

        # ============================================================
        # 导航 + 重试：Buff 偶尔会卡在 about:blank（CF 验证 / 网络抖动 /
        # 登录态过期跳转）。最多重试 3 次，每次检查实际 URL 是否到达 Buff。
        # ============================================================
        nav_ok = False
        for attempt in range(1, 4):
            try:
                # 先试 networkidle（等网络空闲，前端 JS 已发起 sell_order），
                # 超时则回退 domcontentloaded（避免某些页面永远不 networkidle）
                try:
                    page.goto(url, wait_until="networkidle", timeout=20_000)
                except Exception:
                    page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                # 检查是否真的导航到了 Buff 页面（不是 about:blank / 登录页 / CF）
                cur_url = page.url or ""
                if "buff.163.com" in cur_url and "about:blank" not in cur_url:
                    nav_ok = True
                    break
                logger.warning(
                    f"[BUFF-BROWSER] 第 {attempt} 次导航后 URL={cur_url!r} "
                    f"非 Buff 页面，重试...")
                page.wait_for_timeout(1500)
            except Exception as e:
                logger.warning(
                    f"[BUFF-BROWSER] 第 {attempt} 次导航异常 goods_id={goods_id} "
                    f"page={page_num}: {e}，重试...")
                page.wait_for_timeout(1500)

        if not nav_ok:
            logger.warning(
                f"[BUFF-BROWSER] 导航失败（3 次重试仍未到 Buff 页面）"
                f"goods_id={goods_id} page={page_num}")
            try:
                page.close()
            except Exception:
                pass
            break

        # 等待 sell_order 响应（前端 JS 解析 fragment 后发起）
        try:
            deadline = time.time() + 20
            while time.time() < deadline and not captured:
                page.wait_for_timeout(200)
            # 兜底：如果 20s 内没捕获到 sell_order 响应，可能 fragment 没触发 tab 切换，
            # 主动点击「在售」tab 再等 8s
            if not captured:
                logger.info(
                    f"[BUFF-BROWSER] ⚠ 未捕获 sell_order，尝试点击「在售」tab "
                    f"goods_id={goods_id} page={page_num}")
                try:
                    # Buff 在售 tab 的选择器，尝试多种
                    for sel in [
                        'a[href*="tab=selling"]',
                        '.market-tabs .tab-sell',
                        'li[data-key="selling"]',
                        'a:has-text("在售")',
                    ]:
                        try:
                            el = page.query_selector(sel)
                            if el:
                                el.click()
                                break
                        except Exception:
                            continue
                except Exception:
                    pass
                deadline2 = time.time() + 8
                while time.time() < deadline2 and not captured:
                    page.wait_for_timeout(200)
        except Exception as e:
            logger.warning("[BUFF-BROWSER] 等待响应异常 goods_id=%s page=%s：%s",
                           goods_id, page_num, e)
        finally:
            try:
                page.close()
            except Exception:
                pass

        if not captured:
            if non200_responses:
                logger.warning(
                    f"[BUFF-BROWSER] sell_order 响应非 200 goods_id={goods_id} "
                    f"page={page_num}: {non200_responses[:3]}")
            if captured_urls:
                logger.warning(
                    f"[BUFF-BROWSER] 捕获到 sell_order URL 但无 200 响应 goods_id={goods_id} "
                    f"page={page_num}: {captured_urls[:5]}")
            logger.warning(
                f"[BUFF-BROWSER] 未捕获到 sell_order 响应 goods_id={goods_id} "
                f"page={page_num} wear=[{wear_min},{wear_max}]")
            break

        # 取最后一条 sell_order 响应（前端可能发多次，最后一次是最终筛选结果）
        data = captured[-1]
        last_api_code = data.get("code")
        last_api_error = data.get("error")
        items = data.get("data", {}).get("items", []) or []
        last_api_items = len(items)
        total_count = data.get("data", {}).get("total_count", 0)

        # ============================================================
        # 【登录过期识别】Buff 返回 code="Login Required" / error="请先登录"
        #   → 说明浏览器持久化 cookie 已过期。此时必须返回 None，让 query_buff
        #   回退到 API 路径（API 也可能失败但至少会报"请先登录"让用户知道要刷新登录态）。
        #   之前直接 break 返回 [] → 被 query_all 当成"该区间无货"，备注显示
        #   "BUFF 平台查询正常返回 0 条在售"，完全误导用户。
        # ============================================================
        if last_api_code != "OK":
            code_lower = str(last_api_code or "").lower()
            err_lower = str(last_api_error or "").lower()
            if ("login" in code_lower or "login" in err_lower
                    or "登录" in str(last_api_error or "")
                    or "未登录" in str(last_api_error or "")):
                login_required = True
                logger.warning(
                    f"[BUFF-BROWSER] ❌ 浏览器登录态已过期！goods_id={goods_id} "
                    f"code={last_api_code!r} error={last_api_error!r}。"
                    f"返回 None 触发 API 回退。")
                break
            # 【Captcha 识别 2026-09-11】"Captcha Validate Required" /
            # "访问异常, 请完成安全验证" 是平台级风控错误，不是"无在售"——
            # 之前被当 0 条静默返回，材料表误显示"无货"。抛 RuntimeError
            # 让调用方按平台错误处理（不缓存、GUI 显示明确原因）。
            if ("captcha" in code_lower or "captcha" in err_lower
                    or "安全验证" in str(last_api_error or "")
                    or "访问异常" in str(last_api_error or "")):
                logger.warning(
                    f"[BUFF-BROWSER] ❌ 触发安全验证！goods_id={goods_id} "
                    f"code={last_api_code!r} error={last_api_error!r}")
                raise RuntimeError(
                    f"Buff 触发安全验证 (goods_id={goods_id}, "
                    f"code={last_api_code!r})。请在 GUI「Buff 登录态管理」"
                    f"打开浏览器手动完成安全验证后重试。")
            logger.warning(
                f"[BUFF-BROWSER] API 返回非 OK goods_id={goods_id} "
                f"code={last_api_code!r} error={last_api_error!r}")
            break

        logger.info(
            f"[BUFF-BROWSER] ✓ 捕获 sell_order goods_id={goods_id} page={page_num} "
            f"wear=[{wear_min},{wear_max}] items={last_api_items} "
            f"total_count={total_count}")

        if not items:
            break
        all_items.extend(items)
        # 如果本页不满 page_size，说明没有更多页了
        if len(items) < 50 or len(all_items) >= (total_count or 0):
            break

    # ============================================================
    # 【登录过期 → 返回 None】让 query_buff 走 API 回退，给用户明确的"请先登录"提示
    # ============================================================
    if login_required:
        return None

    # ============================================================
    # 【0 条诊断 + 无磨损重试】如果带磨损筛选返回 0 条，再查一次不带磨损的，
    #   区分两种情况：
    #     A) 不带磨损也 0 条 → 该 goods_id 真的无在售 / 登录问题 / 页面没加载出卖货 tab
    #     B) 不带磨损有货 → 磨损区间太窄或 URL fragment 磨损筛选没生效（前端没透传）
    #   这对定位"材料 Buff 全 None"至关重要——之前完全无法区分。
    # ============================================================
    if not all_items and (s_min is not None or s_max is not None):
        logger.info(
            f"[BUFF-BROWSER] 🔍 带磨损筛选返回 0 条 goods_id={goods_id} "
            f"wear=[{wear_min},{wear_max}]，重试不带磨损以区分『区间太窄』vs『真无货』...")
        retry_items = []
        for page_num in range(1, max_pages + 1):
            _throttle(label=f"retry-page{page_num}")
            retry_url = _build_url(goods_id, page_num, None, None)
            try:
                rpage = _browser_ctx.new_page()
            except Exception as e:
                logger.error("[BUFF-BROWSER] 重试创建 page 失败：%s", e)
                # 同主循环：closed 类错误向上抛触发自动重启重试
                raise
            rcaptured = []
            def _on_resp2(resp):
                try:
                    if "/market/goods/sell_order" in resp.url and resp.status == 200:
                        rcaptured.append(resp.json())
                except Exception:
                    pass
            rpage.on("response", _on_resp2)
            rnav_ok = False
            for attempt in range(1, 3):
                try:
                    rpage.goto(retry_url, wait_until="domcontentloaded", timeout=30_000)
                    if "buff.163.com" in (rpage.url or ""):
                        rnav_ok = True
                        break
                    rpage.wait_for_timeout(1500)
                except Exception:
                    rpage.wait_for_timeout(1500)
            if rnav_ok:
                try:
                    deadline = time.time() + 12
                    while time.time() < deadline and not rcaptured:
                        rpage.wait_for_timeout(200)
                except Exception:
                    pass
            try:
                rpage.close()
            except Exception:
                pass
            if rcaptured:
                rdata = rcaptured[-1]
                if rdata.get("code") == "OK":
                    ritems = rdata.get("data", {}).get("items", []) or []
                    retry_items.extend(ritems)
                    rtotal = rdata.get("data", {}).get("total_count", 0)
                    logger.info(
                        f"[BUFF-BROWSER] 🔍 不带磨损重试 goods_id={goods_id} "
                        f"page={page_num} items={len(ritems)} total_count={rtotal}")
                    if not ritems or len(ritems) < 50:
                        break
                else:
                    rcode = str(rdata.get("code") or "")
                    rerror = str(rdata.get("error") or "")
                    logger.warning(
                        f"[BUFF-BROWSER] 不带磨损重试 API 非 OK: code={rcode!r} "
                        f"error={rerror!r}")
                    # 【Captcha 识别 2026-09-11】同主循环：安全验证是平台级
                    # 错误，抛 RuntimeError 而非当"无在售"静默返回 0 条。
                    rcl, rel = rcode.lower(), rerror.lower()
                    if ("captcha" in rcl or "captcha" in rel
                            or "安全验证" in rerror or "访问异常" in rerror):
                        raise RuntimeError(
                            f"Buff 触发安全验证 (goods_id={goods_id}, "
                            f"code={rcode!r})。请在 GUI「Buff 登录态管理」"
                            f"打开浏览器手动完成安全验证后重试。")
                    break
            else:
                logger.warning("[BUFF-BROWSER] 不带磨损重试未捕获到 sell_order 响应")
                break

        if retry_items:
            logger.info(
                f"[BUFF-BROWSER] 📌 诊断结论 goods_id={goods_id}: "
                f"带磨损 wear=[{wear_min},{wear_max}] 返回 0 条，"
                f"但不带磨损返回 {len(retry_items)} 条 → "
                f"『磨损区间太窄』或『URL fragment 磨损筛选未生效』。"
                f"将用不带磨损的结果做客户端 filter_orders 二次过滤。")
            all_items = retry_items
        else:
            logger.warning(
                f"[BUFF-BROWSER] 📌 诊断结论 goods_id={goods_id}: "
                f"带磨损 wear=[{wear_min},{wear_max}] 返回 0 条，"
                f"不带磨损也返回 0 条 → 该 goods_id 真的无在售 / 页面未加载出卖货 tab。"
                f"last_api_code={last_api_code!r} last_api_error={last_api_error!r}")

    if not all_items:
        return []

    # 客户端二次过滤（磨损 + 价格区间）
    from vendor.found_buff import filter_orders
    filtered = filter_orders(all_items, wear_min, wear_max, price_min, price_max)

    # ============================================================
    # 【窄区间 0 条 → 官方档位兜底】
    #   用户材料常填极窄区间（如 BS [0.45,0.49]），Buff 该精确子区间可能真无在售。
    #   此时若不带磨损重试拿到了同档位条目（说明 goods_id 没指错档），放宽到
    #   该磨损档官方范围（如 BS=[0.45,1.0]）再 filter 一次，至少给出该档最低价，
    #   避免材料 Buff 列全 None。同时记录磨损分布便于排查 goods_id 是否错档。
    # ============================================================
    if not filtered and (wear_min is not None or wear_max is not None):
        # 统计 all_items 的磨损分布，判断 goods_id 是否指向正确档位
        wears = []
        for it in all_items:
            try:
                pw = float(it.get("asset_info", {}).get("paintwear", 0))
                wears.append(pw)
            except (TypeError, ValueError):
                pass
        wears.sort()
        n = len(wears)
        w_min = wears[0] if wears else None
        w_max = wears[-1] if wears else None
        logger.info(
            f"[BUFF-BROWSER] 🔍 窄区间 wear=[{wear_min},{wear_max}] 过滤后 0 条，"
            f"all_items 磨损分布: n={n} range=[{w_min:.6f},{w_max:.6f}]"
            if w_min is not None else
            f"[BUFF-BROWSER] 🔍 窄区间过滤后 0 条，all_items 无磨损数据 n={n}")

        # 推断官方档位范围（按 wear_min 所在档位）
        tier_min, tier_max = _infer_official_tier(wear_min, wear_max)
        if tier_min is not None and tier_max is not None:
            tier_filtered = filter_orders(
                all_items, tier_min, tier_max, price_min, price_max)
            if tier_filtered:
                logger.info(
                    f"[BUFF-BROWSER] 📌 官方档位兜底 goods_id={goods_id}: "
                    f"窄区间 [{wear_min},{wear_max}] 0 条，"
                    f"放宽到档位 [{tier_min:.4f},{tier_max:.4f}] 命中 {len(tier_filtered)} 条。"
                    f"（取该档最低价，非用户精确区间）")
                filtered = tier_filtered
            else:
                logger.warning(
                    f"[BUFF-BROWSER] 📌 官方档位兜底也 0 条 goods_id={goods_id}: "
                    f"档位 [{tier_min:.4f},{tier_max:.4f}] 内无在售。"
                    f"all_items 磨损范围 [{w_min:.6f},{w_max:.6f}] → "
                    f"高度怀疑 goods_id 指向错误磨损档（例如材料是 BS 但 goods_id 是 FN）。")

    # 归一化
    results = []
    for item in filtered:
        asset = item.get("asset_info", {})
        try:
            wear = float(asset.get("paintwear", 0))
        except (TypeError, ValueError):
            wear = 0.0
        try:
            price = float(item.get("price", 0))
        except (TypeError, ValueError):
            price = 0.0
        results.append({
            "platform": "buff",
            "order_id": str(item.get("id", "")),
            "goods_id": str(item.get("goods_id", goods_id)),
            "price": price,
            "wear": wear,
            "wear_name": asset.get("wear_name", ""),
            "paintseed": str(asset.get("paintseed", "")),
            "assetid": str(item.get("assetid", "")),
        })
    return results


def query_buff_via_browser(goods_id, wear_min=None, wear_max=None,
                           price_min=None, price_max=None,
                           max_pages=2) -> Optional[List[dict]]:
    """用 Playwright 浏览器查询 Buff 在售订单（带磨损筛选）。

    :param goods_id: Buff 商品 ID
    :param wear_min: 最低磨损（None 表示不限）
    :param wear_max: 最高磨损（None 表示不限）
    :param price_min: 最低价格（None 表示不限，客户端过滤）
    :param price_max: 最高价格（None 表示不限，客户端过滤）
    :param max_pages: 最多查询页数
    :return: 归一化后的订单列表。
             登录过期 / 浏览器不可用 / 查询异常 → 抛 RuntimeError（带具体原因）。
             Playwright 未安装 → 返回 None（调用方应提示安装）。
    """
    if not _playwright_available():
        return None
    result = _submit_to_worker(
        _do_query, goods_id, wear_min, wear_max,
        price_min, price_max, max_pages)
    # _do_query 返回 None = 浏览器登录态过期（sell_order 返回 Login Required）
    if result is None:
        raise RuntimeError(
            f"Buff 浏览器登录态已过期 (goods_id={goods_id})。"
            f"请在 GUI 点「Buff 登录态管理」刷新登录后重试。")
    return result


# ============================================================
# 【网页版购买】2026-09-11：requests + JSON 缓存 session 购买实测返回
# "Login Required / 请先登录"（缓存是过期快照；查询走 Playwright 持久化
# profile 登录态所以正常，两条通道不同源）。购买改为在 Playwright 浏览器
# 页面上下文里发 fetch：cookie 自动携带、浏览器指纹真实、csrf 用活 cookie。
#
# 【支付方式】pay_method=100（"BUFF可用资金"）必须配合
# passback_params={"included_sub_methods":[96]}（子方式 96=BUFF余额·银行卡
# 已授权）。只传 100 不传 passback_params 时服务端默认落到"CS2 待结算余额"
# （已关闭）→ 报"CS2 待结算余额支付功能暂时关闭"。依据：main.js 购买弹窗
# payload 构造（pay_method + passback_params 成对出现）与支付页
# data-method JSON（included_sub_methods:[96], enough=true, 余额 ¥37.74）。
# ============================================================
_PAY_PASSBACK_PARAMS = '{"included_sub_methods":[96]}'


def _do_buy(goods_id, sell_order_id, price, steam_id, pay_method):
    """在 Playwright 持久化浏览器里发购买 POST。worker 线程内执行。

    返回 Buff buy API 的 JSON dict；网络/浏览器层失败抛 RuntimeError。
    """
    global _browser_ctx
    if _browser_ctx is None:
        raise RuntimeError("浏览器未启动")
    # 从持久化 context 取最新 csrf_token（活 cookie）
    csrf = ""
    try:
        for ck in _browser_ctx.cookies(["https://buff.163.com"]):
            if ck.get("name") == "csrf_token":
                csrf = ck.get("value", "")
                break
    except Exception as e:
        logger.warning("[BUFF-BUY] 读取 csrf_token cookie 失败：%s", e)
    if not csrf:
        raise RuntimeError(
            "Buff 浏览器中无 csrf_token cookie——登录态可能已过期，"
            "请在 GUI「Buff 登录态管理」重新扫码登录后重试。")

    page = _browser_ctx.new_page()
    try:
        page.goto(f"https://buff.163.com/goods/{goods_id}?from=market",
                  wait_until="domcontentloaded", timeout=30_000)
        result = page.evaluate(
            """async ([gid, sellOrder, priceStr, steamId, csrfTok, payM, passback]) => {
                const resp = await fetch('/api/market/goods/buy', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'X-CSRFToken': csrfTok,
                        'X-Requested-With': 'XMLHttpRequest',
                    },
                    credentials: 'include',
                    body: JSON.stringify({
                        game: 'csgo',
                        goods_id: String(gid),
                        sell_order_id: String(sellOrder),
                        price: String(priceStr),
                        pay_method: Number(payM),
                        passback_params: String(passback || ''),
                        allow_tradable_cooldown: 0,
                        token: '',
                        cdkey_id: '',
                        password: '',
                        hide_non_epay: true,
                        steamid: String(steamId),
                    }),
                });
                return await resp.json();
            }""",
            [str(goods_id), str(sell_order_id), str(price),
             str(steam_id), csrf, int(pay_method), _PAY_PASSBACK_PARAMS])
        return result
    finally:
        try:
            page.close()
        except Exception:
            pass


def buy_via_browser(goods_id, sell_order_id, price, steam_id, pay_method=100):
    """Playwright 活登录态购买（与查询共用同一持久化浏览器/worker 线程）。

    :return: Buff buy API 的 JSON dict（code=='OK' 即成功）。
             Playwright 未安装 → None（调用方回退 requests）。
    """
    if not _playwright_available():
        return None
    return _submit_to_worker(
        _do_buy, goods_id, sell_order_id, price, steam_id, pay_method)


def close_browser():
    """关闭全局浏览器（程序退出时调用）。"""
    global _worker_thread
    # 发送哨兵值让线程退出
    try:
        _task_queue.put(None)
    except Exception:
        pass
    _worker_thread = None


# 程序退出时自动关闭浏览器，避免残留 chrome 进程
atexit.register(close_browser)
