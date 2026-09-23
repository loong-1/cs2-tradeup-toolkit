<#
.SYNOPSIS  启动已部署好的 Cloudflare Tunnel。
#>
$ErrorActionPreference = "Continue"
$HERE   = Split-Path -Parent $MyInvocation.MyCommand.Path
$CFG    = Join-Path $HERE "config.yml"

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
    Write-Host "❌ 未找到 cloudflared.exe。请先安装：winget install --id Cloudflare.cloudflared" -ForegroundColor Red
    exit 2
}
if (-not (Test-Path $CFG)) {
    Write-Host "❌ config.yml 不存在。先跑 deploy.ps1 完成部署。" -ForegroundColor Red
    exit 1
}
Write-Host "✅ cloudflared = $CF" -ForegroundColor Green
Write-Host "✅ 启动 Cloudflare Tunnel：$CFG" -ForegroundColor Green
Write-Host "   Ctrl+C 停止。" -ForegroundColor DarkGray
& $CF tunnel --config $CFG run
