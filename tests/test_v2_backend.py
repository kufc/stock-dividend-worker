"""四步驟介面所需的底層功能：資料夾統計、可停止與可重試的複製、紀錄快照與預檢、紀錄清單、友善錯誤訊息。"""

import errno
import shutil
import threading
from datetime import datetime
from pathlib import Path

import pytest

from helpers import make_image
from media_sorter import organizer
from media_sorter.config import RENAME_MODES, load_settings, save_settings
from media_sorter.media import folder_stats
from media_sorter.organizer import (
    LogChangedError, classify_rows, describe_error, execute, list_logs, plan_operations, read_log,
    remove_originals, snapshot_log, undo,
)

D = datetime(2024, 1, 1)


def touch(path: Path, text: str = "x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


@pytest.fixture
def trash(tmp_path, monkeypatch):
    bin_dir = tmp_path / "RECYCLE"
    bin_dir.mkdir()
    monkeypatch.setattr(organizer, "_send2trash", lambda p: shutil.move(p, bin_dir / Path(p).name))
    return bin_dir


def organize(tmp_path, files):
    ops = plan_operations([(f, "貓", D) for f in files], tmp_path / "out")
    return ops, execute(ops, tmp_path / "logs", source_dir=tmp_path / "src", output_dir=tmp_path / "out")


# ---------------------------------------------------------------------------- 資料夾統計
def test_folder_stats_counts_kinds_and_skips_hidden(tmp_path):
    make_image(tmp_path / "a.jpg", "red")
    make_image(tmp_path / "sub" / "b.png", "red")
    touch(tmp_path / "clip.mp4", "v")
    touch(tmp_path / "notes.txt")
    touch(tmp_path / "doc.pdf")
    touch(tmp_path / ".hidden.jpg")
    make_image(tmp_path / "已分類" / "c.jpg", "red")
    stats = folder_stats(tmp_path, recursive=True, exclude=tmp_path / "已分類")
    assert (stats.images, stats.videos, stats.other, stats.supported) == (2, 1, 2, 3)
    flat = folder_stats(tmp_path, recursive=False, exclude=tmp_path / "已分類")
    assert (flat.images, flat.videos) == (1, 1)


# ---------------------------------------------------------------------------- 複製：停止、重試
def test_execute_stops_after_current_file(tmp_path):
    files = [touch(tmp_path / "src" / f"{i}.jpg", str(i)) for i in range(5)]
    ops = plan_operations([(f, "貓", D) for f in files], tmp_path / "out")
    stop = threading.Event()
    result = execute(ops, tmp_path / "logs", source_dir=tmp_path / "src", output_dir=tmp_path / "out",
                     on_progress=lambda i, n: stop.set() if i == 2 else None, stop_event=stop)
    assert result.stopped and result.done == 2 and not result.errors
    assert len(read_log(result.log_path)) == 2  # 已完成的部分照常留在紀錄中
    assert [f.read_text() for f in files] == ["0", "1", "2", "3", "4"]


def test_execute_stopped_before_first_file_leaves_no_log(tmp_path):
    a = touch(tmp_path / "src" / "a.jpg")
    ops = plan_operations([(a, "貓", D)], tmp_path / "out")
    stop = threading.Event()
    stop.set()
    result = execute(ops, tmp_path / "logs", source_dir=tmp_path / "src", output_dir=tmp_path / "out", stop_event=stop)
    assert result.stopped and result.done == 0 and result.log_path is None


def test_retry_appends_to_the_same_log(tmp_path, monkeypatch):
    a = touch(tmp_path / "src" / "a.jpg", "A")
    b = touch(tmp_path / "src" / "b.jpg", "B")
    ops = plan_operations([(a, "貓", D), (b, "貓", D)], tmp_path / "out")
    real = organizer._copy_no_clobber
    monkeypatch.setattr(organizer, "_copy_no_clobber",
                        lambda s, d: (_ for _ in ()).throw(OSError(errno.ENOSPC, "磁碟已滿")) if s == b else real(s, d))
    first = execute(ops, tmp_path / "logs", source_dir=tmp_path / "src", output_dir=tmp_path / "out")
    assert first.done == 1 and len(first.errors) == 1 and "磁碟空間不足" in first.errors[0][1]
    monkeypatch.setattr(organizer, "_copy_no_clobber", real)
    retry_ops = [op for op in ops if op.src == b]
    second = execute(retry_ops, tmp_path / "logs", source_dir=tmp_path / "src", output_dir=tmp_path / "out",
                     append_to=first.log_path)
    assert second.done == 1 and second.log_path == first.log_path
    raw = first.log_path.read_bytes()
    assert raw.count(b"\xef\xbb\xbf") == 1  # 追加時不會再寫一次檔頭標記
    assert len(read_log(first.log_path)) == 2
    assert len(list((tmp_path / "logs").glob("整理紀錄_*.csv"))) == 1


# ---------------------------------------------------------------------------- 友善錯誤訊息
@pytest.mark.parametrize("exc, expected", [
    (OSError(errno.ENOSPC, "No space left"), "磁碟空間不足"),
    (PermissionError(13, "denied"), "正被其他程式使用"),
    (FileNotFoundError(2, "missing"), "找不到這個檔案"),
    (FileExistsError(17, "exists"), "沒有覆蓋"),
    (OSError(5, "weird"), "無法處理這個檔案"),
])
def test_describe_error_says_what_happened_and_what_to_do(exc, expected):
    text = describe_error(exc)
    assert expected in text and "詳細：" in text


# ---------------------------------------------------------------------------- 紀錄快照：確認的跟執行的必須是同一份
def test_undo_refuses_when_log_changed_after_confirmation(tmp_path, trash):
    a = touch(tmp_path / "src" / "a.jpg", "A")
    b = touch(tmp_path / "src" / "b.jpg", "B")
    ops, result = organize(tmp_path, [a, b])
    snap = snapshot_log(result.log_path)
    assert len(snap.rows) == 2
    with open(result.log_path, "a", encoding="utf-8-sig") as f:
        f.write("\n")  # 確認之後，紀錄被動過
    with pytest.raises(LogChangedError, match="重新檢視"):
        undo(result.log_path, expected_digest=snap.digest)
    with pytest.raises(LogChangedError):
        remove_originals(result.log_path, expected_digest=snap.digest)
    assert all(op.dst.exists() for op in ops) and a.exists() and b.exists()


def test_operations_run_when_digest_matches(tmp_path, trash):
    a = touch(tmp_path / "src" / "a.jpg", "A")
    _, result = organize(tmp_path, [a])
    snap = snapshot_log(result.log_path)
    assert remove_originals(result.log_path, expected_digest=snap.digest).done == 1


# ---------------------------------------------------------------------------- 預檢
def test_classify_rows_explains_what_will_be_skipped(tmp_path):
    files = [touch(tmp_path / "src" / f"{n}.jpg", n) for n in "abcd"]
    ops, result = organize(tmp_path, files)
    files[0].unlink()  # 原檔不在
    ops[1].dst.unlink()  # 複本不在
    remove_notes = {Path(r["原始路徑"]).name: note for r, note in classify_rows(read_log(result.log_path), "remove")}
    assert remove_notes["a.jpg"] == "略過：原檔已不在"
    assert remove_notes["b.jpg"] == "保留原檔：找不到複本"
    assert remove_notes["c.jpg"] is None and remove_notes["d.jpg"] is None
    undo_notes = {Path(r["原始路徑"]).name: note for r, note in classify_rows(read_log(result.log_path), "undo")}
    assert undo_notes["a.jpg"].startswith("保留：原檔已不在")
    assert undo_notes["b.jpg"] == "略過：複本已不在"
    assert undo_notes["c.jpg"] is None


def test_classify_rows_flags_invalid_rows(tmp_path):
    rows = [{"動作": "delete", "原始路徑": str(tmp_path / "a.jpg"), "新路徑": str(tmp_path / "b.jpg"),
             "來源資料夾": str(tmp_path), "輸出資料夾": str(tmp_path)}]
    (row, note), = classify_rows(rows, "remove")
    assert note.startswith("略過：") and "不明的動作" in note


# ---------------------------------------------------------------------------- 整理紀錄清單
def test_list_logs_shows_every_kind(tmp_path, trash):
    logs = tmp_path / "logs"
    a = touch(tmp_path / "src" / "a.jpg", "A")
    b = touch(tmp_path / "src" / "b.jpg", "B")
    c = touch(tmp_path / "src" / "c.jpg", "C")
    _, active = organize(tmp_path, [a])
    _, to_undo = organize(tmp_path, [b])
    undo(to_undo.log_path)
    _, to_finish = organize(tmp_path, [c])
    remove_originals(to_finish.log_path)
    (logs / "整理紀錄_20240101_000000.csv").write_bytes(b"\xff\xfe garbage")
    (logs / "error.log").write_text("x")
    (logs / "整理紀錄_複本.csv").write_text("x")  # 不是程式產生的檔名：不列出

    infos = {i.path.name: i for i in list_logs(logs)}
    assert infos[active.log_path.name].kind == "active" and infos[active.log_path.name].count == 1
    assert infos[active.log_path.name].source == organizer._canonical(tmp_path / "src")
    assert any(i.kind == "undone" for i in infos.values())
    assert any(i.kind == "finished" for i in infos.values())
    assert infos["整理紀錄_20240101_000000.csv"].kind == "unreadable"
    assert "整理紀錄_複本.csv" not in infos and "error.log" not in infos


# ---------------------------------------------------------------------------- 設定
def test_new_settings_are_validated(tmp_path):
    path = tmp_path / "settings.json"
    save_settings({"rename_mode": "不存在", "thumb_size": "huge", "rename": True}, path)
    settings = load_settings(path)
    assert settings["rename_mode"] == "keep" and settings["thumb_size"] == "medium"
    assert "rename" not in settings  # 舊版的設定欄位會被丟掉
    assert set(RENAME_MODES) == {"keep", "category_seq", "date_category_seq", "custom"}
    save_settings({"rename_mode": "custom", "thumb_size": "large"}, path)
    assert load_settings(path)["rename_mode"] == "custom" and load_settings(path)["thumb_size"] == "large"


# ---------------------------------------------------------------------------- 模型狀態
def test_model_status_detects_cache(tmp_path, monkeypatch):
    pytest.importorskip("torch")
    from media_sorter.classifier import model_status

    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path))
    label, cached = model_status("standard")
    assert "標準" in label and cached is False
    folder = tmp_path / "models--laion--CLIP-ViT-B-32-xlm-roberta-base-laion5B-s13B-b90k" / "snapshots" / "abc"
    touch(folder / "open_clip_model.safetensors", "x")
    assert model_status("standard")[1] is False  # 斷詞器還沒下載，仍然算「需要下載」
    touch(tmp_path / "models--xlm-roberta-base" / "snapshots" / "def" / "tokenizer.json", "x")
    assert model_status("standard")[1] is True


def test_model_status_returns_none_when_unknown(monkeypatch):
    from media_sorter import classifier

    monkeypatch.setattr(classifier, "detect_device", lambda: (_ for _ in ()).throw(RuntimeError("壞了")))
    assert classifier.model_status("auto") is None
