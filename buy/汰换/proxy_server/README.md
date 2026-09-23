# C5 白名单域名本地解析服务 · 使用说明

本目录实现了「域名 `866868686.xyz` → 本机解析 + 回调接收」的完整解决方案，
用于满足 C5 Game 商户 API 的 **tradeUrl 白名单域名校验** 需求。

```
proxy_server/
├── hosts_manager.py      Windows hosts 文件增删查（推荐首选方案）
├── c5_callback_server.py HTTP 回调服务（监听 80 端口，接收 C5 POST /c5/callback）
├── dns_resolver.py       可选：本地 DNS 服务器（需 dnslib，替代 hosts）
└── server_cli.py         统一 CLI：setup / status / hosts / callback / dns / teardown
```

## 快速启动（推荐，三步完成）

以 **管理员身份** 打开 PowerShell：

```powershell
cd F:\steamdt-project\buy\汰换

# 1) 一键部署：写入 hosts (866868686.xyz→127.0.0.1) + 启动回调服务（监听80端口）
python proxy_server\server_cli.py setup
```

运行前已预装：GUI 代码中的 `buy_c5_item` 已自动把 `trade_url`
改为 `http://866868686.xyz/c5/callback`，无需手动改其他地方。

新开一个 **管理员** 终端做健康检查：

```powershell
python proxy_server\server_cli.py status
# 期望：✅ hosts 已配置  ✅ DNS 解析=127.0.0.1  ✅ /health 200
```

### 回收

在 `setup` 终端按 Ctrl+C 停止服务；再执行：

```powershell
python proxy_server\server_cli.py teardown
# → 从 hosts 文件中移除 866868686.xyz 条目
```

---

## 子命令详解

### hosts 管理（写入 Windows hosts 实现本地域名解析）

```powershell
# 写入
python proxy_server\server_cli.py hosts add --hostname 866868686.xyz --ip 127.0.0.1
# 查询是否存在
python proxy_server\server_cli.py hosts check --hostname 866868686.xyz
# 列出所有解析条目
python proxy_server\server_cli.py hosts list
# 移除
python proxy_server\server_cli.py hosts remove --hostname 866868686.xyz
```

- 写入前会自动备份 hosts 文件到 `proxy_server/hosts_backups/hosts.YYYYMMDD_HHMMSS.bak`
- 写入后自动执行 `ipconfig /flushdns` 刷新 DNS 缓存
- 每次写入均需管理员权限；非管理员会明确报错并给出操作指引

### callback HTTP 服务

```powershell
# 监听 0.0.0.0:80（默认）
python proxy_server\server_cli.py callback run --port 80

# 若 80 被 IIS/Skype 占用，改用高位端口（注意 C5 默认访问 80，8080 需自行改 C5_TRADE_URL 端口）
python proxy_server\server_cli.py callback run --port 8080
```

| 路由 | 方法 | 说明 |
|---|---|---|
| `/` | GET | 欢迎页，确认服务正常 |
| `/health` | GET | JSON 健康检查 `{"status":"ok"}` |
| `/c5/callback` | POST / GET | C5 交易回调接口；返回文本 `success`（符合 C5 文档要求）；所有请求写入 SQLite + 文本日志 |

回调日志位置：
- `F:\steamdt-project\buy\汰换\data\c5_callbacks.log` — 文本日志
- `F:\steamdt-project\buy\汰换\data\c5_callbacks.db`  — SQLite（`callbacks` 表）

### DNS 服务（替代 hosts 的可选方案）

适合不想修改 hosts 文件但可以改本机 DNS 服务器的场景：

```powershell
pip install dnslib
python proxy_server\server_cli.py dns --port 53 --ip 127.0.0.1
```

然后在 网络适配器 → IPv4 首选 DNS 填 `127.0.0.1`，备用 `114.114.114.114`。

DNS 行为：
- `*.866868686.xyz` → `127.0.0.1`（TTL 300s）
- 其他所有域名 → 转发到上游（默认 `114.114.114.114:53`）
- 失败返回 SERVFAIL

### status 健康体检

```powershell
python proxy_server\server_cli.py status --port 80
```

检查 5 项：hosts 配置 / DNS 解析 / 端口占用 / HTTP 健康 / Host 虚拟主机访问 + C5 trade_url 配置。
任一异常会给出说明并返回非 0 退出码，便于脚本自动化判定。

---

## 与 GUI 购买流程的联动

1. GUI 内调用 C5 购买时，`core/buyer.py` 的 `buy_c5_item` 自动传入
   `trade_url=http://866868686.xyz/c5/callback`（在 `utils/config.py` 的
   `C5_TRADE_URL` 中集中定义）。
2. C5 平台验证 trade_url 域名是否匹配白名单 — 通过。
3. 订单状态变更时，C5 服务器异步向白名单 URL 回调；若：
   - 公网上 `866868686.xyz` 的 DNS 已指向用户本机（需内网穿透或真实公网IP），
     则回调真实送达，写入本地 SQLite。
   - 若未具备公网入口：C5 服务器端回调失败会重试若干次，但**交易本身不阻塞**
     （购买是否成功以 normal_buy 的同步响应为准）；本地服务仍满足了
     **tradeUrl 字符串必须为白名单域** 这一关键校验点。

若您后续有公网服务器（或内网穿透），只需把穿透端口映射到本机 80，
真实回调即可到达，无需改代码。

## 常见问题

**Q: 启动提示 80 端口被占用？**
A: 先 `netstat -ano | findstr :80` 找到 PID，再决定关闭对应进程，或改用
`--port 8080`（同时把 `config.py` 中 `C5_TRADE_URL` 加上 `:8080` 端口段）。

**Q: hosts 写入报「没有权限」？**
A: 必须以「管理员身份运行」打开 PowerShell / 终端。右键菜单选「以管理员身份运行」。

**Q: 回调成功但 GUI 没看到？**
A: 回调写入独立的 `c5_callbacks.db` 和 `c5_callbacks.log`，与 `purchases.db`
（GUI 历史记录）分开，避免混淆。可以直接打开 SQLite 文件查看 `callbacks` 表。

**Q: DNS 方案 vs hosts 方案选哪个？**
A: 优先 **hosts**：无额外依赖、稳定、立即生效。DNS 适合您的环境正好需要
在局域网内为多台机器提供统一解析的场景。
