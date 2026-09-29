"""第三輪外部審查找到的 5 個問題，每一項都有對應的回歸測試。"""

import csv
import os
import threading
from datetime import datetime
from pathlib import Path

import pytest
from PIL import Image

from helpers import make_image
from media_sorter import media, organizer
from media_sorter.organizer import execute, plan_operations, read_log, undo

D = datetime(2024, 1, 1)


def touch(path: Path, text: str = "x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


# ---------------------------------------------------------------------------- 1. 紀錄寫到一半，復原就當掉
def test_half_written_confirm_row_does_not_break_undo(tmp_path):
    a = touch(tmp_path / "src" / "a.jpg", "A")
    b = touch(tmp_path / "src" / "b.jpg", "B")
    result = execute(plan_operations([(a, "貓", D), (b, "貓", D)], tmp_path / "out"), "move", tmp_path / "logs")
    # 模擬：最後一個檔案搬完後，確認列只寫出「move,」磁碟就滿了（只剩 pending 列完整）
    lines = result.log_path.read_text(encoding="utf-8-sig").splitlines()
    assert lines[-1].startswith("move,")
    truncated = "\n".join(lines[:-1]) + "\nmove,"
    result.log_path.write_text(truncated, encoding="utf-8-sig")

    undone = undo(result.log_path)

    assert not undone.errors, undone.errors
    assert undone.done == 2
    assert a.read_text() == "A" and b.read_text() == "B"


def test_garbage_rows_are_ignored_by_read_log(tmp_path):
    log = tmp_path / "整理紀錄_x.csv"
    log.write_text(",".join(organizer.LOG_FIELDS) + "\nmove,\n,,\n", encoding="utf-8-sig")
    assert read_log(log) == []


# ---------------------------------------------------------------------------- 2. 大圖保護鎖取得太晚
def test_png_decode_happens_only_while_locked(tmp_path, monkeypatch):
    from PIL import ImageFile

    monkeypatch.setattr(media, "LARGE_DECODE_PIXELS", 1000)
    path = tmp_path / "big.png"
    img = Image.new("RGB", (64, 48), (10, 20, 30))
    exif = Image.Exif()
    exif[306] = "2020:01:02 03:04:05"
    img.save(path, exif=exif)  # PNG 的 eXIf 在影像資料之後時，讀 EXIF 會觸發解碼
    unlocked_decodes = []
    real_load = ImageFile.ImageFile.load

    def spy_load(self):
        if getattr(self, "tile", None):  # 還有沒解碼的資料 = 這次呼叫會真的解碼
            unlocked_decodes.append(media._decode_semaphore._value == 1)
        return real_load(self)

    monkeypatch.setattr(ImageFile.ImageFile, "load", spy_load)
    for max_side in (640, None):
        media.load_image(path, max_side=max_side)
    assert unlocked_decodes and not any(unlocked_decodes)  # 每一次真正解碼都在鎖內


# ---------------------------------------------------------------------------- 3. 偽造紀錄仍能搬走無關照片
def test_forged_log_with_consistent_output_folder_is_rejected(tmp_path):
    out = tmp_path / "out"
    victim = touch(out / "貓" / "vacation.jpg", "PRECIOUS")  # 跟這次整理無關的照片
    target = tmp_path / "attacker" / "vacation.jpg"
    log = tmp_path / "logs" / "整理紀錄_forged.csv"
    log.parent.mkdir(parents=True)
    with open(log, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=organizer.LOG_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerow({"動作": "move", "原始路徑": str(target), "新路徑": str(victim), "分類": "貓",
                         "時間": "", "大小": "8", "修改時間": "0", "輸出資料夾": str(out)})

    result = undo(log)

    assert result.done == 0 and result.errors
    assert victim.read_text() == "PRECIOUS" and not target.exists()


def test_tampered_signed_row_is_rejected(tmp_path):
    a = touch(tmp_path / "src" / "a.jpg", "A")
    result = execute(plan_operations([(a, "貓", D)], tmp_path / "out"), "move", tmp_path / "logs")
    elsewhere = tmp_path / "attacker"
    text = result.log_path.read_text(encoding="utf-8-sig").replace(str(tmp_path / "src"), str(elsewhere))
    result.log_path.write_text(text, encoding="utf-8-sig")  # 改掉「原始路徑」但簽章沒跟著變

    undone = undo(result.log_path)

    assert undone.done == 0 and undone.errors
    assert not elsewhere.exists()


def test_signed_log_survives_big5_resave(tmp_path):
    a = touch(tmp_path / "src" / "a.jpg", "A")
    result = execute(plan_operations([(a, "貓", D)], tmp_path / "out"), "move", tmp_path / "logs")
    text = result.log_path.read_text(encoding="utf-8-sig")
    result.log_path.write_bytes(text.encode("cp950"))
    assert undo(result.log_path).done == 1 and a.exists()


# ---------------------------------------------------------------------------- 4. pending-move 原位置又出現檔案 → 被當成完成
def test_ambiguous_pending_move_is_kept_for_retry(tmp_path):
    a = touch(tmp_path / "src" / "a.jpg", "A")
    result = execute(plan_operations([(a, "貓", D)], tmp_path / "out"), "move", tmp_path / "logs")
    lines = result.log_path.read_text(encoding="utf-8-sig").splitlines()
    result.log_path.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8-sig")  # 確認列沒寫進去，只剩 pending
    assert read_log(result.log_path)[0]["動作"] == "pending-move"
    dst = Path(read_log(result.log_path)[0]["新路徑"])
    assert dst.read_text() == "A"
    touch(a, "SOMETHING ELSE")  # 原位置又出現另一個檔案

    undone = undo(result.log_path)

    assert undone.errors  # 不能默默當作完成
    assert result.log_path.exists()  # 紀錄沒有被標成「已復原」
    assert dst.read_text() == "A"
    a.unlink()
    assert undo(result.log_path).done == 1 and a.read_text() == "A"  # 排除後可以重試


# ---------------------------------------------------------------------------- 5. filecmp 快取舊結果
def test_content_check_is_not_fooled_by_cache(tmp_path, monkeypatch):
    a = touch(tmp_path / "src" / "a.jpg", "AAAA")
    result = execute(plan_operations([(a, "貓", D)], tmp_path / "out"), "copy", tmp_path / "logs")
    dst = Path(read_log(result.log_path)[0]["新路徑"])
    deleted = []

    def failing_delete(path, quarantine):
        deleted.append(path)
        raise OSError("第一次刪除失敗")

    monkeypatch.setattr(organizer, "_delete", failing_delete)
    assert undo(result.log_path).errors  # 第一次：內容相同，嘗試刪除但失敗
    st = a.stat()
    a.write_text("BBBB")  # 改成不同內容、相同大小
    os.utime(a, ns=(st.st_atime_ns, st.st_mtime_ns))  # 修改時間也維持不變
    deleted.clear()

    second = undo(result.log_path)

    assert not deleted  # 內容不同，絕不能進入刪除
    assert second.errors and dst.read_text() == "AAAA"


def test_threads_do_not_share_decode_slot_leak(tmp_path, monkeypatch):
    monkeypatch.setattr(media, "LARGE_DECODE_PIXELS", 1000)
    paths = [make_image(tmp_path / f"{i}.png", (i, 0, 0), size=(64, 48)) for i in range(6)]
    threads = [threading.Thread(target=media.load_image, args=(p, 640)) for p in paths]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert media._decode_semaphore._value == 1


@pytest.fixture(autouse=True)
def _no_real_trash(monkeypatch):
    """測試中刪除一律走隔離資料夾，不碰系統資源回收筒。"""
    monkeypatch.setattr(organizer, "_send2trash", None)


def test_correctly_signed_row_outside_output_folder_is_still_rejected(tmp_path):
    """簽章之外的第二道防線：就算簽章正確，新路徑不在輸出資料夾內、副檔名不符也不會動作。"""
    out = tmp_path / "out"
    elsewhere = touch(tmp_path / "elsewhere" / "sub" / "a.jpg", "KEEP")
    src = tmp_path / "src" / "a.jpg"
    rows = [
        ["move", src, elsewhere, "貓", "", "4", "0", out],  # 新路徑不在宣稱的輸出資料夾內
        ["move", tmp_path / "src" / "b.jpg", touch(out / "貓" / "b.exe", "EXE"), "貓", "", "3", "0", out],
    ]
    log = tmp_path / "logs" / "整理紀錄_signed.csv"
    log.parent.mkdir(parents=True)
    with open(log, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(organizer.LOG_FIELDS)
        for row in rows:
            writer.writerow(organizer._signed_row(row))

    result = undo(log)

    assert result.done == 0 and len(result.errors) == 2
    assert elsewhere.read_text() == "KEEP" and not src.exists()
