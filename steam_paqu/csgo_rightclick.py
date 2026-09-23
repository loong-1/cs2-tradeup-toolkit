import json
import time
import csv
import os
from selenium import webdriver
from selenium.webdriver.edge.service import Service
from selenium.webdriver.edge.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from bs4 import BeautifulSoup
from webdriver_manager.microsoft import EdgeChromiumDriverManager


def get_buff_inventory_edge(url, cookie_string, headless=False):
    edge_options = Options()
    if headless:
        edge_options.add_argument('--headless')
    edge_options.add_argument('--disable-blink-features=AutomationControlled')
    edge_options.add_experimental_option('excludeSwitches', ['enable-automation'])
    edge_options.add_experimental_option('useAutomationExtension', False)

    service = Service(EdgeChromiumDriverManager().install())
    driver = webdriver.Edge(service=service, options=edge_options)

    all_items = []
    page_num = 1

    try:
        driver.get("https://buff.163.com")
        time.sleep(1)

        if cookie_string:
            for item in cookie_string.split(';'):
                if '=' in item:
                    name, value = item.strip().split('=', 1)
                    driver.add_cookie({
                        'name': name.strip(),
                        'value': value.strip(),
                        'domain': '.163.com'
                    })
            print("✅ Cookie 注入成功")
            driver.refresh()
            time.sleep(3)
        else:
            print("⚠️ 未提供 Cookie，请手动登录后按 Enter 继续")
            input()

        driver.get(url)
        time.sleep(3)

        while True:
            print(f"\n📄 正在爬取第 {page_num} 页...")
            WebDriverWait(driver, 20).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, 'li.my_inventory'))
            )

            soup = BeautifulSoup(driver.page_source, 'html.parser')
            items_li = soup.select('li.my_inventory')

            if not items_li:
                print("⚠️ 当前页没有物品")
                break

            for li in items_li:
                try:
                    goods_info = json.loads(li['data-goods-info'])
                    asset_info = json.loads(li['data-asset-info'])

                    item = {
                        '名称': goods_info.get('name', ''),
                        '市场简称': goods_info.get('market_hash_name', ''),
                        '售价(元)': goods_info.get('sell_min_price', '0'),
                        '状态': li.get('data-item-info', '{}'),
                        '磨损等级': goods_info.get('tags', {}).get('exterior', {}).get('localized_name', '无'),
                        '磨损数值': asset_info.get('paintwear', ''),
                        '图案模板(Seed)': asset_info.get('info', {}).get('paintindex', ''),
                        '品质': goods_info.get('tags', {}).get('rarity', {}).get('localized_name', ''),
                        '收藏品': goods_info.get('tags', {}).get('itemset', {}).get('localized_name', ''),
                        'AssetID': li.get('data-assetid', ''),
                    }
                    all_items.append(item)
                except Exception as e:
                    print(f"解析物品失败: {e}")
                    continue

            print(f"✅ 本页解析到 {len(items_li)} 件物品，累计 {len(all_items)} 件")

            # 翻页逻辑
            try:
                next_btn = driver.find_element(By.CSS_SELECTOR, 'a.next')
                if 'disabled' in next_btn.get_attribute('class') or next_btn.get_attribute('aria-disabled') == 'true':
                    print("🏁 已是最后一页")
                    break
                next_btn.click()
                time.sleep(3)
                page_num += 1
            except:
                print("🏁 未找到下一页，爬取结束")
                break

        return all_items

    except Exception as e:
        print(f"❌ 爬取出错: {e}")
        return all_items
    finally:
        driver.quit()


def save_to_csv(items, filename='buff_inventory.csv'):
    if not items:
        print("没有数据可保存")
        return
    keys = items[0].keys()
    with open(filename, 'w', newline='', encoding='utf-8-sig') as f:
        writer = csv.DictWriter(f, fieldnames=keys)
        writer.writeheader()
        writer.writerows(items)
    print(f"✅ 数据已保存至 {filename}")


if __name__ == '__main__':
    # 凭证与 SteamID 从项目根目录 .env 读取（见 .env.example），禁止硬编码
    from dotenv import load_dotenv

    load_dotenv(os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

    steam_id = os.getenv("STEAM_ID", "")
    my_cookie = os.getenv("BUFF_COOKIE_STRING", "").strip()
    if not (steam_id and my_cookie):
        raise SystemExit(
            "[配置错误] 需要 STEAM_ID 与 BUFF_COOKIE_STRING，"
            "请复制 .env.example 为 .env 并填写。")

    target_url = (
        'https://buff.163.com/market/steam_inventory?game=csgo'
        f'#page_num=1&page_size=50&fold=false&search=&steamid={steam_id}&state=all')

    inventory = get_buff_inventory_edge(target_url, cookie_string=my_cookie, headless=False)

    print(f"\n📊 总共爬取到 {len(inventory)} 件物品")
    if inventory:
        for i, item in enumerate(inventory[:5], 1):
            print(f"\n--- 样例 {i} ---")
            for k, v in item.items():
                print(f"  {k}: {v}")

        # ----- 保存到固定路径，文件名固定 -----
        save_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            'buff_inventory')
        os.makedirs(save_dir, exist_ok=True)
        save_path = os.path.join(save_dir, 'buff_inventory.csv')   # 固定文件名
        save_to_csv(inventory, save_path)
        # ------------------------------------
    else:
        print("❌ 未获取到任何物品，请检查 Cookie 是否过期。")