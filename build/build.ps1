# 构建 S5A3 服务端管理工具（Windows）
# 需要：Python 3.12、pip install paramiko pyinstaller
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

python -m PyInstaller --noconfirm --clean "S5A3服务端管理.spec"
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 构建失败" }

Write-Host ""
Write-Host "产物: dist\S5A3服务端管理.exe"
Get-ChildItem dist\*.exe | Select-Object Name, Length
Write-Host "自检: .\dist\S5A3服务端管理.exe --selftest"
