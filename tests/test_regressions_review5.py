"""第五輪外部審查找到的 3 個問題的回歸測試（含逐位元組截斷的全面測試）。"""

import csv
import io
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


@pytest.fixture(autouse=True)
def _no_real_trash(monkeypatch):
    monkeypatch.setattr(organizer, "_send2trash", None)


def _last_row_start(raw: bytes) -> int:
    body = raw.rstrip(b"\r\n")
    return body.rfind(b"\n") + 1


# ---------------------------------------------------------------------------- 1. 在位元組層級截斷（例如中文字寫一半）
def test_byte_truncation_at_every_position_of_confirm_row(tmp_path):
    """確認列在任何一個位元組被截斷（含中文字的一半），都要能依 pending 列正常復原。"""
    src = touch(tmp_path / "src" / "a.jpg", "A")
    result = execute(plan_operations([(src, "貓咪", D)], tmp_path / "out"), "move", tmp_path / "logs")
    raw = result.log_path.read_bytes().rstrip(b"\r\n")
    start = _last_row_start(raw)
    dst = Path(read_log(result.log_path)[0]["新路徑"])
    failures = []
    for cut in range(start + 1, len(raw)):
        result.log_path.write_bytes(raw[:cut])
        undone = undo(result.log_path)
        if undone.done != 1 or undone.errors or src.read_text() != "A":
            failures.append((cut, undone.done, undone.errors))
        # 還原到「已搬移」狀態，繼續測下一個截斷位置
        if src.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            organizer._rename_no_clobber(src, dst)
        for p in result.log_path.parent.glob("*.csv"):
            p.unlink()
        (organizer.log_key_dir() / organizer.UNDONE_REGISTRY).unlink(missing_ok=True)
    assert not failures, failures[:5]


def test_byte_truncation_at_every_position_of_second_pending_row(tmp_path):
    """第二個檔案的 pending 列寫到一半就中斷（那個檔案沒被動過）：第一個檔案照常復原、第二個不受影響。"""
    a = touch(tmp_path / "src" / "a.jpg", "A")
    b = touch(tmp_path / "src" / "b.jpg", "B")
    result = execute(plan_operations([(a, "貓咪", D)], tmp_path / "out"), "move", tmp_path / "logs")
    good = result.log_path.read_bytes()
    batch = read_log(result.log_path)[0]["批次"]
    buf = io.StringIO(newline="")
    csv.writer(buf).writerow(organizer._signed_row(
        ["pending-move", b, tmp_path / "out" / "貓咪" / "貓咪_002.jpg", "貓咪", "2024-01-01T00:00:00", "", "",
         tmp_path / "out", batch]))
    pending_b = buf.getvalue().encode("utf-8")
    dst_a = Path(read_log(result.log_path)[0]["新路徑"])
    failures = []
    for cut in range(1, len(pending_b)):
        result.log_path.write_bytes(good + pending_b[:cut])
        undone = undo(result.log_path)
        if undone.done != 1 or undone.errors or a.read_text() != "A" or b.read_text() != "B":
            failures.append((cut, undone.done, undone.errors))
        if a.exists():
            dst_a.parent.mkdir(parents=True, exist_ok=True)
            organizer._rename_no_clobber(a, dst_a)
        for p in result.log_path.parent.glob("*.csv"):
            p.unlink()
        (organizer.log_key_dir() / organizer.UNDONE_REGISTRY).unlink(missing_ok=True)
    assert not failures, failures[:5]


# ---------------------------------------------------------------------------- 2. 錯誤另存檔被當成最新紀錄
def test_side_files_are_never_picked_as_latest_log(tmp_path):
    logs = tmp_path / "logs"
    a = touch(tmp_path / "src" / "a.jpg", "A")
    older = execute(plan_operations([(a, "貓", D)], tmp_path / "out"), "move", logs)
    b = touch(tmp_path / "src" / "b.jpg", "B")
    newer = execute(plan_operations([(b, "狗", D)], tmp_path / "out"), "move", logs)
    with open(newer.log_path, "a", newline="", encoding="utf-8-sig") as f:  # 中間插入一列偽造的列
        csv.writer(f).writerow(["move", str(tmp_path / "x.jpg"), str(tmp_path / "out" / "狗" / "v.jpg"),
                                "狗", "", "", "", str(tmp_path / "out"), "", "bad"])
    rows = newer.log_path.read_text(encoding="utf-8-sig").splitlines()
    rows.append(rows.pop(1))  # 讓偽造列不在最後一列
    newer.log_path.write_text("\n".join(rows) + "\n", encoding="utf-8-sig")

    first = undo(newer.log_path)
    assert first.done == 1 and first.errors  # 偽造列被回報
    assert b.read_text() == "B"
    assert latest_log(logs) == older.log_path  # 下一次按復原，選到的是較早的正常紀錄
    second = undo(latest_log(logs))
    assert second.done == 1 and a.read_text() == "A"
    assert latest_log(logs) is None


def test_latest_log_ignores_unrelated_files(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "整理紀錄_20240101_000000.csv.無法驗證的列.csv").write_text("x")
    (logs / "整理紀錄_複本.csv").write_text("x")
    assert latest_log(logs) is None


# ---------------------------------------------------------------------------- 3. 判定「沒發生」的項目可被舊紀錄重新觸發
def test_replaying_archived_never_happened_entry_does_nothing(tmp_path, monkeypatch):
    a = touch(tmp_path / "src" / "a.jpg", "A")

    def failing_rename(src, dst):
        raise PermissionError("搬移失敗")

    monkeypatch.setattr(organizer, "_rename_no_clobber", failing_rename)
    result = execute(plan_operations([(a, "貓", D)], tmp_path / "out"), "move", tmp_path / "logs")
    monkeypatch.undo()
    assert result.done == 0 and result.log_path.exists()  # 留下合法的 pending 列
    saved = result.log_path.read_bytes()
    dst = Path(read_log(result.log_path)[0]["新路徑"])

    archived = undo(result.log_path)
    assert archived.done == 0 and not archived.errors  # 判定沒有搬過，封存
    assert not result.log_path.exists()

    a.rename(tmp_path / "elsewhere.jpg")  # 原來源位置空出
    touch(dst, "NEW PHOTO")  # 輸出位置出現另一張新照片
    result.log_path.write_bytes(saved)  # 把封存的舊紀錄放回

    replay = undo(result.log_path)

    assert replay.done == 0
    assert dst.read_text() == "NEW PHOTO" and not a.exists()


def test_never_happened_entry_is_kept_if_it_cannot_be_registered(tmp_path, monkeypatch):
    a = touch(tmp_path / "src" / "a.jpg", "A")
    monkeypatch.setattr(organizer, "_rename_no_clobber", lambda s, d: (_ for _ in ()).throw(PermissionError("x")))
    result = execute(plan_operations([(a, "貓", D)], tmp_path / "out"), "move", tmp_path / "logs")
    monkeypatch.undo()

    def broken_registry(entry_id):
        raise OSError("磁碟已滿")

    monkeypatch.setattr(organizer, "_remember_undone", broken_registry)
    undone = undo(result.log_path)
    assert undone.errors and result.log_path.exists()  # 無法登記 → 不封存
    assert latest_log(tmp_path / "logs") == result.log_path
