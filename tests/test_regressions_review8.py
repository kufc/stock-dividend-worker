"""第八輪外部審查：禁止寫入的保護必須涵蓋「比對 → 送出」整段，而且兩份檔案都要保護。"""

import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

import pytest

from media_sorter import organizer
from media_sorter.organizer import execute, plan_operations, remove_originals, undo

D = datetime(2024, 1, 1)
windows_only = pytest.mark.skipif(sys.platform != "win32", reason="只在 Windows 上有強制的共用模式")


def touch(path: Path, text: str = "x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def organize(tmp_path, files):
    ops = plan_operations([(f, "貓", D) for f in files], tmp_path / "out")
    return ops, execute(ops, tmp_path / "logs", source_dir=tmp_path / "src", output_dir=tmp_path / "out")


@pytest.fixture
def guards(monkeypatch):
    """記錄每一個「禁止寫入」的控制代碼：(路徑, 檔案物件)。"""
    opened = []
    real = organizer._open_deny_write

    def spy(path):
        handle = real(path)
        opened.append((Path(path), handle))
        return handle

    monkeypatch.setattr(organizer, "_open_deny_write", spy)
    return opened


def make_trash(tmp_path, monkeypatch, during=None):
    bin_dir = tmp_path / "RECYCLE"
    bin_dir.mkdir(exist_ok=True)

    def fake_send2trash(path):
        if during:
            during(Path(path))
        shutil.move(path, bin_dir / Path(path).name)

    monkeypatch.setattr(organizer, "_send2trash", fake_send2trash)
    return bin_dir


# ---------------------------------------------------------------------------- 1＋2：保護涵蓋送出的那一刻，而且兩份都保護
def test_both_files_are_guarded_while_sending_to_trash(tmp_path, monkeypatch, guards):
    a = touch(tmp_path / "src" / "a.jpg", "A")
    ops, result = organize(tmp_path, [a])
    seen = {}

    def during(staged):
        seen["open"] = {p.name: not h.closed for p, h in guards}

    make_trash(tmp_path, monkeypatch, during)
    assert remove_originals(result.log_path).done == 1
    # 送進回收筒的那一刻：要移除的那份（已改名）和留下的複本，都還握著禁止寫入的控制代碼
    assert seen["open"] == {"a (整理前).jpg": True, ops[0].dst.name: True}
    assert all(h.closed for _, h in guards)  # 結束後都有關閉


def test_undo_guards_both_files_while_sending_to_trash(tmp_path, monkeypatch, guards):
    a = touch(tmp_path / "src" / "a.jpg", "A")
    ops, result = organize(tmp_path, [a])
    seen = {}
    make_trash(tmp_path, monkeypatch, lambda staged: seen.setdefault("open", [not h.closed for _, h in guards]))
    assert undo(result.log_path).done == 1
    assert seen["open"] == [True, True]


def test_reference_in_use_keeps_file(tmp_path, monkeypatch):
    """留下的那一份正被其他程式開著寫入（開不了禁止寫入的控制代碼）：不移除。"""
    a = touch(tmp_path / "src" / "a.jpg", "A")
    ops, result = organize(tmp_path, [a])
    bin_dir = make_trash(tmp_path, monkeypatch)
    real = organizer._open_deny_write

    def busy_reference(path):
        if Path(path) == ops[0].dst:
            raise PermissionError(32, "檔案正由另一個程序使用")
        return real(path)

    monkeypatch.setattr(organizer, "_open_deny_write", busy_reference)
    removed = remove_originals(result.log_path)
    assert removed.done == 0 and removed.errors and a.read_text() == "A" and not list(bin_dir.iterdir())


def test_trash_failure_while_guarded_restores_name(tmp_path, monkeypatch):
    a = touch(tmp_path / "src" / "a.jpg", "A")
    _, result = organize(tmp_path, [a])

    def refuse(path):
        raise PermissionError(32, "資源回收筒無法搬動使用中的檔案")

    monkeypatch.setattr(organizer, "_send2trash", refuse)
    removed = remove_originals(result.log_path)
    assert removed.done == 0 and removed.errors and a.read_text() == "A"  # 失敗就改回原名，什麼都不移除


# ---------------------------------------------------------------------------- Windows：真實的共用模式
@windows_only
def test_windows_nobody_can_write_either_file_while_sending(tmp_path, monkeypatch):
    """重現審查的兩個情境：送出前重新開啟改名後的檔案寫入、修改留下的複本 —— 兩者都必須被拒絕。"""
    a = touch(tmp_path / "src" / "a.jpg", "A")
    ops, result = organize(tmp_path, [a])
    attempts = {}

    def during(staged):
        for label, target in (("staged", staged), ("copy", ops[0].dst)):
            try:
                with open(target, "ab") as f:
                    f.write(b"NEWER")
                attempts[label] = "寫入成功"
            except PermissionError:
                attempts[label] = "被拒絕"

    bin_dir = make_trash(tmp_path, monkeypatch, during)
    removed = remove_originals(result.log_path)
    assert attempts == {"staged": "被拒絕", "copy": "被拒絕"}
    assert removed.done == 1 and ops[0].dst.read_text() == "A"
    assert [p.read_text() for p in bin_dir.iterdir()] == ["A"]  # 回收筒裡的內容 = 留下的內容


@windows_only
@pytest.mark.skipif(os.environ.get("MEDIA_SORTER_TEST_REAL_TRASH") != "1",
                    reason="會放一個小測試檔到真正的資源回收筒；設定 MEDIA_SORTER_TEST_REAL_TRASH=1 才執行")
def test_windows_real_recycle_bin_accepts_guarded_file(tmp_path):
    """確認真正的資源回收筒，能在我們握著禁止寫入控制代碼時搬走檔案。"""
    if organizer._send2trash is None:
        pytest.skip("沒有安裝 send2trash")
    a = touch(tmp_path / "src" / "ai-media-sorter-測試檔-可刪除.jpg", "A")
    ops, result = organize(tmp_path, [a])
    removed = remove_originals(result.log_path)
    assert removed.done == 1 and not removed.errors, removed.errors
    assert not a.exists() and ops[0].dst.read_text() == "A"
