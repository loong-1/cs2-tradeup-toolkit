"""Buff 登录态自动获取与缓存管理。

优先级：
    1) 内存缓存（本次启动已拿到就不再反复读）
    2) 本地 JSON 缓存文件 ``~/.steamdt/buff_session.json``
    3) 直接从本机 Chrome/Edge 的 Cookies SQLite 解密出 buff.163.com cookie
    4) 弹出 Playwright 浏览器让用户扫码登录，登录成功后保存 cookies
    5) 全部失败返回 None，让调用方回退到 ``found_buff.py`` 的硬编码常量

依赖安装（只有当用户想使用 Playwright 扫码模式时才需要）::

    pip install playwright
    playwright install chromium

如果系统缺少 playwright，第 4 步会抛出带安装指引的 RuntimeError，其他步骤不受影响。
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import sqlite3
import tempfile
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional, Tuple, List, Dict

# ---------- 路径常量 ----------

# 优先放在项目目录 data/.buff_auth 下，避免用户家目录的权限/沙箱问题；
# 如果项目目录不可写（例如打包只读），再回退到 ~/.steamdt。
_PROJECT_HOME = Path(__file__).resolve().parent.parent / "data" / ".buff_auth"
_HOME_FALLBACK = Path(os.path.expanduser("~")) / ".steamdt"

try:
    _PROJECT_HOME.mkdir(parents=True, exist_ok=True)
    APP_HOME = _PROJECT_HOME
except Exception:
    try:
        _HOME_FALLBACK.mkdir(parents=True, exist_ok=True)
        APP_HOME = _HOME_FALLBACK
    except Exception:
        # 两个地方都不可写（极端情况），回退到系统 temp 目录
        APP_HOME = Path(tempfile.gettempdir()) / ".steamdt_fallback"
        APP_HOME.mkdir(parents=True, exist_ok=True)

SESSION_JSON = APP_HOME / "buff_session.json"
PLAYWRIGHT_USER_DIR = APP_HOME / "buff_browser_userdata"

# Buff 域名（从 cookies 筛选 host_key）
BUFF_HOST_KEYS = ("buff.163.com", ".buff.163.com")
BUFF_REQUIRED_COOKIES = ("session", "csrf_token")

BUFF_LOGIN_URL = "https://buff.163.com/account/login"
BUFF_HOME_URL = "https://buff.163.com"


# ---------- 数据结构 ----------

@dataclass
class BuffCookies:
    session: str
    csrf_token: str
    source: str             # "json" | "chrome" | "edge" | "playwright"
    saved_at: float

    @property
    def age_seconds(self) -> float:
        return time.time() - self.saved_at


# ---------- 内存缓存 ----------

_MEM_CACHE: Optional[BuffCookies] = None


# ---------- JSON 读写 ----------

def _save_json(cookies: BuffCookies) -> None:
    tmp = SESSION_JSON.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(asdict(cookies), f, ensure_ascii=False, indent=2)
    os.replace(tmp, SESSION_JSON)


def _load_json() -> Optional[BuffCookies]:
    if not SESSION_JSON.exists():
        return None
    try:
        with open(SESSION_JSON, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not all(k in data for k in ("session", "csrf_token", "source", "saved_at")):
            return None
        cookies = BuffCookies(**data)
        if not cookies.session or not cookies.csrf_token:
            return None
        return cookies
    except Exception:
        return None


def save_manual_cookies(session: str, csrf_token: str) -> BuffCookies:
    """手动填写 session / csrf_token 并保存。下一次查询直接生效。"""
    global _MEM_CACHE
    s = (session or "").strip()
    t = (csrf_token or "").strip()
    if not s or not t:
        raise ValueError("session 和 csrf_token 都不能为空。")
    cookies = BuffCookies(session=s, csrf_token=t, source="manual", saved_at=time.time())
    _save_json(cookies)
    _MEM_CACHE = cookies
    return cookies


def clear_all() -> None:
    """清除本地 JSON 缓存和 Playwright user_data_dir，强制下一次重新登录。"""
    global _MEM_CACHE
    _MEM_CACHE = None
    try:
        if SESSION_JSON.exists():
            SESSION_JSON.unlink()
    except Exception:
        pass
    try:
        if PLAYWRIGHT_USER_DIR.exists():
            shutil.rmtree(PLAYWRIGHT_USER_DIR, ignore_errors=True)
    except Exception:
        pass


# ---------- 第 3 级：Chrome / Edge 本地 Cookie DB 解密 ----------

# Local State 的 AES key 缓存：{local_state_path 绝对路径: 32-byte key_bytes}
_OS_CRYPT_KEY_CACHE: Dict[Path, bytes] = {}


def _candidate_cookie_files() -> List[Tuple[str, Path, Path]]:
    """返回本机可能存在的 Chrome/Edge Cookies DB（含多 Profile）。

    优先级：Edge > Chrome（用户明确说用 Edge）。
    :returns: [(browser_name, cookies_db_path, user_data_root), ...]
        user_data_root 是 ``Local State`` 所在目录，解密 v20 cookie 必须用
        这个目录下的 Local State 取 AES key（不能从别的浏览器目录拿）。
    """
    candidates: List[Tuple[str, Path, Path]] = []
    local = Path(os.environ.get("LOCALAPPDATA", ""))
    if not local.exists():
        return candidates

    # 候选「浏览器根目录」：每个条目 (name, user_data_root)，按期望优先级排序
    roots: List[Tuple[str, Path]] = [
        ("edge",   local / "Microsoft" / "Edge" / "User Data"),
        ("chrome", local / "Google" / "Chrome" / "User Data"),
    ]

    def _profile_has_cookies(profile_dir: Path) -> bool:
        """兼容新旧两种 Cookie 存放位置。"""
        return (
            (profile_dir / "Network" / "Cookies").exists()
            or (profile_dir / "Cookies").exists()
        )

    for name, root in roots:
        if not root.exists():
            continue
        profile_names: List[str] = []
        # 默认 Profile
        if _profile_has_cookies(root / "Default"):
            profile_names.append("Default")
        # 扫描 Profile 1 / Profile 2 … （常见上限 10）
        for i in range(1, 11):
            if _profile_has_cookies(root / f"Profile {i}"):
                profile_names.append(f"Profile {i}")
        # 多账号 Profile（大数字 Profile、多用户）用目录扫描兜底，上限 20
        try:
            for entry in root.iterdir():
                if not entry.is_dir():
                    continue
                if entry.name == "Default" or entry.name.startswith("Profile "):
                    if entry.name not in profile_names and _profile_has_cookies(entry):
                        if len(profile_names) < 20:
                            profile_names.append(entry.name)
        except Exception:
            pass
        for prof in profile_names:
            # 新版 Chromium（Edge 120+/Chrome 130+）把 Cookies 从
            #   <profile>/Cookies          →  旧位置
            #   <profile>/Network/Cookies  →  新位置
            # 两个位置都查一下，新的优先（更可能命中）
            for sub in (Path("Network") / "Cookies", Path("Cookies")):
                cookie_db = root / prof / sub
                if cookie_db.exists():
                    if prof == "Default":
                        full_name = name
                    else:
                        full_name = f"{name}:{prof}"
                    candidates.append((full_name, cookie_db, root))
                    break  # 只取同一个 profile 的第一个命中位置
    return candidates


def _load_os_crypt_key(local_state_path: Path) -> Optional[bytes]:
    """从浏览器 User Data/Local State 中 DPAPI 解密出 32-byte AES-GCM key。

    结果按 ``local_state_path`` 全局缓存，避免每解密一个 cookie 都 DPAPI 一次。
    """
    if local_state_path in _OS_CRYPT_KEY_CACHE:
        return _OS_CRYPT_KEY_CACHE[local_state_path]
    key_bytes: Optional[bytes] = None
    try:
        import win32crypt  # type: ignore
    except ImportError:
        win32crypt = None  # type: ignore
    try:
        if local_state_path.exists() and win32crypt is not None:
            with open(local_state_path, "r", encoding="utf-8") as f:
                local_state = json.load(f)
            b64_key = local_state.get("os_crypt", {}).get("encrypted_key")
            if b64_key:
                raw = base64.b64decode(b64_key)
                if raw.startswith(b"DPAPI"):
                    out = win32crypt.CryptUnprotectData(
                        raw[5:], None, None, None, 0)[1]
                    if out and len(out) == 32:
                        key_bytes = bytes(out)
    except Exception:
        key_bytes = None
    if key_bytes is not None:
        _OS_CRYPT_KEY_CACHE[local_state_path] = key_bytes
    return key_bytes


def _decrypt_cookie_win(value_enc: bytes, local_state_path: Path) -> Optional[str]:
    """解密 Windows Chrome/Edge 新版 v20+ cookie 值。

    :param value_enc: cookies.encrypted_value 的原始字节
    :param local_state_path: 对应 User Data 目录下的 Local State 路径；必须和
        ``Cookies`` 文件来自同一浏览器安装（不同安装的 os_crypt.encrypted_key
        不同，混用 100% 解不出来）。
    """
    if not value_enc:
        return None
    try:
        import win32crypt  # type: ignore
    except ImportError:
        win32crypt = None  # type: ignore
    try:
        from Crypto.Cipher import AES  # pycryptodome
    except ImportError:
        AES = None  # type: ignore

    # 旧版（不是 v20 前缀）：直接 DPAPI
    if win32crypt is not None and not value_enc.startswith(b"v20"):
        try:
            out = win32crypt.CryptUnprotectData(value_enc, None, None, None, 0)[1]
            return out.decode("utf-8", errors="ignore") or None
        except Exception:
            pass

    # 新版 v20：需要 Local State 里的 AES-GCM 256-bit key
    if AES is None:
        return None
    key_bytes = _load_os_crypt_key(local_state_path)
    if not key_bytes:
        return None
    try:
        nonce = value_enc[3:15]
        ciphertext = value_enc[15:]
        if len(ciphertext) <= 16:
            return None
        aes = AES.new(key_bytes, AES.MODE_GCM, nonce=nonce)
        plain = aes.decrypt_and_verify(ciphertext[:-16], ciphertext[-16:])
        return plain.decode("utf-8", errors="ignore") or None
    except Exception:
        return None


def _read_cookies_from_db(db_path: Path, user_data_root: Path) -> Dict[str, str]:
    """把 Cookies 文件复制到临时目录再打开，避免占用。

    :param user_data_root: 对应浏览器的 ``User Data`` 根目录（定位 Local State 用）。
    """
    result: Dict[str, str] = {}
    local_state_path = user_data_root / "Local State"
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
                    "WHERE host_key IN (?, ?)",
                    BUFF_HOST_KEYS,
                )
                for host_key, name, enc in cur.fetchall():
                    if name not in BUFF_REQUIRED_COOKIES:
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


def _try_browsers() -> Optional[BuffCookies]:
    for name, db, root in _candidate_cookie_files():
        d = _read_cookies_from_db(db, root)
        s = d.get("session")
        c = d.get("csrf_token")
        if s and c:
            now = time.time()
            cookies = BuffCookies(session=s, csrf_token=c, source=name, saved_at=now)
            _save_json(cookies)
            return cookies
    return None


# ---------- 第 4 级：Playwright 扫码登录 ----------

def _playwright_available() -> bool:
    try:
        import playwright  # noqa: F401
        return True
    except ImportError:
        return False


def login_with_playwright(timeout_seconds: int = 300) -> BuffCookies:
    """弹出浏览器等待用户登录 Buff，返回拿到的 cookies。

    流程：
      1. 用 persistent context 打开 Edge（复用 user_data_dir 里的缓存）
      2. 导航到 buff.163.com 首页
      3. 如果已登录 → 直接读取 session/csrf_token 保存
      4. 如果未登录 → 等待用户扫码/账密登录
      5. 保存到 buff_session.json，Playwright user_data_dir 自动持久化 cookie

    :raises RuntimeError: 没装 playwright、超时、或用户关闭窗口。
    """
    if not _playwright_available():
        raise RuntimeError(
            "未安装 playwright，无法弹出扫码登录浏览器。\n"
            "请先执行：\n"
            "    pip install playwright\n"
            "    playwright install chromium\n"
            "然后重新尝试。\n"
            "提示：如果你不想装 playwright，也可以手动登录 Buff 网站后，\n"
            "回到本软件点「从本机 Chrome/Edge 导入登录态」即可（无需手动复制 Cookie）。")
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        raise RuntimeError(f"playwright 导入失败：{e}") from e

    PLAYWRIGHT_USER_DIR.mkdir(parents=True, exist_ok=True)

    def _extract_buff_cookies(cookies_list) -> Dict[str, str]:
        """从 ctx.cookies() 返回的列表中提取 Buff 的 session / csrf_token。"""
        result: Dict[str, str] = {}
        for c in cookies_list:
            n = c.get("name")
            hk = c.get("domain") or ""
            if n not in BUFF_REQUIRED_COOKIES:
                continue
            # domain 可能是 buff.163.com 或 .buff.163.com
            if "buff.163.com" not in hk:
                continue
            v = c.get("value")
            if v:
                result[n] = v
        return result

    with sync_playwright() as p:
        # 使用 persistent context，保留 user_data_dir 里的缓存，减少 CF 反复出现
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(PLAYWRIGHT_USER_DIR),
            headless=False,
            channel="msedge",
        )
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            # 导航到首页（不是登录页），这样已登录用户的 cookie 会直接加载
            page.goto(BUFF_HOME_URL, wait_until="domcontentloaded", timeout=60_000)

            deadline = time.time() + timeout_seconds
            last_seen: Dict[str, str] = {}
            confirm_count = 0  # 连续检测到 cookie 的次数

            while time.time() < deadline:
                # 获取所有 cookie 然后过滤（不传 URL 参数，避免域名匹配问题）
                all_cookies = ctx.cookies()
                snapshot = _extract_buff_cookies(all_cookies)

                if snapshot.get("session") and snapshot.get("csrf_token"):
                    last_seen = snapshot
                    confirm_count += 1
                    if confirm_count >= 2:
                        # 连续两次检测到 cookie，确认登录成功
                        break
                    # 等 2 秒再确认一次，防止 cookie 还在刷新中
                    try:
                        page.wait_for_timeout(2000)
                    except Exception:
                        pass
                    continue
                else:
                    confirm_count = 0

                # 还没登录，每 2 秒检查一次
                try:
                    if not page.is_closed():
                        # 如果当前页面不是 Buff 首页（可能跳到了登录页），保持页面活跃
                        try:
                            page.wait_for_timeout(2000)
                        except Exception:
                            pass
                except Exception:
                    pass

            if not (last_seen.get("session") and last_seen.get("csrf_token")):
                raise RuntimeError(
                    f"等待登录超时（{timeout_seconds}s），未取到 session + csrf_token。\n"
                    "请在弹出的浏览器中打开 https://buff.163.com 完成登录后再试。")

            now = time.time()
            cookies = BuffCookies(
                session=last_seen["session"],
                csrf_token=last_seen["csrf_token"],
                source="playwright", saved_at=now)
            _save_json(cookies)
            return cookies
        finally:
            try:
                ctx.close()
            except Exception:
                pass


# ---------- 统一对外 API ----------

def get_session_cookies(force_refresh: bool = False,
                        allow_playwright: bool = False) -> Optional[BuffCookies]:
    """依次尝试内存 → JSON → Chrome/Edge → (可选) Playwright 扫码。

    :param force_refresh: 跳过内存/JSON，直接从浏览器导入（适合点「刷新登录态」）
    :param allow_playwright: 前 3 级都失败时，是否允许弹窗 Playwright 让用户扫码
    """
    global _MEM_CACHE
    if not force_refresh and _MEM_CACHE and _MEM_CACHE.session and _MEM_CACHE.csrf_token:
        return _MEM_CACHE

    order: List[str] = []
    if not force_refresh:
        j = _load_json()
        if j and j.session and j.csrf_token:
            _MEM_CACHE = j
            return j
        order.append("json_miss")

    br = _try_browsers()
    if br:
        _MEM_CACHE = br
        return br
    order.append("browser_miss")

    if allow_playwright:
        pw = login_with_playwright()
        _MEM_CACHE = pw
        return pw

    return None


def status_text() -> str:
    """返回一段人类可读的当前登录态说明，用于 UI 显示。"""
    c = get_session_cookies()
    if not c:
        return (
            "❌ 未获取到 Buff 登录态。\n"
            "可选做法：\n"
            "  ① 先在 Chrome/Edge 中打开 https://buff.163.com 登录，然后回到本软件点「🔄 从本机浏览器导入」\n"
            "  ② 直接点「🆕 扫码登录（Playwright）」，在弹出的浏览器中扫码，登录一次后永久复用")
    age_h = c.age_seconds / 3600
    src_map = {"chrome": "本机 Chrome 浏览器 Cookie", "edge": "本机 Edge 浏览器 Cookie",
               "playwright": "Playwright 扫码登录（持久化缓存）", "json": "本地 JSON 缓存"}
    src = src_map.get(c.source, c.source)
    return (
        f"✅ Buff 登录态有效（来源：{src}）。\n"
        f"session 前缀：{c.session[:24]}…\n"
        f"csrf_token 前缀：{c.csrf_token[:24]}…\n"
        f"已缓存时长：{age_h:.1f} 小时。")
