@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set PYTHONUNBUFFERED=1

echo [WEB] 控制台啟動腳本

echo [執行] auto_fight.py %*
py -u auto_fight.py %*
set EXIT_CODE=%errorlevel%
echo [WEB] 腳本結束，exit_code=%EXIT_CODE%
exit /b %EXIT_CODE%
