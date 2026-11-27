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
