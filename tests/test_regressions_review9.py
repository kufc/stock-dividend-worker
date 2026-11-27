"""第九輪外部審查（P0）：「必須留下的那一份」在保護期間不能被刪除、改名或被別的檔案取代。

原本兩份檔案用同一種開法（FILE_SHARE_READ | FILE_SHARE_DELETE）：禁止寫入，但允許刪除與改名。
審查者握著保護控制代碼的同時把「該留下的複本」刪掉，刪除成功。
現在留下的那一份不給 FILE_SHARE_DELETE；送出前還會再確認它仍是同一個檔案（其他系統只有這個檢查）。

Windows 的共用模式只有 Windows（或 Wine）會強制執行，所以分成兩組：
- 所有系統：送出前「留下的那一份」已經被刪除／改名／取代 → 中止、什麼都不移除
- 只在 Windows：握著保護的期間，刪除、改名、取代都被系統拒絕（本行程與另一個行程都試）
另外：複製的 fsync 與寫入後驗證、沒有資源回收筒的磁碟。
"""

import errno
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

from media_sorter import organizer
from media_sorter.organizer import execute, latest_log, plan_operations, remove_originals, undo

D = datetime(2024, 1, 1)
windows_only = pytest.mark.skipif(sys.platform != "win32", reason="只在 Windows 上有強制的共用模式")


def touch(path: Path, text: str = "x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def organize(tmp_path, files):
    ops = plan_operations([(f, "貓", D) for f in files], tmp_path / "out")
    return ops, execute(ops, tmp_path / "logs", source_dir=tmp_path / "src", output_dir=tmp_path / "out")


def make_trash(tmp_path, monkeypatch, during=None):
    bin_dir = tmp_path / "RECYCLE"
    bin_dir.mkdir(exist_ok=True)

    def fake_send2trash(path):
        if during:
            during(Path(path))
        shutil.move(path, bin_dir / Path(path).name)

    monkeypatch.setattr(organizer, "_send2trash", fake_send2trash)
    return bin_dir


def tamper_after_compare(monkeypatch, action):
    """在「握著兩份比對完」之後、「送出」之前動手腳（模擬另一個程式剛好在這個空檔動到留下的那一份）。"""
    real = organizer._same_content

    def compare_then_tamper(a, b, *args, **kwargs):
        same = real(a, b, *args, **kwargs)
        if kwargs.get("reader_b") is not None:  # 只在握著控制代碼的那一次比對之後
            try:
                action()
            except OSError:
                pass  # Windows 會直接拒絕（那是另一組測試）；被拒絕就代表保護有效
        return same

    monkeypatch.setattr(organizer, "_same_content", compare_then_tamper)


# ---------------------------------------------------------------------------- 所有系統：送出前再確認留下的那一份
@pytest.mark.parametrize("how", ["delete", "rename", "replace"])
def test_kept_copy_gone_before_sending_aborts_removal(tmp_path, monkeypatch, how):
    a = touch(tmp_path / "src" / "a.jpg", "A")
    ops, result = organize(tmp_path, [a])
    copy = ops[0].dst
    other = touch(tmp_path / "other.jpg", "A")  # 內容相同、但不是同一個檔案

    def action():
        if how == "delete":
            os.remove(copy)
        elif how == "rename":
            os.rename(copy, copy.with_name("moved.jpg"))
        else:
            os.replace(other, copy)

    tamper_after_compare(monkeypatch, action)
    bin_dir = make_trash(tmp_path, monkeypatch)
    removed = remove_originals(result.log_path)
    if sys.platform == "win32":  # Windows：動手腳本身就被拒絕，所以移除照常完成，複本原封不動
        assert removed.done == 1 and copy.read_text() == "A"
        return
    assert removed.done == 0 and removed.errors
    assert a.read_text() == "A" and not list(bin_dir.iterdir())  # 原檔改回原名、沒有進回收筒
    assert latest_log(tmp_path / "logs") == result.log_path


def test_undo_original_gone_before_sending_keeps_the_copy(tmp_path, monkeypatch):
    """復原時留下的是原檔：原檔在空檔中被刪掉的話，複本就是唯一的一份，不能移除。"""
    a = touch(tmp_path / "src" / "a.jpg", "A")
    ops, result = organize(tmp_path, [a])
    tamper_after_compare(monkeypatch, lambda: os.remove(a))
    bin_dir = make_trash(tmp_path, monkeypatch)
    undone = undo(result.log_path)
    if sys.platform == "win32":
        assert undone.done == 1 and a.read_text() == "A"
        return
    assert undone.done == 0 and undone.errors
    assert ops[0].dst.read_text() == "A" and not list(bin_dir.iterdir())


def test_kept_file_is_opened_without_delete_sharing(tmp_path, monkeypatch):
    """留下的那一份走「連刪除都不允許」的開法，要移除的那一份走「允許刪除」的開法（回收筒才搬得走）。"""
    a = touch(tmp_path / "src" / "a.jpg", "A")
    ops, result = organize(tmp_path, [a])
    calls = []
    real_removable, real_kept = organizer._open_deny_write, organizer._open_deny_write_delete
    monkeypatch.setattr(organizer, "_open_deny_write", lambda p: (calls.append(("removable", Path(p).name)), real_removable(p))[1])
    monkeypatch.setattr(organizer, "_open_deny_write_delete", lambda p: (calls.append(("kept", Path(p).name)), real_kept(p))[1])
    make_trash(tmp_path, monkeypatch)
    assert remove_originals(result.log_path).done == 1
    assert calls == [("removable", "a (整理前).jpg"), ("kept", ops[0].dst.name)]


# ---------------------------------------------------------------------------- Windows：系統真的拒絕
def _try(action) -> str:
    try:
        action()
    except PermissionError:
        return "denied"
    except OSError as exc:
        return f"error {exc.winerror if hasattr(exc, 'winerror') else exc.errno}"
    return "done"


CHILD = r"""
import os, sys
target, other, how = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    if how == "delete":
        os.remove(target)
    elif how == "rename":
        os.rename(target, target + ".moved")
    elif how == "replace":
        os.replace(other, target)
    else:
        open(target, "ab").close()
except PermissionError:
    sys.exit(7)
sys.exit(0)
"""


@windows_only
@pytest.mark.parametrize("how", ["delete", "rename", "replace", "write"])
def test_windows_kept_copy_cannot_be_removed_while_guarded(tmp_path, how):
    """審查的重現步驟：握著保護控制代碼時，刪除「該留下的那一份」。同一個行程與另一個行程都要被拒絕。"""
    keep = touch(tmp_path / "keep.jpg", "A")
    other = touch(tmp_path / "other.jpg", "B")
    actions = {
        "delete": lambda: os.remove(keep),
        "rename": lambda: os.rename(keep, keep.with_name("moved.jpg")),
        "replace": lambda: os.replace(other, keep),
        "write": lambda: open(keep, "ab").close(),
    }
    with organizer._open_deny_write_delete(keep) as guard:
        assert _try(actions[how]) == "denied"
        child = subprocess.run([sys.executable, "-c", CHILD, str(keep), str(other), how], timeout=120)
        assert child.returncode == 7, "另一個行程沒有被拒絕"
        assert keep.exists() and guard.read() == b"A"
    assert keep.read_text() == "A"
    assert _try(actions[how]) == "done"  # 放開之後一切照常（確認剛才真的是被保護擋下，不是別的原因）


@windows_only
def test_windows_file_to_remove_can_still_be_moved_away(tmp_path):
    """要移除的那一份必須允許刪除／搬移，否則資源回收筒搬不走它；但一樣不能寫入。"""
    staged = touch(tmp_path / "a (整理前).jpg", "A")
    with organizer._open_deny_write(staged):
        assert _try(lambda: open(staged, "ab").close()) == "denied"
        os.rename(staged, tmp_path / "in-trash.jpg")
    assert (tmp_path / "in-trash.jpg").read_text() == "A"


@windows_only
@pytest.mark.parametrize("how", ["delete", "rename", "replace"])
def test_windows_removal_survives_attacks_on_the_kept_copy(tmp_path, monkeypatch, how):
    """整段流程：送回收筒的那一刻去刪／改名／取代複本，全部被拒絕，結束後複本還在、內容正確。"""
    a = touch(tmp_path / "src" / "a.jpg", "A")
    ops, result = organize(tmp_path, [a])
    copy = ops[0].dst
    other = touch(tmp_path / "other.jpg", "B")
    outcome = {}

    def during(staged):
        actions = {"delete": lambda: os.remove(copy), "rename": lambda: os.rename(copy, copy.with_name("m.jpg")),
                   "replace": lambda: os.replace(other, copy)}
        outcome["same process"] = _try(actions[how])
        outcome["other process"] = subprocess.run(
            [sys.executable, "-c", CHILD, str(copy), str(other), how], timeout=120).returncode

    bin_dir = make_trash(tmp_path, monkeypatch, during)
    removed = remove_originals(result.log_path)
    assert outcome == {"same process": "denied", "other process": 7}
    assert removed.done == 1 and copy.read_text() == "A"
    assert [p.read_text() for p in bin_dir.iterdir()] == ["A"]


# ---------------------------------------------------------------------------- 複製：fsync 與寫入後驗證
def test_copy_is_fsynced_before_it_gets_its_final_name(tmp_path, monkeypatch):
    a = touch(tmp_path / "src" / "a.jpg", "A" * 5000)
    synced = []
    real_fsync = os.fsync

    def spy(fd):
        if os.fstat(fd).st_size == 5000:  # 媒體檔本身（紀錄檔的大小不同）
            synced.append([p.name for p in (tmp_path / "out" / "貓").iterdir()])
        real_fsync(fd)

    monkeypatch.setattr(organizer.os, "fsync", spy)
    ops, result = organize(tmp_path, [a])
    assert result.done == 1
    assert len(synced) == 1 and synced[0][0].endswith(organizer.PARTIAL_SUFFIX)  # fsync 時還是暫存檔名


def test_copy_that_cannot_be_flushed_is_not_kept(tmp_path, monkeypatch):
    a = touch(tmp_path / "src" / "a.jpg", "A" * 5000)
    real_fsync = os.fsync

    def failing(fd):
        if os.fstat(fd).st_size == 5000:
            raise OSError(errno.EIO, "I/O error")
        real_fsync(fd)

    monkeypatch.setattr(organizer.os, "fsync", failing)
    _, result = organize(tmp_path, [a])
    assert result.done == 0 and len(result.errors) == 1
    assert a.read_text() == "A" * 5000
    assert not [p for p in (tmp_path / "out").rglob("*") if p.is_file()]  # 沒有留下半成品


def test_copy_that_reads_back_differently_is_not_kept(tmp_path, monkeypatch):
    """寫進去的內容讀回來不一樣（壞掉的磁碟、不穩的網路磁碟）：複本不保留，原檔不動。"""
    a = touch(tmp_path / "src" / "a.jpg", "GOOD" * 100)
    b = touch(tmp_path / "src" / "b.jpg", "FINE" * 100)
    real_hash = organizer._hash_file

    def corrupted_read(path):
        if Path(path).name.startswith("a"):  # a.jpg 的暫存檔
            return b"\0" * 32
        return real_hash(path)

    monkeypatch.setattr(organizer, "_hash_file", corrupted_read)
    ops = plan_operations([(a, "貓", D), (b, "貓", D)], tmp_path / "out", rename=False)
    result = execute(ops, tmp_path / "logs", source_dir=tmp_path / "src", output_dir=tmp_path / "out")
    assert result.done == 1 and [e[0] for e in result.errors] == [a]
    assert "讀回來" in result.errors[0][1]
    assert sorted(p.name for p in (tmp_path / "out" / "貓").iterdir()) == ["b.jpg"]
    assert a.read_text() == "GOOD" * 100


def test_source_modified_during_copy_is_not_kept(tmp_path, monkeypatch):
    a = touch(tmp_path / "src" / "a.jpg", "A" * 100)
    real_hash = organizer._hash_file
    real_fingerprint = organizer._fingerprint
    calls = []

    def fingerprint(path):
        calls.append(path)
        if len(calls) == 2:  # 複製完再看一次之前，原檔被改寫
            Path(path).write_text("B" * 150)
        return real_fingerprint(path)

    monkeypatch.setattr(organizer, "_fingerprint", fingerprint)
    monkeypatch.setattr(organizer, "_hash_file", real_hash)
    _, result = organize(tmp_path, [a])
    assert result.done == 0 and "原檔被其他程式修改" in result.errors[0][1]
    assert not [p for p in (tmp_path / "out").rglob("*") if p.is_file()]


# ---------------------------------------------------------------------------- 沒有資源回收筒的磁碟
def test_originals_on_a_drive_without_recycle_bin_are_not_removed(tmp_path, monkeypatch):
    """網路磁碟、隨身碟：「送回收筒」其實是永久刪除。不能默默當成有回收筒 → 不移除，並說明原因。"""
    a = touch(tmp_path / "src" / "a.jpg", "A")
    ops, result = organize(tmp_path, [a])
    bin_dir = make_trash(tmp_path, monkeypatch)
    monkeypatch.setattr(organizer, "recycle_bin_problem", lambda p: "這是網路磁碟，沒有資源回收筒")
    rows = organizer.read_log(result.log_path)
    assert "沒有資源回收筒" in organizer.classify_rows(rows, "remove")[0][1]  # 確認視窗裡就先講
    removed = remove_originals(result.log_path)
    assert removed.done == 0 and "沒有資源回收筒" in removed.errors[0][1]
    assert a.read_text() == "A" and ops[0].dst.read_text() == "A" and not list(bin_dir.iterdir())
    assert latest_log(tmp_path / "logs") == result.log_path  # 紀錄留著


def test_undo_on_a_drive_without_recycle_bin_quarantines_on_the_same_drive(tmp_path, monkeypatch):
    """復原：複本所在的磁碟沒有回收筒 → 搬到同一個磁碟（輸出資料夾裡）的隔離資料夾，而不是刪除。"""
    a = touch(tmp_path / "src" / "a.jpg", "A")
    ops, result = organize(tmp_path, [a])
    bin_dir = make_trash(tmp_path, monkeypatch)
    monkeypatch.setattr(organizer, "recycle_bin_problem", lambda p: "這是網路磁碟，沒有資源回收筒")
    undone = undo(result.log_path)
    assert undone.done == 1 and not ops[0].dst.exists() and not list(bin_dir.iterdir())
    kept = list((tmp_path / "out" / organizer.QUARANTINE_DIR).rglob("*.jpg"))
    assert len(kept) == 1 and kept[0].read_text() == "A"
    assert any("沒有資源回收筒" in w and str(kept[0]) in w for w in undone.warnings)
    assert a.read_text() == "A"


def test_recycle_bin_check_on_this_system(tmp_path):
    problem = organizer.recycle_bin_problem(touch(tmp_path / "a.jpg"))
    assert problem is None  # 本機固定磁碟（其他系統交給 send2trash 自己判斷）


@windows_only
def test_windows_drive_kinds():
    assert organizer.recycle_bin_problem_for_drive_type(3) is None  # DRIVE_FIXED
    for kind in (0, 1, 2, 4, 5, 6):  # 不明、不存在、卸除式、網路、光碟、RAM 磁碟
        assert "沒有資源回收筒" in organizer.recycle_bin_problem_for_drive_type(kind)


@windows_only
def test_windows_network_drive_is_detected():
    """需要一個網路磁碟（Wine：在 HKLM\\Software\\Wine\\Drives 把某個磁碟代號設成 network）。"""
    drive = os.environ.get("MEDIA_SORTER_TEST_NETWORK_DRIVE")
    if not drive:
        pytest.skip("設定 MEDIA_SORTER_TEST_NETWORK_DRIVE=磁碟代號（例如 N:）才執行")
    assert "網路磁碟" in organizer.recycle_bin_problem(Path(drive + "\\any\\file.jpg"))


# ---------------------------------------------------------------------------- 回收筒被關掉、或檔案比回收筒大（第十輪審查 B1）
GB = 1024**3


def setting(**overrides):
    values = dict(policy_off=False, nuke_on_delete=False, max_capacity_mb=None, volume_bytes=500 * GB, file_size=10 * 1024**2)
    values.update(overrides)
    return organizer.recycle_bin_setting_problem(**values)


def test_recycle_bin_settings_that_turn_trash_into_permanent_delete():
    """程式是用「不跳確認視窗」的方式送回收筒，所以這些情況下 Windows 會不吭聲地永久刪除。"""
    assert setting() is None                                            # 一般情況
    assert "不要將檔案移到資源回收筒" in setting(nuke_on_delete=True)    # 使用者關掉回收筒
    assert "系統管理原則" in setting(policy_off=True)                    # 公司政策
    assert setting(max_capacity_mb=1024, file_size=1 * GB) is None      # 剛好放得下
    assert "容量" in setting(max_capacity_mb=1024, file_size=1 * GB + 1)  # 比使用者設定的容量大
    assert setting(max_capacity_mb=0, file_size=1) is not None
    # 沒改過設定：用 Windows 的預設容量（前 40 GB 的 10% ＋ 其餘的 5%）
    assert organizer.default_bin_capacity(40 * GB) == 4 * GB
    assert organizer.default_bin_capacity(500 * GB) == 4 * GB + 23 * GB
    assert setting(volume_bytes=100 * GB, file_size=6 * GB) is None      # 容量 7 GB
    assert "容量" in setting(volume_bytes=100 * GB, file_size=8 * GB)
    assert setting(volume_bytes=None, file_size=999 * GB) is None       # 連磁碟大小都查不到：只有前兩種查得出來


def test_oversized_or_disabled_bin_keeps_the_original(tmp_path, monkeypatch):
    """整段流程：回收筒收不下這個原檔 → 不移除，原檔與複本都在，確認視窗也先講。"""
    a = touch(tmp_path / "src" / "a.jpg", "A")
    ops, result = organize(tmp_path, [a])
    bin_dir = make_trash(tmp_path, monkeypatch)
    monkeypatch.setattr(organizer, "recycle_bin_problem",
                        lambda p: organizer.recycle_bin_setting_problem(
                            policy_off=False, nuke_on_delete=True, max_capacity_mb=None, volume_bytes=None, file_size=1))
    rows = organizer.read_log(result.log_path)
    assert "不要將檔案移到資源回收筒" in organizer.classify_rows(rows, "remove")[0][1]
    removed = remove_originals(result.log_path)
    assert removed.done == 0 and "不要將檔案移到資源回收筒" in removed.errors[0][1]
    assert a.read_text() == "A" and ops[0].dst.read_text() == "A" and not list(bin_dir.iterdir())


@windows_only
def test_windows_reads_the_recycle_bin_settings_from_the_registry(tmp_path):
    """真的去讀登錄檔：把這個磁碟的回收筒設成「不要移到回收筒」→ 偵測得到；改回來 → 正常。"""
    import winreg

    target = touch(tmp_path / "a.jpg", "A")
    root = os.path.splitdrive(str(target))[0] + "\\"
    settings = organizer._recycle_bin_settings(root)
    assert set(settings) == {"policy_off", "nuke_on_delete", "max_capacity_mb", "volume_bytes"}
    import ctypes

    buffer = ctypes.create_unicode_buffer(64)
    if not ctypes.windll.kernel32.GetVolumeNameForVolumeMountPointW(root, buffer, len(buffer)):
        pytest.skip("這個磁碟查不到磁碟區識別碼（例如 Wine 對應到 Linux 資料夾的磁碟）")
    guid = buffer.value[buffer.value.find("{"):buffer.value.find("}") + 1]
    key_path = r"Software\Microsoft\Windows\CurrentVersion\Explorer\BitBucket\Volume" + "\\" + guid
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path) as key:
        try:
            before = winreg.QueryValueEx(key, "NukeOnDelete")[0]
        except OSError:
            before = None
        try:
            winreg.SetValueEx(key, "NukeOnDelete", 0, winreg.REG_DWORD, 1)
            assert organizer._recycle_bin_settings(root)["nuke_on_delete"] is True
            assert "不要將檔案移到資源回收筒" in organizer.recycle_bin_problem(target)
        finally:
            if before is None:
                winreg.DeleteValue(key, "NukeOnDelete")
            else:
                winreg.SetValueEx(key, "NukeOnDelete", 0, winreg.REG_DWORD, before)
    assert organizer._recycle_bin_settings(root)["nuke_on_delete"] is False
