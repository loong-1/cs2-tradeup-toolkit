# SteamDT — CS2 饰品数据与汰换工具集

围绕 **CS2 饰品价格 / 磨损 / 汰换（Trade Up）** 的工具集合：多平台比价、汰换配方的期望值优化、库存抓取，以及若干 OCR / 目标检测实验脚本。

> ⚠️ 本项目涉及第三方平台的**个人登录凭证**。所有凭证一律通过根目录 `.env` 提供，**不入库**。首次使用请先读 [配置说明](#配置说明)。

---

## 目录结构

```
steamdt-project/
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

## 免责声明

本项目仅用于个人学习与效率工具。使用者需自行遵守各平台的用户协议与 robots 规则。
因使用本工具产生的账号风险（限流、封禁、资金损失）由使用者自行承担。
