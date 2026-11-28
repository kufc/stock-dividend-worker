"""在真的 Windows 上驗證「資源回收筒被關掉時」各層保護的實際行為。**會暫時改登錄檔**（結束時改回來），
所以要明確同意才執行：

    set MEDIA_SORTER_ALLOW_REGISTRY_TEST=1
    python tools/verify_recycle_refusal_windows.py

做的事：把暫存檔所在磁碟的回收筒設成「不要將檔案移到資源回收筒」（目前使用者的 NukeOnDelete=1），然後各在一個
新的行程裡量三件事，最後把設定改回原樣：
 1. organizer.recycle_bin_problem 有沒有看出來（程式的第一道保護；它應該回傳原因）
 2. 新介面（IFileOperation＋「不能進回收筒就失敗」）：應該失敗、檔案留在原地
 3. 舊介面（send2trash）：預期會永久刪除——這是為什麼需要前兩道保護。只是記錄，不算通過或失敗
結束碼 0＝第 1、2 項都符合預期。
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import tempfile
import winreg
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CHILD = r"""
import sys
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from media_sorter import organizer, recycle
what, target = sys.argv[2], sys.argv[3]
if what == "problem":
    print("RESULT", organizer.recycle_bin_problem(Path(target)))
elif what == "modern":
    try:
        recycle.recycle_modern(target)
        print("RESULT no-error")
    except recycle.RecycleUnsupported as exc:
        print("RESULT unsupported", exc)
    except OSError as exc:
        print("RESULT refused", exc)
elif what == "legacy":
    try:
        organizer._send2trash(target)
        print("RESULT no-error")
    except Exception as exc:
        print("RESULT error", type(exc).__name__, exc)
"""


class _BinInfo(ctypes.Structure):
    _pack_ = 8
    _fields_ = [("cbSize", ctypes.c_ulong), ("i64Size", ctypes.c_longlong), ("i64NumItems", ctypes.c_longlong)]


def bin_items(root: str) -> int:
    info = _BinInfo()
    info.cbSize = ctypes.sizeof(info)
    ctypes.windll.shell32.SHQueryRecycleBinW(root, ctypes.byref(info))
    return int(info.i64NumItems)


def volume_key(root: str) -> str:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetVolumeNameForVolumeMountPointW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    buffer = ctypes.create_unicode_buffer(64)
    if not kernel32.GetVolumeNameForVolumeMountPointW(root, buffer, len(buffer)):
        raise OSError("找不到磁碟的代號")
    guid = buffer.value[buffer.value.find("{"):buffer.value.find("}") + 1]
    return r"Software\Microsoft\Windows\CurrentVersion\Explorer\BitBucket\Volume" + "\\" + guid


def child(what: str, target: Path) -> str:
    out = subprocess.run([sys.executable, "-c", CHILD, str(ROOT), what, str(target)], capture_output=True, text=True,
                         encoding="utf-8", timeout=120, env={**os.environ, "PYTHONUTF8": "1"})
    lines = [line for line in out.stdout.splitlines() if line.startswith("RESULT")]
    return lines[-1][7:] if lines else f"（沒有結果）{out.stdout[-300:]} {out.stderr[-300:]}"


def main() -> int:
    if sys.platform != "win32":
        print("只能在 Windows 上執行")
        return 2
    if os.environ.get("MEDIA_SORTER_ALLOW_REGISTRY_TEST") != "1":
        print("這個檢查會暫時修改登錄檔；設定 MEDIA_SORTER_ALLOW_REGISTRY_TEST=1 才執行")
        return 2
    folder = Path(tempfile.mkdtemp(prefix="ams-refusal-"))
    root = folder.anchor
    key = volume_key(root)
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_READ | winreg.KEY_WRITE) as handle:
        try:
            previous = winreg.QueryValueEx(handle, "NukeOnDelete")
        except OSError:
            previous = None
        winreg.SetValueEx(handle, "NukeOnDelete", 0, winreg.REG_DWORD, 1)
    ok = True
    try:
        files = {}
        for name in ("problem", "modern", "legacy"):
            files[name] = folder / f"{name}.jpg"
            files[name].write_bytes(b"photo" * 1000)
        problem = child("problem", files["problem"])
        print(f"1. recycle_bin_problem：{problem}")
        ok &= problem != "None"
        before = bin_items(root)
        modern = child("modern", files["modern"])
        print(f"2. 新介面：{modern}；檔案還在：{files['modern'].exists()}；回收筒項目數 {before} → {bin_items(root)}")
        ok &= modern.startswith("refused") and files["modern"].exists()
        before = bin_items(root)
        legacy = child("legacy", files["legacy"])
        gone = not files["legacy"].exists()
        print(f"3. 舊介面：{legacy}；檔案還在：{not gone}；回收筒項目數 {before} → {bin_items(root)}"
              + ("　← 永久刪除了" if gone and bin_items(root) == before else ""))
    finally:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_WRITE) as handle:
            if previous is None:
                winreg.DeleteValue(handle, "NukeOnDelete")
            else:
                winreg.SetValueEx(handle, "NukeOnDelete", 0, previous[1], previous[0])
        print("登錄檔已改回原樣")
    print("符合預期" if ok else "不符合預期")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
