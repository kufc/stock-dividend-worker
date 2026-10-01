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

from .classifier import combine_frames, score
from .config import Category
from .media import load_media, media_kind
from .vocabulary import VOCABULARY

PENDING = "pending"      # AI 已判斷，等待使用者確認
CONFIRMED = "confirmed"  # 使用者已確認分類
SKIPPED = "skipped"      # 使用者選擇不處理
ERROR = "error"          # 檔案讀取失敗

TAG_COUNT = 5
LONG_ASPECT = 1.6  # 長寬比超過這個值（例如手機截圖）就切成多塊正方形分別判斷再平均
_next_uid = itertools.count(1)


@dataclass
class Item:
    path: Path
    kind: str
    embedding: np.ndarray | None = None
    date: datetime | None = None
    thumbnail: bytes | None = None
    probs: np.ndarray | None = None
    prob_names: tuple[str, ...] = ()  # probs 每個位置對應的分類名稱（分類清單改變時不會對錯位置）
    top_similarity: float = 1.0       # 與最像分類的原始相似度；太低代表「哪個分類都不太像」
    tags: list[tuple[str, float]] = field(default_factory=list)
    error: str | None = None
    status: str = PENDING
    chosen: str | None = None
    uid: int = field(default_factory=lambda: next(_next_uid), compare=False)  # 清單中穩定的識別碼
    _ranking: tuple = field(default=(None, ()), repr=False, compare=False)

    @property
    def analyzed(self) -> bool:
        return self.probs is not None

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
        for attr in ("embedding", "date", "thumbnail", "probs", "prob_names", "top_similarity", "tags", "error"):
            setattr(self, attr, getattr(other, attr))
        if other.status == ERROR and self.status == PENDING:
            self.status = ERROR

    def best(self, categories: list[Category]) -> tuple[str | None, float]:
        top = self.suggestions(categories, 1)
        return top[0] if top else (None, 0.0)

    def is_low(self, categories: list[Category], threshold: float, similarity_floor: float) -> bool:
        """AI 沒把握：跟所有分類都不太像；或第一名機率低於門檻、而且領先第二名不多（分類一多，機率本來就會分散，
        只看第一名的絕對值會把大半檔案都標成沒把握；第一名明顯領先時其實是清楚的判斷）。"""
        if self.status != PENDING or not self.analyzed:
            return False
        if self.top_similarity < similarity_floor:
            return True
        top = self.suggestions(categories, 2)
        if not top:
            return True
        best = top[0][1]
        second = top[1][1] if len(top) > 1 else 0.0
        return best < threshold and (best - second) < threshold / 2

    def final_category(self, categories: list[Category]) -> str | None:
        """確認過就用使用者的選擇，否則用 AI 的第一名。"""
        if self.status == CONFIRMED:
            return self.chosen
        if self.status == PENDING:
            return self.best(categories)[0]
        return None


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


def _apply_scores(item: Item, class_emb: np.ndarray, names: tuple[str, ...], vocab_emb: np.ndarray | None,
                  logit_scale: float) -> None:
    if item.embedding is None or not np.isfinite(item.embedding).all():
        item.status, item.error, item.probs = ERROR, "AI 計算結果異常（NaN），請在設定中改用其他模型或回報問題", None
        return
    item.probs = score(item.embedding[None, :], class_emb, logit_scale)[0]
    item.prob_names = names
    item.top_similarity = float((class_emb @ item.embedding).max())
    if vocab_emb is not None:
        vocab_probs = score(item.embedding[None, :], vocab_emb, logit_scale)[0]
        top = np.argsort(-vocab_probs)[:TAG_COUNT]
        item.tags = [(VOCABULARY[i][0], float(vocab_probs[i])) for i in top]


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
) -> list[Item]:
    # 保險：設定檔的值萬一是 0 或負數（例如壞掉的 batch_size=0），批次大小至少是 1，避免辨識迴圈空轉不前進
    batch_size = max(1, int(batch_size))
    video_frames = max(1, int(video_frames))
    items = [Item(Path(p), media_kind(Path(p)) or "image") for p in paths]
    names = tuple(c.name for c in categories)
    class_emb = classifier.encode_categories(categories)
    vocab_emb = classifier.vocabulary_embeddings()
    total = len(items)
    done = 0
    stop_event = stop_event or threading.Event()
    workers = workers or max(2, min(8, (os.cpu_count() or 4)))
    # 預先讀取的量以「畫面數」計算（一支影片 = 多張畫面），避免大量影片時吃光記憶體
    frame_budget = max(batch_size * 2, workers * 2)

    def load(item: Item):
        media = load_media(item.path, item.kind, video_frames)
        crops = media.frames if item.kind == "video" else square_crops(media.frames[0])
        tensors = [classifier.prepare(crop) for crop in crops]
        return media.date, media.thumbnail, tensors  # 不保留原始畫面，省下一半記憶體

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
                    item.date, item.thumbnail, tensors = future.result()
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
                start += len(tensors)
                _apply_scores(item, class_emb, names, vocab_emb, classifier.logit_scale)
                finish(index, item)
    finally:
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
        probs = score(emb, class_emb, classifier.logit_scale)
        top_sims = (emb @ class_emb.T).max(axis=1)
        vocab_probs = score(emb, vocab_emb, classifier.logit_scale)
        top_tags = np.argsort(-vocab_probs, axis=1)[:, :TAG_COUNT]
        for row, item in enumerate(scored):
            item.probs, item.prob_names, item.top_similarity = probs[row], names, float(top_sims[row])
            item.tags = [(VOCABULARY[i][0], float(vocab_probs[row, i])) for i in top_tags[row]]
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
