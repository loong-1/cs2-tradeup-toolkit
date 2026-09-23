import requests
import csv
import sys
import json

CSV_PATH = "物品箱子磨损对照表1_有效磨损及goods_id.csv"

def load_skins(csv_path):
    """加载CSV，建立 市场哈希名称 -> 皮肤信息 的字典（假设哈希名唯一）"""
    skin_map = {}
    with open(csv_path, 'r', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        for row in reader:
            row = {k: v.strip() for k, v in row.items()}
            hash_name = row.get('市场哈希名称', '')
            if hash_name:
                skin_map[hash_name] = row
    return skin_map

def build_skin_id(skin_info, is_stat_trak):
    """根据皮肤信息和是否StatTrak构建ID字符串"""
    quality = skin_info.get('品质代码', '').strip()
    collection = skin_info.get('收藏品英文名称', '').strip()
    crate_id = skin_info.get('crate_id', '').strip()
    hash_name = skin_info.get('市场哈希名称', '').strip()
    st_flag = 'st=1' if is_stat_trak else 'st=0'

    if quality == 'rarity_ancient_weapon':
        # 隐秘级
        if not crate_id:
            raise ValueError(f"隐秘级皮肤 {hash_name} 缺少 crate_id")
        return f"special||{crate_id}||{st_flag}||{hash_name}"
    else:
        # 非隐秘级
        if not collection or not quality:
            raise ValueError(f"皮肤 {hash_name} 缺少收藏品或品质代码")
        return f"{collection}||{quality}||{st_flag}||{hash_name}"

def main():
    # 加载皮肤映射
    try:
        skin_map = load_skins(CSV_PATH)
    except FileNotFoundError:
        print(f"错误：找不到文件 {CSV_PATH}")
        sys.exit(1)

    # 输入材料数量
    while True:
        try:
            count = int(input("请输入材料数量（通常为10）: ").strip())
            if count > 0:
                break
            print("数量必须大于0")
        except ValueError:
            print("请输入整数")

    materials = []
    for i in range(count):
        print(f"\n--- 材料 {i+1} ---")
        while True:
            hash_name = input("输入市场哈希名称（如 'AK-47 | Head Shot'）: ").strip()
            if not hash_name:
                print("哈希名称不能为空")
                continue
            if hash_name not in skin_map:
                print(f"未找到哈希名称为 '{hash_name}' 的皮肤，请重新输入")
                continue
            skin_info = skin_map[hash_name]
            break

        while True:
            try:
                wear = float(input("磨损值 (0~1): ").strip())
                if 0 <= wear <= 1:
                    break
                print("磨损值必须在0~1之间")
            except ValueError:
                print("请输入数字")

        st_input = input("是否StatTrak? (y/n，默认n): ").strip().lower()
        is_st = st_input.startswith('y')

        try:
            skin_id = build_skin_id(skin_info, is_st)
        except ValueError as e:
            print(f"构建ID失败: {e}")
            sys.exit(1)

        materials.append({"id": skin_id, "float": wear})
        print(f"  已添加: {skin_id}")

    # 构造payload
    payload = {"materials": materials}
    print("\n📦 请求payload:")
    print(json.dumps(payload, indent=2, ensure_ascii=False))

    # 发送请求
    url = "https://cs2up.cn/api/simulator/calculate"
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Content-Type": "application/json",
        "Referer": "https://cs2up.cn/simulator.html"
    }
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if data.get('ok'):
            outputs = data['result']['outputs']
            print("\n=== 产出结果 ===")
            for out in outputs:
                print(f"皮肤: {out['cn']}")
                print(f"  预期磨损: {out['expected_wear']:.6f}")
                print(f"  概率: {out['probability']*100:.2f}%")
                print("-"*30)
        else:
            print("API返回错误:", data)
    except requests.exceptions.HTTPError as e:
        print(f"HTTP错误: {e}")
        print("响应内容:", resp.text)
    except Exception as e:
        print("请求异常:", e)

if __name__ == "__main__":
    main()