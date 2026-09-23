"""ECO 网页库存爬虫。

用 Selenium + Edge 爬取 https://www.ecosteam.cn/html/person/mysteam.html，
解析 <li class="card_item"> 获取库存（保持网页显示顺序）。

翻页直到最后一页，爬取间隔 1.2s。
需要提供 ECO 网页的登录 Cookie。
"""
import json
import logging
import re
import time
from typing import Callable, Optional

logger = logging.getLogger(__name__)

ECO_INVENTORY_URL = "https://www.ecosteam.cn/html/person/mysteam.html"
ECO_BASE_URL = "https://www.ecosteam.cn"

# 爬取间隔（秒）
SCRAPE_DELAY = 1.2


def scrape_eco_inventory(cookie_string: str,
                         headless: bool = False,
                         on_progress: Optional[Callable] = None,
                         max_pages: int = 100) -> list:
    """用 Selenium 爬取 ECO 网页库存。

    Args:
        cookie_string: ECO 网页的登录 Cookie 字符串（从浏览器 F12 复制）
        headless: 是否无头模式
        on_progress: 回调 (page, total_so_far) -> None
        max_pages: 最大页数安全限制

    Returns:
        list[dict]: 库存物品列表，格式与 ECO API 返回一致：
            {AssetId, StockId, GoodsId, HashName, GoodsName, PaintWear, ...}
    """
    from selenium import webdriver
    from selenium.webdriver.edge.service import Service
    from selenium.webdriver.edge.options import Options
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from bs4 import BeautifulSoup
    try:
        from webdriver_manager.microsoft import EdgeChromiumDriverManager
        driver_manager = EdgeChromiumDriverManager().install()
    except Exception:
        driver_manager = None

    edge_options = Options()
    if headless:
        edge_options.add_argument("--headless")
    edge_options.add_argument("--disable-blink-features=AutomationControlled")
    edge_options.add_experimental_option("excludeSwitches", ["enable-automation"])
    edge_options.add_experimental_option("useAutomationExtension", False)

    if driver_manager:
        service = Service(driver_manager)
        driver = webdriver.Edge(service=service, options=edge_options)
    else:
        driver = webdriver.Edge(options=edge_options)

    all_items: list[dict] = []
    page_num = 1

    try:
        # 1. 先打开 ECO 首页，注入 Cookie
        driver.get(ECO_BASE_URL)
        time.sleep(1)

        if cookie_string:
            for item in cookie_string.split(";"):
                if "=" in item:
                    name, value = item.strip().split("=", 1)
                    driver.add_cookie({
                        "name": name.strip(),
                        "value": value.strip(),
                        "domain": ".ecosteam.cn",
                    })
            logger.info("ECO Cookie 注入成功")
            driver.refresh()
            time.sleep(2)
        else:
            logger.warning("未提供 Cookie，请手动登录后按 Enter")
            input()

        # 2. 打开库存页面
        driver.get(ECO_INVENTORY_URL)
        time.sleep(3)

        # 3. 循环翻页爬取
        # 记录上一页第一个卡片的 asset_id，用于检测翻页成功
        prev_first_asset_id = None

        while page_num <= max_pages:
            logger.info("正在爬取第 %d 页...", page_num)

            # 等待卡片加载（非第一页时，需等待第一个卡片的 asset_id 变化）
            try:
                if page_num == 1:
                    WebDriverWait(driver, 20).until(
                        EC.presence_of_element_located(
                            (By.CSS_SELECTOR, "li.card_item")))
                else:
                    # 翻页后等待新卡片加载：第一个卡片的 data-assetid 变化
                    def _asset_id_changed(driver):
                        cards = driver.find_elements(
                            By.CSS_SELECTOR, "li.card_item")
                        if not cards:
                            return False
                        new_id = cards[0].get_attribute("data-assetid")
                        return new_id and new_id != prev_first_asset_id
                    WebDriverWait(driver, 20).until(_asset_id_changed)
            except Exception:
                logger.warning("第 %d 页未找到卡片或翻页超时，爬取结束", page_num)
                break

            soup = BeautifulSoup(driver.page_source, "html.parser")
            cards = soup.select("li.card_item")

            if not cards:
                logger.info("第 %d 页无卡片，爬取结束", page_num)
                break

            # 记录本页第一个 asset_id，供下一轮翻页检测用
            prev_first_asset_id = cards[0].get("data-assetid")

            for card in cards:
                item = _parse_card(card)
                if item:
                    all_items.append(item)

            logger.info("第 %d 页: %d 件，累计 %d 件",
                        page_num, len(cards), len(all_items))
            if on_progress:
                on_progress(page_num, len(all_items))

            # 4. 翻页
            if not _click_next_page(driver):
                logger.info("已到最后一页，爬取结束")
                break

            page_num += 1
            time.sleep(SCRAPE_DELAY)

        return all_items

    except Exception as e:
        logger.error("ECO 网页爬取出错: %s", e)
        return all_items
    finally:
        driver.quit()


def _parse_card(card) -> Optional[dict]:
    """解析单个 <li class="card_item"> 卡片。

    基于实际 ECO 网页 HTML 结构解析：
    - data-wear: 磨损值
    - .goodsName a: 物品名称
    - .card-left span: 磨损等级 / 品质
    - detail_modal .paint_seed: 图案模板
    - detail_modal .paint_index: 皮肤编号
    - detail_content data-stickers/data-keychains: 贴纸/挂件
    """
    asset_id = card.get("data-assetid", "")
    if not asset_id:
        return None

    stock_id = card.get("data-stockid", "")
    goods_id = card.get("data-goodsid", "")
    steam_price = card.get("data-steamprice", "")
    game_id = card.get("data-gameid", "730")

    # 磨损值（直接从 data-wear 属性获取）
    paint_wear_str = card.get("data-wear", "0")
    try:
        paint_wear_float = float(paint_wear_str)
    except (ValueError, TypeError):
        paint_wear_float = 0.0

    # 物品名称（从 .goodsName a 获取）
    goods_name = ""
    goods_name_el = card.select_one(".goodsName a")
    if goods_name_el:
        goods_name = goods_name_el.get_text(strip=True)

    # 如果没找到，从 detail_modal 的 .name 获取
    if not goods_name:
        name_el = card.select_one(".product_info .name")
        if name_el:
            goods_name = name_el.get_text(strip=True)

    # 如果还没找到，从 img alt 获取
    if not goods_name:
        img = card.select_one(".product_image img")
        if img:
            goods_name = img.get("alt", "").strip()

    # 磨损等级（从 .card-left span 获取）
    wear_name = ""
    card_left_spans = card.select(".card-left span")
    if card_left_spans:
        wear_name = card_left_spans[0].get_text(strip=True)

    # paint seed（从 detail_modal .paint_seed 获取）
    paint_seed = 0
    paint_seed_el = card.select_one(".paint_seed")
    if paint_seed_el:
        text = paint_seed_el.get_text()
        m = re.search(r"(\d+)", text)
        if m:
            paint_seed = int(m.group(1))

    # paint index（从 detail_modal .paint_index 获取）
    paint_index = 0
    paint_index_el = card.select_one(".paint_index")
    if paint_index_el:
        text = paint_index_el.get_text()
        m = re.search(r"(\d+)", text)
        if m:
            paint_index = int(m.group(1))

    # 贴纸（从 detail_content data-stickers 获取）
    stickers = []
    detail_content = card.select_one(".detail_content")
    if detail_content:
        stickers_str = detail_content.get("data-stickers", "[]")
        try:
            stickers = json.loads(stickers_str)
        except (json.JSONDecodeError, TypeError):
            stickers = []
        keychains_str = detail_content.get("data-keychains", "[]")
        try:
            keychains = json.loads(keychains_str)
        except (json.JSONDecodeError, TypeError):
            keychains = []
    else:
        keychains = []

    # 是否可交易
    can_trade = card.get("data-cantrade", "false").lower() == "true"

    # 稀有度/品质
    rarity = card.get("data-rarity", "")

    return {
        "AssetId": asset_id,
        "StockId": stock_id,
        "GoodsId": goods_id,
        "HashName": goods_name,
        "GoodsName": goods_name,
        "PaintWear": str(paint_wear_float),
        "PaintSeed": paint_seed,
        "PaintIndex": paint_index,
        "Tradable": can_trade,
        "Stickers": stickers,
        "Keychains": keychains,
        "Status": int(card.get("data-status", -1)),
        "SteamPrice": steam_price,
        "GameId": game_id,
        "WearName": wear_name,
        "Rarity": rarity,
    }


def _click_next_page(driver) -> bool:
    """点击 layui 分页的下一页按钮。返回是否成功点击。

    ECO 网页使用 layui-laypage 分页组件：
    <a class="layui-laypage-next" data-page="2">
    最后一页时 next 按钮无 data-page 或被禁用。
    """
    from selenium.webdriver.common.by import By

    # layui 分页的下一页按钮
    try:
        next_btns = driver.find_elements(
            By.CSS_SELECTOR, "a.layui-laypage-next")
        for btn in next_btns:
            # 检查是否可见且可用
            if not btn.is_displayed():
                continue
            cls = btn.get_attribute("class") or ""
            # layui-disabled 表示已到最后一页
            if "layui-disabled" in cls:
                logger.info("下一页按钮已禁用，已到最后一页")
                return False
            data_page = btn.get_attribute("data-page")
            if not data_page:
                logger.info("下一页按钮无 data-page，已到最后一页")
                return False
            logger.info("点击下一页 (data-page=%s)", data_page)
            btn.click()
            return True
    except Exception as e:
        logger.warning("点击下一页失败: %s", e)

    return False
