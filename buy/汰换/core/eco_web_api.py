"""ECO 网页端汰换「开始模拟」官方 API 封装。

与 core/eco_client.py（开放平台 openapi.ecosteam.cn 的 RSA 签名体系）完全独立，
不要把两套体系的代码混在一起。本模块只负责：
  POST https://www.ecosteam.cn/Api/ReplaceSimulation/StartSimulation
  认证方式：网页端登录态 Cookie（refreshToken + loginToken + HMACCOUNT 等）

典型用法：
  from core import eco_web_api
  materials = [
      {"HashName": "XM1014 | Gum Wall Camo (Field-Tested)",
       "MaterialSource": 2,
       "WearValue": "0.2565198540687561",
       "Sort": i} for i in range(10)
  ]
  result = eco_web_api.start_simulation_web(materials)
  # result = ResultData 字典（Status/StatusData 壳已剥掉）
  #   keys: ResultFromWeaponBoxs, MaterialFromWeaponBoxs,
  #         EstimatedCost, CapitalPreservationRate, MaxResultImg,
  #         SimulationAllPrice
  outputs = eco_web_api.simulation_to_output_skeleton(result)
  # outputs = [{"name_cn","hash_name","prob","wear_float","reference_price",
  #             "collection_hash","rarity"}, ...]  （概率/磨损用 ECO 官方值，
  #  价格字段 reference_price 仅保留原始展示值，正式计算必须走 price_fetcher）

错误处理：
  - HTTP / 网络 / JSON 解析错误 → RuntimeError
  - 外层 StatusCode != "0" 或内层 ResultCode != "0" → RuntimeError（含 ResultCode+ResultMsg）
  - "请不要重复请求"（ResultCode=="1"）→ 也抛 RuntimeError，由调用方决定缓存/降级
"""
import functools
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Iterable

import requests

from utils.config import (
    ECO_WEB_COOKIE,
    ECO_WEB_START_SIM_URL,
    ECO_WEB_START_SIM_TIMEOUT,
    ECO_WEB_SIM_CACHE_SIZE,
    ECO_WEB_USER_CUSTOM_LIST_URL,
    ECO_WEB_CUSTOM_DELETE_URL,
    ECO_WEB_CUSTOM_BATCH_ADD_URL,
    ECO_WEB_SAVE_SIM_RESULT_URL,
    ECO_WEB_FORMULA_DETAIL_URL,
    ECO_WEB_REAL_REPLACE_RESULT_URL,
    ECO_WEB_API_TIMEOUT,
    ECO_WEB_USER_CUSTOM_CACHE_SIZE,
    ECO_WEB_FORMULA_DETAIL_CACHE_SIZE,
    ECO_WEB_REAL_REPLACE_CACHE_SIZE,
    ECO_WEB_REQUEST_INTERVAL,       # 新增：所有 ECO Api/* 请求前通用节流
    ECO_WEB_START_SIM_INTERVAL,      # 新增：StartSimulation 专属额外节流
)

logger = logging.getLogger(__name__)


# ============================================================
# 常量
# ============================================================

# MaterialSource 编码（与 ECO 前端 recipe.*.js 一致）：
#   1 = 市场(MarketList)   2 = 库存(StockList)   3 = 自定义(UserCustomList)
SOURCE_MARKET = 1
SOURCE_STOCK = 2
SOURCE_CUSTOM = 3

# 浏览器默认 UA（模拟 Edge/Chrome，避免被当作爬虫直接拦截）
_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/127.0.0.0 Safari/537.36 Edg/127.0.0.0"
)


# ============================================================
# 工具函数
# ============================================================

def _parse_cookie_str(cookie_str: str) -> dict:
    """把 "k1=v1; k2=v2" 形式的 Cookie 字符串转成 dict。"""
    result = {}
    if not cookie_str:
        return result
    for part in cookie_str.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, v = part.split("=", 1)
        result[k.strip()] = v.strip()
    return result


# ECO 网页汰换 Cookie（www.ecosteam.cn）必需的 3 个键；
# 缺少任意一个都会触发 ResultCode=4001 "用户未登录"。
_ECO_WEB_REQUIRED_COOKIE_KEYS = ("clientId", "refreshToken", "loginToken")

# loginToken 自动刷新：ECO 官方真实接口（真 HTTP 已验证 2026-08-31：
#   POST https://www.ecosteam.cn/Api/Login/RefreshToken  json={refreshToken}
# → StatusCode=0 / ResultCode=0 → ResultData: {Token(新loginToken), RefreshToken,
#   ClientId, TokenExpireDateForCST, RefreshTokenExpireDateForCST})
# 过期 refreshToken 本身也能用（ResultCode=0 还能换出一对新 token），只有
# refreshToken 也过期时才返回非 0，此时才需要用户手动抓 cookie。
_ECO_LOGIN_REFRESH_URL = "https://www.ecosteam.cn/Api/Login/RefreshToken"


def _dict_to_cookie_str(cookies: dict) -> str:
    """把 cookies dict 拼成 "k1=v1; k2=v2" 形式（写回 user_settings / Cookie 头用）。"""
    if not isinstance(cookies, dict):
        return ""
    parts = []
    for k, v in cookies.items():
        if v is None:
            continue
        parts.append(f"{k}={v}")
    return "; ".join(parts)


def try_refresh_login_via_api(cookie_dict: dict,
                              *, save_to_user_settings: bool = True
                              ) -> dict | None:
    """当 loginToken 过期（ResultCode=4001 用户未登录）时，用 refreshToken
    自动换一对新的 {clientId, refreshToken, loginToken}，避免用户每次都手动
    抓 F12 Cookie（对齐经验 891904：优先 refresh 兜底，不要一上来就让用户清缓存）。

    Args:
        cookie_dict: 原 cookies dict（必须含 refreshToken 非空）
        save_to_user_settings: True 时把新 cookie_str 写入 user_settings.json
                               （下一次启动就用新的，不会再回退到 config 里的旧值）

    Returns:
        - 刷新成功：新的完整 cookies dict（含 ClientId/RefreshToken/loginToken 三键，
                    也可能带 Set-Cookie 里回传的 HMACCOUNT/acw_tc 等，请求直接喂）
        - 刷新失败（refreshToken 也失效 / 网络错 / ResultCode != 0）：None，
                    调用方应继续走「引导用户手动抓 Cookie」路径。
    """
    if not isinstance(cookie_dict, dict):
        return None
    rt = str(cookie_dict.get("refreshToken") or "").strip()
    if not rt:
        return None
    try:
        from utils.config import ECO_WEB_REQUEST_INTERVAL
        try:
            d = float(ECO_WEB_REQUEST_INTERVAL or 0)
            if d > 0:
                time.sleep(d)
        except (TypeError, ValueError):
            pass
        headers = _default_headers(content_json=True)
        # refreshToken 接口本身用旧 cookie 三键也能发；ECO 官方也允许 json body 单传
        resp = requests.post(
            _ECO_LOGIN_REFRESH_URL,
            headers=headers,
            json={"refreshToken": rt},
            cookies=cookie_dict,
            timeout=float(ECO_WEB_API_TIMEOUT or 15.0),
        )
        if resp.status_code != 200:
            logger.debug("[ECO-REFRESH] HTTP %s != 200，自动刷新失败：%s",
                         resp.status_code, resp.text[:200])
            return None
        try:
            dj = resp.json()
        except (ValueError, json.JSONDecodeError) as e:
            logger.debug("[ECO-REFRESH] 响应非 JSON：%s；text=%s", e, resp.text[:300])
            return None
        sc = str(dj.get("StatusCode", ""))
        sd = dj.get("StatusData") if isinstance(dj.get("StatusData"), dict) else {}
        rc = str(sd.get("ResultCode", ""))
        if sc != "0" or rc != "0":
            logger.debug("[ECO-REFRESH] 失败 StatusCode=%r ResultCode=%r ResultMsg=%r",
                         sc, rc, sd.get("ResultMsg"))
            return None
        rd = sd.get("ResultData") or {}
        if not isinstance(rd, dict):
            return None
        new_client = str(rd.get("ClientId") or cookie_dict.get("clientId") or "").strip()
        new_refresh = str(rd.get("RefreshToken") or "").strip()
        new_login = str(rd.get("Token") or "").strip()
        if not (new_client and new_refresh and new_login):
            logger.debug("[ECO-REFRESH] 缺少新三键：ClientId=%r RefreshToken=%r Token=%r",
                         new_client[:12], new_refresh[:12], new_login[:12])
            return None
        # 合并：保留原 cookie 其他键（HMACCOUNT / userLocale / language / 风控 cookie
        #      如 acw_tc / cdn_sec_tc 等），三键用 refresh 换出来的新值覆盖。
        merged = dict(cookie_dict)
        merged["clientId"] = new_client
        merged["refreshToken"] = new_refresh
        merged["loginToken"] = new_login
        # Set-Cookie 里可能回写了新的 acw_tc/HMACCOUNT 等 — 合并不覆盖三键
        set_cookie = resp.headers.get("Set-Cookie")
        if set_cookie:
            try:
                # requests 会把多 Set-Cookie 用逗号合并成一个值，按 ", " 切但日期
                # 里也有 ", "，所以仅按 "; " split 后对每个 token 取 k=v 是安全的
                sc_dict = _parse_cookie_str(set_cookie)
                for k, v in sc_dict.items():
                    if k in _ECO_WEB_REQUIRED_COOKIE_KEYS:
                        continue   # 三键一定用 RefreshToken 接口返回的（值明确）
                    if not v:
                        continue
                    merged[k] = v
            except Exception:
                pass
        # 可选：写回 user_settings.json（GUI 下一次启动就带新 cookie，不会再读 config 里的旧值）
        if save_to_user_settings:
            try:
                from core import user_settings as _us
                new_str = _dict_to_cookie_str({
                    k: merged[k]
                    for k in _ECO_WEB_REQUIRED_COOKIE_KEYS
                    if k in merged and merged[k]
                })
                if new_str:
                    _us.update(eco_web_cookie=new_str)
                    logger.info(
                        "[ECO-REFRESH] loginToken 自动刷新成功，已写入 user_settings.json："
                        "new_loginToken=%s… new_refreshToken=%s…",
                        new_login[:8], new_refresh[:8])
            except Exception as e:
                logger.debug("[ECO-REFRESH] user_settings.json 写入失败（不影响本次请求）：%s", e)
        return merged
    except requests.RequestException as e:
        logger.debug("[ECO-REFRESH] 网络失败 %s：%s", type(e).__name__, e)
        return None
    except Exception as e:
        logger.debug("[ECO-REFRESH] 其他异常 %s：%s", type(e).__name__, e)
        return None


def _validate_eco_web_cookies(cookies: dict,
                              context: str = "ECO 网页接口") -> None:
    """校验 ECO 网页 Cookie 是否包含必需的 3 键且值非空。

    - 缺失/为空时，抛 RuntimeError，错误信息包含：当前 Cookie 实际含哪些键
      （首 8 字符脱敏）、用户可操作的抓 cookie 步骤指引。
    - 目的：ResultCode=4001 最常见原因是 Cookie 过期或字段缺，但用户收到
      "用户未登录" 4 字后不知道该怎么办，这里直接给可执行指引。
    """
    if not isinstance(cookies, dict):
        cookies = {}
    missing = [k for k in _ECO_WEB_REQUIRED_COOKIE_KEYS
               if not str(cookies.get(k) or "").strip()]
    if not missing:
        return
    # 脱敏展示已有键（只显示首 8 字符，避免日志泄露）
    have = []
    for k, v in cookies.items():
        vs = str(v or "")
        masked = (vs[:8] + "…") if len(vs) > 8 else vs
        have.append(f"{k}={masked!r}")
    have_summary = ", ".join(have) if have else "<空>"
    raise RuntimeError(
        f"【{context}】ECO 网页 Cookie 缺少必填字段：{missing}。\n"
        f"  当前 Cookie 实际内容（已脱敏）：{have_summary}\n"
        f"  错误 ResultCode='4001'（用户未登录）的标准修复步骤：\n"
        f"  1) 打开浏览器，登录 https://www.ecosteam.cn/forge/recipe/create （用手机号+验证码或账号密码登录，确保能手动创建汰换配方）\n"
        f"  2) 登录成功后，按 F12 打开『开发者工具』 → 切到 Network（网络）页签 → 选中任意一个 XHR/Fetch 请求（比如 RefreshLogin 或访问 www.ecosteam.cn 的请求）\n"
        f"  3) 在请求头里找到『Cookie:』这一行，整行复制（格式为 clientId=xxx; refreshToken=xxx; loginToken=xxx）\n"
        f"  4) 打开 buy/汰换/utils/config.py 第 85 行附近的 ECO_WEB_COOKIE = \"...\" ，整行替换成你刚复制的值并保存。\n"
        f"  5) 重新运行程序即可。\n"
        f"  常见坑：\n"
        f"   - 只复制了 loginToken，没复制 clientId/refreshToken（ECO 同时需要 3 个，1 个缺 = 4001）\n"
        f"   - 浏览器 F12 复制时带了前缀『Cookie:』4 个字 → 请去掉前缀，只保留分号分隔的三个键值对\n"
        f"   - 登录后立刻复制（ECO 登录态有效期约 1~7 天，过期会再次 4001，按上述步骤重新抓一次即可）")


def _wrap_4001_error_if_needed(err: Exception, cookies: dict) -> Exception:
    """当 RuntimeError 里出现 ResultCode='4001'/'用户未登录'时，把"抓 Cookie 指引"
    作为附加文本拼到错误信息末尾，便于用户直接按步骤操作。"""
    if not isinstance(err, RuntimeError):
        return err
    msg = str(err)
    if "4001" not in msg and "用户未登录" not in msg:
        return err
    # 已经包含完整指引（比如 _validate_eco_web_cookies 抛出的）就不重复
    if "抓 cookie 步骤指引" in msg or "ResultCode='4001'（用户未登录）的标准修复步骤" in msg:
        return err
    missing = [k for k in _ECO_WEB_REQUIRED_COOKIE_KEYS
               if not str(cookies.get(k) or "").strip()]
    have = []
    for k, v in (cookies or {}).items():
        vs = str(v or "")
        masked = (vs[:8] + "…") if len(vs) > 8 else vs
        have.append(f"{k}={masked!r}")
    have_summary = ", ".join(have) if have else "<空>"
    hint = (
        f"\n\n【ECO 4001 用户未登录 — 5 步快速修复】\n"
        f"  当前 Cookie 内容（已脱敏）：{have_summary}\n"
        f"  缺失必填字段：{missing if missing else '三键齐全，但 loginToken 已过期（最常见）'}\n"
        f"  1) Chrome/Edge 打开 https://www.ecosteam.cn/forge/recipe/create ，登录自己的账号\n"
        f"  2) 登录成功后按 F12 → Network → 刷新页面 → 点最上面第一个 www.ecosteam.cn 请求\n"
        f"  3) 右侧 Request Headers → 找到『Cookie: clientId=...; refreshToken=...; loginToken=...』整行复制（去掉前缀『Cookie:』4 字）\n"
        f"  4) 粘贴替换 buy/汰换/utils/config.py 第 85 行 ECO_WEB_COOKIE = \"...\" 的值\n"
        f"  5) 保存后重新运行程序即可；如果再次出现，按同样步骤刷新一次 Cookie（ECO 登录态有时效）。\n"
    )
    return RuntimeError(msg + hint)


def _material_cache_key(materials: Iterable[dict]) -> str:
    """把 10 件材料规范化为可哈希字符串，用作 LRU 缓存 key。

    对每件材料，按 Sort 升序排列后，取 (HashName, WearValue) 拼接。
    MaterialSource 不影响 ECO 的计算结果（只影响前端 UI 展示/库存扣减），
    所以不参与 cache key。
    """
    items = []
    for m in materials:
        sort = int(m.get("Sort", 0) or 0)
        hn = (m.get("HashName") or "").strip()
        wv = (m.get("WearValue") or "").strip()
        items.append((sort, hn, wv))
    items.sort(key=lambda x: x[0])
    return "|".join(f"{hn}:{wv}" for _, hn, wv in items)


def _validate_materials(materials):
    """校验 StartSimulation 输入 Materials：10 件、字段齐全。

    非法时抛 ValueError。
    """
    if not isinstance(materials, (list, tuple)):
        raise ValueError(
            f"StartSimulation Materials 必须是 list，收到 {type(materials).__name__}")
    if len(materials) != 10:
        raise ValueError(
            f"StartSimulation Materials 必须正好 10 件，实际 {len(materials)} 件")
    for i, m in enumerate(materials):
        if not isinstance(m, dict):
            raise ValueError(f"Materials[{i}] 必须是 dict，实际 {type(m).__name__}")
        if not (m.get("HashName") or "").strip():
            raise ValueError(f"Materials[{i}] 缺少 HashName")
        if "WearValue" not in m or m["WearValue"] in (None, ""):
            raise ValueError(f"Materials[{i}] 缺少 WearValue")
        if m.get("MaterialSource") not in (SOURCE_MARKET, SOURCE_STOCK, SOURCE_CUSTOM):
            raise ValueError(
                f"Materials[{i}] MaterialSource 必须是 1/2/3，实际 {m.get('MaterialSource')!r}")


def _validate_and_unwrap(resp_json: dict, api_name: str = "ECO 网页接口") -> Any:
    """剥离响应的 StatusCode/StatusData 两层壳；任何非 0 抛 RuntimeError。

    - 兼容 ResultData 是 dict/list/基本类型 三种情况（列表接口通常返回 list）。
    - api_name 仅用于构造异常提示。
    """
    if not isinstance(resp_json, dict):
        raise RuntimeError(
            f"{api_name} 响应不是 dict: {resp_json!r}")
    status_code = str(resp_json.get("StatusCode", ""))
    status_msg = resp_json.get("StatusMsg") or ""
    if status_code != "0":
        raise RuntimeError(
            f"{api_name} 外层失败 StatusCode={status_code!r} "
            f"StatusMsg={status_msg!r}")
    status_data = resp_json.get("StatusData")
    if not isinstance(status_data, dict):
        raise RuntimeError(
            f"{api_name} StatusData 缺失或非 dict: {status_data!r}")
    result_code = str(status_data.get("ResultCode", ""))
    result_msg = status_data.get("ResultMsg") or ""
    if result_code != "0":
        raise RuntimeError(
            f"{api_name} 内层失败 ResultCode={result_code!r} "
            f"ResultMsg={result_msg!r}")
    # ResultData 可为空 (保存类接口)、list (列表类)、dict (详情/模拟类)
    return status_data.get("ResultData")


def _normalize_cookies(cookies) -> dict:
    """把各种 Cookie 输入（None/str/dict/CookieJar）统一成 dict 形式。

    None=【用户 GUI 填写的 Cookie 优先】 > config.ECO_WEB_COOKIE 兜底。
    返回的 dict 可直接用于 requests cookies 参数，也便于 json.dumps 作为缓存 key。
    """
    if cookies is None:
        # 两级覆盖：user_settings.eco_web_cookie（GUI 面板填入）> config.ECO_WEB_COOKIE
        from core import user_settings as _us
        override = _us.get_eco_web_cookie()
        cookie_src = override if (isinstance(override, str) and override.strip()) \
                    else (ECO_WEB_COOKIE or "")
        return _parse_cookie_str(cookie_src)
    if isinstance(cookies, str):
        return _parse_cookie_str(cookies)
    if isinstance(cookies, dict):
        return dict(cookies)
    try:
        return {k: v for k, v in cookies.items()}
    except Exception:
        return {}


def _default_headers(content_json: bool = True) -> dict:
    """默认 headers（与 StartSimulation 完全一致，避免被 ECO 风控拦截）。"""
    h = {
        "User-Agent": _DEFAULT_UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Origin": "https://www.ecosteam.cn",
        "Referer": "https://www.ecosteam.cn/forge/recipe/create",
    }
    if content_json:
        h["Content-Type"] = "application/json;charset=UTF-8"
    return h


def _do_request(method: str,
                url: str,
                *,
                api_name: str,
                params: dict | None = None,
                json_body=None,
                cookies=None,
                timeout: float,
                extra_headers: dict | None = None) -> Any:
    """统一请求封装：method + params/json + 超时 + 剥壳 + 异常分类。"""
    # ---- C1: ECO 全局请求节流（用户可通过 config.ECO_WEB_REQUEST_INTERVAL 自定义）----
    try:
        interval = float(ECO_WEB_REQUEST_INTERVAL or 0)
        if interval > 0:
            time.sleep(interval)
    except (TypeError, ValueError):
        interval = 0.0
    headers = _default_headers(content_json=(json_body is not None))
    if extra_headers:
        headers.update(extra_headers)

    # ---- Cookie 校验（www.ecosteam.cn 7 个 Api/* 接口都需要）：缺键就提前抛，
    #      避免发请求后拿到含糊的 4001 "用户未登录"还得手动排
    cookie_dict = _normalize_cookies(cookies)
    _validate_eco_web_cookies(cookie_dict, context=api_name)

    try:
        resp = requests.request(
            method=method,
            url=url,
            params=params,
            json=json_body,
            headers=headers,
            cookies=cookie_dict,
            timeout=timeout,
        )
    except requests.RequestException as e:
        raise RuntimeError(
            f"{api_name} 网络错误: {e.__class__.__name__}: {e}") from e

    if resp.status_code != 200:
        preview = resp.text[:500] if resp.text else ""
        raise RuntimeError(
            f"{api_name} HTTP {resp.status_code} != 200，响应预览: {preview!r}")
    try:
        data = resp.json()
    except (ValueError, json.JSONDecodeError) as e:
        preview = resp.text[:500] if resp.text else ""
        raise RuntimeError(
            f"{api_name} 响应 JSON 解析失败 {e}，响应预览: {preview!r}") from e
    try:
        return _validate_and_unwrap(data, api_name=api_name)
    except RuntimeError as err:
        # 【loginToken 过期兜底】ResultCode=4001/ResultMsg=用户未登录 是 ECO 登录态
        # 有效期短造成的最常见错误；90% 的情况 refreshToken 仍然有效。
        # 对齐经验 891904：先按 refreshToken 自动换一对新 token → 重放原请求一次；
        # 仅当 refresh 也失败（refreshToken 也过期了 / 网络错 / 返回非 0）时，才走
        # 原『5 步手动抓 F12 Cookie』指引，不要一上来就让用户操作。
        #
        # 注意：_do_request 是单次请求原子，这里在内部只做 1 次自动重试（避免 refresh
        # 又触发 refresh 的死循环）。
        msg = str(err)
        if ("4001" in msg or "用户未登录" in msg) and \
                "抓 cookie 步骤指引" not in msg and \
                "ResultCode='4001'（用户未登录）的标准修复步骤" not in msg:
            # try_refresh_login_via_api 内部会写 user_settings.json（持久化新 cookie）
            new_cookie = try_refresh_login_via_api(cookie_dict,
                                                   save_to_user_settings=True)
            if new_cookie:
                logger.info(
                    "[ECO-REFRESH] %s 返回 4001（loginToken 过期），自动刷新成功，"
                    "将用新 token 重放请求一次。new_loginToken=%s…",
                    api_name,
                    (str(new_cookie.get("loginToken") or "")[:8] + "…")
                    if new_cookie.get("loginToken") else "")
                # 重放前再睡一次节流：refresh 请求本身已经消耗了 1 次节流配额，
                # 这里保持 ECO_WEB_REQUEST_INTERVAL 的速率，避免下一条被限流。
                try:
                    d = float(ECO_WEB_REQUEST_INTERVAL or 0)
                    if d > 0:
                        time.sleep(d)
                except (TypeError, ValueError):
                    pass
                try:
                    resp2 = requests.request(
                        method=method, url=url, params=params, json=json_body,
                        headers=headers, cookies=new_cookie, timeout=timeout)
                except requests.RequestException as e2:
                    raise RuntimeError(
                        f"{api_name} 重放(刷新后)网络错误: {type(e2).__name__}: {e2}"
                    ) from e2
                if resp2.status_code != 200:
                    preview2 = resp2.text[:500] if resp2.text else ""
                    # 重放仍失败 → 回到『指引用户手动抓 Cookie』
                    wrapped = RuntimeError(
                        f"{api_name} 刷新后重放 HTTP {resp2.status_code} != 200，"
                        f"响应预览: {preview2!r}")
                    raise _wrap_4001_error_if_needed(wrapped, new_cookie) from err
                try:
                    data2 = resp2.json()
                except (ValueError, json.JSONDecodeError) as e3:
                    preview3 = resp2.text[:500] if resp2.text else ""
                    wrapped = RuntimeError(
                        f"{api_name} 刷新后重放 JSON 解析失败 {e3}，"
                        f"响应预览: {preview3!r}")
                    raise _wrap_4001_error_if_needed(wrapped, new_cookie) from err
                try:
                    return _validate_and_unwrap(data2, api_name=api_name)
                except RuntimeError as err2:
                    # 重放仍然 4001 才包用户指引
                    raise _wrap_4001_error_if_needed(err2, new_cookie) from err2
            # new_cookie is None：refreshToken 也不行/网络错 — 包用户手动抓 cookie 指引
            raise _wrap_4001_error_if_needed(err, cookie_dict) from err
        # 非 4001 错误：直接透传（不包用户指引）
        raise _wrap_4001_error_if_needed(err, cookie_dict) from err


# ============================================================
# 核心请求封装
# ============================================================

@functools.lru_cache(maxsize=ECO_WEB_SIM_CACHE_SIZE)
def _start_simulation_cached(cache_key: str,
                             materials_json: str,
                             cookies_json: str,
                             timeout: float,
                             url: str) -> dict:
    """带 LRU 缓存的底层请求。参数全是可哈希基础类型（functools.lru_cache 要求）。

    上层 start_simulation_web 负责拼这些参数；cache_key 必须是材料规范化后的 key，
    cookies_json 是 cookies dict 的 json 字符串（用于保证不同 Cookie 不互串结果，
    尽管 ECO 的计算结果应当与登录用户无关——但保守起见保留）。
    """
    materials = json.loads(materials_json)
    cookies = json.loads(cookies_json) or None

    # ---- C1: StartSimulation 请求节流
    #  由于 StartSimulation 没走通用 _do_request，需要单独 sleep：
    #   sleep = ECO_WEB_REQUEST_INTERVAL（全局通用）+ ECO_WEB_START_SIM_INTERVAL（专属额外）
    try:
        interval = float(ECO_WEB_REQUEST_INTERVAL or 0)
        sim_interval = float(ECO_WEB_START_SIM_INTERVAL or 0)
        total_sleep = max(0.0, interval) + max(0.0, sim_interval)
        if total_sleep > 0:
            time.sleep(total_sleep)
    except (TypeError, ValueError):
        total_sleep = 0.0

    headers = {
        "User-Agent": _DEFAULT_UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Content-Type": "application/json;charset=UTF-8",
        "Origin": "https://www.ecosteam.cn",
        "Referer": "https://www.ecosteam.cn/forge/recipe/create",
    }
    # Cookie 既可以放到 headers.Cookie 里，也可以作为 requests 的 cookies 参数；
    # 这里用 requests 的 cookies 参数（dict 形式）更清晰。
    cookie_dict = dict(cookies) if isinstance(cookies, dict) else {}
    # 【StartSimulation 专属】Cookie 校验：缺 clientId/refreshToken/loginToken 任意一个
    #   就直接抛带完整 5 步指引的错误，不发 HTTP 浪费时间。
    _validate_eco_web_cookies(cookie_dict, context="StartSimulation")
    try:
        resp = requests.post(
            url,
            json={"Materials": materials, "GameId": 730},
            headers=headers,
            cookies=cookie_dict,
            timeout=timeout,
        )
    except requests.RequestException as e:
        raise RuntimeError(
            f"StartSimulation 网络错误: {e.__class__.__name__}: {e}") from e

    if resp.status_code != 200:
        preview = resp.text[:500] if resp.text else ""
        raise RuntimeError(
            f"StartSimulation HTTP {resp.status_code} != 200，响应预览: {preview!r}")
    try:
        data = resp.json()
    except (ValueError, json.JSONDecodeError) as e:
        preview = resp.text[:500] if resp.text else ""
        raise RuntimeError(
            f"StartSimulation 响应 JSON 解析失败 {e}，响应预览: {preview!r}") from e
    try:
        return _validate_and_unwrap(data, api_name="StartSimulation")
    except RuntimeError as err:
        # 拿到 4001 后，在原"用户未登录"末尾追加【5 步抓 Cookie 修复指引】
        raise _wrap_4001_error_if_needed(err, cookie_dict) from err


# ---- ECO StartSimulation 磨损档为【开区间】（实测 2026-08）----
# 实测结论（M4A4 Mainframe FT / Negev Bulkhead FN 真实 HTTP 验证）：
#   HashName=(Field-Tested) 时 WearValue=0.38 或 0.15 → 2001「材料磨损值错误」；
#   0.379999 / 0.150001 → 成功。即档位区间为 (0.15, 0.38) 两端开。
#   同理 Factory New 为 (0, 0.07)：0.0 与 0.07 均被拒。
# 因此构造请求体时必须把 WearValue 夹进 HashName 档位的开区间内。
_ECO_TIER_RANGES = [
    ("Factory New",  0.00, 0.07),
    ("Minimal Wear", 0.07, 0.15),
    ("Field-Tested", 0.15, 0.38),
    ("Well-Worn",    0.38, 0.45),
    ("Battle-Scarred", 0.45, 1.00),
]
_ECO_TIER_EPS = 1e-6   # 端点内缩量（实测 0.379999 可通过）


def _clamp_wear_to_tier(hash_name: str, wear_value,
                        skin_min=None, skin_max=None) -> str:
    """把 WearValue 夹进【皮肤浮点区间 ∩ 档位区间】的开区间内，规避 2001。

    实测（2026-08 真实 HTTP）ECO 的校验规则：
      - 档位区间开：FT 传 0.38/0.15 → 2001，0.379999/0.150001 → OK；
      - 皮肤区间开：Candy Apple（皮肤 0~0.30）FT 传 0.30 → 2001，
        0.299999 → OK。即有效域 = (皮肤min, 皮肤max) ∩ (档位min, 档位max)。

    Args:
        skin_min/skin_max: 皮肤实际浮点范围（GUI 从主 CSV「磨损区间」解析传入；
            None=未知，跳过皮肤夹紧，仅按档位处理）。

    - 无法解析浮点 / HashName 无档位后缀 → 仅按皮肤区间处理或原样返回；
    - 值越界 → 内缩 EPS（1e-6）；档内正常值 → 原样返回（保留完整精度字符串）。
    """
    s = str(wear_value).strip()
    try:
        f = float(s)
    except (TypeError, ValueError):
        return s
    orig = f

    # ---- 1) 皮肤实际浮点区间（开区间，两端都拒）----
    try:
        if skin_min is not None:
            smin = float(skin_min)
            if 0.0 <= smin < 1.0 and f <= smin:
                f = smin + _ECO_TIER_EPS
        if skin_max is not None:
            smax = float(skin_max)
            if 0.0 < smax <= 1.0 and f >= smax:
                f = smax - _ECO_TIER_EPS
    except (TypeError, ValueError):
        pass

    # ---- 2) HashName 档位区间（开区间）----
    hn = str(hash_name or "").strip()
    for suffix, tlo, thi in _ECO_TIER_RANGES:
        if hn.endswith(f"({suffix})") or f"({suffix})" in hn:
            if f <= tlo:
                f = tlo + _ECO_TIER_EPS
            if f >= thi:
                f = thi - _ECO_TIER_EPS
            break

    if f != orig:
        return f"{f:.16f}"
    return s


def start_simulation_web(materials, cookies=None, game_id: int = 730,
                         timeout: float | None = None,
                         headers: dict | None = None,
                         use_cache: bool = True) -> dict:
    """调用 ECO 网页 StartSimulation，返回剥壳后的 ResultData dict。

    Args:
        materials: 长度必须 = 10，每个元素是 dict，至少含：
            - HashName (str): 完整英文 market_hash_name，
                例如 'AWP | Wildfire (Field-Tested)'
            - MaterialSource (int): 1=市场 2=库存 3=自定义
            - WearValue (str): 浮点数字符串，例如 '0.2565198540687561'
            - Sort (int): 0..9 顺序
        cookies: 认证凭据，支持以下形式（None=用 config.ECO_WEB_COOKIE）：
            - dict: requests 原生格式
            - str: "k=v; k2=v2" 字符串
            - CookieJar: requests 兼容
        game_id: 固定 730 (CS2)，暂仅保留参数位，不参与 URL/请求体变化
        timeout: 请求超时秒数；None=用 config.ECO_WEB_START_SIM_TIMEOUT
        headers: 额外 HTTP 头（会覆盖同名默认头）；一般不需要
        use_cache: True=命中相同材料集合时复用缓存；False=强制请求

    Returns:
        dict: ResultData（已剥 StatusCode/StatusData 两层），键：
            ResultFromWeaponBoxs / MaterialFromWeaponBoxs /
            EstimatedCost / CapitalPreservationRate / MaxResultImg /
            SimulationAllPrice

    Raises:
        ValueError: 参数校验失败（材料件数/缺字段/MaterialSource 非法等）
        RuntimeError: HTTP/网络/JSON/业务 ResultCode 非 0 等任何失败。
    """
    del game_id  # 当前只支持 CS2(730)，请求体写死，参数留位

    _validate_materials(materials)

    # ---- 规范化 cookies ----
    cookie_dict = _normalize_cookies(cookies)

    # ---- 规范化材料（保证每项有缺省 Sort / 数字类型归一化）----
    norm_materials = []
    for i, m in enumerate(materials):
        sort = int(m.get("Sort", i) if m.get("Sort") not in (None, "") else i)
        mat_src = int(m.get("MaterialSource") or SOURCE_STOCK)
        norm_materials.append({
            "HashName": (m.get("HashName") or "").strip(),
            "MaterialSource": mat_src,
            "WearValue": _clamp_wear_to_tier(
                (m.get("HashName") or "").strip(),
                m.get("WearValue"),
                skin_min=m.get("SkinMinFloat"),
                skin_max=m.get("SkinMaxFloat")),
            "Sort": sort,
        })
    # 按 Sort 稳定排序，使得同一组材料无论传入顺序如何，cache_key 相同
    norm_materials.sort(key=lambda x: x["Sort"])

    # 若 headers 有额外覆盖，为了简单起见不参与缓存 key（一般场景不需要）
    if headers:
        logger.debug("start_simulation_web 传入自定义 headers，将覆盖默认头。")
        # 在缓存层中，headers 不参与 key；但真实请求里要 merge
        # 这里走非缓存路径以避免冲突
        use_cache = False

    if timeout is None:
        timeout = ECO_WEB_START_SIM_TIMEOUT

    cache_key = _material_cache_key(norm_materials)

    if use_cache:
        try:
            return _start_simulation_cached(
                cache_key,
                json.dumps(norm_materials, ensure_ascii=False, sort_keys=True),
                json.dumps(cookie_dict, ensure_ascii=False, sort_keys=True),
                float(timeout),
                ECO_WEB_START_SIM_URL,
            )
        except RuntimeError:
            raise
        except Exception as e:
            raise RuntimeError(f"StartSimulation 缓存调用异常: {e}") from e

    # ---- 非缓存路径：直接请求（合并自定义 headers）----
    full_headers = {
        "User-Agent": _DEFAULT_UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Content-Type": "application/json;charset=UTF-8",
        "Origin": "https://www.ecosteam.cn",
        "Referer": "https://www.ecosteam.cn/forge/recipe/create",
    }
    if headers:
        full_headers.update(headers)
    try:
        resp = requests.post(
            ECO_WEB_START_SIM_URL,
            json={"Materials": norm_materials, "GameId": 730},
            headers=full_headers,
            cookies=cookie_dict,
            timeout=float(timeout),
        )
    except requests.RequestException as e:
        raise RuntimeError(
            f"StartSimulation 网络错误: {e.__class__.__name__}: {e}") from e
    if resp.status_code != 200:
        preview = resp.text[:500] if resp.text else ""
        raise RuntimeError(
            f"StartSimulation HTTP {resp.status_code} != 200，响应预览: {preview!r}")
    try:
        data = resp.json()
    except (ValueError, json.JSONDecodeError) as e:
        preview = resp.text[:500] if resp.text else ""
        raise RuntimeError(
            f"StartSimulation 响应 JSON 解析失败 {e}，响应预览: {preview!r}") from e
    return _validate_and_unwrap(data, api_name="StartSimulation")


# ============================================================
# 响应 → 汰换产出骨架（不做价格计算，留待 recipe_matcher 用 price_fetcher 覆盖）
# ============================================================

def _exterior_from_property(properties: list) -> str:
    """从 ECO 响应的 Property 数组里解析外观磨损等级的 Value（中文）。

    Property 数组通常 4 条：Exterior / Type / Rarity / Quality，
    每条形如 {"Category":"Exterior","Key":"外观","Value":"久经沙场","Color":"#..."},
    或可能 Key 也是英文（不同版本前端可能不一致）—— 这里 Category/Key 都匹配一遍。
    """
    if not isinstance(properties, list):
        return ""
    for p in properties:
        if not isinstance(p, dict):
            continue
        cat = (p.get("Category") or "").lower()
        key = (p.get("Key") or "").lower()
        if "exterior" in (cat, key) or "外观" in (p.get("Category"), p.get("Key")):
            return (p.get("Value") or "").strip()
    return ""


# Steam market_hash_name 常见格式：基础名 (英文磨损等级)
# 例：'AWP | Wildfire (Field-Tested)' → 抽出 'Field-Tested'
_HASH_WEAR_RE = re.compile(r"\((Factory New|Minimal Wear|Field-Tested|"
                           r"Well-Worn|Battle-Scarred)\)\s*$")


def exterior_from_hash_name(hash_name: str) -> str:
    """从带英文磨损后缀的 HashName 中解析中文外观等级。"""
    if not hash_name:
        return ""
    mm = _HASH_WEAR_RE.search(hash_name.strip())
    if not mm:
        return ""
    from core.data_manager import wear_en_to_cn  # 延迟导入避免循环
    return wear_en_to_cn(mm.group(1))


def simulation_to_output_skeleton(result_data: dict) -> list[dict]:
    """把 StartSimulation 的 ResultData 展平成 list[产出 dict]。

    - 分组（WeaponBoxHashName）只影响显示，这里会把它的 hash 记在每条产出里。
    - OutputProbability 保留 ECO 官方精确值；不再本地做 n/10×1/k 分摊。
    - WearValue 保留 ECO 官方精确值；不再本地用 avg_input×(max-min)+min 计算。
    - ReferencePrice 仅保留原始值作参考展示，**正式价格计算一律在 recipe_matcher 里
      用 price_fetcher.query_all 替换**。

    Args:
        result_data: start_simulation_web() 返回的 ResultData dict。

    Returns:
        list[dict]，每项字段：
            hash_name (str)       : 英文完整 HashName（带英文磨损后缀）
            name_cn (str)         : 中文显示名（SPName）
            prob (float)          : 官方 OutputProbability（注意：所有组所有产出
                                    概率之和应为 1.0——若 ECO 按组分摊，
                                    这里只保留其原始数值，不二次归一）
            wear_float (float)    : 官方产出 WearValue
            wear_grade_cn (str)   : 中文磨损等级（优先取 Property.Exterior，
                                    兜底从 hash_name 英文后缀反推）
            reference_price (float) : ECO 返回的参考价（仅展示，**不用于计算**）
            collection_hash (str) : 所属 WeaponBoxHashName（收藏品 Hash）
            collection_name (str) : 所属 WeaponBoxName（收藏品中文名）
            rarity (str)          : 稀有度（如 '隐秘'/'保密' 等中文）
            sub_type_name (str)   : 子类名（如 '步枪'/'手枪' 等）
            image (str)           : GoodsImg
    """
    outputs = []
    weapon_boxes = result_data.get("ResultFromWeaponBoxs") or []
    if not isinstance(weapon_boxes, list):
        return outputs
    for box in weapon_boxes:
        if not isinstance(box, dict):
            continue
        col_hash = (box.get("WeaponBoxHashName") or "").strip()
        col_name = (box.get("WeaponBoxName") or "").strip()
        sim_results = box.get("SimulationResults") or []
        if not isinstance(sim_results, list):
            continue
        for s in sim_results:
            if not isinstance(s, dict):
                continue
            hash_name = (s.get("HashName") or "").strip()
            name_cn = (s.get("SPName") or "").strip() or hash_name
            try:
                prob = float(s.get("OutputProbability") or 0.0)
            except (TypeError, ValueError):
                prob = 0.0
            try:
                wear_float = float(s.get("WearValue") or 0.0)
            except (TypeError, ValueError):
                wear_float = 0.0
            # 保留 ECO 官方返回的原始磨损字符串（如 '0.2565198540687561'，16 位小数），
            # 供 GUI 直接展示官方精度；float 转换仅用于计算
            wear_value_str = str(s.get("WearValue") or "").strip() or "0"
            try:
                ref_price = float(s.get("ReferencePrice") or 0.0)
            except (TypeError, ValueError):
                ref_price = 0.0
            # 中文磨损等级：优先 Property → 兜底 hash_name 反推
            wear_cn = _exterior_from_property(s.get("Property") or [])
            if not wear_cn:
                wear_cn = exterior_from_hash_name(hash_name)
            rarity = (s.get("Rarity") or "").strip()
            sub_type_name = (s.get("SubTypeName") or "").strip()
            image = (s.get("GoodsImg") or "").strip()
            outputs.append({
                "hash_name": hash_name,
                "name_cn": name_cn,
                "prob": prob,
                "wear_float": wear_float,
                "wear_value_str": wear_value_str,
                "wear_grade_cn": wear_cn,
                "reference_price": ref_price,
                "collection_hash": col_hash,
                "collection_name": col_name,
                "rarity": rarity,
                "sub_type_name": sub_type_name,
                "image": image,
            })
    return outputs


# ============================================================
# 用户自定义饰品（UserCustomList / CustomDelete / customBatchAdd）
# ============================================================

@functools.lru_cache(maxsize=ECO_WEB_USER_CUSTOM_CACHE_SIZE)
def _user_custom_list_cached(cookies_json: str,
                             timeout: float,
                             game_id: int) -> list | dict:
    """LRU 缓存底；参数全可哈希。"""
    return _do_request(
        "GET",
        ECO_WEB_USER_CUSTOM_LIST_URL,
        api_name="UserCustomList",
        params={"GameId": int(game_id)},
        json_body=None,
        cookies=json.loads(cookies_json) or None,
        timeout=timeout,
    )


def user_custom_list(cookies=None, game_id: int = 730,
                     timeout: float | None = None,
                     use_cache: bool = True) -> list | dict:
    """读取 ECO 平台当前用户的自定义饰品列表（含用户自定义磨损区间）。

    GET /Api/ReplaceSimulation/UserCustomList?GameId=730
    ResultData 通常是 list[dict]，每一项至少含：
      - HashName / SPName / CustomID / MinFloat / MaxFloat / Remark / Image / Rarity / ...

    Args:
        cookies: 同 start_simulation_web
        game_id: 默认 730
        timeout: 默认读 config.ECO_WEB_API_TIMEOUT
        use_cache: True=同 Cookie+game_id 走 LRU；False=强制刷新
    """
    cookie_dict = _normalize_cookies(cookies)
    if timeout is None:
        timeout = ECO_WEB_API_TIMEOUT
    if use_cache:
        return _user_custom_list_cached(
            json.dumps(cookie_dict, ensure_ascii=False, sort_keys=True),
            float(timeout),
            int(game_id),
        )
    return _do_request(
        "GET",
        ECO_WEB_USER_CUSTOM_LIST_URL,
        api_name="UserCustomList",
        params={"GameId": int(game_id)},
        json_body=None,
        cookies=cookie_dict,
        timeout=float(timeout),
    )


def custom_delete(identifiers, cookies=None, game_id: int = 730,
                  timeout: float | None = None) -> Any:
    """删除 ECO 自定义饰品。POST /Api/ReplaceSimulation/CustomDelete

    Args:
        identifiers: 待删除项，支持两种形式：
            - int/str 单个 CustomID
            - list/set，每项是 CustomID 或 dict（dict 会自动取其 CustomID/Id 字段）
        cookies/game_id/timeout: 同其它接口

    Returns:
        ResultData（多数情况为 null/空字典，成功主要看两层 StatusCode 都为 0）
    """
    if timeout is None:
        timeout = ECO_WEB_API_TIMEOUT
    # ---- 归一化 identifiers -> list[int|str] ----
    ids: list = []
    if isinstance(identifiers, (dict,)):
        # 传入了单条字典
        identifiers = [identifiers]
    if not isinstance(identifiers, (list, tuple, set)):
        identifiers = [identifiers]
    for item in identifiers:
        if isinstance(item, dict):
            v = item.get("CustomID")
            if v in (None, ""):
                v = item.get("Id") or item.get("ID") or item.get("id")
            ids.append(v)
        else:
            ids.append(item)
    # 过滤空
    ids = [x for x in ids if x not in (None, "", [])]
    if not ids:
        raise ValueError("custom_delete 传入了空 identifiers，没有可删除的项")

    body = {
        "GameId": int(game_id),
        # ECO 前端一般传数组；若后端兼容单元素则仍用 list
        "CustomIds": ids if len(ids) > 1 else ids,
    }
    # 部分站点可能字段名叫 ids/IdList，这里再同时塞一份便于兼容（ECO自身多半认 CustomIds）
    return _do_request(
        "POST",
        ECO_WEB_CUSTOM_DELETE_URL,
        api_name="CustomDelete",
        params=None,
        json_body=body,
        cookies=_normalize_cookies(cookies),
        timeout=float(timeout),
    )


def _validate_custom_items(custom_items: list) -> None:
    if not isinstance(custom_items, (list, tuple)) or not custom_items:
        raise ValueError(
            "custom_batch_add custom_items 必须是非空 list/dict 列表")
    for i, item in enumerate(custom_items):
        if not isinstance(item, dict):
            raise ValueError(
                f"custom_batch_add[{i}] 必须是 dict，实际 {type(item).__name__}")
        hn = (item.get("HashName") or item.get("hash_name") or "").strip()
        if not hn:
            raise ValueError(f"custom_batch_add[{i}] 缺少 HashName")
        try:
            wmin = float(item.get("MinFloat", item.get("wear_min", 0.0)))
            wmax = float(item.get("MaxFloat", item.get("wear_max", 1.0)))
        except (TypeError, ValueError) as e:
            raise ValueError(
                f"custom_batch_add[{i}] 磨损区间必须是数字: {e}") from e
        if not (0.0 <= wmin < wmax <= 1.0):
            raise ValueError(
                f"custom_batch_add[{i}] 磨损区间非法: MinFloat={wmin} MaxFloat={wmax}")


def custom_batch_add(custom_items: list, cookies=None, game_id: int = 730,
                     timeout: float | None = None) -> Any:
    """批量写入用户自定义饰品。POST /Api/ReplaceSimulation/customBatchAdd

    custom_items 每项字段（兼容中英文 key）：
        - HashName / hash_name      : 英文完整 MarketHashName（带磨损后缀）
        - SPName / name_cn（可选）  : 中文显示名；不填时 ECO 可能自行回填
        - MinFloat / wear_min       : 最低磨损（float，0.0 起）
        - MaxFloat / wear_max       : 最高磨损（float，必须大于 MinFloat 且 <=1.0）
        - Remark / remark（可选）   : 备注（例：来自自动汰换面板 槽位 3）
        - Image / image（可选）

    Returns:
        ResultData（通常含成功条数 / 新 CustomID 映射，具体字段以 ECO 实际返回为准；
        调用方只要"未抛异常即视为成功"）
    """
    _validate_custom_items(custom_items)
    if timeout is None:
        timeout = ECO_WEB_API_TIMEOUT
    payload = []
    for it in custom_items:
        wmin = float(it.get("MinFloat", it.get("wear_min", 0.0)))
        wmax = float(it.get("MaxFloat", it.get("wear_max", 1.0)))
        payload.append({
            "HashName": (it.get("HashName") or it.get("hash_name") or "").strip(),
            "SPName": (it.get("SPName") or it.get("name_cn") or "").strip(),
            "MinFloat": wmin,
            "MaxFloat": wmax,
            "Remark": (it.get("Remark") or it.get("remark") or "").strip(),
            "Image": (it.get("Image") or it.get("image") or "").strip(),
            "GameId": int(game_id),
        })
    body = {"GameId": int(game_id), "List": payload}
    # 写操作一律不缓存（避免添加后读列表仍命中旧值）
    return _do_request(
        "POST",
        ECO_WEB_CUSTOM_BATCH_ADD_URL,
        api_name="customBatchAdd",
        params=None,
        json_body=body,
        cookies=_normalize_cookies(cookies),
        timeout=float(timeout),
    )


# ============================================================
# 保存配方 / 读取配方详情
# ============================================================

def save_simulation_result(simulation_result_data_or_payload,
                           cookies=None,
                           game_id: int = 730,
                           timeout: float | None = None,
                           formula_name: str | None = None) -> dict:
    """保存 StartSimulation 结果为“我的配方”，返回至少含 FormulaID。

    POST /Api/ReplaceSimulation/SaveSimulationResult

    Args:
        simulation_result_data_or_payload:
            两种形式都支持：
              1) dict=start_simulation_web 返回的 ResultData（函数内自动组装为
                 ECO 前端 SaveSimulationResult 所要求的 Payload）；
              2) dict=直接传完整请求体（含 Materials/Outputs/Name 等字段，
                 此时除 formula_name 外其他参数不做改写）。
            识别方式：若 dict 同时包含 'ResultFromWeaponBoxs' 与
            'MaterialFromWeaponBoxs' 两个键，则按 (1) 处理；否则按 (2) 处理。
        formula_name: 保存后的配方名；None=自动生成形如 'AutoSave-YYYYMMDD-HHMMSS'
        cookies/game_id/timeout: 同上

    Returns:
        dict: ResultData，通常为 {'FormulaID': 'xxx', ...}
        （ECO 具体返回字段以实际为准，这里只要未抛异常即视为保存成功）
    """
    if timeout is None:
        timeout = ECO_WEB_API_TIMEOUT
    data = simulation_result_data_or_payload
    if not isinstance(data, dict):
        raise ValueError(
            "save_simulation_result 参数必须是 dict（StartSimulation ResultData 或 完整请求体）")

    def _looks_like_result_data(d: dict) -> bool:
        return ("ResultFromWeaponBoxs" in d
                or "MaterialFromWeaponBoxs" in d)

    if _looks_like_result_data(data):
        # ---- 从 ResultData 构建 SaveSimulationResult 所需字段 ----
        materials_flat = []
        m_boxes = data.get("MaterialFromWeaponBoxs") or []
        if isinstance(m_boxes, list):
            for box in m_boxes:
                if not isinstance(box, dict):
                    continue
                fms = box.get("FormulaMaterials") or []
                if isinstance(fms, list):
                    materials_flat.extend(fms)
        # 若 MaterialFromWeaponBoxs 为空（历史极端场景），留空列表即可，不抛错
        outputs_flat = simulation_to_output_skeleton(data)

        import datetime as _dt
        name = (formula_name or "").strip() or (
            "AutoSave-" + _dt.datetime.now().strftime("%Y%m%d-%H%M%S"))

        payload = {
            "GameId": int(game_id),
            "Name": name,
            "Materials": materials_flat,
            # 注意：ECO 所要求的字段可能叫 Outputs / SimulationResults；
            # 这里同时塞两份，便于匹配后端可能的字段名。
            "Outputs": outputs_flat,
            "SimulationResults": outputs_flat,
            "EstimatedCost": float(data.get("EstimatedCost") or 0.0),
            "CapitalPreservationRate": float(
                data.get("CapitalPreservationRate") or 0.0),
            "SimulationAllPrice": float(data.get("SimulationAllPrice") or 0.0),
            "ResultRaw": data,
        }
    else:
        payload = dict(data)
        payload.setdefault("GameId", int(game_id))
        if formula_name:
            payload["Name"] = formula_name

    return _do_request(
        "POST",
        ECO_WEB_SAVE_SIM_RESULT_URL,
        api_name="SaveSimulationResult",
        params=None,
        json_body=payload,
        cookies=_normalize_cookies(cookies),
        timeout=float(timeout),
    ) or {}


@functools.lru_cache(maxsize=ECO_WEB_FORMULA_DETAIL_CACHE_SIZE)
def _formula_detail_cached(formula_id: str,
                           cookies_json: str,
                           game_id: int,
                           timeout: float) -> dict:
    return _do_request(
        "GET",
        ECO_WEB_FORMULA_DETAIL_URL,
        api_name="FormulaDetail",
        params={"FormulaID": formula_id, "GameId": int(game_id)},
        json_body=None,
        cookies=json.loads(cookies_json) or None,
        timeout=timeout,
    ) or {}


def formula_detail(formula_id: str, cookies=None, game_id: int = 730,
                   timeout: float | None = None,
                   use_cache: bool = True) -> dict:
    """读取已保存配方详情。GET ?FormulaID=xxx&GameId=730

    ResultData 通常包含：FormulaID / Name / Materials(list) / Outputs(list) /
    EstimatedCost / CapitalPreservationRate / SaveTime / ... 等字段。
    """
    if not (formula_id or "").strip():
        raise ValueError("formula_detail formula_id 不能为空")
    if timeout is None:
        timeout = ECO_WEB_API_TIMEOUT
    fid = str(formula_id).strip()
    cookie_dict = _normalize_cookies(cookies)
    if use_cache:
        return _formula_detail_cached(
            fid,
            json.dumps(cookie_dict, ensure_ascii=False, sort_keys=True),
            int(game_id),
            float(timeout),
        )
    return _do_request(
        "GET",
        ECO_WEB_FORMULA_DETAIL_URL,
        api_name="FormulaDetail",
        params={"FormulaID": fid, "GameId": int(game_id)},
        json_body=None,
        cookies=cookie_dict,
        timeout=float(timeout),
    ) or {}


# ============================================================
# 真实汰换结果对比（RealReplaceResult）
# ============================================================

@functools.lru_cache(maxsize=ECO_WEB_REAL_REPLACE_CACHE_SIZE)
def _real_replace_result_cached(formula_id: str,
                                cookies_json: str,
                                game_id: int,
                                timeout: float) -> list | dict:
    return _do_request(
        "GET",
        ECO_WEB_REAL_REPLACE_RESULT_URL,
        api_name="RealReplaceResult",
        params={"FormulaID": formula_id, "GameId": int(game_id)},
        json_body=None,
        cookies=json.loads(cookies_json) or None,
        timeout=timeout,
    )


def real_replace_result(formula_id: str | None = None,
                        *,
                        cookies=None,
                        game_id: int = 730,
                        timeout: float | None = None,
                        use_cache: bool = True,
                        **extra_params) -> list | dict:
    """GET /Api/ReplaceDiagnosis/RealReplaceResult 真实汰换结果。

    常见的参数形式（抓包未确认时，默认使用 FormulaID + GameId）：
      - FormulaID: 保存配方后的 FormulaID（调用方可传 None 不传该参数，
                   并通过 extra_params 指定其它参数，如 ReplaceId 等）
      - extra_params: 额外 query 参数，键值会合并到请求 URL 上。

    ResultData 通常是 list[dict]，每项描述一次真实汰换执行：
      - 实际投入 10 件材料 / 实际产出 1 件 HashName & WearValue / 执行时间 等。
    具体字段名以 ECO 返回为准；调用方通过 recipe_matcher.real_replace_compare()
    做统一解析 + price_fetcher 重算成本/期望。
    """
    if timeout is None:
        timeout = ECO_WEB_API_TIMEOUT
    params = {"GameId": int(game_id)}
    if (formula_id or "").strip():
        params["FormulaID"] = str(formula_id).strip()
    if extra_params:
        for k, v in extra_params.items():
            if v is None:
                continue
            params[k] = v
    cookie_dict = _normalize_cookies(cookies)
    if use_cache:
        # 缓存 key 包含所有 query 参数（排序）
        params_key = json.dumps(params, ensure_ascii=False, sort_keys=True)
        return _real_replace_result_cached_via_params(
            params_key,
            json.dumps(cookie_dict, ensure_ascii=False, sort_keys=True),
            float(timeout),
        )
    return _do_request(
        "GET",
        ECO_WEB_REAL_REPLACE_RESULT_URL,
        api_name="RealReplaceResult",
        params=params,
        json_body=None,
        cookies=cookie_dict,
        timeout=float(timeout),
    )


@functools.lru_cache(maxsize=ECO_WEB_REAL_REPLACE_CACHE_SIZE)
def _real_replace_result_cached_via_params(params_json: str,
                                           cookies_json: str,
                                           timeout: float) -> list | dict:
    params = json.loads(params_json)
    return _do_request(
        "GET",
        ECO_WEB_REAL_REPLACE_RESULT_URL,
        api_name="RealReplaceResult",
        params=params,
        json_body=None,
        cookies=json.loads(cookies_json) or None,
        timeout=timeout,
    )


# ============================================================
# Playwright 弹出浏览器登录（网页登录后自动抓取 Cookie）
# ============================================================

ECO_HOME_URL = "https://www.ecosteam.cn/forge/recipe/create"

# 独立 user_data_dir（与 C5/Buff 的 Playwright 实例互不抢锁）
_ECO_PLAYWRIGHT_USER_DIR = (
    Path(__file__).resolve().parent.parent
    / "data" / ".eco_auth" / "eco_browser_userdata"
)


def _playwright_available() -> bool:
    try:
        import playwright  # noqa: F401
        return True
    except ImportError:
        return False


def login_with_playwright(timeout_seconds: int = 600) -> dict:
    """弹出 Edge 等待用户登录 ECO，返回抓到的完整 Cookie dict。

    流程（同 c5_auth / buff_auth 的 Playwright 登录）：
      1. persistent context 打开 Edge（独立 user_data_dir，登录一次后长期复用，
         不受 Edge 运行锁 / app-bound 加密影响）
      2. 导航到汰换创建页（未登录会自动跳转登录页）
      3. 已登录（上次登录过）→ 三键直接可读，立即返回
      4. 未登录 → 等待用户完成登录（轮询 cookie，连续 2 次确认
         clientId/refreshToken/loginToken 三键齐全；loginToken 只在登录
         成功后出现）
      5. 返回 ecosteam.cn 域的全部 cookie（含 HMACCOUNT/acw_tc 等风控键）

    :return: cookie dict（键 -> 值），至少包含必填 3 键
    :raises RuntimeError: 未安装 playwright / 超时未登录 / 浏览器启动失败
    """
    if not _playwright_available():
        raise RuntimeError(
            "未安装 playwright，无法弹出登录浏览器。\n"
            "请先执行：\n"
            "    pip install playwright\n"
            "    playwright install chromium\n"
            "然后重新尝试。\n"
            "提示：不想装 playwright 的话，也可以用「🌐 打开 ECO 登录页」+ "
            "手动粘贴三段 Cookie。")
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        raise RuntimeError(f"playwright 导入失败: {e}") from e

    _ECO_PLAYWRIGHT_USER_DIR.mkdir(parents=True, exist_ok=True)

    def _extract_eco_cookies(cookies_list) -> dict:
        """从 ctx.cookies() 提取 ecosteam.cn 域的全部 cookie（含风控 cookie）。"""
        result: dict = {}
        for c in cookies_list:
            name = c.get("name")
            domain = c.get("domain") or ""
            if not name or "ecosteam.cn" not in domain:
                continue
            v = c.get("value")
            if v:
                result[name] = v
        return result

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(_ECO_PLAYWRIGHT_USER_DIR),
            headless=False,
            channel="msedge",
        )
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(ECO_HOME_URL, wait_until="domcontentloaded",
                      timeout=60_000)

            deadline = time.time() + timeout_seconds
            last_seen: dict = {}
            confirm_count = 0

            def _has_required(d: dict) -> bool:
                return all(str(d.get(k) or "").strip()
                           for k in _ECO_WEB_REQUIRED_COOKIE_KEYS)

            while time.time() < deadline:
                snapshot = _extract_eco_cookies(ctx.cookies())
                if _has_required(snapshot):
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

            if not _has_required(last_seen):
                missing = [k for k in _ECO_WEB_REQUIRED_COOKIE_KEYS
                           if not str(last_seen.get(k) or "").strip()]
                raise RuntimeError(
                    f"等待登录超时（{timeout_seconds}s），三键未齐全"
                    f"（缺 {missing}）。\n"
                    "请在弹出的浏览器中完成 ECO 登录后再试。")

            logger.info(
                "[ECO-LOGIN] Playwright 登录成功，抓到 %d 条 ecosteam.cn cookie"
                "（loginToken=%s…）", len(last_seen),
                str(last_seen.get("loginToken"))[:8])
            return last_seen
        finally:
            try:
                ctx.close()
            except Exception:
                pass
