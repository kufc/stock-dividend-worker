"""對抗測試找到的介面問題的回歸測試（需要圖形環境）。"""

import threading

import pytest

from fakes import FakeClassifier
from helpers import make_image
from conftest import pump
from test_app import pytestmark  # noqa: F401 - 共用圖形環境檢查

GATE = threading.Event()


class GatedClassifier(FakeClassifier):
    """第一批之後停住，模擬「辨識進行中」的狀態。"""

    batches = 0

    def encode_prepared(self, tensors):
        GatedClassifier.batches += 1
        if GatedClassifier.batches > 1:
            GATE.wait(10)
        return super().encode_prepared(tensors)


def start_gated(app, tmp_path, n=6, color=(250, 10, 10)):
    GATE.clear()
    GatedClassifier.batches = 0
    app.classifier_factory = GatedClassifier
    app.settings["batch_size"] = 2
    for i in range(n):
        make_image(tmp_path / "photos" / f"{i:02d}.png", color)
    app.folder_var.set(str(tmp_path / "photos"))
    app.toggle_analysis()
    pump(app.root, lambda: sum(it.analyzed for it in app.items) >= 2)


def finish(app):
    GATE.set()
    pump(app.root, lambda: not app.analysis_running())


def test_delete_and_reorder_categories_during_analysis(app, tmp_path):
    from media_sorter.analysis import CONFIRMED
    from media_sorter.config import Category

    start_gated(app, tmp_path)
    app.set_categories([Category("藍"), Category("紅")])  # 刪掉「綠」並調換順序：以前會 IndexError
    app.accept_confident_all()
    confirmed = {it.chosen for it in app.items if it.status == CONFIRMED}
    assert confirmed == {"紅"}  # 以前會因為位置錯亂被確認成別的分類
    finish(app)
    assert all(it.best(app.categories)[0] == "紅" for it in app.items)


def test_decisions_made_during_analysis_are_kept(app, tmp_path):
    from media_sorter.analysis import CONFIRMED, SKIPPED

    start_gated(app, tmp_path)
    last = app.tree.get_children()[-1]
    app.tree.selection_set(last)
    app.set_selected("__skip__")
    first = app.tree.get_children()[0]
    app.tree.selection_set(first)
    app.set_selected("綠")
    finish(app)
    assert app.items[app._index_of(last)].status == SKIPPED
    assert app.items[app._index_of(first)].status == CONFIRMED
    assert app.items[app._index_of(first)].chosen == "綠"


def test_selection_is_cleared_after_organize(app, tmp_path):
    for i in range(6):
        make_image(tmp_path / "photos" / f"{i:02d}.png", (250, 10, 10))
    app.folder_var.set(str(tmp_path / "photos"))
    app.toggle_analysis()
    pump(app.root, lambda: app.items and not app.analysis_running())
    iids = app.tree.get_children()
    app.tree.selection_set(iids[:3])
    app.set_selected("紅")
    app.organize()  # askyesnocancel → True，但這裡只有已確認＋高信心
    pump(app.root, lambda: not app.organizing)
    assert app.tree.selection() == () and app.current is None


def test_relative_output_folder_goes_next_to_photos(app, tmp_path):
    app.folder_var.set(str(tmp_path / "photos"))
    app.output_var.set("整理好")
    assert app.output_dir() == (tmp_path / "photos" / "整理好")


def test_one_bad_message_does_not_block_others(app, tmp_path, monkeypatch):
    errors = []
    monkeypatch.setattr(app, "log_error", errors.append)
    app.queue.put(("item", app._run_id, 999, None))  # 壞掉的訊息
    app.queue.put(("status", app._run_id, "後面的訊息仍然會處理"))
    app._drain_queue()
    assert app.status_var.get() == "後面的訊息仍然會處理" and errors


def test_stale_messages_from_previous_run_are_ignored(app):
    app.queue.put(("status", app._run_id - 1, "舊的"))
    app._drain_queue()
    assert app.status_var.get() != "舊的"


def test_undo_disabled_while_organizing(app, tmp_path):
    app.organizing = True
    app._set_busy(True)
    assert app.undo_btn.instate(["disabled"]) and app.category_btn.instate(["disabled"])
    app.undo_last()  # 不會動作
    app.organizing = False
    app._set_busy(False)
    assert not app.undo_btn.instate(["disabled"])


def test_renaming_category_keeps_confirmations(app, tmp_path):
    from media_sorter.analysis import CONFIRMED
    from media_sorter.config import Category

    make_image(tmp_path / "photos" / "a.png", (250, 10, 10))
    app.folder_var.set(str(tmp_path / "photos"))
    app.toggle_analysis()
    pump(app.root, lambda: app.items and not app.analysis_running())
    app.accept_confident_all()
    assert app.items[0].status == CONFIRMED and app.items[0].chosen == "紅"
    app.set_categories([Category("紅色系"), Category("綠"), Category("藍")], {"紅": "紅色系"})
    assert app.items[0].status == CONFIRMED and app.items[0].chosen == "紅色系"


def test_category_folder_clash_is_rejected(app, monkeypatch):
    from tkinter import messagebox

    warnings = []
    monkeypatch.setattr(messagebox, "showwarning", lambda *a, **k: warnings.append(a))
    assert app.add_category("旅行/日本", [])
    assert not app.add_category("旅行_日本", [])  # 會用到同一個資料夾 → 拒絕
    assert app.add_category("Cat", []) and not app.add_category("cat", [])  # Windows 不分大小寫
    assert len(warnings) >= 2
    assert app.add_category("紅", []) is True  # 已存在的名稱直接視為成功


def test_multi_confirm_under_filter_selects_next_remaining(app, tmp_path):
    for i in range(8):
        make_image(tmp_path / "photos" / f"{i:02d}.png", (250, 10, 10))
    app.folder_var.set(str(tmp_path / "photos"))
    app.toggle_analysis()
    pump(app.root, lambda: app.items and not app.analysis_running())
    app.filter_var.set("待確認")
    app.refresh_tree()
    iids = app.tree.get_children()
    app.tree.selection_set(iids[:3])
    app.root.update()
    app.choice_var.set("紅")
    app.confirm_current()
    app.root.update()
    assert app.tree.selection() == (iids[3],)  # 以前會跳過好幾列


@pytest.fixture(autouse=True)
def _release_gate():
    yield
    GATE.set()


def test_unreadable_log_can_be_set_aside(app, tmp_path, monkeypatch):
    from tkinter import messagebox

    import media_sorter.app as app_module
    from media_sorter import organizer

    logs = tmp_path / "logs"
    logs.mkdir()
    bad = logs / "整理紀錄_20240101_000000.csv"
    bad.write_bytes(b"\xff\xfe garbage")
    asked = []
    monkeypatch.setattr(messagebox, "askyesno", lambda *a, **k: asked.append(a[1]) or True)
    monkeypatch.setattr(messagebox, "showerror", lambda *a, **k: pytest.fail(f"error: {a}"))
    app.undo_last()
    assert asked and "無法讀取整理紀錄" in asked[0]
    assert not bad.exists() and (logs / (organizer.UNREADABLE_PREFIX + bad.name)).exists()
    assert app_module.latest_log(logs) is None  # 之後按鈕不會再卡在同一份壞紀錄


def test_confirmation_lists_folders_and_files(app, tmp_path, monkeypatch):

    from media_sorter.organizer import execute, plan_operations

    src = tmp_path / "photos"
    a = make_image(src / "sub" / "IMG_1.png", (250, 10, 10))
    ops = plan_operations([(a, "紅", __import__("datetime").datetime(2024, 1, 1))], src / "已分類")
    execute(ops, tmp_path / "logs", source_dir=src, output_dir=src / "已分類")
    import media_sorter.app as app_module

    shown = []
    monkeypatch.setattr(app_module, "confirm_with_list",
                        lambda master, title, message, lines, ok: shown.append((message, lines)) or False)
    app.folder_var.set(str(src))
    app.remove_originals_last()
    message, lines = shown[0]
    assert "來源資料夾" in message and str(src) in message and "⚠" not in message
    assert any("IMG_1.png" in line for line in lines)


def test_confirmation_lists_every_file_and_warns_on_other_folder(app, tmp_path, monkeypatch):
    import media_sorter.app as app_module
    from media_sorter.organizer import execute, plan_operations

    src = tmp_path / "photos"
    files = [make_image(src / f"IMG_{i:02d}.png", (250, 10, 10)) for i in range(15)]
    when = __import__("datetime").datetime(2024, 1, 1)
    ops = plan_operations([(f, "紅", when) for f in files], src / "已分類")
    execute(ops, tmp_path / "logs", source_dir=src, output_dir=src / "已分類")
    shown = []
    monkeypatch.setattr(app_module, "confirm_with_list",
                        lambda master, title, message, lines, ok: shown.append((message, lines)) or False)
    app.folder_var.set(str(tmp_path / "別的資料夾"))
    app.remove_originals_last()
    message, lines = shown[0]
    assert all(any(f.name in line for line in lines) for f in files)  # 15 個全部列出，不省略
    assert "⚠" in message  # 紀錄的來源資料夾跟目前選的不同


def test_header_only_log_can_be_set_aside(app, tmp_path, monkeypatch):
    from tkinter import messagebox

    import media_sorter.app as app_module
    from media_sorter import organizer

    logs = tmp_path / "logs"
    logs.mkdir()
    empty = logs / "整理紀錄_20240101_000000.csv"
    empty.write_text(",".join(organizer.LOG_FIELDS) + "\n", encoding="utf-8-sig")
    asked = []
    monkeypatch.setattr(messagebox, "askyesno", lambda *a, **k: asked.append(a[1]) or True)
    app.undo_last()
    assert asked and "沒有可以" in asked[0]
    assert app_module.latest_log(logs) is None  # 不再卡在這份空紀錄
