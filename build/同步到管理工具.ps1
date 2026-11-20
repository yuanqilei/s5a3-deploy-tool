#!/usr/bin/env pwsh
<#
.SYNOPSIS
  把 S5A3 运维工具发布到干净包目录（C:\DNF\管理工具\S5A3服务端管理工具）。

.DESCRIPTION
  每次改完源码、重新 PyInstaller 打包后执行一次即可。
  - 只同步固定 9 个文件，不碰包内其它东西
  - 同步前清掉包内的 s5a3_config.json / __pycache__ / backups（含明文密码，不该外带）
  - 同步后逐文件比对 SHA256，不一致就报错退出

.EXAMPLE
  pwsh -File .\同步到管理工具.ps1
#>
$ErrorActionPreference = "Stop"

$Src = $PSScriptRoot
$Dst = "C:\DNF\管理工具\S5A3服务端管理工具"

$Files = @(
    "S5A3服务端管理.exe",
    "S5A3BG.jpg",
    "S5A3工具图标.ico",
    "启动部署工具.bat",
    "s5a3_deploy_tool.py",
    "README-给DSH.md",
    "登录器服务端-使用攻略.md",
    "客户端更新-设计方案-20261118.md",
    "注册码登录-接口契约-20261118.md",
    "UI优化-交付说明-20261118.md",
    "复核意见-20261119.md",
    "GM-二次开发指南-20261119.md",
    "网关与登录器-交接.md",
    "HANDOFF-2026-11-17.md",
    "归档-20261118.md",
    "同步到管理工具.ps1"
)

$miss = $Files | Where-Object { -not (Test-Path (Join-Path $Src $_)) }
if ($miss) {
    Write-Host "[失败] 源目录缺少文件：" -ForegroundColor Red
    $miss | ForEach-Object { Write-Host "   - $_" }
    exit 1
}

New-Item -ItemType Directory -Force -Path $Dst | Out-Null

# 清理不该带的
foreach ($junk in @("s5a3_config.json", "__pycache__", "backups")) {
    $p = Join-Path $Dst $junk
    if (Test-Path $p) { Remove-Item $p -Recurse -Force -ErrorAction SilentlyContinue }
}

# 复制
foreach ($f in $Files) { Copy-Item (Join-Path $Src $f) (Join-Path $Dst $f) -Force }

# 校验
$bad = @()
foreach ($f in $Files) {
    $a = (Get-FileHash (Join-Path $Src $f) -Algorithm SHA256).Hash
    $b = (Get-FileHash (Join-Path $Dst $f) -Algorithm SHA256).Hash
    $flag = if ($a -eq $b) { "OK  " } else { "差异"; if ($a -ne $b) { $bad += $f } }
    "{0} {1,-30} {2}" -f $flag, $f, $a.Substring(0, 16)
}

# 包内不应有残留
$junk2 = Get-ChildItem $Dst -Force | Where-Object { $_.Name -match 's5a3_config|__pycache__|backups' }
if ($junk2) {
    Write-Host "[失败] 包内有残留：" -ForegroundColor Red
    $junk2 | ForEach-Object { Write-Host "   - $($_.Name)" }
    exit 1
}

if ($bad.Count -gt 0) {
    Write-Host "[失败] 以下文件哈希不一致：" -ForegroundColor Red
    $bad | ForEach-Object { Write-Host "   - $_" }
    exit 1
}

$count = (Get-ChildItem $Dst -Force -File | Measure-Object).Count
Write-Host ""
Write-Host "[完成] 已同步 $count 个文件到 $Dst" -ForegroundColor Green
