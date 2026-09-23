# 目录结构变更说明

**重组日期**：2026-08-25
**重组范围**：`F:\steamdt-project\buy\` 全目录

---

## 一、变更概要

| 变更类型 | 数量 |
|---|---|
| 新建目录 | 3 |
| 迁移文件 | 5 |
| 更新代码引用 | 4 |
| 清理 __pycache__ | 6 |
| 新建文档 | 2 |

---

## 二、原路径 → 新路径对应表

### 2.1 数据文件迁移

| 原路径 | 新路径 | 说明 |
|---|---|---|
| `F:\steamdt-project\物品箱子磨损对照表1_有效磨损及goods_id.csv` | `F:\steamdt-project\buy\data\物品箱子磨损对照表1_有效磨损及goods_id.csv` | 主物品映射表，从项目根目录移入 buy/data/ |
| `F:\steamdt-project\buy\buff\buff_csv\best_order.txt` | `F:\steamdt-project\buy\data\csv\buff\best_order.txt` | Buff 最佳订单记录 |
| `F:\steamdt-project\buy\buff\buff_csv\buff_orders_1115503.csv` | `F:\steamdt-project\buy\data\csv\buff\buff_orders_1115503.csv` | Buff 查询结果 |
| `F:\steamdt-project\buy\buff\buff_csv\buff_orders_1115648.csv` | `F:\steamdt-project\buy\data\csv\buff\buff_orders_1115648.csv` | Buff 查询结果 |
| `F:\steamdt-project\buy\c5\c5_csv\USP-S _ Ticket to Hell (Battle-Scarred).csv` | `F:\steamdt-project\buy\data\csv\c5\USP-S _ Ticket to Hell (Battle-Scarred).csv` | C5 查询结果 |

### 2.2 删除的目录

| 已删除 | 原因 |
|---|---|
| `buy/buff/buff_csv/` | 内容已迁移至 `buy/data/csv/buff/`，目录清空 |
| `buy/c5/c5_csv/` | 内容已迁移至 `buy/data/csv/c5/`，目录清空 |
| 6 个 `__pycache__/` | Python 字节码缓存，重新运行时自动重建 |

### 2.3 新建的目录

| 新目录 | 用途 |
|---|---|
| `buy/data/` | 统一数据目录（主物品表 + CSV 输出） |
| `buy/data/csv/buff/` | Buff 平台 CSV 输出 |
| `buy/data/csv/c5/` | C5 平台 CSV 输出 |
| `buy/docs/` | 项目文档 |

---

## 三、代码引用更新

### 3.1 `汰换/utils/config.py`

| 行号 | 原值 | 新值 |
|---|---|---|
| 15-16 | `os.path.join(PROJECT_ROOT, "物品箱子磨损对照表1_有效磨损及goods_id.csv")` | `os.path.join(PROJECT_ROOT, "buy", "data", "物品箱子磨损对照表1_有效磨损及goods_id.csv")` |

### 3.2 `buff/found_buff.py`

| 行号 | 原值 | 新值 |
|---|---|---|
| 29 | `r"F:\steamdt-project\buy\buff\buff_csv"` | `r"F:\steamdt-project\buy\data\csv\buff"` |

### 3.3 `buff/buy_buff.py`

| 行号 | 原值 | 新值 |
|---|---|---|
| 13 | `r"F:\steamdt-project\buy\buff\buff_csv"` | `r"F:\steamdt-project\buy\data\csv\buff"` |

### 3.4 `c5/found_c5.py`

| 行号 | 原值 | 新值 |
|---|---|---|
| 66 | `r"F:\steamdt-project\buy\c5\c5_csv"` | `r"F:\steamdt-project\buy\data\csv\c5"` |

---

## 四、未变更的内容

以下目录/文件保持原位，未做任何移动：

| 路径 | 保持原因 |
|---|---|
| `buy/汰换/` 目录名 | Cloudflare Tunnel 配置写死该路径；用户所有命令均引用此路径 |
| `buy/buff/` 顶层位置 | `config.py` 通过 `sys.path` 注入，移动会破坏 import |
| `buy/c5/` 顶层位置 | 同上 |
| `buy/汰换/` 内部结构 | 已合理（core/gui/utils/proxy_server/data），无需调整 |
| `buy/汰换/proxy_server/cf-tunnel/` | Cloudflare Tunnel 部署文件，路径敏感 |

---

## 五、验证清单

- [x] 文件迁移完成（5 个文件）
- [x] 代码引用更新（4 个文件）
- [x] __pycache__ 清理（6 个目录）
- [x] `python main.py` 启动验证
- [x] 主物品表加载验证（9771 条）
- [x] GUI 物品下拉 / 物品总览标签页正常
