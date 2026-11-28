"""資源回收筒的第二種做法（IFileOperation，預設關閉）。見 media_sorter/recycle.py 開頭的說明。"""

import sys

import pytest

from media_sorter import organizer, recycle
from media_sorter.recycle import STAGE_OPERATION, STAGE_SETUP, RecycleUnsupported

_REAL = recycle.recycle_modern  # 有些測試會把模組裡的換掉，這裡留著真的


def test_modern_path_is_off_by_default_and_legacy_is_used(tmp_path, monkeypatch):
    monkeypatch.delenv(recycle.MODERN_ENV, raising=False)
    assert recycle.MODERN_DEFAULT is False and not recycle.modern_enabled()
    monkeypatch.setattr(recycle, "recycle_modern", lambda path: pytest.fail("預設不該走新介面"))
    seen = []
    recycle.to_recycle_bin("x", seen.append)
    assert seen == ["x"]


def test_unavailable_interface_falls_back_to_legacy(monkeypatch):
    monkeypatch.setenv(recycle.MODERN_ENV, "1")
    monkeypatch.setattr(recycle, "recycle_modern", lambda path: (_ for _ in ()).throw(RecycleUnsupported("no")))
    seen = []
    recycle.to_recycle_bin("x", seen.append)
    assert seen == ["x"]


def test_refusal_by_windows_is_never_retried_with_the_legacy_call(tmp_path, monkeypatch):
    """最重要的一條：新介面說「進不了回收筒」時，絕對不能退回舊介面——舊介面在同樣的情況下會永久刪除。"""
    monkeypatch.setenv(recycle.MODERN_ENV, "1")
    victim = tmp_path / "keep.jpg"
    victim.write_bytes(b"data")
    monkeypatch.setattr(recycle, "recycle_modern",
                        lambda path: _REAL(path, lambda p: (STAGE_OPERATION, 0x80270000, False)))
    with pytest.raises(OSError, match="無法把這個檔案送進資源回收筒"):
        recycle.to_recycle_bin(str(victim), lambda p: pytest.fail("不可以退回舊介面"))
    assert victim.read_bytes() == b"data"


@pytest.mark.parametrize("outcome,error", [
    ((STAGE_SETUP, 0x80004001, False), RecycleUnsupported),       # Wine：介面是空殼
    ((STAGE_OPERATION, 0x80070005, False), OSError),              # 存取被拒
    ((STAGE_OPERATION, 0, True), OSError),                        # 被中止
    ((STAGE_OPERATION, 0, False), OSError),                       # 回報成功但檔案還在
    ((STAGE_OPERATION, 0x80004004, True, {"will_recycle": False}), OSError),   # Windows 說不會進回收筒：我們中止了
    ((STAGE_OPERATION, 0, False, {}), OSError),                   # Windows 沒有先問我們：無法確認去向
])
def test_outcomes_of_the_shell_call(tmp_path, outcome, error):
    victim = tmp_path / "a.jpg"
    victim.write_bytes(b"x")
    with pytest.raises(error):
        _REAL(str(victim), lambda path: outcome)
    assert victim.exists()


def test_success_requires_the_file_to_be_gone(tmp_path):
    victim = tmp_path / "a.jpg"
    victim.write_bytes(b"x")

    def fake(path):
        victim.unlink()
        return STAGE_OPERATION, 0, False

    _REAL(str(victim), fake)  # 不丟錯誤


def test_refusal_message_says_the_file_would_have_been_deleted_for_good(tmp_path):
    """真的 Windows 上實測：回收筒被關掉時，光靠「不能進回收筒就失敗」的旗標，檔案照樣被永久刪除。
    所以改成刪除前由 Windows 告訴我們會不會進回收筒；不會就中止並說明。"""
    victim = tmp_path / "a.jpg"
    victim.write_bytes(b"x")
    with pytest.raises(OSError, match="不會進資源回收筒"):
        _REAL(str(victim), lambda path: (STAGE_OPERATION, 0x80004004, True, {"will_recycle": False}))
    assert victim.read_bytes() == b"x"


def test_a_missing_system_library_counts_as_unsupported(tmp_path):
    def broken(path):
        raise OSError("cannot load shell32")

    with pytest.raises(RecycleUnsupported):
        _REAL(str(tmp_path / "a.jpg"), broken)


@pytest.mark.skipif(sys.platform == "win32", reason="Windows 上有另一個測試實際呼叫")
def test_other_systems_report_unsupported(tmp_path):
    with pytest.raises(RecycleUnsupported):
        _REAL(str(tmp_path / "a.jpg"))


@pytest.mark.skipif(sys.platform != "win32", reason="呼叫 Windows 的 IFileOperation")
def test_real_call_either_recycles_or_reports_unsupported_without_touching_the_file(tmp_path):
    """真的呼叫系統。Wine 的 IFileOperation 是空殼 → 必須回報不支援、檔案原封不動（這是 Wine 驗得到的部分）。
    **在真的 Windows 上這個測試應該走到「檔案進了回收筒」那一支**；上市前請確認它不是從 except 那邊通過的
    （docs/release-checklist.md）。"""
    victim = tmp_path / "recycle-me.jpg"
    victim.write_bytes(b"photo")
    try:
        _REAL(str(victim))
    except RecycleUnsupported:
        assert victim.read_bytes() == b"photo"
        print("IFileOperation 不支援（Wine？）：只驗證到退回原本做法的部分")
    else:
        assert not victim.exists()


def test_organizer_routes_both_removals_through_the_same_helper(tmp_path, monkeypatch):
    """復原與移除原檔都經過 organizer._trash；沒開新介面時它就是 send2trash（測試換掉的那個也會被用到）。"""
    monkeypatch.delenv(recycle.MODERN_ENV, raising=False)
    seen = []
    monkeypatch.setattr(organizer, "_send2trash", seen.append)
    organizer._trash("some-file")
    assert seen == ["some-file"]
    source = (organizer.__file__ and open(organizer.__file__, encoding="utf-8").read())
    assert source.count("_send2trash(") == 1  # 只剩 _trash 裡面那一個呼叫


# ---------------------------------------------------------------- 真的資源回收筒（只在 Windows、明確要求時才跑）
real_bin = pytest.mark.skipif(
    sys.platform != "win32" or __import__("os").environ.get("MEDIA_SORTER_TEST_REAL_TRASH") != "1",
    reason="會放小測試檔到真正的資源回收筒；Windows 上設定 MEDIA_SORTER_TEST_REAL_TRASH=1 才執行")


def _bin_items(path) -> int:
    import ctypes

    class Info(ctypes.Structure):
        _pack_ = 8
        _fields_ = [("cbSize", ctypes.c_ulong), ("i64Size", ctypes.c_longlong), ("i64NumItems", ctypes.c_longlong)]

    info = Info()
    info.cbSize = ctypes.sizeof(info)
    assert ctypes.windll.shell32.SHQueryRecycleBinW(str(path.anchor), ctypes.byref(info)) == 0
    return int(info.i64NumItems)


def _organized(tmp_path):
    from datetime import datetime

    source = tmp_path / "src" / "ai-media-sorter-測試檔-可刪除.jpg"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"photo-bytes" * 500)
    ops = organizer.plan_operations([(source, "貓", datetime(2024, 1, 2))], tmp_path / "out")
    result = organizer.execute(ops, tmp_path / "logs", source_dir=tmp_path / "src", output_dir=tmp_path / "out")
    assert result.done == 1
    return source, ops[0].dst, result.log_path


def _watch_modern(monkeypatch):
    """記下新介面每次呼叫的結果，確認它真的被用到（而不是悄悄退回舊介面）。"""
    seen = []

    def spy(path):
        try:
            _REAL(path)
        except Exception as exc:
            seen.append(type(exc).__name__)
            raise
        seen.append("recycled")

    monkeypatch.setattr(recycle, "recycle_modern", spy)
    return seen


@real_bin
@pytest.mark.parametrize("modern", [False, True], ids=["legacy", "modern"])
def test_real_bin_takes_the_original_while_the_copy_is_guarded(tmp_path, monkeypatch, modern):
    """整個產品最關鍵的一步，在真的 Windows 上：移除原檔時程式握著原檔與複本的保護，
    真的資源回收筒要能把原檔搬走，複本原封不動。兩種介面各跑一次。"""
    monkeypatch.setenv(recycle.MODERN_ENV, "1" if modern else "0")
    seen = _watch_modern(monkeypatch)
    source, copy, log = _organized(tmp_path)
    data, before = copy.read_bytes(), _bin_items(source)
    removed = organizer.remove_originals(log)
    assert removed.done == 1 and not removed.errors, removed.errors
    assert not source.exists() and copy.read_bytes() == data
    assert _bin_items(source) == before + 1          # 進了回收筒，不是被永久刪除
    assert seen == (["recycled"] if modern else [])


@real_bin
@pytest.mark.parametrize("modern", [False, True], ids=["legacy", "modern"])
def test_real_bin_takes_the_copy_on_undo(tmp_path, monkeypatch, modern):
    monkeypatch.setenv(recycle.MODERN_ENV, "1" if modern else "0")
    seen = _watch_modern(monkeypatch)
    source, copy, log = _organized(tmp_path)
    data, before = source.read_bytes(), _bin_items(source)
    undone = organizer.undo(log)
    assert undone.done == 1 and not undone.errors, undone.errors
    assert not copy.exists() and source.read_bytes() == data
    assert _bin_items(source) == before + 1
    assert seen == (["recycled"] if modern else [])
