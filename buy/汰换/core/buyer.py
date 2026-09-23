"""购买执行：调用 buy_buff / buy_c5 / eco_client 脚本函数，并记录到数据库。"""
import logging
from vendor import buy_buff, buy_c5
from core import eco_client
from core import c5_web_api

from utils.config import C5_APP_KEY, C5_TRADE_URL
from core.data_manager import log_operation
from core import buff_auth

logger = logging.getLogger(__name__)

# ECO 购买复用同一个 Steam 交易链接
ECO_TRADE_LINK = C5_TRADE_URL


def _resolve_buff_cookies():
    """优先用 BuffAuthManager 的动态 session/csrf，找不到回退到 buy_buff.py 常量。"""
    c = buff_auth.get_session_cookies(force_refresh=False, allow_playwright=False)
    if c and c.session and c.csrf_token:
        return c.session, c.csrf_token
    return buy_buff.SESSION, buy_buff.CSRF_TOKEN


def buy_buff_item(goods_id, sell_order_id, price):
    """调用 Buff 购买接口。返回 (success, message, order_id)。

    【2026-09-11】requests + buff_auth JSON 快照购买实测返回 "Login Required"
    （快照是过期 session；查询走 Playwright 持久化 profile 登录态所以正常，
    两条通道不同源）。改为优先 Playwright 网页版购买（活登录态 + 真实
    浏览器指纹），requests 仅作回退。
    """
    # 1) Playwright 活登录态购买（与查询同一持久化浏览器）
    try:
        from core import buff_browser_query
        result = buff_browser_query.buy_via_browser(
            goods_id, sell_order_id, price, steam_id=buy_buff.STEAM_ID)
        if result is not None:
            code = result.get("code")
            if code == "OK":
                data = result.get("data") or {}
                order_id = str(data.get("transaction_id", "") or
                               data.get("order_id", "") or "")
                return True, "购买成功(网页)", order_id
            err = result.get("error", "未知错误")
            # 登录过期给明确指引
            if code == "Login Required":
                return False, (f"Buff 浏览器登录态已过期（{err}）。"
                               f"请在 GUI「Buff 登录态管理」重新扫码登录。"), ""
            return False, f"Buff 网页购买失败: {err}", ""
        logger.warning("[BUY-BUFF] Playwright 未安装，回退 requests 通道")
    except Exception as e:
        logger.warning("[BUY-BUFF] 网页版购买异常，回退 requests 通道: %s", e)

    # 2) 回退：requests + buff_auth 快照（旧通道，可能登录过期）
    try:
        session, csrf = _resolve_buff_cookies()
        result = buy_buff.buy_item(
            str(goods_id), str(sell_order_id), str(price),
            session=session, csrf_token=csrf)
        code = result.get("code")
        success = (code == "OK")
        if success:
            msg = "购买成功"
            order_id = ""
            data = result.get("data") or {}
            order_id = str(data.get("transaction_id", "") or
                           data.get("order_id", "") or "")
        else:
            msg = result.get("error", "未知错误")
            order_id = ""
        return success, msg, order_id
    except Exception as e:
        return False, f"Buff购买异常: {e}", ""


def buy_c5_item(product_id, buy_price, item_name=""):
    """C5 购买：merchant API 优先 → 无权限(820001)时走网页版 API 全自动购买。

    网页版路径（curl.exe 绕 WAF）已实单验证：preview → create → pay 余额支付。

    Args:
        product_id: C5 在售商品 ID（查询结果的 productId）
        buy_price: 购买价格（preview 价必须与它一致，否则中止）

    返回 (success, message, order_id)。
    """
    # 1. 先尝试官方 merchant API（万一以后开通了购买权限，直接成）
    try:
        result = buy_c5.normal_buy(
            C5_APP_KEY, int(product_id), float(buy_price),
            trade_url=C5_TRADE_URL)
        if result is not None:
            success = bool(result.get("success", False)) or result.get("code") == 0
            if success:
                data = result.get("data") or {}
                order_id = str(data.get("orderId", "") or
                               data.get("tradeNo", "") or "")
                return True, "购买成功(API)", order_id
            error_code = result.get("errorCode", 0)
            error_msg = result.get("errorMsg", "")
            # 820001=未开通购买权限，回退到网页版 API
            if error_code == 820001 or "权限" in str(error_msg):
                logger.info("C5 merchant API 无购买权限，走网页版 API 购买...")
            else:
                # 其他错误（如商品下架）不回退
                return False, error_msg or "未知错误", ""
    except Exception as e:
        logger.warning(f"C5 merchant API 购买异常，走网页版 API: {e}")

    # 2. 网页版 API 全自动购买（curl.exe，实单验证过）
    try:
        return c5_web_api.buy(product_id, buy_price)
    except Exception as e:
        return False, f"C5网页购买异常: {e}", ""


def buy_eco_item(goods_num, price):
    """调用 ECO 指定商品购买接口。

    返回 (success, message, order_id)。
    ECO 查询结果的 order_id 即为 GoodsNum，购买时直接传入。
    """
    try:
        result = eco_client.buy_by_goods_num(
            goods_num=str(goods_num),
            price=float(price),
            trade_link=ECO_TRADE_LINK,
        )
        code = str(result.get("ResultCode", ""))
        data = result.get("ResultData") or {}
        if code == "0":
            order_no = str(data.get("OrderNo", "") or "")
            payment = data.get("PaymentPrice", "")
            msg = f"购买成功"
            if payment:
                msg += f"(支付¥{payment})"
            return True, msg, order_no
        elif code == "7009":
            # 价格已变动，返回最新价格提示
            new_price = data.get("NewPrice", "")
            msg = f"价格已变动，最新价¥{new_price}，请重新查询"
            return False, msg, ""
        else:
            msg = result.get("ResultMsg", "") or f"ECO错误码:{code}"
            return False, msg, ""
    except Exception as e:
        return False, f"ECO购买异常: {e}", ""


def buy_item(order, item_name):
    """统一购买入口。

    :param order: 查询结果 dict（含 platform/order_id/goods_id/price/wear）
    :param item_name: 物品名称（用于日志记录）
    :return: (success, message, order_id)
    """
    platform = order["platform"]
    if platform == "buff":
        success, msg, order_id = buy_buff_item(
            order.get("goods_id", ""), order["order_id"], order["price"])
    elif platform == "c5":
        success, msg, order_id = buy_c5_item(
            order["order_id"], order["price"], item_name=item_name)
    elif platform == "eco":
        success, msg, order_id = buy_eco_item(
            order["order_id"], order["price"])
    else:
        return False, f"未知平台: {platform}", ""

    log_operation(
        item_name=item_name,
        wear=order.get("wear", 0),
        platform=platform,
        price=order.get("price", 0),
        quantity=1,
        op_type="购买",
        status="成功" if success else "失败",
        order_id=order_id,
        remark=msg,
    )
    return success, msg, order_id
