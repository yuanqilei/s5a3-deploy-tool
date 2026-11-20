@echo off
chcp 65001 >nul
setlocal
set "SCRIPT=%~dp0s5a3_deploy_tool.py"

if exist "C:\py312\pythonw.exe" (
    start "" "C:\py312\pythonw.exe" "%SCRIPT%"
    goto :eof
)
if exist "C:\py312\python.exe" (
    start "" "C:\py312\python.exe" "%SCRIPT%"
    goto :eof
)

where pythonw >nul 2>&1
if %errorlevel%==0 (
    start "" pythonw "%SCRIPT%"
    goto :eof
)

echo 未找到可用的 Python（需要带 tkinter 和 paramiko）。
echo 请先安装 Python 3.10+ 并执行: pip install paramiko
pause
