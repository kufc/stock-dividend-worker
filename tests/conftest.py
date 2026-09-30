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


def _make_app(monkeypatch, tmp_path, scaling=None, geometry=None):
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
    monkeypatch.setattr(app_module, "show_problem", lambda *a, **k: pytest.fail(f"problem: {a}"))

    from media_sorter import config

    for name in ("settings.json", "categories.json"):  # 設定檔在測試之間不共用
        (config.APP_DIR / name).unlink(missing_ok=True)
    tk = pytest.importorskip("tkinter")
    root = tk.Tk()
    if scaling is not None:
        root.tk.call("tk", "scaling", scaling)
    instance = app_module.App(root, classifier_factory=FakeClassifier, model_status_func=lambda choice: None)
    if geometry:
        root.geometry(geometry)
    instance.set_categories([Category("紅"), Category("綠"), Category("藍")])
    return instance


def _close_app(instance):
    import tkinter

    instance.shutdown()
    try:
        instance.root.destroy()
    except tkinter.TclError:  # 測試裡已經關過視窗
        pass
    import gc

    gc.collect()  # 在主執行緒回收 Tk 物件，避免被背景執行緒回收而當掉


@pytest.fixture
def app(monkeypatch, tmp_path):
    instance = _make_app(monkeypatch, tmp_path)
    yield instance
    _close_app(instance)


@pytest.fixture
def app_factory(monkeypatch, tmp_path):
    """可以指定縮放與視窗大小的版本，一個測試裡可以建立多個（一次只用一個）。"""
    made = []

    def make(scaling=None, geometry=None):
        while made:
            _close_app(made.pop())
        made.append(_make_app(monkeypatch, tmp_path, scaling, geometry))
        return made[-1]

    yield make
    while made:
        _close_app(made.pop())


def run_analysis(app, folder):
    """選好資料夾、開始辨識，等到全部辨識完成。"""
    app.folder_var.set(str(folder))
    app.start_analysis()
    pump(app.root, lambda: app.items and all(item.analyzed or item.error for item in app.items)
         and not app.analysis_running())


def copy_all(app):
    """在步驟 3 按「開始複製」並等到完成。"""
    assert app.show_step(3)
    app.start_copy()
    pump(app.root, lambda: not app.organizing and app.step == 4)
