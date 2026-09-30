import os
import sys
import tempfile
from pathlib import Path

# 測試時把設定檔、紀錄寫到暫存資料夾，不影響使用者的資料
os.environ.setdefault("MEDIA_SORTER_HOME", tempfile.mkdtemp(prefix="media_sorter_test_"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))


import time  # noqa: E402

import pytest  # noqa: E402

from fakes import FakeClassifier  # noqa: E402


def pump(root, condition, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        root.update()
        if condition():
            return
        time.sleep(0.02)
    raise AssertionError("等待逾時")


@pytest.fixture
def app(monkeypatch, tmp_path):
    from tkinter import messagebox

    import media_sorter.app as app_module
    from media_sorter.config import Category

    answers = {"askyesno": False, "askyesnocancel": True, "askokcancel": True}
    for name, value in answers.items():
        monkeypatch.setattr(messagebox, name, lambda *a, value=value, **k: value)
    monkeypatch.setattr(messagebox, "showinfo", lambda *a, **k: None)
    monkeypatch.setattr(messagebox, "showwarning", lambda *a, **k: pytest.fail(f"warning: {a}"))
    monkeypatch.setattr(messagebox, "showerror", lambda *a, **k: pytest.fail(f"error: {a}"))
    monkeypatch.setattr(app_module, "LOG_DIR", tmp_path / "logs")

    tk = pytest.importorskip("tkinter")
    root = tk.Tk()
    instance = app_module.App(root, classifier_factory=FakeClassifier)
    instance.set_categories([Category("紅"), Category("綠"), Category("藍")])
    yield instance
    instance.stop_event.set()
    root.destroy()
    import gc

    gc.collect()  # 在主執行緒回收 Tk 物件，避免被背景執行緒回收而當掉
