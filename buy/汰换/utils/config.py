"""全局配置：路径、凭证、平台参数。

支持两种运行环境：
- 开发态：本文件位于 汰换/utils/config.py，所有路径基于 汰换/ 解析。
- 打包态（PyInstaller）：sys.executable 是 exe；sys._MEIPASS 是
  临时解压目录（含源码、vendor 子包、主 CSV 等只读资源）。
  用户可变数据（数据库、日志、items.csv）写入 exe 同级 data/ 目录，
  避免每次启动都从临时目录重新创建。
"""
import os
import sys


def _is_frozen() -> bool:
    return getattr(sys, "frozen", False)


# ---- 目录结构 ----
if _is_frozen():
    # PyInstaller 临时解压目录：只读资源（.py 源码、vendor/、data/主CSV）
    _BUNDLE_DIR = sys._MEIPASS  # type: ignore[attr-defined]
    # exe 同级目录：用户可写区域，存放 data/ 数据库与日志
    _APP_DIR = os.path.dirname(sys.executable)
else:
    # 开发态：config.py 位于 汰换/utils/config.py，BASE_DIR 应为 汰换/
    _BUNDLE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    _APP_DIR = _BUNDLE_DIR

BASE_DIR = _APP_DIR

# 外部平台脚本目录（原位于 buy/buff 和 buy/c5，已合并拷贝至 vendor/ 子包）
VENDOR_DIR = os.path.join(_BUNDLE_DIR, "vendor")
# 兼容旧代码：BUFF_DIR / C5_DIR 仍可作为 sys.path 入口指向 vendor
BUFF_DIR = VENDOR_DIR
C5_DIR = VENDOR_DIR

# 用户可变数据目录（开发态 = 汰换/data；打包态 = exe 同级 data/）
DATA_DIR = os.path.join(_APP_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "purchases.db")
ITEMS_CSV_PATH = os.path.join(DATA_DIR, "items.csv")

# 物品箱子磨损对照表（主数据源，只读资源；打包时与 vendor 一起 --add-data）
MAIN_ITEMS_CSV = os.path.join(
    _BUNDLE_DIR, "data", "物品箱子磨损对照表1_有效磨损及goods_id.csv")

# 汰换游戏内交互模板图片（模板匹配定位用，2026-09-02）
TEMPLATES_DIR = os.path.join(DATA_DIR, "templates")
# 右键物品后弹窗中的「汰换」按钮（260x48，image4）
TAIHUAN_POPUP_BTN = os.path.join(TEMPLATES_DIR, "taihuan_popup_btn.png")
# 库存界面右上角的汰换入口按钮（145x261，image2，暂未自动化）
TAIHUAN_ENTRY_BTN = os.path.join(TEMPLATES_DIR, "taihuan_entry_btn.png")

# 将 vendor 加入 sys.path，兼容原代码中 `import found_buff` 等顶层导入
if VENDOR_DIR not in sys.path:
    sys.path.insert(0, VENDOR_DIR)

# ---- 加载 .env：所有凭证一律从环境变量读取，禁止在源码中硬编码 ----
# 开发态：项目根目录 .env（F:\steamdt-project\.env）
# 打包态：exe 同级 .env
_ENV_CANDIDATES = [
    os.path.join(_BUNDLE_DIR, "..", "..", ".env"),
    os.path.join(_APP_DIR, ".env"),
]
try:
    from dotenv import load_dotenv
    for _p in _ENV_CANDIDATES:
        if os.path.exists(_p):
            load_dotenv(_p)
            break
except ImportError:  # python-dotenv 未安装时退化为纯环境变量
    pass

# ---- C5 平台参数（取自 found_c5.py 使用示例）----
C5_APP_KEY = os.getenv("C5_APP_KEY", "")
C5_APP_ID = 730

# ---- C5 白名单域名 + 回调 URL（本地解析服务配套使用）----
C5_WHITELIST_DOMAIN = "866868686.xyz"
# C5 购买接口的 tradeUrl 参数要求填 Steam 交易链接（非回调地址）
# 格式: https://steamcommunity.com/tradeoffer/new/?partner=XXX&token=YYY
# 获取: https://steamcommunity.com/my/tradeoffers/privacy 页面「交易链接」
C5_TRADE_URL = os.getenv("STEAM_TRADE_URL", "")
# C5 回调通知地址（与 tradeUrl 不同，这是订单状态回调）
C5_CALLBACK_URL = f"http://{C5_WHITELIST_DOMAIN}/c5/callback"
C5_CALLBACK_LISTEN_HOST = "0.0.0.0"
C5_CALLBACK_DEFAULT_PORT = 80

# ---- C5 网页版 API（curl.exe 绕 WAF，全自动购买）----
# 网页版 create 接口必填的收货 SteamID64
C5_STEAM_ID = os.getenv("C5_STEAM_ID", "") or os.getenv("STEAM_ID", "")

# 确保数据目录存在
os.makedirs(DATA_DIR, exist_ok=True)


# ============================================================
# Buff 爬取强度配置（可调，防止 429 限流）
# ============================================================
# 单个物品爬取的最大页数（每页约80条，3页≈240条）
BUFF_MAX_PAGES = 3
# 每页请求之间的间隔秒数（越大越安全，2.0 为默认）
BUFF_PAGE_DELAY = 2.0
# 触发 429 后的退避基础秒数（实际等待 = RETRY * 2^attempt）
BUFF_RETRY_DELAY = 5

# C5 平台翻页数
C5_MAX_PAGES = 5

# ============================================================
# ECO 网页爬虫 Cookie（从浏览器 F12 复制）
#   优先读环境变量 ECO_WEB_COOKIE；为空时由 core/user_settings.py
#   读取 GUI 保存的 data/user_settings.json 覆盖。
# ============================================================
ECO_WEB_COOKIE = os.getenv("ECO_WEB_COOKIE", "")


# ============================================================
# ECO 网页汰换官方模拟（StartSimulation）配置
#  双体系说明：
#   - openapi.ecosteam.cn：RSA 签名开放平台，走现有 core/eco_client.py
#   - www.ecosteam.cn/Api/ReplaceSimulation/*：网页端 Cookie 接口，本轮新增封装
# ============================================================
# 是否优先使用 ECO 官方 StartSimulation 计算产出磨损&概率；
# False=只用本地估算（不发网络请求，无 Cookie 依赖）
ECO_WEB_USE_OFFICIAL_SIMULATION = True
# StartSimulation 接口 URL
ECO_WEB_START_SIM_URL = (
    "https://www.ecosteam.cn/Api/ReplaceSimulation/StartSimulation"
)
# 请求超时（秒）
ECO_WEB_START_SIM_TIMEOUT = 25
# 相同材料集合的结果 LRU 缓存条目数（避免同一 combo 短时间重复请求触发去重）
ECO_WEB_SIM_CACHE_SIZE = 128
# 其余 6 个汰换配套接口
ECO_WEB_USER_CUSTOM_LIST_URL = (
    "https://www.ecosteam.cn/Api/ReplaceSimulation/UserCustomList"
)
ECO_WEB_CUSTOM_DELETE_URL = (
    "https://www.ecosteam.cn/Api/ReplaceSimulation/CustomDelete"
)
ECO_WEB_CUSTOM_BATCH_ADD_URL = (
    "https://www.ecosteam.cn/Api/ReplaceSimulation/customBatchAdd"
)
ECO_WEB_SAVE_SIM_RESULT_URL = (
    "https://www.ecosteam.cn/Api/ReplaceSimulation/SaveSimulationResult"
)
ECO_WEB_FORMULA_DETAIL_URL = (
    "https://www.ecosteam.cn/Api/ReplaceSimulation/FormulaDetail"
)
ECO_WEB_REAL_REPLACE_RESULT_URL = (
    "https://www.ecosteam.cn/Api/ReplaceDiagnosis/RealReplaceResult"
)
# 配套接口统一超时（含列表/保存/详情等轻量接口）
ECO_WEB_API_TIMEOUT = 20
# LRU 缓存大小（读接口 UserCustomList/FormulaDetail/RealReplaceResult 各自独立）
ECO_WEB_USER_CUSTOM_CACHE_SIZE = 16
ECO_WEB_FORMULA_DETAIL_CACHE_SIZE = 64
ECO_WEB_REAL_REPLACE_CACHE_SIZE = 64
# ============================================================
# ECO 网页汰换接口全局节流（避免 www.ecosteam.cn 短时间多次请求
# 触发 429 或风控，你可以按自己账号情况自定义）
#  说明：每发 1 次 www.ecosteam.cn/Api/* HTTP 请求前会先 sleep 这么多秒，
#        用于主动"放慢"并发（即使 LRU 命中时也按这个节奏，因为 API 级的限流
#        对用户账号是全局的，所以这里给的是最稳妥的单请求前间隔）。
#  StartSimulation 作为最重接口可以单独给更大间隔。
#  单位：秒；允许填 0 = 不额外节流（仅靠现有 LRU/指数退避）
# ============================================================
ECO_WEB_REQUEST_INTERVAL = 0.5      # 所有 ECO 网页汰换 API 的通用请求前间隔（默认 0.5s）
ECO_WEB_START_SIM_INTERVAL = 1.5    # StartSimulation 专属请求前间隔（默认 1.5s，叠加通用间隔）

# ============================================================
# ECO 开放平台（openapi.ecosteam.cn，RSA 签名体系）全局请求节流
#   → 解决 ResultCode=6001 "接口请求频次过快"。
#   10 件同收藏同皮肤的汰换产出会在 ThreadPoolExecutor(16) 里并发调
#   SellGoodsList（再叠加 3 档命名降级重试 → 1s 内 40+ 次 → 6001 限流）。
#   这里给的默认 1.2s ≈ 50次/分钟，对合作方白名单账号比较稳。
#   GUI 可通过 user_settings.eco_openapi_request_interval_s 覆盖，
#   优先级与 BUFF_REQUEST_INTERVAL 一致：GUI > config 默认。
# ============================================================
ECO_OPENAPI_REQUEST_INTERVAL = 1.2  # 秒，范围 [0.0, 30.0]

# ============================================================
# C5 开放平台（openapi.c5game.com）全局请求节流
#   → 避免 merchant/market/v2/products/list 返回 429 Too Many Requests。
#   C5 合作方 IP 白名单有严格的分钟/秒级限额，10 件并发
#   重复 query_c5 时很容易在第二、三件就 429。
#   同样支持 GUI 覆盖：user_settings.c5_request_interval_s。
# ============================================================
C5_REQUEST_INTERVAL = 1.0           # 秒

# ============================================================
# Buff 平台查询全局节流（避免 buff.163.com 在「10 件材料并发查价」时
#   命中 429「触发限流」，导致部分价格 None / 成本偏低）。
#  说明：每条发往 Buff /api/market/goods/sell_order 的 HTTP 请求（包括
#        第一页、翻页、10 件并发里的每件单独查询）发出前都会先 sleep
#        这个间隔。默认 1.0s 对普通账号较稳；0 = 仅靠指数退避，有 429 风险。
#  GUI 可覆盖：用户在主界面「槽位旁边」的节流框里自定义的值优先级更高，
#  会保存在 data/user_settings.json，不会修改本 config.py 文件。
#  单位：秒，范围 [0.0, 30.0]
# ============================================================
BUFF_REQUEST_INTERVAL = 1.0         # 每条 BUFF 查询请求前的通用间隔（默认 1.0s）

# ============================================================
# 【Buff 浏览器查询间隔】Playwright 浏览器爬取（汰换/查询共用的
# query_buff_via_browser 通道）每次查询任务之间的间隔：
#   实际等待 = BUFF_BROWSER_QUERY_INTERVAL + random(0, BUFF_BROWSER_QUERY_JITTER)
#   即 1.5s 基础 + 0~2.0s 随机震荡，拟人化爬取节奏，降低触发安全验证的概率。
#   worker 线程串行执行任务，时间戳节流即可，无需加锁。
# ============================================================
BUFF_BROWSER_QUERY_INTERVAL = 1.5   # 秒
BUFF_BROWSER_QUERY_JITTER = 2.0     # 秒（0~2.0 随机）


# ============================================================
# 游戏内交互参数（可调，便于校准）
# ============================================================
class GameInteractConfig:
    """CS2 游戏内物品定位参数。

    这些值来自原 cs2滚动查询.py 的实测：
    - 网格 9 列 × 卡片 165×190，间距 28，起始 (77, 154)
    - 可视 5 行，每行 250 像素
    - 滚动步进 125，每 8 次滚轮后滑块上移 1 像素修正
    - 滑块轨道 X=1815，宽 40，顶 178，底 960

    修改这些值即可在 GUI 中实时调整，无需改源码。
    """
    # 网格布局
    COLS = 9
    CARD_W = 165
    CARD_H = 190
    GAP_X = 28
    BIG_X = 77
    BIG_Y = 154
    VISIBLE_ROWS = 5
    # 垂直行距（2026-09-02 截图实测：卡片内部物品图底边位于
    # 卡片顶 +143，序列 297/487/677/867/1057 间距恒 190，
    # 与 BIG_Y=154 行顶系列一一对应，确认行距 = 190）
    ROW_PITCH = 190

    # 滚动参数（2026-08-31 实测标定）：
    # - 1 格滚轮（pyautogui.scroll(-125)）滚动 2 行物品
    # - 每滚动 10 格物品（= 5 格滚轮）后，滑块上移 1 像素修正
    PIXELS_PER_ROW = 62.5   # 1 行 = 125/2（1 格滚轮 125 像素滚动 2 行）
    SCROLL_STEP = 125
    SCROLL_MOUSE_X = 30
    SCROLL_MOUSE_Y = 613
    SCROLL_DELAY = 0.05

    # 滑块轨道参数
    TRACK_X = 1815
    TRACK_WIDTH = 40
    TRACK_TOP = 178
    TRACK_BOTTOM = 960
    # TRACK_HEIGHT 在运行时动态计算（= TRACK_BOTTOM - TRACK_TOP）

    def __init__(self):
        # 实例属性，便于运行时调整
        self.TRACK_HEIGHT = self.TRACK_BOTTOM - self.TRACK_TOP

    # ★ 滚轮批大小与滑块修正像素（核心可调参数）
    BATCH_SIZE = 5           # 每 5 格滚轮（= 10 格物品）后滑块修正
    SLIDER_ADJUST_PIXEL = 1  # 滑块每次上移 1 像素

    # 汰换合同过滤视图（4 列，2026-09-02 由 image5 截图实测标定）：
    # - 卡片与 9 列视图同尺寸，但每行只排 4 个，右侧为汰换栏
    # - 列左边缘：53 / 245 / 437 / 629（列距 192）
    # - 行上边缘：306 / 493 / 680 / 867（行距 187，可见 4 行）
    FILTERED_COLS = 4
    FILTERED_BIG_X = 53        # 第一列卡片左边缘（客户区坐标）
    FILTERED_PITCH_X = 192     # 列间距
    FILTERED_BIG_Y = 306       # 第一行卡片上边缘
    FILTERED_PITCH_Y = 187     # 行间距
    FILTERED_VISIBLE_ROWS = 4  # 可完整点击的可见行数
    # 每格滚轮滚动的行数（2026-09-02 相位相关实测标定）：
    # 过滤视图 1 格 scroll(-125) = 96px = 96/190 ≈ 0.505 行，
    # 与仓库视图（125px=2 行）完全不同！连续快速滚动线性无惯性。
    FILTERED_ROWS_PER_NOTCH = 96 / 190
    FILTERED_CLICK_DY = 90     # 点击位置距卡片顶部像素

    # 拖拽延迟
    DRAG_DURATION = 0.1

    # 调试模式
    DEBUG = False


def get_game_interact_config() -> "GameInteractConfig":
    """返回游戏交互配置实例（未来可扩展为从 JSON 加载）。"""
    return GameInteractConfig()


# ============================================================
# 自动汰换编排参数
# ============================================================
# 每件材料点击后等待秒数（让游戏 UI 响应）
TAIHUAN_INTER_ITEM_DELAY = 1.0

# 汰换前/后是否自动刷新库存
TAIHUAN_REFRESH_INVENTORY_BEFORE = True
TAIHUAN_REFRESH_INVENTORY_AFTER = True

