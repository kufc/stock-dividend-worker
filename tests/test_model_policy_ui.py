"""介面上的連網政策：模型不能用時先問使用者，同意了才下載；「設定」裡可以要求檢查並修復。"""

from tkinter import messagebox

import pytest

from conftest import pump
from fakes import FakeClassifier
from helpers import make_image
from media_sorter import netpolicy
from media_sorter.dialogs import SettingsDialog


def photos(tmp_path, n=3):
    src = tmp_path / "src"
    for i in range(n):
        make_image(src / f"IMG_{i}.jpg", (250, 10, 10))
    return src


@pytest.fixture
def model_missing(app):
    """模型一開始不能用（離線模式下回報 ModelNotReady）；「下載」之後才載得起來。"""
    state = {"ready": False, "factory_calls": 0, "downloads": []}

    def factory(preset):
        state["factory_calls"] += 1
        if not state["ready"]:
            raise netpolicy.ModelNotReady("standard", ["缺少 open_clip_pytorch_model.bin"])
        return FakeClassifier(preset)

    def download(preset, on_progress=None):
        state["downloads"].append(preset)
        if on_progress:
            on_progress("open_clip_pytorch_model.bin", 1, 1)
        state["ready"] = True
        return ["open_clip_pytorch_model.bin"]

    app.classifier_factory = factory
    app.model_download_func = download
    return state


def test_declining_the_download_never_downloads(app, model_missing, monkeypatch, tmp_path):
    asked = []
    monkeypatch.setattr(messagebox, "askyesno", lambda title, text, **k: (asked.append(text), False)[1])
    app.folder_var.set(str(photos(tmp_path)))
    app.start_analysis()
    pump(app.root, lambda: asked and not app.analysis_running())
    text = asked[0]
    assert "離線模式" in text and "huggingface.co" in text and "不會上傳" in text and "缺少" in text
    assert model_missing["downloads"] == [] and model_missing["factory_calls"] == 1  # 沒有下載，也沒有偷偷重試
    assert not app.model_task_running and "沒有下載" in app.status_var.get()
    assert not any(item.analyzed for item in app.items)


def test_agreeing_downloads_once_and_then_analyzes(app, model_missing, monkeypatch, tmp_path):
    monkeypatch.setattr(messagebox, "askyesno", lambda *a, **k: True)
    app.folder_var.set(str(photos(tmp_path)))
    app.start_analysis()
    pump(app.root, lambda: app.items and all(item.analyzed for item in app.items) and not app.analysis_running())
    assert model_missing["downloads"] == ["standard"]
    assert not app.model_task_running


def test_failed_download_is_reported_and_nothing_is_retried(app, model_missing, monkeypatch, tmp_path):
    import media_sorter.app as app_module

    shown = []
    monkeypatch.setattr(messagebox, "askyesno", lambda *a, **k: True)
    monkeypatch.setattr(app_module, "show_problem", lambda *a, **k: shown.append(a))
    app.log_error = lambda text: None
    app.model_download_func = lambda preset, on_progress=None: (_ for _ in ()).throw(ConnectionError("沒有網路"))
    app.folder_var.set(str(photos(tmp_path)))
    app.start_analysis()
    pump(app.root, lambda: shown)
    assert "下載失敗" in shown[0][1] and model_missing["factory_calls"] == 1 and not app.model_task_running


def test_settings_offers_repair_and_it_needs_confirmation(app, monkeypatch):
    calls = []
    app.model_repair_func = lambda choice: (calls.append(choice), ("標準", [], []))[1]
    dialog = SettingsDialog(app.root, app.settings, app.on_settings_saved, on_repair_model=app.repair_model)
    assert dialog.repair_btn.cget("text") == "檢查並修復 AI 模型…"
    asked = []
    monkeypatch.setattr(messagebox, "askyesno", lambda title, text, **k: (asked.append(text), False)[1])
    dialog.repair_btn.invoke()
    app.root.update()
    assert "huggingface.co" in asked[0] and calls == []  # 沒確認就什麼都不做

    infos = []
    monkeypatch.setattr(messagebox, "askyesno", lambda *a, **k: True)
    monkeypatch.setattr(messagebox, "showinfo", lambda title, text, **k: infos.append(text))
    app.repair_model()
    pump(app.root, lambda: infos)
    assert calls == [app.settings["model"]] and "沒有連網" in infos[0] and not app.model_task_running
