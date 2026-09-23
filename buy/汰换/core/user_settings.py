"""用户运行时设置：独立于 config.py 的 GUI 用户偏好/凭据持久化。

与 config.py（静态、git 提交、开发/运维改的"配置"）的区别：
  - 本模块存的是"登录态/用户个人偏好"，git 不会提交，也不应该改 config.py 源码。
  - 读取优先级：【user_settings.json 覆盖】 > 【config.py 默认值】。

当前仅包含以下字段：
  - eco_web_cookie: str    对应 config.ECO_WEB_COOKIE
  - （未来可扩展：c5_login_cookies/buff_session_override 等 GUI 用户填写的字段）
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

# -------- 路径（与 c5_web_cookies.json、items.csv 同目录 data/）--------
_BASE_DIR = Path(__file__).resolve().parent.parent   # buy/汰换
DATA_DIR = _BASE_DIR / "data"
USER_SETTINGS_PATH = DATA_DIR / "user_settings.json"

_LOCK = threading.RLock()
_MEM_CACHE: dict | None = None   # 内存缓存；写入后立刻 update，避免再读磁盘


def _ensure_data_dir() -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass


def _default_settings() -> dict[str, Any]:
    return {
        "eco_web_cookie": "",      # 空=回退到 config.ECO_WEB_COOKIE
        "_schema_version": 1,
        "_created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def _read_from_disk() -> dict[str, Any]:
    if not USER_SETTINGS_PATH.exists():
        return _default_settings()
    try:
        with open(USER_SETTINGS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("root must be dict")
        # 合并缺失字段（向前兼容老版本）
        base = _default_settings()
        base.update({k: v for k, v in data.items() if not k.startswith("_")})
        return base
    except (OSError, ValueError, json.JSONDecodeError):
        # 文件坏了就用默认值 + 备份，避免数据丢失
        try:
            bak = USER_SETTINGS_PATH.with_suffix(".broken.json")
            USER_SETTINGS_PATH.replace(bak)
        except OSError:
            pass
        return _default_settings()


def load_all() -> dict[str, Any]:
    """加载完整 settings dict（返回一个拷贝，外部修改不影响缓存）。"""
    global _MEM_CACHE
    with _LOCK:
        if _MEM_CACHE is None:
            _MEM_CACHE = _read_from_disk()
        return dict(_MEM_CACHE)


def get(key: str, default: Any = None) -> Any:
    data = load_all()
    return data.get(key, default)


def save_all(data: dict[str, Any]) -> dict[str, Any]:
    """完整覆盖保存（内部使用，加锁）。返回合并后的最终 settings dict。"""
    global _MEM_CACHE
    with _LOCK:
        _ensure_data_dir()
        base = _default_settings()
        for k, v in (data or {}).items():
            if k.startswith("_"):
                continue   # 内部字段不允许外部覆盖
            base[k] = v
        base["_updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        # 写磁盘
        tmp_path = USER_SETTINGS_PATH.with_suffix(".tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(base, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, USER_SETTINGS_PATH)
        _MEM_CACHE = base
        return dict(base)


def update(**kwargs: Any) -> dict[str, Any]:
    """更新一个或多个字段；返回最终 settings dict。"""
    data = load_all()
    data.update({k: v for k, v in kwargs.items()})
    return save_all(data)


# ============================================================
# 语义化接口（GUI / eco_web_api 直接用这些，不要硬编码 key 字符串）
# ============================================================

ECO_WEB_COOKIE_KEY = "eco_web_cookie"
BUFF_REQ_INTERVAL_KEY = "buff_request_interval_s"   # float 秒；空/None 表示回退 config.BUFF_REQUEST_INTERVAL
ECO_OPENAPI_REQ_INTERVAL_KEY = "eco_openapi_request_interval_s"   # float 秒，对应 config.ECO_OPENAPI_REQUEST_INTERVAL
C5_REQ_INTERVAL_KEY = "c5_request_interval_s"                   # float 秒，对应 config.C5_REQUEST_INTERVAL
REPLACE_PROBE_ENABLED_KEY = "replace_probe_enabled"             # bool；替换模式（低档更便宜标红提示）开关


def get_eco_web_cookie() -> str:
    """读取 ECO 网页 Cookie（用户设置覆盖优先，空则返回 "" 让调用方回退 config.py）。"""
    val = get(ECO_WEB_COOKIE_KEY, "")
    return val if isinstance(val, str) else ""


def set_eco_web_cookie(cookie_str: str) -> dict[str, Any]:
    """保存 GUI 填写的 ECO 网页 Cookie。会覆盖 config.py 的默认值直到再次调用本 API 清空。

    传空字符串等价于"清空 GUI 覆盖，下次启动重新用 config.py 的值"。
    """
    return update(**{ECO_WEB_COOKIE_KEY: str(cookie_str or "")})


def clear_eco_web_cookie() -> dict[str, Any]:
    return set_eco_web_cookie("")


def get_buff_request_interval(config_default: float | None = None) -> float:
    """读 GUI 覆盖的 Buff 请求前间隔（秒）。若无/非法 → 回退到传进来的 config_default。

    返回值会钳制到 [0.0, 30.0]；返回 0.0 代表不主动做请求前 sleep（仍有 429 指数退避）。
    """
    raw = get(BUFF_REQ_INTERVAL_KEY, None)
    try:
        v = float(raw) if raw is not None and raw != "" else None
    except (TypeError, ValueError):
        v = None
    if v is None:
        try:
            v = float(config_default) if config_default is not None else 0.0
        except (TypeError, ValueError):
            v = 0.0
    if v < 0.0:
        v = 0.0
    if v > 30.0:
        v = 30.0
    return v


def set_buff_request_interval(seconds: float) -> dict[str, Any]:
    """保存 GUI 覆盖的 Buff 请求前间隔（秒）。传 None / -1 / 空会删除 GUI 覆盖 → 回退 config 默认。"""
    try:
        v = float(seconds) if seconds is not None and seconds != "" else None
    except (TypeError, ValueError):
        v = None
    if v is None:
        # 移除覆盖
        data = load_all()
        data.pop(BUFF_REQ_INTERVAL_KEY, None)
        return save_all(data)
    # 钳制
    if v < 0.0:
        v = 0.0
    if v > 30.0:
        v = 30.0
    return update(**{BUFF_REQ_INTERVAL_KEY: float(v)})


# ------------ 通用：节流参数 get/set 工厂（避免 Buff/ECO/C5 三段相同模板重复）------------
def _get_interval_generic(key: str, config_default) -> float:
    raw = get(key, None)
    try:
        v = float(raw) if raw is not None and raw != "" else None
    except (TypeError, ValueError):
        v = None
    if v is None:
        try:
            v = float(config_default) if config_default is not None else 0.0
        except (TypeError, ValueError):
            v = 0.0
    return max(0.0, min(30.0, v))


def _set_interval_generic(key: str, seconds) -> dict[str, Any]:
    try:
        v = float(seconds) if seconds is not None and seconds != "" else None
    except (TypeError, ValueError):
        v = None
    if v is None:
        data = load_all()
        data.pop(key, None)
        return save_all(data)
    return update(**{key: float(max(0.0, min(30.0, v)))})


def get_eco_openapi_request_interval(config_default=None) -> float:
    """ECO 开放平台 SellGoodsList 等接口的 GUI 覆盖请求前间隔（秒）。非法/空 → config 默认。"""
    return _get_interval_generic(ECO_OPENAPI_REQ_INTERVAL_KEY, config_default)


def set_eco_openapi_request_interval(seconds) -> dict[str, Any]:
    """保存 ECO OPENAPI 请求间隔覆盖；None/空/非法 → 清除覆盖，回退 config 默认。"""
    return _set_interval_generic(ECO_OPENAPI_REQ_INTERVAL_KEY, seconds)


def get_c5_request_interval(config_default=None) -> float:
    """C5 开放平台 products/list 的 GUI 覆盖请求前间隔（秒）。非法/空 → config 默认。"""
    return _get_interval_generic(C5_REQ_INTERVAL_KEY, config_default)


def set_c5_request_interval(seconds) -> dict[str, Any]:
    """保存 C5 请求间隔覆盖；None/空/非法 → 清除覆盖，回退 config 默认。"""
    return _set_interval_generic(C5_REQ_INTERVAL_KEY, seconds)
