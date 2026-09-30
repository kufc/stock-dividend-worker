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
    assert app.step == 2  # 第一批結果出來就自動進到「確認分類」
    assert len(app.tree.get_children()) == 4
    assert {item.path.name: item.best(app.categories)[0] for item in app.items} == {
        "r1.jpg": "紅", "r2.jpg": "紅", "g.png": "綠", "b.mp4": "藍"}
    assert app.current is not None  # 第一個待確認項目自動選取並預覽

    # 按「1」只是選取候選分類；按 Enter（確認並看下一個）才會確認並跳到下一個
    first = app.current
    app.pick_suggestion(0)
    assert app.items[first].status == PENDING
    app.confirm_current()
    assert app.items[first].status == CONFIRMED
    assert app.current is not None and app.current != first

    # 依「建議分類」篩選
    app.filter_var.set("建議分類：紅")
    app.refresh_tree()
    assert len(app.tree.get_children()) == 2
    app.filter_var.set("全部")
    app.refresh_tree()

    # 新增分類 → 重新計算，不影響已確認的項目
    app.add_category("黃", ["yellow"])
    assert app.items[first].status == CONFIRMED
    assert app.items[0].probs.shape == (4,)

    # 步驟 3：預設只整理已確認的；勾選後才連「建議較明確」但未確認的一起整理
    assert app.show_step(3)
    assert len(app.plan.ops) == 1
    app.include_pending_var.set(True)
    app.refresh_plan()
    assert len(app.plan.ops) == 4
    app.rename_mode_var.set("category_seq")
    app.refresh_plan()
    planned = sorted(f"{op.dst.parent.name}/{op.dst.name}" for op in app.plan.ops)
    app.start_copy()
    pump(app.root, lambda: not app.organizing and app.step == 4)
    out = src / "已分類"
    assert sorted(p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file()) == [
        "紅/紅_001.jpg", "紅/紅_002.jpg", "綠/綠_001.png", "藍/藍_001.mp4"]
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

    # 多選後批次套用分類
    iids = app.tree.get_children()
    app.tree.selection_set(iids[:2])
    app.root.update()
    app.batch_var.set("綠")
    app.apply_batch()
    assert [app.items[app._index_of(i)].chosen for i in iids[:2]] == ["綠", "綠"]
    assert all(app.items[app._index_of(i)].status == CONFIRMED for i in iids[:2])

    # 單一項目略過
    app.tree.selection_set(iids[2])
    app.root.update()
    app.skip_current()
    assert app.items[app._index_of(iids[2])].status == SKIPPED

    # 全部決定完了：右邊顯示「分類確認完成」
    assert app.page2.view_state == "finished" and app.current is None

    # 只整理已確認的，保留原檔名（預設）
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
