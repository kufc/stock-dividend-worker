"""辨識流程：批次讀取檔案（多執行緒）→ 送進 AI 模型（顯示卡）→ 算出每個檔案最像哪些分類。

這個模組不含任何介面程式碼，方便測試；介面透過 callback 接收進度與結果。
"""

from __future__ import annotations

import itertools
import os
import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import wait as wait_futures
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

import numpy as np
from PIL import Image

from .classifier import combine_frames, score, score_frames
from .config import Category
from .media import ANALYSIS_SIZE, THUMB_SIZE, load_media, media_kind
from .vocabulary import VOCABULARY

PENDING = "pending"      # AI 已判斷，等待使用者確認
CONFIRMED = "confirmed"  # 使用者已確認分類
SKIPPED = "skipped"      # 使用者選擇不處理
ERROR = "error"          # 檔案讀取失敗

TAG_COUNT = 5
LONG_ASPECT = 1.6  # 長寬比超過這個值（例如手機截圖）就切成多塊正方形分別判斷再平均
# 前處理的版本：讀圖尺寸、切塊方式、影片取樣方式、向量怎麼合併⋯⋯只要改了會讓「同一個檔案算出不同向量」的東西，
# 就把這個數字加一，保存的辨識結果（workdb）就會全部重算，不會新舊混用。
PREPROCESS_VERSION = 2  # 2：影片取樣拿掉黑畫面


def model_key(classifier) -> str:
    """辨識結果是用哪個模型、哪個版本、哪一版前處理算出來的。保存的結果只有這個字串相同時才沿用。"""
    name = getattr(classifier, "model_name", None) or type(classifier).__name__
    revision = getattr(classifier, "model_revision", "") or ""
    return f"{name}@{revision}|prep{PREPROCESS_VERSION}|{ANALYSIS_SIZE}px|thumb{THUMB_SIZE}|crop{LONG_ASPECT}"
_next_uid = itertools.count(1)


@dataclass
class Item:
    path: Path
    kind: str
    embedding: np.ndarray | None = None
    frames: np.ndarray | None = None  # 影片：每張取樣畫面各自的向量（逐張比對後平均機率，見 score_frames）
    date: datetime | None = None
    thumbnail: bytes | None = None    # 縮圖的 JPEG 位元組：只有「沒有工作資料庫」時才留在記憶體（例如測試、指令列工具）
    thumb_file: Path | None = None    # 有工作資料庫時縮圖放在磁碟上，這裡只記檔名（十萬張也不占記憶體）
    fingerprint: tuple[int, int] | None = None  # 辨識當時檔案的（大小, 修改時間）
    cached: bool = False              # 這次是沿用保存的結果，沒有重新讀檔與計算
    incomplete: bool = False          # 檔案看起來沒傳完（見 media.looks_incomplete）：一律進「需檢查」
    # 從你的修正學到的：這個檔案跟你處理過的某個檔案很像，但 AI 給它的分類跟你當時的決定不一樣 → (你的決定, 相似度)。
    # 你的決定是分類名稱，或 None（你略過了那個檔案）。有這個提示的檔案會進「需檢查」。見 neighbor_hints。
    neighbor_hint: tuple | None = None
    probs: np.ndarray | None = None
    prob_names: tuple[str, ...] = ()  # probs 每個位置對應的分類名稱（分類清單改變時不會對錯位置）
    top_similarity: float = 1.0       # 與最像分類的原始相似度
    other_similarity: float = -1.0    # 與「其他東西、雜物」的相似度；比 top_similarity 高代表可能不屬於任何分類
    tags: list[tuple[str, float]] = field(default_factory=list)
    error: str | None = None
    status: str = PENDING
    chosen: str | None = None
    uid: int = field(default_factory=lambda: next(_next_uid), compare=False)  # 清單中穩定的識別碼
    _ranking: tuple = field(default=(None, ()), repr=False, compare=False)

    def __setattr__(self, name, value):
        """你的決定（status／chosen）一改變就通知 watcher（App 用它立刻寫進工作資料庫）。"""
        object.__setattr__(self, name, value)
        if name in ("status", "chosen"):
            watcher = self.__dict__.get("watcher")
            if watcher is not None:
                watcher(self)

    @property
    def analyzed(self) -> bool:
        return self.probs is not None

    def thumbnail_data(self) -> bytes | None:
        """縮圖的 JPEG 位元組：記憶體裡有就用，否則從磁碟快取讀（讀不到回傳 None，呼叫端會改讀原檔）。"""
        if self.thumbnail:
            return self.thumbnail
        if self.thumb_file is not None:
            try:
                return self.thumb_file.read_bytes()
            except OSError:
                return None
        return None

    @property
    def has_thumbnail(self) -> bool:
        return bool(self.thumbnail) or self.thumb_file is not None

    def suggestions(self, categories: list[Category], k: int = 3) -> list[tuple[str, float]]:
        """AI 認為最像的前 k 個分類（只列出目前還存在的分類）。"""
        if self.probs is None:
            return []
        if self._ranking[0] is not self.probs:  # 排序結果快取起來，大量檔案時介面才不會變慢
            order = np.argsort(-self.probs)
            self._ranking = (self.probs, tuple((self.prob_names[i], float(self.probs[i])) for i in order))
        current = {c.name for c in categories}
        result = []
        for name, prob in self._ranking[1]:
            if name in current:
                result.append((name, prob))
                if len(result) >= k:
                    break
        return result

    def merge_analysis(self, other: "Item") -> None:
        """把背景辨識的結果併入這個項目，保留使用者已經做的確認／略過。"""
        for attr in ("embedding", "frames", "date", "thumbnail", "thumb_file", "fingerprint", "cached", "incomplete", "probs",
                     "prob_names", "top_similarity", "other_similarity", "tags", "error"):
            setattr(self, attr, getattr(other, attr))
        if self.status == PENDING:
            if other.status == ERROR:
                self.status = ERROR
            elif other.status in (CONFIRMED, SKIPPED):  # 上次保存的決定；這一次已經動過的以這一次為準
                self.chosen = other.chosen
                self.status = other.status

    def best(self, categories: list[Category]) -> tuple[str | None, float]:
        top = self.suggestions(categories, 1)
        return top[0] if top else (None, 0.0)

    def is_low(self, categories: list[Category], threshold: float, similarity_floor: float) -> bool:
        """AI 沒把握：第一名的模型分數低於門檻，或比起任何分類更像「其他東西」（fits_nothing）。
        （相似度低於 similarity_floor 也算，但預設不使用。）

        用真模型實測過：分對的照片第一名幾乎都在門檻以上；不屬於任何分類的照片有一半左右會落在門檻以下。
        「第一名要明顯領先第二名」這類額外條件沒有讓分對的照片少被標，反而漏掉該檢查的，所以只看門檻。
        """
        if self.status != PENDING or not self.analyzed:
            return False
        return (self.best(categories)[1] < threshold or self.top_similarity < similarity_floor
                or self.fits_nothing or self.incomplete or self.neighbor_hint is not None)

    @property
    def fits_nothing(self) -> bool:
        """比起任何一個分類，更像「其他東西、雜物」→ 可能不屬於任何分類。"""
        return self.other_similarity > self.top_similarity

    def final_category(self, categories: list[Category]) -> str | None:
        """確認過就用使用者的選擇，否則用 AI 的第一名。"""
        if self.status == CONFIRMED:
            return self.chosen
        if self.status == PENDING:
            return self.best(categories)[0]
        return None


def planned_category(item: Item, categories: list[Category], threshold: float, similarity_floor: float,
                     low_mode: str, unsorted_name: str) -> tuple[str | None, bool]:
    """整理時這個檔案要進哪個分類資料夾：回傳 (分類名稱或 None＝不整理, 是不是「AI 沒把握、使用者也沒改」)。

    - 略過、讀取失敗、還沒辨識的：不整理
    - 使用者指定過的：照使用者的
    - AI 有把握的：AI 的第一名
    - AI 沒把握、使用者也沒改的，依 low_mode：「leave」不整理、「unsorted」放 unsorted_name、其他值照 AI 的第一名
    （這條規則以前寫在視窗類別裡，要開視窗才測得到。）
    """
    if item.status not in (PENDING, CONFIRMED) or not item.analyzed:
        return None, False
    category = item.final_category(categories)
    if not category:
        return None, False
    if item.status == PENDING and item.is_low(categories, threshold, similarity_floor):
        if low_mode == "leave":
            return None, True
        if low_mode == "unsorted":
            return unsorted_name, True
        return category, True
    return category, False


# 「從你的修正學習」的相似度下限（圖片向量之間的 cosine）。0.8：開發集上模擬「使用者看完需檢查的檔案」之後，
# 不屬於任何分類卻被接受的比例從 34.8% 降到 27.1%（高精準模型 40.6% → 31.8%），分對的檔案沒有任何一個被多標；
# 0.85 以上幾乎沒有效果，更低沒有試（會開始把只是有點像的檔案也拉進來）。見 docs/evaluation.md。
NEIGHBOR_MIN_SIMILARITY = 0.8
_HINT_CHUNK = 2048


def neighbor_hints(embeddings: np.ndarray, predictions: list, example_embeddings: np.ndarray, example_labels: list,
                   min_similarity: float = NEIGHBOR_MIN_SIMILARITY) -> list:
    """從使用者的修正學習：每個還沒處理的檔案，找「使用者處理過的檔案」裡最像的那一個。
    夠像（相似度 ≥ min_similarity）、而使用者當時的決定跟 AI 給這個檔案的分類不一樣 → 回傳 (使用者的決定, 相似度)，
    否則 None。使用者的決定是分類名稱，或 None（略過＝不屬於任何分類）。

    只用來「多提醒」，不會自動改分類、也不會把原本要檢查的檔案變成不用檢查：使用者改錯一張，最壞只是多看幾張。
    只看同一批檔案裡的修正（不跨資料夾、不保存）。
    """
    if len(embeddings) == 0 or len(example_embeddings) == 0:
        return [None] * len(embeddings)
    examples = np.asarray(example_embeddings, dtype=np.float32)
    result: list = []
    for start in range(0, len(embeddings), _HINT_CHUNK):
        sims = np.asarray(embeddings[start:start + _HINT_CHUNK], dtype=np.float32) @ examples.T
        nearest = sims.argmax(axis=1)
        best = sims[np.arange(len(nearest)), nearest]
        for row, (index, similarity) in enumerate(zip(nearest, best)):
            label = example_labels[index]
            if similarity >= min_similarity and label != predictions[start + row]:
                result.append((label, float(similarity)))
            else:
                result.append(None)
    return result


def learn_from_decisions(items: list[Item], categories: list[Category]) -> int:
    """依目前清單裡使用者的決定（指定的分類、略過）重新計算每個待處理檔案的 neighbor_hint。回傳有提示的檔案數。"""
    usable = lambda it: it.embedding is not None and it.status != ERROR  # noqa: E731
    examples = [it for it in items if usable(it) and it.status in (CONFIRMED, SKIPPED)]
    pending = [it for it in items if usable(it) and it.status == PENDING and it.analyzed]
    for it in items:
        if it.neighbor_hint is not None and it.status != PENDING:
            it.neighbor_hint = None
    if not pending:
        return 0
    if not examples:
        for it in pending:
            it.neighbor_hint = None
        return 0
    hints = neighbor_hints(np.stack([it.embedding for it in pending]), [it.best(categories)[0] for it in pending],
                           np.stack([it.embedding for it in examples]),
                           [it.chosen if it.status == CONFIRMED else None for it in examples])
    for it, hint in zip(pending, hints, strict=True):
        it.neighbor_hint = hint
    return sum(hint is not None for hint in hints)


def square_crops(img: Image.Image) -> list[Image.Image]:
    """細長的圖片（手機截圖、直式照片）切成 2~3 塊重疊的正方形，避免 AI 只看到中間一段。"""
    w, h = img.size
    long_side, short_side = max(w, h), min(w, h)
    if short_side == 0 or long_side / short_side < LONG_ASPECT:
        return [img]
    n = 3 if long_side / short_side >= 2.2 else 2
    step = (long_side - short_side) / (n - 1)
    boxes = []
    for i in range(n):
        offset = round(i * step)
        boxes.append((offset, 0, offset + short_side, h) if w > h else (0, offset, w, offset + short_side))
    return [img.crop(box) for box in boxes]


def _other_embedding(classifier) -> np.ndarray | None:
    getter = getattr(classifier, "other_embedding", None)
    return getter() if getter is not None else None


def _apply_scores(item: Item, class_emb: np.ndarray, names: tuple[str, ...], vocab_emb: np.ndarray | None,
                  logit_scale: float, other_emb: np.ndarray | None = None) -> None:
    if item.embedding is None or not np.isfinite(item.embedding).all():
        item.status, item.error, item.probs = ERROR, "AI 計算結果異常（NaN），請在設定中改用其他模型或回報問題", None
        return
    item.probs = _probs(item, class_emb, logit_scale, other_emb)
    item.prob_names = names
    item.top_similarity = float((class_emb @ item.embedding).max())
    item.other_similarity = float(other_emb @ item.embedding) if other_emb is not None else -1.0
    if vocab_emb is not None:
        vocab_probs = _probs(item, vocab_emb, logit_scale)
        top = np.argsort(-vocab_probs)[:TAG_COUNT]
        item.tags = [(VOCABULARY[i][0], float(vocab_probs[i])) for i in top]


def _probs(item: Item, text_emb: np.ndarray, logit_scale: float, other_emb: np.ndarray | None = None) -> np.ndarray:
    """這個檔案對各個文字描述的相對分數：圖片用它的向量；影片逐張畫面比對後平均。
    other_emb：分類很少時一起比的「其他東西」（見 classifier.score）；內建詞彙不需要。"""
    if item.frames is not None and len(item.frames) > 1 and np.isfinite(item.frames).all():
        return score_frames(item.frames, text_emb, logit_scale, other_emb)
    return score(item.embedding[None, :], text_emb, logit_scale, other_emb)[0]


def analyze(
    paths: list[Path],
    classifier,
    categories: list[Category],
    *,
    video_frames: int = 8,
    batch_size: int = 16,
    on_item: Callable[[int, Item], None] | None = None,
    on_progress: Callable[[int, int], None] | None = None,
    stop_event: threading.Event | None = None,
    workers: int | None = None,
    store=None,
) -> list[Item]:
    """store：工作資料庫（workdb.WorkStore）。有給的話：
    - 沒變過的檔案（路徑、大小、修改時間、模型與前處理版本都相同）直接沿用保存的向量，不讀檔也不進模型
    - 每一批算完就寫入並 commit；縮圖存到磁碟，不留在記憶體
    - 上次保存的「你指定的分類／略過」會套回來（分類已經不存在的除外）
    """
    # 保險：設定檔的值萬一是 0 或負數（例如壞掉的 batch_size=0），批次大小至少是 1，避免辨識迴圈空轉不前進
    batch_size = max(1, int(batch_size))
    video_frames = max(1, int(video_frames))
    items = [Item(Path(p), media_kind(Path(p)) or "image") for p in paths]
    names = tuple(c.name for c in categories)
    class_emb = classifier.encode_categories(categories)
    vocab_emb = classifier.vocabulary_embeddings()
    other_emb = _other_embedding(classifier)
    total = len(items)
    done = 0
    stop_event = stop_event or threading.Event()
    key = model_key(classifier) if store is not None else ""

    def frames_of(item: Item) -> int:
        return video_frames if item.kind == "video" else 0

    def restore(item: Item) -> bool:
        """沿用保存的辨識結果。回傳 True 表示這個檔案不用重新辨識。"""
        if store is None:
            return False
        from .workdb import file_fingerprint

        item.fingerprint = file_fingerprint(item.path)
        try:
            cached = store.lookup(item.path, key, frames_of(item), item.fingerprint) if item.fingerprint else None
            decision = store.load_decision(item.path, item.fingerprint) if cached is not None else None
        except Exception:  # noqa: BLE001 - 資料庫讀不出來就當作沒有保存過，照常辨識
            return False
        if cached is None:
            return False
        item.embedding, item.frames, item.date, item.thumb_file = cached.embedding, cached.frames, cached.date, cached.thumb_file
        item.incomplete = cached.incomplete
        item.cached = True
        _apply_scores(item, class_emb, names, vocab_emb, classifier.logit_scale, other_emb)
        if decision and item.status == PENDING and (decision[0] == SKIPPED or decision[1] in names):
            item.chosen = decision[1] if decision[0] == CONFIRMED else None
            item.status = decision[0]
        return True

    def checkpoint(item: Item) -> None:
        """剛算完的檔案寫進工作資料庫；縮圖改放磁碟。"""
        if store is None or item.embedding is None or item.status == ERROR or item.fingerprint is None:
            return
        from .workdb import file_fingerprint

        if file_fingerprint(item.path) != item.fingerprint:
            return  # 辨識途中檔案被改過：這次的結果照用，但不保存（下次會重新辨識）
        try:
            thumb_file = store.save_analysis(item.path, item.kind, key, frames_of(item), item.fingerprint,
                                             item.embedding, item.frames, item.date, item.thumbnail,
                                             incomplete=item.incomplete)
        except Exception:  # noqa: BLE001 - 存不進去（磁碟滿了⋯）不影響這次辨識，只是下次要重算
            return
        if thumb_file is not None:
            item.thumb_file, item.thumbnail = thumb_file, None
    workers = workers or max(2, min(8, (os.cpu_count() or 4)))
    # 預先讀取的量以「畫面數」計算（一支影片 = 多張畫面），避免大量影片時吃光記憶體
    frame_budget = max(batch_size * 2, workers * 2)

    def load(item: Item):
        media = load_media(item.path, item.kind, video_frames)
        crops = media.frames if item.kind == "video" else square_crops(media.frames[0])
        tensors = [classifier.prepare(crop) for crop in crops]
        return media.date, media.thumbnail, tensors, media.incomplete  # 不保留原始畫面，省下一半記憶體

    def finish(index: int, item: Item) -> None:
        nonlocal done
        done += 1
        if on_item:
            on_item(index, item)
        if on_progress:
            on_progress(done, total)

    def cost(item: Item) -> int:
        return video_frames if item.kind == "video" else 1

    pool = ThreadPoolExecutor(max_workers=workers)
    queue: deque = deque()
    in_flight = 0
    source = iter(enumerate(items))

    def refill() -> None:
        nonlocal in_flight
        while in_flight < frame_budget or len(queue) < workers:
            nxt = next(source, None)
            if nxt is None:
                return
            if stop_event.is_set():
                return
            if restore(nxt[1]):  # 沿用保存的結果：不讀檔、不進模型
                finish(nxt[0], nxt[1])
                continue
            queue.append((nxt[0], nxt[1], pool.submit(load, nxt[1])))
            in_flight += cost(nxt[1])

    try:
        refill()
        while queue and not stop_event.is_set():
            # 收集一批畫面（影片會有多張），一次送進顯示卡
            batch: list[tuple[int, Item, list]] = []
            frame_count = 0
            while queue and frame_count < batch_size and not stop_event.is_set():
                index, item, future = queue[0]
                while not future.done() and not stop_event.is_set():
                    wait_futures([future], timeout=0.2)
                if stop_event.is_set():
                    break
                queue.popleft()
                in_flight -= cost(item)
                try:
                    item.date, item.thumbnail, tensors, item.incomplete = future.result()
                except Exception as exc:  # noqa: BLE001 - 任何讀檔錯誤都只影響該檔案
                    item.status, item.error = ERROR, f"{type(exc).__name__}: {exc}"
                    finish(index, item)
                    continue
                batch.append((index, item, tensors))
                frame_count += len(tensors)
            refill()
            if not batch or stop_event.is_set():
                continue

            all_tensors = [t for _, _, tensors in batch for t in tensors]
            embeddings = np.concatenate([
                classifier.encode_prepared(all_tensors[i:i + batch_size])
                for i in range(0, len(all_tensors), batch_size)
            ])
            start = 0
            for index, item, tensors in batch:
                item.embedding = combine_frames(embeddings[start:start + len(tensors)])
                if item.kind == "video":
                    item.frames = embeddings[start:start + len(tensors)].copy()
                start += len(tensors)
                _apply_scores(item, class_emb, names, vocab_emb, classifier.logit_scale, other_emb)
                checkpoint(item)
            if store is not None:
                store.commit()  # 這一批的每個檔案都已經落地；之後程式被強制結束也不用重算
            for index, item, _tensors in batch:
                finish(index, item)
    finally:
        if store is not None:
            try:
                store.commit()
            except Exception:  # noqa: BLE001
                pass
        pool.shutdown(wait=False, cancel_futures=True)
    return items


def rescore(items: list[Item], classifier, categories: list[Category]) -> int:
    """分類清單改變後重新計算（不需要重新讀檔，只重算文字向量；上萬個檔案也只要一下子）。

    回傳因為分類被刪除而改回「待確認」的項目數。
    """
    names = tuple(c.name for c in categories)
    scored = [it for it in items if it.embedding is not None and it.status != ERROR]
    if scored:
        class_emb = classifier.encode_categories(categories)
        vocab_emb = classifier.vocabulary_embeddings()
        emb = np.stack([it.embedding for it in scored])
        other_emb = _other_embedding(classifier)
        probs = score(emb, class_emb, classifier.logit_scale, other_emb)
        top_sims = (emb @ class_emb.T).max(axis=1)
        vocab_probs = score(emb, vocab_emb, classifier.logit_scale)
        top_tags = np.argsort(-vocab_probs, axis=1)[:, :TAG_COUNT]
        for row, item in enumerate(scored):
            item.probs, item.prob_names, item.top_similarity = probs[row], names, float(top_sims[row])
            if other_emb is not None:
                item.other_similarity = float(other_emb @ item.embedding)
            item.tags = [(VOCABULARY[i][0], float(vocab_probs[row, i])) for i in top_tags[row]]
            if item.frames is not None:  # 影片：逐張畫面重算（數量少，逐一處理即可）
                _apply_scores(item, class_emb, names, vocab_emb, classifier.logit_scale, other_emb)
    reverted = 0
    for item in items:
        if item.status == CONFIRMED and item.chosen not in names:
            item.status, item.chosen = PENDING, None
            reverted += 1
    return reverted


def accept_confident(items: list[Item], categories: list[Category], threshold: float,
                     similarity_floor: float = 0.0) -> int:
    """把 AI 有把握、尚未確認的項目，全部直接採用 AI 的第一名。回傳處理數量。"""
    count = 0
    for item in items:
        if item.status == PENDING and item.analyzed and not item.is_low(categories, threshold, similarity_floor):
            name, _ = item.best(categories)
            if name:
                item.status, item.chosen = CONFIRMED, name
                count += 1
    return count


# 「AI 還看到」裡描述拍法、不適合當分類的詞
NOT_A_CATEGORY = {"黑白照片", "模糊照片", "夜間照片", "特寫", "空拍", "全景", "室內", "戶外"}
SUGGEST_MIN_SCORE = 0.5  # 內建詞彙第一名的分數要夠高，才算「這個檔案就是這個東西」
_VOCAB_INDEX = {zh: i for i, (zh, _) in enumerate(VOCABULARY)}


def suggest_categories(items: list[Item], categories: list[Category], covered: np.ndarray | None = None,
                       limit: int = 6) -> list[tuple[str, int]]:
    """很多檔案明顯是某種東西（內建詞彙的第一名分數很高），但分類清單裡沒有 → 建議新增。回傳 [(名稱, 檔案數)]。

    AI 一定會把每個檔案塞進現有的某一類；分類不合內容時結果就會很怪，加上合適的分類才分得準。
    covered：classifier.covered_tags() 的結果；檔案所在的分類已經涵蓋那個詞時不算（「汽車」在「交通工具」裡）。
    """
    existing = {c.name: j for j, c in enumerate(categories)}
    counts: dict[str, int] = {}
    for item in items:
        if item.status == ERROR or not item.tags:
            continue
        name, prob = item.tags[0]
        if prob < SUGGEST_MIN_SCORE or name in existing or name in NOT_A_CATEGORY:
            continue
        column = existing.get(item.best(categories)[0])
        if covered is not None and column is not None and name in _VOCAB_INDEX and covered[_VOCAB_INDEX[name], column]:
            continue
        counts[name] = counts.get(name, 0) + 1
    minimum = max(3, len(items) // 50)
    ranked = sorted(counts.items(), key=lambda kv: -kv[1])
    return [(name, count) for name, count in ranked[:limit] if count >= minimum]
