"""ECO 开放平台 API 客户端。

实现要点：
1. SHA256withRSA 签名（按官方文档）：参数名按 0-9a-zA-Z 排序（小写在前），
   拼成 key=value&key=value 格式，用 RSA 私钥 SHA256 签名后 Base64 编码。
2. 公共参数：PartnerId、Timestamp（秒级时间戳）、Sign。
3. 请求方式：POST application/json，Header 可带 language=1（英文）/2（中文）。
4. 接口基址：https://openapi.ecosteam.cn

主要封装：
- query_steam_stock: 查询已绑定 Steam 账号的库存（推荐，分页，返回 AssetId
  与 PaintWear、贴纸、印花等完整字段，用于游戏内定位 + 汰换方案匹配）。
- steam_inventory_query: 通过交易链接查询库存（轻量版，仅 AssetId/HashName/Price）。
- get_balance: 查询 ECO 账户余额（用于汰换前检查资金）。
"""
import json
import random
import time
import base64
import logging

import requests

try:
    # 优先使用 cryptography（PyInstaller 兼容性好）
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.backends import default_backend
    _USE_CRYPTOGRAPHY = True
except ImportError:
    _USE_CRYPTOGRAPHY = False

from vendor import eco_creds

logger = logging.getLogger(__name__)

ECO_API_BASE = "https://openapi.ecosteam.cn"
ECO_QUERY_STOCK_PATH = "/Api/Selling/QueryStock"
ECO_STEAM_INVENTORY_QUERY_PATH = "/Api/open/purchase/SteamInventoryQuery"
ECO_GET_BALANCE_PATH = "/Api/Account/GetBalance"
ECO_QUERY_BIND_STEAM_PATH = "/Api/User/QueryBindSteam"
ECO_SELL_GOODS_LIST_PATH = "/Api/Market/SellGoodsList"
ECO_BUY_BY_GOODS_NUM_PATH = "/Api/open/buy/BuyByGoodsNum"

# 请求超时（秒）
REQUEST_TIMEOUT = 15

# ============================================================
# 【ECO 开放平台全局节流锁】—— 解决 ResultCode=6001 "接口请求频次过快"：
#   用户 10 件同收藏、同皮肤材料 → 产出里同一件 (hash_name, wear_min, wear_max)
#   会在 query_all 被 ThreadPoolExecutor(10+) 并发重复发 → ECO 同一秒 10+ 条
#   直接 6001，3 档降级时请求数 ×3 → 更爆炸。
#   做法：**所有 _post（openapi.ecosteam.cn 的全部接口）共享一把 Lock 串行发**，
#         且两条请求 monotonic 时间差必须 >= ECO_OPENAPI_REQUEST_INTERVAL；
#         一旦拿到 6001，立即指数退避（1.5s / 3s / 6s ...）后重试一次，
#         避免 6001 → 立刻再发 → 更严限流 的恶性循环。
# ============================================================
import threading as _eco_th

try:
    from utils.config import ECO_OPENAPI_REQUEST_INTERVAL as _CFG_ECO_INTERVAL
except Exception:   # 老版本 config.py 没加这个常量时的兜底
    _CFG_ECO_INTERVAL = 1.2   # 1.2s 对普通合作方账号最稳妥（≈50 次/分钟）

_ECO_REQ_LOCK = _eco_th.Lock()
_ECO_LAST_REQ_TS: float = 0.0   # time.monotonic()

# GUI 运行时覆盖（优先）：如果 user_settings 有 eco_openapi_request_interval_s，
# 用那个值，否则回退 config.ECO_OPENAPI_REQUEST_INTERVAL。
# 延迟 import，避免模块初始化循环依赖。
def _get_effective_eco_interval() -> float:
    try:
        from core import user_settings as _us
        val = getattr(_us, "get_eco_openapi_request_interval", None)
        if callable(val):
            v = val(_CFG_ECO_INTERVAL)
            try: return max(0.0, min(30.0, float(v)))
            except (TypeError, ValueError): return float(_CFG_ECO_INTERVAL)
    except Exception:
        pass
    try: return max(0.0, min(30.0, float(_CFG_ECO_INTERVAL)))
    except (TypeError, ValueError): return 1.2


# ============================================================
# RSA 签名
# ============================================================
def _load_private_key_pkcs8(pem_b64: str):
    """加载 PKCS8 格式 RSA 私钥（去头尾、去换行的纯Base64字符串）。"""
    der = base64.b64decode(pem_b64)
    if _USE_CRYPTOGRAPHY:
        return serialization.load_der_private_key(
            der, password=None, backend=default_backend())
    else:
        # 退化到 pycryptodome（rsa_crypt.py 风格）
        from Crypto.PublicKey import RSA
        return RSA.import_key(der)


def _sign_with_cryptography(private_key, message: str) -> str:
    """用 cryptography 库进行 SHA256withRSA 签名。"""
    sig = private_key.sign(
        message.encode("utf-8"),
        padding.PKCS1v15(),
        hashes.SHA256(),
    )
    return base64.b64encode(sig).decode("ascii")


def _sign_with_pycryptodome(private_key, message: str) -> str:
    """用 pycryptodome 进行 SHA256withRSA 签名。"""
    from Crypto.Hash import SHA256
    from Crypto.Signature import pkcs1_15
    h = SHA256.new(message.encode("utf-8"))
    sig = pkcs1_15.new(private_key).sign(h)
    return base64.b64encode(sig).decode("ascii")


def _build_sign_string(params: dict) -> str:
    """按 ECO 规则拼接待签名字符串。

    规则：
    1. 对所有参数（除 Sign）按参数名 0-9a-zA-Z 排序（小写字母在前）。
    2. 若值是 list/dict，序列化为紧凑 JSON（无多余空格/换行）。
    3. 拼成 key=value&key=value 格式。
    """
    def sort_key(k: str):
        # ECO 文档排序规则：0-9 在前，然后小写字母，然后大写字母
        # 标准做法是按 ASCII 升序（数字<大写<小写），但文档示例显示 bar<foo<foo_bar<foobar
        # 实际上 ECO 用的是 str.lower 比较 + 大小写敏感回退
        return k.lower()

    items = []
    for key in sorted(params.keys(), key=sort_key):
        if key == "Sign":
            continue
        value = params[key]
        if isinstance(value, (dict, list)):
            value_str = json.dumps(value, ensure_ascii=False,
                                   separators=(",", ":"))
        else:
            value_str = str(value) if value is not None else ""
        if value_str == "":
            continue
        items.append(f"{key}={value_str}")
    return "&".join(items)


def generate_sign(params: dict) -> str:
    """生成 ECO API 签名。params 不应包含 Sign 字段。"""
    if not eco_creds.ECO_RSA_PRIVATE_KEY:
        raise RuntimeError("ECO RSA 私钥未配置，请在 vendor/eco_creds.py 填入。")

    message = _build_sign_string(params)
    private_key = _load_private_key_pkcs8(eco_creds.ECO_RSA_PRIVATE_KEY)

    if _USE_CRYPTOGRAPHY:
        return _sign_with_cryptography(private_key, message)
    else:
        return _sign_with_pycryptodome(private_key, message)


# ============================================================
# 通用请求
# ============================================================
def _build_request_params(business_params: dict) -> dict:
    """注入公共参数并签名。"""
    params = dict(business_params)
    params["PartnerId"] = eco_creds.ECO_PARTNER_ID
    params["Timestamp"] = str(int(time.time()))
    params["Sign"] = generate_sign(params)
    return params


def _post(path: str, business_params: dict, language: str = "2",
          *, _retry_attempt: int = 0) -> dict:
    """发送 POST 请求。返回 JSON dict。校验 ResultCode=="0"，否则抛 RuntimeError。

    Args:
        path: 接口路径，如 /Api/Selling/QueryStock
        business_params: 业务参数（不含公共参数）
        language: 1=English, 2=中文
        _retry_attempt: 内部字段（6001 自动重试次数标记，不要传）。
                        第 0 次正常发；拿到 6001 后指数退避 1.5s×(1.5^n) 后最多再重发一次。
    """
    global _ECO_LAST_REQ_TS
    tname = _eco_th.current_thread().name
    # ============== 全局节流门（Lock + monotonic 间隔） ==============
    interval = _get_effective_eco_interval()
    with _ECO_REQ_LOCK:
        now = time.monotonic()
        if _ECO_LAST_REQ_TS > 0.0 and interval > 0:
            wait_s = max(0.0, interval - (now - _ECO_LAST_REQ_TS))
            if wait_s > 0:
                # 0~0.5s 随机震荡：避免固定间隔的请求指纹被风控识别（6001）
                jitter = random.uniform(0.0, 0.5)
                logger.debug(
                    "[ECO-OPENAPI-GATE] 💤 thread=%s path=%s 补 sleep=%.3fs+jitter%.3fs "
                    "(上一条 %+.3fs 前, 需要 ≥%.2fs 间隔)",
                    tname, path, wait_s, jitter, now - _ECO_LAST_REQ_TS, interval)
                time.sleep(wait_s + jitter)
        # 更新最后请求时间戳（无论成功与否），避免多线程连续撞
        _ECO_LAST_REQ_TS = time.monotonic()
        # ============ 真正发 HTTP ============
        url = ECO_API_BASE + path
        params = _build_request_params(business_params)
        headers = {
            "Content-Type": "application/json; charset=UTF-8",
            "language": language,
        }
        pub_params = {k: v for k, v in params.items() if k != "Sign"}
        logger.info("ECO POST %s start params=%s (attempt #%d)",
                    path, pub_params, _retry_attempt + 1)
        try:
            resp = requests.post(url, json=params, headers=headers,
                                 timeout=REQUEST_TIMEOUT)
        except requests.RequestException as e:
            raise RuntimeError(
                f"ECO API {path} 网络错误: {type(e).__name__}: {e}") from e
        try:
            resp.raise_for_status()
        except requests.HTTPError as e:
            if resp.status_code == 429 and _retry_attempt < 2:
                backoff = 1.5 * (1.6 ** _retry_attempt)
                logger.warning(
                    "[ECO-OPENAPI-GATE] HTTP 429 path=%s 退避 %.1fs 后自动重试 "
                    "(attempt #%d → #%d)",
                    path, backoff, _retry_attempt + 1, _retry_attempt + 2)
                time.sleep(backoff)
                _ECO_LAST_REQ_TS = time.monotonic()   # 重试前再更新一次，保证
                return _post(path, business_params, language=language,
                             _retry_attempt=_retry_attempt + 1)
            raise RuntimeError(
                f"ECO API {path} HTTP {resp.status_code} != 200："
                f"{resp.text[:400]}") from e
        try:
            data = resp.json()
        except ValueError as e:
            raise RuntimeError(
                f"ECO API {path} 响应非 JSON (HTTP {resp.status_code})：{e}") from e
        code = str(data.get("ResultCode", ""))
        msg  = str(data.get("ResultMsg", ""))
        if code == "0":
            logger.info("ECO POST %s done ResultCode=%s ResultMsg=%s (attempt #%d)",
                        path, code, msg or "(ok)", _retry_attempt + 1)
            return data
        # ============== 非 0：ResultCode=6001 自动指数退避重试一次 ==============
        #   ECO 的 6001 是"请求频次过快"，业务完全没执行，直接退避后重发是安全的。
        if code == "6001" and _retry_attempt < 2:
            backoff = 1.5 * (1.5 ** _retry_attempt)
            logger.warning(
                "[ECO-OPENAPI-GATE] %s 拿到 ResultCode=6001 (%s)，退避 %.1fs 自动重试 "
                "(attempt #%d → #%d)，hash_name=%s",
                path, msg, backoff, _retry_attempt + 1, _retry_attempt + 2,
                business_params.get("HashName", "(N/A)"))
            time.sleep(backoff)
            _ECO_LAST_REQ_TS = time.monotonic()
            return _post(path, business_params, language=language,
                         _retry_attempt=_retry_attempt + 1)
        # 其他错误 / 6001 重试后仍然不是 0：抛出给上层（query_eco 的 3 档降级）
        raise RuntimeError(
            f"ECO API {path} 返回错误 ResultCode={code!r} ResultMsg={msg!r} "
            f"params={pub_params} (attempt #{_retry_attempt + 1})")


# ============================================================
# 业务接口
# ============================================================
def query_steam_stock(page_index: int = 1, page_size: int = 100,
                      steam_id: str = None, game_id: str = None,
                      stock_status: int = -1) -> dict:
    """查询已绑定 Steam 账号的库存（推荐接口）。

    POST /Api/Selling/QueryStock

    Args:
        page_index: 当前页，从 1 开始
        page_size: 每页条数，单次最大 100
        steam_id: SteamId（可选，留空则查当前 Partner 绑定账号）
        game_id: Steam 游戏ID，CS2=730
        stock_status: 库存状态 -1全部 1待上架 2冷却中 3上架中
                      4不可交易 5交易中

    Returns:
        {
          "ResultCode": "0",
          "ResultMsg": "成功",
          "ResultData": {
            "PageIndex": 1, "PageSize": 100, "TotalRecord": 123,
            "PageResult": [ {StockId, AssetId, HashName, GoodsName,
                             PaintWear, PaintSeed, PaintIndex,
                             Stickers, Keychains, Tradable, ...} ]
          }
        }
    """
    params = {
        "PageIndex": page_index,
        "PageSize": min(max(page_size, 1), 100),
        "SteamId": steam_id or eco_creds.STEAM_ID,
        "GameId": game_id or eco_creds.CS2_GAME_ID,
        "StockStatus": stock_status,
    }
    return _post(ECO_QUERY_STOCK_PATH, params)


def steam_inventory_query(trade_partner: str = None,
                          trade_token: str = None,
                          game_id: str = None) -> dict:
    """通过交易链接查询 Steam 库存（轻量版）。

    POST /Api/open/purchase/SteamInventoryQuery

    返回的 PageResult 仅含 GameId/AssetId/HashName/Price 四个字段。
    本项目主要使用 query_steam_stock（返回字段更全，含磨损和贴纸）。
    """
    params = {
        "TradeToken": trade_token or eco_creds.STEAM_TRADE_TOKEN,
        "TradePartner": trade_partner or eco_creds.STEAM_TRADE_PARTNER,
        "GameId": game_id or eco_creds.CS2_GAME_ID,
    }
    return _post(ECO_STEAM_INVENTORY_QUERY_PATH, params)


def get_balance() -> dict:
    """查询 ECO 账户当前可用余额。"""
    return _post(ECO_GET_BALANCE_PATH, {})


def query_bind_steam(page_index: int = 1, page_size: int = 20) -> dict:
    """查询已绑定的 Steam 账号列表（含 SteamId）。"""
    params = {
        "PageIndex": page_index,
        "PageSize": page_size,
    }
    return _post(ECO_QUERY_BIND_STEAM_PATH, params)


# ============================================================
# 分页拉取完整库存
# ============================================================
def fetch_all_steam_stock(steam_id: str = None,
                          game_id: str = None,
                          stock_status: int = -1,
                          on_progress=None) -> list:
    """分页拉取全部库存，返回 PageResult 列表。

    Args:
        on_progress: 可选回调 (page, total) -> None
    """
    all_items: list[dict] = []
    page = 1
    while True:
        data = query_steam_stock(
            page_index=page, page_size=100,
            steam_id=steam_id, game_id=game_id,
            stock_status=stock_status)
        result = data.get("ResultData") or {}
        page_result = result.get("PageResult") or []
        total = result.get("TotalRecord", 0)
        all_items.extend(page_result)
        if on_progress:
            on_progress(page, total)
        if not page_result or len(all_items) >= total:
            break
        page += 1
        # 安全上限：100 页
        if page > 100:
            logger.warning("库存分页超过 100 页，已截断。")
            break
        time.sleep(0.25)  # 礼貌限速
    return all_items


# ============================================================
# 查询在售商品列表（用于比价）
# ============================================================
def query_sell_goods_list(hash_name: str,
                          game_id: str = "730",
                          page_index: int = 1,
                          page_size: int = 50,
                          start_paint_wear: float = None,
                          end_paint_wear: float = None,
                          good_range: int = 1,
                          is_auto_shipping: bool = None,
                          is_first: bool = None) -> dict:
    """查询指定商品模板下的在售/预售商品列表。

    POST /Api/Market/SellGoodsList

    Args:
        hash_name: Steam 市场哈希名（必填），如 'AK-47 | Redline (Field-Tested)'
        game_id: Steam 游戏ID，CS2=730
        page_index: 页码，从1开始
        page_size: 每页条数，单次最大100
        start_paint_wear: 磨损最小值（>0生效）
        end_paint_wear: 磨损最大值（>0生效）
        good_range: 1=在售商品, 2=预售商品
        is_auto_shipping: 是否自动发货。
                          【重要】None(默认)=不传该参数=查全部在售（与官网一致）。
                          True=只查自动发货商品，实测会漏掉绝大多数挂单
                          （如 SSG08手刹FT：自动发货 0 条 vs 全部 227 条），
                          仅在明确只想买自动发货商品时才传 True。
        is_first: 是否极速发货（None=不限）

    Returns:
        {
          "ResultCode": "0",
          "ResultMsg": "成功",
          "ResultData": {
            "PageIndex": 1, "PageSize": 50, "TotalRecord": N,
            "PageResult": [ {GoodsNum, HashName, GoodsName, SellingPrice,
                             PaintWear, PaintSeed, PaintIndex, ...} ]
          }
        }
    """
    if not isinstance(hash_name, str) or not hash_name.strip():
        raise RuntimeError(f"SellGoodsList：hash_name 不能为空或非字符串 {hash_name!r}")
    params = {
        "GameId": game_id,
        "HashName": hash_name,
        "PageIndex": page_index,
        "PageSize": min(max(page_size, 1), 100),
        "GoodRange": good_range,
    }
    # IsAutoShipping 仅在显式指定时才传：不传=全部在售（含非自动发货），
    # 传 True 会把结果限制为自动发货商品（数量极少，不代表市场真实供给）。
    if is_auto_shipping is not None:
        params["IsAutoShipping"] = is_auto_shipping
    if start_paint_wear is not None and start_paint_wear > 0:
        params["StartPaintWear"] = start_paint_wear
    if end_paint_wear is not None and end_paint_wear > 0:
        params["EndPaintWear"] = end_paint_wear
    if is_first is not None:
        params["IsFirst"] = is_first
    data = _post(ECO_SELL_GOODS_LIST_PATH, params)
    rd = data.get("ResultData") or {}
    total = int(rd.get("TotalRecord") or 0)
    items = rd.get("PageResult") or []
    logger.info(
        "ECO SellGoodsList hash_name=%s game=%s page=%d TotalRecord=%d returned=%d "
        "wear=[%s,%s]",
        hash_name, game_id, page_index, total, len(items),
        params.get("StartPaintWear"), params.get("EndPaintWear"))
    return data


def fetch_all_sell_goods(hash_name: str,
                         game_id: str = "730",
                         start_paint_wear: float = None,
                         end_paint_wear: float = None,
                         max_pages: int = 5,
                         on_progress=None) -> list:
    """分页拉取指定商品的全部在售列表，返回 PageResult 列表。

    Args:
        max_pages: 最大页数安全限制（每页100条）
        on_progress: 回调 (page, total) -> None
    """
    if not isinstance(hash_name, str) or not hash_name.strip():
        raise RuntimeError(f"fetch_all_sell_goods：hash_name 不能为空 {hash_name!r}")
    all_items: list[dict] = []
    page = 1
    last_total = 0
    while page <= max_pages:
        data = query_sell_goods_list(
            hash_name=hash_name,
            game_id=game_id,
            page_index=page,
            page_size=100,
            start_paint_wear=start_paint_wear,
            end_paint_wear=end_paint_wear,
        )
        result = data.get("ResultData") or {}
        page_result = result.get("PageResult") or []
        total = int(result.get("TotalRecord") or 0)
        last_total = total
        all_items.extend(page_result)
        if on_progress:
            on_progress(page, total)
        if not page_result or len(all_items) >= total:
            break
        page += 1
        time.sleep(0.3)
    logger.info(
        "ECO fetch_all_sell_goods DONE hash_name=%s game=%s wear=[%s,%s] "
        "TotalRecord=%d fetched=%d pages=%d",
        hash_name, game_id, start_paint_wear, end_paint_wear,
        last_total, len(all_items), page - 1)
    return all_items


# ============================================================
# 购买接口
# ============================================================
def buy_by_goods_num(goods_num: str,
                     price: float,
                     trade_link: str,
                     game_id: str = "730",
                     merchant_no: str = None) -> dict:
    """指定商品购买。

    POST /Api/open/buy/BuyByGoodsNum

    Args:
        goods_num: ECO 商品编号（从 SellGoodsList 的 GoodsNum 字段获取）
        price: 商品价格（元），需与在售价格一致
        trade_link: 收货方 Steam 交易链接
        game_id: Steam 游戏ID，CS2=730
        merchant_no: 商户订单号（可选，第三方自定义，最大64字符）

    Returns:
        {
          "ResultCode": "0",  # 0=成功, 7009=价格变动需重新查询
          "ResultMsg": "成功",
          "ResultData": { "OrderNo": "...", "PaymentPrice": 0.21, ... }
        }

    常见 ResultCode:
        - "0": 成功
        - "7009": 价格已变动，ResultData.NewPrice 返回最新可购买最低价
        - 其他: 失败，看 ResultMsg
    """
    params = {
        "GoodsNum": goods_num,
        "Price": price,
        "TradeLink": trade_link,
        "GameId": game_id,
    }
    if merchant_no:
        params["MerchantNo"] = merchant_no[:64]
    return _post(ECO_BUY_BY_GOODS_NUM_PATH, params)
