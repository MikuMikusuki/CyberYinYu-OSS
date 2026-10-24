@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist "logs\bot.log" (
    type "logs\bot.log"
) else (
    echo 还没有日志文件。先启动一次机器人。
)
pause
