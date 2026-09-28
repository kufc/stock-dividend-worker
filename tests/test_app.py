"""視窗程式的端對端測試：辨識 → 確認 → 整理 → 復原。需要圖形環境（Linux 可用 xvfb-run）。"""

import os
import sys
import time

import pytest

tk = pytest.importorskip("tkinter")

from fakes import FakeClassifier  # noqa: E402
from helpers import make_image, make_video  # noqa: E402


def _display_available() -> bool:
    if sys.platform == "win32" or sys.platform == "darwin":
        return True
    return bool(os.environ.get("DISPLAY"))


pytestmark = pytest.mark.skipif(not _display_available(), reason="需要圖形環境")


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

    root = tk.Tk()
    instance = app_module.App(root, classifier_factory=FakeClassifier)
    instance.set_categories([Category("紅"), Category("綠"), Category("藍")])
    yield instance
    root.destroy()


def test_full_flow(app, tmp_path, monkeypatch):
    from media_sorter.analysis import CONFIRMED, PENDING

    src = tmp_path / "photos"
    red = make_image(src / "r1.jpg", (250, 10, 10))
    make_image(src / "r2.jpg", (240, 20, 20))
    make_image(src / "sub" / "g.png", (10, 250, 10))
    video = make_video(src / "b.mp4", (10, 10, 250))
    app.folder_var.set(str(src))

    app.toggle_analysis()
    pump(app.root, lambda: not app.worker.is_alive() and app.queue.empty() and app.items
         and all(item.analyzed for item in app.items))
    assert len(app.tree.get_children()) == 4
    assert {item.path.name: item.best(app.categories)[0] for item in app.items} == {
        "r1.jpg": "紅", "r2.jpg": "紅", "g.png": "綠", "b.mp4": "藍"}
    assert app.current is not None  # 第一個項目自動選取並預覽

    # 按「1」選第一個候選：確認並自動跳到下一個
    first = app.current
    app.pick_suggestion(0)
    app.root.update()
    assert app.items[first].status == CONFIRMED
    assert app.current != first

    # 「低信心」篩選與依 AI 判斷篩選
    app.filter_var.set("AI 判斷為：紅")
    app.refresh_tree()
    assert len(app.tree.get_children()) == 2
    app.filter_var.set("全部")
    app.refresh_tree()

    # 新增分類 → 重新計算，不影響已確認的項目
    app.add_category("黃", ["yellow"])
    assert app.items[first].status == CONFIRMED
    assert app.items[0].probs.shape == (4,)

    # 整理：未確認的也依 AI 判斷（askyesnocancel → True）
    assert any(item.status == PENDING for item in app.items)
    app.organize()
    pump(app.root, lambda: not app.items)
    out = src / "已分類"
    assert sorted(p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file()) == [
        "紅/紅_001.jpg", "紅/紅_002.jpg", "綠/綠_001.png", "藍/藍_001.mp4"]
    assert not red.exists() and not video.exists()

    # 復原
    import media_sorter.app as app_module
    from tkinter import messagebox

    monkeypatch.setattr(messagebox, "askyesno", lambda *a, **k: True)
    app.undo_last()
    assert red.exists() and video.exists()
    assert not any(p.is_file() for p in out.rglob("*"))
    assert app_module.latest_log(tmp_path / "logs") is None


def test_manual_choice_skip_and_batch(app, tmp_path):
    from media_sorter.analysis import CONFIRMED, SKIPPED

    src = tmp_path / "photos"
    for i in range(3):
        make_image(src / f"{i}.png", (250, 10, 10))
    app.folder_var.set(str(src))
    app.toggle_analysis()
    pump(app.root, lambda: not app.worker.is_alive() and app.queue.empty() and app.items
         and all(item.analyzed for item in app.items))

    # 多選後批次套用分類
    iids = app.tree.get_children()
    app.tree.selection_set(iids[:2])
    app.batch_var.set("綠")
    app.apply_batch()
    assert [app.items[int(i)].chosen for i in iids[:2]] == ["綠", "綠"]
    assert all(app.items[int(i)].status == CONFIRMED for i in iids[:2])

    # 單一項目略過
    app.tree.selection_set(iids[2])
    app.root.update()
    app.choice_var.set("__skip__")
    app.confirm_current()
    assert app.items[int(iids[2])].status == SKIPPED

    # 只整理已確認的（沒有待確認項目，不會詢問）
    app.action_var.set("copy")
    app.rename_var.set(False)
    app.organize()
    pump(app.root, lambda: len(app.items) == 1)
    assert sorted(p.name for p in (src / "已分類" / "綠").iterdir()) == ["0.png", "1.png"]
    assert (src / "0.png").exists()  # 複製模式保留原檔
