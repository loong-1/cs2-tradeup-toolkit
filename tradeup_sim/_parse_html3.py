import re

# 读取产物文件
with open(r'c:\Users\28704\.trae-cn\attachments\6aa4e0466f3b120e92aeb18d\60a23615-be46-41f4-b218-c31b6dabe920_51648c4d-91e3-4b49-a290-0d4cec5c99e7__div class....txt', 'r', encoding='utf-8') as f:
    out_html = f.read()

# 解析产物卡片
# 名称: x-text="item.cn || item.name">NAME<
# 概率: formatPercent(item.probability)">PROB<
# 收藏品: collectionDisplayName(item.collection)">COLL<
# 磨损: wearLabel(item.wear_code)">WEAR<
# 预计磨损: formatFloat(item.expected_wear)">WEAR_VAL<
# 价格: formatMoney(item.price)">PRICE<
pattern = re.compile(
    r'x-text="item\.cn \|\| item\.name">([^<]+)</div>.*?'
    r'formatPercent\(item\.probability\)">([^<]+)</span>.*?'
    r'collectionDisplayName\(item\.collection\)">([^<]+)</span>.*?'
    r'wearLabel\(item\.wear_code\)">([^<]+)</span>.*?'
    r'formatFloat\(item\.expected_wear\)">([^<]+)</div>.*?'
    r'formatMoney\(item\.price\)">([^<]+)</div>',
    re.DOTALL
)
outputs = []
for m in pattern.finditer(out_html):
    outputs.append({
        "name": m.group(1).strip(),
        "probability": m.group(2).strip(),
        "collection": m.group(3).strip(),
        "wear": m.group(4).strip(),
        "expected_wear": float(m.group(5).strip()),
        "price": m.group(6).strip(),
    })

print(f"产物数: {len(outputs)}")
for i, o in enumerate(outputs):
    print(f"  {i+1}. {o['name']} | {o['collection']} | {o['wear']} | 预计磨损={o['expected_wear']:.6f} | 概率={o['probability']} | {o['price']}")
