import requests
import time
import random
from typing import Optional, Dict, Any

def normal_buy(
    app_key: str,
    product_id: int,
    buy_price: float,
    out_trade_no: Optional[str] = None,
    trade_url: str = "https://www.c5game.com",
    timeout: int = 10
) -> Optional[Dict[str, Any]]:
    """
    普通购买接口

    :param app_key: 您的 app-key
    :param product_id: 在售商品 ID（即查询结果中的 productId / sell_order_id）
    :param buy_price: 购买价格（必须与商品当前价格一致，否则可能失败）
    :param out_trade_no: 商户单号（可选，不传则自动生成）
    :param trade_url: 交易链接（文档要求必填，可填默认值或回调地址）
    :param timeout: 请求超时秒数
    :return: API 返回的 JSON 数据，失败返回 None
    """
    url = "https://openapi.c5game.com/merchant/trade/v2/normal-buy"
    params = {"app-key": app_key}
    headers = {"Content-Type": "application/json"}

    # 若未传入商户单号，自动生成（时间戳+随机数，保证唯一）
    if out_trade_no is None:
        out_trade_no = f"BUY{int(time.time() * 1000)}{random.randint(1000, 9999)}"

    payload = {
        "outTradeNo": out_trade_no,
        "tradeUrl": trade_url,
        "productId": product_id,
        "buyPrice": buy_price
    }

    try:
        resp = requests.post(url, params=params, json=payload, headers=headers, timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.RequestException as e:
        print(f"购买请求失败: {e}")
        return None