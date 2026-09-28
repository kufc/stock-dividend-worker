"""第二輪外部審查找到的 7 個問題，每一項都有對應的回歸測試。"""

import csv
import errno
import os
import shutil
import threading
from datetime import datetime
from pathlib import Path

import pytest
from PIL import Image

from media_sorter import organizer
from media_sorter.config import MODEL_CHOICES, load_settings, save_settings
from media_sorter.organizer import execute, plan_operations, read_log, undo
from fakes import FakeClassifier
from helpers import make_image

D = datetime(2024, 1, 1)


def touch(path: Path, text: str = "x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def write_raw_log(path: Path, rows: list[dict]) -> Path:
    """繞過 execute()，直接手動寫一份（可能被竄改過的）紀錄檔，用來測試 undo 的防護。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=organizer.LOG_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    return path


# ---------------------------------------------------------------------------- 1. .partial 暫存檔可能覆蓋既有檔案
def test_copy_does_not_clobber_existing_partial_file(tmp_path):
    out_dir = tmp_path / "out" / "貓"
    out_dir.mkdir(parents=True)
    existing_partial = touch(out_dir / "貓_001.jpg.partial", "KEEP")
    a = touch(tmp_path / "a.jpg", "NEW")

    result = execute(plan_operations([(a, "貓", D)], tmp_path / "out"), "copy", tmp_path / "logs")

    assert result.done == 1
    assert existing_partial.read_text() == "KEEP"  # 使用者自己放的暫存檔完全沒被動到


def test_cross_device_move_success_does_not_clobber_existing_partial_file(tmp_path, monkeypatch):
    out_dir = tmp_path / "out" / "貓"
    out_dir.mkdir(parents=True)
    existing_partial = touch(out_dir / "貓_001.jpg.partial", "KEEP")
    a = touch(tmp_path / "a.jpg", "NEW")

    def exdev(*args, **kwargs):
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    monkeypatch.setattr(organizer, "_rename_no_clobber",
                        lambda s, d: exdev() if Path(s) == a else os.rename(s, d))

    result = execute(plan_operations([(a, "貓", D)], tmp_path / "out"), "move", tmp_path / "logs")

    assert result.done == 1 and not a.exists()
    assert existing_partial.read_text() == "KEEP"


def test_cross_device_move_failure_does_not_clobber_existing_partial_file(tmp_path, monkeypatch):
    out_dir = tmp_path / "out" / "貓"
    out_dir.mkdir(parents=True)
    existing_partial = touch(out_dir / "貓_001.jpg.partial", "KEEP")
    a = touch(tmp_path / "a.jpg", "NEW")

    def exdev(*args, **kwargs):
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    monkeypatch.setattr(organizer, "_rename_no_clobber",
                        lambda s, d: exdev() if Path(s) == a else os.rename(s, d))

    def broken_copy(src, dst, *args, **kwargs):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(shutil, "copyfile", broken_copy)

    result = execute(plan_operations([(a, "貓", D)], tmp_path / "out"), "move", tmp_path / "logs")

    assert result.done == 0 and result.errors
    assert a.read_text() == "NEW"  # 原檔沒被動到
    assert existing_partial.read_text() == "KEEP"  # 使用者自己放的暫存檔完全沒被動到


# ---------------------------------------------------------------------------- 2. 檔案已搬移但紀錄寫入失敗 → 無法復原
def test_confirm_row_write_failure_is_recoverable(tmp_path, monkeypatch):
    """確認列寫入失敗：檔案已經搬移、result.done==1、aborted，但紀錄檔還在，可以按 undo 搬回原位。"""
    a = touch(tmp_path / "a.jpg", "A")
    calls = {"n": 0}
    real_fsync = os.fsync

    def flaky_fsync(fd):
        calls["n"] += 1
        if calls["n"] >= 2:  # 第 1 次是 pending 列（成功），第 2 次是確認列（失敗）
            raise OSError(errno.ENOSPC, "No space left on device")
        real_fsync(fd)

    monkeypatch.setattr(organizer.os, "fsync", flaky_fsync)
    result = execute(plan_operations([(a, "貓", D)], tmp_path / "out"), "move", tmp_path / "logs")

    assert result.done == 1 and result.aborted
    assert result.log_path is not None and result.log_path.exists()
    assert not a.exists()  # 檔案確實已經搬走了
    assert "已預先記錄" in result.errors[0][1]

    monkeypatch.undo()  # 復原時用回真正的 fsync
    undone = undo(result.log_path)
    assert undone.done == 1 and not undone.errors
    assert a.read_text() == "A"  # 能夠正常搬回原位


def test_pending_row_write_failure_touches_no_file(tmp_path, monkeypatch):
    """連 pending 列都寫不進去：完全不碰檔案，不會出現「動過但沒有任何紀錄」的檔案。"""
    a = touch(tmp_path / "a.jpg", "A")

    def broken_fsync(fd):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(organizer.os, "fsync", broken_fsync)
    result = execute(plan_operations([(a, "貓", D)], tmp_path / "out"), "move", tmp_path / "logs")

    assert result.done == 0 and result.aborted
    assert a.exists() and a.read_text() == "A"
    assert not any((tmp_path / "out").rglob("*.*"))


# ---------------------------------------------------------------------------- 3. 部分復原失敗時重寫紀錄會先清空原檔
def test_undo_log_rewrite_failure_keeps_original_log_intact(tmp_path, monkeypatch):
    a = touch(tmp_path / "src" / "a.jpg", "A")
    result = execute(plan_operations([(a, "貓", D)], tmp_path / "out"), "move", tmp_path / "logs")
    touch(a, "NEW FILE")  # 原位置出現同名檔 → 這一列復原會失敗，需要重寫紀錄
    original_bytes = result.log_path.read_bytes()

    def broken_writerows(self, rows):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(csv.DictWriter, "writerows", broken_writerows)
    undone = undo(result.log_path)
    assert undone.done == 0 and undone.errors  # 復原本身就失敗，紀錄檔也寫不進去
    assert result.log_path.read_bytes() == original_bytes  # 原紀錄完全沒變
    assert not list(result.log_path.parent.glob("*.tmp"))  # 沒有殘留暫存檔

    monkeypatch.undo()  # 讓寫入恢復正常
    a.unlink()  # 移開擋路的新檔案
    retried = undo(result.log_path)
    assert retried.done == 1 and not retried.errors
    assert a.read_text() == "A"  # 原紀錄還在，可以再次復原


# ---------------------------------------------------------------------------- 4. 原檔被修改／替換後，復原仍刪掉複本
def test_undo_keeps_copy_when_original_content_later_changed(tmp_path):
    a = touch(tmp_path / "src" / "a.jpg", "ORIGINAL")
    result = execute(plan_operations([(a, "貓", D)], tmp_path / "out"), "copy", tmp_path / "logs")
    dst = Path(read_log(result.log_path)[0]["新路徑"])
    touch(a, "CHANGED LATER")  # 使用者後來把原檔換成別的內容（不是改複本）

    undone = undo(result.log_path)

    assert undone.done == 0 and len(undone.errors) == 1
    assert dst.read_text() == "ORIGINAL"  # 複本保留舊內容，沒有被刪掉
    assert a.read_text() == "CHANGED LATER"


def test_undo_quarantines_copy_when_send2trash_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(organizer, "_send2trash", None)
    a = touch(tmp_path / "src" / "a.jpg", "A")
    result = execute(plan_operations([(a, "貓", D)], tmp_path / "out"), "copy", tmp_path / "logs")
    dst = Path(read_log(result.log_path)[0]["新路徑"])

    undone = undo(result.log_path)

    assert undone.done == 1 and not undone.errors
    assert not dst.exists()  # 不在原本的位置了
    quarantined = list((tmp_path / "logs" / organizer.QUARANTINE_DIR).rglob(dst.name))
    assert len(quarantined) == 1 and quarantined[0].read_text() == "A"  # 搬到隔離資料夾而不是憑空消失
    assert undone.warnings and "隔離資料夾" in undone.warnings[0]


# ---------------------------------------------------------------------------- 5. 遭竄改的紀錄可指定任意檔案
def test_undo_rejects_unknown_action(tmp_path):
    out = tmp_path / "out" / "貓"
    out.mkdir(parents=True)
    src = touch(tmp_path / "src" / "a.jpg", "SAME")
    dst = touch(out / "a.jpg", "SAME")
    log = write_raw_log(tmp_path / "logs" / "整理紀錄_forged.csv", [{
        "動作": "delete", "原始路徑": str(src.resolve()), "新路徑": str(dst.resolve()), "分類": "貓",
        "時間": "", "大小": "", "修改時間": "", "輸出資料夾": str(out.parent.resolve()),
    }])

    result = undo(log)

    assert result.done == 0 and len(result.errors) == 1
    assert src.read_text() == "SAME" and dst.read_text() == "SAME"


def test_undo_rejects_relative_path(tmp_path):
    out = tmp_path / "out" / "貓"
    out.mkdir(parents=True)
    src = touch(tmp_path / "src" / "a.jpg", "SAME")
    dst = touch(out / "a.jpg", "SAME")
    log = write_raw_log(tmp_path / "logs" / "整理紀錄_forged.csv", [{
        "動作": "move", "原始路徑": "a.jpg", "新路徑": str(dst.resolve()), "分類": "貓",
        "時間": "", "大小": "", "修改時間": "", "輸出資料夾": str(out.parent.resolve()),
    }])

    result = undo(log)

    assert result.done == 0 and len(result.errors) == 1
    assert src.read_text() == "SAME" and dst.read_text() == "SAME"


def test_undo_rejects_extension_mismatch(tmp_path):
    out = tmp_path / "out" / "貓"
    out.mkdir(parents=True)
    src = touch(tmp_path / "src" / "a.jpg", "SAME")
    dst = touch(out / "a.png", "SAME")
    log = write_raw_log(tmp_path / "logs" / "整理紀錄_forged.csv", [{
        "動作": "copy", "原始路徑": str(src.resolve()), "新路徑": str(dst.resolve()), "分類": "貓",
        "時間": "", "大小": str(dst.stat().st_size), "修改時間": str(dst.stat().st_mtime_ns),
        "輸出資料夾": str(out.parent.resolve()),
    }])

    result = undo(log)

    assert result.done == 0 and len(result.errors) == 1
    assert src.exists() and dst.read_text() == "SAME"


def test_undo_rejects_path_outside_output_folder(tmp_path):
    out = tmp_path / "out" / "貓"
    out.mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    src = touch(tmp_path / "src" / "a.jpg", "SAME")
    dst = touch(elsewhere / "a.jpg", "SAME")  # 實際位置不在宣稱的輸出資料夾內
    log = write_raw_log(tmp_path / "logs" / "整理紀錄_forged.csv", [{
        "動作": "move", "原始路徑": str(src.resolve()), "新路徑": str(dst.resolve()), "分類": "貓",
        "時間": "", "大小": "", "修改時間": "", "輸出資料夾": str((tmp_path / "out").resolve()),
    }])

    result = undo(log)

    assert result.done == 0 and len(result.errors) == 1
    assert src.exists() and dst.read_text() == "SAME"


def test_undo_copy_missing_size_and_mtime_still_requires_content_match(tmp_path):
    out = tmp_path / "out" / "貓"
    out.mkdir(parents=True)
    src = touch(tmp_path / "src" / "a.jpg", "ORIGINAL")
    dst = touch(out / "a.jpg", "DIFFERENT CONTENT")  # 偽造的紀錄想騙去刪除，但內容跟原檔不同
    log = write_raw_log(tmp_path / "logs" / "整理紀錄_forged.csv", [{
        "動作": "copy", "原始路徑": str(src.resolve()), "新路徑": str(dst.resolve()), "分類": "貓",
        "時間": "", "大小": "", "修改時間": "", "輸出資料夾": str(out.parent.resolve()),
    }])

    result = undo(log)

    assert result.done == 0 and len(result.errors) == 1
    assert src.read_text() == "ORIGINAL" and dst.read_text() == "DIFFERENT CONTENT"


# ---------------------------------------------------------------------------- 6. batch_size=0 讓辨識空轉
def test_load_settings_clamps_invalid_values(tmp_path):
    path = tmp_path / "settings.json"
    save_settings({
        "batch_size": 0, "video_frames": 0, "confidence_threshold": 5,
        "model": "不存在的模型", "action": "delete", "include_subfolders": "yes",
    }, path)

    settings = load_settings(path)

    assert settings["batch_size"] >= 1
    assert settings["video_frames"] >= 1
    assert 0.05 <= settings["confidence_threshold"] <= 0.95
    assert settings["model"] in MODEL_CHOICES
    assert settings["action"] in ("move", "copy")
    assert isinstance(settings["include_subfolders"], bool)

    save_settings({"batch_size": -5, "video_frames": "abc"}, path)
    settings2 = load_settings(path)
    assert settings2["batch_size"] >= 1
    assert settings2["video_frames"] >= 1


def test_analyze_with_batch_size_zero_completes_quickly(tmp_path):
    from media_sorter.analysis import analyze
    from media_sorter.config import Category

    paths = [make_image(tmp_path / f"{i}.png", (250, 10, 10)) for i in range(4)]
    result: dict = {}
    worker = threading.Thread(target=lambda: result.update(items=analyze(
        paths, FakeClassifier(), [Category("紅")], batch_size=0, video_frames=0)))
    worker.start()
    worker.join(5)
    assert not worker.is_alive(), "analyze(batch_size=0) 卡住了"
    assert len(result["items"]) == 4 and all(it.analyzed for it in result["items"])


# ---------------------------------------------------------------------------- 7. 超大圖片可能耗盡記憶體
def test_max_image_pixels_is_capped_around_300_million():
    assert 250_000_000 < Image.MAX_IMAGE_PIXELS < 400_000_000


def test_oversized_image_reports_friendly_chinese_error(tmp_path, monkeypatch):
    from media_sorter import media

    monkeypatch.setattr(media.Image, "MAX_IMAGE_PIXELS", 100)  # 把門檻調到很小
    path = make_image(tmp_path / "big.png", (10, 20, 30), size=(64, 48))  # 64*48 遠超過門檻

    with pytest.raises(ValueError, match="圖片太大"):
        media.load_image(path)


def test_large_non_jpeg_image_uses_decode_semaphore(tmp_path, monkeypatch):
    from PIL import ImageFile

    from media_sorter import media

    monkeypatch.setattr(media, "LARGE_DECODE_PIXELS", 1000)  # 64*48=3072，會被視為「很大」
    path = make_image(tmp_path / "big.png", (10, 20, 30), size=(64, 48))
    # Pillow 讀取這張圖前後會呼叫好幾次 load()（讀 EXIF、轉色彩⋯），只有解碼那一次需要鎖住
    lock_states = []
    real_load = ImageFile.ImageFile.load  # PNG 等格式的 load() 是在 ImageFile 這個中介類別實作，不是 Image.Image

    def spy_load(self):
        lock_states.append(media._decode_semaphore._value == 0)
        return real_load(self)

    monkeypatch.setattr(ImageFile.ImageFile, "load", spy_load)

    media.load_image(path)

    assert any(lock_states)  # 至少有一次是在鎖住的狀態下解碼
    assert media._decode_semaphore._value == 1  # 用完後有釋放
