"""視窗程式的端對端測試：辨識 → 確認 → 預覽 → 複製 → 移除複本／處理原檔。需要圖形環境（Linux 可用 xvfb-run）。"""

import os
import sys

import pytest

tk = pytest.importorskip("tkinter")

from pathlib import Path  # noqa: E402

from conftest import copy_all, pump, run_analysis  # noqa: E402
from helpers import make_image, make_video  # noqa: E402


def _display_available() -> bool:
    if sys.platform == "win32" or sys.platform == "darwin":
        return True
    return bool(os.environ.get("DISPLAY"))


pytestmark = pytest.mark.skipif(not _display_available(), reason="需要圖形環境")


def test_full_flow(app, tmp_path, monkeypatch):
    from media_sorter.analysis import CONFIRMED, PENDING

    src = tmp_path / "photos"
    red = make_image(src / "r1.jpg", (250, 10, 10))
    make_image(src / "r2.jpg", (240, 20, 20))
    make_image(src / "sub" / "g.png", (10, 250, 10))
    video = make_video(src / "b.mp4", (10, 10, 250))

    run_analysis(app, src)
    assert app.step == 2  # 第一批結果出來就自動進到「檢查分類」
    assert len(app.tree.get_children()) == 4  # 「全部」
    assert {item.path.name: item.best(app.categories)[0] for item in app.items} == {
        "r1.jpg": "紅", "r2.jpg": "紅", "g.png": "綠", "b.mp4": "藍"}
    assert app.current is not None  # 第一個檔案自動選取並預覽

    # 左邊的分類清單：數量就是 AI 的結果；點一個分類只看那個分類的檔案
    assert app.page2.buckets.item("cat:紅", "values")[0] == "2"
    app.select_bucket("cat:紅")
    assert len(app.tree.get_children()) == 2
    # 分錯的：選起來、按正確的分類 → 立刻生效、從這個分類消失，選取跳到下一個
    r1 = app._iid(next(i for i, it in enumerate(app.items) if it.path.name == "r1.jpg"))
    app.tree.selection_set(r1)
    app.assign("綠")
    assert app.items[app._index_of(r1)].status == CONFIRMED and app.items[app._index_of(r1)].chosen == "綠"
    assert len(app.tree.get_children()) == 1 and app.tree.selection() == app.tree.get_children()
    assert app.items[app._index_of(app.tree.selection()[0])].status == PENDING
    app.select_bucket("all")

    # 新增分類 → 重新計算，不影響你指定過的
    app.add_category("黃", ["yellow"])
    assert app.items[app._index_of(r1)].chosen == "綠"
    assert app.items[0].probs.shape == (4,)

    # 步驟 3：不用逐一確認，AI 分好的加上你指定的全部都會整理
    assert app.show_step(3)
    assert len(app.plan.ops) == 4
    planned = sorted(f"{op.dst.parent.name}/{op.dst.name}" for op in app.plan.ops)
    app.start_copy()
    pump(app.root, lambda: not app.organizing and app.step == 4)
    out = src / "已分類"
    assert sorted(p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file()) == [
        "紅/r2.jpg", "綠/g.png", "綠/r1.jpg", "藍/b.mp4"]
    assert planned == sorted(p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file())  # 預覽＝實際結果
    assert red.exists() and video.exists()  # 整理只複製，原檔不動
    assert not app.items and app.run_info.done == 4

    # 移除這次建立的複本（＝復原）
    import media_sorter.app as app_module
    from media_sorter.organizer import latest_log

    monkeypatch.setattr(app_module, "confirm_with_list", lambda *a, **k: True)
    app.undo_log(app.run_info.log_path)
    pump(app.root, lambda: not app.organizing)
    assert red.exists() and video.exists()
    assert not any(p.is_file() for p in out.rglob("*"))
    assert latest_log(tmp_path / "logs") is None


def test_manual_choice_skip_and_batch(app, tmp_path):
    from media_sorter.analysis import CONFIRMED, SKIPPED

    src = tmp_path / "photos"
    for i in range(3):
        make_image(src / f"{i}.png", (250, 10, 10))
    run_analysis(app, src)

    # 多選後一次改分類
    iids = app.tree.get_children()
    app.tree.selection_set(iids[:2])
    app.assign("綠")
    assert [app.items[app._index_of(i)].chosen for i in iids[:2]] == ["綠", "綠"]
    assert all(app.items[app._index_of(i)].status == CONFIRMED for i in iids[:2])

    # 單一項目略過
    app.tree.selection_set(iids[2])
    app.skip_current()
    assert app.items[app._index_of(iids[2])].status == SKIPPED
    assert app.page2.buckets.item("skipped", "values")[0] == "1"

    # 略過的不整理；保留原檔名（預設）
    copy_all(app)
    assert sorted(p.name for p in (src / "已分類" / "綠").iterdir()) == ["0.png", "1.png"]
    assert (src / "0.png").exists()  # 原檔不動
    assert len(app.items) == 1  # 略過的那個還留在清單裡


def test_remove_originals_after_review(app, tmp_path, monkeypatch):
    import shutil

    import media_sorter.app as app_module
    from media_sorter import organizer

    bin_dir = tmp_path / "RECYCLE"
    bin_dir.mkdir()
    monkeypatch.setattr(organizer, "_send2trash", lambda p: shutil.move(p, bin_dir / Path(p).name))
    src = tmp_path / "photos"
    red = make_image(src / "r.jpg", (250, 10, 10))
    app.rename_mode_var.set("category_seq")
    run_analysis(app, src)
    app.confirm_current()
    copy_all(app)
    copy = src / "已分類" / "紅" / "紅_001.jpg"
    assert red.exists() and copy.exists()

    monkeypatch.setattr(app_module, "confirm_with_list", lambda *a, **k: True)
    app.remove_originals_here()
    pump(app.root, lambda: not app.organizing)
    assert not red.exists() and (bin_dir / "r (整理前).jpg").exists() and copy.exists()  # 原檔進回收筒，複本留著
    assert organizer.latest_log(tmp_path / "logs") is None
