import os
import csv
import json
import time
import logging
import requests
from dotenv import load_dotenv
from typing import Optional, List, Dict, Any

# ---------- 数据模型定义 ----------
class PlatformPriceVO:
    """平台价格信息"""
    def __init__(
        self,
        bidding_count: Optional[int] = None,
        bidding_price: Optional[float] = None,
        platform: Optional[str] = None,
        platform_item_id: Optional[str] = None,
        sell_count: Optional[int] = None,
        sell_price: Optional[float] = None,
        update_time: Optional[int] = None,
    ):
        self.bidding_count = bidding_count
        self.bidding_price = bidding_price
        self.platform = platform
        self.platform_item_id = platform_item_id
        self.sell_count = sell_count
        self.sell_price = sell_price
        self.update_time = update_time

    @classmethod
    def from_dict(cls, data: dict) -> "PlatformPriceVO":
        return cls(
            bidding_count=data.get("biddingCount"),
            bidding_price=data.get("biddingPrice"),
            platform=data.get("platform"),
            platform_item_id=data.get("platformItemId"),
            sell_count=data.get("sellCount"),
            sell_price=data.get("sellPrice"),
            update_time=data.get("updateTime"),
        )


class ApifoxModel:
    """统一响应模型"""
    def __init__(
        self,
        data: Optional[List[PlatformPriceVO]] = None,
        error_code: Optional[int] = None,
        error_code_str: Optional[str] = None,
        error_data: Optional[Dict[str, Any]] = None,
        error_msg: Optional[str] = None,
        success: Optional[bool] = None,
    ):
        self.data = data
        self.error_code = error_code
        self.error_code_str = error_code_str
        self.error_data = error_data
        self.error_msg = error_msg
        self.success = success

    @classmethod
    def from_dict(cls, resp: dict) -> "ApifoxModel":
        data_list = resp.get("data")
        if data_list and isinstance(data_list, list):
            parsed_data = [PlatformPriceVO.from_dict(item) for item in data_list]
        else:
            parsed_data = None
        return cls(
            data=parsed_data,
            error_code=resp.get("errorCode"),
            error_code_str=resp.get("errorCodeStr"),
            error_data=resp.get("errorData"),
            error_msg=resp.get("errorMsg"),
            success=resp.get("success"),
        )


class PlatformBaseInfoVO:
    """平台基础信息（平台名称 + 平台饰品 id）。"""

    def __init__(self, name: Optional[str] = None, item_id: Optional[str] = None):
        self.name = name
        self.item_id = item_id

    @classmethod
    def from_dict(cls, data: dict) -> "PlatformBaseInfoVO":
        return cls(name=data.get("name"), item_id=data.get("itemId"))

    def to_dict(self) -> dict:
        return {"name": self.name, "itemId": self.item_id}


class BaseInfoVO:
    """饰品基础信息（/open/cs2/v1/base 返回条目）。"""

    def __init__(
        self,
        name: Optional[str] = None,
        market_hash_name: Optional[str] = None,
        platform_list: Optional[List[PlatformBaseInfoVO]] = None,
    ):
        self.name = name
        self.market_hash_name = market_hash_name
        self.platform_list = platform_list or []

    @classmethod
    def from_dict(cls, data: dict) -> "BaseInfoVO":
        platforms = [
            PlatformBaseInfoVO.from_dict(p)
            for p in (data.get("platformList") or [])
        ]
        return cls(
            name=data.get("name"),
            market_hash_name=data.get("marketHashName"),
            platform_list=platforms,
        )

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "marketHashName": self.market_hash_name,
            "platformList": [p.to_dict() for p in self.platform_list],
        }


# ---------- 日志配置 ----------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

load_dotenv()

STEAMDT_API_KEY = os.getenv("STEAMDT_API_KEY")
STEAMDT_BASE_URL = os.getenv("STEAMDT_BASE_URL", "https://open.steamdt.com")
CALLBACK_URL = os.getenv("CALLBACK_URL")

# ---------- base 接口缓存（该接口每天只能调用一次，必须本地保存） ----------
BASE_CACHE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "steam_items_cache.json")
BASE_CSV_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "各平台物品id表.csv")
# 距上次保存超过该时间（秒）视为过期，可重新拉取。23 小时，给每日调用留余量。
BASE_CACHE_TTL_SECONDS = 23 * 3600


class SteamDTClient:
    """SteamDT API 客户端（类型化版本）"""

    def __init__(self, api_key: Optional[str] = None, base_url: Optional[str] = None):
        self.api_key = api_key or STEAMDT_API_KEY
        self.base_url = base_url or STEAMDT_BASE_URL

        if not self.api_key:
            raise ValueError("API Key 未设置，请在 .env 文件中配置 STEAMDT_API_KEY")

        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "User-Agent": "SteamDT-Python-Client/1.0.0"
        })

    def _request(self, method: str, endpoint: str, data: Optional[dict] = None, params: Optional[dict] = None) -> dict:
        """发送请求并返回原始 JSON 字典"""
        url = f"{self.base_url}{endpoint}"
        logger.info(f"发送请求: {method} {url}")

        try:
            response = self.session.request(method, url, json=data, params=params)
            logger.info(f"响应状态码: {response.status_code}")
            result = response.json()
            logger.debug(f"响应内容: {json.dumps(result, indent=2, ensure_ascii=False)}")
            return result
        except requests.exceptions.RequestException as e:
            logger.error(f"请求失败: {str(e)}")
            if hasattr(e, "response") and e.response:
                logger.error(f"响应内容: {e.response.text}")
            raise

    def get(self, endpoint: str, params: Optional[dict] = None) -> dict:
        return self._request("GET", endpoint, params=params)

    def post(self, endpoint: str, data: Optional[dict] = None) -> dict:
        return self._request("POST", endpoint, data=data)

    # ---------- 核心接口（返回类型化对象） ----------
    def get_price(self, market_hash_name: str) -> ApifoxModel:
        """
        通过 marketHashName 查询饰品价格
        返回 ApifoxModel 对象，包含平台价格列表
        """
        params = {"marketHashName": market_hash_name}
        raw = self.get("/open/cs2/v1/price/single", params=params)
        return ApifoxModel.from_dict(raw)

    def batch_get_price(self, market_hash_names: List[str]) -> Dict[str, List[PlatformPriceVO]]:
        """
        批量查询饰品价格（最多 100 个）。
        返回 { marketHashName: [PlatformPriceVO, ...] }

        失败时抛出 RuntimeError，errorMsg 中会包含"上限"等频率限制提示。
        """
        if not market_hash_names:
            return {}
        names = list(market_hash_names[:100])
        raw = self.post("/open/cs2/v1/price/batch",
                        data={"marketHashNames": names})
        result: Dict[str, List[PlatformPriceVO]] = {}
        if not raw.get("success"):
            err = raw.get("errorMsg") or raw.get("errorCodeStr") or "未知错误"
            raise RuntimeError(f"批量价格查询失败: {err}")
        for item in (raw.get("data") or []):
            mhn = item.get("marketHashName", "")
            data_list = item.get("dataList") or []
            result[mhn] = [PlatformPriceVO.from_dict(d) for d in data_list]
        return result

    def get_wear(self, inspect_url: str, notify_url: Optional[str] = None) -> dict:
        """
        查询磨损（异步回调）
        仍返回原始 dict，因为回调结果是异步的，此处不解析
        """
        url = notify_url or CALLBACK_URL
        if not url:
            raise ValueError("notify_url 未设置，请在 .env 中配置 CALLBACK_URL 或传入参数")
        payload = {
            "inspectUrl": inspect_url,
            "notifyUrl": url
        }
        return self.post("/open/cs2/v1/wear", payload)

    def get_base_info(self) -> List[BaseInfoVO]:
        """
        获取全部饰品基础信息（name / marketHashName / 各平台 itemId）。

        注意：该接口每天只能调用一次，请配合 save_base_info 保存返回结果。
        """
        raw = self.get("/open/cs2/v1/base")
        if not raw.get("success"):
            err = raw.get("errorMsg") or raw.get("errorCodeStr") or raw.get("errorCode")
            raise RuntimeError(f"获取饰品基础信息失败: {err}")
        data = raw.get("data") or []
        return [BaseInfoVO.from_dict(item) for item in data]


# ---------- base 缓存读写 ----------

def save_base_info(items: List[BaseInfoVO]) -> str:
    """将 base 接口返回保存到本地：JSON 全量缓存 + CSV 平台物品 id 表。"""
    with open(BASE_CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump([it.to_dict() for it in items], f,
                  ensure_ascii=False, indent=2)
    with open(BASE_CSV_PATH, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["皮肤名称", "市场哈希名称", "平台", "平台物品ID"])
        for it in items:
            for p in it.platform_list:
                writer.writerow([it.name, it.market_hash_name, p.name, p.item_id])
    return (f"已保存 {len(items)} 条饰品基础信息\n"
            f"  JSON: {BASE_CACHE_PATH}\n"
            f"  CSV : {BASE_CSV_PATH}")


def load_base_info_cache() -> List[BaseInfoVO]:
    """从本地 JSON 缓存读取 base 数据；不存在或损坏时返回空列表。"""
    if not os.path.exists(BASE_CACHE_PATH):
        return []
    try:
        with open(BASE_CACHE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return [BaseInfoVO.from_dict(it) for it in data]
    except Exception as e:
        logger.warning(f"读取 base 缓存失败: {e}")
        return []


def base_cache_stale() -> bool:
    """base 缓存是否过期（超过 23 小时视为过期，需重新拉取）。"""
    if not os.path.exists(BASE_CACHE_PATH):
        return True
    age = time.time() - os.path.getmtime(BASE_CACHE_PATH)
    return age > BASE_CACHE_TTL_SECONDS


def get_or_fetch_base_info(force_refresh: bool = False) -> List[BaseInfoVO]:
    """
    获取饰品基础信息：优先读本地缓存，缓存缺失/过期时才调用接口并保存。
    调用接口会消耗当天的一次调用额度，请谨慎。
    """
    if not force_refresh and not base_cache_stale():
        cached = load_base_info_cache()
        if cached:
            logger.info(f"使用本地缓存（{len(cached)} 条，"
                        f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(os.path.getmtime(BASE_CACHE_PATH)))} 保存）")
            return cached

    logger.info("缓存缺失或已过期，开始调用 /open/cs2/v1/base 接口（消耗当日一次调用额度）")
    client = SteamDTClient()
    items = client.get_base_info()
    if not items:
        raise RuntimeError("接口返回空数据")
    print(save_base_info(items))
    return items


# ---------- 命令行入口 ----------
def main():
    import argparse

    parser = argparse.ArgumentParser(
        prog="send_request",
        description="SteamDT 开放平台客户端")
    sub = parser.add_subparsers(dest="cmd")

    # base：饰品基础信息（每天只能调用一次，默认读缓存）
    p_base = sub.add_parser("base", help="获取/刷新饰品基础信息缓存")
    p_base.add_argument("--refresh", action="store_true",
                        help="强制调用接口刷新（消耗当日一次调用额度）")

    # price：按 marketHashName 查询价格
    p_price = sub.add_parser("price", help="查询饰品价格")
    p_price.add_argument("market_hash_name", help="marketHashName，如 AWP | Wildfire (Field-Tested)")

    args = parser.parse_args()

    try:
        if args.cmd == "base":
            items = get_or_fetch_base_info(force_refresh=args.refresh)
            logger.info(f"🧾 基础信息共 {len(items)} 条")
            sample = items[0] if items else None
            if sample:
                plats = ", ".join(f"{p.name}={p.item_id}" for p in sample.platform_list)
                logger.info(f"示例: {sample.name} | {sample.market_hash_name} | {plats}")
        elif args.cmd == "price":
            client = SteamDTClient()
            price_resp = client.get_price(args.market_hash_name)
            if price_resp.success and price_resp.data:
                for item in price_resp.data:
                    logger.info(
                        f"平台: {item.platform}, "
                        f"在售: {item.sell_price}, "
                        f"求购: {item.bidding_price}"
                    )
            else:
                logger.error(f"价格查询失败: {price_resp.error_msg}")
        else:
            parser.print_help()
    except Exception as e:
        logger.error(f"❌ 执行失败: {str(e)}", exc_info=True)
        raise SystemExit(1)


if __name__ == "__main__":
    main()