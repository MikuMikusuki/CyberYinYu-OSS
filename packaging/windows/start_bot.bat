@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] 找不到 .venv，请先运行 install.ps1。
    pause
    exit /b 1
)
if not exist "otowa.env" (
    echo [ERROR] 找不到 otowa.env，请先复制 .env.example 并填写配置。
    pause
    exit /b 1
)

set "PYTHONUTF8=1"
echo [INFO] 正在启动 CyberYinYu...
".venv\Scripts\python.exe" "bot.py"
set "EXIT_CODE=%ERRORLEVEL%"
echo.
echo [INFO] 进程已退出，代码 %EXIT_CODE%。
pause
exit /b %EXIT_CODE%
