# -*- coding: utf-8 -*-
"""解析用户提供的 HTML 文件，验证磨损公式。"""
import re

# 读取第一个文件（材料）
with open(r'c:\Users\28704\.trae-cn\attachments\6aa4e0466f3b120e92aeb18d\a8977c78-ea96-4225-ba8e-454b82df786f_4734bfb0-ac0a-4689-828d-3e204c9c156c__div class....txt', 'r', encoding='utf-8') as f:
    html1 = f.read()

# 读取第二个文件（产物）
with open(r'c:\Users\28704\.trae-cn\attachments\6aa4e0466f3b120e92aeb18d\60a23615-be46-41f4-b218-c31b6dabe920_51648c4d-91e3-4b49-a290-0d4cec5c99e7__div class....txt', 'r', encoding='utf-8') as f:
    html2 = f.read()

# 解析材料：从渲染后的 HTML 中提取
# 每个材料卡片包含: 名称, 收藏品, 磨损等级, float输入值, float范围
def parse_materials(html):
    materials = []
    # 匹配渲染后的材料卡片
    # 名称在 x-text="slot.item.cn || slot.item.name">名称</div>
    # 收藏品在 collectionDisplayName(...)">收藏品</span>
    # 磨损等级在 wearLabel(...)">等级</span>
    # float值在 float_input 的 value
    # float范围在 formatFloat(min_float) ~ formatFloat(max_float)
    
    # 用正则提取每个材料块
    pattern = r'x-text="slot\.item\.cn \|\| slot\.item\.name">([^<]+)</div>.*?collectionDisplayName\(slot\.item\.collection\)">([^<]+)</span>.*?wearLabel\(slot\.item\.wear_code\)">([^<]+)</span>.*?x-model="selected\[slot\.index\]\.float_input"[^>]*>([^<]*)</div>.*?text-base-content/50"[^>]*>([^<]+)</div>'
    matches = re.findall(pattern, html, re.DOTALL)
    for name, coll, wear, float_val, float_range in matches:
        name = name.strip()
        coll = coll.strip()
        wear = wear.strip()
        float_val = float_val.strip()
        float_range = float_range.strip()
        # 解析 float 范围
        range_match = re.findall(r'[\d.]+', float_range.replace(',', ''))
        mn, mx = float(range_match[0]), float(range_match[1]) if len(range_match) >= 2 else (0.0, 1.0)
        materials.append({
            'name': name,
            'collection': coll,
            'wear_grade': wear,
            'float': float(float_val) if float_val else None,
            'min_float': mn,
            'max_float': mx,
        })
    return materials

def parse_outputs(html):
    outputs = []
    # 匹配产物卡片
    pattern = r'x-text="item\.cn \|\| item\.name">([^<]+)</div>.*?formatPercent\(item\.probability\)">([^<]+)</span>.*?collectionDisplayName\(item\.collection\)">([^<]+)</span>.*?wearLabel\(item\.wear_code\)">([^<]+)</span>.*?formatFloat\(item\.expected_wear\)">([^<]+)</div>'
    matches = re.findall(pattern, html, re.DOTALL)
    for name, prob, coll, wear, expected_wear in matches:
        outputs.append({
            'name': name.strip(),
            'probability': prob.strip(),
            'collection': coll.strip(),
            'wear_grade': wear.strip(),
            'expected_wear': float(expected_wear.strip()),
        })
    return outputs

mats = parse_materials(html1)
outs = parse_outputs(html2)

print(f"材料数: {len(mats)}")
for i, m in enumerate(mats):
    print(f"  [{i+1}] {m['name']} | {m['collection']} | {m['wear_grade']} | float={m['float']} | range=[{m['min_float']}, {m['max_float']}]")

print(f"\n产物数: {len(outs)}")
for o in outs:
    print(f"  {o['name']} | {o['collection']} | {o['wear_grade']} | expected_wear={o['expected_wear']} | prob={o['probability']}")

# 计算 avg_n
if mats:
    n_vals = []
    for m in mats:
        if m['float'] is not None:
            span = m['max_float'] - m['min_float']
            if span > 0:
                n = (m['float'] - m['min_float']) / span
                n_vals.append(n)
                print(f"  {m['name']}: n = ({m['float']} - {m['min_float']}) / {span} = {n:.6f}")
    avg_n = sum(n_vals) / len(n_vals)
    print(f"\navg_n = {avg_n:.6f}")
    
    print(f"\n验证产物磨损:")
    for o in outs:
        # 需要知道产物的 min_float 和 max_float
        # 从材料中找不到，需要从数据库查
        expected = o['expected_wear']
        print(f"  {o['name']}: expected_wear={expected:.6f}")
