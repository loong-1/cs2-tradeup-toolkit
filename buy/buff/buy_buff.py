import requests
import json
import os
import sys

# ================= 配置区 =================
# 凭证从项目根目录 .env 读取（见 .env.example），禁止硬编码到源码。
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        ".env"))
except ImportError:
    pass

PAY_METHOD = 80
STEAM_ID = os.getenv("STEAM_ID", "")
SESSION = os.getenv("BUFF_SESSION", "")
CSRF_TOKEN = os.getenv("BUFF_CSRF_TOKEN", "")

# CSV 保存目录（统一存放于 buy/data/csv/buff/，与 found_buff.py 保持一致）
CSV_SAVE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "buy", "data", "csv", "buff")
# =========================================

def buy_item(goods_id, sell_order_id, price):
    """真实购买（谨慎使用）"""
    url = "https://buff.163.com/api/market/goods/buy"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Content-Type": "application/json",
        "Referer": f"https://buff.163.com/goods/{goods_id}?from=market",
        "X-CSRFToken": CSRF_TOKEN,
        "X-Requested-With": "XMLHttpRequest",
        "Cookie": f"session={SESSION}; csrf_token={CSRF_TOKEN}; game=csgo",
    }
    payload = {
        "game": "csgo",
        "goods_id": goods_id,
        "sell_order_id": sell_order_id,
        "price": price,
        "pay_method": PAY_METHOD,
        "allow_tradable_cooldown": 0,
        "token": "",
        "cdkey_id": "",
        "hide_non_epay": True,
        "steamid": STEAM_ID,
    }
    resp = requests.post(url, headers=headers, json=payload, timeout=15)
    return resp.json()

def read_best_order():
    """读取 found_buff.py 生成的最优订单文件"""
    best_file = os.path.join(CSV_SAVE_DIR, "best_order.txt")
    if not os.path.exists(best_file):
        print(f"[错误] 未找到最优订单文件 {best_file}，请先运行 found_buff.py")
        return None
    with open(best_file, 'r', encoding='utf-8') as f:
        lines = [line.strip() for line in f.readlines()]
    if len(lines) < 3:
        print("[错误] 最优订单文件格式错误")
        return None
    return lines[0], lines[1], lines[2]  # goods_id, sell_order_id, price

# ================= 主流程 =================
if __name__ == "__main__":
    print("=" * 60)
    print("BUFF 自动购买脚本")
    print("=" * 60)

    # 支持命令行参数：python buy_buff.py <goods_id> <sell_order_id> <price>
    if len(sys.argv) == 4:
        goods_id = sys.argv[1]
        sell_order_id = sys.argv[2]
        price = sys.argv[3]
        print(f"[参数] 使用命令行指定的订单：商品ID={goods_id}, 订单ID={sell_order_id}, 价格={price}")
    else:
        # 否则从 found_buff.py 生成的文件读取
        result = read_best_order()
        if result is None:
            sys.exit(1)
        goods_id, sell_order_id, price = result
        print(f"[文件] 读取最优订单：商品ID={goods_id}, 订单ID={sell_order_id}, 价格={price}")

    # 确认购买
    print("\n⚠️  即将执行真实购买！")
    confirm = input(f"确认购买该订单？(输入 yes 继续): ")
    if confirm.lower() != 'yes':
        print("已取消购买")
        sys.exit(0)

    # 执行购买
    print("正在提交购买请求...")
    try:
        result = buy_item(goods_id, sell_order_id, price)
        print("购买结果：", json.dumps(result, indent=2, ensure_ascii=False))
        if result.get('code') == 'OK':
            print("\n✅ 购买成功！")
        else:
            print(f"\n❌ 购买失败：{result.get('error', '未知错误')}")
    except Exception as e:
        print(f"[异常] 购买请求出错：{e}")