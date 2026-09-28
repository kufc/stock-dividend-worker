@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title AI 媒體分類器 - 安裝

set "PY="
py -3 --version >nul 2>&1 && set "PY=py -3"
if not defined PY python --version >nul 2>&1 && set "PY=python"
if not defined PY (
    echo.
    echo 找不到 Python！
    echo 請到 https://www.python.org/downloads/ 下載安裝 Python 3.12，
    echo 安裝時記得勾選「Add python.exe to PATH」，裝完後再雙擊 install.bat。
    echo.
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo 建立虛擬環境 .venv ⋯
    %PY% -m venv .venv
)
if not exist ".venv\Scripts\python.exe" (
    echo 建立虛擬環境失敗，請確認 Python 安裝正確。
    pause
    exit /b 1
)

".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m media_sorter.installer
if errorlevel 1 (
    echo.
    echo 安裝沒有完成，請把上面的訊息截圖以便排除問題。
    pause
    exit /b 1
)
echo.
pause
