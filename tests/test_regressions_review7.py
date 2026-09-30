"""第七輪外部審查找到的問題的回歸測試。

其中「已開啟的寫入控制代碼」要靠 Windows 的共用模式才能真正防住，
所以另外附了只在 Windows 上執行的測試（用真實的 CreateFileW 握住可寫入的控制代碼）。
"""

import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

import pytest

from media_sorter import organizer
from media_sorter.organizer import execute, latest_log, plan_operations, remove_originals, undo

D = datetime(2024, 1, 1)


def touch(path: Path, text: str = "x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


@pytest.fixture
def trash(tmp_path, monkeypatch):
    bin_dir = tmp_path / "RECYCLE"
    bin_dir.mkdir()

    def fake_send2trash(path):
        shutil.move(path, bin_dir / Path(path).name)

    monkeypatch.setattr(organizer, "_send2trash", fake_send2trash)
    return bin_dir


def organize(tmp_path, files):
    ops = plan_operations([(f, "貓", D) for f in files], tmp_path / "out")
    return ops, execute(ops, tmp_path / "logs", source_dir=tmp_path / "src", output_dir=tmp_path / "out")


# ---------------------------------------------------------------------------- 1. 已開啟的寫入控制代碼
def test_writer_detected_by_deny_write_open_keeps_file(tmp_path, trash, monkeypatch):
    """第二次比對的開檔被拒（Windows 上代表有程式握著可寫入的控制代碼）：檔案改回原名、不送回收筒。"""
    a = touch(tmp_path / "src" / "a.jpg", "A")
    _, result = organize(tmp_path, [a])

    def sharing_violation(path):
        raise PermissionError(32, "檔案正由另一個程序使用")

    monkeypatch.setattr(organizer, "_open_deny_write", sharing_violation)
    removed = remove_originals(result.log_path)

    assert removed.done == 0 and "正被其他程式開啟寫入" in removed.errors[0][1]
    assert a.read_text() == "A" and not list(trash.iterdir())
    assert latest_log(tmp_path / "logs") == result.log_path  # 留著，關掉那個程式後可以重試


def test_final_compare_reads_through_the_guarded_handle(tmp_path, trash, monkeypatch):
    """第二次比對必須讀「禁止寫入」的那個控制代碼，而不是另外開一次檔。"""
    a = touch(tmp_path / "src" / "a.jpg", "A")
    _, result = organize(tmp_path, [a])
    opened = []
    real = organizer._open_deny_write

    def spy(path):
        handle = real(path)
        opened.append(handle)
        return handle

    monkeypatch.setattr(organizer, "_open_deny_write", spy)
    assert remove_originals(result.log_path).done == 1
    assert opened and all(h.closed for h in opened)  # 用過，而且有關閉（不會洩漏控制代碼）


def test_deny_write_handle_is_closed_even_when_sizes_differ(tmp_path, monkeypatch):
    a = touch(tmp_path / "a.jpg", "AAAA")
    b = touch(tmp_path / "b.jpg", "BB")
    handle = organizer._open_deny_write(a)
    assert organizer._same_content(a, b, reader_a=handle) is False
    handle.close()
    assert handle.closed


# -- 只在 Windows 執行：用真實的控制代碼重現審查描述的情境
windows_only = pytest.mark.skipif(sys.platform != "win32", reason="只在 Windows 上有強制的共用模式")


def _open_writer_sharing_everything(path: Path):
    """模擬照片編輯器：以 GENERIC_WRITE 開啟，並允許其他人讀取、寫入、刪除／改名。"""
    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create_file.restype = wintypes.HANDLE
    generic_write, share_all, open_existing = 0x40000000, 0x1 | 0x2 | 0x4, 3
    handle = create_file(str(path), generic_write, share_all, None, open_existing, 0x80, None)
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    return os.fdopen(msvcrt.open_osfhandle(handle, os.O_WRONLY), "wb")


@windows_only
def test_windows_open_writer_handle_blocks_removal(tmp_path, trash):
    a = touch(tmp_path / "src" / "a.jpg", "A")
    ops, result = organize(tmp_path, [a])
    writer = _open_writer_sharing_everything(a)
    try:
        removed = remove_originals(result.log_path)
        assert removed.done == 0 and removed.errors
        writer.write(b"NEWER")  # 移除嘗試之後才寫入
        writer.flush()
    finally:
        writer.close()
    assert a.read_bytes().startswith(b"NEWER")  # 新內容在原位、原檔名
    assert not list(trash.iterdir()) and ops[0].dst.read_text() == "A"


@windows_only
def test_windows_reader_does_not_block_deny_write_open(tmp_path):
    a = touch(tmp_path / "a.jpg", "A")
    with open(a, "rb"):  # 一般的唯讀開啟（例如看圖程式）
        with organizer._open_deny_write(a) as guard:
            assert guard.read() == b"A"


@windows_only
def test_windows_deny_write_blocks_new_writers(tmp_path):
    a = touch(tmp_path / "a.jpg", "A")
    with organizer._open_deny_write(a):
        with pytest.raises(PermissionError):
            open(a, "ab")


# ---------------------------------------------------------------------------- 4. 別名輔助函式在 Windows 不能被略過
@windows_only
def test_windows_case_alias_is_available_without_symlink_rights(tmp_path):
    from test_copy_first_safety import alias_of

    a = touch(tmp_path / "src" / "a.jpg", "A")
    alias = alias_of(a)
    assert alias is not None and str(alias) != str(a) and alias.parent == a.parent  # 大小寫別名，不是符號連結


def test_undo_also_uses_guarded_compare(tmp_path, trash, monkeypatch):
    a = touch(tmp_path / "src" / "a.jpg", "A")
    ops, result = organize(tmp_path, [a])
    monkeypatch.setattr(organizer, "_open_deny_write", lambda p: (_ for _ in ()).throw(PermissionError(32, "使用中")))
    undone = undo(result.log_path)
    assert undone.done == 0 and undone.errors and ops[0].dst.read_text() == "A"
