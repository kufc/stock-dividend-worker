@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title AI 媒體分類器 - 安裝
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

if not exist "media_sorter\installer.py" (
    echo.
    echo 找不到程式檔案！請先在 ZIP 上按右鍵「全部解壓縮」，
    echo 再執行解壓縮後資料夾裡的 install.bat（不要直接在 ZIP 裡雙擊）。
    echo.
    pause
    exit /b 1
)

rem 已存在的 .venv 若是 32 位元或版本太舊，刪掉重建
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -c "import sys,struct;sys.exit(not(sys.version_info[:2]>=(3,10) and struct.calcsize('P')==8))" >nul 2>&1
    if errorlevel 1 (
        echo 現有的 .venv 無法使用，重新建立⋯
        rmdir /s /q ".venv"
    )
)

if exist ".venv\Scripts\python.exe" goto :install

rem 依序尋找建議的 Python 版本（64 位元、3.10 以上）
set "PY="
set "CHECK=import sys,struct;sys.exit(not(sys.version_info[:2]>=(3,10) and struct.calcsize('P')==8))"
for %%V in (3.12 3.11 3.13 3.10) do (
    if not defined PY py -%%V -c "%CHECK%" >nul 2>&1 && set "PY=py -%%V"
)
if not defined PY py -3 -c "%CHECK%" >nul 2>&1 && set "PY=py -3"
if not defined PY python -c "%CHECK%" >nul 2>&1 && set "PY=python"
if not defined PY (
    echo.
    echo 找不到可用的 Python！（需要 64 位元的 Python 3.10 以上）
    echo 請到 https://www.python.org/downloads/ 下載安裝 Python 3.12，
    echo 安裝時記得勾選「Add python.exe to PATH」，裝完後再雙擊 install.bat。
    echo.
    pause
    exit /b 1
)
echo 使用 %PY% 建立虛擬環境 .venv ⋯
%PY% -m venv .venv
if not exist ".venv\Scripts\python.exe" (
    echo 建立虛擬環境失敗，請確認 Python 安裝正確。
    pause
    exit /b 1
)

:install
rem （更新 pip 與後續安裝都由 media_sorter.installer 處理，含 pip 憑證元件當掉時的自動改用備援）
".venv\Scripts\python.exe" -m media_sorter.installer
if errorlevel 1 (
    echo.
    echo 安裝沒有完成，請把上面的訊息截圖以便排除問題。
    pause
    exit /b 1
)
echo.
pause
