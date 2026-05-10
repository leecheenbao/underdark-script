@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo [ADB] 連線 127.0.0.1:16384 ...
adb connect 127.0.0.1:16384
if errorlevel 1 (
    echo [錯誤] adb 指令失敗，請確認已安裝 ADB 並在 PATH 中。
    pause
    exit /b 1
)

echo.
echo [執行] auto_fight.py ...
py auto_fight.py

echo.
pause
