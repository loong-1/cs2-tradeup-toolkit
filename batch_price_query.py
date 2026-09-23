import csv
import time
import logging
from send_request import SteamDTClient

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def read_names(csv_path):
    with open(csv_path, 'r', encoding='utf-8-sig') as f:
        reader = csv.reader(f)
        next(reader, None)
        names = [row[1].strip() for row in reader if len(row) >= 2 and row[1].strip()]
    return list(dict.fromkeys(names))

def main():
    client = SteamDTClient()
    names = read_names("items_table.csv")
    logger.info(f"共 {len(names)} 个唯一饰品名称")

    for name in names:
        logger.info(f"查询: {name}")
        result = client.get_price(name)
        if result.get('success') and result.get('data'):
            for item in result['data']:
                print(f"{name} | {item['platform']} | 在售:{item.get('sellPrice')} | 求购:{item.get('biddingPrice')}")
        else:
            logger.warning(f"无数据或失败: {result.get('errorMsg')}")
        time.sleep(0.3)

if __name__ == '__main__':
    main()