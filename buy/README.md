# SteamDT 汰换工具

集成 Buff / C5 双平台比价查询、一键购买、数据归档的桌面应用。

## 目录结构

```
buy/
├── buff/                         # Buff 平台脚本（查询 + 购买）
│   ├── found_buff.py
│   └── buy_buff.py
├── c5/                           # C5 平台脚本（查询 + 购买）
│   ├── found_c5.py
│   └── buy_c5.py
├── data/                         # 统一数据目录
│   ├── csv/                      # 平台 CSV 输出
│   │   ├── buff/                 # Buff 查询结果 CSV
│   │   └── c5/                   # C5 查询结果 CSV
│   └── 物品箱子磨损对照表1_有效磨损及goods_id.csv  # 主物品映射表
├── 汰换/                         # 主应用（PySide6 GUI）
│   ├── core/                     # 业务逻辑（查询/购买/数据）
│   ├── gui/                      # 界面（查询/历史/物品总览）
│   ├── utils/                    # 配置
│   ├── proxy_server/             # C5 回调服务 + Cloudflare Tunnel
│   ├── data/                     # 应用数据（SQLite + items.csv）
│   ├── main.py                   # 入口
│   └── requirements.txt
└── docs/                         # 文档
    └── RESTRUCTURE_LOG.md        # 目录重组变更说明
```

## 快速开始

```powershell
cd F:\steamdt-project\buy\汰换
pip install -r requirements.txt
python main.py
```

## 核心功能

- **比价查询**：并行查询 Buff + C5，按价格升序展示
- **一键购买**：选中商品后直接调用平台购买接口
- **数据归档**：所有查询/购买操作写入 SQLite，支持历史筛选与 CSV 导出
- **物品总览**：从主物品表加载 9700+ 皮肤，按品质/收藏品筛选
- **C5 回调服务**：本地 HTTP 服务接收 C5 异步回调，配合 Cloudflare Tunnel 实现公网可达

## 凭证管理

所有凭证统一从**项目根目录的 `.env`** 读取，源码中不再硬编码任何密钥。

```powershell
copy .env.example .env   # 首次使用
```

涉及的变量：`BUFF_SESSION`、`BUFF_CSRF_TOKEN`、`BUFF_COOKIE_STRING`、
`C5_APP_KEY`、`C5_STEAM_ID`、`STEAM_ID`、`STEAM_TRADE_URL`、
`ECO_PARTNER_ID`、`ECO_RSA_PRIVATE_KEY`、`ECO_WEB_COOKIE`。

- 变量缺失时程序会明确报错，不会静默使用兜底值。
- `data/user_settings.json`（GUI 填写）运行时**覆盖** `.env` 中的同名值。
- `.env` 与 `data/` 下的凭证文件均已在 `.gitignore` 中排除，不会被提交。
