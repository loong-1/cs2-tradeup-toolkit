import re

# 读取材料文件
with open(r'c:\Users\28704\.trae-cn\attachments\6aa4e0466f3b120e92aeb18d\a8977c78-ea96-4225-ba8e-454b82df786f_4734bfb0-ac0a-4689-828d-3e204c9c156c__div class....txt', 'r', encoding='utf-8') as f:
    mat_html = f.read()

# 查找所有 float_input 附近的 value
# 模式: float_input" ... value="..."
float_vals = re.findall(r'float_input"[^>]*?value="([^"]*)"', mat_html)
print("材料 float_input values:", float_vals)

# 也查找 price_input
price_vals = re.findall(r'price_input"[^>]*?value="([^"]*)"', mat_html)
print("材料 price_input values:", price_vals)

# 查找所有像浮动值的数字（0.xxxx 格式）在 input value 中
all_values = re.findall(r'value="([\d.]+)"', mat_html)
print("所有 input values:", all_values)
