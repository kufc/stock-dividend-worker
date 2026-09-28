"""對抗測試找到的檔案安全問題：每一項都有對應的回歸測試。"""

import errno
import os
import shutil
import threading
from datetime import datetime
from pathlib import Path

import pytest

from media_sorter import organizer
from media_sorter.organizer import execute, latest_log, plan_operations, read_log, undo

D = datetime(2024, 1, 1)


def touch(path: Path, text: str = "x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_copy_undo_restores_when_original_was_deleted(tmp_path):
    src = touch(tmp_path / "src" / "a.jpg", "ONLY")
    ops = plan_operations([(src, "貓", D)], tmp_path / "out")
    result = execute(ops, "copy", tmp_path / "logs")
    src.unlink()  # 使用者確認複本沒問題後刪掉原檔
    undone = undo(result.log_path)
    assert undone.done == 1 and not undone.errors
    assert src.read_text() == "ONLY"  # 唯一的一份被搬回原位，而不是被刪掉


def test_copy_undo_keeps_modified_copy(tmp_path):
    src = touch(tmp_path / "src" / "a.jpg", "A")
    ops = plan_operations([(src, "貓", D)], tmp_path / "out")
    result = execute(ops, "copy", tmp_path / "logs")
    ops[0].dst.write_text("EDITED BY USER")
    undone = undo(result.log_path)
    assert undone.done == 0 and len(undone.errors) == 1
    assert ops[0].dst.read_text() == "EDITED BY USER"


def test_failed_undo_keeps_log_for_retry(tmp_path):
    logs = tmp_path / "logs"
    old = touch(tmp_path / "src" / "old.jpg")
    first = execute(plan_operations([(old, "舊", D)], tmp_path / "out"), "move", logs)
    os.utime(first.log_path, (1, 1))  # 讓第一次的紀錄明顯比較舊
    a = touch(tmp_path / "src" / "a.jpg", "A")
    b = touch(tmp_path / "src" / "b.jpg", "B")
    second = execute(plan_operations([(a, "貓", D), (b, "貓", D)], tmp_path / "out"), "move", logs)
    touch(a, "NEW FILE WITH SAME NAME")  # 原位置出現同名檔 → 這一列復原會失敗

    undone = undo(second.log_path)
    assert undone.done == 1 and len(undone.errors) == 1
    assert latest_log(logs) == second.log_path  # 再按一次復原仍是這次的紀錄，不會去復原更早的整理
    assert len(read_log(second.log_path)) == 1  # 只留下失敗的那一列

    a.unlink()
    assert undo(second.log_path).done == 1
    assert a.read_text() == "A" and latest_log(logs) == first.log_path


def test_long_names_do_not_hang(tmp_path):
    stem = "x" * 120
    a = touch(tmp_path / "a" / f"{stem}.jpg")
    b = touch(tmp_path / "b" / f"{stem}.jpg")
    result: dict = {}
    worker = threading.Thread(target=lambda: result.update(ops=plan_operations(
        [(a, "貓" * 110, D), (b, "貓" * 110, D)], tmp_path / "out", pattern="{分類}_{原檔名}")))
    worker.start()
    worker.join(5)
    assert not worker.is_alive(), "plan_operations 卡住了"
    names = [op.dst.name for op in result["ops"]]
    assert len(set(names)) == 2 and all(len(Path(n).stem) <= organizer.MAX_NAME for n in names)
    ops = plan_operations([(a, "貓" * 150, D), (b, "貓" * 150, D)], tmp_path / "out")
    assert [op.dst.stem[-3:] for op in ops] == ["001", "002"]  # 序號一定完整


def test_categories_mapping_to_same_folder_share_numbering(tmp_path):
    files = [touch(tmp_path / f"{i}.jpg") for i in range(3)]
    ops = plan_operations([(files[0], "Cat", D), (files[1], "cat", D), (files[2], "旅行/日本", D)], tmp_path / "out")
    assert len({op.dst.as_posix().lower() for op in ops}) == 3
    assert len({op.dst.parent.name.lower() for op in ops[:2]}) == 1


def test_same_file_is_planned_once(tmp_path):
    a = touch(tmp_path / "a.jpg")
    assert len(plan_operations([(a, "貓", D), (a, "狗", D)], tmp_path / "out")) == 1


def test_two_runs_in_same_second_keep_separate_logs(tmp_path):
    logs = tmp_path / "logs"
    r1 = execute(plan_operations([(touch(tmp_path / "a.jpg"), "貓", D)], tmp_path / "out"), "move", logs)
    r2 = execute(plan_operations([(touch(tmp_path / "b.jpg"), "貓", D)], tmp_path / "out"), "move", logs)
    assert r1.log_path != r2.log_path and r1.log_path.exists() and r2.log_path.exists()


def test_undo_reads_log_resaved_as_big5(tmp_path):
    a = touch(tmp_path / "src" / "a.jpg", "A")
    result = execute(plan_operations([(a, "貓", D)], tmp_path / "out"), "move", tmp_path / "logs")
    text = result.log_path.read_text(encoding="utf-8-sig")
    result.log_path.write_bytes(text.encode("cp950"))  # Excel 另存成 ANSI
    assert undo(result.log_path).done == 1 and a.exists()


def test_failed_copy_leaves_no_partial_file(tmp_path, monkeypatch):
    a = touch(tmp_path / "a.jpg", "A" * 1000)

    def broken_copy(src, dst, *args, **kwargs):
        Path(dst).write_text("half")
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(shutil, "copyfile", broken_copy)
    ops = plan_operations([(a, "貓", D)], tmp_path / "out")
    result = execute(ops, "copy", tmp_path / "logs")
    assert result.done == 0 and result.errors
    assert not any((tmp_path / "out").rglob("*.*"))  # 沒有殘留截斷的檔案
    assert a.read_text() == "A" * 1000


def test_log_write_failure_aborts_and_keeps_log(tmp_path, monkeypatch):
    # 第 1 次 fsync 是第一個檔案的 pending 列（成功），第 2 次是它的確認列（失敗）：
    # 檔案已經搬移但紀錄不到，整批必須立刻停止，後面的檔案完全不能再動。
    files = [touch(tmp_path / f"{i}.jpg", str(i)) for i in range(3)]
    calls = {"n": 0}
    real_fsync = os.fsync

    def flaky_fsync(fd):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise OSError(errno.ENOSPC, "No space left on device")
        real_fsync(fd)

    monkeypatch.setattr(organizer.os, "fsync", flaky_fsync)
    result = execute(plan_operations([(f, "貓", D) for f in files], tmp_path / "out"), "move", tmp_path / "logs")
    assert result.aborted and result.log_path is not None and result.log_path.exists()
    assert result.done == 1
    assert sum(1 for f in files if f.exists()) == 2  # 第二、三個檔案完全沒被動到（只有第一個已預先記錄）


def test_cross_device_move_falls_back_to_copy(tmp_path, monkeypatch):
    a = touch(tmp_path / "a.jpg", "A")

    def exdev(*args, **kwargs):
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    monkeypatch.setattr(organizer, "_rename_no_clobber",
                        lambda s, d: exdev() if Path(s) == a else os.rename(s, d))
    ops = plan_operations([(a, "貓", D)], tmp_path / "out")
    result = execute(ops, "move", tmp_path / "logs")
    assert result.done == 1 and not a.exists() and ops[0].dst.read_text() == "A"


def test_cross_device_move_with_locked_original_logged_as_copy(tmp_path, monkeypatch):
    a = touch(tmp_path / "a.jpg", "A")
    monkeypatch.setattr(organizer, "_rename_no_clobber",
                        lambda s, d: (_ for _ in ()).throw(OSError(errno.EXDEV, "x")) if Path(s) == a
                        else os.rename(s, d))
    real_unlink = os.unlink
    monkeypatch.setattr(organizer.os, "unlink",
                        lambda p: (_ for _ in ()).throw(PermissionError("locked")) if Path(p) == a else real_unlink(p))
    result = execute(plan_operations([(a, "貓", D)], tmp_path / "out"), "move", tmp_path / "logs")
    assert result.done == 1 and result.warnings
    assert read_log(result.log_path)[0]["動作"] == "copy"  # 復原時會移除重複的複本


@pytest.mark.parametrize("pattern", ["{分類}_{序號}", "{原檔名}", "{分類}"])
def test_never_overwrites_existing_output(tmp_path, pattern):
    existing = touch(tmp_path / "out" / "貓" / "貓_001.jpg", "KEEP")
    touch(tmp_path / "out" / "貓" / "a.jpg", "KEEP")
    touch(tmp_path / "out" / "貓" / "貓.jpg", "KEEP")
    a = touch(tmp_path / "a.jpg", "NEW")
    result = execute(plan_operations([(a, "貓", D)], tmp_path / "out", pattern=pattern), "move", tmp_path / "logs")
    assert result.done == 1 and existing.read_text() == "KEEP"
    assert sum(1 for p in (tmp_path / "out" / "貓").iterdir() if p.read_text() == "KEEP") == 3
