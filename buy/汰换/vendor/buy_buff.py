import requests
import json
import os
import sys

# ================= 配置区 =================
# 支付方式：1=BUFF余额（可提现到银行卡）, 44=网易支付(组合), 49=支付宝, 51=支付宝花呗, 6=微信
# 注意：80=CS2待结算余额支付（已被Buff关闭），不要使用
# 2026-09-11：旧值 1（老"账户余额"通道）实测报"可用余额不足"——
# Buff 网页支付页 data-method 显示现行余额支付是 value=100 "BUFF可用资金"
# （子方式 96 = BUFF余额（银行卡已授权），enough=true）
PAY_METHOD = 100
# pay_method=100 必须配合 passback_params 指定子方式 96，否则服务端默认
# 落到"CS2 待结算余额"（已关闭）→ 报"CS2 待结算余额支付功能暂时关闭"
PASSBACK_PARAMS = '{"included_sub_methods":[96]}'

# 凭证从项目根目录 .env 读取（见 .env.example），禁止硬编码到源码。
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__))))), ".env"))
except ImportError:
    pass

STEAM_ID = os.getenv("STEAM_ID", "")
SESSION = os.getenv("BUFF_SESSION", "")
CSRF_TOKEN = os.getenv("BUFF_CSRF_TOKEN", "")

# CSV 保存目录（统一存放于 buy/data/csv/buff/，与 found_buff.py 保持一致）
CSV_SAVE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))),
    "buy", "data", "csv", "buff")
# =========================================

def buy_item(goods_id, sell_order_id, price, session=None, csrf_token=None):
    """真实购买（谨慎使用）。

    Args:
        session: Buff session cookie，留空则用本文件常量 SESSION
        csrf_token: Buff csrf_token，留空则用本文件常量 CSRF_TOKEN
    """
    sess = session or SESSION
    csrf = csrf_token or CSRF_TOKEN

    # 先访问 Buff 首页获取最新的 csrf_token cookie（CSRF token 有时效，
    # 过期的 token 会导致 POST 请求报"页面已过期"）
    csrf = _refresh_csrf_token(sess, csrf)

    url = "https://buff.163.com/api/market/goods/buy"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Content-Type": "application/json",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Origin": "https://buff.163.com",
        "Referer": f"https://buff.163.com/goods/{goods_id}?from=market",
        "X-CSRFToken": csrf,
        "X-Requested-With": "XMLHttpRequest",
        "Cookie": f"session={sess}; csrf_token={csrf}; game=csgo",
    }
    payload = {
        "game": "csgo",
        "goods_id": goods_id,
        "sell_order_id": sell_order_id,
        "price": price,
        "pay_method": PAY_METHOD,
        "passback_params": PASSBACK_PARAMS,
        "allow_tradable_cooldown": 0,
        "token": "",
        "cdkey_id": "",
        "password": "",
        "hide_non_epay": True,
        "steamid": STEAM_ID,
    }
    resp = requests.post(url, headers=headers, json=payload, timeout=15)
    return resp.json()


def _refresh_csrf_token(session, old_csrf):
    """访问 Buff 首页刷新 csrf_token cookie。

    Buff 的 csrf_token 有时效，POST 请求（如购买）会校验 CSRF，
    过期的 token 导致"页面已过期"。GET 请求（如查询）不校验所以不受影响。
    通过访问首页，服务器会在 Set-Cookie 里返回最新的 csrf_token。
    """
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Cookie": f"session={session}; csrf_token={old_csrf}; game=csgo",
        }
        resp = requests.get("https://buff.163.com", headers=headers,
                            timeout=10, allow_redirects=True)
        # 从 Set-Cookie 提取最新的 csrf_token
        new_csrf = old_csrf
        for cookie in resp.cookies:
            if cookie.name == "csrf_token":
                new_csrf = cookie.value
                break
        if new_csrf != old_csrf:
            print(f"[购买] 已刷新 csrf_token: {old_csrf[:20]}... -> {new_csrf[:20]}...")
        return new_csrf
    except Exception as e:
        print(f"[购买] 刷新 csrf_token 失败，使用旧值: {e}")
        return old_csrf

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