# 构建 S5A3 服务端管理工具（Windows）
# 需要：Python 3.12、pip install paramiko pyinstaller
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

python -m PyInstaller --noconfirm --clean --onefile --windowed `
  --name "S5A3服务端管理" `
  --icon "S5A3工具图标.ico" `
  --version-file "version_info.txt" `
  --add-data "S5A3BG.jpg;." `
  s5a3_deploy_tool.py

Write-Host ""
Write-Host "产物: dist\S5A3服务端管理.exe"
Get-ChildItem dist\*.exe | Select-Object Name, Length
Write-Host "自检: .\dist\S5A3服务端管理.exe --selftest"
