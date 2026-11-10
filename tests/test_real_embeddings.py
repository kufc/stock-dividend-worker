"""用真模型事先算好的向量（tests/manual/embeddings_*.npz，425 張真實照片）檢查判斷規則，不需要下載模型。

圖片：scikit-image／scikit-learn 內附照片、合成的截圖與收據，加上 ImageNette 驗證集每類 40 張。
其中 136 張屬於某個預設分類，289 張不屬於任何預設分類（魚、錄音機、電鋸、法國號、加油機、高爾夫球、降落傘⋯）。
向量由 tests/manual/dump_embeddings.py 產生；預設分類的描述改了之後要重新產生。
"""

from collections import Counter
from pathlib import Path

import numpy as np
import pytest

from media_sorter.analysis import Item, _apply_scores
from media_sorter.classifier import OTHER_TEXTS, SIMILARITY_FLOOR, TAG_COVERED_SIMILARITY, normalize
from media_sorter.config import DEFAULT_CATEGORIES, Category
from media_sorter.vocabulary import VOCABULARY

MANUAL = Path(__file__).resolve().parent / "manual"
NONE = "（都不像）"


class Recorded:
    """假裝是 Classifier：文字向量直接查事先存好的結果。"""

    def __init__(self, path: Path):
        data = np.load(path)
        self.names = data["image_names"].tolist()
        self.labels = data["image_labels"].tolist()
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
    return categories, items, answers


def _rates(categories, items, answers):
    correct = [it for it in items if answers[it.path.name] == it.best(categories)[0]]
    nothing = [it for it in items if answers[it.path.name] == NONE]
    has_answer = [it for it in items if answers[it.path.name] not in (NONE, "?")]
    low = lambda group: sum(it.is_low(categories, 0.5, SIMILARITY_FLOOR) for it in group) / len(group)  # noqa: E731
    return len(correct) / len(has_answer), low(correct), low(nothing)


def test_default_categories_classify_real_photos_well(scored):
    categories, items, answers = scored
    accuracy, _, _ = _rates(categories, items, answers)
    assert len(items) >= 400 and accuracy >= 0.90


def test_needs_checking_rarely_flags_correct_files_and_catches_about_half_of_the_misfits(scored):
    """「需檢查」＝第一名分數低於門檻，或比任何分類更像「其他東西、雜物」。"""
    categories, items, answers = scored
    _, correct_flagged, misfits_flagged = _rates(categories, items, answers)
    assert correct_flagged <= 0.03  # 分對的幾乎不會被標
    assert misfits_flagged >= 0.45  # 不屬於任何分類的，大約一半會被標出來
    only_threshold = sum(it.best(categories)[1] < 0.5 for it in items if answers[it.path.name] == NONE)
    with_other = sum(it.is_low(categories, 0.5, SIMILARITY_FLOOR) for it in items if answers[it.path.name] == NONE)
    assert with_other >= only_threshold * 1.3  # 「其他東西」這個比較確實多抓到不少


def test_similarity_floor_would_have_flagged_correct_files(scored):
    """以前的 0.18 相似度門檻：連分對的檔案都有一大半低於它，所以關掉。"""
    categories, items, answers = scored
    correct = [it for it in items if answers[it.path.name] == it.best(categories)[0]]
    assert sum(it.top_similarity < 0.18 for it in correct) / len(correct) > 0.3
    assert SIMILARITY_FLOOR == 0


def test_missing_categories_are_suggested_but_covered_ones_are_not(recorded, scored):
    """建議新增的分類：魚、樂器、球類運動（真的沒有對應分類）；汽車、公車屬於「交通工具」，不該建議。"""
    categories, items, _ = scored
    sims = recorded.vocabulary_embeddings() @ recorded.encode_categories(categories).T
    words = [zh for zh, _ in VOCABULARY]
    column = {c.name: j for j, c in enumerate(categories)}
    existing = set(column)
    counts: Counter = Counter()
    for item in items:
        name, prob = item.tags[0]
        if prob >= 0.5 and name not in existing and sims[words.index(name), column[item.best(categories)[0]]] < TAG_COVERED_SIMILARITY:
            counts[name] += 1
    suggested = {name for name, count in counts.items() if count >= max(3, len(items) // 50)}
    assert {"魚", "樂器", "球類運動"} <= suggested
    assert not {"汽車", "公車"} & suggested


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
    assert sum(after[n] in ("魚", "樂器") for n in were_right) / len(were_right) <= 0.03
