"""在真的 Windows 上驗證新式回收筒介面（media_sorter/recycle.py）真的把檔案送進資源回收筒。

    python tools/verify_recycle_windows.py

做的事：建一個暫存檔 → 用 IFileOperation 送進回收筒 → 確認檔案不在原地、而且回收筒裡的項目數多了一個。
介面不能用（例如在 Wine 裡）會以結束碼 2 結束並說明，不算通過。只在 Windows 上有意義。
"""

from __future__ import annotations

import ctypes
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from media_sorter import recycle  # noqa: E402


class _BinInfo(ctypes.Structure):
    _pack_ = 8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 1
    _fields_ = [("cbSize", ctypes.c_ulong), ("i64Size", ctypes.c_longlong), ("i64NumItems", ctypes.c_longlong)]


def bin_items(root: str) -> int:
    info = _BinInfo()
    info.cbSize = ctypes.sizeof(info)
    result = ctypes.windll.shell32.SHQueryRecycleBinW(root, ctypes.byref(info))
    if result != 0:
        raise OSError(f"SHQueryRecycleBinW 失敗：0x{result & 0xFFFFFFFF:08X}")
    return int(info.i64NumItems)


def main() -> int:
    if sys.platform != "win32":
        print("只能在 Windows 上執行")
        return 2
    folder = Path(tempfile.mkdtemp(prefix="ams-recycle-"))
    victim = folder / "recycle-me.jpg"
    victim.write_bytes(b"photo" * 1000)
    root = victim.anchor
    before = bin_items(root)
    try:
        recycle.recycle_modern(str(victim))
    except recycle.RecycleUnsupported as exc:
        print(f"IFileOperation 不能用：{exc}（檔案還在：{victim.exists()}）")
        return 2
    after = bin_items(root)
    print(f"檔案還在原地：{victim.exists()}；{root} 的回收筒項目數 {before} → {after}")
    if victim.exists() or after != before + 1:
        print("沒有通過")
        return 1
    print("通過：檔案進了資源回收筒")
    return 0


if __name__ == "__main__":
    sys.exit(main())
