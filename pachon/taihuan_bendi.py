import csv
import itertools
import time
import random
import signal
import sys
from collections import defaultdict
from typing import List, Dict, Optional, Tuple

from find_price_buff import get_lowest_sell_price

# ---------- 全局变量，用于保存状态 ----------
_skins_global = []
_csv_path_global = ""
_price_cache_global = {}

# ---------- 常量 ----------
WEAR_THRESHOLDS = {
    '崭新出厂': 0.07,
    '略有磨损': 0.15,
    '久经沙场': 0.38,
    '破损不堪': 0.45,
    '战痕累累': 1.0
}

QUALITY_LEVELS = {
    '消费级': 0,
    '工业级': 1,
    '军规级': 2,
    '受限级': 3,
    '保密级': 4,
    '隐秘级': 5,
    '稀有特殊物品': 6
}

# ---------- 数据类 ----------
class Skin:
    def __init__(self, collection: str, name: str, quality: str, min_f: float, max_f: float,
                 is_stattrak: bool, goods_id: int = 0, actual_wear: float = None,
                 wear_grade: str = '', price: float = 0.0):
        self.collection = collection
        self.name = name
        self.quality = quality
        self.min_f = min_f
        self.max_f = max_f
        self.is_stattrak = is_stattrak
        self.goods_id = goods_id
        self.price = price
        self.actual_wear = actual_wear
        self.wear_grade = wear_grade

# ---------- 辅助函数 ----------
def get_next_quality(quality: str) -> Optional[str]:
    level = QUALITY_LEVELS.get(quality)
    if level is None or level >= 5:
        return None
    for q, l in QUALITY_LEVELS.items():
        if l == level + 1:
            return q
    return None

def get_wear_grade(float_val: float) -> str:
    if float_val <= 0.07:
        return '崭新出厂'
    elif float_val <= 0.15:
        return '略有磨损'
    elif float_val <= 0.38:
        return '久经沙场'
    elif float_val <= 0.45:
        return '破损不堪'
    else:
        return '战痕累累'

def get_wear_range(grade: str, skin_min: float, skin_max: float) -> tuple:
    if grade not in WEAR_THRESHOLDS:
        return skin_min, skin_max
    upper = WEAR_THRESHOLDS[grade]
    prev_upper = 0.0
    for g, val in WEAR_THRESHOLDS.items():
        if g == grade:
            break
        prev_upper = val
    low = max(skin_min, prev_upper)
    high = min(skin_max, upper)
    if low > high:
        return skin_min, skin_max
    return low, high

# ---------- 加载数据（读取已有的 price_buff） ----------
def load_skins(csv_path: str) -> List[Skin]:
    skins = []
    with open(csv_path, 'r', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        for row in reader:
            quality = row['品质'].strip()
            is_st = 'StatTrak™' in row['皮肤名称']
            min_max = row['磨损区间'].replace('~', '').split()
            min_f = float(min_max[0])
            max_f = float(min_max[1])
            goods_id_str = row.get('goods_id', '0')
            goods_id = int(goods_id_str) if goods_id_str and goods_id_str != '' else 0
            actual_wear = None
            if '实际磨损' in row and row['实际磨损']:
                actual_wear = float(row['实际磨损'])
            wear_grade = row.get('磨损', '')
            price_str = row.get('price_buff', '')
            price = float(price_str) if price_str and price_str != '' else 0.0
            skin = Skin(
                collection=row['收藏品名称'].strip(),
                name=row['皮肤名称'].strip(),
                quality=quality,
                min_f=min_f,
                max_f=max_f,
                is_stattrak=is_st,
                goods_id=goods_id,
                actual_wear=actual_wear,
                wear_grade=wear_grade,
                price=price
            )
            skins.append(skin)
    return skins

# ---------- 保存价格到 CSV ----------
def save_prices_to_csv(skins: List[Skin], csv_path: str, output_path: str = None):
    """将当前已获取的价格写入 CSV（增量保存）"""
    rows = []
    with open(csv_path, 'r', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        for row in reader:
            rows.append(row)

    price_map = {}
    for skin in skins:
        if skin.price > 0:
            key = (skin.name, skin.wear_grade)
            price_map[key] = skin.price

    if 'price_buff' not in fieldnames:
        fieldnames = list(fieldnames) + ['price_buff']

    for row in rows:
        key = (row['皮肤名称'], row.get('磨损', ''))
        row['price_buff'] = price_map.get(key, '')

    output_path = output_path if output_path else csv_path
    with open(output_path, 'w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"💾 价格已保存到 {output_path}")

# ---------- 信号处理函数 ----------
def signal_handler(sig, frame):
    print("\n⚠️ 收到中断信号，正在保存已获取的数据...")
    if _skins_global:
        save_prices_to_csv(_skins_global, _csv_path_global, output_path=_csv_path_global)
    sys.exit(0)

# ---------- 价格填充（断点续传 + 定期保存 + 起始行号） ----------
def fill_prices(skins: List[Skin], csv_path: str, cache: Dict[int, float] = None,
                verbose: bool = True, save_interval: int = 10, start_index: int = 0):
    """
    查询价格：
    - 从 start_index 开始处理（已存在的价格仍会跳过）
    - 如果 skin.price > 0，则跳过（已获取）
    - 每成功查询 save_interval 个皮肤自动保存一次
    - 支持 Ctrl+C 中断保存
    """
    global _skins_global, _csv_path_global, _price_cache_global
    _skins_global = skins
    _csv_path_global = csv_path
    _price_cache_global = cache if cache is not None else {}

    # 注册信号处理
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    if cache is None:
        cache = {}

    success_count = 0
    total_skins = len(skins)
    
    # 显示起始信息
    if start_index > 0:
        print(f"📍 从第 {start_index} 行开始处理")
        if start_index < total_skins:
            first_skin = skins[start_index]
            print(f"   首个皮肤: {first_skin.name} ({first_skin.wear_grade})")
    
    for idx in range(start_index, total_skins):
        skin = skins[idx]
        
        # 断点续传：如果已有价格，跳过
        if skin.price > 0:
            if verbose:
                print(f"⏭️ 跳过 {skin.name} ({skin.wear_grade})，已有价格: ¥{skin.price:.2f}")
            continue

        if skin.goods_id == 0:
            if verbose:
                print(f"⏭️ 跳过 {skin.name} ({skin.wear_grade})，无 goods_id")
            continue

        query_min, query_max = skin.min_f, skin.max_f
        if skin.wear_grade:
            query_min, query_max = get_wear_range(skin.wear_grade, skin.min_f, skin.max_f)

        try:
            result = get_lowest_sell_price(skin.goods_id, query_min, query_max)
        except Exception as e:
            print(f"❌ 请求异常: {e}")
            result = None

        if result:
            price = result['price']
            skin.price = price
            cache[skin.goods_id] = price
            success_count += 1
            if verbose:
                print(f"✓ {skin.name} ({skin.wear_grade}): ¥{price:.2f}")
        else:
            if verbose:
                print(f"✗ 未找到 {skin.name} ({skin.wear_grade}) 的价格")

        # 每成功查询 save_interval 个，自动保存一次
        if success_count % save_interval == 0 and success_count > 0:
            print(f"💾 已成功查询 {success_count} 个，自动保存...")
            save_prices_to_csv(skins, csv_path, output_path=csv_path)

        # 随机间隔 15~18 秒
        if idx < total_skins - 1:
            wait_time = random.uniform(15, 18)
            if verbose:
                print(f"⏳ 等待 {wait_time:.1f} 秒后继续...")
            time.sleep(wait_time)

    # 循环结束后最终保存
    save_prices_to_csv(skins, csv_path, output_path=csv_path)
    print("✅ 所有价格查询完成并已保存。")

# ---------- 构建索引 ----------
def build_index(skins: List[Skin]) -> Dict:
    index = defaultdict(lambda: defaultdict(list))
    for skin in skins:
        key_quality = skin.quality + (" (StatTrak)" if skin.is_stattrak else " (Normal)")
        index[skin.collection][key_quality].append(skin)
    return index

def calculate_output_float(materials: List[Skin], target_skin: Skin) -> float:
    avg_input = sum((s.actual_wear if s.actual_wear is not None else s.min_f) for s in materials) / 10.0
    return avg_input * (target_skin.max_f - target_skin.min_f) + target_skin.min_f

# ---------- 汰换搜索 ----------
def find_tradeups(index: Dict, target_collection: str, target_quality: str,
                  include_stattrak: bool = False, max_budget: float = 1000.0,
                  top_n: int = 5, candidate_limit: int = 20,
                  sort_by: str = 'price', verbose: bool = True):
    quality_key = target_quality + (" (StatTrak)" if include_stattrak else " (Normal)")
    targets = index.get(target_collection, {}).get(quality_key, [])
    if not targets:
        print(f"未找到目标收藏品 '{target_collection}' 中品质 '{target_quality}' 的皮肤")
        return

    lower_qual = get_next_quality(target_quality)
    if not lower_qual:
        print("目标品质已达最高级，无法合成")
        return
    material_quality_key = lower_qual + (" (StatTrak)" if include_stattrak else " (Normal)")
    candidates = index.get(target_collection, {}).get(material_quality_key, [])
    if len(candidates) < 10:
        print(f"材料不足（需要10件，当前只有{len(candidates)}件）")
        return

    if sort_by == 'price':
        candidates.sort(key=lambda s: s.price)
    elif sort_by == 'float':
        candidates.sort(key=lambda s: s.actual_wear if s.actual_wear is not None else s.min_f)
    else:
        candidates.sort(key=lambda s: s.price)

    if len(candidates) > candidate_limit:
        candidates = candidates[:candidate_limit]
        print(f"候选材料过多，已按 {sort_by} 最低筛选前 {candidate_limit} 件")

    results = []
    for target in targets:
        target_price = target.price
        if target_price == 0:
            continue
        for combo in itertools.combinations(candidates, 10):
            total_cost = sum(s.price for s in combo)
            if total_cost > max_budget:
                continue
            out_float = calculate_output_float(list(combo), target)
            wear_grade = get_wear_grade(out_float)
            profit = target_price - total_cost
            roi = (profit / total_cost * 100) if total_cost > 0 else 0
            materials_detail = []
            for s in combo:
                wear_used = s.actual_wear if s.actual_wear is not None else s.min_f
                materials_detail.append({
                    'name': s.name,
                    'price': s.price,
                    'wear': wear_used,
                    'wear_grade': get_wear_grade(wear_used)
                })
            results.append({
                'materials_detail': materials_detail,
                'material_cost': total_cost,
                'target': target.name,
                'target_price': target_price,
                'output_float': out_float,
                'wear_grade': wear_grade,
                'profit': profit,
                'roi': roi
            })

    results.sort(key=lambda x: x['profit'], reverse=True)
    top_results = results[:top_n]

    print(f"\n===== 最优汰换方案（目标收藏品：{target_collection}，目标品质：{target_quality}）=====")
    for i, r in enumerate(top_results):
        print(f"\n--- 方案 {i+1} ---")
        print(f"目标皮肤: {r['target']} (售价: ¥{r['target_price']:.2f})")
        print(f"材料清单 (共10件):")
        for idx, mat in enumerate(r['materials_detail'], 1):
            print(f"  {idx}. {mat['name']}  | 价格: ¥{mat['price']:.2f}  | 磨损: {mat['wear']:.4f} ({mat['wear_grade']})")
        print(f"材料总成本: ¥{r['material_cost']:.2f}")
        print(f"产出磨损: {r['output_float']:.4f} ({r['wear_grade']})")
        print(f"净利润: ¥{r['profit']:.2f}  (ROI: {r['roi']:.1f}%)")

    if top_results:
        save_results_to_csv(top_results, target_collection, target_quality)

def save_results_to_csv(results, collection, quality):
    filename = f"汰换方案_{collection}_{quality}.csv"
    with open(filename, 'w', encoding='utf-8-sig', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['方案编号', '目标皮肤', '目标售价', '产出磨损', '磨损等级', '材料总成本', '利润', 'ROI', '材料详情'])
        for i, r in enumerate(results, 1):
            materials_str = '; '.join([f"{m['name']}(¥{m['price']:.2f}, {m['wear']:.4f})" for m in r['materials_detail']])
            writer.writerow([
                i,
                r['target'],
                f"{r['target_price']:.2f}",
                f"{r['output_float']:.4f}",
                r['wear_grade'],
                f"{r['material_cost']:.2f}",
                f"{r['profit']:.2f}",
                f"{r['roi']:.1f}%",
                materials_str
            ])
    print(f"\n方案已保存至: {filename}")

# ---------- 主程序 ----------
if __name__ == "__main__":
    csv_file = "物品箱子磨损对照表1_有效磨损及goods_id.csv"

    # 加载皮肤（自动读取已有的 price_buff）
    skins = load_skins(csv_file)
    print(f"加载了 {len(skins)} 个皮肤")
    
    # 统计已查询的数量
    already_has_price = sum(1 for s in skins if s.price > 0)
    print(f"其中 {already_has_price} 个皮肤已有价格（将跳过）")

    # -------- 交互式起始行号选择 --------
    while True:
        start_input = input("\n请输入起始行号（从0开始，直接回车从0开始）: ").strip()
        if start_input == "":
            start_index = 0
            break
        if start_input.isdigit():
            start_index = int(start_input)
            if 0 <= start_index < len(skins):
                # 显示该行的皮肤信息供确认
                skin = skins[start_index]
                print(f"将从第 {start_index} 行开始，对应皮肤: {skin.name} ({skin.wear_grade})")
                confirm = input("确认？(y/n，默认 y): ").strip().lower()
                if confirm in ('', 'y', 'yes'):
                    break
                else:
                    print("重新输入起始行号。")
            else:
                print(f"行号超出范围（0 ~ {len(skins)-1}），请重新输入。")
        else:
            print("请输入有效的数字或直接回车。")

    # 可选：只查询特定收藏品（取消注释即可）
    # skins = [s for s in skins if s.collection == "CS20 Case"]

    # 填充价格（自动跳过已有价格的皮肤，定期保存，支持 Ctrl+C）
    fill_prices(skins, csv_file, verbose=True, save_interval=10, start_index=start_index)

    # 构建索引
    index = build_index(skins)

    # 示例搜索
    find_tradeups(
        index=index,
        target_collection="CS20 Case",
        target_quality="隐秘级",
        include_stattrak=False,
        max_budget=500.0,
        top_n=5,
        candidate_limit=20,
        sort_by='price',
        verbose=True
    )