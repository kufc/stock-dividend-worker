#!/usr/bin/env bash
# 在 Linux 上用 Wine 實際跑一次「安裝 → 使用 → 解除安裝」，驗證安裝程式的行為（不需要 Windows 電腦）。
# 用法：installer/test_in_wine.sh path/to/AI-Media-Sorter-Setup-x.y.z.exe
# 需要：wine64、xvfb（apt install wine64 xvfb）。安裝時用 /SKIPDEPS 略過 PyTorch 與模型的下載。
# 選用：WHEELS=資料夾（放 pillow、numpy、av、send2trash 的 win_amd64 wheel），會再用安裝好的 Python 實際開啟視窗程式跑一遍。
#   注意：Wine 9 缺少 ucrtbase 的 crealf，numpy 2.x 在 Wine 裡無法載入，請用 numpy==1.26.4（真正的 Windows 沒有這個問題）。
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
SETUP="$(realpath "$1")"
export PATH="$PATH:/usr/lib/wine" WINEDEBUG=-all WINEARCH=win64 LC_ALL=C.UTF-8 LANG=C.UTF-8
export WINEPREFIX="${WINEPREFIX:-/tmp/wine-media-sorter-test}"
rm -rf "$WINEPREFIX"
Xvfb :77 -screen 0 1280x1024x24 >/dev/null 2>&1 &
XVFB=$!
trap 'kill $XVFB 2>/dev/null' EXIT
export DISPLAY=:77
sleep 2
# 執行 Windows 程式並等到它啟動的所有子程序都結束（解除安裝程式會複製自己到暫存資料夾再執行）
W() { timeout "${T:-110}" wine64 "$@" 2>&1 | grep -v "X connection"; local rc=${PIPESTATUS[0]}; [ -z "${NOWAIT:-}" ] && timeout 60 wineserver -w 2>/dev/null; return $rc; }
LOCAL="$WINEPREFIX/drive_c/users/$(whoami)/AppData/Local"
PROG="$LOCAL/Programs/AI Media Sorter"; DATA="$LOCAL/AI Media Sorter"
START="$WINEPREFIX/drive_c/users/$(whoami)/AppData/Roaming/Microsoft/Windows/Start Menu/Programs"
WINPROG="C:\\users\\$(whoami)\\AppData\\Local\\Programs\\AI Media Sorter"
UNKEY='HKCU\Software\Microsoft\Windows\CurrentVersion\Uninstall\AIMediaSorter'
fail=0
check() { if eval "$2"; then echo "  ✔ $1"; else echo "  ✘ $1"; fail=1; fi; }

echo "== 安裝（靜默、略過下載）"
W "$SETUP" /S /SKIPDEPS; echo "  結束代碼 $?"
check "程式與內建 Python 已安裝" '[ -f "$PROG/python/python.exe" ] && [ -f "$PROG/app/media_sorter/app.py" ] && [ -f "$PROG/uninstall.exe" ]'
check "有安裝標記 installed.flag" '[ -f "$PROG/app/installed.flag" ]'
check "開始功能表有捷徑" '[ "$(find "$START" -name "*.lnk" | wc -l)" -ge 4 ]'
if command -v osslsigncode >/dev/null && osslsigncode verify -in "$SETUP" >/dev/null 2>&1 || osslsigncode verify -in "$SETUP" 2>&1 | grep -q "Signer's certificate"; then
  check "安裝好的 uninstall.exe 也有數位簽章" 'osslsigncode verify -in "$PROG/uninstall.exe" 2>&1 | grep -q "Signer.s certificate"'
fi
check "「設定 → 應用程式」有登錄項目" 'W reg query "$UNKEY" | grep -q "DisplayVersion"'
check "登錄項目有靜默解除安裝指令" 'W reg query "$UNKEY" | grep -q "QuietUninstallString"'
echo "== 內建 Python 可以執行、找得到使用者資料夾、tkinter 可用"
cd "$PROG/app" && W ../python/python.exe -c "
from media_sorter import config; import tkinter
r = tkinter.Tk(); r.update(); r.destroy()
print('INSTALLED', config.INSTALLED); print('DATA', config.APP_DIR)
config.ensure_data_dir()" | tee /tmp/wine-py.txt
check "資料放在 %LOCALAPPDATA%\\AI Media Sorter" 'grep -q "AI Media Sorter" /tmp/wine-py.txt && [ -f "$DATA/.media-sorter-data" ]'

if [ -n "${WHEELS:-}" ]; then
  echo "== 用安裝好的 Python 實際開啟視窗程式跑一遍（假模型；WHEELS 資料夾裡放 pillow、numpy、av、send2trash 的 win_amd64 wheel）"
  for w in "$WHEELS"/*.whl; do python3 -c "import sys,zipfile; zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])" "$w" "$PROG/python/Lib/site-packages"; done
  cd "$PROG/app" && MEDIA_SORTER_TESTS="Z:$(realpath "$HERE/../tests" | tr / '\\')" T=100 W ../python/python.exe "Z:$(realpath "$HERE/wine_app_smoke.py" | tr / '\\')" | tee /tmp/wine-smoke.txt
  check "視窗程式在安裝版裡完整跑完（辨識→確認→複製，原檔還在）" 'grep -q SMOKE-OK /tmp/wine-smoke.txt'
fi

echo "== 準備使用者資料（設定、整理紀錄、復原移除裡的檔案）"
mkdir -p "$DATA/logs/復原移除/x"; echo '{}' > "$DATA/settings.json"; echo '{}' > "$DATA/categories.json"
echo l > "$DATA/logs/整理紀錄_20240101_000000.csv"; echo e > "$DATA/logs/error.log"; echo 你的檔案 > "$DATA/logs/復原移除/x/複本.jpg"
mkdir -p "$LOCAL/../../Documents/照片" && echo 原檔 > "$LOCAL/../../Documents/照片/IMG_1.jpg"

echo "== 程式開著時，解除安裝要拒絕"
rm -f /tmp/wine-locked.flag
( cd "$PROG/app" && NOWAIT=1 T=150 W ../python/python.exe -c "
from media_sorter.config import acquire_app_lock
import time; h = acquire_app_lock(); open('Z:/tmp/wine-locked.flag', 'w').write('x'); time.sleep(60)" ) &
HOLD=$!
for _ in $(seq 60); do [ -f /tmp/wine-locked.flag ] && break; sleep 1; done
# 管理工具的用法：uninstall.exe /S _?=安裝資料夾（_?= 後面的路徑不能加引號，所以經由批次檔呼叫）
printf '@"%s\\uninstall.exe" /S _?=%s\r\n@exit /b %%errorlevel%%\r\n' "$WINPROG" "$WINPROG" > /tmp/wine-uninstall.bat
NOWAIT=1 W cmd /c 'Z:\tmp\wine-uninstall.bat'; rc=$?; echo "  解除安裝結束代碼 $rc"
check "解除安裝：程式開著時拒絕（結束代碼 5）" '[ $rc -eq 5 ]'
check "解除安裝：程式開著時什麼都沒被刪" '[ -f "$PROG/python/python.exe" ] && [ -f "$PROG/app/media_sorter/app.py" ] && [ -f "$DATA/settings.json" ]'
NOWAIT=1 W "$SETUP" /S /SKIPDEPS; rc=$?; echo "  安裝／升級結束代碼 $rc"
check "程式開著時，升級安裝拒絕（結束代碼 5）" '[ $rc -eq 5 ]'
wait $HOLD; timeout 60 wineserver -w; sleep 1

echo "== 解除安裝（靜默，不加 _?=：跟使用者在「設定 → 應用程式」按解除安裝時一樣）：預設保留所有個人資料"
W "$PROG/uninstall.exe" /S; echo "  結束代碼 $?"
check "程式與 Python 已移除（含下載的套件）" '[ ! -d "$PROG/python" ] && [ ! -d "$PROG/app" ]'
check "開始功能表捷徑已移除" '[ "$(find "$START" -name "*.lnk" | wc -l)" -eq 0 ]'
check "登錄項目已移除" '! W reg query "$UNKEY" | grep -q "DisplayName"'
check "設定與整理紀錄都還在" '[ -f "$DATA/settings.json" ] && [ -f "$DATA/logs/整理紀錄_20240101_000000.csv" ]'
check "使用者的照片沒被動到" '[ -f "$LOCAL/../../Documents/照片/IMG_1.jpg" ]'
check "安裝資料夾本身也移除了（裡面沒有別的東西）" '[ ! -e "$PROG" ]'

echo "== 重新安裝，再解除安裝並勾選移除設定與整理紀錄"
W "$SETUP" /S /SKIPDEPS >/dev/null
W "$PROG/uninstall.exe" /S /REMOVE_SETTINGS /REMOVE_LOGS >/dev/null; echo "  結束代碼 $?"
check "設定已移除" '[ ! -f "$DATA/settings.json" ] && [ ! -f "$DATA/categories.json" ]'
check "整理紀錄已移除" '[ ! -f "$DATA/logs/整理紀錄_20240101_000000.csv" ] && [ ! -f "$DATA/logs/error.log" ]'
check "「復原移除」裡的檔案（使用者的）仍然保留" '[ -f "$DATA/logs/復原移除/x/複本.jpg" ]'
check "使用者的照片沒被動到" '[ -f "$LOCAL/../../Documents/照片/IMG_1.jpg" ]'
[ $fail -eq 0 ] && echo "全部通過" || echo "有項目失敗"
exit $fail
