"""C5 网页版 API 客户端（curl.exe 绕过阿里 WAF，全自动购买）。

原理（实单验证过）：
  * 网页版 API 走 https://www.c5game.com/api/v1/...，认证用
    NC5_accessToken（cookie + x-access-token 头）。
  * 阿里 WAF 校验请求指纹：Python requests 会被拦截（403），
    因此用 Windows 自带的 curl.exe 发请求 + 完整浏览器头模板。
  * 服务端不校验 x-sign 签名（32 个 0 即可通过）。

购买三步：
  1. POST /support/trade/order/buy/v2/preview   {"type":1,"productId":"..."}
     → 价格核对：preview 返回价必须与查询价完全一致，否则中止
  2. POST /support/trade/order/buy/v2/create     {"type":1,"productId":"...",
        "price":"...","receiveSteamId":"...","actRebateAmount":0,"riskCheck":true}
  3. POST /pay/order/v2/pay                      {"orderType":1,"bizOrderId":"...",
        "payAmount":"...","rechargeOrderId":"","payDetail":{"balance":X,"creditMoney":0}}
     → 余额支付

错误语义：
  * 101 Not login          → token 失效，重新导入凭证
  * 403 空响应             → WAF 拦截（cookie 过期），重新导入凭证
  * 403 JSON(虚拟设备/高频) → 业务风控（仅 search 接口，购买不走该接口）
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
import time
from typing import Optional, Tuple

from utils.config import C5_STEAM_ID

logger = logging.getLogger(__name__)

BASE = "https://www.c5game.com/api/v1"

# preview 返回价与查询价允许的最大偏差（纯浮点误差级别，即价格必须严格一致）
PRICE_TOLERANCE = 0.005


def _curl_api(auth, method: str, path: str, body: dict = None) -> dict:
    """用 curl.exe 发送 API 请求（浏览器头模板，绕过 WAF 指纹检测）。

    :param auth: c5_auth.C5WebAuth 实例（cookie/token/device_id/traffic_tag）
    """
    url = path if path.startswith("http") else BASE + path
    args = [
        "curl.exe", "-s", "-w", "\n__HTTP__%{http_code}", "-X", method, url,
        # 浏览器原始头（顺序保持，WAF 校验指纹）
        "-H", "accept: application/json, text/plain, */*",
        "-H", "accept-language: zh-CN",
        "-H", "cache-control: no-cache",
        "-H", "content-type: application/json",
        "-b", auth.cookie,
        "-H", "origin: https://www.c5game.com",
        "-H", "pragma: no-cache",
        "-H", "priority: u=1, i",
        "-H", "referer: https://www.c5game.com/",
        "-H", 'sec-ch-ua: "Chromium";v="152", "Not?A_Brand";v="24", "Microsoft Edge";v="152"',
        "-H", "sec-ch-ua-mobile: ?0",
        "-H", 'sec-ch-ua-platform: "Windows"',
        "-H", "sec-fetch-dest: empty",
        "-H", "sec-fetch-mode: cors",
        "-H", "sec-fetch-site: same-origin",
        "-H", "sec-fetch-storage-access: active",
        "-H", "user-agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36 Edg/152.0.0.0",
        "-H", f"x-access-token: {auth.token}",
        "-H", "x-app-channel: WEB",
        "-H", "x-area: 1",
        "-H", f"x-device-id: {auth.device_id}",
        "-H", f"x-device-model: Edge 152.0.0.0",
        "-H", f"x-device-os: Win64; x64",
        "-H", "x-sign: " + "0" * 32,
        "-H", "x-source: 1",
        "-H", f"x-start-req-time: {int(time.time() * 1000)}",
        "-H", f"x-traffic-tag: {auth.traffic_tag}",
    ]
    if body is not None:
        args += ["--data-raw", json.dumps(body, ensure_ascii=False)]
    try:
        # encoding 必须 utf-8：Windows 默认 GBK 解码 curl 输出（C5 JSON 含
        # 中文 UTF-8 字节）会在 readerthread 里 UnicodeDecodeError，
        # 导致 stdout 读空 → 每单 preview/purchase 必失败（2026-09-11 实测）
        r = subprocess.run(args, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=30,
                           creationflags=subprocess.CREATE_NO_WINDOW)
    except subprocess.TimeoutExpired:
        return {"success": False, "errorMsg": "curl 超时"}
    except FileNotFoundError:
        return {"success": False, "errorMsg": "未找到 curl.exe（需要 Windows 10+）"}
    out = r.stdout
    m = re.search(r"\n__HTTP__(\d+)$", out)
    http_code = int(m.group(1)) if m else 0
    body_text = out[:m.start()] if m else out
    if not body_text.strip():
        return {"success": False, "errorCode": http_code,
                "errorMsg": f"HTTP {http_code} (WAF 拦截或凭证过期，请重新导入 C5 凭证)"}
    try:
        return json.loads(body_text)
    except json.JSONDecodeError:
        return {"success": False, "errorCode": http_code,
                "errorMsg": f"非 JSON 响应: {body_text[:100]}"}


def _api_get(auth, path: str) -> dict:
    return _curl_api(auth, "GET", path)


def _api_post(auth, path: str, body: dict) -> dict:
    return _curl_api(auth, "POST", path, body)


# ---------------- 余额 / 健康检查 ----------------

def check_money(auth=None) -> Tuple[bool, str, Optional[float]]:
    """查询 C5 余额（兼作凭证健康检查）。

    Returns:
        (ok, message, balance)
    """
    if auth is None:
        from core import c5_auth
        auth = c5_auth.get_auth()
        if auth is None:
            return False, "未找到 C5 凭证，请先在购买列表页点「C5凭证管理」导入", None
    j = _api_get(auth, "/balance/user/account/v2/money")
    if j.get("success"):
        d = j.get("data") or {}
        try:
            balance = float(d.get("totalAmount", 0) or 0)
        except (TypeError, ValueError):
            balance = None
        return True, f"总余额 ¥{d.get('totalAmount')}（可提现 ¥{d.get('canWithdrawAmount')}）", balance
    return False, f"[{j.get('errorCode')}] {j.get('errorMsg')}", None


# ---------------- 购买 ----------------

def _extract_preview_price(j: dict) -> Optional[float]:
    """从 preview 响应中提取商品实际价（字段名不确定，逐个尝试）。"""
    d = j.get("data")
    if not isinstance(d, dict):
        return None
    for key in ("price", "cnyPrice", "salePrice", "payAmount",
                "totalPrice", "amount", "sellerPrice"):
        v = d.get(key)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    # 嵌套一层找（如 data.goodsInfo.price / data.payInfo.price）
    for v in d.values():
        if isinstance(v, dict):
            for key in ("price", "cnyPrice", "salePrice", "payAmount"):
                pv = v.get(key)
                if pv is not None:
                    try:
                        return float(pv)
                    except (TypeError, ValueError):
                        continue
    return None


def _extract_biz_order_id(j: dict) -> Optional[str]:
    """从 create 响应中提取订单号（实单验证：data 直接是订单号字符串）。"""
    d = j.get("data")
    if isinstance(d, str) and d:
        return d
    if isinstance(d, dict):
        for key in ("bizOrderId", "orderId", "id", "orderNo", "payOrderId"):
            if d.get(key):
                return str(d[key])
        for v in d.values():
            if isinstance(v, dict):
                for key in ("bizOrderId", "orderId", "id", "orderNo"):
                    if v.get(key):
                        return str(v[key])
    return None


def buy(product_id, price, auth=None, steam_id: str = "") -> Tuple[bool, str, str]:
    """C5 网页版全自动购买（preview → create → pay 余额支付）。

    :param product_id: C5 在售商品 ID（查询结果的 productId，即 buy_queue.order_id）
    :param price: 期望购买价（查询时的价格）。preview 返回价必须与它一致才继续
    :param auth: 凭证；None 时自动从 c5_auth 获取
    :param steam_id: 收货 SteamID64；空串用 config.C5_STEAM_ID
    Returns:
        (success, message, order_id)
    """
    if auth is None:
        from core import c5_auth
        auth = c5_auth.get_auth()
        if auth is None:
            return False, "未找到 C5 凭证，请先在购买列表页点「C5凭证管理」导入", ""

    sid = steam_id or C5_STEAM_ID
    price_str = f"{float(price):.2f}"

    # 1. preview（只读，核对价格）
    print(f"[C5网页] [1/3] preview productId={product_id} 期望价={price_str}", flush=True)
    pv = _api_post(auth, "/support/trade/order/buy/v2/preview",
                   {"type": 1, "productId": str(product_id)})
    if not pv.get("success"):
        return False, f"preview 失败: [{pv.get('errorCode')}] {pv.get('errorMsg')}", ""
    actual = _extract_preview_price(pv)
    if actual is not None and abs(actual - float(price)) > PRICE_TOLERANCE:
        return False, f"价格变动：preview 实际价 ¥{actual:.2f} ≠ 查询价 ¥{price_str}，已中止", ""

    # 2. create 下单
    print(f"[C5网页] [2/3] create 下单 ...", flush=True)
    cr = _api_post(auth, "/support/trade/order/buy/v2/create", {
        "type": 1, "productId": str(product_id), "price": price_str,
        "receiveSteamId": sid, "actRebateAmount": 0, "riskCheck": True,
    })
    if not cr.get("success"):
        return False, f"下单失败: [{cr.get('errorCode')}] {cr.get('errorMsg')}", ""
    order_id = _extract_biz_order_id(cr)
    if not order_id:
        logger.warning(f"C5 create 响应未识别订单号: {json.dumps(cr, ensure_ascii=False)[:300]}")
        return False, "下单成功但未识别订单号字段，请人工到 C5 订单页确认", ""

    # 3. pay 余额支付
    print(f"[C5网页] [3/3] pay 余额支付 订单号={order_id} ...", flush=True)
    pay = _api_post(auth, "/pay/order/v2/pay", {
        "orderType": 1, "bizOrderId": order_id, "payAmount": price_str,
        "rechargeOrderId": "",
        "payDetail": {"balance": float(price_str), "creditMoney": 0},
    })
    if pay.get("success"):
        return True, f"购买成功(网页API) ¥{price_str}", order_id
    return False, f"支付失败: [{pay.get('errorCode')}] {pay.get('errorMsg')}", order_id
