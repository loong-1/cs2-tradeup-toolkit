r"""公网 IP 自动探测 + Cloudflare Tunnel 部署文件生成器。

功能（单文件）：
  1. 多源并行探测公网 IPv4（ifconfig.me / ipinfo.io / ifconfig.co / myip.ipip.net / checkip.amazonaws.com）
  2. 枚举本机所有网卡 IPv4，与公网 IP 比对 → 判定是否拥有「独立公网 IP」
  3. 在 proxy_server/cf-tunnel/ 下生成：
     - config.yml.tmpl : Tunnel 配置模板（含占位符，deploy.ps1 会填真实值生成 config.yml）
     - deploy.ps1      : 一键部署脚本（cloudflared login → create → route dns → 生成 config.yml）
     - run_cf_tunnel.ps1 : 部署完成后，一键启动隧道
     - README.md        : 操作步骤 + 公网 IP 诊断结论 + 常见问题

用法：
  cd F:\steamdt-project\buy\汰换
  python proxy_server\setup_cf_tunnel.py                              # 全默认：866868686.xyz → 本机 80
  python proxy_server\setup_cf_tunnel.py --domain demo.866868686.xyz   # 绑子域名
  python proxy_server\setup_cf_tunnel.py --local-port 8080             # 本地回源换端口
  python proxy_server\setup_cf_tunnel.py --tunnel-name c5-callback-01  # 自定义隧道名
"""
import argparse
import ipaddress
import os
import socket
import subprocess
import sys
import threading
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path

PROXY_DIR = Path(__file__).resolve().parent
OUT_DIR = PROXY_DIR / "cf-tunnel"
OUT_DIR.mkdir(parents=True, exist_ok=True)

# ---------- 1. 公网 IP 探测 ----------

IP_SOURCES = [
    ("ifconfig.me",        "https://ifconfig.me/ip"),
    ("ipinfo.io/ip",       "https://ipinfo.io/ip"),
    ("ifconfig.co",        "https://ifconfig.co/ip"),
    ("ipip.net",           "https://myip.ipip.net/"),  # 返回文本，可能含中文：当前 IP：1.2.3.4
    ("aws checkip",       "https://checkip.amazonaws.com/"),
    ("ipv4.icanhazip.com", "https://ipv4.icanhazip.com/"),
]


def _clean_ip(text: str) -> str:
    """从响应文本中抽取第一个 IPv4。"""
    import re
    m = re.search(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", text or "")
    if not m:
        return ""
    ip = m.group(0)
    try:
        ipaddress.IPv4Address(ip)
        return ip
    except (ipaddress.AddressValueError, ValueError):
        return ""


def _fetch_one(name: str, url: str, timeout: float, holder: dict, lock: threading.Lock):
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "curl/8.0",
            "Accept": "text/plain",
        })
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", errors="ignore")
            ip = _clean_ip(body)
            if ip:
                with lock:
                    holder.setdefault("ips", []).append((name, ip))
    except Exception:
        pass


def detect_public_ip(timeout_per_source: float = 4.5,
                     max_wait: float = 7.0) -> list:
    """并行探测公网 IP。返回 [(source, ip)]，结果按响应速度排序。

    达到 max_wait 或全部完成即返回。
    """
    holder = {}
    lock = threading.Lock()
    threads = []
    for name, url in IP_SOURCES:
        t = threading.Thread(target=_fetch_one,
                             args=(name, url, timeout_per_source, holder, lock),
                             daemon=True)
        t.start()
        threads.append(t)

    deadline = datetime.now().timestamp() + max_wait
    # 每 100ms 检查是否至少有 2 个结果（稳定）或超时
    while datetime.now().timestamp() < deadline:
        if len(holder.get("ips", [])) >= 2:
            break
        threading.Event().wait(0.1)
    # 最长等到 deadline
    for t in threads:
        remaining = max(0.0, deadline - datetime.now().timestamp())
        t.join(timeout=remaining if remaining > 0 else 0)
    return holder.get("ips", [])


def list_local_ips() -> list:
    """返回本机 IPv4 列表（去重，排除回环）。"""
    ips = set()
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, family=socket.AF_INET):
            ips.add(info[4][0])
    except Exception:
        pass
    # socket.getaddrinfo 可能不全，再走 netifaces-like fallback
    try:
        # Windows 风格：通过 GetAdaptersAddresses 需要 ctypes；简单起见用 socket
        for iface in socket.gethostbyname_ex(socket.gethostname())[2]:
            ips.add(iface)
    except Exception:
        pass
    return sorted([ip for ip in ips if not ip.startswith("127.")])


def has_direct_public_ip(public_ip: str, local_ips: list) -> bool:
    if not public_ip:
        return False
    if public_ip in local_ips:
        return True
    # 某些路由器把本机 LAN 口的公网 IP 映射为另一个地址（1:1 NAT），
    # 此时虽然不等，但如果公网 IP 不在 CGNAT/私网段即可认为能做端口映射
    try:
        ip = ipaddress.IPv4Address(public_ip)
    except Exception:
        return False
    # 私网 / CGNAT / 回环 / 链路本地 / 组播 / 保留
    if ip.is_private or ip.is_loopback or ip.is_link_local \
            or ip.is_multicast or ip.is_reserved:
        return False
    # 100.64.0.0/10 = Carrier-grade NAT (共享公网)
    if ip in ipaddress.ip_network("100.64.0.0/10"):
        return False
    # 其他公网可达 IP → 认为可以配置端口映射
    return True


# ---------- 2. cloudflared 检测 ----------

def find_cloudflared() -> tuple:
    """查找 cloudflared。返回 (path, version) 或 (None, None)。"""
    candidates = []
    # PATH
    try:
        r = subprocess.run(["where.exe", "cloudflared"], capture_output=True,
                           text=True, timeout=5)
        if r.returncode == 0:
            for line in r.stdout.strip().splitlines():
                line = line.strip()
                if line.lower().endswith("cloudflared.exe") or \
                        line.lower().endswith("cloudflared"):
                    candidates.append(line)
    except Exception:
        pass
    # winget 默认安装位置
    winget_root = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Packages"
    if winget_root.exists():
        for p in winget_root.glob("*Cloudflare.cloudflared*\\cloudflared.exe"):
            candidates.append(str(p))
    # 常见手动下载目录
    for extra in [
        r"C:\Program Files\cloudflared\cloudflared.exe",
        r"C:\tools\cloudflared.exe",
        Path.home() / "scoop" / "shims" / "cloudflared.exe",
        Path.home() / "bin" / "cloudflared.exe",
    ]:
        ep = Path(extra)
        if ep.exists():
            candidates.append(str(ep))

    for cand in candidates:
        try:
            r = subprocess.run([cand, "--version"], capture_output=True,
                               text=True, timeout=5)
            if r.returncode == 0:
                return cand, r.stdout.strip()
        except Exception:
            continue
    return None, None


# ---------- 3. 模板生成 ----------

CONFIG_YML_TMPL = r"""# ------------------------------------------------------------
# Cloudflare Tunnel 配置文件（由 setup_cf_tunnel.py 生成）
# 绑定域名：{DOMAIN}       回源本地：http://127.0.0.1:{LOCAL_PORT}
# 生成时间：{GEN_TIME}
# ------------------------------------------------------------
tunnel: __TUNNEL_ID__
credentials-file: __CRED_FILE__

ingress:
  - hostname: {DOMAIN}
    service: http://127.0.0.1:{LOCAL_PORT}
    originRequest:
      httpHostHeader: "{DOMAIN}"
      noTLSVerify: true
      connectTimeout: 10s
      tcpKeepAlive: 30s

  # 兜底：返回 404
  - service: http_status:404
"""

DEPLOY_PS1_TMPL = r"""<#
.SYNOPSIS
  一键部署 Cloudflare Tunnel：认证 → 创建隧道 → 绑定 DNS → 生成 config.yml → 打印启动命令
  对应域名：{DOMAIN}  →  本地 http://127.0.0.1:{LOCAL_PORT}

.NOTES
  必须先安装 cloudflared。如未安装脚本会给出安装命令。
  首次运行需要浏览器授权 Cloudflare 账户（1 次即可）。
#>

$ErrorActionPreference = "Stop"

$HERE      = Split-Path -Parent $MyInvocation.MyCommand.Path
$TMPL_FILE = Join-Path $HERE "config.yml.tmpl"
$OUT_FILE  = Join-Path $HERE "config.yml"
$TUNNEL_NAME = "{TUNNEL_NAME}"
$DOMAIN    = "{DOMAIN}"
$LOCAL_PORT = {LOCAL_PORT}

# 0. 定位 cloudflared（先 PATH → 再扫常见安装目录，避免 winget 安装后当前会话 PATH 未刷新）
function Find-CloudflaredExe {{
    $cmd = (Get-Command cloudflared.exe -ErrorAction SilentlyContinue).Source
    if ($cmd) {{ return $cmd }}
    $candidates = @(
        (Join-Path ${{env:ProgramFiles(x86)}} "cloudflared\cloudflared.exe"),
        (Join-Path $env:ProgramFiles "cloudflared\cloudflared.exe"),
        (Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Links\cloudflared.exe"),
        (Join-Path $env:USERPROFILE "scoop\shims\cloudflared.exe"),
        (Join-Path $env:USERPROFILE "bin\cloudflared.exe"),
        (Join-Path $env:USERPROFILE ".local\bin\cloudflared.exe")
    )
    # WinGet Packages 目录通配搜索
    $wpkg = Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Packages"
    if (Test-Path $wpkg) {{
        Get-ChildItem -Path $wpkg -Recurse -Filter cloudflared.exe -ErrorAction SilentlyContinue |
            ForEach-Object {{ $candidates += $_.FullName }}
    }}
    foreach ($p in $candidates) {{
        if ([string]::IsNullOrWhiteSpace($p)) {{ continue }}
        if (Test-Path -LiteralPath $p) {{ return $p }}
    }}
    return $null
}}
$CF = Find-CloudflaredExe
if (-not $CF) {{
    Write-Host "❌ 未找到 cloudflared。请安装（任选其一）：" -ForegroundColor Red
    Write-Host "   1) winget install --id Cloudflare.cloudflared"
    Write-Host "   2) 手动下载: https://github.com/cloudflare/cloudflared/releases"
    Write-Host "   安装完成后重新运行本脚本。"
    exit 4
}}
Write-Host "✅ cloudflared = $CF" -ForegroundColor Green
& $CF --version

# 1. 认证前置检查：未认证会报缺少 origin cert
Write-Host "`n[1/5] 检查 Cloudflare 认证状态..." -ForegroundColor Cyan
$listResult = & $CF tunnel list 2>&1 | Out-String
if ($listResult -match "origin cert|cert\.pem|Unauthorized|not logged in|login") {{
    Write-Host "   ⚠️  未检测到 origin cert（cert.pem），先登录授权..."
    Write-Host "   即将弹出浏览器 → 选择 Cloudflare 账户与 866868686.xyz 域名所在 Zone。" -ForegroundColor Yellow
    Read-Host "   回车后打开浏览器完成授权" | Out-Null
    & $CF tunnel login
    if ($LASTEXITCODE -ne 0) {{
        Write-Host "❌ 登录失败，手动按 README.md 步骤操作后重试。" -ForegroundColor Red
        exit 2
    }}
}}

# 2. 列出已有隧道，避免重名
Write-Host "`n[2/5] 创建隧道 $TUNNEL_NAME（若已存在则复用）..." -ForegroundColor Cyan
$tunnelListJson = & $CF tunnel list --output json 2>$null | ConvertFrom-Json -ErrorAction SilentlyContinue
$existing = $tunnelListJson | Where-Object {{ $_.name -eq $TUNNEL_NAME }}
if ($existing) {{
    $TUNNEL_ID = $existing.id
    Write-Host "   ✅ 复用已存在隧道 ID=$TUNNEL_ID" -ForegroundColor Green
}} else {{
    & $CF tunnel create $TUNNEL_NAME
    if ($LASTEXITCODE -ne 0) {{
        Write-Host "❌ 创建隧道失败：检查账户权限或网络连通 Cloudflare API。" -ForegroundColor Red
        exit 3
    }}
    $tunnelListJson = & $CF tunnel list --output json 2>$null | ConvertFrom-Json -ErrorAction SilentlyContinue
    $TUNNEL_OBJ = $tunnelListJson | Where-Object {{ $_.name -eq $TUNNEL_NAME }}
    $TUNNEL_ID = $TUNNEL_OBJ.id
    Write-Host "   ✅ 新建隧道 ID=$TUNNEL_ID" -ForegroundColor Green
}}

# 3. 凭据文件路径（cloudflared 默认：$env:USERPROFILE\.cloudflared\<id>.json）
$CF_HOME = Join-Path $env:USERPROFILE ".cloudflared"
$CRED_FILE = (Resolve-Path (Join-Path $CF_HOME "$TUNNEL_ID.json") -ErrorAction SilentlyContinue).Path
if (-not $CRED_FILE) {{
    # fallback：其他可能位置
    $CRED_FILE = (Get-ChildItem -Path $CF_HOME -Filter "$TUNNEL_ID*.json" -ErrorAction SilentlyContinue | Select-Object -First 1).FullName
}}
if (-not $CRED_FILE) {{
    Write-Host "❌ 找不到凭据文件 $TUNNEL_ID.json，检查 $CF_HOME 目录。" -ForegroundColor Red
    exit 5
}}
Write-Host "   ✅ 凭据文件 = $CRED_FILE" -ForegroundColor Green

# 4. 绑定 DNS 路由（给 866868686.xyz 加 CNAME → 隧道 CNAME）
Write-Host "`n[4/5] 绑定 Cloudflare DNS 记录：$DOMAIN → 隧道 $TUNNEL_NAME" -ForegroundColor Cyan
& $CF tunnel route dns -f $TUNNEL_ID $DOMAIN
if ($LASTEXITCODE -ne 0) {{
    Write-Host "   ⚠️  DNS 绑定返回非 0（若已绑定 CNAME 指向该隧道可忽略）" -ForegroundColor Yellow
}} else {{
    Write-Host "   ✅ DNS CNAME 绑定成功，全球生效约 1-3 分钟" -ForegroundColor Green
}}

# 5. 生成 config.yml（替换占位符）
Write-Host "`n[5/5] 生成 config.yml → $OUT_FILE" -ForegroundColor Cyan
$tmpl = Get-Content $TMPL_FILE -Raw
$tmpl = $tmpl.Replace("__TUNNEL_ID__", $TUNNEL_ID)
# 路径转 POSIX 风格，避免 Windows 反斜杠问题
$CRED_FILE_POSIX = $CRED_FILE -replace '\\', '/'
$tmpl = $tmpl.Replace("__CRED_FILE__", $CRED_FILE_POSIX)
Set-Content -Path $OUT_FILE -Value $tmpl -Encoding UTF8
Write-Host "   ✅ config.yml 写入成功" -ForegroundColor Green

Write-Host "`n"
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "🎉 隧道部署完成！启动命令：" -ForegroundColor Green
Write-Host "   powershell -File $(Join-Path $HERE "run_cf_tunnel.ps1")" -ForegroundColor Yellow
Write-Host "   或直接：" -ForegroundColor Green
Write-Host "   & `"$CF`" tunnel --config `"$OUT_FILE`" run" -ForegroundColor Yellow
Write-Host "`n验证：2-3 分钟后手机（关 Wi-Fi）访问 http://$DOMAIN/health"
Write-Host "============================================================" -ForegroundColor Cyan
"""

RUN_PS1_TMPL = r"""<#
.SYNOPSIS  启动已部署好的 Cloudflare Tunnel。
#>
$ErrorActionPreference = "Continue"
$HERE   = Split-Path -Parent $MyInvocation.MyCommand.Path
$CFG    = Join-Path $HERE "config.yml"

function Find-CloudflaredExe {{
    $cmd = (Get-Command cloudflared.exe -ErrorAction SilentlyContinue).Source
    if ($cmd) {{ return $cmd }}
    $candidates = @(
        (Join-Path ${{env:ProgramFiles(x86)}} "cloudflared\cloudflared.exe"),
        (Join-Path $env:ProgramFiles "cloudflared\cloudflared.exe"),
        (Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Links\cloudflared.exe"),
        (Join-Path $env:USERPROFILE "scoop\shims\cloudflared.exe"),
        (Join-Path $env:USERPROFILE "bin\cloudflared.exe"),
        (Join-Path $env:USERPROFILE ".local\bin\cloudflared.exe")
    )
    $wpkg = Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Packages"
    if (Test-Path $wpkg) {{
        Get-ChildItem -Path $wpkg -Recurse -Filter cloudflared.exe -ErrorAction SilentlyContinue |
            ForEach-Object {{ $candidates += $_.FullName }}
    }}
    foreach ($p in $candidates) {{
        if ([string]::IsNullOrWhiteSpace($p)) {{ continue }}
        if (Test-Path -LiteralPath $p) {{ return $p }}
    }}
    return $null
}}
$CF = Find-CloudflaredExe
if (-not $CF) {{
    Write-Host "❌ 未找到 cloudflared.exe。请先安装：winget install --id Cloudflare.cloudflared" -ForegroundColor Red
    exit 2
}}
if (-not (Test-Path $CFG)) {{
    Write-Host "❌ config.yml 不存在。先跑 deploy.ps1 完成部署。" -ForegroundColor Red
    exit 1
}}
Write-Host "✅ cloudflared = $CF" -ForegroundColor Green
Write-Host "✅ 启动 Cloudflare Tunnel：$CFG" -ForegroundColor Green
Write-Host "   Ctrl+C 停止。" -ForegroundColor DarkGray
& $CF tunnel --config $CFG run
"""

README_TMPL = r"""# Cloudflare Tunnel 部署指南（C5 回调 → 本机 80 端口）

> 本目录内容由 `proxy_server/setup_cf_tunnel.py` 自动生成：
> - 域名：`{DOMAIN}`
> - 回源地址：`http://127.0.0.1:{LOCAL_PORT}`
> - 期望隧道名：`{TUNNEL_NAME}`
> - 生成时间：`{GEN_TIME}`

---

## 0. 公网 IP 诊断结论（脚本自动检测）

| 项 | 值 |
|---|---|
| 探测源数 | `{IP_SRC_COUNT}` |
| 公网 IP（多数一致的） | `{PUBLIC_IP_CONSENSUS}` |
| 本机网卡 IPv4 | `{LOCAL_IPS_STR}` |
| 是否拥有独立公网 IP | **`{HAS_PUBLIC_STR}`** |

### 建议：

- 👉 **`{HAS_PUBLIC_STR}` 独立公网 IP**
    {PUBLIC_RECO}
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
3. 创建/复用隧道（名：`{TUNNEL_NAME}`）
4. 绑定 DNS CNAME（`{DOMAIN}` → 隧道 CNAME 记录 `{{隧道ID}}.cfargotunnel.com`，deploy.ps1 会自动填真实值）
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
http://{DOMAIN}/health
```
预期返回：`{{"status":"ok"}}`

再模拟 C5 回调：
```bash
curl -X POST http://{DOMAIN}/c5/callback \
  -H "Content-Type: application/json" \
  -d '{{"tradeNo":"TEST999","status":1}}'
```
→ 返回文本 `success`，同时本机 `data/c5_callbacks.db` 的 `callbacks` 表新增一行。

## 5. 同步 hosts 注意事项

由于 Cloudflare Tunnel 接管了 {DOMAIN} 的公网解析（CNAME → *.cfargotunnel.com），
如果你在本机**仍然需要 `866868686.xyz → 127.0.0.1`**（C5 参数校验时本地访问该地址也走
HTTP 200 健康返回），请保留之前通过 `server_cli.py hosts add` 写入的 hosts 条目即可，
两者不冲突（hosts 优先级 > 公网 DNS）。

### 取消 hosts 场景
若希望本机访问 {DOMAIN} 也走 Cloudflare Tunnel（真实绕一圈公网回来），可以：
```powershell
python proxy_server\server_cli.py hosts remove --hostname {DOMAIN}
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
A: 说明本地回源 `http://127.0.0.1:{LOCAL_PORT}` 没起来。先跑 `server_cli.py status`
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

"""


# ---------- 主流程 ----------

def main():
    ap = argparse.ArgumentParser(description="公网 IP 探测 + Cloudflare Tunnel 文件生成器")
    ap.add_argument("--domain", default="866868686.xyz",
                    help="要绑定的域名（含子域），默认 866868686.xyz")
    ap.add_argument("--local-port", type=int, default=80,
                    help="本机回调服务端口（默认 80）")
    ap.add_argument("--tunnel-name", default="c5-callback",
                    help="Cloudflare Tunnel 名称")
    ap.add_argument("--no-ip-detect", action="store_true",
                    help="跳过公网 IP 探测（离线场景）")
    args = ap.parse_args()

    domain = args.domain
    port = args.local_port
    tunnel_name = args.tunnel_name
    gen_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    print("=" * 64)
    print("🛰️  公网 IP 探测 + Cloudflare Tunnel 文件生成器")
    print("=" * 64)

    # 1. IP 探测
    local_ips = list_local_ips()
    if args.no_ip_detect:
        print("⏭  已跳过公网 IP 探测")
        pub_ips = []
    else:
        print(f"\n[1/4] 探测公网 IP（{len(IP_SOURCES)} 个源并行，最多 7s）...")
        pub_ips = detect_public_ip()
        if not pub_ips:
            print("   ❌ 所有源均未返回有效 IP。可能是网络不通。")
            print("      继续使用 --no-ip-detect 选项可离线生成配置文件。")
            sys.exit(3)

    # 找共识 IP
    ip_counts = {}
    for _, ip in pub_ips:
        ip_counts[ip] = ip_counts.get(ip, 0) + 1
    consensus_ip = max(ip_counts.items(), key=lambda x: x[1])[0] if ip_counts else ""

    print(f"\n[2/4] 公网 IP 探测结果（{len(pub_ips)} 个源）：")
    for src, ip in pub_ips:
        mark = "✅" if ip == consensus_ip else "  "
        print(f"   {mark}  [{src:20s}]  {ip}")
    if consensus_ip:
        print(f"\n   多数一致 IP: {consensus_ip}")

    print(f"\n[3/4] 本机网卡 IPv4（非回环）：")
    if local_ips:
        for ip in local_ips:
            print(f"   - {ip}")
    else:
        print("   ⚠️  未枚举到网卡地址")

    has_public = has_direct_public_ip(consensus_ip, local_ips)
    print(f"\n   🎯 是否拥有独立公网 IP：{'✅ YES' if has_public else '❌ NO（大内网/CGNAT 或其他共享IP场景）'}")
    if has_public:
        public_reco = ("你的本地 WAN 口有独立公网 IP，也可以不用 Tunnel，直接在 DNS 填 A 记录 + "
                       "路由器端口映射 80 → 本机即可。Tunnel 仍推荐（省掉维护端口映射 + 抗扫描）。")
    else:
        public_reco = ("你的本地没有独立公网 IP，**必须使用 Cloudflare Tunnel 或其他内网穿透方案** "
                       "才能让 C5 服务器的回调真实送达本机。")

    print(f"\n[4/4] 生成配置文件到 {OUT_DIR}")
    vars_ctx = dict(
        DOMAIN=domain,
        LOCAL_PORT=port,
        TUNNEL_NAME=tunnel_name,
        GEN_TIME=gen_time,
    )
    cfg_tmpl_text = CONFIG_YML_TMPL.format(**vars_ctx)
    deploy_text = DEPLOY_PS1_TMPL.format(**vars_ctx)
    run_text = RUN_PS1_TMPL.format(**vars_ctx)
    readme_text = README_TMPL.format(
        **vars_ctx,
        IP_SRC_COUNT=len(pub_ips) if pub_ips else 0,
        PUBLIC_IP_CONSENSUS=consensus_ip or "(未检测到)",
        LOCAL_IPS_STR=", ".join(local_ips) if local_ips else "(空)",
        HAS_PUBLIC_STR="YES 有" if has_public else "NO 无",
        PUBLIC_RECO=public_reco,
    )

    # .ps1 必须用 utf-8-sig（带 BOM），否则 Windows PowerShell 5 默认
    # 用系统代码页（GBK）解析，含中文时会乱码并导致 ParserError / MissingEndCurlyBrace
    _enc_ps1 = "utf-8-sig"
    (OUT_DIR / "config.yml.tmpl").write_text(cfg_tmpl_text, encoding="utf-8")
    (OUT_DIR / "deploy.ps1").write_text(deploy_text, encoding=_enc_ps1)
    (OUT_DIR / "run_cf_tunnel.ps1").write_text(run_text, encoding=_enc_ps1)
    (OUT_DIR / "README.md").write_text(readme_text, encoding="utf-8")

    print(f"   ✅ config.yml.tmpl   （{len(cfg_tmpl_text)} bytes）")
    print(f"   ✅ deploy.ps1        （{len(deploy_text)} bytes）")
    print(f"   ✅ run_cf_tunnel.ps1 （{len(run_text)} bytes）")
    print(f"   ✅ README.md         （{len(readme_text)} bytes）")

    # 5. 检测 cloudflared
    print(f"\n[附加] cloudflared 安装检测：")
    cf_path, cf_ver = find_cloudflared()
    if cf_path:
        print(f"   ✅ 已安装: {cf_path}  版本: {cf_ver}")
    else:
        print("   ❌ 未安装。安装命令：")
        print("      winget install --id Cloudflare.cloudflared")

    print("\n" + "=" * 64)
    print("下一步：")
    print(f"  1) cd {OUT_DIR}")
    print("  2) powershell -ExecutionPolicy Bypass -File .\\deploy.ps1")
    print("  3) 完成后跑：powershell -File .\\run_cf_tunnel.ps1")
    print(f"  详细步骤见：{OUT_DIR / 'README.md'}")
    print("=" * 64)


if __name__ == "__main__":
    main()
