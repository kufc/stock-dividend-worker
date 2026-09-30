"""對抗測試找到的檔案安全問題：每一項都有對應的回歸測試。"""

import errno
import shutil
import threading
from datetime import datetime
from pathlib import Path

import pytest

from media_sorter import organizer
from media_sorter.organizer import execute, plan_operations

D = datetime(2024, 1, 1)


def touch(path: Path, text: str = "x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


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
    r1 = execute(plan_operations([(touch(tmp_path / "a.jpg"), "貓", D)], tmp_path / "out"), logs)
    r2 = execute(plan_operations([(touch(tmp_path / "b.jpg"), "貓", D)], tmp_path / "out"), logs)
    assert r1.log_path != r2.log_path and r1.log_path.exists() and r2.log_path.exists()


def test_failed_copy_leaves_no_partial_file(tmp_path, monkeypatch):
    a = touch(tmp_path / "a.jpg", "A" * 1000)

    def broken_copy(src, dst, *args, **kwargs):
        Path(dst).write_text("half")
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(shutil, "copyfile", broken_copy)
    ops = plan_operations([(a, "貓", D)], tmp_path / "out")
    result = execute(ops, tmp_path / "logs")
    assert result.done == 0 and result.errors
    assert not any((tmp_path / "out").rglob("*.*"))  # 沒有殘留截斷的檔案
    assert a.read_text() == "A" * 1000


@pytest.mark.parametrize("pattern", ["{分類}_{序號}", "{原檔名}", "{分類}"])
def test_never_overwrites_existing_output(tmp_path, pattern):
    existing = touch(tmp_path / "out" / "貓" / "貓_001.jpg", "KEEP")
    touch(tmp_path / "out" / "貓" / "a.jpg", "KEEP")
    touch(tmp_path / "out" / "貓" / "貓.jpg", "KEEP")
    a = touch(tmp_path / "a.jpg", "NEW")
    result = execute(plan_operations([(a, "貓", D)], tmp_path / "out", pattern=pattern), tmp_path / "logs")
    assert result.done == 1 and existing.read_text() == "KEEP"
    assert sum(1 for p in (tmp_path / "out" / "貓").iterdir() if p.read_text() == "KEEP") == 3
