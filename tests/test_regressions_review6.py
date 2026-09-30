"""第六輪外部審查找到的 3 個問題的回歸測試。"""

import csv
import io
import os
import shutil
from datetime import datetime
from pathlib import Path

import pytest

from media_sorter import organizer
from media_sorter.organizer import execute, latest_log, plan_operations, read_log, remove_originals, undo

D = datetime(2024, 1, 1)


def touch(path: Path, text: str = "x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


@pytest.fixture
def trash(tmp_path, monkeypatch):
    bin_dir = tmp_path / "RECYCLE"
    bin_dir.mkdir()
    counter = {"n": 0}

    def fake_send2trash(path):
        counter["n"] += 1
        shutil.move(path, bin_dir / f"{counter['n']}_{Path(path).name}")

    monkeypatch.setattr(organizer, "_send2trash", fake_send2trash)
    return bin_dir


def organize(tmp_path, files, category="貓"):
    ops = plan_operations([(f, category, D) for f in files], tmp_path / "out")
    return ops, execute(ops, tmp_path / "logs", source_dir=tmp_path / "src", output_dir=tmp_path / "out")


# ---------------------------------------------------------------------------- 1. 比對後、移除前，原檔被修改
def test_original_modified_after_compare_is_not_removed(tmp_path, trash, monkeypatch):
    a = touch(tmp_path / "src" / "a.jpg", "A")
    ops, result = organize(tmp_path, [a])
    real = organizer._same_content

    def compare_then_modify(x, y, **kwargs):
        same = real(x, y, **kwargs)
        if same and Path(x) == a:  # 比對「通過」的那一刻，另一個程式改寫了原檔
            a.write_text("NEWER")
        return same

    monkeypatch.setattr(organizer, "_same_content", compare_then_modify)
    removed = remove_originals(result.log_path)

    assert removed.done == 0 and removed.errors
    assert a.read_text() == "NEWER"  # 最新內容仍在原位、原檔名
    assert not list(trash.iterdir())  # 什麼都沒進資源回收筒


def test_original_rewritten_at_last_moment_keeps_newer_content(tmp_path, trash, monkeypatch):
    """改名之後才有人照原路徑存檔：那會產生一個新檔，我們手上這份（舊內容 = 複本）才進回收筒。"""
    a = touch(tmp_path / "src" / "a.jpg", "A")
    ops, result = organize(tmp_path, [a])
    real = organizer._same_content

    def compare_then_modify(x, y, **kwargs):
        same = real(x, y, **kwargs)
        if same and f"({organizer.STAGE_MARK_ORIGINAL})" in Path(x).name:  # 改名後那次比對通過的那一刻
            a.write_text("NEWER")
        return same

    monkeypatch.setattr(organizer, "_same_content", compare_then_modify)
    removed = remove_originals(result.log_path)

    assert a.read_text() == "NEWER"  # 新內容留在原位
    assert ops[0].dst.read_text() == "A"
    assert [p.read_text() for p in trash.iterdir()] == ["A"] and removed.done == 1


def test_copy_modified_after_compare_is_not_removed_on_undo(tmp_path, trash, monkeypatch):
    a = touch(tmp_path / "src" / "a.jpg", "A")
    ops, result = organize(tmp_path, [a])
    real = organizer._same_content

    def compare_then_modify(x, y, **kwargs):
        same = real(x, y, **kwargs)
        if same and Path(y) == ops[0].dst:
            ops[0].dst.write_text("EDITED")
        return same

    monkeypatch.setattr(organizer, "_same_content", compare_then_modify)
    undone = undo(result.log_path)
    assert undone.done == 0 and ops[0].dst.read_text() == "EDITED" and not list(trash.iterdir())


def test_file_in_use_is_reported_not_removed(tmp_path, trash, monkeypatch):
    a = touch(tmp_path / "src" / "a.jpg", "A")
    _, result = organize(tmp_path, [a])
    real = organizer._rename_no_clobber

    def busy(src, dst):
        if Path(src) == a:
            raise PermissionError("[WinError 32] 檔案正由另一個程序使用")
        return real(src, dst)

    monkeypatch.setattr(organizer, "_rename_no_clobber", busy)
    removed = remove_originals(result.log_path)
    assert removed.done == 0 and removed.errors and a.read_text() == "A"
    assert latest_log(tmp_path / "logs") == result.log_path  # 留著下次重試


# ---------------------------------------------------------------------------- 2. 偽造紀錄指定未參與整理的檔案
def write_log(path: Path, rows: list[list]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    buf = io.StringIO(newline="")
    writer = csv.writer(buf)
    writer.writerow(organizer.LOG_FIELDS)
    writer.writerows(rows)
    path.write_text(buf.getvalue(), encoding="utf-8-sig")
    return path


def test_rows_outside_recorded_folders_are_rejected(tmp_path, trash):
    a = touch(tmp_path / "src" / "a.jpg", "A")
    ops, result = organize(tmp_path, [a])
    victim = touch(tmp_path / "elsewhere" / "v.jpg", "A")  # 別的資料夾裡內容相同的照片
    twin = touch(tmp_path / "out" / "貓" / "twin.jpg", "A")
    rows = read_log(result.log_path)
    forged = [[r["動作"], r["原始路徑"], r["新路徑"], r["分類"], r["時間"], r["大小"], r["來源資料夾"], r["輸出資料夾"]]
              for r in rows]
    forged.append(["copy", str(victim), str(twin), "貓", "", "1", forged[0][6], forged[0][7]])
    write_log(result.log_path, forged)

    removed = remove_originals(result.log_path)

    assert victim.read_text() == "A" and not a.exists()  # 只有真正在來源資料夾內的才被移除
    assert removed.done == 1 and any("來源資料夾" in w for w in removed.warnings)


def test_inconsistent_folders_make_log_unreadable(tmp_path):
    log = write_log(tmp_path / "logs" / "整理紀錄_20240101_000000.csv", [
        ["copy", str(tmp_path / "s1" / "a.jpg"), str(tmp_path / "o" / "貓" / "a.jpg"), "貓", "", "1",
         str(tmp_path / "s1"), str(tmp_path / "o")],
        ["copy", str(tmp_path / "s2" / "b.jpg"), str(tmp_path / "o" / "貓" / "b.jpg"), "貓", "", "1",
         str(tmp_path / "s2"), str(tmp_path / "o")],
    ])
    with pytest.raises(ValueError, match="不一致"):
        read_log(log)


def test_log_roots_are_exposed_for_confirmation(tmp_path):
    a = touch(tmp_path / "src" / "sub" / "a.jpg", "A")
    _, result = organize(tmp_path, [a])
    source, output = organizer.log_roots(read_log(result.log_path))
    assert Path(source) == (tmp_path / "src").resolve() and Path(output) == (tmp_path / "out").resolve()


# ---------------------------------------------------------------------------- 3. 異常紀錄讓按鈕失敗且沒有正常提示
def test_oversized_field_gives_value_error_not_csv_error(tmp_path):
    log = tmp_path / "logs" / "整理紀錄_20240101_000000.csv"
    log.parent.mkdir()
    huge = "x" * (csv.field_size_limit() + 10)
    log.write_text(",".join(organizer.LOG_FIELDS) + "\n" + '"' + huge + '\n', encoding="utf-8-sig")
    with pytest.raises(ValueError):
        read_log(log)


def test_long_but_legitimate_paths_are_fine(tmp_path):
    long_dir = tmp_path / ("長路徑" * 10) / ("d" * 60)  # 夠長，但仍在 Windows 預設的 260 字元路徑限制內
    a = touch(long_dir / "a.jpg", "A")
    ops = plan_operations([(a, "貓", D)], tmp_path / "out")
    result = execute(ops, tmp_path / "logs", source_dir=long_dir, output_dir=tmp_path / "out")
    assert len(read_log(result.log_path)) == 1


def test_set_aside_unreadable_log(tmp_path):
    log = tmp_path / "logs" / "整理紀錄_20240101_000000.csv"
    log.parent.mkdir()
    log.write_bytes(b"\xff\xfe garbage")
    moved = organizer.set_aside_log(log)
    assert moved.exists() and not log.exists() and latest_log(tmp_path / "logs") is None


def test_alias_path_helper_on_this_platform(tmp_path):
    """測試用的「同一個檔案的另一條路徑」在這個平台上要能建立（Windows 用大小寫、其他用符號連結）。"""
    from test_copy_first_safety import alias_of

    a = touch(tmp_path / "src" / "a.jpg", "A")
    alias = alias_of(a)
    if alias is None:
        pytest.skip("此環境無法建立同一個檔案的別名路徑")
    assert os.path.samefile(alias, a) and str(alias) != str(a)
