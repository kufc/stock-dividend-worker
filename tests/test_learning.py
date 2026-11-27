"""從使用者的修正學習（同一批檔案內）。

使用者指定分類或略過的檔案是「活範例」：其餘還沒處理的檔案裡，跟某個範例很像、但 AI 給的分類跟使用者當時的決定
不一樣的，放進「需檢查」。只會多提醒，不會自動改分類，也不會讓原本要檢查的檔案變成不用檢查。
"""

import sys
from pathlib import Path

import numpy as np
import pytest

from conftest import pump, run_analysis
from helpers import make_image
from media_sorter import analysis
from media_sorter.analysis import CONFIRMED, PENDING, SKIPPED, Item, learn_from_decisions, neighbor_hints
from media_sorter.classifier import normalize
from media_sorter.config import Category

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
CATS = [Category("紅"), Category("綠")]


def unit(*values):
    return normalize(np.asarray(values, dtype=np.float32))


def make_item(name, embedding, top, status=PENDING, chosen=None):
    probs = np.array([0.95, 0.05] if top == "紅" else [0.05, 0.95])
    return Item(Path(name), "image", embedding=embedding, probs=probs, prob_names=("紅", "綠"), status=status, chosen=chosen)


def test_neighbor_hints_only_fire_on_close_disagreement():
    examples = np.stack([unit(1, 0, 0), unit(0, 1, 0)])
    labels = ["綠", None]  # 使用者把第一個改成「綠」、把第二個略過
    pending = np.stack([unit(1, 0.05, 0), unit(1, 0.05, 0), unit(0.02, 1, 0), unit(0.6, 0.6, 0.5), unit(0, 0, 1)])
    predictions = ["紅", "綠", "紅", "紅", "紅"]
    hints = neighbor_hints(pending, predictions, examples, labels, 0.8)
    assert hints[0][0] == "綠" and hints[0][1] > 0.99     # 很像被改成「綠」的那張，AI 卻說「紅」→ 提醒
    assert hints[1] is None                               # 很像，而且 AI 跟使用者一致 → 不提醒
    assert hints[2][0] is None and hints[2][1] > 0.99     # 很像被略過的那張 → 提醒（可能也不屬於任何分類）
    assert hints[3] is None and hints[4] is None          # 不夠像 → 不提醒
    assert neighbor_hints(pending, predictions, np.zeros((0, 3)), [], 0.8) == [None] * 5
    assert neighbor_hints(np.zeros((0, 3)), [], examples, labels, 0.8) == []


def test_large_inputs_are_processed_in_chunks(monkeypatch):
    monkeypatch.setattr(analysis, "_HINT_CHUNK", 7)
    rng = np.random.default_rng(0)
    pending, examples = normalize(rng.normal(size=(50, 8))), normalize(rng.normal(size=(9, 8)))
    predictions, labels = ["a"] * 50, ["b"] * 9
    chunked = neighbor_hints(pending, predictions, examples, labels, 0.3)
    monkeypatch.setattr(analysis, "_HINT_CHUNK", 10_000)
    whole = neighbor_hints(pending, predictions, examples, labels, 0.3)
    # 分批與一次算完的相似度可能差在浮點數最後幾位（Windows 的數學函式庫會，Wine 下實測差 1e-7），所以比到小數第五位
    assert [h and h[0] for h in chunked] == [h and h[0] for h in whole] and any(chunked)
    assert all(abs(a[1] - b[1]) < 1e-5 for a, b in zip(chunked, whole) if a)


def test_learning_flags_lookalikes_and_never_changes_categories():
    fixed = make_item("fixed.jpg", unit(1, 0, 0), "紅", CONFIRMED, "綠")       # AI 說紅，使用者改成綠
    twin = make_item("twin.jpg", unit(1, 0.03, 0), "紅")                        # 長得幾乎一樣，AI 也說紅
    other = make_item("other.jpg", unit(0, 0, 1), "紅")
    agree = make_item("agree.jpg", unit(1, 0.02, 0.01), "綠")                   # AI 本來就說綠
    items = [fixed, twin, other, agree]
    assert not twin.is_low(CATS, 0.7, 0.0)
    assert learn_from_decisions(items, CATS) == 1
    assert twin.neighbor_hint[0] == "綠" and twin.is_low(CATS, 0.7, 0.0)        # 進「需檢查」
    assert (twin.status, twin.chosen, twin.best(CATS)[0]) == (PENDING, None, "紅")  # 分類與狀態都沒被動
    assert other.neighbor_hint is None and agree.neighbor_hint is None and fixed.neighbor_hint is None

    fixed.status, fixed.chosen = PENDING, None                                   # 使用者撤回那個修正
    assert learn_from_decisions(items, CATS) == 0 and twin.neighbor_hint is None and not twin.is_low(CATS, 0.7, 0.0)

    fixed.status = SKIPPED                                                       # 改成略過
    learn_from_decisions(items, CATS)
    assert twin.neighbor_hint[0] is None and twin.is_low(CATS, 0.7, 0.0)
    twin.status, twin.chosen = CONFIRMED, "紅"                                   # 使用者看過、決定維持紅
    learn_from_decisions(items, CATS)
    assert twin.neighbor_hint is None                                            # 已經處理過的不再帶提示


def test_a_closer_agreeing_example_wins_over_a_farther_disagreeing_one():
    near = make_item("near.jpg", unit(1, 0.02, 0), "紅", CONFIRMED, "紅")
    far = make_item("far.jpg", unit(1, 0.5, 0), "紅", CONFIRMED, "綠")
    target = make_item("t.jpg", unit(1, 0.03, 0), "紅")
    learn_from_decisions([near, far, target], CATS)
    assert target.neighbor_hint is None


def test_app_learns_within_the_batch(app, tmp_path):
    """介面：把一張改掉之後，長得很像的其他檔案進「需檢查」，說明會講原因；撤回就恢復。"""
    src = tmp_path / "photos"
    for i in range(4):
        make_image(src / f"red_{i}.png", (250, 10 + i, 10))      # 四張幾乎一樣的紅色
    make_image(src / "green.png", (10, 250, 10))
    run_analysis(app, src)
    app.select_bucket("all")
    index = next(i for i, it in enumerate(app.items) if it.path.name == "red_0.png")
    assert not any(app._is_low(it) for it in app.items)
    app.tree.selection_set(app._iid(index))
    app.skip_selected()                                           # 使用者：這張不屬於任何分類
    pump(app.root, lambda: any(it.neighbor_hint for it in app.items))
    flagged = sorted(it.path.name for it in app.items if app._is_low(it))
    assert flagged == ["red_1.png", "red_2.png", "red_3.png"]     # 其他三張紅的被拉進需檢查；綠的不受影響
    assert app.page2.buckets.item("low", "values")[0] == "3"
    twin = next(i for i, it in enumerate(app.items) if it.path.name == "red_1.png")
    app.tree.selection_set(app._iid(twin))
    app.on_select()
    assert "很像" in app.page2.notice_var.get() and "略過" in app.page2.notice_var.get()
    assert all(it.status == PENDING for it in app.items if it.path.name != "red_0.png")  # 沒有任何檔案被自動改掉
    assert app.show_step(3)
    app.root.update()
    assert {op.src.name: op.dst.parent.name for op in app.plan.ops if op.src.name.startswith("red_")} == {
        "red_1.png": "未分類", "red_2.png": "未分類", "red_3.png": "未分類"}     # 預設放未分類，不是悄悄進「紅」
    app.show_step(2)
    app.undo_last() if hasattr(app, "undo_last") else None


def test_threshold_has_a_documented_basis_on_the_dev_set():
    """0.8 的依據：開發集上模擬「使用者看完需檢查的檔案」，未知誤收明顯下降，而且分對的檔案一個都沒被多標。"""
    import evaluate

    for preset in ("standard", "accurate"):
        before, after = evaluate.learning_simulation(evaluate.load("dev", preset))
        assert after["unknown_false_accept"] <= before["unknown_false_accept"] - 0.05
        assert after["correct_flagged"] <= before["correct_flagged"] + 0.002
        assert after["flagged"] <= before["flagged"] + 0.05
    assert analysis.NEIGHBOR_MIN_SIMILARITY == 0.8
