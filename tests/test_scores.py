"""第九輪外部審查（P1）：分數是分類之間的相對值，不能呈現得像正確率；沒把握的預設放「未分類」；分類太少要另外處理。"""

from pathlib import Path

import numpy as np
import pytest

from conftest import run_analysis
from helpers import make_image
from media_sorter import app as app_module
from media_sorter.analysis import Item, _apply_scores, rescore
from media_sorter.classifier import FEW_CATEGORIES, normalize, score, score_frames
from media_sorter.config import DEFAULT_CATEGORIES, Category

MANUAL = Path(__file__).resolve().parent / "manual"


def unit(*values):
    return normalize(np.asarray(values, dtype=np.float64))


# ---------------------------------------------------------------------------- 分類太少（1～2 個）
def test_one_category_without_other_is_always_100_percent():
    """重現審查的例子：只有一個分類時，就算圖片向量跟分類向量方向相反，softmax 還是 100%。"""
    image, category = unit(1, 0, 0), unit(-1, 0, 0)
    assert score(image[None], category[None], 100.0)[0, 0] == 1.0


def test_one_category_is_scored_against_other_things():
    category, other = unit(1, 0, 0), unit(0, 1, 0)
    opposite, alike, between = unit(-1, 0, 0), unit(1, 0.05, 0), unit(1, 1, 0)
    probs = score(np.stack([opposite, alike, between]), category[None], 100.0, other)
    assert probs.shape == (3, 1)
    assert probs[0, 0] < 0.01      # 完全不像：不再是 100%
    assert probs[1, 0] > 0.99      # 很像：仍然很高
    assert abs(probs[2, 0] - 0.5) < 1e-6  # 跟「其他東西」一樣像：一半


def test_two_categories_neither_fits():
    cats, other = np.stack([unit(1, 0, 0), unit(0, 1, 0)]), unit(0, 0, 1)
    neither = unit(0.1, 0.05, 1)
    plain = score(neither[None], cats, 100.0)[0]
    assert plain.max() > 0.9  # 沒有「其他」：兩個都不像，其中一個還是拿到高分
    with_other = score(neither[None], cats, 100.0, other)[0]
    assert with_other.max() < 0.01 and with_other.sum() < 1  # 一起比：兩個都很低，總和不到 1


def test_three_or_more_categories_are_unchanged():
    """分類夠多時不加「其他」：維持用真實照片調過的行為（門檻、評測都是以這個為準）。"""
    assert FEW_CATEGORIES == 3
    rng = np.random.default_rng(0)
    cats, other, images = normalize(rng.normal(size=(3, 8))), normalize(rng.normal(size=8)), normalize(rng.normal(size=(5, 8)))
    assert np.array_equal(score(images, cats, 50.0, other), score(images, cats, 50.0))
    assert np.allclose(score(images, cats, 50.0, other).sum(axis=1), 1.0)


def test_video_frames_use_the_same_rule():
    category, other = unit(1, 0, 0), unit(0, 1, 0)
    frames = np.stack([unit(-1, 0, 0), unit(0, 1, 0)])
    assert score_frames(frames, category[None], 100.0, other)[0] < 0.01
    assert score_frames(frames, category[None], 100.0)[0] == 1.0


class OneVector:
    logit_scale = 100.0

    def __init__(self, other):
        self._other = other

    def encode_categories(self, categories):
        return np.stack([unit(1, 0, 0), unit(0, 0, 1)][:len(categories)])

    def vocabulary_embeddings(self):
        from media_sorter.vocabulary import VOCABULARY

        return normalize(np.random.default_rng(1).random((len(VOCABULARY), 3)))

    def other_embedding(self):
        return self._other


@pytest.mark.parametrize("count", [1, 2])
def test_items_that_fit_nothing_are_flagged_with_few_categories(count):
    clf = OneVector(unit(0, 1, 0))
    categories = [Category("貓"), Category("狗")][:count]
    names = tuple(c.name for c in categories)
    stray = Item(Path("stray.jpg"), "image", embedding=unit(0.05, 1, 0.02))
    cat = Item(Path("cat.jpg"), "image", embedding=unit(1, 0.02, 0))
    for item in (stray, cat):
        _apply_scores(item, clf.encode_categories(categories), names, clf.vocabulary_embeddings(), 100.0, clf.other_embedding())
    assert stray.best(categories)[1] < 0.5 and stray.is_low(categories, 0.5, 0.0)
    assert cat.best(categories) == ("貓", pytest.approx(1.0, abs=1e-3)) and not cat.is_low(categories, 0.5, 0.0)
    rescore([stray, cat], clf, categories)  # 改分類後重算也走同一條規則
    assert stray.best(categories)[1] < 0.5 and not cat.is_low(categories, 0.5, 0.0)


@pytest.mark.parametrize("preset", ["standard", "accurate"])
@pytest.mark.parametrize("names", [["貓"], ["美食"], ["貓", "狗"], ["風景", "文件收據"]])
def test_few_categories_on_real_photos(preset, names):
    """真模型的向量（1,243 張，開發用的那一批）：只設 1～2 個分類時，屬於它的照片幾乎都分對而且有把握；
    不屬於的照片大部分被標成「需檢查」（預設放到「未分類」），不會全部硬塞進來。"""
    from test_real_embeddings import Recorded

    path = MANUAL / f"embeddings_{preset}.npz"
    if not path.exists():
        pytest.skip(f"沒有 {path.name}")
    recorded = Recorded(path)
    answers = dict(zip(recorded.names, recorded.labels, strict=True))
    categories = [Category(c.name, list(c.prompts)) for c in DEFAULT_CATEGORIES if c.name in names]
    items = recorded.items(categories)
    inside = [it for it in items if answers[it.path.name] in names]
    outside = [it for it in items if answers[it.path.name] not in names and answers[it.path.name] != "?"]
    right = sum(it.best(categories)[0] == answers[it.path.name] and not it.is_low(categories, 0.5, 0.0) for it in inside)
    flagged = sum(it.is_low(categories, 0.5, 0.0) for it in outside)
    assert right / len(inside) >= 0.97
    assert flagged / len(outside) >= 0.6  # 實測 66%～86%；仍有一到三成會被歸進來，介面會先警告
    assert max(it.best(categories)[1] for it in outside) <= 1.0
    assert sum(it.best(categories)[1] > 0.999 for it in outside) < len(outside) * 0.25  # 不再「全部都是 100%」


# ---------------------------------------------------------------------------- 呈現：名次，不是百分比
def test_score_note_never_calls_it_accuracy_and_describes_the_margin():
    note = app_module.score_note([("貓", 0.93), ("狗", 0.05), ("風景", 0.02)], 5)
    assert "不代表正確率" in note and "第一名明顯領先" in note and "%" not in note
    assert "前兩名很接近" in app_module.score_note([("貓", 0.45), ("狗", 0.40)], 5)
    assert "領先不多" in app_module.score_note([("貓", 0.6), ("狗", 0.2)], 5)
    assert "其他東西" in app_module.score_note([("貓", 0.55)], 1)
    assert app_module.score_note([], 5) == ""


def test_no_percent_sign_anywhere_on_the_review_page(app, tmp_path):
    """檢查分類那一頁所有看得到的文字裡都沒有「%」：分數不呈現成機率。"""
    src = tmp_path / "photos"
    for i, color in enumerate([(250, 10, 10), (10, 250, 10), (128, 128, 128)]):
        make_image(src / f"IMG_{i}.png", color)
    run_analysis(app, src)
    app.root.update()

    def texts(widget):
        for child in widget.winfo_children():
            try:
                yield str(child.cget("text"))
            except Exception:  # noqa: BLE001 - 沒有 text 的元件
                pass
            yield from texts(child)

    page = app.page2
    for index in range(len(app.items)):
        app.tree.selection_set(app._iid(index))
        app.on_select()
        app.root.update()
        shown = list(texts(page)) + [page.score_var.get(), page.notice_var.get()]
        assert not [t for t in shown if "%" in t], [t for t in shown if "%" in t]
    buttons = [b.cget("text") for b in page.candidate_buttons if b.winfo_ismapped()]
    assert buttons and buttons[0].startswith("①") and "AI 建議" in buttons[0]


def test_low_confidence_default_is_the_unsorted_folder(app, tmp_path):
    from media_sorter.views import DEFAULT_LOW_MODE, LOW_MODES

    assert DEFAULT_LOW_MODE == "unsorted" == LOW_MODES[0][0] and app.low_mode_var.get() == "unsorted"
    app.low_mode_var.set("suggest")
    src = tmp_path / "photos"
    make_image(src / "gray.png", (128, 128, 128))
    make_image(src / "red.png", (250, 10, 10))
    run_analysis(app, src)  # 每次重新辨識都回到預設，不沿用上一次改過的選擇
    assert app.low_mode_var.get() == "unsorted"
    app.select_bucket("low")
    assert "未分類" in app.page2.grid_hint_var.get()


def test_one_category_end_to_end_puts_strays_in_unsorted(app, tmp_path, monkeypatch):
    """只有一個分類：不像它的檔案進「需檢查」，預設整理到「未分類」，而不是 100% 分進唯一的分類。"""
    from fakes import FakeClassifier

    class WithOther(FakeClassifier):
        def other_embedding(self):
            return normalize(np.asarray([0.2, 1.0, 1.0]))

    app.classifier_factory = WithOther
    app.set_categories([Category("紅")])
    src = tmp_path / "photos"
    make_image(src / "red.png", (250, 10, 10))
    make_image(src / "teal.png", (10, 200, 200))
    run_analysis(app, src)
    by_name = {it.path.name: it for it in app.items}
    assert not app._is_low(by_name["red.png"]) and app._is_low(by_name["teal.png"])
    assert by_name["teal.png"].best(app.categories)[1] < 0.5
    assert app.show_step(3)
    app.root.update()
    assert {op.src.name: op.dst.parent.name for op in app.plan.ops} == {"red.png": "紅", "teal.png": "未分類"}
