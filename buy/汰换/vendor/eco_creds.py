"""ECO 开放平台凭证占位文件。

按项目约束：敏感数据由原脚本管理，GUI 不存储。
此文件仅提供占位与读取入口，实际密钥请用户填入或通过环境变量加载。

ECO 开放平台接入流程：
1. 注册登录 ECOSteam App
2. 我的 → 右上角设置 → 账号与安全 → 开放能力申请
3. 审核通过后，回到页面点击「查看身份ID」
4. 输入 RSA 公钥，获取身份ID（PartnerId）
5. 将 PartnerId 与 RSA 私钥填入下方或设置为环境变量
"""
import os

# ============ 凭证（全部从环境变量 / 项目根目录 .env 读取）============
# 加载项目根目录 .env（开发态 = F:\steamdt-project\.env）
# 路径：buy/汰换/vendor/eco_creds.py → 上溯 4 层到项目根
try:
    from dotenv import load_dotenv

    load_dotenv(os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__))))), ".env"))
except ImportError:
    pass

# 你的 ECO PartnerId（身份ID）
ECO_PARTNER_ID = os.environ.get("ECO_PARTNER_ID", "").strip()

# 你的 RSA PKCS8 格式私钥（去掉头尾标识和换行符的纯 Base64 字符串）
# 即你在 ECO 平台申请时使用的公钥对应的私钥
ECO_RSA_PRIVATE_KEY = os.environ.get("ECO_RSA_PRIVATE_KEY", "").strip()

# ECO 平台 RSA 公钥（用于验签回调，可选）
ECO_RSA_PUBLIC_KEY = os.environ.get("ECO_RSA_PUBLIC_KEY", "").strip()

# ============ 交易链接（用于 SteamInventoryQuery 接口）============
# 从 https://steamcommunity.com/tradeoffer/new/?partner=XXX&token=YYY 解析
STEAM_TRADE_PARTNER = os.environ.get("STEAM_TRADE_PARTNER", "")
STEAM_TRADE_TOKEN = os.environ.get("STEAM_TRADE_TOKEN", "")

# ============ SteamId（用于 QueryStock 接口）============
STEAM_ID = os.environ.get("STEAM_ID", "").strip()

# CS2 的 Steam 游戏 ID
CS2_GAME_ID = "730"


def is_configured() -> bool:
    """检查是否已填入必要凭证。"""
    return bool(ECO_PARTNER_ID and ECO_RSA_PRIVATE_KEY)
