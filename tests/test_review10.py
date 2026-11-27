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


# ============================================================================ 第二批：B4、B7、B11、B14、B15、C3、C7、C8、C9、C11、C12、C13
import socket  # noqa: E402
import threading  # noqa: E402


# ---------------------------------------------------------------------------- B4：只在雲端的檔案
@pytest.fixture
def cloud_files(monkeypatch):
    """讓檔名含 cloud 的檔案看起來像 OneDrive「只限線上」（真的屬性只有 Windows 的雲端資料夾才有）。"""
    monkeypatch.setattr(media, "is_cloud_only", lambda path: "cloud" in Path(path).name)


def test_cloud_only_attribute_bits_and_default():
    assert media.CLOUD_ONLY_ATTRIBUTES == 0x1000 | 0x40000 | 0x400000  # OFFLINE、RECALL_ON_OPEN、RECALL_ON_DATA_ACCESS
    assert media.is_cloud_only(Path(__file__)) is False and media.is_cloud_only(Path("no-such-file")) is False


def test_scan_can_skip_cloud_only_files_and_stats_count_them(tmp_path, cloud_files):
    src = tmp_path / "src"
    for name in ("a.png", "cloud_b.png", "cloud_c.png"):
        make_image(src / name, (1, 2, 3))
    touch(src / "cloud_notes.txt")
    assert len(media.scan_folder(src)) == 3
    assert [p.name for p in media.scan_folder(src, skip_cloud_only=True)] == ["a.png"]
    stats = media.folder_stats(src)
    assert stats.images == 3 and stats.cloud_only == 2  # 不支援的檔案不算（本來就不會去讀）


@pytest.mark.parametrize("answer,expected", [(False, ["a.png"]), (True, ["a.png", "cloud_b.png"]), (None, None)])
def test_user_is_asked_before_cloud_files_are_downloaded(app, tmp_path, monkeypatch, cloud_files, answer, expected):
    from tkinter import messagebox

    src = tmp_path / "photos"
    make_image(src / "a.png", (250, 10, 10))
    make_image(src / "cloud_b.png", (250, 10, 10))
    asked = []
    monkeypatch.setattr(messagebox, "askyesnocancel", lambda title, text, **k: (asked.append(text), answer)[1])
    app.folder_var.set(str(src))
    pump(app.root, lambda: app.stats is not None)
    assert "只存在雲端" in app.page1.hint_var.get()
    app.start_analysis()
    assert "下載回這台電腦" in asked[0]
    if expected is None:
        assert not app.analysis_running() and not app.items  # 取消：什麼都不做
        return
    pump(app.root, lambda: app.items and all(it.analyzed for it in app.items) and not app.analysis_running())
    assert sorted(it.path.name for it in app.items) == expected


# ---------------------------------------------------------------------------- B7：縮圖快取上限
def test_thumbnail_cache_is_trimmed_oldest_first_and_keeps_decisions(tmp_path):
    src = tmp_path / "src"
    paths = [make_image(src / f"IMG_{i}.png", (250, 10, 10), size=(200, 150)) for i in range(6)]
    store = WorkStore(tmp_path / "cache")
    try:
        analyze(paths, FakeClassifier(), CATS, store=store, workers=2)
        store.save_decision(paths[0], "confirmed", "藍")
        for i, path in enumerate(paths):  # 讓「最後一次掃到」的時間有先後：0 最舊
            store._db.execute("UPDATE files SET seen_at=? WHERE path=?", (f"2024-01-0{i + 1}T00:00:00", str(path)))
        store.commit()
        total = store.thumb_bytes()
        assert total > 0 and store.enforce_thumb_limit(total) == 0          # 沒超過：不動
        removed = store.enforce_thumb_limit(total // 2)
        assert 0 < removed < 6 and store.thumb_bytes() <= total // 2
        again = {it.path.name: it.cached for it in analyze(paths, FakeClassifier(), CATS, store=store, workers=2)}
        assert again["IMG_5.png"] and not again["IMG_0.png"]                # 最舊的被清掉，最新的還在
        assert store.load_decision(paths[0]) == ("confirmed", "藍")          # 你的決定不是快取，不會被清
    finally:
        store.close()


# ---------------------------------------------------------------------------- B11：離線的第二道攔截
@pytest.fixture
def guard():
    netpolicy.install_network_guard()
    netpolicy._guard["blocked"].clear()
    yield
    netpolicy.remove_network_guard()


def test_network_guard_blocks_outbound_connections_but_not_local_ones(guard):
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    try:
        client = socket.socket()
        client.settimeout(5)
        client.connect(server.getsockname())  # 本機連線不擋
        client.close()
    finally:
        server.close()
    for target in (("203.0.113.9", 443), ("huggingface.co", 443)):
        s = socket.socket()
        with pytest.raises(netpolicy.NetworkBlocked, match="離線模式"):
            s.connect(target)
        with pytest.raises(netpolicy.NetworkBlocked):
            s.connect_ex(target)
        s.close()
    with pytest.raises(OSError):  # 走標準函式庫的高階介面也一樣被擋（不是只擋直接呼叫 connect 的）
        socket.create_connection(("203.0.113.9", 443), timeout=2)
    assert "203.0.113.9" in netpolicy.blocked_connections()


def test_network_guard_opens_only_inside_an_approved_download(guard, tmp_path, monkeypatch):
    seen = {}

    def fetch(file):
        s = socket.socket()
        s.settimeout(0.3)
        try:
            s.connect(("203.0.113.9", 9))       # 連得上連不上不重要：重點是沒有被「離線模式」擋下
        except netpolicy.NetworkBlocked:
            seen["blocked"] = True
        except OSError:
            seen["allowed"] = True
        finally:
            s.close()
        raise ConnectionError("測試不真的下載")

    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path))
    with pytest.raises(ConnectionError):
        netpolicy.download_preset("standard", tmp_path, stamp_file=tmp_path / "s.json", fetch=fetch)
    assert seen == {"allowed": True}
    s = socket.socket()
    with pytest.raises(netpolicy.NetworkBlocked):   # 下載結束（就算失敗）之後立刻恢復攔截
        s.connect(("203.0.113.9", 9))
    s.close()


def test_real_app_entry_point_installs_the_guard():
    source = (ROOT / "media_sorter" / "app.py").read_text(encoding="utf-8")
    main_body = source[source.index("def main() -> None:"):]
    assert main_body.index("netpolicy.install_network_guard()") < main_body.index("App(root)")


# ---------------------------------------------------------------------------- B14、B15
def test_start_bat_does_not_reexpand_the_folder_path():
    bat = (ROOT / "start.bat").read_text(encoding="utf-8")
    assert 'cd /d "%~dp0"' in bat and "call install.bat" in bat
    assert 'call "%~dp0' not in bat  # call 會把路徑裡的 % 再展開一次，資料夾名稱有 % 就找不到


def test_installed_copy_ignores_the_data_dir_override(monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_SORTER_HOME", str(tmp_path / "elsewhere"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    monkeypatch.setattr(config, "INSTALLED", False)
    assert config.default_data_dir() == tmp_path / "elsewhere"            # 開發／免安裝：照環境變數
    monkeypatch.setattr(config, "INSTALLED", True)
    assert config.default_data_dir() == tmp_path / "Local" / config.DATA_DIR_NAME  # 安裝版：一定是解除安裝程式認得的位置


# ---------------------------------------------------------------------------- C3：預覽不再逐檔問大小
def test_plan_preview_uses_sizes_recorded_during_analysis(app, tmp_path, monkeypatch):
    src = photos(tmp_path, 5)
    run_analysis(app, src)
    expected = sum(p.stat().st_size for p in src.glob("*.png"))
    assert app.show_step(3)
    real_stat = Path.stat
    sources = {str(p) for p in src.glob("*.png")}
    calls = []

    def counting(self, *args, **kwargs):
        if str(self) in sources:
            calls.append(str(self))
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", counting)
    app.refresh_plan()
    assert app.plan.total_bytes == expected and len(app.plan.ops) == 5
    assert calls == []  # 以前：每次重新整理預覽，每個原檔被問兩次大小


def test_free_space_check_accepts_a_known_total(tmp_path, monkeypatch):
    a = touch(tmp_path / "src" / "a.jpg", "A")
    ops = organizer.plan_operations([(a, "貓", D)], tmp_path / "out")
    monkeypatch.setattr(organizer.shutil, "disk_usage", lambda p: __import__("shutil")._ntuple_diskusage(10**12, 0, 10**9))
    assert organizer.free_space_problem(ops, need=10) is None
    assert "空間不足" in organizer.free_space_problem(ops, need=5 * 10**9)


# ---------------------------------------------------------------------------- C7、C8、C9、C11、C12、C13
def test_starting_analysis_keeps_messages_that_belong_to_no_run(app, tmp_path):
    app.folder_var.set(str(photos(tmp_path)))
    handled = []
    original = app._handle_message
    app._handle_message = lambda kind, *args: (handled.append(kind), original(kind, *args))[1]
    app.queue.put(("viewer", 999, None, "讀取中的放大視窗"))  # 還沒被處理的「放大檢視」結果
    app.queue.put(("status", app._run_id, "上一輪殘留的訊息"))
    app.start_analysis()
    pump(app.root, lambda: all(it.analyzed for it in app.items) and not app.analysis_running())
    assert "viewer" in handled  # 以前：開始辨識時整個佇列被清空，放大視窗永遠停在「讀取中」
    assert "上一輪殘留的訊息" not in app.status_var.get()


def test_stop_during_scan_ends_before_analysis(tmp_path):
    src = tmp_path / "src"
    for i in range(5):
        make_image(src / f"{i}.png", (1, 2, 3))
    stop = threading.Event()
    stop.set()
    assert media.scan_folder(src, stop_event=stop) == []
    assert len(media.scan_folder(src, stop_event=threading.Event())) == 5


def test_stop_while_the_model_is_loading_never_starts_analysis(app, tmp_path):
    gate = threading.Event()

    def slow_factory(preset):
        gate.wait(10)
        return FakeClassifier(preset)

    app.classifier_factory = slow_factory
    app.folder_var.set(str(photos(tmp_path)))
    app.start_analysis()
    pump(app.root, lambda: "載入 AI 模型" in app.status_var.get())
    app.stop_analysis()
    assert "這一步結束後就會停" in app.status_var.get()  # 以前：按了沒有任何反應
    gate.set()
    pump(app.root, lambda: not app.analysis_running())
    assert "已停止" in app.status_var.get() and not any(it.analyzed for it in app.items)


def test_changing_categories_without_a_loaded_model_says_suggestions_are_stale(app, tmp_path):
    run_analysis(app, photos(tmp_path))
    app.classifier = None  # 例如剛按過「檢查並修復 AI 模型…」
    app.set_categories([Category("紅"), Category("綠"), Category("藍"), Category("新分類")])
    assert "AI 的建議還是舊的" in app.status_var.get()


def test_closing_during_a_model_download_asks_first(app, monkeypatch):
    from tkinter import messagebox

    asked = []
    monkeypatch.setattr(messagebox, "askyesno", lambda title, text, **k: (asked.append(text), False)[1])
    app.model_task_running = True
    app.on_close()
    assert "下載會中斷" in asked[0] and app.root.winfo_exists()  # 回答「否」：不關
    app.model_task_running = False


def test_error_log_is_rotated_and_its_full_path_is_shown(app, tmp_path, monkeypatch):
    import media_sorter.app as app_module

    logs = tmp_path / "logs"
    monkeypatch.setattr(app_module, "ERROR_LOG_LIMIT", 200)
    app.log_error("x" * 300)
    app.log_error("第二筆")
    assert (logs / "error.log.1").read_text(encoding="utf-8").count("x") == 300
    assert "第二筆" in (logs / "error.log").read_text(encoding="utf-8") and "xxx" not in (logs / "error.log").read_text(encoding="utf-8")
    shown = []
    monkeypatch.setattr(app_module, "show_problem", lambda *a, **k: shown.append(a))
    app._show_error("測試")
    assert str(logs / "error.log") in shown[0][2]  # 完整位置，不是只寫 logs\error.log


def test_remaining_time_estimate(app, monkeypatch):
    import media_sorter.app as app_module

    assert app_module.format_remaining(30) == "剩不到 1 分鐘"
    assert app_module.format_remaining(600) == "大約還要 10 分鐘" and "小時" in app_module.format_remaining(9000)
    clock = [1000.0]
    monkeypatch.setattr(app_module.time, "monotonic", lambda: clock[0])
    app._eta_start = None
    assert app._eta_text(0, 1000) == ""          # 剛開始：不亂猜
    clock[0] += 2
    assert app._eta_text(3, 1000) == ""          # 樣本太少
    clock[0] += 58
    assert "大約還要 9 分鐘" in app._eta_text(100, 1000)  # 60 秒做了 100 個，還有 900 個
    assert app._eta_text(1000, 1000) == ""
