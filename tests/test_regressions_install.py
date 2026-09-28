"""安裝與啟動相關的回歸測試。"""

import sys

from media_sorter import __main__ as entry
from media_sorter.config import acquire_app_lock
from media_sorter.installer import choose_torch_build, driver_too_old


def test_pythonw_streams_are_redirected(monkeypatch, tmp_path):
    import media_sorter.config as config

    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    entry._ensure_console_streams()
    sys.stderr.write("progress bar output\n")  # 以前會 AttributeError
    sys.stderr.flush()
    assert "progress bar output" in (tmp_path / "logs" / "console.log").read_text(encoding="utf-8")


def test_torch_build_choice():
    assert choose_torch_build([("RTX 4070", 8.9, 12)])[0] == "cu128"
    assert choose_torch_build([("TITAN V", 7.0, 12)])[0] == "cu126"
    assert choose_torch_build([("GTX 1060", 6.1, 6)])[0] == "cu126"
    assert choose_torch_build([("舊驅動查不到", 0.0, 8)])[0] == "cu128"
    assert choose_torch_build([]) is None
    assert driver_too_old("472.12") and not driver_too_old("572.83")


def test_app_lock_is_exclusive(tmp_path):
    first = acquire_app_lock(tmp_path)
    assert first is not None
    assert acquire_app_lock(tmp_path) is None
    first.close()
    again = acquire_app_lock(tmp_path)
    assert again is not None
    again.close()
