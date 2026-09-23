import requests
import csv
import time
import os
import re
from typing import Optional, Dict, Any

# ---------- 工具函数 ----------
def safe_filename(name: str) -> str:
    """移除 Windows 文件名非法字符，替换为下划线"""
    return re.sub(r'[\\/:*?"<>|]', '_', name)

def get_wear_name(float_wear: float) -> str:
    """根据磨损值返回中文磨损等级"""
    if float_wear < 0.07:
        return "崭新出厂"
    elif float_wear < 0.15:
        return "略有磨损"
    elif float_wear < 0.38:
        return "久经沙场"
    elif float_wear < 0.45:
        return "破损不堪"
    else:
        return "战痕累累"

# ---------- 单页查询函数 ----------
def query_product_list(
    app_key: str,
    item_id: Optional[int] = None,
    market_hash_name: Optional[str] = None,
    app_id: Optional[int] = None,
    delivery: Optional[int] = None,
    page_num: int = 1,
    page_size: int = 20,
    asset_type: int = 1,
    timeout: int = 10
) -> Optional[Dict[str, Any]]:
    url = "https://openapi.c5game.com/merchant/market/v2/products/list"
    params = {"app-key": app_key}
    headers = {"Content-Type": "application/json"}

    payload = {}
    if item_id is not None:
        payload["itemId"] = item_id
    if market_hash_name is not None:
        payload["marketHashName"] = market_hash_name
    if app_id is not None:
        payload["appId"] = app_id
    if delivery is not None:
        payload["delivery"] = delivery
    payload["pageNum"] = page_num
    payload["pageSize"] = min(page_size, 50)
    payload["assetType"] = asset_type

    try:
        resp = requests.post(url, params=params, json=payload, headers=headers, timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.RequestException as e:
        print(f"请求失败: {e}")
        return None

# ---------- 翻页并保存 CSV ----------
def fetch_all_products_and_save_csv(
    app_key: str,
    output_dir: str = None,   # None = 默认 <项目根>/buy/data/csv/c5
    market_hash_name: Optional[str] = None,
    app_id: Optional[int] = None,
    item_id: Optional[int] = None,
    delivery: Optional[int] = None,
    asset_type: int = 1,
    page_size: int = 50,
    sleep_interval: float = 0.3,
    max_pages: int = 200
) -> None:
    """
    翻页获取所有在售商品并保存为 CSV（列名与 buff_orders_1115648.csv 统一）
    字段顺序：sell_order_id, price, paintwear, wear_name, paintseed, assetid, goods_id
    """
    if not market_hash_name and not item_id:
        print("错误：请至少提供 market_hash_name+app_id 或 item_id")
        return
    if market_hash_name and app_id is None:
        print("错误：使用 market_hash_name 时必须同时提供 app_id")
        return

    if not output_dir:
        # 默认 <项目根>/buy/data/csv/c5，不写死绝对路径
        output_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "data", "csv", "c5")
    os.makedirs(output_dir, exist_ok=True)

    # 生成文件名（无时间戳）
    if market_hash_name:
        base = safe_filename(market_hash_name)
    else:
        base = f"item_{item_id}"
    filename = f"{base}.csv"
    filepath = os.path.join(output_dir, filename)

    print(f"开始抓取，输出文件：{filepath}")
    print("注意：文件名不含时间戳，多次运行将覆盖同名文件。")

    page = 1
    total_written = 0
    has_more = True

    with open(filepath, mode='w', newline='', encoding='utf-8-sig') as f:
        writer = csv.writer(f)
        # 按 buff_orders 的列名顺序
        headers = ["sell_order_id", "price", "paintwear", "wear_name", "paintseed", "assetid", "goods_id"]
        writer.writerow(headers)

        while has_more and page <= max_pages:
            print(f"正在获取第 {page} 页...")
            result = query_product_list(
                app_key=app_key,
                item_id=item_id,
                market_hash_name=market_hash_name,
                app_id=app_id,
                delivery=delivery,
                page_num=page,
                page_size=page_size,
                asset_type=asset_type
            )

            if not result or not result.get("success"):
                error_msg = result.get("errorMsg") if result else "未知错误"
                print(f"第 {page} 页请求失败：{error_msg}，停止翻页。")
                break

            data = result.get("data", {})
            product_list = data.get("list", [])
            if not product_list:
                print(f"第 {page} 页无数据，可能已到末尾。")
                break

            rows = []
            for product in product_list:
                asset_info = product.get("assetInfo", {})
                float_wear = asset_info.get("floatWear")
                row = [
                    product.get("productId", ""),
                    product.get("price", ""),
                    float_wear if float_wear is not None else "",   # paintwear 存放磨损值
                    get_wear_name(float_wear) if float_wear is not None else "",
                    asset_info.get("paintSeed", ""),
                    asset_info.get("assetId", ""),
                    item_id if item_id is not None else ""          # goods_id
                ]
                rows.append(row)

            writer.writerows(rows)
            total_written += len(rows)
            print(f"第 {page} 页写入 {len(rows)} 条记录，累计 {total_written} 条。")

            has_more = data.get("hasMore", False)
            if has_more:
                page += 1
                time.sleep(sleep_interval)
            else:
                print("已获取全部数据。")

    print(f"完成！共写入 {total_written} 条记录，保存至：{filepath}")

# ---------- 当前请求 IP 检测 ----------
def detect_current_ip(app_key: str, timeout: int = 10) -> Dict[str, Any]:
    """
    调用 C5 API 检测当前请求 IP（C5 服务器视角）。

    当 IP 不在白名单时，C5 会在 errorMsg 中返回类似：
        "未设置ip白名单或ip不在白名单中，当前请求ip 36.230.26.10"
    本函数从该消息中提取 IP 并返回。

    :return: dict {
        "success": bool,        # C5 API 是否调用成功（IP 在白名单内且返回数据）
        "ip": str,              # C5 看到的当前请求 IP（白名单失败时返回，否则为空）
        "raw_msg": str,         # 原始返回消息
        "in_whitelist": bool,   # 当前 IP 是否已在白名单内
    }
    """
    # 用一个最小的请求触发 C5 鉴权，仅为探测 IP，不依赖具体商品
    result = query_product_list(
        app_key=app_key,
        market_hash_name="ping",
        app_id=730,
        page_num=1,
        page_size=1,
        timeout=timeout,
    )

    if result is None:
        return {
            "success": False,
            "ip": "",
            "raw_msg": "请求失败（网络异常或超时）",
            "in_whitelist": False,
        }

    if result.get("success"):
        return {
            "success": True,
            "ip": "",
            "raw_msg": "API 调用成功，当前 IP 已在白名单内。",
            "in_whitelist": True,
        }

    # 失败：尝试从 errorMsg 中提取 IP
    error_msg = result.get("errorMsg", "") or str(result)
    # 匹配 "当前请求ip 36.230.26.10" / "请求ip: 1.2.3.4" / "ip 1.2.3.4" 等格式
    m = re.search(
        r"(?:当前请求ip|请求ip|当前ip|ip)\s*[:：]?\s*(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})",
        error_msg, re.IGNORECASE)
    if m:
        return {
            "success": False,
            "ip": m.group(1),
            "raw_msg": error_msg,
            "in_whitelist": False,
        }

    return {
        "success": False,
        "ip": "",
        "raw_msg": error_msg or "未知错误",
        "in_whitelist": False,
    }

# ============= 使用示例 =============
if __name__ == "__main__":
    import os

    from dotenv import load_dotenv

    # 凭证从项目根目录 .env 读取（见 .env.example），禁止硬编码
    load_dotenv(os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        ".env"))
    APP_KEY = os.getenv("C5_APP_KEY", "")
    if not APP_KEY:
        raise SystemExit("[配置错误] 未设置 C5_APP_KEY，请复制 .env.example 为 .env 并填写。")

    # 按饰品名称查询，如果希望 goods_id 有值，可传入 item_id
    fetch_all_products_and_save_csv(
        app_key=APP_KEY,
        market_hash_name="USP-S | Ticket to Hell (Battle-Scarred)",
        app_id=730,
        # item_id=123456,   # 如果有对应的 goods_id 可传入，否则留空
        page_size=50,
        sleep_interval=0.3
    )