@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title AI 媒體分類器 - 下載並安裝 AI 元件
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PYTHONUNBUFFERED=1"
echo.
echo ============================================================
echo  AI 媒體分類器 - 下載並安裝 AI 元件（PyTorch、AI 模型）
echo  這一步需要網路，會下載數 GB 的檔案，請不要關閉這個視窗。
echo ============================================================
echo.
"%~dp0..\python\python.exe" -u -m media_sorter.installer
set "RC=%errorlevel%"
echo.
if not "%RC%"=="0" (
    echo ✘ 沒有完成。請看上面的說明，處理後可以從開始功能表執行「修復」再試一次。
    if /i not "%~1"=="nopause" pause
    exit /b %RC%
)
if /i "%~1"=="pause" pause
exit /b 0
