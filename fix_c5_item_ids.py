"""通过 C5 OpenAPI 批量修正「各平台物品id表.csv」中 C5 平台的物品 ID。

原理：
    1. 读取 CSV，收集所有 C5 平台行的「市场哈希名称」
    2. 分批调用 POST /merchant/product/price/batch 查询真实 itemId
       （该接口返回「有在售挂单」的物品，未返回的物品保持原值）
    3. 逐行对比：API 返回的 itemId 与 CSV 现有值不一致则修正
    4. 写回前自动备份原表（.bak_时间戳）

用法：
    python fix_c5_item_ids.py [--csv 路径] [--batch 50] [--sleep 0.25]
"""
import argparse
import csv
import json
import os
import shutil
import sys
import time

import requests
from dotenv import load_dotenv

# C5 app-key：从项目根目录 .env 读取（见 .env.example），禁止硬编码。
# 未配置时启动即报错，避免"静默用错密钥"。
load_dotenv()
APP_KEY = os.getenv("C5_APP_KEY", "")
if not APP_KEY:
    raise SystemExit(
        "[配置错误] 未设置 C5_APP_KEY。请复制 .env.example 为 .env 并填入你的 C5 app-key。")
C5_API_URL = "https://openapi.c5game.com/merchant/product/price/batch"
C5_APP_ID = "730"
# 主物品表路径：可用 --csv 覆盖；默认相对项目根目录解析，不写死绝对路径。
DEFAULT_CSV = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "数据集", "各平台物品id表.csv")


def query_batch(app_key: str, names: list, timeout: int = 20) -> dict:
    """调用 C5 batch 接口查询一批 marketHashName 的 itemId。"""
    resp = requests.post(
        C5_API_URL,
        params={"app-key": app_key},
        json={"appId": C5_APP_ID, "marketHashNames": names},
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()


def build_mapping(app_key: str, names: list, batch_size: int, sleep_s: float) -> dict:
    """分批查询，返回 {marketHashName: itemId} 完整映射。"""
    mapping = {}
    total = len(names)
    total_batches = (total + batch_size - 1) // batch_size
    for idx, start in enumerate(range(0, total, batch_size)):
        batch = names[start:start + batch_size]
        for attempt in range(3):
            try:
                result = query_batch(app_key, batch)
                break
            except Exception as e:
                print(f"  批次 {idx + 1}/{total_batches} 请求异常({attempt + 1}/3): {e}")
                time.sleep(2 * (attempt + 1))
        else:
            print(f"  批次 {idx + 1}/{total_batches} 连续失败，跳过该批")
            continue
        data = result.get("data") or {}
        for name in batch:
            hit = data.get(name)
            if hit and hit.get("itemId"):
                mapping[name] = str(hit["itemId"])
        if (idx + 1) % 25 == 0 or idx + 1 == total_batches:
            print(f"  进度 {idx + 1}/{total_batches} 批，已命中 {len(mapping)} 个名称", flush=True)
        time.sleep(sleep_s)
    return mapping


def main() -> int:
    parser = argparse.ArgumentParser(description="修正 CSV 中 C5 平台物品 ID")
    parser.add_argument("--csv", default=DEFAULT_CSV, help="要修正的 CSV 路径")
    parser.add_argument("--batch", type=int, default=50, help="每批查询的名称数（默认 50）")
    parser.add_argument("--sleep", type=float, default=0.25, help="每批间隔秒（默认 0.25）")
    parser.add_argument("--cache", default=None,
                        help="映射缓存 JSON 路径；存在则跳过查询直接加载（默认 csv 同目录 c5_item_id_mapping.json）")
    args = parser.parse_args()

    csv_path = args.csv
    if not os.path.exists(csv_path):
        print(f"[错误] CSV 不存在: {csv_path}")
        return 1

    cache_path = args.cache or os.path.join(os.path.dirname(csv_path), "c5_item_id_mapping.json")

    # 1. 读 CSV
    with open(csv_path, "r", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        header = next(reader)
        rows = list(reader)
    c5_idx = [i for i, r in enumerate(rows) if len(r) >= 4 and (r[2] or "").strip() == "C5"]
    if not c5_idx:
        print("[错误] CSV 中没有 C5 平台记录")
        return 1
    unique_names = sorted({rows[i][1].strip() for i in c5_idx})
    print(f"C5 平台行数: {len(c5_idx)}，唯一 marketHashName: {len(unique_names)}")

    # 2. 获取 itemId 映射（优先缓存）
    if os.path.exists(cache_path):
        with open(cache_path, "r", encoding="utf-8") as f:
            mapping = json.load(f)
        print(f"已加载映射缓存: {cache_path}（{len(mapping)} 条）")
    else:
        print(f"开始分批查询（每批 {args.batch} 个，间隔 {args.sleep}s，共 "
              f"{(len(unique_names) + args.batch - 1) // args.batch} 批）...")
        mapping = build_mapping(APP_KEY, unique_names, args.batch, args.sleep)
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(mapping, f, ensure_ascii=False, indent=1)
        print(f"映射结果已缓存到: {cache_path}")

    # 3. 对比修正
    changed, same, keep = 0, 0, 0
    for i in c5_idx:
        name = rows[i][1].strip()
        old = rows[i][3].strip()
        new = mapping.get(name)
        if new is None:
            keep += 1  # API 未返回（无在售挂单等），保持原值
        elif new == old:
            same += 1
        else:
            rows[i][3] = new
            changed += 1

    # 4. 备份后写回
    backup = f"{csv_path}.bak_{time.strftime('%Y%m%d_%H%M%S')}"
    shutil.copy2(csv_path, backup)
    try:
        with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow(header)
            writer.writerows(rows)
    except PermissionError:
        print(f"\n[错误] CSV 被其他程序占用，无法写入: {csv_path}")
        print(f"请关闭正在打开该文件的程序（Excel/WPS/记事本等），"
              f"然后重新运行: python fix_c5_item_ids.py --cache {cache_path}")
        print(f"（原表已备份: {backup}，本次修正内容尚未丢失，映射缓存已保存）")
        return 2
    print(f"\n完成！备份: {backup}")
    print(f"  命中 API 的条目: {same + changed}  (其中修正 {changed} 条，一致 {same} 条)")
    print(f"  API 未返回、保持原值: {keep} 条")
    print(f"  本次查询共命中 {len(mapping)} / {len(unique_names)} 个唯一名称")
    return 0


if __name__ == "__main__":
    sys.exit(main())