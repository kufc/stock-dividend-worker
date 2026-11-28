"""把檔案送進 Windows 資源回收筒的第二種做法：IFileOperation 加上「不能進回收筒就失敗」的旗標。

為什麼需要：send2trash（沒有 pywin32 時）用舊的 SHFileOperationW。回收筒被關掉、容量不夠、路徑太長時，
舊介面會**不問就永久刪除**；organizer.recycle_bin_problem 事先檢查這些情況，但那是我們自己列舉 Windows 的規則，
可能漏。新介面可以在刪除前問 Windows「這個檔案會不會進回收筒」：

- 光靠 FOFX_RECYCLEONDELETE 這個旗標**不夠**。在真的 Windows（GitHub 的 windows-latest）上實測：回收筒被設成
  「不要將檔案移到資源回收筒」時，帶著這個旗標的刪除照樣成功，檔案被永久刪除。
- 所以另外掛一個進度接收器（IFileOperationProgressSink）：Windows 在刪每個檔案之前會呼叫它的 PreDeleteItem，
  參數裡的 TSF_DELETE_RECYCLE_IF_POSSIBLE 表示「這個檔案會進回收筒」。沒有這個旗標就回傳中止，檔案留在原地。

**預設關閉**（MODERN_DEFAULT）。開啟與否看 tools/verify_recycle_windows.py 與
tools/verify_recycle_refusal_windows.py 在真的 Windows 上的結果（docs/release-checklist.md）。
Wine 的 IFileOperation 是空殼（每個方法都回 E_NOTIMPL），在那裡只驗得到「介面不能用時退回原本的做法」。
可以用環境變數 MEDIA_SORTER_MODERN_RECYCLE=1／0 個別開關。
"""

from __future__ import annotations

import os
import sys
import uuid
from typing import Callable

MODERN_ENV = "MEDIA_SORTER_MODERN_RECYCLE"
MODERN_DEFAULT = False

FOF_SILENT, FOF_NOCONFIRMATION, FOF_ALLOWUNDO, FOF_NOERRORUI = 0x0004, 0x0010, 0x0040, 0x0400
FOFX_RECYCLEONDELETE, FOFX_EARLYFAILURE = 0x00080000, 0x00100000
FLAGS = FOF_SILENT | FOF_NOCONFIRMATION | FOF_ALLOWUNDO | FOF_NOERRORUI | FOFX_RECYCLEONDELETE | FOFX_EARLYFAILURE
CLSID_FILE_OPERATION = "3ad05575-8857-4850-9277-11b85bdb8e09"
IID_FILE_OPERATION = "947aab5f-0a5c-4c13-b4d6-4bf7836fc9f8"
IID_SHELL_ITEM = "43826d1e-e718-42ee-bc55-a1e261c37bfe"
IID_PROGRESS_SINK = "04b0f1a7-9490-44bc-96e1-4296a31252e2"
IID_UNKNOWN = "00000000-0000-0000-c000-000000000046"
TSF_DELETE_RECYCLE_IF_POSSIBLE = 0x80
E_ABORT, E_NOINTERFACE = 0x80004004, 0x80004002
# IFileOperation 的方法在虛擬函式表裡的位置（前三個是 IUnknown）
VT_RELEASE, VT_SET_FLAGS, VT_DELETE_ITEM, VT_PERFORM, VT_ANY_ABORTED = 2, 5, 18, 21, 22
STAGE_SETUP, STAGE_OPERATION = "setup", "operation"


class RecycleUnsupported(Exception):
    """這台電腦不能用新介面，而且**還沒有對檔案做任何事**：可以安全地退回原本的做法。"""


def modern_enabled() -> bool:
    value = os.environ.get(MODERN_ENV)
    if value is not None:
        return value.strip() not in ("", "0", "false", "False")
    return MODERN_DEFAULT


def _make_sink(ctypes, seen: dict):
    """用 ctypes 做一個 IFileOperationProgressSink。只有 PreDeleteItem／PostDeleteItem 有作用，其餘回傳成功。
    回傳 (物件指標, 要保持存活的東西)。只支援 64 位元（單一呼叫慣例，參數個數不影響堆疊）；程式本來就只支援 64 位元。"""
    def hresult(value: int) -> int:
        return ctypes.c_long(value).value

    pointer = ctypes.c_void_p
    wanted = {uuid.UUID(IID_PROGRESS_SINK).bytes_le, uuid.UUID(IID_UNKNOWN).bytes_le}

    def query_interface(this, riid, out):
        if ctypes.string_at(riid, 16) in wanted:
            out[0] = this
            return 0
        out[0] = None
        return hresult(E_NOINTERFACE)

    def pre_delete(this, flags, item):
        seen["will_recycle"] = bool(flags & TSF_DELETE_RECYCLE_IF_POSSIBLE)
        return 0 if seen["will_recycle"] else hresult(E_ABORT)   # 不會進回收筒：中止，檔案留在原地

    def post_delete(this, flags, item, result, new_item):
        seen["post_result"], seen["in_bin"] = result & 0xFFFFFFFF, bool(new_item)
        return 0

    ok0 = lambda this: 0                                                                      # noqa: E731
    f = ctypes.WINFUNCTYPE
    long_, dword = ctypes.c_long, ctypes.c_ulong
    methods = [
        f(long_, pointer, pointer, ctypes.POINTER(pointer))(query_interface),
        f(dword, pointer)(lambda this: 2),                                                    # AddRef
        f(dword, pointer)(lambda this: 1),                                                    # Release
        f(long_, pointer)(ok0),                                                               # StartOperations
        f(long_, pointer, long_)(lambda this, hr: 0),                                         # FinishOperations
        f(long_, pointer, dword, pointer, pointer)(lambda *a: 0),                             # PreRenameItem
        f(long_, pointer, dword, pointer, pointer, long_, pointer)(lambda *a: 0),             # PostRenameItem
        f(long_, pointer, dword, pointer, pointer, pointer)(lambda *a: 0),                    # PreMoveItem
        f(long_, pointer, dword, pointer, pointer, pointer, long_, pointer)(lambda *a: 0),    # PostMoveItem
        f(long_, pointer, dword, pointer, pointer, pointer)(lambda *a: 0),                    # PreCopyItem
        f(long_, pointer, dword, pointer, pointer, pointer, long_, pointer)(lambda *a: 0),    # PostCopyItem
        f(long_, pointer, dword, pointer)(pre_delete),                                        # PreDeleteItem
        f(long_, pointer, dword, pointer, long_, pointer)(post_delete),                       # PostDeleteItem
        f(long_, pointer, dword, pointer, pointer)(lambda *a: 0),                             # PreNewItem
        f(long_, pointer, dword, pointer, pointer, pointer, dword, long_, pointer)(lambda *a: 0),  # PostNewItem
        f(long_, pointer, ctypes.c_uint, ctypes.c_uint)(lambda *a: 0),                        # UpdateProgress
        f(long_, pointer)(ok0), f(long_, pointer)(ok0), f(long_, pointer)(ok0),               # Reset／Pause／ResumeTimer
    ]
    table = (pointer * len(methods))(*[ctypes.cast(m, pointer) for m in methods])
    obj = pointer(ctypes.addressof(table))        # 物件的第一個欄位就是虛擬函式表的位址
    return ctypes.addressof(obj), (methods, table, obj)


def _shell_delete(path: str) -> tuple:
    """呼叫 IFileOperation。回傳 (做到哪個階段, HRESULT, 使用者或系統是否中止, 接收器看到的事)。
    階段是 STAGE_SETUP 時，檔案保證還沒被碰過。只在 Windows 上能呼叫。"""
    import ctypes  # noqa: PLC0415
    from ctypes import wintypes  # noqa: PLC0415

    seen: dict = {}

    def guid(text: str):
        return (ctypes.c_byte * 16).from_buffer_copy(uuid.UUID(text).bytes_le)

    ole32, shell32 = ctypes.WinDLL("ole32"), ctypes.WinDLL("shell32")
    ole32.CoInitializeEx.restype = ctypes.c_long
    ole32.CoInitializeEx.argtypes = (ctypes.c_void_p, wintypes.DWORD)
    ole32.CoCreateInstance.restype = ctypes.c_long
    ole32.CoCreateInstance.argtypes = (ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p,
                                       ctypes.POINTER(ctypes.c_void_p))
    shell32.SHCreateItemFromParsingName.restype = ctypes.c_long
    shell32.SHCreateItemFromParsingName.argtypes = (wintypes.LPCWSTR, ctypes.c_void_p, ctypes.c_void_p,
                                                    ctypes.POINTER(ctypes.c_void_p))

    def method(obj: ctypes.c_void_p, index: int, *argtypes):
        table = ctypes.cast(ctypes.cast(obj, ctypes.POINTER(ctypes.c_void_p))[0], ctypes.POINTER(ctypes.c_void_p))
        function = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)(table[index])
        return lambda *args: function(obj, *args) & 0xFFFFFFFF

    initialized = (ole32.CoInitializeEx(None, 2) & 0xFFFFFFFF) in (0, 1)  # 這個執行緒已經用別的模式初始化過也可以繼續
    operation, item = ctypes.c_void_p(), ctypes.c_void_p()
    try:
        hr = ole32.CoCreateInstance(guid(CLSID_FILE_OPERATION), None, 1, guid(IID_FILE_OPERATION),
                                    ctypes.byref(operation)) & 0xFFFFFFFF
        if hr or not operation:
            return STAGE_SETUP, hr or 0x80004005, False, seen
        hr = method(operation, VT_SET_FLAGS, wintypes.DWORD)(FLAGS)
        if hr:
            return STAGE_SETUP, hr, False, seen
        hr = shell32.SHCreateItemFromParsingName(path, None, guid(IID_SHELL_ITEM), ctypes.byref(item)) & 0xFFFFFFFF
        if hr or not item:
            return STAGE_SETUP, hr or 0x80004005, False, seen
        sink, keep_alive = _make_sink(ctypes, seen)
        hr = method(operation, VT_DELETE_ITEM, ctypes.c_void_p, ctypes.c_void_p)(item, sink)
        if hr:
            return STAGE_SETUP, hr, False, seen
        hr = method(operation, VT_PERFORM)()
        aborted = wintypes.BOOL(0)
        method(operation, VT_ANY_ABORTED, ctypes.POINTER(wintypes.BOOL))(ctypes.byref(aborted))
        del keep_alive
        return STAGE_OPERATION, hr, bool(aborted.value), seen
    finally:
        for obj in (item, operation):
            if obj:
                method(obj, VT_RELEASE)()
        if initialized:
            ole32.CoUninitialize()


def recycle_modern(path: str, shell_delete: Callable[[str], tuple] | None = None) -> None:
    """用新介面把 path 送進回收筒。成功就回傳；介面不能用（檔案還沒被碰）丟 RecycleUnsupported；
    Windows 說進不了回收筒、被中止、或做完檔案還在，丟 OSError——這時**不可以**退回舊介面，因為舊介面會永久刪除。"""
    if shell_delete is None:
        if sys.platform != "win32":
            raise RecycleUnsupported("不是 Windows")
        shell_delete = _shell_delete
    try:
        stage, hr, aborted, *rest = shell_delete(os.path.abspath(path))
    except (OSError, AttributeError, ValueError) as exc:  # 載不到系統函式庫、函式不存在：什麼都還沒做
        raise RecycleUnsupported(str(exc)) from exc
    seen = rest[0] if rest else {"will_recycle": True}
    if stage == STAGE_SETUP:
        raise RecycleUnsupported(f"IFileOperation 無法使用（0x{hr:08X}）")
    if seen.get("will_recycle") is False:
        raise OSError("Windows 表示這個檔案不會進資源回收筒（會被永久刪除），所以沒有移除"
                      + ("" if os.path.lexists(path) else "；但檔案已經不在原地"))
    if "will_recycle" not in seen:   # Windows 沒有先問我們就做了：無法確認去向
        raise OSError("無法確認 Windows 是否把檔案送進資源回收筒"
                      + ("，檔案還在原地" if os.path.lexists(path) else "，而且檔案已經不在原地"))
    if hr or aborted:
        raise OSError(f"Windows 無法把這個檔案送進資源回收筒（0x{hr:08X}{'，操作被中止' if aborted else ''}），檔案沒有被移除")
    if os.path.lexists(path):
        raise OSError("Windows 回報已送進資源回收筒，但檔案還在原地；沒有再做任何處理")


def to_recycle_bin(path: str, legacy: Callable[[str], object]) -> None:
    """送進回收筒：開啟新介面時先用新的，新介面不能用才用 legacy（send2trash）。新介面說「進不了回收筒」時不退回。"""
    if modern_enabled():
        try:
            recycle_modern(path)
            return
        except RecycleUnsupported:
            pass
    legacy(path)
