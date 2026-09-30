from datetime import datetime
from pathlib import Path

from media_sorter.organizer import execute, latest_log, plan_operations, render_name, safe_name, undo


def touch(path: Path, text: str = "x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_safe_name_replaces_windows_invalid_chars():
    assert safe_name('貓/狗:*?"<>|') == "貓_狗_______"
    assert safe_name("  ") == "未命名"
    assert safe_name("CON") == "_CON"
    assert safe_name("名稱. ") == "名稱"


def test_render_name_tokens():
    name = render_name("{日期}_{分類}_{序號}_{原檔名}", category="貓", date=datetime(2024, 5, 1),
                       stem="IMG_1", seq=7, width=3)
    assert name == "20240501_貓_007_IMG_1"


def test_plan_numbers_by_date_and_groups_by_category(tmp_path):
    src = tmp_path / "src"
    a = touch(src / "b.JPG")
    b = touch(src / "a.jpg")
    c = touch(src / "c.png")
    entries = [
        (a, "貓", datetime(2024, 1, 2)),
        (b, "貓", datetime(2024, 1, 1)),
        (c, "狗", datetime(2024, 1, 1)),
    ]
    ops = plan_operations(entries, tmp_path / "out", rename=True, pattern="{分類}_{序號}")
    result = {op.src.name: op.dst.relative_to(tmp_path / "out").as_posix() for op in ops}
    assert result == {"a.jpg": "貓/貓_001.jpg", "b.JPG": "貓/貓_002.jpg", "c.png": "狗/狗_001.png"}


def test_plan_skips_existing_names(tmp_path):
    touch(tmp_path / "out" / "貓" / "貓_001.jpg")
    src = touch(tmp_path / "x.jpg")
    ops = plan_operations([(src, "貓", datetime(2024, 1, 1))], tmp_path / "out", pattern="{分類}_{序號}")
    assert ops[0].dst.name == "貓_002.jpg"


def test_plan_sequence_per_date_prefix(tmp_path):
    files = [touch(tmp_path / f"{i}.jpg") for i in range(3)]
    dates = [datetime(2024, 1, 1), datetime(2024, 1, 1), datetime(2024, 1, 2)]
    ops = plan_operations(list(zip(files, ["貓"] * 3, dates, strict=True)), tmp_path / "out", pattern="{分類}_{日期}_{序號}")
    assert [op.dst.name for op in ops] == ["貓_20240101_001.jpg", "貓_20240101_002.jpg", "貓_20240102_001.jpg"]


def test_plan_pattern_without_sequence_adds_number_only_on_collision(tmp_path):
    a = touch(tmp_path / "a" / "same.jpg")
    b = touch(tmp_path / "b" / "same.jpg")
    ops = plan_operations([(a, "貓", datetime(2024, 1, 1)), (b, "貓", datetime(2024, 1, 2))],
                          tmp_path / "out", pattern="{分類}_{原檔名}")
    assert [op.dst.name for op in ops] == ["貓_same.jpg", "貓_same_002.jpg"]


def test_plan_without_rename_keeps_names_and_resolves_collisions(tmp_path):
    a = touch(tmp_path / "a" / "same.jpg")
    b = touch(tmp_path / "b" / "same.jpg")
    ops = plan_operations([(a, "貓", datetime(2024, 1, 1)), (b, "貓", datetime(2024, 1, 1))],
                          tmp_path / "out", rename=False)
    assert sorted(op.dst.name for op in ops) == ["same (2).jpg", "same.jpg"]


def test_copy_and_undo_roundtrip(tmp_path):
    a = touch(tmp_path / "src" / "a.jpg", "A")
    b = touch(tmp_path / "src" / "sub" / "b.mp4", "B")
    ops = plan_operations([(a, "貓", datetime(2024, 1, 1)), (b, "影片", datetime(2024, 1, 1))], tmp_path / "out")
    logs = tmp_path / "logs"
    result = execute(ops, logs)
    assert result.done == 2 and not result.errors
    assert a.read_text() == "A" and b.read_text() == "B"  # 原檔完全不動
    assert (tmp_path / "out" / "貓" / "貓_001.jpg").read_text() == "A"
    assert latest_log(logs) == result.log_path

    undone = undo(result.log_path)
    assert undone.done == 2 and not undone.errors
    assert a.read_text() == "A" and b.read_text() == "B"
    assert not (tmp_path / "out" / "貓").exists()  # 複本移除後，空資料夾會被清掉
    assert latest_log(logs) is None  # 紀錄已標記為已復原


def test_execute_reports_errors_and_continues(tmp_path):
    a = touch(tmp_path / "a.jpg")
    missing = tmp_path / "missing.jpg"
    ops = plan_operations([(missing, "貓", datetime(2024, 1, 1)), (a, "貓", datetime(2024, 1, 2))], tmp_path / "out")
    result = execute(ops, tmp_path / "logs")
    assert result.done == 1
    assert [p.name for p, _ in result.errors] == ["missing.jpg"]
