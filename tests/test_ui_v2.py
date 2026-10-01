"""四步驟介面的行為測試：步驟限制、分類清單與縮圖格、撤回、版面、紀錄頁、重試與停止（需要圖形環境）。"""

import pytest

from conftest import copy_all, pump, run_analysis
from helpers import make_image
from test_app import pytestmark  # noqa: F401 - 共用圖形環境檢查


def photos(tmp_path, n=4, color=(250, 10, 10), name="IMG_{i:02d}.png"):
    src = tmp_path / "photos"
    for i in range(n):
        make_image(src / name.format(i=i), color)
    return src


# ------------------------------------------------------------------ 步驟與按鈕狀態
def test_steps_are_gated(app, tmp_path):
    assert app.step == 1
    assert not app.show_step(2) and not app.show_step(3) and not app.show_step(4)
    src = photos(tmp_path)
    run_analysis(app, src)
    assert app.step == 2 and app.step_available(2)
    assert app.step_available(3) and not app.step_available(4)  # AI 分好就可以預覽，不必逐一確認
    copy_all(app)
    assert app.step == 4 and app.step_available(4)


def test_step3_needs_analysis_to_finish(app, tmp_path):
    from test_regressions_gui import finish, start_gated

    start_gated(app, tmp_path)
    app.confirm_current()
    assert app.confirmed_count() >= 1 and not app.step_available(3)  # 辨識中不能預覽整理
    assert app.page2.next_btn.instate(["disabled"])
    finish(app)
    assert app.step_available(3) and not app.page2.next_btn.instate(["disabled"])


def test_folder_step_shows_counts_and_model_notice(monkeypatch, tmp_path):
    from conftest import _close_app, _make_app

    src = photos(tmp_path, 3)
    make_image(src / "sub" / "x.jpg", (0, 0, 0))
    (src / "notes.txt").write_text("x")
    instance = _make_app(monkeypatch, tmp_path)
    try:
        instance.model_status_func = lambda choice: ("標準（測試）", False)
        instance._start_model_status()
        instance.folder_var.set(str(src))
        page = instance.page1
        pump(instance.root, lambda: page.count_vars["images"].get() == "4" and "第一次辨識會先下載" in page.model_var.get())
        assert page.count_vars["other"].get() == "1"
        assert page.start_btn.cget("text") == "開始辨識 4 個檔案"
        instance.subfolders_var.set(False)
        pump(instance.root, lambda: page.count_vars["images"].get() == "3")
        instance.folder_var.set("")
        pump(instance.root, lambda: page.start_btn.instate(["disabled"]))
        assert "還沒有選擇資料夾" in page.empty_var.get()
    finally:
        _close_app(instance)


def test_empty_folder_stays_on_step1_with_notice(app, tmp_path):
    (tmp_path / "empty").mkdir()
    app.folder_var.set(str(tmp_path / "empty"))
    app.start_analysis()
    pump(app.root, lambda: not app.analysis_running())
    assert app.step == 1
    assert "沒有找到支援的圖片或影片" in app.page1.notice_var.get()


# ------------------------------------------------------------------ 檢查分類
def test_low_confidence_goes_to_the_check_bucket(app, tmp_path):
    from media_sorter.analysis import PENDING

    src = tmp_path / "photos"
    make_image(src / "gray.png", (128, 128, 128))  # 跟三個分類都一樣不像 → AI 沒把握
    make_image(src / "red.png", (250, 10, 10))
    run_analysis(app, src)
    gray = next(it for it in app.items if it.path.name == "gray.png")
    assert app._is_low(gray) and gray.status == PENDING
    assert app.page2.buckets.item("low", "values")[0] == "1"
    app.select_bucket("low")
    assert len(app.tree.get_children()) == 1 and app.current == app.items.index(gray)
    assert "沒有把握" in app.page2.notice_var.get()
    # 沒把握的檔案預設仍依 AI 建議整理；步驟 3 可以改成放到「未分類」或不整理
    assert app.show_step(3)
    app.root.update()
    assert len(app.plan.ops) == 2 and app.page3.low_frame.winfo_ismapped()
    app.low_mode_var.set("unsorted")
    app.refresh_plan()
    assert {op.dst.parent.name for op in app.plan.ops if op.src.name == "gray.png"} == {"未分類"}
    app.low_mode_var.set("leave")
    app.refresh_plan()
    assert [op.src.name for op in app.plan.ops] == ["red.png"] and "沒把握而這次不整理 1 個" in app.page3.excluded_var.get()


def test_ai_suggestion_is_highlighted_but_nothing_is_confirmed(app, tmp_path):
    from media_sorter.analysis import PENDING

    run_analysis(app, photos(tmp_path, 2))
    item = app.items[app.current]
    assert item.best(app.categories)[0] == "紅" and item.status == PENDING
    styles = [str(b.cget("style")) for b in app.page2.category_buttons]
    assert styles == ["ChoiceOn.TButton", "Choice.TButton", "Choice.TButton"]  # AI 建議的那個按鈕亮起來
    assert app.page2.accept_btn.cget("text") == "採用 AI 建議：紅"
    assert "不代表正確率" in app.page2.score_var.get()  # 不把模型分數說成「準確率」


def test_undo_last_confirmation(app, tmp_path):
    from media_sorter.analysis import CONFIRMED, PENDING, SKIPPED

    run_analysis(app, photos(tmp_path, 3))
    first = app.current
    app.confirm_current()
    second = app.current
    app.skip_current()
    assert app.items[first].status == CONFIRMED and app.items[second].status == SKIPPED
    app.undo_last_confirm()  # 撤回「略過」
    assert app.items[second].status == PENDING and app.current == second
    app.undo_last_confirm()  # 再撤回「確認」
    assert app.items[first].status == PENDING and app.current == first
    assert app.page2.undo_btn.instate(["disabled"])


def test_batch_actions_only_touch_selected_thumbnails(app, tmp_path):
    from media_sorter.analysis import CONFIRMED, PENDING

    run_analysis(app, photos(tmp_path, 5))
    iids = app.tree.get_children()
    app.tree.selection_set(iids[1:3])
    assert "已選取 2 個檔案" in app.page2.sel_var.get()
    app.accept_selected()
    statuses = [app.items[app._index_of(i)].status for i in iids]
    assert statuses == [PENDING, CONFIRMED, CONFIRMED, PENDING, PENDING]
    assert app.tree.selection() == (iids[3],)  # 處理完跳到下一個
    app.tree.selection_set(())
    assert all(b.instate(["disabled"]) for b in (*app.page2.category_buttons, *app.page2.action_buttons))


def test_category_list_filters_the_grid(app, tmp_path):
    src = tmp_path / "photos"
    make_image(src / "beach_01.png", (250, 10, 10))
    make_image(src / "beach_02.png", (10, 250, 10))
    make_image(src / "city_01.png", (10, 10, 250))
    run_analysis(app, src)
    buckets = app.page2.buckets
    assert [buckets.item(k, "values")[0] for k in ("all", "cat:紅", "cat:綠", "cat:藍", "skipped")] == ["3", "1", "1", "1", "0"]
    buckets.selection_set("cat:綠")  # 點左邊的分類
    app.root.update()
    assert app.bucket_var.get() == "cat:綠" and len(app.tree.get_children()) == 1
    assert app.items[app._index_of(app.tree.get_children()[0])].path.name == "beach_02.png"
    assert app.page2.grid_title_var.get().startswith("綠")
    app.select_bucket("all")
    assert len(app.tree.get_children()) == 3 and buckets.selection() == ("all",)


def test_new_category_takes_the_selected_files(app, tmp_path, monkeypatch):
    from tkinter import simpledialog

    from media_sorter.analysis import CONFIRMED

    run_analysis(app, photos(tmp_path, 3))
    iids = app.tree.get_children()
    app.tree.selection_set(iids[:2])
    monkeypatch.setattr(simpledialog, "askstring", lambda *a, **k: "寶寶")
    app.add_category_prompt()
    assert [c.name for c in app.categories][-1] == "寶寶"
    assert [app.items[app._index_of(i)].chosen for i in iids[:2]] == ["寶寶", "寶寶"]
    assert all(app.items[app._index_of(i)].status == CONFIRMED for i in iids[:2])
    assert app.page2.buckets.item("cat:寶寶", "values")[0] == "2"
    assert len(app.page2.category_buttons) == 4  # 下面多了一個分類按鈕


def test_check_bucket_empties_as_you_fix(app, tmp_path):
    src = tmp_path / "photos"
    make_image(src / "gray.png", (128, 128, 128))
    make_image(src / "red.png", (250, 10, 10))
    run_analysis(app, src)
    app.select_bucket("low")
    assert len(app.tree.get_children()) == 1
    app.assign("藍")
    assert not app.tree.get_children() and "沒有需要檢查的檔案了" in app.tree.empty_text
    assert app.page2.buckets.item("low", "values")[0] == "0" and app.page2.view_state == "placeholder"


def test_thumbnails_are_drawn_in_the_grid(app, tmp_path):
    run_analysis(app, photos(tmp_path, 3))
    app.root.update()
    assert all(app.image_for(iid) is not None for iid in app.tree.get_children())
    assert len(app.thumbs) == 3 and app.tree.canvas.find_all()  # 畫在縮圖格上


# ------------------------------------------------------------------ 版面
def test_thumbnail_size_is_remembered(app, tmp_path):
    from media_sorter.config import load_settings

    run_analysis(app, photos(tmp_path, 2))
    app.set_thumb_size("large")
    assert app.tree.thumb == app.px(208) and app.page2.size_buttons["large"].instate(["pressed"])
    app.on_close()  # 關閉時存檔；下次開啟會用同樣的大小
    assert load_settings()["thumb_size"] == "large"


def test_narrow_window_hides_the_preview_and_keeps_buttons(app, tmp_path):
    run_analysis(app, photos(tmp_path, 2))
    page = app.page2
    app.root.geometry("920x700")  # 比 1100（100% 縮放）窄 → 收起右邊的預覽
    pump(app.root, lambda: page.narrow)
    app.root.update()
    assert not page.detail_frame.winfo_ismapped()
    assert page.accept_btn.winfo_ismapped() and page.next_btn.winfo_ismapped() and page.buckets.winfo_ismapped()
    app.root.geometry("1300x700")
    pump(app.root, lambda: not page.narrow)
    app.root.update()
    assert page.detail_frame.winfo_ismapped()


SCREENS = [(1366, 768), (1920, 1080)]
SCALES = [1.3333, 1.6667, 2.0, 2.6667]  # Windows 縮放 100%／125%／150%／200%


@pytest.mark.parametrize("screen", SCREENS)
@pytest.mark.parametrize("scaling", SCALES)
def test_primary_buttons_stay_inside_the_window(app_factory, tmp_path, screen, scaling):
    """各種螢幕大小與縮放：每個步驟的主要按鈕都必須完整顯示在視窗內。"""
    app = app_factory(scaling=scaling)
    root = app.root
    app._init_geometry(screen)  # 假裝這台電腦的螢幕是 screen 大小
    root.minsize(min(app.px(900), screen[0] - 20), min(app.px(560), screen[1] - 80))
    root.update()
    assert root.winfo_width() <= screen[0] and root.winfo_height() <= screen[1] - 40  # 留給工作列

    def inside(widget):
        root.update()
        assert widget.winfo_ismapped(), widget
        left, top = widget.winfo_rootx(), widget.winfo_rooty()
        tag = (screen, scaling, str(widget))
        assert left >= root.winfo_rootx() and top >= root.winfo_rooty(), tag
        assert left + widget.winfo_width() <= root.winfo_rootx() + root.winfo_width(), tag
        assert top + widget.winfo_height() <= root.winfo_rooty() + root.winfo_height(), tag

    src = photos(tmp_path, 3)
    app.folder_var.set(str(src))
    pump(root, lambda: app.stats is not None)
    inside(app.page1.start_btn)
    run_analysis(app, src)
    inside(app.page2.next_btn)
    inside(app.page2.accept_btn)
    inside(app.page2.skip_btn)
    inside(app.page2.category_buttons[-1])
    if root.winfo_height() / app.theme.scale >= 420:  # 200% 縮放配 768 高的螢幕，邏輯高度只剩 344，本來就放不下
        assert app.tree.canvas.winfo_height() >= min(app.px(120), root.winfo_height() // 6)  # 縮圖格不能被擠到看不見
    app.confirm_current()
    assert app.show_step(3)
    inside(app.page3.copy_btn)
    inside(app.page3.back_btn)
    app.start_copy()
    pump(root, lambda: not app.organizing and app.step == 4)
    inside(app.page4.open_btn)
    inside(app.page4.again_btn)


def test_window_never_larger_than_the_screen(app):
    assert app.root.winfo_width() <= app.root.winfo_screenwidth()
    assert app.root.winfo_height() <= app.root.winfo_screenheight()


# ------------------------------------------------------------------ 步驟 3：預覽與命名
def test_plan_shows_real_output_and_matches_what_runs(app, tmp_path):
    src = tmp_path / "photos"
    make_image(src / "a.jpg", (250, 10, 10), exif_date="2024:05:03 10:00:00")
    make_image(src / "b.jpg", (250, 10, 10), exif_date="2024:05:04 10:00:00")
    run_analysis(app, src)
    for _ in range(2):
        app.confirm_current()
    app.rename_mode_var.set("date_category_seq")
    assert app.show_step(3)
    assert app.page3.output_entry.get() == str(src / "已分類")  # 顯示的是真正的輸出路徑
    planned = {f"{op.dst.parent.name}\\{op.dst.name}" for op in app.plan.ops}
    assert planned == {"紅\\20240503_紅_001.jpg", "紅\\20240504_紅_001.jpg"}
    shown = {app.page3.tree.item(i, "values")[1] for i in app.page3.tree.get_children()}
    assert shown == planned  # 表格上的就是實際會做的
    app.start_copy()
    pump(app.root, lambda: not app.organizing and app.step == 4)
    made = {f"{p.parent.name}\\{p.name}" for p in (src / "已分類").rglob("*") if p.is_file()}
    assert made == planned


def test_plan_includes_ai_results_and_keeps_names(app, tmp_path):
    run_analysis(app, photos(tmp_path, 4))
    assert app.show_step(3)
    assert len(app.plan.ops) == 4 and app.rename_mode_var.get() == "keep"
    assert app.plan.ops[0].dst.name == app.plan.ops[0].src.name  # 預設保留原檔名
    assert not app.page3.excluded_var.get() and not app.page3.low_frame.winfo_ismapped()
    assert app.page3.copy_btn.cget("text") == "開始複製 4 個檔案"
    assert "AI 分好的 4 個、你指定的 0 個" in app.page3.summary_var.get()
    app.show_step(2)
    app.tree.selection_set(app.tree.get_children()[0])
    app.skip_selected()
    assert app.show_step(3)
    assert len(app.plan.ops) == 3 and "不會整理：已略過 1 個" in app.page3.excluded_var.get()


def test_custom_pattern_is_validated_and_previewed(app, tmp_path):
    run_analysis(app, photos(tmp_path, 2))
    app.confirm_current()
    assert app.show_step(3)
    app.rename_mode_var.set("custom")
    app.pattern_var.set("")
    app.refresh_plan()
    assert app.page3.problem_var.get() and app.page3.copy_btn.instate(["disabled"])
    app.pattern_var.set("旅行_{分類}_{序號}")
    app.refresh_plan()
    assert not app.page3.problem_var.get() and app.plan.ops[0].dst.name == "旅行_紅_001.png"


def test_output_default_follows_analyzed_folder(app, tmp_path):
    src = photos(tmp_path, 2)
    run_analysis(app, src)
    app.folder_var.set(str(tmp_path / "別的資料夾"))  # 辨識完又改了輸入欄
    app.confirm_current()
    assert app.show_step(3)
    assert app.plan.source == src and app.plan.output == src / "已分類"  # 仍以辨識當時的資料夾為準


# ------------------------------------------------------------------ 步驟 4：失敗、重試、停止
def _fail_once(monkeypatch, name):
    from media_sorter import organizer

    original = organizer._copy_no_clobber
    state = {"fail": True}

    def flaky(src, dst):
        if state["fail"] and src.name == name:
            raise PermissionError(13, "Permission denied", str(src))
        return original(src, dst)

    monkeypatch.setattr(organizer, "_copy_no_clobber", flaky)
    return state


def test_partial_failure_can_be_retried_into_the_same_log(app, tmp_path, monkeypatch):
    from media_sorter.organizer import list_logs

    src = photos(tmp_path, 3)
    run_analysis(app, src)
    for _ in range(3):
        app.confirm_current()
    state = _fail_once(monkeypatch, "IMG_01.png")
    copy_all(app)
    info = app.run_info
    assert info.done == 2 and len(info.failures) == 1 and len(app.items) == 1  # 失敗的還留在清單
    assert "部分完成" in app.page4.banner_title.get() and "1 個失敗" in app.page4.banner_title.get()
    row = app.page4.fail_tree.item(app.page4.fail_tree.get_children()[0], "values")
    assert row[0] == "IMG_01.png" and "沒有權限" in row[1] and "詳細" not in row[1]  # 白話說明，技術細節收起來
    assert app.page4.retry_btn.cget("text") == "重試失敗的 1 個"

    state["fail"] = False
    app.retry_remaining()
    pump(app.root, lambda: not app.organizing and app.run_info.done == 3)
    assert not info.failures and not app.items
    assert "完成：已複製 3 個檔案" in app.page4.banner_title.get()
    logs = [i for i in list_logs(tmp_path / "logs") if i.kind == "active"]
    assert len(logs) == 1 and logs[0].count == 3  # 同一次整理只有一份紀錄


def test_technical_details_are_collapsed_until_asked(app, tmp_path, monkeypatch):
    run_analysis(app, photos(tmp_path, 2))
    app.confirm_current()
    app.confirm_current()
    _fail_once(monkeypatch, "IMG_00.png")
    copy_all(app)
    p = app.page4
    assert not p.details_text.winfo_ismapped()
    app.toggle_fail_details()
    app.root.update()
    assert p.details_text.winfo_ismapped() and "Permission denied" in p.details_text.get("1.0", "end")
    app.toggle_fail_details()
    assert not p.details_text.winfo_ismapped()


def test_stop_finishes_current_file_then_can_continue(app, tmp_path, monkeypatch):
    from media_sorter import organizer

    src = photos(tmp_path, 4)
    run_analysis(app, src)
    for _ in range(4):
        app.confirm_current()
    original = organizer._copy_no_clobber

    def copy_then_stop(a, b):
        original(a, b)
        app.copy_stop.set()  # 模擬使用者在第一個檔案複製時按了「完成目前檔案後停止」

    monkeypatch.setattr(organizer, "_copy_no_clobber", copy_then_stop)
    assert app.show_step(3)
    app.start_copy()
    assert app.page3.copy_btn.cget("text") == "完成目前檔案後停止"
    pump(app.root, lambda: not app.organizing and app.step == 4)
    assert app.run_info.done == 1 and app.run_info.stopped and not app.run_info.failures
    assert "已停止：已複製 1／4 個" in app.page4.banner_title.get()
    assert app.page4.retry_btn.cget("text") == "繼續複製剩下的 3 個"

    monkeypatch.setattr(organizer, "_copy_no_clobber", original)
    app.retry_remaining()
    pump(app.root, lambda: not app.organizing and app.run_info.done == 4)
    assert len([p for p in (src / "已分類").rglob("*") if p.is_file()]) == 4


def test_actions_are_disabled_while_copying(app, tmp_path):
    run_analysis(app, photos(tmp_path, 2))
    app.confirm_current()
    assert app.show_step(3)
    app.organizing = True
    app._set_busy(True)
    assert app.page3.back_btn.instate(["disabled"]) and app.header.records_btn.instate(["disabled"])
    assert not app.show_step(2)
    app.organizing = False
    app._set_busy(False)
    assert app.page3.back_btn.instate(["!disabled"])


def test_start_over_clears_the_session(app, tmp_path):
    run_analysis(app, photos(tmp_path, 3))
    app.tree.selection_set(app.tree.get_children()[1:])
    app.skip_selected()
    copy_all(app)
    assert app.items  # 略過的兩個還在
    app.start_over()  # askyesno 預設回答「否」→ 不離開
    assert app.step == 4 and app.items
    from tkinter import messagebox

    messagebox.askyesno = lambda *a, **k: True
    app.start_over()
    assert app.step == 1 and not app.items and app.run_info is None


# ------------------------------------------------------------------ 整理紀錄與危險操作
def test_records_dialog_lists_batches_and_names_actions_clearly(app, tmp_path, monkeypatch):
    import media_sorter.app as app_module

    src = photos(tmp_path, 2)
    run_analysis(app, src)
    app.confirm_current()
    app.confirm_current()
    copy_all(app)
    app.open_records()
    win = app.records_win
    app.root.update()
    assert len(win.tree.get_children()) == 1
    values = win.tree.item(win.tree.get_children()[0], "values")
    assert values[1] == str(src) and values[2] == str(src / "已分類") and values[3] == 2 or values[3] == "2"
    assert win.undo_btn.cget("text") == "移除這次建立的複本…"
    assert win.remove_btn.cget("text") == "將這批原檔移到回收筒…"
    assert str(win.remove_btn.cget("style")) == "Danger.TButton"
    assert win.focus_get() is not win.remove_btn  # 危險操作不是預設焦點
    assert win.undo_btn.instate(["!disabled"]) and win.remove_btn.instate(["!disabled"])

    shown = []
    monkeypatch.setattr(app_module, "confirm_with_list", lambda *a, **k: shown.append(k) or False)
    win._act(app.remove_originals_log)
    assert shown and shown[0]["danger"] is True  # 需要再確認一次，而且預設是取消
    win.destroy()


def test_changed_log_after_confirmation_is_refused(app, tmp_path, monkeypatch):
    import media_sorter.app as app_module

    src = photos(tmp_path, 2)
    run_analysis(app, src)
    app.confirm_current()
    app.confirm_current()
    copy_all(app)
    log = app.run_info.log_path
    copies = [p for p in (src / "已分類").rglob("*") if p.is_file()]

    def confirm_then_tamper(*args, **kwargs):
        with open(log, "a", encoding="utf-8") as f:
            f.write("\n")  # 使用者按下確定之前，紀錄被改動
        return True

    problems = []
    monkeypatch.setattr(app_module, "confirm_with_list", confirm_then_tamper)
    monkeypatch.setattr(app_module, "show_problem", lambda *a, **k: problems.append(a))
    app.undo_log(log)
    pump(app.root, lambda: bool(problems))
    assert "在你確認之後被改變" in problems[0][1]
    assert all(p.exists() for p in copies) and log.exists()  # 什麼都沒做


def test_unexpected_errors_are_explained_not_dumped(app, monkeypatch):
    import media_sorter.app as app_module

    shown = []
    monkeypatch.setattr(app_module, "show_problem", lambda master, what, todo="", details="", **k:
                        shown.append((what, todo, details)))
    monkeypatch.setattr(app, "log_error", lambda text: None)
    try:
        raise RuntimeError("boom")
    except RuntimeError as exc:
        app.on_tk_error(type(exc), exc, exc.__traceback__)
    what, todo, details = shown[0]
    assert "非預期" in what and "error.log" in todo and "Traceback" in details and "boom" in details


def test_help_opens_the_usage_document(app, monkeypatch):
    import media_sorter.app as app_module

    opened = []
    monkeypatch.setattr(app_module, "open_in_file_manager", opened.append)
    app.open_help()
    assert opened and opened[0].name.startswith("使用說明與免責聲明") and opened[0].exists()


def test_mouse_wheel_scrolls_only_the_area_under_the_pointer(app, tmp_path):
    run_analysis(app, photos(tmp_path, 2))
    app.confirm_current()
    app.root.geometry("900x520")
    assert app.show_step(3)
    pump(app.root, lambda: app.page3.scroll.canvas.yview()[1] < 1.0)  # 內容比視窗高，才需要捲動
    scroll = app.page3.scroll
    target = scroll.inner.winfo_children()[0]  # 內容裡面的元件（滑鼠通常在這種元件上）
    x, y = target.winfo_rootx() + 5, target.winfo_rooty() + 5
    target.event_generate("<MouseWheel>", delta=-120, rootx=x, rooty=y)
    app.root.update()
    assert scroll.canvas.yview()[0] > 0
    scroll.scroll_to_top()
    # 滑鼠在其他區域（例如上方的步驟列）時，不會捲動內容
    header = app.header.labels[0]
    header.event_generate("<MouseWheel>", delta=-120, rootx=header.winfo_rootx() + 2, rooty=header.winfo_rooty() + 2)
    app.root.update()
    assert scroll.canvas.yview()[0] == 0


def test_step2_gives_keyboard_focus_to_the_grid(app, tmp_path, monkeypatch):
    """按下「開始辨識」之後焦點在按鈕上；進到檢查分類時要把焦點交給縮圖格，之後按數字鍵／Enter／S 才會作用在檔案上。"""
    focused = []
    monkeypatch.setattr(app.tree, "focus_set", lambda: focused.append(True))
    run_analysis(app, photos(tmp_path, 3))
    assert focused  # 進到步驟 2 時，程式主動把焦點交給縮圖格


def test_shortcut_keys_accept_skip_and_assign(app, tmp_path):
    from media_sorter.analysis import CONFIRMED, PENDING, SKIPPED

    run_analysis(app, photos(tmp_path, 4))
    for sequence in ("<Return>", "1", "2", "9", "0", "s", "S", "<Control-z>", "<Control-a>", "<space>"):  # 快速鍵確實綁定在視窗上
        assert app.root.bind(sequence), sequence
    first = app.current
    assert app.handle_key("Return", "Canvas")  # Enter：採用 AI 建議
    assert app.items[first].status == CONFIRMED and app.items[first].chosen == "紅" and app.current != first
    second = app.current
    assert app.handle_key("s", "Canvas")
    assert app.items[second].status == SKIPPED
    third = app.current
    assert app.handle_key("2", "Canvas")  # 數字鍵＝下面的第 2 個分類按鈕
    assert app.items[third].status == CONFIRMED and app.items[third].chosen == "綠"
    fourth = app.current
    assert fourth not in (first, second, third)
    # 在輸入欄位、下拉選單或按鈕上，按鍵屬於那個元件，不會誤觸
    for widget_class in ("TEntry", "TCombobox", "Text"):
        assert not app.handle_key("Return", widget_class) and not app.handle_key("s", widget_class)
    assert not app.handle_key("Return", "TButton") and not app.handle_key("space", "TButton")  # 按鈕上是「按下那個按鈕」
    assert app.items[fourth].status == PENDING
    assert app.handle_key("ctrl-z", "Canvas")  # 撤回剛才的「綠」
    assert app.items[third].status == PENDING and app.current == third
    assert app.handle_key("ctrl-a", "Canvas") and len(app.tree.selection()) == 4
    app.show_step(1)
    assert not app.handle_key("Return", "Canvas")  # 其他步驟不作用




def test_grid_selection_works_like_a_file_manager(app, tmp_path):
    """縮圖格：點一下選取、Ctrl 加選、Shift 連選、方向鍵移動、按空白處取消選取。"""
    from types import SimpleNamespace

    run_analysis(app, photos(tmp_path, 6))
    grid = app.tree
    app.root.update()
    iids = grid.get_children()

    def click(pos, **mods):
        x0, y0, x1, y1 = grid._cell_box(pos)
        grid._on_click(SimpleNamespace(x=(x0 + x1) // 2, y=(y0 + y1) // 2), **mods)

    click(1)
    assert grid.selection() == (iids[1],) and app.current == app._index_of(iids[1])
    click(3, toggle=True)
    assert grid.selection() == (iids[1], iids[3])
    click(4, extend=True)  # 從最後點的那個（3）連選到 4
    assert grid.selection() == (iids[3], iids[4])
    grid._move(0, 1)  # 方向鍵：右
    assert grid.selection() == (iids[5],) and grid.focus() == iids[5]
    grid._on_click(SimpleNamespace(x=10_000, y=10_000))  # 點在縮圖以外的地方
    assert grid.selection() == () and app.current is None and app.page2.view_state == "placeholder"
    grid.select_all()
    assert len(grid.selection()) == 6 and "已選取 6 個檔案" in app.page2.sel_var.get()
