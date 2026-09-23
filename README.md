# cs2-tradeup-toolkit — CS2 饰品数据与汰换工具集

围绕 **CS2 饰品价格 / 磨损 / 汰换（Trade Up）** 的工具集合：多平台比价、汰换配方的期望值优化、库存抓取，以及若干 OCR / 目标检测实验脚本。

> ⚠️ 本项目涉及第三方平台的**个人登录凭证**。所有凭证一律通过根目录 `.env` 提供，**不入库**。首次使用请先读 [配置说明](#配置说明)。

---

## 与同类项目的差异

GitHub 上 CS2 汰换工具已有数十个，但绝大多数是**只读计算器**：算 EV、列市场、导表格。本项目的定位不同：

| 维度 | 常见同类项目 | 本项目 |
|---|---|---|
| **数据源** | Steam / CSFloat / Skinport 等海外市场 | **Buff163 / C5GAME / ECO** 等国内平台，含 C5 开放平台 IP 白名单对接 |
| **执行能力** | 只算不买，输出表格或提醒 | **带登录态真实下单**（Buff / C5 / ECO 三平台购买队列 + 限流控制） |
| **求解算法** | 枚举 / 贪心，或分支定界精确解 | **动态规划（DP）求解最优配方**，含 GPU 加速路径 |
| **游戏内自动化** | 无 | **截图 → YOLO 检测汰换入口 → OCR 识别 → 自动点击**，全链路自动化 |
| **形态** | CLI 或网页 | **PySide6 桌面应用**，多面板 GUI（查询 / 监控 / 队列 / 库存 / 历史） |

一句话：别人做的是「告诉你哪个合约划算」，本项目做的是「算出划算合约，然后去把它买下来」。

### 算法要点

- **磨损归一化**：按 CS2 规则由输入皮肤 float 推导产出 float，支持多收藏品混合配比
- **DP 最优配方**：在 10 件材料的约束下搜索期望收益最大的组合，而非暴力枚举
- **EV 评估**：产出概率 × 各磨损档位市场价 − 材料成本，含手续费

---

## 目录结构

```
cs2-tradeup-toolkit/
├── send_request.py            # SteamDT 开放平台客户端（价格 / 磨损 / 基础信息）
├── batch_price_query.py       # 批量价格查询示例
├── callback_server.py         # 异步回调接收服务（磨损查询回调 / 域名验证）
├── fix_c5_item_ids.py         # 通过 C5 OpenAPI 批量校正物品 ID 表
├── connect_edge.py            # 调试辅助：连接已打开的 Edge 并导出登录态
├── start_services.bat         # 一键启动回调服务 + 请求端
│
├── tradeup_sim/               # 汰换模拟核心（算法 + GUI）
│   ├── engine.py              #   DP 求解最优配方（含 GPU 加速）+ EV 评估
│   ├── database.py            #   SQLite 数据层 + CSV 导入
│   ├── models.py              #   数据模型（Skin / Material / TradeUpResult）
│   ├── gui.py, main.py        #   PySide6 界面
│   └── data/tradeup.db        #   皮肤库（运行后生成，不入库）
│
├── buy/                       # 比价与购买
│   ├── buff/, c5/             #   两平台查询 / 下单脚本
│   ├── data/                  #   主物品表 + CSV 输出
│   └── 汰换/                  #   汰换比价桌面应用（PySide6）
│       ├── core/              #     业务逻辑：认证 / 查询 / 购买 / 库存
│       ├── gui/               #     界面
│       ├── utils/config.py    #     全局配置（路径 + 非敏感参数）
│       ├── vendor/            #     平台脚本副本 + ECO 凭证入口
│       └── data/              #     运行时数据（不入库）
│
├── 数据集/                    # 主物品映射表（大文件，部分不入库）
├── steam_paqu/, pachon/       # 早期 Selenium 脚本（已被 buy/ 取代）
├── yolo/, dataset/, models/, runs/   # 目标检测 / OCR 实验
│
├── .env                       # 真实凭证（不入库）
└── .env.example               # 凭证模板（入库）
```

---

## 配置说明

### 1. 安装依赖

```powershell
# 顶层工具
pip install -r requirements.txt

# 汰换桌面应用
pip install -r buy/汰换/requirements.txt
```

**解释器**：项目在 Python 3.11 ~ 3.14 下运行。若同时装了多个 Python，请统一用一个，
不要一部分命令走 `python`、另一部分走 `py`（两者可能指向不同环境）。

### 2. 创建 `.env`

```powershell
copy .env.example .env
```

然后填入你自己的凭证。各变量的获取方式：

| 变量 | 用途 | 获取方式 |
|---|---|---|
| `STEAMDT_API_KEY` | SteamDT 价格 / 磨损接口 | [open.steamdt.com](https://open.steamdt.com) 申请 |
| `CALLBACK_URL` | 磨损查询的异步回调地址 | 需公网可达（可用内网穿透） |
| `PORT` | 回调服务监听端口 | 默认 `8080` |
| `C5_APP_KEY` | C5 开放平台 | [openapi.c5game.com](https://openapi.c5game.com) 申请 |
| `C5_STEAM_ID` | C5 收货 SteamID64 | Steam 个人资料页 |
| `BUFF_SESSION` / `BUFF_CSRF_TOKEN` | Buff 查询与下单 | 登录 buff.163.com → F12 → Network → 任意 XHR 请求的 Cookie |
| `BUFF_COOKIE_STRING` | 旧脚本用的完整 Cookie 串 | 同上，`session=...; csrf_token=...` 整段 |
| `STEAM_ID` | 你的 SteamID64（17 位） | Steam 个人资料页 |
| `STEAM_TRADE_URL` | Steam 交易链接 | [交易报价隐私设置](https://steamcommunity.com/my/tradeoffers/privacy) |
| `ECO_PARTNER_ID` / `ECO_RSA_PRIVATE_KEY` | ECO 开放平台 | ECOSteam App → 我的 → 设置 → 账号与安全 → 开放能力申请 |
| `ECO_WEB_COOKIE` | ECO 网页版 | 登录 www.ecosteam.cn → F12 → Cookie |

> **优先级**：`buy/汰换` 的 GUI 里填写的凭证会存到 `data/user_settings.json`，
> 运行时**覆盖** `.env` 中的同名值。GUI 适用于临时登录态，`.env` 适用于长期配置。

### 3. 凭证安全

- `.env`、`data/user_settings.json`、`data/.buff_auth/`、`data/.c5_auth/`、`data/.eco_auth/`
  均已在 `.gitignore` 中排除。
- **源码中不再存在任何硬编码密钥**；缺失必需变量时程序会明确报错，而不是静默使用兜底值。
- 若凭证曾以明文形式进入过任何仓库或分享渠道，请到对应平台**重新生成**（见下方"密钥轮换"）。

### 4. 运行

```powershell
# SteamDT 基础信息（每天仅可调用一次，默认读本地缓存）
python send_request.py base

# 单件价格查询
python send_request.py price "AWP | Wildfire (Field-Tested)"

# 回调服务（接收异步结果）
python callback_server.py

# 一键启动回调服务 + 请求端
start_services.bat
```

汰换桌面应用：

```powershell
cd buy/汰换
python main.py
```

汰换模拟（独立 GUI）：

```powershell
python -m tradeup_sim.main
```

---

## 密钥轮换

> 如果旧凭证曾经被提交、截图或分享过，**改代码不能止损**，必须到平台重新生成：

| 平台 | 操作 |
|---|---|
| SteamDT | 后台重置 API Key |
| C5 开放平台 | 重新申请 app-key（注意 IP 白名单） |
| Buff | 退出登录 → 重新登录，旧的 session 立即失效 |
| ECO 开放平台 | 重新生成 RSA 密钥对，用新公钥换新的 PartnerId |
| ECO 网页版 | 退出登录使 loginToken 失效 |
| Steam | [交易链接](https://steamcommunity.com/my/tradeoffers/privacy) 页面点"撤销"重新生成 |

更新 `.env` 后重启程序即可，无需改任何源码。

---

## 已知限制

- **SteamDT `base` 接口每天只能调用一次**。程序默认读 `steam_items_cache.json` 缓存（TTL 23 小时），
  加 `--refresh` 才会真正发起请求。
- **Buff / C5 有严格限流**。请求间隔可在 GUI 中调节，默认值偏保守；调小容易被 429。
- **C5 开放平台绑定 IP 白名单**，换网络后需重新报备。
- `tradeup_sim/price_updater.py` 依赖 `buy/汰换/utils/config.py` 的主物品表路径，
  因此这两个目录**不能单独拆开**。
- 部分目标皮肤因数据库缺少对应收藏品的下级材料而无法求解，会抛出"无法找到有效配方"。
  这是数据缺口，不是算法问题。

---

## 法律与合规风险

> ⚠️ **请在下载或使用前完整阅读本节。** 本节说明本项目的法律边界与已知风险。
> 作者不是律师，以下内容**不构成法律意见**。

### 1. 第三方平台服务条款

本项目通过非公开接口 / 浏览器自动化方式访问 **Buff163、C5GAME、ECO、Steam** 等平台。
这些平台的服务条款通常**禁止或限制**下列行为：

- 使用自动化脚本代替人工操作（爬虫、自动下单、批量查询）
- 绕过或规避平台的风控、限流、WAF
- 将平台数据用于第三方展示或再分发

**使用本项目可能构成对上述平台服务条款的违约**，由此导致的后果由使用者自行承担。

### 2. 账号与资金风险

- 平台可能对自动化行为采取**限流、临时封禁、永久封禁**等措施
- 自动下单功能涉及**真实资金支出**，配置错误可能导致误购、超额购买
- 涉及 Steam 交易链接的操作可能导致**交易冷却、饰品冻结**
- **建议先用小额、低频方式验证**，确认行为可控后再扩大使用

### 3. 凭证与密钥合规

- 本项目**不提供任何 API Key**，所有凭证必须由使用者**自行通过平台官方渠道申请**
- 请勿共享、转售、公开他人的 API Key 或登录态
- 请勿将本人凭证提交到任何公开仓库
- C5 开放平台绑定 **IP 白名单**，请勿将白名单授权给非本人控制的地址

### 4. 数据合规

- 通过本项目抓取的市场数据（价格、库存、挂单）版权归各平台所有
- 上述数据**不得用于商业性再分发或对外提供数据服务**
- 请遵守各平台的 `robots.txt` 与数据使用政策

### 5. 无担保声明

本软件按**"现状"（AS IS）**提供，不附带任何明示或默示的担保，包括但不限于
适销性、特定用途适用性、不侵权的担保。作者不保证软件无错误、不中断，
或不满足使用者的特定需求。

### 6. 责任限制

在适用法律允许的最大范围内，**作者不对任何直接、间接、附带、特殊、惩罚性或
后果性损害承担责任**，包括但不限于：账号封禁、资金损失、数据丢失、利润损失、
商誉损害——无论是否已被告知此类损害的可能性。

---

## 许可协议

本项目采用 **非商业使用许可（Non-Commercial License）**，全文见 [LICENSE](LICENSE)。

| 允许 | 禁止 |
|---|---|
| ✅ 个人学习、技术研究 | ❌ 任何商业用途 |
| ✅ 阅读、克隆、修改源码 | ❌ 出售、出租、按次收费、订阅收费 |
| ✅ 在非商业项目中引用 | ❌ 集成进商业产品或提供付费服务 |
| ✅ 分享给他人（须保留本许可） | ❌ 去除或修改版权声明与许可 |
| | ❌ 商业组织内部用于生产性经营 |
| | ❌ 营利性的饰品交易、套利、代下单业务 |

**商业用途**包括但不限于：以本软件为基础提供付费服务、集成进商业产品、
用于营利性交易或代购业务、在商业组织内部用于生产性目的。

如需商业授权，请联系作者。

---

## 免责声明

本项目仅用于**个人学习与技术研究**。使用者需自行遵守各平台的用户协议、`robots` 规则
及所在地法律法规。因使用本工具产生的任何账号风险（限流、封禁）、资金损失、法律责任，
**由使用者自行承担**，作者不承担任何责任。
