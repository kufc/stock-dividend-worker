@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\install-ok.txt" (
    echo 尚未安裝完成，先進行安裝⋯
    call "%~dp0install.bat"
    if errorlevel 1 exit /b 1
)
if not exist ".venv\install-ok.txt" exit /b 1
start "" ".venv\Scripts\pythonw.exe" -m media_sorter
