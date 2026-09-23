"""C5 网页版登录态自动获取与缓存管理（架构同 buff_auth）。

优先级：
    1) 内存缓存（本次启动已拿到就不再反复读）
    2) 本地 JSON 缓存 ``data/.c5_auth/c5_web_config.json``
    3) 直接从本机 Chrome/Edge 的 Cookies SQLite 解密出 c5game.com 全部 cookie
       （token=NC5_accessToken, device_id=NC5_deviceId, traffic_tag=当前毫秒时间戳）
       ⚠ 局限：浏览器运行中会独占锁定 Cookies DB（Errno 13）；
       新版 Edge/Chrome 的 app-bound 加密 cookie 也可能解不开 → 失败走 4/5 级
    4) 手动导入：GUI 粘贴「Copy as cURL (cmd)」文本或纯 cookie 串
    5) Playwright 弹出浏览器登录（最可靠）：独立 user_data_dir，登录一次长期复用，
       直接从 ctx.cookies() 抓取，不受文件锁 / app-bound 加密影响

依赖说明：
    浏览器解密复用 buff_auth 里的通用函数（候选路径扫描 / DPAPI / AES-GCM v20），
    需要 pycryptodome + pywin32（项目 requirements 已含）。
    Playwright 登录需要 pip install playwright + playwright install chromium。
"""
from __future__ import annotations

import json
import re
import shutil
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, Optional

from core.buff_auth import (
    _candidate_cookie_files,
    _decrypt_cookie_win,
)

# ---------- 路径常量 ----------
_PROJECT_HOME = Path(__file__).resolve().parent.parent / "data" / ".c5_auth"
try:
    _PROJECT_HOME.mkdir(parents=True, exist_ok=True)
    APP_HOME = _PROJECT_HOME
except Exception:
    APP_HOME = Path(__file__).resolve().parent.parent / "data"

SESSION_JSON = APP_HOME / "c5_web_config.json"

C5_HOST_KEY = "c5game.com"
C5_REQUIRED_COOKIES = ("NC5_accessToken", "NC5_deviceId")


# ---------- 数据结构 ----------

@dataclass
class C5WebAuth:
    cookie: str        # 完整 cookie 串（含 WAF 的 ssxmod_itna 等）
    token: str         # NC5_accessToken 的值（x-access-token 头）
    device_id: str     # NC5_deviceId 的值（x-device-id 头）
    traffic_tag: str   # x-traffic-tag 头（毫秒时间戳）
    source: str        # "json" | "edge" | "chrome" | "manual"
    saved_at: float

    @property
    def age_hours(self) -> float:
        return (time.time() - self.saved_at) / 3600


# ---------- 内存缓存 ----------

_MEM_CACHE: Optional[C5WebAuth] = None


# ---------- JSON 读写 ----------

def _save_json(auth: C5WebAuth) -> None:
    tmp = SESSION_JSON.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(asdict(auth), f, ensure_ascii=False, indent=2)
    import os
    os.replace(tmp, SESSION_JSON)


def _load_json() -> Optional[C5WebAuth]:
    if not SESSION_JSON.exists():
        return None
    try:
        with open(SESSION_JSON, "r", encoding="utf-8") as f:
            data = json.load(f)
        need = ("cookie", "token", "device_id", "traffic_tag", "source", "saved_at")
        if not all(k in data for k in need):
            return None
        auth = C5WebAuth(**data)
        if not auth.cookie or not auth.token:
            return None
        return auth
    except Exception:
        return None


def clear_all() -> None:
    """清除本地缓存，强制下一次重新导入。"""
    global _MEM_CACHE
    _MEM_CACHE = None
    try:
        if SESSION_JSON.exists():
            SESSION_JSON.unlink()
    except Exception:
        pass


# ---------- 第 3 级：Chrome / Edge 本地 Cookie DB 解密 ----------

def _read_c5_cookies_from_db(db_path: Path, user_data_root: Path) -> Dict[str, str]:
    """复制 Cookies DB 到临时目录再打开，解密 c5game.com 域的全部 cookie。"""
    result: Dict[str, str] = {}
    local_state_path = user_data_root / "Local State"
    import sqlite3
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td) / "Cookies"
        try:
            shutil.copyfile(db_path, tmp)
        except Exception:
            return result
        try:
            conn = sqlite3.connect(str(tmp))
            try:
                cur = conn.execute(
                    "SELECT host_key, name, encrypted_value FROM cookies "
                    "WHERE host_key LIKE ?",
                    (f"%{C5_HOST_KEY}%",),
                )
                for host_key, name, enc in cur.fetchall():
                    if not name or name in result:
                        continue
                    if isinstance(enc, str):
                        enc = enc.encode("latin-1", errors="ignore")
                    dec = _decrypt_cookie_win(bytes(enc), local_state_path)
                    if dec:
                        result[name] = dec
            finally:
                conn.close()
        except Exception:
            return result
    return result


def _build_from_cookie_dict(d: Dict[str, str], source: str) -> Optional[C5WebAuth]:
    """用解密出的 cookie dict 构造 C5WebAuth（缺关键 cookie 返回 None）。"""
    token = d.get("NC5_accessToken")
    device_id = d.get("NC5_deviceId", "")
    if not token:
        return None
    cookie_str = "; ".join(f"{k}={v}" for k, v in d.items() if v)
    return C5WebAuth(
        cookie=cookie_str,
        token=token,
        device_id=device_id,
        traffic_tag=str(int(time.time() * 1000)),
        source=source,
        saved_at=time.time(),
    )


def _try_browsers() -> Optional[C5WebAuth]:
    for name, db, root in _candidate_cookie_files():
        d = _read_c5_cookies_from_db(db, root)
        auth = _build_from_cookie_dict(d, name)
        if auth:
            _save_json(auth)
            return auth
    return None


# ---------- 第 4 级：手动导入（curl 文本 / 纯 cookie 串） ----------

def _clean_cmd_escape(s: str) -> str:
    """去掉 cmd 转义符：^x -> x（Copy as cURL (cmd) 会产生）。"""
    out, i = [], 0
    while i < len(s):
        if s[i] == "^" and i + 1 < len(s):
            out.append(s[i + 1])
            i += 2
        else:
            out.append(s[i])
            i += 1
    return "".join(out)


def import_curl_text(text: str) -> C5WebAuth:
    """从粘贴的文本导入凭证。自动识别两种格式：

    1. 「Copy as cURL (cmd)」完整文本：提取 -b cookie + x-access-token/
       x-device-id/x-traffic-tag 头
    2. 纯 cookie 串（name=value; name2=value2）：NC5_accessToken /
       NC5_deviceId 从中提取

    :raises ValueError: 格式不对或缺少关键凭证
    """
    text = text.strip()
    if not text:
        raise ValueError("内容为空")

    cookie = ""
    token = ""
    device_id = ""
    traffic_tag = ""

    if "-b " in text or "x-access-token" in text.lower() or text.startswith("curl"):
        # curl 文本格式
        m = re.search(r'-b \^"(.*?)"\^', text, re.S) or \
            re.search(r'-b "(.*?)"', text, re.S) or \
            re.search(r"--cookie ['\"](.*?)['\"]", text, re.S)
        if m:
            cookie = _clean_cmd_escape(m.group(1))
        tok_m = re.search(r'x-access-token:\s*([\w.\-]+)', text, re.I)
        if tok_m:
            token = tok_m.group(1)
        dev_m = re.search(r'x-device-id:\s*([\w.\-]+)', text, re.I)
        if dev_m:
            device_id = dev_m.group(1)
        tt_m = re.search(r'x-traffic-tag:\s*(\d+)', text, re.I)
        if tt_m:
            traffic_tag = tt_m.group(1)
    else:
        cookie = text

    # 从 cookie 串里补齐缺失字段
    d = _parse_cookie_str(cookie)
    if not token:
        token = d.get("nc5_accesstoken", "")
    if not device_id:
        device_id = d.get("nc5_deviceid", "")
    if not traffic_tag:
        traffic_tag = str(int(time.time() * 1000))

    if not cookie:
        raise ValueError("未识别出 cookie（既不是 curl 文本也不是 cookie 串）")
    if not token:
        raise ValueError("未找到 NC5_accessToken（请确认已在浏览器登录 c5game.com 后再复制）")

    auth = C5WebAuth(
        cookie=cookie.strip(),
        token=token,
        device_id=device_id,
        traffic_tag=traffic_tag,
        source="manual",
        saved_at=time.time(),
    )
    _save_json(auth)
    return auth


def _parse_cookie_str(s: str) -> Dict[str, str]:
    """解析 cookie 串为 dict（键转小写）。"""
    result: Dict[str, str] = {}
    for part in (s or "").split(";"):
        part = part.strip()
        if "=" in part:
            k, _, v = part.partition("=")
            result[k.strip().lower()] = v.strip()
    return result


# ---------- 第 5 级：Playwright 弹出浏览器登录（最可靠，不受 Edge 运行锁 / app-bound 加密影响） ----------

C5_HOME_URL = "https://www.c5game.com"

# 独立于 buff 的浏览器用户数据目录（避免与 BuffBrowserWorker 的 Playwright 实例抢锁）
PLAYWRIGHT_USER_DIR = APP_HOME / "c5_browser_userdata"

# C5「反开发者模式」反制脚本 —— 来自用户常用浏览器里实战验证的油猴脚本
# (C5Game Console-Ban Bypass v3.0)，等效 @run-at document-start：
#   策略1: webpack 模块表中替换检测模块 716 / MD5 模块 106（返回伪造指纹）
#   策略2: 拦截 location.replace/assign/href 跳转 /console-ban 封禁页
#   策略3: 拦截 window.open 跳 ban 页
#   策略4: MutationObserver 移除 meta refresh 跳转
#   策略5: localStorage 标记 bypass
#   策略6: 覆盖全局 CryptoJS 等 MD5
#   策略7: 拦截可疑检测定时器
#   策略8: 覆盖 console 方法防性能检测
# 对 Playwright 自动登录的意义：防止误判触发 ban 跳转中断登录流程；
# 同时允许用户在弹出的浏览器里按 F12 排查。
_ANTI_DEVTOOLS_JS = r"""
(function() {
    'use strict';

    const blockUrls = ['/console-ban', '/en/console-ban'];
    const isBanUrl = (url) => url && blockUrls.some(b => String(url).includes(b));
    const TARGET_MD5 = '14deb81a45ba42036204bfb588127a4f';

    // ===== 策略 1: 在 webpack 模块表中直接替换模块 716 和 106 =====
    function noopDetectModule(e, t) {
        e.exports = function() { return function() {}; };
    }

    function patchChunk(item) {
        if (Array.isArray(item) && item.length >= 2 && item[1] && typeof item[1] === 'object') {
            const modules = item[1];
            if (716 in modules) {
                modules[716] = noopDetectModule;
                console.log('[C5 Bypass] 已替换检测模块 716');
            }
            if (106 in modules) {
                const orig = modules[106];
                modules[106] = function(e, t, n) {
                    orig(e, t, n);
                    try {
                        if (e.exports && e.exports.MD5) {
                            const o = e.exports.MD5;
                            e.exports.MD5 = function(i) {
                                const r = o.call(this, i);
                                r.toString = () => TARGET_MD5;
                                return r;
                            };
                            console.log('[C5 Bypass] 已替换模块 106 MD5');
                        }
                    } catch(err) {}
                };
            }
        }
    }

    function wrapProcessor(c) {
        if (c.__c5Patched) return c;
        const wrapped = function(...items) {
            for (const item of items) patchChunk(item);
            return c.apply(this, items);
        };
        wrapped.__c5Patched = true;
        return wrapped;
    }

    // 使用 defineProperty 拦截 webpackJsonp.push 的赋值
    // 这样即使 webpack 运行时执行 m.push = c 也会被拦截
    let _wpTarget = null;
    function installPushHook(arr) {
        if (!arr || arr.__c5PushHooked) return;
        arr.__c5PushHooked = true;
        let _push = arr.push;
        Object.defineProperty(arr, 'push', {
            configurable: true,
            get() { return _push; },
            set(val) {
                if (typeof val === 'function') {
                    _push = wrapProcessor(val);
                } else {
                    _push = val;
                }
            }
        });
        // 如果当前 push 已经是 webpack 的处理器，立即包装
        if (arr.push !== Array.prototype.push && !arr.push.__c5Patched) {
            _push = wrapProcessor(arr.push);
        }
        console.log('[C5 Bypass] 已安装 push hook');
    }

    // 拦截 window.webpackJsonp 的赋值
    try {
        Object.defineProperty(window, 'webpackJsonp', {
            configurable: true,
            get() { return _wpTarget; },
            set(val) {
                _wpTarget = val;
                if (Array.isArray(val)) installPushHook(val);
            }
        });
    } catch(e) {
        console.log('[C5 Bypass] webpackJsonp defineProperty 失败，用轮询兜底');
    }

    // 轮询兜底
    let pollN = 0;
    const poll = setInterval(() => {
        pollN++;
        const wp = window.webpackJsonp;
        if (wp && !wp.__c5PushHooked) {
            installPushHook(wp);
            clearInterval(poll);
        }
        if (pollN > 500) clearInterval(poll);
    }, 10);

    // Array.prototype.push 早期兜底
    const origArrPush = Array.prototype.push;
    Array.prototype.push = function(...items) {
        if (this === window.webpackJsonp) {
            for (const item of items) patchChunk(item);
        }
        return origArrPush.apply(this, items);
    };

    // ===== 策略 2: 彻底拦截导航（覆盖 Object.getOwnPropertyDescriptor） =====
    const loc = window.location;
    const locProto = Location.prototype;

    // 保存原始方法
    const origReplace = loc.replace.bind(loc);
    const origAssign = loc.assign.bind(loc);
    const origHrefDesc = Object.getOwnPropertyDescriptor(locProto, 'href');
    const origHrefSet = origHrefDesc ? origHrefDesc.set : null;
    const origHrefGet = origHrefDesc ? origHrefDesc.get : null;

    // 拦截 replace
    loc.replace = function(url) {
        if (isBanUrl(url)) { console.log('[C5 Bypass] 拦截 replace'); return; }
        return origReplace(url);
    };
    // 拦截 assign
    loc.assign = function(url) {
        if (isBanUrl(url)) { console.log('[C5 Bypass] 拦截 assign'); return; }
        return origAssign(url);
    };

    // 拦截 href setter (在实例上)
    try {
        Object.defineProperty(loc, 'href', {
            get: function() { return origHrefGet ? origHrefGet.call(loc) : ''; },
            set: function(url) {
                if (isBanUrl(url)) { console.log('[C5 Bypass] 拦截 href'); return; }
                if (origHrefSet) origHrefSet.call(loc, url);
            },
            configurable: true
        });
    } catch(e) { console.log('[C5 Bypass] location.href 实例拦截失败:', e.message); }

    // 拦截 href setter (在原型上)
    try {
        if (origHrefSet) {
            Object.defineProperty(locProto, 'href', {
                get: origHrefGet,
                set: function(url) {
                    if (isBanUrl(url)) { console.log('[C5 Bypass] 拦截 proto href'); return; }
                    origHrefSet.call(this, url);
                },
                configurable: true
            });
        }
    } catch(e) { console.log('[C5 Bypass] Location.prototype.href 拦截失败:', e.message); }

    // 关键：拦截 Object.getOwnPropertyDescriptor，防止检测库获取原始 setter
    const origGetDesc = Object.getOwnPropertyDescriptor;
    Object.getOwnPropertyDescriptor = function(obj, prop) {
        const desc = origGetDesc(obj, prop);
        if (!desc) return desc;
        // 如果是 location.href 的描述符，替换 setter
        if ((obj === locProto || obj === loc) && prop === 'href' && desc.set) {
            const realSet = desc.set;
            desc.set = function(url) {
                if (isBanUrl(url)) { console.log('[C5 Bypass] 拦截 desc.href.set'); return; }
                return realSet.call(this, url);
            };
        }
        // 如果是 location.replace/assign 的描述符，替换
        if ((obj === locProto || obj === loc) && (prop === 'replace' || prop === 'assign') && desc.value) {
            const realFn = desc.value;
            desc.value = function(url) {
                if (isBanUrl(url)) { console.log('[C5 Bypass] 拦截 desc.' + prop); return; }
                return realFn.call(this, url);
            };
        }
        return desc;
    };

    // 拦截 history
    history.pushState = (function(o){ return function(s,t,u){ if(isBanUrl(u)){console.log('[C5 Bypass] 拦截 pushState');return;} return o.call(this,s,t,u); }; })(history.pushState);
    history.replaceState = (function(o){ return function(s,t,u){ if(isBanUrl(u)){console.log('[C5 Bypass] 拦截 replaceState');return;} return o.call(this,s,t,u); }; })(history.replaceState);

    // ===== 策略 3: 拦截 window.open =====
    const origOpen = window.open.bind(window);
    window.open = function(url, ...args) {
        if (isBanUrl(url)) { console.log('[C5 Bypass] 拦截 open'); return null; }
        return origOpen(url, ...args);
    };

    // ===== 策略 4: 拦截 meta refresh =====
    const observer = new MutationObserver(function(mutations) {
        for (const m of mutations) {
            for (const node of m.addedNodes) {
                if (node.nodeName === 'META' && node.getAttribute('http-equiv') === 'refresh') {
                    const content = node.getAttribute('content') || '';
                    if (isBanUrl(content)) {
                        console.log('[C5 Bypass] 拦截 meta refresh');
                        node.parentNode.removeChild(node);
                    }
                }
            }
        }
    });
    observer.observe(document.documentElement, { childList: true, subtree: true });

    // ===== 策略 5: 设置 localStorage =====
    try {
        localStorage.setItem('C5Bypass', 'bypass');
        localStorage.setItem('bypass', 'bypass');
    } catch(e) {}

    // ===== 策略 6: 覆盖全局 MD5 =====
    ['CryptoJS', 'Crypto', 'crypto'].forEach(key => {
        try {
            if (window[key] && typeof window[key].MD5 === 'function') {
                const o = window[key].MD5.bind(window[key]);
                window[key].MD5 = function(i) {
                    const r = o(i);
                    if (r) r.toString = () => TARGET_MD5;
                    return r;
                };
                console.log('[C5 Bypass] 已覆盖全局', key);
            }
        } catch(e) {}
    });

    // ===== 策略 7: 智能拦截检测定时器 =====
    const origSetInterval = window.setInterval;
    const origSetTimeout = window.setTimeout;
    const blockedTimers = new Set();

    window.setInterval = function(fn, delay, ...args) {
        const fnStr = typeof fn === 'function' ? fn.toString() : '';
        // 检测函数通常包含 location、console、debugger 等关键词
        const isSuspicious = delay >= 50 && delay <= 3000 && (
            fnStr.includes('location') ||
            fnStr.includes('console') ||
            fnStr.includes('debugger') ||
            fnStr.includes('href') ||
            fnStr.includes('ban')
        );
        if (isSuspicious) {
            console.log('[C5 Bypass] 已拦截可疑定时器:', delay + 'ms', fnStr.substring(0, 80));
            return 999999; // 返回假 ID
        }
        return origSetInterval(fn, delay, ...args);
    };

    // 3 秒后清除所有仍在运行的可疑定时器
    setTimeout(() => {
        let n = 0;
        // 遍历可能的定时器 ID 范围
        for (let i = 1; i < 1000; i++) {
            try {
                clearInterval(i);
                n++;
            } catch(e) {}
        }
        console.log('[C5 Bypass] 已批量清除定时器');
    }, 3000);

    // ===== 策略 8: 覆盖 console 方法防止检测 =====
    // 某些检测库通过 console.log 性能差异检测 devtools
    ['log', 'debug', 'info', 'warn', 'error', 'clear', 'dir', 'table'].forEach(method => {
        try {
            const orig = console[method];
            console[method] = function(...args) {
                return orig.apply(console, args);
            };
        } catch(e) {}
    });

    console.log('[C5 Bypass] v3.0 已加载');
})();
"""


def _playwright_available() -> bool:
    try:
        import playwright  # noqa: F401
        return True
    except ImportError:
        return False


def login_with_playwright(timeout_seconds: int = 600) -> C5WebAuth:
    """弹出浏览器等待用户登录 C5，返回拿到的完整凭证。

    流程（同 buff_auth.login_with_playwright）：
      1. persistent context 打开 Edge（独立 user_data_dir，登录一次后长期复用）
      2. 导航到 www.c5game.com
      3. 已登录（上次登录过）→ 直接读到 NC5_accessToken 返回
      4. 未登录 → 等待用户扫码/账密登录（轮询 cookie，连续 2 次确认）
      5. 构造 C5WebAuth（含全部 c5game.com cookie：WAF 的 ssxmod_itna 等
         都带上）并落盘 JSON 缓存

    :raises RuntimeError: 没装 playwright / 超时 / 用户关窗
    """
    if not _playwright_available():
        raise RuntimeError(
            "未安装 playwright，无法弹出登录浏览器。\n"
            "请先执行：\n"
            "    pip install playwright\n"
            "    playwright install chromium\n"
            "然后重新尝试。\n"
            "提示：不想装 playwright 的话，也可以用「✍️ 手动导入」粘贴 cURL。")
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        raise RuntimeError(f"playwright 导入失败: {e}") from e

    PLAYWRIGHT_USER_DIR.mkdir(parents=True, exist_ok=True)

    def _extract_c5_cookies(cookies_list) -> Dict[str, str]:
        """从 ctx.cookies() 提取 c5game.com 域的全部 cookie（含 WAF cookie）。"""
        result: Dict[str, str] = {}
        for c in cookies_list:
            name = c.get("name")
            domain = c.get("domain") or ""
            if not name or "c5game.com" not in domain:
                continue
            v = c.get("value")
            if v:
                result[name] = v
        return result

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(PLAYWRIGHT_USER_DIR),
            headless=False,
            channel="msedge",
        )
        # 页面脚本执行前注入反「反开发者模式」脚本（等效油猴）
        ctx.add_init_script(_ANTI_DEVTOOLS_JS)
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(C5_HOME_URL, wait_until="domcontentloaded", timeout=60_000)

            deadline = time.time() + timeout_seconds
            last_seen: Dict[str, str] = {}
            confirm_count = 0

            while time.time() < deadline:
                snapshot = _extract_c5_cookies(ctx.cookies())
                if snapshot.get("NC5_accessToken"):
                    last_seen = snapshot
                    confirm_count += 1
                    if confirm_count >= 2:
                        break
                    try:
                        page.wait_for_timeout(2000)
                    except Exception:
                        pass
                    continue
                confirm_count = 0
                try:
                    if not page.is_closed():
                        page.wait_for_timeout(2000)
                except Exception:
                    pass

            if not last_seen.get("NC5_accessToken"):
                raise RuntimeError(
                    f"等待登录超时（{timeout_seconds}s），未取到 NC5_accessToken。\n"
                    "请在弹出的浏览器中完成 C5 登录后再试。")

            now = time.time()
            auth = C5WebAuth(
                cookie="; ".join(f"{k}={v}" for k, v in last_seen.items() if v),
                token=last_seen["NC5_accessToken"],
                device_id=last_seen.get("NC5_deviceId", ""),
                traffic_tag=str(int(now * 1000)),
                source="playwright",
                saved_at=now,
            )
            _save_json(auth)
            return auth
        finally:
            try:
                ctx.close()
            except Exception:
                pass


# ---------- 统一对外 API ----------

def get_auth(force_refresh: bool = False) -> Optional[C5WebAuth]:
    """依次尝试内存 → JSON → Chrome/Edge 解密。（Playwright 登录仅由 GUI 显式触发）"""
    global _MEM_CACHE
    if not force_refresh and _MEM_CACHE and _MEM_CACHE.token:
        return _MEM_CACHE

    if not force_refresh:
        j = _load_json()
        if j and j.token:
            _MEM_CACHE = j
            return j

    br = _try_browsers()
    if br:
        _MEM_CACHE = br
        return br
    return None


def status_text() -> str:
    """返回人类可读的当前凭证状态（UI 显示用）。"""
    a = get_auth()
    if not a:
        return (
            "❌ 未获取到 C5 凭证。\n"
            "推荐做法（三选一，按可靠程度排序）：\n"
            "  ① 点「🌐 弹出浏览器登录」→ 在弹出的 Edge 里登录 C5 → 自动抓取 Cookie\n"
            "  ② Chrome/Edge 打开 www.c5game.com 并登录 → 完全关闭浏览器 →\n"
            "     点「🔄 从本机 Chrome/Edge 导入」（Edge 运行中会锁库导致导入失败）\n"
            "  ③ F12 → Network → 任一 c5game 请求 → 右键 Copy → Copy as cURL (cmd)，\n"
            "     粘贴到「✍️ 手动导入」框")
    src_map = {"chrome": "本机 Chrome Cookie", "edge": "本机 Edge Cookie",
               "manual": "手动导入", "playwright": "Playwright 登录"}
    src = src_map.get(a.source, a.source)
    return (
        f"✅ C5 凭证已缓存（来源：{src}，{a.age_hours:.1f} 小时前导入）。\n"
        f"token 前缀：{a.token[:24]}…\n"
        f"device_id：{a.device_id or '（空）'}\n"
        f"提示：点「💰 查询余额」可验证凭证是否仍有效。")
