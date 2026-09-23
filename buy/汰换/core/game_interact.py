"""游戏内自动点击模块（改造自 cs2滚动查询.py）。

核心逻辑：
1. 激活 CS2 窗口
2. 从 CSV 读取库存物品索引
3. 对每件目标物品：
   a. 计算目标行号
   b. 若需滚动：每 8 次滚轮 → 滑块上移 1 像素修正
   c. 右键点击物品

参数化：
- BATCH_SIZE（滚轮批大小，默认 8）
- SLIDER_ADJUST_PIXEL（滑块上移像素，默认 1）
- 其他布局参数均通过 utils.config.GameInteractConfig 读取，便于校准
"""
import csv
import logging
import os
import time
from dataclasses import dataclass
from typing import Callable, Optional

from utils.config import GameInteractConfig

logger = logging.getLogger(__name__)


# ============================================================
# 依赖检测（PyInstaller 打包后 cv2/pyautogui 可能缺失）
# ============================================================
def _check_deps() -> "bool | str":
    """检查游戏点击所需依赖是否可用。

    返回 True 表示全部可用；否则返回缺失的模块名（如 'cv2'）。
    """
    for mod in ("cv2", "numpy", "pyautogui", "pygetwindow",
                "win32gui", "win32con"):
        try:
            __import__(mod)
        except ImportError as e:
            logger.error("游戏点击依赖缺失：%s", e)
            return mod
    return True


# ============================================================
# 1. 激活窗口
# ============================================================
def activate_window(title_keywords: str):
    """激活 CS2 窗口，返回窗口句柄。"""
    import pygetwindow as gw
    try:
        windows = gw.getWindowsWithTitle(title_keywords)
        if windows:
            window = windows[0]
            if not window.isActive:
                window.activate()
            time.sleep(0.3)
            return window._hWnd
    except Exception as e:
        logger.error("查找窗口 '%s' 失败：%s", title_keywords, e)
    return None


def find_cs2_window():
    """尝试激活 CS2 窗口（中英文标题都试）。"""
    hwnd = activate_window("反恐精英：全球攻势")
    if hwnd is None:
        hwnd = activate_window("Counter-Strike: Global Offensive")
    if hwnd is None:
        hwnd = activate_window("Counter-Strike 2")
    return hwnd


# ============================================================
# 2. 滑块检测与微调
# ============================================================
def get_slider_position(cfg: GameInteractConfig):
    """检测滑块当前屏幕坐标。"""
    import cv2
    import numpy as np
    import pyautogui

    screenshot = pyautogui.screenshot(region=(
        cfg.TRACK_X, cfg.TRACK_TOP,
        cfg.TRACK_WIDTH, cfg.TRACK_HEIGHT))
    img_np = np.array(screenshot)
    gray = cv2.cvtColor(img_np, cv2.COLOR_RGB2GRAY)
    _, thresh = cv2.threshold(gray, 100, 255, cv2.THRESH_BINARY)
    points = cv2.findNonZero(thresh)
    if points is not None:
        # 兼容不同 OpenCV 版本的 findNonZero 返回形状：
        # 老版 (N,1,2)，新版 (N,2) —— 统一 reshape 成 (N,2) 再取 y 列
        pts = np.asarray(points).reshape(-1, 2)
        avg_y = float(np.mean(pts[:, 1]))
        global_y = int(avg_y + cfg.TRACK_TOP)
        global_x = cfg.TRACK_X + cfg.TRACK_WIDTH // 2
        return global_x, global_y
    return None


def adjust_slider_up(cfg: GameInteractConfig,
                    pixels: Optional[int] = None) -> bool:
    """滑块上移 N 像素（默认 cfg.SLIDER_ADJUST_PIXEL）。"""
    import pyautogui

    if pixels is None:
        pixels = cfg.SLIDER_ADJUST_PIXEL

    pos = get_slider_position(cfg)
    if pos is None:
        logger.warning("无法获取滑块位置，跳过微调")
        return False

    start_x, start_y = pos
    target_y = start_y - pixels
    target_y = max(cfg.TRACK_TOP + 10,
                   min(cfg.TRACK_BOTTOM - 10, target_y))

    logger.info("滑块上移 %d 像素：%d → %d", pixels, start_y, target_y)
    pyautogui.moveTo(start_x, start_y, duration=0.05)
    pyautogui.mouseDown()
    pyautogui.moveTo(start_x, target_y, duration=cfg.DRAG_DURATION)
    pyautogui.mouseUp()
    time.sleep(0.2)
    # 鼠标回到左侧滚动条区域
    pyautogui.moveTo(cfg.SCROLL_MOUSE_X, cfg.SCROLL_MOUSE_Y, duration=0.1)
    return True


# ============================================================
# 3. CSV 查找物品
# ============================================================
def get_item_info(csv_path: str, target_name: str, cfg: GameInteractConfig):
    """从库存 CSV 查找目标物品，返回 (idx, row, cx, total_rows)。"""
    encodings = ["utf-8-sig", "utf-8", "gbk", "gb2312"]
    rows = None
    for enc in encodings:
        try:
            with open(csv_path, "r", encoding=enc) as f:
                reader = csv.DictReader(f)
                rows = list(reader)
                break
        except Exception:
            continue
    if rows is None:
        raise ValueError(f"无法读取 CSV：{csv_path}")

    total_items = len(rows)
    total_rows = (total_items + cfg.COLS - 1) // cfg.COLS
    logger.info("库存物品 %d 件，总行数 %d", total_items, total_rows)

    for idx, row in enumerate(rows):
        # 优先匹配「名称」列，其次 asset_id
        name = (row.get("名称") or "").strip()
        if not name:
            # 兼容其他列名
            name = (row.get("goods_name") or
                    row.get("GoodsName") or
                    row.get("hash_name") or "").strip()
        if target_name.lower() in name.lower():
            col = idx % cfg.COLS
            row_num = idx // cfg.COLS
            cx = (cfg.BIG_X + col * (cfg.CARD_W + cfg.GAP_X)
                  + cfg.CARD_W // 2)
            return idx, row_num, cx, total_rows
    raise ValueError(f"库存中未找到 '{target_name}'")


# ============================================================
# 4. 点击物品
# ============================================================
def click_item(hwnd, cx: int, cy: int):
    """客户区坐标转屏幕坐标并右键点击。"""
    import pyautogui
    import win32gui

    left_top = win32gui.ClientToScreen(hwnd, (0, 0))
    screen_x = left_top[0] + cx
    screen_y = left_top[1] + cy
    pyautogui.moveTo(screen_x, screen_y, duration=0.1)
    pyautogui.rightClick()
    logger.info("右键点击屏幕 (%d, %d)", screen_x, screen_y)


# ============================================================
# 5. 滚动到目标行
# ============================================================
def scroll_to_row(hwnd, target_row: int, cfg: GameInteractConfig,
                  on_progress: Optional[Callable[[int, int], None]] = None):
    """滚动到目标行（含每 BATCH_SIZE 次滚轮→滑块上移修正）。"""
    import pyautogui
    import win32gui

    if target_row < cfg.VISIBLE_ROWS:
        logger.info("目标行 %d 在可视区域内，无需滚动", target_row)
        return

    rows_to_scroll = target_row
    total_pixels = rows_to_scroll * cfg.PIXELS_PER_ROW
    total_times = (total_pixels + cfg.SCROLL_STEP - 1) // cfg.SCROLL_STEP
    logger.info("目标行 %d，需滚动 %d 行，总滚轮次数 %d",
                target_row, rows_to_scroll, total_times)

    # 移动鼠标到左侧滚动条区域并聚焦
    rect = win32gui.GetWindowRect(hwnd)
    screen_x = rect[0] + cfg.SCROLL_MOUSE_X
    screen_y = rect[1] + cfg.SCROLL_MOUSE_Y
    pyautogui.moveTo(screen_x, screen_y, duration=0.1)
    pyautogui.click()
    time.sleep(0.1)

    executed = 0
    while executed < total_times:
        batch = min(cfg.BATCH_SIZE, total_times - executed)
        for _ in range(batch):
            pyautogui.scroll(-cfg.SCROLL_STEP)
            time.sleep(cfg.SCROLL_DELAY)
        executed += batch
        logger.info("滚轮进度 %d/%d", executed, total_times)
        if on_progress:
            on_progress(executed, total_times)

        # 每批滚轮后，滑块上移修正
        time.sleep(0.5)
        adjust_slider_up(cfg)
        time.sleep(0.3)


# ============================================================
# 6. 单件物品定位 + 右键
# ============================================================
def get_item_info_by_asset_id(csv_path: str, asset_id: str,
                              cfg: GameInteractConfig):
    """按 asset_id 精确定位库存物品，返回 (idx, row, cx, total_rows)。

    供"按方案匹配后点击"用：物品点击前一刻调用，保证定位基于最新
    库存顺序（仓库会随汰换执行而变化）。
    """
    encodings = ["utf-8-sig", "utf-8", "gbk", "gb2312"]
    rows = None
    for enc in encodings:
        try:
            with open(csv_path, "r", encoding=enc) as f:
                reader = csv.DictReader(f)
                rows = list(reader)
                break
        except Exception:
            continue
    if rows is None:
        raise ValueError(f"无法读取 CSV：{csv_path}")

    total_items = len(rows)
    total_rows = (total_items + cfg.COLS - 1) // cfg.COLS

    for idx, row in enumerate(rows):
        rid = (row.get("asset_id") or row.get("AssetId") or "").strip()
        if rid and rid == str(asset_id).strip():
            col = idx % cfg.COLS
            row_num = idx // cfg.COLS
            cx = (cfg.BIG_X + col * (cfg.CARD_W + cfg.GAP_X)
                  + cfg.CARD_W // 2)
            return idx, row_num, cx, total_rows
    raise ValueError(f"库存中未找到 asset_id={asset_id}")


def locate_and_click_item_by_asset_id(
        hwnd, csv_path: str, asset_id: str, cfg: GameInteractConfig,
        on_progress: Optional[Callable[[int, int], None]] = None) -> bool:
    """按 asset_id 定位并右键点击指定物品（点击前一刻重新读 CSV 定位）。"""
    try:
        idx, row, cx, total_rows = get_item_info_by_asset_id(
            csv_path, asset_id, cfg)
        logger.info("asset_id=%s 索引 %d 行 %d", asset_id, idx, row)
    except ValueError as e:
        logger.error(str(e))
        return False

    scroll_to_row(hwnd, row, cfg, on_progress=on_progress)
    # 滚动后目标所在的可视行号：
    # - row >= VISIBLE_ROWS：scroll_to_row 已把目标行滚到可视区顶部 → 可视行 0
    # - row < VISIBLE_ROWS：目标本就在可视区、未滚动 → 可视行 = row 本身
    # （2026-09-02 修复：此前 click_y 写死第 0 行中心，可视区内非首行
    #   物品会点到其正上方一行的物品，如 MP7(行1列3) 点成了 R8(行0列3)）
    visual_row = 0 if row >= cfg.VISIBLE_ROWS else row
    click_y = cfg.BIG_Y + visual_row * cfg.ROW_PITCH + cfg.CARD_H // 2
    click_item(hwnd, cx, click_y)
    return True


def locate_and_click_item(hwnd, csv_path: str, item_name: str,
                          cfg: GameInteractConfig,
                          on_progress: Optional[Callable[[int, int], None]] = None) -> bool:
    """定位并右键点击指定物品。

    Returns: True 成功点击，False 失败。
    """
    try:
        idx, row, cx, total_rows = get_item_info(csv_path, item_name, cfg)
        logger.info("物品 '%s' 索引 %d 行 %d", item_name, idx, row)
    except ValueError as e:
        logger.error(str(e))
        return False

    scroll_to_row(hwnd, row, cfg, on_progress=on_progress)

    # 点击位置：同 locate_and_click_item_by_asset_id 的可视行修正
    visual_row = 0 if row >= cfg.VISIBLE_ROWS else row
    click_y = cfg.BIG_Y + visual_row * cfg.ROW_PITCH + cfg.CARD_H // 2
    click_item(hwnd, cx, click_y)
    return True


# ============================================================
# 7. 批量点击多件物品
# ============================================================
@dataclass
class ClickResult:
    item_name: str
    success: bool
    message: str


def batch_click_items(item_names: list[str],
                      csv_path: str,
                      cfg: Optional[GameInteractConfig] = None,
                      on_item_start: Optional[Callable[[str, int, int], None]] = None,
                      on_item_done: Optional[Callable[[ClickResult], None]] = None,
                      on_scroll_progress: Optional[Callable[[int, int], None]] = None,
                      inter_item_delay: float = 1.0) -> list[ClickResult]:
    """批量右键点击多件物品。

    Args:
        item_names: 物品名称列表（支持部分匹配）
        csv_path: 库存 CSV 路径
        cfg: 游戏交互参数，None 用默认
        on_item_start: 回调 (item_name, idx_1based, total)
        on_item_done: 回调 (ClickResult)
        on_scroll_progress: 回调 (executed, total) 滚动进度
        inter_item_delay: 每件物品点击后等待秒数

    Returns: ClickResult 列表
    """
    if cfg is None:
        from utils.config import get_game_interact_config
        cfg = get_game_interact_config()

    results: list[ClickResult] = []

    deps_ok = _check_deps()
    if deps_ok is not True:
        for name in item_names:
            results.append(ClickResult(name, False,
                                       f"依赖缺失：{deps_ok} 未安装"))
        return results

    if not os.path.exists(csv_path):
        msg = f"库存 CSV 不存在：{csv_path}"
        for name in item_names:
            results.append(ClickResult(name, False, msg))
        return results

    hwnd = find_cs2_window()
    if hwnd is None:
        logger.error("未找到 CS2 窗口，请确保游戏已运行")
        for name in item_names:
            results.append(ClickResult(name, False, "未找到 CS2 窗口"))
        return results
    logger.info("CS2 窗口已激活，句柄 %s", hwnd)

    total = len(item_names)
    for i, name in enumerate(item_names, 1):
        if on_item_start:
            on_item_start(name, i, total)
        try:
            ok = locate_and_click_item(
                hwnd, csv_path, name, cfg,
                on_progress=on_scroll_progress)
            results.append(ClickResult(
                name, ok, "成功" if ok else "定位失败"))
        except Exception as e:
            logger.exception("点击 '%s' 时异常", name)
            results.append(ClickResult(name, False, f"异常：{e}"))
        if on_item_done:
            on_item_done(results[-1])
        time.sleep(inter_item_delay)

    return results


# ============================================================
# 8. 汰换合同流程（2026-09-02 新流程）
#    右键物品 → 左键弹窗「汰换」按钮 → 4 列过滤视图左键添加材料
# ============================================================
def load_inventory_rows(csv_path: str) -> list[dict]:
    """读取库存 CSV（多编码尝试），保持文件顺序返回 dict 列表。"""
    encodings = ["utf-8-sig", "utf-8", "gbk", "gb2312"]
    rows = None
    for enc in encodings:
        try:
            with open(csv_path, "r", encoding=enc) as f:
                rows = list(csv.DictReader(f))
                break
        except Exception:
            continue
    if rows is None:
        raise ValueError(f"无法读取 CSV：{csv_path}")
    return rows


def find_template_on_screen(template_path: str,
                            threshold: float = 0.8):
    """全屏截图模板匹配，返回匹配中心屏幕坐标 (x, y)。

    未找到（或分数低于阈值）返回 None。
    模板路径含中文时 cv2.imread 不可用，改用 np.fromfile + imdecode。
    """
    import cv2
    import numpy as np
    import pyautogui

    data = np.fromfile(template_path, dtype=np.uint8)
    tpl = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if tpl is None:
        raise ValueError(f"模板图片无法读取：{template_path}")
    scr = cv2.cvtColor(np.array(pyautogui.screenshot()),
                       cv2.COLOR_RGB2BGR)
    if (tpl.shape[0] > scr.shape[0]
            or tpl.shape[1] > scr.shape[1]):
        logger.warning("模板 %s 尺寸大于屏幕截图", template_path)
        return None
    res = cv2.matchTemplate(scr, tpl, cv2.TM_CCOEFF_NORMED)
    _, max_v, _, max_loc = cv2.minMaxLoc(res)
    logger.info("模板匹配 %s: score=%.4f at %s",
                os.path.basename(template_path), max_v, max_loc)
    if max_v < threshold:
        return None
    h, w = tpl.shape[:2]
    return max_loc[0] + w // 2, max_loc[1] + h // 2


def click_left_screen(x: int, y: int):
    """在屏幕坐标 (x, y) 左键点击。"""
    import pyautogui
    pyautogui.moveTo(x, y, duration=0.1)
    pyautogui.click()
    logger.info("左键点击屏幕 (%d, %d)", x, y)


def click_filtered_cell(hwnd, visual_row: int, col: int,
                        cfg: GameInteractConfig):
    """左键点击汰换合同过滤视图（4 列）中的单元格。

    Args:
        visual_row: 目标在当前可视区的行号（0 = 第一行可见行）
        col: 列号（0-3）
    """
    import pyautogui
    import win32gui

    cx = (cfg.FILTERED_BIG_X + col * cfg.FILTERED_PITCH_X
          + cfg.FILTERED_PITCH_X // 2)
    cy = cfg.FILTERED_BIG_Y + visual_row * cfg.FILTERED_PITCH_Y \
        + cfg.FILTERED_CLICK_DY
    left_top = win32gui.ClientToScreen(hwnd, (0, 0))
    pyautogui.moveTo(left_top[0] + cx, left_top[1] + cy, duration=0.1)
    pyautogui.click()
    logger.info("过滤视图左键 [可视行%d 列%d] 屏幕(%d, %d)",
                visual_row, col,
                left_top[0] + cx, left_top[1] + cy)


def scroll_filtered_rows(hwnd, notches: int,
                         cfg: GameInteractConfig):
    """过滤视图滚轮滚动（鼠标悬停在网格中部）。

    Args:
        notches: 正数 = 向下滚动（内容上移），负数 = 向上滚动
    """
    import pyautogui
    import win32gui

    left_top = win32gui.ClientToScreen(hwnd, (0, 0))
    # 网格中部（第2列/第2行附近），确保悬停在网格上方
    mx = left_top[0] + cfg.FILTERED_BIG_X + cfg.FILTERED_PITCH_X
    my = left_top[1] + cfg.FILTERED_BIG_Y + cfg.FILTERED_PITCH_Y
    pyautogui.moveTo(mx, my, duration=0.1)
    step = -cfg.SCROLL_STEP if notches > 0 else cfg.SCROLL_STEP
    for _ in range(abs(notches)):
        pyautogui.scroll(step)
        time.sleep(cfg.SCROLL_DELAY)
    logger.info("过滤视图滚动 %d 格滚轮", notches)
