"""第四輪外部審查找到的 4 個問題（以及同類隱患）的回歸測試。"""

import csv
import io
from datetime import datetime
from pathlib import Path

import pytest

from media_sorter import organizer
from media_sorter.organizer import execute, plan_operations, read_log, undo

D = datetime(2024, 1, 1)


def touch(path: Path, text: str = "x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def raw_rows(log: Path) -> list[list[str]]:
    return list(csv.reader(io.StringIO(log.read_text(encoding="utf-8-sig"), newline="")))


def write_raw(log: Path, rows: list[list[str]]) -> None:
    buf = io.StringIO(newline="")
    csv.writer(buf).writerows(rows)
    log.write_text(buf.getvalue(), encoding="utf-8-sig")


@pytest.fixture(autouse=True)
def _no_real_trash(monkeypatch):
    monkeypatch.setattr(organizer, "_send2trash", None)


# ---------------------------------------------------------------------------- 1. 確認列簽章沒寫完 → 丟掉有效的 pending 列
@pytest.mark.parametrize("cut", [1, 10, 40, 64, 70, 90, 120])
def test_truncated_confirm_signature_keeps_valid_pending_row(tmp_path, cut):
    a = touch(tmp_path / "src" / "a.jpg", "A")
    result = execute(plan_operations([(a, "貓", D)], tmp_path / "out"), "move", tmp_path / "logs")
    text = result.log_path.read_text(encoding="utf-8-sig").rstrip("\r\n")
    result.log_path.write_text(text[:-cut], encoding="utf-8-sig")  # 確認列最後的簽章只寫出一部分

    undone = undo(result.log_path)

    assert undone.done == 1 and not undone.errors, undone.errors
    assert a.read_text() == "A"
    assert any("寫入不完整" in w for w in undone.warnings)


def test_forged_full_row_at_end_is_reported_not_ignored(tmp_path):
    """欄位齊全但沒有合法批次編號的最後一列不是「寫到一半」，要回報而不是默默忽略。"""
    a = touch(tmp_path / "src" / "a.jpg", "A")
    result = execute(plan_operations([(a, "貓", D)], tmp_path / "out"), "move", tmp_path / "logs")
    rows = raw_rows(result.log_path)
    victim = touch(tmp_path / "out" / "貓" / "victim.jpg", "V")
    rows.append(["move", str(tmp_path / "x.jpg"), str(victim), "貓", "", "", "", str(tmp_path / "out"), "", ""])
    write_raw(result.log_path, rows)

    undone = undo(result.log_path)

    assert undone.done == 1 and len(undone.errors) == 1
    assert victim.read_text() == "V" and a.read_text() == "A"


def test_failed_undo_keeps_all_valid_rows_of_that_file(tmp_path):
    a = touch(tmp_path / "src" / "a.jpg", "A")
    result = execute(plan_operations([(a, "貓", D)], tmp_path / "out"), "move", tmp_path / "logs")
    text = result.log_path.read_text(encoding="utf-8-sig").rstrip("\r\n")
    result.log_path.write_text(text[:-10], encoding="utf-8-sig")
    touch(a, "BLOCKER")  # 原位置被占用 → 這次復原失敗，要重寫紀錄

    assert undo(result.log_path).errors
    actions = [r["動作"] for r in read_log(result.log_path)]
    assert actions == ["pending-move"]  # 有效的 pending 列一定要留下來
    a.unlink()
    assert undo(result.log_path).done == 1 and a.read_text() == "A"


# ---------------------------------------------------------------------------- 2. 重放已復原的舊紀錄
def test_replaying_undone_log_does_not_move_new_file(tmp_path):
    a = touch(tmp_path / "src" / "a.jpg", "OLD")
    result = execute(plan_operations([(a, "貓", D)], tmp_path / "out"), "move", tmp_path / "logs")
    saved = result.log_path.read_bytes()
    dst = Path(read_log(result.log_path)[0]["新路徑"])
    assert undo(result.log_path).done == 1 and a.read_text() == "OLD"

    a.rename(tmp_path / "moved-away.jpg")  # 原來源位置空了
    touch(dst, "NEW PHOTO")  # 原輸出位置出現內容不同的新照片
    result.log_path.write_bytes(saved)  # 把舊紀錄放回待復原清單

    replay = undo(result.log_path)

    assert replay.done == 0
    assert dst.read_text() == "NEW PHOTO" and not a.exists()


def test_same_file_organized_again_can_still_be_undone(tmp_path):
    """防重放不能擋到正常使用：整理 → 復原 → 再整理一次同一個檔案 → 再復原。"""
    a = touch(tmp_path / "src" / "a.jpg", "A")
    for _ in range(2):
        result = execute(plan_operations([(a, "貓", D)], tmp_path / "out"), "move", tmp_path / "logs")
        assert not a.exists()
        assert undo(result.log_path).done == 1 and a.read_text() == "A"


# ---------------------------------------------------------------------------- 3. 簽章欄出現中文字 → 整次復原中斷
def test_non_ascii_signature_only_fails_that_row(tmp_path):
    a = touch(tmp_path / "src" / "a.jpg", "A")
    b = touch(tmp_path / "src" / "b.jpg", "B")
    result = execute(plan_operations([(a, "貓", D), (b, "貓", D)], tmp_path / "out"), "move", tmp_path / "logs")
    rows = raw_rows(result.log_path)
    sig = rows[0].index("簽章")
    for row in rows[1:]:
        if row[1] == str(a):
            row[sig] = "簽章被改成中文"
    write_raw(result.log_path, rows)

    undone = undo(result.log_path)

    assert b.read_text() == "B"  # 其他檔案照常復原
    assert undone.done == 1 and len(undone.errors) == 1
    assert not a.exists()


def test_unexpected_error_in_one_row_does_not_stop_others(tmp_path, monkeypatch):
    a = touch(tmp_path / "src" / "a.jpg", "A")
    b = touch(tmp_path / "src" / "b.jpg", "B")
    result = execute(plan_operations([(a, "貓", D), (b, "貓", D)], tmp_path / "out"), "move", tmp_path / "logs")
    real = organizer._move_back

    def flaky(dst, src):
        if src == a:
            raise TypeError("意料之外的錯誤")
        return real(dst, src)

    monkeypatch.setattr(organizer, "_move_back", flaky)
    undone = undo(result.log_path)
    assert b.read_text() == "B" and undone.done == 1 and len(undone.errors) == 1


# ---------------------------------------------------------------------------- 4. 分類名稱含換行 → 自己的紀錄被判定簽章不符
def test_category_with_newline_can_be_undone(tmp_path):
    a = touch(tmp_path / "src" / "a.jpg", "A")
    result = execute(plan_operations([(a, "cat\n dog", D)], tmp_path / "out"), "move", tmp_path / "logs")
    assert not a.exists()
    undone = undo(result.log_path)
    assert undone.done == 1 and not undone.errors, undone.errors
    assert a.read_text() == "A"


# ---------------------------------------------------------------------------- 同類隱患：簽章欄位邊界可被搬動
def test_signature_binds_field_boundaries():
    fields = ["move", "C:/a\x1fb", "C:/c"]
    shifted = ["move", "C:/a", "b\x1fC:/c"]
    assert organizer._signature(fields) != organizer._signature(shifted)
