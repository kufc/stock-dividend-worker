@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title AI 媒體分類器 - 解除安裝
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

if not exist "media_sorter\uninstaller.py" (
    echo.
    echo 找不到解除安裝程式檔案。請在程式資料夾裡執行這個檔案。
    echo.
    pause
    exit /b 1
)

rem 優先用「.venv 以外」的 Python 執行，這樣才能連 .venv 一起移除；
rem 找不到時改用 .venv 裡的 Python，結束後由這個檔案移除 .venv。
set "PY="
set "CHECK=import sys;sys.exit(not sys.version_info[:2]>=(3,8))"
for %%V in (3.12 3.11 3.13 3.10) do (
    if not defined PY py -%%V -c "%CHECK%" >nul 2>&1 && set "PY=py -%%V"
)
if not defined PY py -3 -c "%CHECK%" >nul 2>&1 && set "PY=py -3"
if not defined PY python -c "%CHECK%" >nul 2>&1 && set "PY=python"
if not defined PY if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
if not defined PY (
    echo.
    echo 找不到可用的 Python，無法執行解除安裝程式。
    echo 可以自行刪除整個程式資料夾；你的照片與整理結果不在這個資料夾裡，不會受影響。
    echo.
    pause
    exit /b 1
)

%PY% -m media_sorter.uninstaller %*
set "RC=%errorlevel%"

rem 結束代碼 10：剛才是用 .venv 裡的 Python 執行的，現在它已經結束，可以移除 .venv 了。
rem （解除安裝程式已經確認 .venv 是一般資料夾、不是連結，才會回傳 10。）
if "%RC%"=="10" (
    echo.
    echo 移除運作環境（.venv）⋯
    rmdir /s /q ".venv" >nul 2>&1
    if exist ".venv" (
        echo ✘ 無法完全移除 .venv，可能有程式正在使用它。請關閉相關視窗後再執行一次 uninstall.bat。
        set "RC=1"
    ) else (
        echo ✔ 解除安裝完成。你的照片與整理結果都沒有被動到。
        set "RC=0"
    )
)
echo.
pause
exit /b %RC%
