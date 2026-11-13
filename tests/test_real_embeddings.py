"""用真模型事先算好的向量（tests/manual/embeddings_*.npz）檢查判斷規則，不需要下載模型。

1,243 張有標記的真實圖片：每個預設分類 45～92 張（來源見 tests/manual/photo_sources.txt），
加上不屬於任何預設分類的 ImageNette 七類、圖表、海報、商品照、雜物，以及梗圖。
向量由 tests/manual/dump_embeddings.py 產生；內建的分類描述改了之後要重新產生。
"""

from pathlib import Path

import numpy as np
import pytest

from media_sorter.analysis import Item, _apply_scores, suggest_categories
from media_sorter.classifier import OTHER_TEXTS, SIMILARITY_FLOOR, covered_tags, normalize
from media_sorter.config import DEFAULT_CATEGORIES, Category
from media_sorter.prompts import LEGACY_DEFAULT_PROMPTS
from media_sorter.vocabulary import VOCABULARY

MANUAL = Path(__file__).resolve().parent / "manual"
NONE = "（都不像）"


class Recorded:
    """假裝是 Classifier：文字向量直接查事先存好的結果。"""

    def __init__(self, path: Path):
        data = np.load(path)
        self.names = data["image_names"].tolist()
        self.labels = data["image_labels"].tolist()
        self.groups = data["image_groups"].tolist() if "image_groups" in data.files else [""] * len(self.names)
        self.images = normalize(data["image_emb"].astype(np.float64))
        self.frame_owner = data["frame_owner"]
        self.frames = normalize(data["frame_emb"].astype(np.float64))
        self.logit_scale = float(data["logit_scale"])
        self._text = dict(zip(data["texts"].tolist(), normalize(data["text_emb"].astype(np.float64)), strict=True))

    def _group(self, texts) -> np.ndarray:
        missing = [t for t in texts if t not in self._text]
        if missing:
            pytest.skip(f"向量檔裡沒有這些描述（預設分類改過了？請重跑 dump_embeddings.py）：{missing[:3]}")
        return normalize(np.mean([self._text[t] for t in texts], axis=0))

    def encode_categories(self, categories):
        return np.stack([self._group(c.texts()) for c in categories])

    def vocabulary_embeddings(self):
        return np.stack([self._group([zh, en, f"a photo of {en}"]) for zh, en in VOCABULARY])

    def other_embedding(self):
        return self._group(list(OTHER_TEXTS))

    def items(self, categories) -> list[Item]:
        names = tuple(c.name for c in categories)
        class_emb, vocab_emb, other = self.encode_categories(categories), self.vocabulary_embeddings(), self.other_embedding()
        result = []
        for position, name in enumerate(self.names):
            video = name.endswith(".mp4")
            item = Item(Path(name), "video" if video else "image", embedding=self.images[position],
                        frames=self.frames[self.frame_owner == position] if video else None)
            _apply_scores(item, class_emb, names, vocab_emb, self.logit_scale, other)
            result.append(item)
        return result


@pytest.fixture(scope="module", params=["standard", "accurate"])
def recorded(request):
    path = MANUAL / f"embeddings_{request.param}.npz"
    if not path.exists():
        pytest.skip(f"沒有 {path.name}")
    return Recorded(path)


@pytest.fixture(scope="module")
def scored(recorded):
    categories = [Category(c.name, list(c.prompts)) for c in DEFAULT_CATEGORIES]
    items = recorded.items(categories)
    answers = dict(zip(recorded.names, recorded.labels, strict=True))
    for name, group in zip(recorded.names, recorded.groups, strict=True):
        if group == "none_meme":  # 梗圖現在有自己的預設分類
            answers[name] = "迷因梗圖"
    return categories, items, answers


def _rates(categories, items, answers):
    correct = [it for it in items if answers[it.path.name] == it.best(categories)[0]]
    nothing = [it for it in items if answers[it.path.name] == NONE]
    has_answer = [it for it in items if answers[it.path.name] not in (NONE, "?")]
    low = lambda group: sum(it.is_low(categories, 0.5, SIMILARITY_FLOOR) for it in group) / len(group)  # noqa: E731
    return len(correct) / len(has_answer), low(correct), low(nothing)


def test_default_categories_classify_real_photos_well(scored):
    """每個預設分類至少 40 張真實圖片；每一類各自的正確率平均起來要夠高（不讓張數多的類別蓋過其他類）。"""
    categories, items, answers = scored
    per_category = {}
    for category in categories:
        mine = [it for it in items if answers[it.path.name] == category.name]
        if len(mine) < 40:
            pytest.skip(f"「{category.name}」只有 {len(mine)} 張（舊的向量檔？請重跑 dump_embeddings.py）")
        per_category[category.name] = sum(it.best(categories)[0] == category.name for it in mine) / len(mine)
    assert sum(per_category.values()) / len(per_category) >= 0.95, per_category
    assert min(per_category.values()) >= 0.80, per_category


def test_richer_descriptions_beat_the_old_short_ones(recorded, scored):
    """每類十幾句說法（現在的預設）比舊版每類兩三句準。"""
    categories, items, answers = scored
    old = [Category(c.name, list(LEGACY_DEFAULT_PROMPTS[c.name])) for c in categories if c.name in LEGACY_DEFAULT_PROMPTS]
    new = [c for c in categories if c.name in LEGACY_DEFAULT_PROMPTS]
    names = {c.name for c in new}

    def balanced(cats, scored_items):
        hits = {name: [] for name in names}
        for it in scored_items:
            if answers[it.path.name] in names:
                hits[answers[it.path.name]].append(it.best(cats)[0] == answers[it.path.name])
        return sum(sum(h) / len(h) for h in hits.values()) / len(hits)

    assert balanced(new, recorded.items(new)) >= balanced(old, recorded.items(old)) + 0.01


def test_needs_checking_rarely_flags_correct_files_and_catches_many_misfits(scored):
    """「需檢查」＝第一名分數低於門檻，或比任何分類更像「其他東西、雜物」。"""
    categories, items, answers = scored
    _, correct_flagged, misfits_flagged = _rates(categories, items, answers)
    assert correct_flagged <= 0.03  # 分對的幾乎不會被標
    assert misfits_flagged >= 0.38  # 不屬於任何分類的：大約一半會被標出來
    nothing = [it for it in items if answers[it.path.name] == NONE]
    only_threshold = sum(it.best(categories)[1] < 0.5 for it in nothing)
    assert sum(it.is_low(categories, 0.5, SIMILARITY_FLOOR) for it in nothing) >= only_threshold * 1.2
    correct = [it for it in items if answers[it.path.name] == it.best(categories)[0]]
    assert sum(it.fits_nothing for it in correct) / len(correct) <= 0.01  # 「更像其他東西」幾乎不會誤標分對的


def test_memes_get_their_own_category(scored):
    """聊天資料夾裡很多的梗圖：有了「迷因梗圖」分類後幾乎都分得進去，不再散落到人像、截圖、動漫裡。"""
    categories, items, answers = scored
    memes = [it for it in items if answers[it.path.name] == "迷因梗圖"]
    if not memes:
        pytest.skip("向量檔裡沒有梗圖")
    assert sum(it.best(categories)[0] == "迷因梗圖" for it in memes) / len(memes) >= 0.9
    others = [it for it in items if answers[it.path.name] not in (NONE, "?", "迷因梗圖")]
    assert sum(it.best(categories)[0] == "迷因梗圖" for it in others) / len(others) <= 0.02


def test_similarity_floor_would_have_flagged_correct_files(scored):
    """以前的 0.18 相似度門檻：連分對的檔案都有一大半低於它，所以關掉。"""
    categories, items, answers = scored
    correct = [it for it in items if answers[it.path.name] == it.best(categories)[0]]
    assert sum(it.top_similarity < 0.18 for it in correct) / len(correct) > 0.3
    assert SIMILARITY_FLOOR == 0


def test_missing_categories_are_suggested_but_covered_ones_are_not(recorded, scored):
    """建議新增的分類：魚、樂器、球類運動（真的沒有對應分類）；汽車、公車屬於「交通工具」，不該建議。"""
    categories, items, _ = scored
    covered = covered_tags(recorded.vocabulary_embeddings(), recorded.encode_categories(categories))
    suggested = {name for name, _ in suggest_categories(items, categories, covered, limit=10)}
    assert {"魚", "樂器", "球類運動"} <= suggested
    assert not {"汽車", "公車"} & suggested
    unfiltered = {name for name, _ in suggest_categories(items, categories, None, limit=10)}
    assert {"汽車", "公車"} & unfiltered  # 不過濾的話就會建議這些


def test_adding_the_suggested_categories_pulls_the_misfits_out(recorded, scored):
    """照建議加上「魚」「樂器」之後：原本被硬塞到別類的魚和法國號有九成移過去，分對的檔案幾乎不受影響。"""
    categories, items, answers = scored
    english = dict(VOCABULARY)
    more = categories + [Category(name, [f"a photo of {english[name]}"]) for name in ("魚", "樂器")]
    if any(t not in recorded._text for c in more[-2:] for t in c.texts()):
        pytest.skip("向量檔裡沒有新分類的描述")
    after = {it.path.name: it.best(more)[0] for it in recorded.items(more)}
    fish = [n for n in after if n.startswith("inet_tench")]
    horn = [n for n in after if n.startswith("inet_french_horn")]
    assert sum(after[n] == "魚" for n in fish) / len(fish) >= 0.85
    assert sum(after[n] == "樂器" for n in horn) / len(horn) >= 0.85
    were_right = [it.path.name for it in items if answers[it.path.name] == it.best(categories)[0]]
    assert sum(after[n] in ("魚", "樂器") for n in were_right) / len(were_right) <= 0.05  # 標準模型 1%、高精準模型 4%
