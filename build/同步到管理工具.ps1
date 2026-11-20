#!/usr/bin/env pwsh
<#
.SYNOPSIS
  构建并发布「纯净版」S5A3 服务端管理工具到 C:\DNF\管理工具\S5A3服务端管理工具

.DESCRIPTION
  一条命令完成：重新打包 → 清空目标目录 → 只放 4 个文件（exe / 使用说明 / 版本信息 / SHA256SUMS）。
  目标目录**不含任何源码**（.py/.spec/.ps1 等），供他人直接使用。

  每次更新都执行一次，旧版本会被整体覆盖。

.EXAMPLE
  pwsh -File .\同步到管理工具.ps1              # 用 version_info.txt 里的版本号
  pwsh -File .\同步到管理工具.ps1 -Version 2026.11.21.1
  pwsh -File .\同步到管理工具.ps1 -SkipBuild   # 只发布已有 dist 产物
#>
param(
    [string]$Version = "",
    [switch]$SkipBuild,
    [string]$Target = "C:\DNF\管理工具\S5A3服务端管理工具"
)
$ErrorActionPreference = "Stop"
$Src = $PSScriptRoot
$Python = "C:\py312\python.exe"

if (-not (Test-Path $Python)) { throw "找不到 Python: $Python" }

# 版本号：从 version_info.txt 解析 filevers=(a, b, c, d)
if (-not $Version) {
    $vi = Join-Path $Src "version_info.txt"
    if (Test-Path $vi) {
        $m = Select-String -Path $vi -Pattern 'filevers=\((\d+),\s*(\d+),\s*(\d+),\s*(\d+)\)' |
            Select-Object -First 1
        if ($m) { $Version = "$($m.Matches[0].Groups[1].Value).$($m.Matches[0].Groups[2].Value).$($m.Matches[0].Groups[3].Value).$($m.Matches[0].Groups[4].Value)" }
    }
}
if (-not $Version) { throw "无法确定版本号，请用 -Version 指定" }
Write-Host "版本: $Version" -ForegroundColor Cyan

# 1) 打包
if (-not $SkipBuild) {
    Write-Host "[1/3] PyInstaller 打包…" -ForegroundColor Cyan
    Push-Location $Src
    try {
        & $Python -m PyInstaller --noconfirm --clean "S5A3服务端管理.spec"
        if ($LASTEXITCODE -ne 0) { throw "PyInstaller 失败" }
    } finally { Pop-Location }
} else {
    Write-Host "[1/3] 跳过打包（-SkipBuild）" -ForegroundColor Yellow
}

$exe = Join-Path $Src "dist\S5A3服务端管理.exe"
if (-not (Test-Path $exe)) { throw "未找到产物: $exe" }

# 2) 纯净发布（清空目标目录 + 写入 4 个文件 + 自检）
Write-Host "[2/3] 发布纯净版…" -ForegroundColor Cyan
$pub = Join-Path $Src "publish_clean.py"
if (-not (Test-Path $pub)) { throw "缺少 publish_clean.py（应与本脚本同目录）" }
& $Python $pub --exe $exe --version $Version --target $Target
if ($LASTEXITCODE -ne 0) { throw "发布失败" }

# 3) 复核
Write-Host "[3/3] 复核目录…" -ForegroundColor Cyan
Get-ChildItem $Target -Force | Select-Object Name, Length | Format-Table -AutoSize
$srcHash = (Get-FileHash $exe -Algorithm SHA256).Hash.ToLower()
$dstHash = (Get-FileHash (Join-Path $Target "S5A3服务端管理.exe") -Algorithm SHA256).Hash.ToLower()
if ($srcHash -ne $dstHash) { throw "哈希不一致: $srcHash vs $dstHash" }
Write-Host "[完成] $Target  版本 $Version  sha256 $($srcHash.Substring(0,16))…" -ForegroundColor Green
