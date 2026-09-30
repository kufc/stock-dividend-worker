"""「先複製、確認後再移除原檔」模型的安全性測試。

核心保證：任何一次刪除（復原移除複本、移除原檔）都要求「另一份內容完全相同的檔案仍然存在」，
所以不論紀錄檔寫到一半、被竄改或被重新放回，都不會讓任何一份內容從磁碟上消失。
"""

import csv
import errno
import io
import os
import random
import shutil
from datetime import datetime
from pathlib import Path

import pytest

from media_sorter import organizer
from media_sorter.organizer import (
    execute, free_space_problem, latest_log, plan_operations, read_log, remove_originals, undo,
)

D = datetime(2024, 1, 1)


def touch(path: Path, text: str = "x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


@pytest.fixture
def trash(tmp_path, monkeypatch):
    """模擬資源回收筒：被送進去的檔案搬到這個資料夾，方便檢查。"""
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


def roots(tmp_path, source="src", output="out") -> list[str]:
    return [organizer._canonical(tmp_path / source), organizer._canonical(tmp_path / output)]


def write_log(path: Path, rows: list[list]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    buf = io.StringIO(newline="")
    writer = csv.writer(buf)
    writer.writerow(organizer.LOG_FIELDS)
    writer.writerows(rows)
    path.write_text(buf.getvalue(), encoding="utf-8-sig")
    return path


def alias_of(path: Path) -> Path | None:
    """同一個檔案的另一條路徑：不分大小寫的檔案系統（Windows、macOS）用大小寫不同的路徑，
    否則用一個指向該資料夾的符號連結；兩者都做不到就回傳 None（測試會略過）。"""
    upper = path.with_name(path.name.swapcase())
    try:
        if upper != path and upper.exists() and os.path.samefile(upper, path):
            return upper
    except OSError:
        pass
    link = path.parent.parent / f"alias-{path.parent.name}"
    try:
        if not link.exists():
            link.symlink_to(path.parent, target_is_directory=True)
        return link / path.name
    except (OSError, NotImplementedError):
        return None


# ---------------------------------------------------------------------------- 整理：原檔完全不動
def test_organize_never_touches_originals(tmp_path):
    a = touch(tmp_path / "src" / "a.jpg", "A")
    before = (a.read_bytes(), a.stat().st_mtime_ns)
    ops, result = organize(tmp_path, [a])
    assert result.done == 1
    assert (a.read_bytes(), a.stat().st_mtime_ns) == before
    assert ops[0].dst.read_text() == "A"


def test_log_write_failure_stops_but_originals_are_safe(tmp_path, monkeypatch):
    files = [touch(tmp_path / "src" / f"{i}.jpg", str(i)) for i in range(3)]

    def broken_fsync(fd):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(organizer.os, "fsync", broken_fsync)
    _, result = organize(tmp_path, files)
    assert result.aborted and "手動刪除" in result.errors[0][1]
    assert [f.read_text() for f in files] == ["0", "1", "2"]


def test_existing_partial_file_is_never_clobbered(tmp_path):
    existing = touch(tmp_path / "out" / "貓" / "貓_001.jpg.partial", "KEEP")
    a = touch(tmp_path / "src" / "a.jpg", "NEW")
    _, result = organize(tmp_path, [a])
    assert result.done == 1 and existing.read_text() == "KEEP"


def test_free_space_check(tmp_path, monkeypatch):
    a = touch(tmp_path / "src" / "a.jpg", "A" * 1000)
    ops = plan_operations([(a, "貓", D)], tmp_path / "out")
    assert free_space_problem(ops) is None
    monkeypatch.setattr(organizer.shutil, "disk_usage", lambda p: shutil._ntuple_diskusage(10, 10, 10))
    assert "空間不足" in free_space_problem(ops)


# ---------------------------------------------------------------------------- 復原：只移除內容相同的複本
def test_undo_keeps_copy_when_original_is_gone(tmp_path, trash):
    a = touch(tmp_path / "src" / "a.jpg", "ONLY")
    ops, result = organize(tmp_path, [a])
    a.unlink()
    undone = undo(result.log_path)
    assert undone.done == 0 and not undone.errors
    assert ops[0].dst.read_text() == "ONLY"
    assert any("原檔已不在" in w for w in undone.warnings)


def test_undo_keeps_modified_copy(tmp_path, trash):
    a = touch(tmp_path / "src" / "a.jpg", "A")
    ops, result = organize(tmp_path, [a])
    ops[0].dst.write_text("EDITED")
    undone = undo(result.log_path)
    assert undone.done == 0 and ops[0].dst.read_text() == "EDITED"


def test_undo_content_check_is_not_fooled_by_cache(tmp_path, monkeypatch):
    a = touch(tmp_path / "src" / "a.jpg", "AAAA")
    ops, result = organize(tmp_path, [a])
    calls = []
    monkeypatch.setattr(organizer, "_send2trash", lambda p: (calls.append(p), (_ for _ in ()).throw(OSError("鎖住"))))
    assert undo(result.log_path).errors  # 第一次：內容相同、嘗試移除但失敗，留在紀錄中重試
    assert ops[0].dst.exists()  # 失敗後改回原檔名
    st = a.stat()
    a.write_text("BBBB")  # 內容不同，大小與修改時間都相同
    os.utime(a, ns=(st.st_atime_ns, st.st_mtime_ns))
    calls.clear()
    undo(result.log_path)
    assert not calls and ops[0].dst.read_text() == "AAAA"


def test_undo_failure_is_retried_and_other_rows_continue(tmp_path, trash, monkeypatch):
    a = touch(tmp_path / "src" / "a.jpg", "A")
    b = touch(tmp_path / "src" / "b.jpg", "B")
    ops, result = organize(tmp_path, [a, b])
    real = organizer._send2trash
    monkeypatch.setattr(organizer, "_send2trash",
                        lambda p: (_ for _ in ()).throw(TypeError("意外")) if ops[0].dst.stem in Path(p).name else real(p))
    first = undo(result.log_path)
    assert first.done == 1 and len(first.errors) == 1 and not ops[1].dst.exists()
    assert ops[0].dst.exists()  # 失敗後改回原檔名
    monkeypatch.setattr(organizer, "_send2trash", real)
    assert latest_log(tmp_path / "logs") == result.log_path  # 只留下失敗的那一列
    assert undo(result.log_path).done == 1 and not ops[0].dst.exists()


def test_undo_quarantines_when_no_recycle_bin(tmp_path, monkeypatch):
    monkeypatch.setattr(organizer, "_send2trash", None)
    a = touch(tmp_path / "src" / "a.jpg", "A")
    ops, result = organize(tmp_path, [a])
    undone = undo(result.log_path)
    assert undone.done == 1 and not ops[0].dst.exists()
    assert list((tmp_path / "logs" / organizer.QUARANTINE_DIR).rglob("*.jpg"))[0].read_text() == "A"


# ---------------------------------------------------------------------------- 移除原檔
def test_remove_originals_only_when_copy_is_identical(tmp_path, trash):
    a = touch(tmp_path / "src" / "a.jpg", "A")
    b = touch(tmp_path / "src" / "b.jpg", "B")
    c = touch(tmp_path / "src" / "c.jpg", "C")
    ops, result = organize(tmp_path, [a, b, c])
    ops[1].dst.write_text("EDITED COPY")  # 複本被改過
    ops[2].dst.unlink()  # 複本不見了

    removed = remove_originals(result.log_path)

    assert removed.done == 1 and not a.exists()
    assert b.read_text() == "B" and c.read_text() == "C"  # 另外兩個原檔保留
    assert len(removed.errors) == 2
    assert latest_log(tmp_path / "logs") == result.log_path  # 失敗的兩列留著可以重試


def test_remove_originals_requires_recycle_bin(tmp_path, monkeypatch):
    monkeypatch.setattr(organizer, "_send2trash", None)
    a = touch(tmp_path / "src" / "a.jpg", "A")
    _, result = organize(tmp_path, [a])
    with pytest.raises(OSError, match="資源回收筒"):
        remove_originals(result.log_path)
    assert a.read_text() == "A"


def test_after_removing_originals_undo_keeps_copies(tmp_path, trash):
    a = touch(tmp_path / "src" / "a.jpg", "A")
    ops, result = organize(tmp_path, [a])
    assert remove_originals(result.log_path).done == 1
    assert latest_log(tmp_path / "logs") is None  # 已完成的紀錄不會再被選中
    saved = result.log_path.with_name(result.log_path.name.replace("整理紀錄_", organizer.FINISHED_PREFIX))
    shutil.copy(saved, result.log_path)  # 就算把紀錄放回來再按復原
    undone = undo(result.log_path)
    assert undone.done == 0 and ops[0].dst.read_text() == "A"  # 唯一的一份不會被刪


def test_same_file_through_another_path_is_never_deleted(tmp_path, trash):
    """兩個不同的路徑其實指向同一個檔案（這裡用目錄連結；Windows 上也包含目錄連接點、大小寫不同的路徑）：
    那不是「另一份」，絕不能因為「內容相同」就刪掉——刪掉就等於刪掉唯一的一份。"""
    src = touch(tmp_path / "src" / "貓" / "a.jpg", "ONLY")
    dst = alias_of(src)  # 字串不同，實際上就是 src
    if dst is None:
        pytest.skip("此環境無法建立同一個檔案的別名路徑")
    row = ["copy", str(src), str(dst), "貓", "", "4", organizer._canonical(tmp_path / "src"),
           organizer._canonical(dst.parent.parent)]
    log = write_log(tmp_path / "logs" / "整理紀錄_20240101_000000.csv", [row])
    undo(log)
    assert src.read_text() == "ONLY"
    write_log(log, [row])
    remove_originals(log)
    assert src.read_text() == "ONLY"


# ---------------------------------------------------------------------------- 紀錄檔損壞、竄改、重放
def test_byte_truncation_anywhere_never_crashes_or_loses_data(tmp_path, trash):
    a = touch(tmp_path / "src" / "a.jpg", "A")
    b = touch(tmp_path / "src" / "b.jpg", "B")
    ops, result = organize(tmp_path, [a, b], category="貓咪")
    raw = result.log_path.read_bytes()
    for cut in range(len(raw)):
        for dst in (o.dst for o in ops):  # 每次都從「複本都在」的狀態開始
            if not dst.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(a if dst == ops[0].dst else b, dst)
        result.log_path.write_bytes(raw[:cut])
        for func in (undo, remove_originals):
            try:
                func(result.log_path)
            except ValueError:  # 只有檔頭都不完整時才會「無法辨識」，而且此時完全沒有動作
                pass
            for p in result.log_path.parent.glob("*.csv"):
                if p != result.log_path:
                    p.unlink()
            if not result.log_path.exists():
                result.log_path.write_bytes(raw[:cut])
        for content in ("A", "B"):  # 兩份內容在資源回收筒以外至少各留一份
            outside = [p for p in tmp_path.rglob("*.jpg") if trash not in p.parents and p.read_text() == content]
            assert outside, (cut, content)
        for p in trash.iterdir():  # 從回收筒「救回」，準備下一輪
            name = p.name.split("_", 1)[1]
            original = a if name == "a.jpg" else b
            if not original.exists():
                shutil.move(p, original)
            else:
                p.unlink()


def test_fuzzed_forged_logs_never_lose_any_content(tmp_path, trash):
    """隨機產生大量偽造紀錄（任意路徑配對、內容相同的檔案、硬連結、不存在的路徑、壞掉的欄位），
    執行復原與移除原檔後，每一種內容在資源回收筒以外都至少還留著一份。"""
    rng = random.Random(1234)
    root = tmp_path / "world"
    for round_no in range(40):
        if root.exists():
            shutil.rmtree(root)
        files = []
        for i in range(8):
            f = touch(root / f"d{i % 3}" / f"f{i}.jpg", rng.choice(["P", "Q", "R", f"U{i}"]))
            files.append(f)
        link = root / "d0" / "link.jpg"
        os.link(files[0], link)
        files.append(link)
        for f in list(files):  # 同一個檔案的其他路徑（大小寫不同或目錄連結）
            alias = alias_of(f)
            if alias is not None:
                files.append(alias)
        contents = {f.read_text() for f in files}
        candidates = files + [root / "nope.jpg", Path("relative.jpg"), root / "d1" / "f1.png"]
        rows = []
        # 每份紀錄的來源／輸出資料夾選一次：大多是正確的（讓大部分回合真的執行到刪除），少數故意指到別處
        src_root = rng.choice([organizer._canonical(root)] * 4 + [str(tmp_path), str(root / "d0")])
        out_root = rng.choice([organizer._canonical(root)] * 4 + [str(tmp_path), str(root / "d1")])
        for _ in range(12):
            src, dst = rng.choice(candidates), rng.choice(candidates)
            action = rng.choice(["copy", "copy", "copy", "move", "delete", ""])
            rows.append([action, str(src), str(dst), "貓", "", rng.choice(["1", "", "x"]), src_root, out_root])
        log = write_log(tmp_path / "logs" / "整理紀錄_20240101_000000.csv", rows)
        for func in (undo, remove_originals, undo):
            if not log.exists():
                write_log(log, rows)
            try:
                func(log)
            except ValueError:  # 來源／輸出資料夾不一致的紀錄會整份拒絕
                pass
        for content in contents:
            outside = [p for p in root.rglob("*.jpg") if not p.is_symlink() and p.is_file() and p.read_text() == content]
            assert outside, (round_no, content)
    assert len(list(trash.iterdir())) >= 10  # 確實有觸發刪除，不是每次都剛好什麼都沒做


def test_replaying_old_log_is_harmless(tmp_path, trash):
    a = touch(tmp_path / "src" / "a.jpg", "OLD")
    ops, result = organize(tmp_path, [a])
    saved = result.log_path.read_bytes()
    assert undo(result.log_path).done == 1
    touch(ops[0].dst, "NEW PHOTO")  # 輸出位置出現內容不同的新照片
    result.log_path.write_bytes(saved)  # 把舊紀錄放回
    replay = undo(result.log_path)
    assert replay.done == 0 and ops[0].dst.read_text() == "NEW PHOTO" and a.read_text() == "OLD"


def test_log_resaved_as_big5_and_newline_category(tmp_path, trash):
    a = touch(tmp_path / "src" / "a.jpg", "A")
    ops, result = organize(tmp_path, [a], category="cat\n dog")
    text = result.log_path.read_text(encoding="utf-8-sig")
    result.log_path.write_bytes(text.encode("cp950", errors="replace"))
    assert undo(result.log_path).done == 1 and not ops[0].dst.exists() and a.exists()


def test_latest_log_ignores_other_files(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "整理紀錄_20240101_000000.csv.無法驗證的列.csv").write_text("x")
    (logs / "整理紀錄_複本.csv").write_text("x")
    (logs / "已復原_20240101_000000.csv").write_text("x")
    assert latest_log(logs) is None


def test_read_log_skips_garbage(tmp_path):
    log = tmp_path / "整理紀錄_x.csv"
    log.write_text(",".join(organizer.LOG_FIELDS) + "\ncopy,\n,,\n", encoding="utf-8-sig")
    assert read_log(log) == []
