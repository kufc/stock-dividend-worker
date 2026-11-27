"""第十輪外部審查（39 條）裡這一輪處理的項目。編號對應審查報告：B1 追問、B2、B3、B5、B6、B8、B10、C1、C4、C10、A9。"""

import json
import os
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

import pytest
from PIL import Image

from conftest import copy_all, pump, run_analysis
from fakes import FakeClassifier
from helpers import make_image
from media_sorter import analysis, config, installer, media, netpolicy, organizer
from media_sorter.analysis import analyze
from media_sorter.config import Category
from media_sorter.workdb import DB_NAME, WorkStore

ROOT = Path(__file__).resolve().parent.parent
CATS = [Category("紅"), Category("綠"), Category("藍")]
D = datetime(2024, 1, 1)


def touch(path: Path, text: str = "x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


# ---------------------------------------------------------------------------- B1 追問：長路徑、被重新導向的資料夾
def test_paths_too_long_for_the_recycle_bin_are_refused():
    assert organizer.path_too_long_for_bin("C:\\" + "a" * 100) is None
    assert organizer.path_too_long_for_bin("C:\\" + "a" * (organizer.MAX_TRASH_PATH - 3)) is None
    problem = organizer.path_too_long_for_bin("C:\\" + "a" * 300)
    assert "路徑太長" in problem and "永久刪除" in problem
    # 留了餘裕：移除前會暫時把檔名加上「 (整理前)」，不能卡在剛好 260
    assert organizer.MAX_TRASH_PATH + len(f" ({organizer.STAGE_MARK_ORIGINAL} 99)") < 260


@pytest.mark.skipif(sys.platform != "win32", reason="讀 Windows 的登錄檔")
def test_windows_redirected_known_folder_settings_are_honoured(tmp_path):
    """「圖片」資料夾被重新導向時，回收筒設定記在 BitBucket\\KnownFolder\\{代號}，不在磁碟那一筆。"""
    import ctypes
    import winreg
    from ctypes import wintypes

    pictures = "{33E28130-4E1E-4676-835A-98395C3BC3BB}"

    class GUID(ctypes.Structure):
        _fields_ = [("a", wintypes.DWORD), ("b", wintypes.WORD), ("c", wintypes.WORD), ("d", ctypes.c_ubyte * 8)]

    guid, found = GUID(), ctypes.c_wchar_p()
    ctypes.windll.ole32.CLSIDFromString(pictures, ctypes.byref(guid))
    if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(found)) != 0 or not found.value:
        pytest.skip("這個環境查不到「圖片」資料夾")
    inside, outside = found.value + "\\2024\\IMG_1.jpg", str(tmp_path / "IMG_1.jpg")
    assert organizer._known_folder_bin_settings(inside) == (False, None)
    key_path = r"Software\Microsoft\Windows\CurrentVersion\Explorer\BitBucket\KnownFolder" + "\\" + pictures
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path) as key:
        winreg.SetValueEx(key, "NukeOnDelete", 0, winreg.REG_DWORD, 1)
        winreg.SetValueEx(key, "MaxCapacity", 0, winreg.REG_DWORD, 500)
    try:
        assert organizer._known_folder_bin_settings(inside) == (True, 500)
        assert organizer._known_folder_bin_settings(inside.upper()) == (True, 500)   # 不分大小寫
        assert organizer._known_folder_bin_settings(outside) == (False, None)        # 不在那個資料夾裡：不受影響
        assert organizer._known_folder_bin_settings(found.value + "2\\x.jpg") == (False, None)  # 只是名稱開頭一樣
    finally:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key_path)


# ---------------------------------------------------------------------------- B2：資料庫暫時打不開 ≠ 壞掉
def test_locked_database_is_not_thrown_away(tmp_path, monkeypatch):
    """被別的程式鎖住時，以前會把資料庫（含使用者改過的分類）擱置、開一個空的。現在：往上丟，資料庫原封不動。"""
    folder = tmp_path / "cache"
    store = WorkStore(folder)
    store.save_decision(touch(tmp_path / "a.jpg"), "confirmed", "紅")
    store.close()
    real = WorkStore._connect

    def locked(self):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(WorkStore, "_connect", locked)
    with pytest.raises(sqlite3.OperationalError):
        WorkStore(folder)
    monkeypatch.setattr(WorkStore, "_connect", real)
    assert not list(folder.glob("無法讀取_*"))  # 沒有被改名擱置
    again = WorkStore(folder)
    assert again.stats() == (0, 1) and again.recreated_from is None
    again.close()


def test_corrupt_database_that_cannot_be_renamed_is_not_deleted(tmp_path, monkeypatch):
    folder = tmp_path / "cache"
    folder.mkdir()
    (folder / DB_NAME).write_bytes(b"garbage" * 500)
    monkeypatch.setattr(os, "replace", lambda *a: (_ for _ in ()).throw(PermissionError(13, "使用中")))
    with pytest.raises(OSError):
        WorkStore(folder)
    assert (folder / DB_NAME).read_bytes() == b"garbage" * 500  # 以前：改名失敗就直接刪掉


def test_app_runs_without_the_store_and_says_so(app_factory, monkeypatch):
    from tkinter import messagebox

    import media_sorter.workdb as workdb

    monkeypatch.setattr(workdb.WorkStore, "_connect",
                        lambda self: (_ for _ in ()).throw(sqlite3.OperationalError("database is locked")))
    app = app_factory()
    assert app.store is None
    shown = []
    monkeypatch.setattr(messagebox, "showinfo", lambda title, text, **k: shown.append(text))
    app.show_startup_notices()
    assert "之前保存的資料沒有被動到" in shown[0]


def test_old_database_gets_the_new_column_without_losing_decisions(tmp_path):
    folder = tmp_path / "cache"
    store = WorkStore(folder)
    target = touch(tmp_path / "a.jpg")
    store.save_decision(target, "confirmed", "紅")
    store._db.execute("ALTER TABLE files DROP COLUMN incomplete")  # 變回 1.1.0 剛發行時的樣子
    store._db.commit()
    store.close()
    again = WorkStore(folder)
    assert again.recreated_from is None and again.load_decision(target) == ("confirmed", "紅")
    assert "incomplete" in {row[1] for row in again._db.execute("PRAGMA table_info(files)")}
    again.close()


# ---------------------------------------------------------------------------- B5：設定檔壞掉
@pytest.mark.parametrize("content", [b"", b"{ not json", b"\xff\xfe\x00bad", b"[1, 2, 3]", b'{"categories": "x"}',
                                      b'{"categories": [1, "x", {"name": "\xe8\xb2\x93"}]}'])
def test_corrupt_settings_and_categories_never_stop_the_program(tmp_path, content):
    config.LOAD_PROBLEMS.clear()
    settings, categories = tmp_path / "settings.json", tmp_path / "categories.json"
    settings.write_bytes(content)
    categories.write_bytes(content)
    loaded = config.load_settings(settings)       # 以前：json.JSONDecodeError 一路丟到啟動失敗，還叫人重新安裝
    assert loaded["batch_size"] == config.DEFAULT_SETTINGS["batch_size"]
    names = [c.name for c in config.load_categories(categories)]
    assert names  # 一定有可用的分類
    unreadable = content in (b"", b"{ not json", b"\xff\xfe\x00bad")
    if unreadable:
        assert len(config.LOAD_PROBLEMS) == 2 and "改用預設值" in config.LOAD_PROBLEMS[0]
        kept = sorted(p.name for p in tmp_path.iterdir())
        assert all(".壞掉_" in name for name in kept) and len(kept) == 2   # 壞掉的那份改名留著，內容沒丟
        assert any(p.read_bytes() == content for p in tmp_path.iterdir())
    else:
        assert not config.LOAD_PROBLEMS
    config.LOAD_PROBLEMS.clear()


def test_settings_are_fsynced_before_replacing_the_old_file(tmp_path, monkeypatch):
    order = []
    real_fsync, real_replace = os.fsync, os.replace
    monkeypatch.setattr(config.os, "fsync", lambda fd: (order.append("fsync"), real_fsync(fd))[1])
    monkeypatch.setattr(config.os, "replace", lambda a, b: (order.append("replace"), real_replace(a, b))[1])
    config.save_settings(dict(config.DEFAULT_SETTINGS), tmp_path / "settings.json")
    assert order == ["fsync", "replace"]
    assert json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))["model"] == "auto"


def test_startup_problems_are_shown_once(app, monkeypatch):
    from tkinter import messagebox

    shown = []
    monkeypatch.setattr(messagebox, "showinfo", lambda title, text, **k: shown.append(text))
    config.LOAD_PROBLEMS.append("「settings.json」讀不出來（JSONDecodeError），這次改用預設值。")
    app.show_startup_notices()
    app.show_startup_notices()
    assert len(shown) == 1 and "settings.json" in shown[0] and not config.LOAD_PROBLEMS


# ---------------------------------------------------------------------------- B6：被強制結束留下的暫存檔
def test_stale_partial_files_of_ours_are_cleaned_but_nothing_else(tmp_path):
    folder = tmp_path / "out" / "貓"
    stale = touch(folder / f"貓_009.jpg.abc123{organizer.PARTIAL_SUFFIX}", "half")
    fresh = touch(folder / f"貓_010.jpg.def456{organizer.PARTIAL_SUFFIX}", "half")
    users = touch(folder / "我的檔案.jpg.partial", "KEEP")      # 使用者自己的檔案（別的程式的暫存檔也長這樣）
    photo = touch(folder / "貓_001.jpg", "PHOTO")
    old = time.time() - organizer.STALE_PARTIAL_SECONDS - 60
    for path in (stale, users, photo):
        os.utime(path, (old, old))
    a = touch(tmp_path / "src" / "a.jpg", "A")
    ops = organizer.plan_operations([(a, "貓", D)], tmp_path / "out")
    result = organizer.execute(ops, tmp_path / "logs", source_dir=tmp_path / "src", output_dir=tmp_path / "out")
    assert result.done == 1
    assert not stale.exists()                                   # 上次留下的：清掉
    assert fresh.exists()                                       # 還很新（保守起見不碰）
    assert users.read_text() == "KEEP" and photo.read_text() == "PHOTO"


# ---------------------------------------------------------------------------- B8：修復模型不會刪到快取以外
@pytest.mark.skipif(sys.platform == "win32", reason="需要建立符號連結")
def test_repair_never_deletes_through_a_link_that_leaves_the_cache(tmp_path):
    cache = tmp_path / "hub"
    outside = touch(tmp_path / "elsewhere" / "my-model.bin", "MINE")
    inside_blob = touch(cache / "models--x" / "blobs" / "abc", "blob")
    link_out = cache / "models--x" / "snapshots" / "r" / "a.bin"
    link_in = cache / "models--x" / "snapshots" / "r" / "b.bin"
    link_out.parent.mkdir(parents=True)
    link_out.symlink_to(outside)
    link_in.symlink_to(inside_blob)
    netpolicy._remove_file(link_out, cache)
    netpolicy._remove_file(link_in, cache)
    assert not link_out.is_symlink() and outside.read_text() == "MINE"   # 只拆連結
    assert not link_in.is_symlink() and not inside_blob.exists()          # 快取裡的照舊一起刪


# ---------------------------------------------------------------------------- B3：免安裝版只接受 Python 3.12
def test_portable_install_requires_python_312_and_does_not_upgrade_pip(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(installer, "pip", lambda *args: (calls.append(args), True)[1])
    monkeypatch.setattr(installer.sys, "platform", "win32")
    monkeypatch.setattr(installer, "hashes_apply", lambda: False)   # 例如電腦上只有 Python 3.13
    assert installer.main() == 1
    assert calls == [] and "Python 3.12" in capsys.readouterr().out  # 什麼都沒裝，而且講清楚要什麼
    source = (ROOT / "media_sorter" / "installer.py").read_text(encoding="utf-8")
    assert 'pip("--upgrade"' not in source
    bat = (ROOT / "install.bat").read_text(encoding="utf-8")
    assert "==(3,12)" in bat and "3.11" not in bat and "3.13" not in bat and "3.10" not in bat


# ---------------------------------------------------------------------------- A9：沒傳完的圖
def test_truncated_images_are_detected_from_the_file_tail(tmp_path):
    for suffix in (".jpg", ".png"):
        full = tmp_path / f"full{suffix}"
        Image.effect_noise((600, 400), 60).convert("RGB").save(full, quality=92)
        cut = tmp_path / f"cut{suffix}"
        cut.write_bytes(full.read_bytes()[: full.stat().st_size * 2 // 3])
        assert not media.looks_incomplete(full) and media.looks_incomplete(cut)
    assert not media.looks_incomplete(make_image(tmp_path / "a.bmp", (1, 2, 3)))   # 其他格式不判斷
    assert not media.looks_incomplete(tmp_path / "missing.jpg")


def test_truncated_image_goes_to_needs_checking_and_survives_restart(tmp_path):
    src = tmp_path / "src"
    good = make_image(src / "good.jpg", (250, 10, 10), size=(300, 200))
    cut = src / "cut.jpg"
    Image.new("RGB", (600, 400), (250, 10, 10)).save(cut, quality=95)
    cut.write_bytes(cut.read_bytes()[: cut.stat().st_size // 2])
    store = WorkStore(tmp_path / "cache")
    try:
        for attempt in ("第一次", "重開後"):
            items = {it.path.name: it for it in analyze([good, cut], FakeClassifier(), CATS, store=store, workers=2)}
            assert items["cut.jpg"].analyzed and items["cut.jpg"].incomplete, attempt
            assert items["cut.jpg"].is_low(CATS, 0.5, 0.0), attempt        # 就算 AI 很有把握也要人看一下
            assert not items["good.jpg"].incomplete and not items["good.jpg"].is_low(CATS, 0.5, 0.0), attempt
        assert items["cut.jpg"].cached
    finally:
        store.close()


# ---------------------------------------------------------------------------- 介面：C1、C4、C10、B10
def photos(tmp_path, n=4):
    src = tmp_path / "photos"
    for i in range(n):
        make_image(src / f"IMG_{i}.png", (250, 10, 10))
    return src


def test_double_click_on_start_does_not_stop_the_analysis(app, tmp_path):
    app.folder_var.set(str(photos(tmp_path)))
    app.toggle_analysis()
    assert app.analysis_running()
    app.toggle_analysis()                        # 連點兩下的第二下
    assert not app.stop_event.is_set()
    pump(app.root, lambda: all(it.analyzed for it in app.items) and not app.analysis_running())
    # 過了那一小段時間之後，同一顆按鈕就是「停止」（用「正在辨識」的狀態直接檢查，不靠時間差）
    app.analysis_running = lambda: True
    app._started_at = time.monotonic() - 5
    app.toggle_analysis()
    assert app.stop_event.is_set()
    del app.analysis_running


def test_double_click_on_copy_does_not_stop_the_copy(app, tmp_path):
    run_analysis(app, photos(tmp_path))
    assert app.show_step(3)
    app.toggle_copy()
    assert app.organizing
    app.toggle_copy()
    assert not app.copy_stop.is_set()
    pump(app.root, lambda: not app.organizing and app.step == 4)
    assert len(list((tmp_path / "photos" / "已分類").rglob("*.png"))) == 4


def test_output_cannot_be_changed_while_copying(app, tmp_path, monkeypatch):
    from tkinter import filedialog

    run_analysis(app, photos(tmp_path))
    assert app.show_step(3)
    before = app.output_var.get()
    monkeypatch.setattr(filedialog, "askdirectory", lambda **k: pytest.fail("複製中不應該開選擇資料夾的視窗"))
    app.start_copy()
    assert app.organizing and app.page3.browse_btn.instate(["disabled"])
    app.choose_output()
    assert app.output_var.get() == before
    pump(app.root, lambda: not app.organizing and app.step == 4)
    assert app.page3.browse_btn.instate(["!disabled"])


def test_output_folder_equal_to_source_is_refused(app, tmp_path):
    src = photos(tmp_path)
    run_analysis(app, src)
    assert app.show_step(3)
    app.output_var.set(str(src))
    app.refresh_plan()
    assert app.plan.problem and "不能就是來源資料夾" in app.plan.problem and not app.plan.ops
    app.start_copy()
    assert not app.organizing and not any(p.is_dir() for p in src.iterdir())
    app.output_var.set(str(src / "已分類"))
    app.refresh_plan()
    assert app.plan.problem is None and len(app.plan.ops) == 4


def test_retry_uses_the_categories_as_they_are_now(app, tmp_path, monkeypatch):
    """複製失敗後回去把那個檔案改成別的分類（或略過），再按重試：要照現在的分類，不是失敗當時的計畫。"""
    src = photos(tmp_path, 3)
    run_analysis(app, src)
    real = organizer._copy_no_clobber
    monkeypatch.setattr(organizer, "_copy_no_clobber",
                        lambda s, d: (_ for _ in ()).throw(PermissionError(13, "使用中")) if Path(s).name != "IMG_0.png" else real(s, d))
    assert app.show_step(3)
    app.start_copy()
    pump(app.root, lambda: not app.organizing and app.step == 4)
    assert len(app.run_info.failures) == 2
    by_name = {it.path.name: it for it in app.items}
    by_name["IMG_1.png"].chosen = "藍"
    by_name["IMG_1.png"].status = analysis.CONFIRMED      # 改成另一個分類
    by_name["IMG_2.png"].status = analysis.SKIPPED        # 這個不要了
    monkeypatch.setattr(organizer, "_copy_no_clobber", real)
    app.retry_remaining()
    pump(app.root, lambda: not app.organizing)
    out = src / "已分類"
    copies = sorted(p.relative_to(out).as_posix() for p in out.rglob("*.png"))
    assert copies == ["紅/IMG_0.png", "藍/IMG_1.png"]   # IMG_1 進了新的分類；IMG_2 被略過所以沒複製
    assert all(p.exists() for p in src.glob("IMG_*.png"))
