<#
.SYNOPSIS
  一键部署 Cloudflare Tunnel：认证 → 创建隧道 → 绑定 DNS → 生成 config.yml → 打印启动命令
  对应域名：866868686.xyz  →  本地 http://127.0.0.1:80

.NOTES
  必须先安装 cloudflared。如未安装脚本会给出安装命令。
  首次运行需要浏览器授权 Cloudflare 账户（1 次即可）。
#>

$ErrorActionPreference = "Stop"

$HERE      = Split-Path -Parent $MyInvocation.MyCommand.Path
$TMPL_FILE = Join-Path $HERE "config.yml.tmpl"
$OUT_FILE  = Join-Path $HERE "config.yml"
$TUNNEL_NAME = "c5-callback"
$DOMAIN    = "866868686.xyz"
$LOCAL_PORT = 80

# 0. 定位 cloudflared（先 PATH → 再扫常见安装目录，避免 winget 安装后当前会话 PATH 未刷新）
function Find-CloudflaredExe {
    $cmd = (Get-Command cloudflared.exe -ErrorAction SilentlyContinue).Source
    if ($cmd) { return $cmd }
    $candidates = @(
        (Join-Path ${env:ProgramFiles(x86)} "cloudflared\cloudflared.exe"),
        (Join-Path $env:ProgramFiles "cloudflared\cloudflared.exe"),
        (Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Links\cloudflared.exe"),
        (Join-Path $env:USERPROFILE "scoop\shims\cloudflared.exe"),
        (Join-Path $env:USERPROFILE "bin\cloudflared.exe"),
        (Join-Path $env:USERPROFILE ".local\bin\cloudflared.exe")
    )
    # WinGet Packages 目录通配搜索
    $wpkg = Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Packages"
    if (Test-Path $wpkg) {
        Get-ChildItem -Path $wpkg -Recurse -Filter cloudflared.exe -ErrorAction SilentlyContinue |
            ForEach-Object { $candidates += $_.FullName }
    }
    foreach ($p in $candidates) {
        if ([string]::IsNullOrWhiteSpace($p)) { continue }
        if (Test-Path -LiteralPath $p) { return $p }
    }
    return $null
}
$CF = Find-CloudflaredExe
if (-not $CF) {
    Write-Host "❌ 未找到 cloudflared。请安装（任选其一）：" -ForegroundColor Red
    Write-Host "   1) winget install --id Cloudflare.cloudflared"
    Write-Host "   2) 手动下载: https://github.com/cloudflare/cloudflared/releases"
    Write-Host "   安装完成后重新运行本脚本。"
    exit 4
}
Write-Host "✅ cloudflared = $CF" -ForegroundColor Green
& $CF --version

# 1. 认证前置检查：未认证会报缺少 origin cert
Write-Host "`n[1/5] 检查 Cloudflare 认证状态..." -ForegroundColor Cyan
$listResult = & $CF tunnel list 2>&1 | Out-String
if ($listResult -match "origin cert|cert\.pem|Unauthorized|not logged in|login") {
    Write-Host "   ⚠️  未检测到 origin cert（cert.pem），先登录授权..."
    Write-Host "   即将弹出浏览器 → 选择 Cloudflare 账户与 866868686.xyz 域名所在 Zone。" -ForegroundColor Yellow
    Read-Host "   回车后打开浏览器完成授权" | Out-Null
    & $CF tunnel login
    if ($LASTEXITCODE -ne 0) {
        Write-Host "❌ 登录失败，手动按 README.md 步骤操作后重试。" -ForegroundColor Red
        exit 2
    }
}

# 2. 列出已有隧道，避免重名
Write-Host "`n[2/5] 创建隧道 $TUNNEL_NAME（若已存在则复用）..." -ForegroundColor Cyan
$tunnelListJson = & $CF tunnel list --output json 2>$null | ConvertFrom-Json -ErrorAction SilentlyContinue
$existing = $tunnelListJson | Where-Object { $_.name -eq $TUNNEL_NAME }
if ($existing) {
    $TUNNEL_ID = $existing.id
    Write-Host "   ✅ 复用已存在隧道 ID=$TUNNEL_ID" -ForegroundColor Green
} else {
    & $CF tunnel create $TUNNEL_NAME
    if ($LASTEXITCODE -ne 0) {
        Write-Host "❌ 创建隧道失败：检查账户权限或网络连通 Cloudflare API。" -ForegroundColor Red
        exit 3
    }
    $tunnelListJson = & $CF tunnel list --output json 2>$null | ConvertFrom-Json -ErrorAction SilentlyContinue
    $TUNNEL_OBJ = $tunnelListJson | Where-Object { $_.name -eq $TUNNEL_NAME }
    $TUNNEL_ID = $TUNNEL_OBJ.id
    Write-Host "   ✅ 新建隧道 ID=$TUNNEL_ID" -ForegroundColor Green
}

# 3. 凭据文件路径（cloudflared 默认：$env:USERPROFILE\.cloudflared\<id>.json）
$CF_HOME = Join-Path $env:USERPROFILE ".cloudflared"
$CRED_FILE = (Resolve-Path (Join-Path $CF_HOME "$TUNNEL_ID.json") -ErrorAction SilentlyContinue).Path
if (-not $CRED_FILE) {
    # fallback：其他可能位置
    $CRED_FILE = (Get-ChildItem -Path $CF_HOME -Filter "$TUNNEL_ID*.json" -ErrorAction SilentlyContinue | Select-Object -First 1).FullName
}
if (-not $CRED_FILE) {
    Write-Host "❌ 找不到凭据文件 $TUNNEL_ID.json，检查 $CF_HOME 目录。" -ForegroundColor Red
    exit 5
}
Write-Host "   ✅ 凭据文件 = $CRED_FILE" -ForegroundColor Green

# 4. 绑定 DNS 路由（给 866868686.xyz 加 CNAME → 隧道 CNAME）
Write-Host "`n[4/5] 绑定 Cloudflare DNS 记录：$DOMAIN → 隧道 $TUNNEL_NAME" -ForegroundColor Cyan
& $CF tunnel route dns -f $TUNNEL_ID $DOMAIN
if ($LASTEXITCODE -ne 0) {
    Write-Host "   ⚠️  DNS 绑定返回非 0（若已绑定 CNAME 指向该隧道可忽略）" -ForegroundColor Yellow
} else {
    Write-Host "   ✅ DNS CNAME 绑定成功，全球生效约 1-3 分钟" -ForegroundColor Green
}

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
