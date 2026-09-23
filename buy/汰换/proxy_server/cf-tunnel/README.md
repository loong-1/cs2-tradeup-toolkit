# Cloudflare Tunnel 部署指南（C5 回调 → 本机 80 端口）

> 本目录内容由 `proxy_server/setup_cf_tunnel.py` 自动生成：
> - 域名：`866868686.xyz`
> - 回源地址：`http://127.0.0.1:80`
> - 期望隧道名：`c5-callback`
> - 生成时间：`2026-08-25 09:22:42`

---

## 0. 公网 IP 诊断结论（脚本自动检测）

| 项 | 值 |
|---|---|
| 探测源数 | `6` |
| 公网 IP（多数一致的） | `222.214.160.89` |
| 本机网卡 IPv4 | `192.168.1.6` |
| 是否拥有独立公网 IP | **`YES 有`** |

### 建议：

- 👉 **`YES 有` 独立公网 IP**
    你的本地 WAN 口有独立公网 IP，也可以不用 Tunnel，直接在 DNS 填 A 记录 + 路由器端口映射 80 → 本机即可。Tunnel 仍推荐（省掉维护端口映射 + 抗扫描）。
- 👉 **无论是否有公网 IP，Cloudflare Tunnel（本目录）都能用**——它走 Cloudflare 全球边缘网络作为中转，无需本地公网 IP，也不需要配置路由器端口映射。

---

## 1. 安装 cloudflared

任选其一：

```powershell
# a. winget（微软官方包管理器，推荐）
winget install --id Cloudflare.cloudflared

# b. 手动下载
#    访问 https://github.com/cloudflare/cloudflared/releases
#    下载 cloudflared-windows-amd64.msi 或 cloudflared-windows-amd64.exe
#    安装或放进 PATH 目录即可
```

安装后验证：
```powershell
cloudflared --version
```

## 2. 一键部署（PowerShell）

```powershell
cd F:\steamdt-project\buy\汰换\proxy_server\cf-tunnel
powershell -ExecutionPolicy Bypass -File .\deploy.ps1
```

脚本会自动走 5 步：
1. 验证 cloudflared + 检查 cert.pem
2. 未登录则弹出浏览器授权 Cloudflare（选账户 + 866868686.xyz 所在 Zone）
3. 创建/复用隧道（名：`c5-callback`）
4. 绑定 DNS CNAME（`866868686.xyz` → 隧道 CNAME 记录 `{隧道ID}.cfargotunnel.com`，deploy.ps1 会自动填真实值）
5. 替换占位符生成最终的 config.yml

## 3. 启动隧道

```powershell
cd F:\steamdt-project\buy\汰换\proxy_server\cf-tunnel
powershell -ExecutionPolicy Bypass -File .\run_cf_tunnel.ps1
```

保持窗口常开。需要开机自启可参考 cloudflared 官方 service install 命令：
```powershell
cloudflared service install
```

## 4. 验证公网可达

**手机关 Wi-Fi 用蜂窝网络**打开浏览器访问：
```
http://866868686.xyz/health
```
预期返回：`{"status":"ok"}`

再模拟 C5 回调：
```bash
curl -X POST http://866868686.xyz/c5/callback \
  -H "Content-Type: application/json" \
  -d '{"tradeNo":"TEST999","status":1}'
```
→ 返回文本 `success`，同时本机 `data/c5_callbacks.db` 的 `callbacks` 表新增一行。

## 5. 同步 hosts 注意事项

由于 Cloudflare Tunnel 接管了 866868686.xyz 的公网解析（CNAME → *.cfargotunnel.com），
如果你在本机**仍然需要 `866868686.xyz → 127.0.0.1`**（C5 参数校验时本地访问该地址也走
HTTP 200 健康返回），请保留之前通过 `server_cli.py hosts add` 写入的 hosts 条目即可，
两者不冲突（hosts 优先级 > 公网 DNS）。

### 取消 hosts 场景
若希望本机访问 866868686.xyz 也走 Cloudflare Tunnel（真实绕一圈公网回来），可以：
```powershell
python proxy_server\server_cli.py hosts remove --hostname 866868686.xyz
```

## 6. 常见问题

**Q: deploy.ps1 报 `missing origin cert`？**
A: 首次必须跑一次 `cloudflared tunnel login` 浏览器授权。deploy.ps1 已自动处理，按提示回车即可。
   如果是无头/远程服务器无法开浏览器：改用 **Zero Trust 令牌**方式：
   Cloudflare Zero Trust 控制台 → Access → Service Auth → Service Tokens → 创建，把
   `CF_API_TOKEN` 设为环境变量后再跑 deploy。

**Q: `tunnel route dns` 报 `no matching zone`？**
A: 说明 Cloudflare 账户中 866868686.xyz 未添加到 Zone。到 Cloudflare Dashboard → Add site，
   按提示把域名 NS 服务器改成 Cloudflare 提供的（例如 `xxx.ns.cloudflare.com`）。
   NS 生效（几小时到最多 48h）后再跑 deploy。

**Q: 启动 tunnel 后公网访问 502/503？**
A: 说明本地回源 `http://127.0.0.1:80` 没起来。先跑 `server_cli.py status`
   确认 callback 服务在 80 端口监听并 `/health` 正常。确认后重启 cf tunnel。

**Q: 想绑 `*.866868686.xyz`（所有子域名）？**
A: 修改 deploy.ps1 顶部 `$DOMAIN` 变量为 `*.866868686.xyz` 重跑即可；config.yml 的
   ingress 已按域名配置，支持多 hostname 分段。

## 7. 文件清单

| 文件 | 作用 | 状态 |
|---|---|---|
| `README.md` | 本文档 | ✓ 已生成 |
| `config.yml.tmpl` | Tunnel 配置模板（带占位符，**别改**） | ✓ 已生成 |
| `config.yml` | 最终配置（占位符替换后） | ⏳ deploy.ps1 生成 |
| `deploy.ps1` | 一键部署脚本（login/create/route dns/generate config） | ✓ 已生成 |
| `run_cf_tunnel.ps1` | 部署完成后启动 tunnel | ✓ 已生成 |

