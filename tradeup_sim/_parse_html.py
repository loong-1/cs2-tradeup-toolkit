import re

def parse_tradeup_html(filepath):
    """解析汰换页面 HTML，提取材料和产物信息。"""
    with open(filepath, 'r', encoding='utf-8') as f:
        html = f.read()

    # 提取每个材料卡片的关键信息
    # 名称: x-text="slot.item.cn || slot.item.name">NAME<
    # 收藏品: collectionDisplayName(slot.item.collection)">COLL<
    # 磨损: wearLabel(slot.item.wear_code)">WEAR<
    # 浮动范围: min_float)} ~ ${max_float}`">LO ~ HI<
    items = []
    # 找所有有实际名称的卡片（x-text 后面有实际文本内容）
    # 模式: name">实际名< ... collection">收藏< ... wear">磨损< ... min_float} ~ ${max_float}`">范围<
    # 用非贪婪匹配
    pattern = re.compile(
        r'x-text="slot\.item\.cn \|\| slot\.item\.name">([^<]+)</div>.*?'
        r'x-text="collectionDisplayName\(slot\.item\.collection\)">([^<]+)</span>.*?'
        r'x-text="wearLabel\(slot\.item\.wear_code\)">([^<]+)</span>.*?'
        r'x-text="`\$\{formatFloat\(slot\.item\.min_float\)\} ~ \$\{formatFloat\(slot\.item\.max_float\)\}`">([^<]+)</div>',
        re.DOTALL
    )
    for m in pattern.finditer(html):
        name = m.group(1).strip()
        coll = m.group(2).strip()
        wear = m.group(3).strip()
        frange = m.group(4).strip()
        items.append({"name": name, "collection": coll, "wear": wear, "float_range": frange})
    return items

# 第一个文件：材料
mats = parse_tradeup_html(r'c:\Users\28704\.trae-cn\attachments\6aa4e0466f3b120e92aeb18d\a8977c78-ea96-4225-ba8e-454b82df786f_4734bfb0-ac0a-4689-828d-3e204c9c156c__div class....txt')
print(f"材料数: {len(mats)}")
for i, m in enumerate(mats):
    print(f"  {i+1}. {m['name']} | {m['collection']} | {m['wear']} | float: {m['float_range']}")

print()
# 第二个文件：产物
outs = parse_tradeup_html(r'c:\Users\28704\.trae-cn\attachments\6aa4e0466f3b120e92aeb18d\60a23615-be46-41f4-b218-c31b6dabe920_51648c4d-91e3-4b49-a290-0d4cec5c99e7__div class....txt')
print(f"产物数: {len(outs)}")
for i, o in enumerate(outs):
    print(f"  {i+1}. {o['name']} | {o['collection']} | {o['wear']} | float: {o['float_range']}")
