@echo off
chcp 65001 >nul
cd /d "%~dp0"
:: 強制 Python 以 UTF-8 輸出，避免 CP950 編碼錯誤
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

echo =========================================
echo  Under Dark 控制台 - 環境檢查
echo =========================================

:: 確認 Python 可用
where py >nul 2>&1
if %errorlevel% == 0 (
    set PYTHON=py
    goto :check_venv
)
where python >nul 2>&1
if %errorlevel% == 0 (
    set PYTHON=python
    goto :check_venv
)
echo [錯誤] 找不到 Python，請至 https://www.python.org/ 下載安裝（安裝時勾選 Add to PATH）
pause
exit /b 1

:check_venv
:: 若虛擬環境不存在則建立
if not exist ".venv\Scripts\activate.bat" (
    echo [設定] 建立虛擬環境 .venv ...
    %PYTHON% -m venv .venv
)

:: 啟用虛擬環境
call .venv\Scripts\activate.bat

:: 檢查並安裝依賴
echo [設定] 確認套件已安裝 ...
pip show flask >nul 2>&1
if %errorlevel% neq 0 (
    echo [設定] 安裝依賴套件（首次需要數分鐘）...
    pip install opencv-python flask pillow ddddocr numpy
)

echo.
echo =========================================
echo  啟動控制台  http://127.0.0.1:5050
echo  關閉此視窗即停止伺服器
echo =========================================
echo.

python image_tester.py

pause
