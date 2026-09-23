import requests
import time
import json
import random
import csv
import os
from datetime import datetime

# ================= 配置区 =================
# 凭证从项目根目录 .env 读取（见 .env.example），禁止硬编码到源码。
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        ".env"))
except ImportError:
    pass

GOODS_ID = 1115503
PAY_METHOD = 80
STEAM_ID = os.getenv("STEAM_ID", "")

SESSION = os.getenv("BUFF_SESSION", "")
CSRF_TOKEN = os.getenv("BUFF_CSRF_TOKEN", "")

# 筛选条件（None 表示不限制）
WEAR_MIN = 0.15
WEAR_MAX = 0.21
PRICE_MIN = 0
PRICE_MAX = 100.0

# 请求设置
MAX_RETRIES = 3
BASE_DELAY = 2
RETRY_DELAY = 5

# CSV 保存目录（统一存放于 buy/data/csv/buff/ 下）
CSV_SAVE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "buy", "data", "csv", "buff")
# =========================================

def safe_request(url, headers, params=None, retries=MAX_RETRIES):
    """带重试机制的GET请求"""
    for attempt in range(retries):
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=10)
            if resp.status_code == 200:
                return resp
            elif resp.status_code == 429:
                wait = RETRY_DELAY * (2 ** attempt) + random.uniform(0, 1)
                print(f"[警告] 触发限流(429)，等待 {wait:.1f} 秒后重试...")
                time.sleep(wait)
            elif resp.status_code >= 500:
                wait = RETRY_DELAY * (2 ** attempt)
                print(f"[警告] 服务器错误 {resp.status_code}，等待 {wait} 秒后重试...")
                time.sleep(wait)
            else:
                print(f"[错误] 请求失败，状态码 {resp.status_code}")
                return resp
        except Exception as e:
            print(f"[异常] 请求出错：{e}，重试中...")
            time.sleep(RETRY_DELAY)
    return None

def get_all_sell_orders(goods_id):
    """获取全部在售订单，带重试和防限流"""
    all_items = []
    page = 1
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": f"https://buff.163.com/goods/{goods_id}?from=market",
        "Cookie": f"session={SESSION}; csrf_token={CSRF_TOKEN}; game=csgo",
    }

    while True:
        url = "https://buff.163.com/api/market/goods/sell_order"
        params = {
            "game": "csgo",
            "goods_id": goods_id,
            "page_num": page,
            "sort_by": "price.asc",
            "_": int(time.time() * 1000)
        }

        resp = safe_request(url, headers, params)
        if resp is None:
            print("[错误] 多次重试后仍失败，停止获取")
            break

        try:
            data = resp.json()
        except:
            print("[错误] 返回内容不是合法 JSON，跳过本页")
            break

        if data.get('code') != 'OK':
            print(f"[警告] API 返回错误：{data.get('error', '未知错误')}")
            break

        items = data.get('data', {}).get('items', [])
        if not items:
            print(f"[信息] 第 {page} 页无数据，停止翻页")
            break

        all_items.extend(items)
        print(f"[信息] 已获取第 {page} 页，累计 {len(all_items)} 个订单")
        page += 1
        time.sleep(BASE_DELAY)

    return all_items

def filter_orders(orders, wear_min=None, wear_max=None, price_min=None, price_max=None):
    """按磨损值和价格范围筛选订单"""
    filtered = []
    for item in orders:
        paintwear_raw = item.get('asset_info', {}).get('paintwear')
        if paintwear_raw is None:
            continue
        try:
            paintwear = float(paintwear_raw)
        except (TypeError, ValueError):
            continue

        try:
            price = float(item.get('price', 0))
        except (TypeError, ValueError):
            continue

        if wear_min is not None and paintwear < wear_min:
            continue
        if wear_max is not None and paintwear > wear_max:
            continue
        if price_min is not None and price < price_min:
            continue
        if price_max is not None and price > price_max:
            continue

        filtered.append(item)
    return filtered

def save_orders_to_csv(orders, goods_id):
    """将所有订单保存到 CSV 文件（无时间戳）"""
    os.makedirs(CSV_SAVE_DIR, exist_ok=True)

    filename = f"buff_orders_{goods_id}.csv"
    filepath = os.path.join(CSV_SAVE_DIR, filename)

    fieldnames = [
        'sell_order_id', 'price', 'paintwear', 'wear_name',
        'paintseed', 'assetid', 'goods_id'
    ]

    with open(filepath, 'w', newline='', encoding='utf-8-sig') as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        writer.writeheader()
        for item in orders:
            asset_info = item.get('asset_info', {})
            paintwear_raw = asset_info.get('paintwear')
            try:
                paintwear = float(paintwear_raw) if paintwear_raw is not None else ''
            except (TypeError, ValueError):
                paintwear = ''
            writer.writerow({
                'sell_order_id': item.get('id', ''),
                'price': item.get('price', ''),
                'paintwear': paintwear,
                'wear_name': asset_info.get('wear_name', ''),
                'paintseed': asset_info.get('paintseed', ''),
                'assetid': item.get('assetid', ''),
                'goods_id': item.get('goods_id', goods_id)
            })

    print(f"\n[保存] 所有订单已保存到：{filepath}")

def save_best_order(goods_id, sell_order_id, price):
    """将最优订单信息保存到临时文件，供 buy_buff.py 读取"""
    best_file = os.path.join(CSV_SAVE_DIR, "best_order.txt")
    with open(best_file, 'w', encoding='utf-8') as f:
        f.write(f"{goods_id}\n{sell_order_id}\n{price}\n")
    print(f"[信息] 最优订单信息已保存至：{best_file}")

# ================= 主流程 =================
if __name__ == "__main__":
    print("=" * 60)
    print("开始获取所有在售订单...")
    all_orders = get_all_sell_orders(GOODS_ID)
    print(f"共获取到 {len(all_orders)} 个在售订单\n")

    if not all_orders:
        print("没有找到任何订单，请检查商品ID或凭证是否有效。")
        exit()

    # 保存所有订单到 CSV
    save_orders_to_csv(all_orders, GOODS_ID)

    # 统计订单的整体价格和磨损范围（用于调试筛选条件）
    prices = []
    wears = []
    for item in all_orders:
        try:
            prices.append(float(item['price']))
        except:
            pass
        wear_raw = item.get('asset_info', {}).get('paintwear')
        if wear_raw:
            try:
                wears.append(float(wear_raw))
            except:
                pass
    if prices:
        print(f"[调试] 该商品所有订单价格范围：¥{min(prices):.2f} ~ ¥{max(prices):.2f}")
    if wears:
        print(f"[调试] 磨损值范围：{min(wears):.6f} ~ {max(wears):.6f}")
    print()

    # 应用筛选
    print("正在按条件筛选...")
    filtered = filter_orders(
        all_orders,
        wear_min=WEAR_MIN,
        wear_max=WEAR_MAX,
        price_min=PRICE_MIN,
        price_max=PRICE_MAX
    )
    print(f"筛选后剩余 {len(filtered)} 个订单\n")

    if not filtered:
        print("没有符合当前筛选条件的订单，请调整 WEAR_MAX / PRICE_MAX 等参数。")
        exit()

    # 按价格升序排序
    filtered_sorted = sorted(filtered, key=lambda x: float(x['price']))

    # 显示前10个
    show_count = min(10, len(filtered_sorted))
    print(f"显示前 {show_count} 个符合条件的订单：")
    print("-" * 50)
    for idx, item in enumerate(filtered_sorted[:show_count], 1):
        order_id = item['id']
        price = item['price']
        paintwear_raw = item['asset_info'].get('paintwear')
        try:
            paintwear = float(paintwear_raw)
        except:
            paintwear = 0.0
        wear_name = item.get('asset_info', {}).get('wear_name', '')
        print(f"{idx}. 订单ID: {order_id}, 价格: ¥{price}, 磨损: {paintwear:.6f} {wear_name}")
    print("-" * 50)

    # 保存最优订单（最低价且符合筛选）到临时文件
    best = filtered_sorted[0]
    save_best_order(GOODS_ID, best['id'], best['price'])
    print(f"\n[提示] 可以使用 buy_buff.py 自动购买该订单（订单ID: {best['id']}，价格: ¥{best['price']}）")