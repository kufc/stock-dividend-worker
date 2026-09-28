"""辨識流程：批次讀取檔案（多執行緒）→ 送進 AI 模型（顯示卡）→ 算出每個檔案最像哪些分類。

這個模組不含任何介面程式碼，方便測試；介面透過 callback 接收進度與結果。
"""

from __future__ import annotations

import os
import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

import numpy as np

from .classifier import combine_frames, score
from .config import Category
from .media import load_media, media_kind
from .vocabulary import VOCABULARY

PENDING = "pending"      # AI 已判斷，等待使用者確認
CONFIRMED = "confirmed"  # 使用者已確認分類
SKIPPED = "skipped"      # 使用者選擇不處理
ERROR = "error"          # 檔案讀取失敗

TAG_COUNT = 5


@dataclass
class Item:
    path: Path
    kind: str
    embedding: np.ndarray | None = None
    date: datetime | None = None
    thumbnail: bytes | None = None
    probs: np.ndarray | None = None
    tags: list[tuple[str, float]] = field(default_factory=list)
    error: str | None = None
    status: str = PENDING
    chosen: str | None = None

    @property
    def analyzed(self) -> bool:
        return self.probs is not None

    def suggestions(self, categories: list[Category], k: int = 3) -> list[tuple[str, float]]:
        if self.probs is None:
            return []
        order = np.argsort(-self.probs)[:k]
        return [(categories[i].name, float(self.probs[i])) for i in order]

    def best(self, categories: list[Category]) -> tuple[str | None, float]:
        top = self.suggestions(categories, 1)
        return top[0] if top else (None, 0.0)

    def final_category(self, categories: list[Category]) -> str | None:
        """確認過就用使用者的選擇，否則用 AI 的第一名。"""
        if self.status == CONFIRMED:
            return self.chosen
        if self.status == PENDING:
            return self.best(categories)[0]
        return None


def _apply_scores(item: Item, class_emb: np.ndarray, vocab_emb: np.ndarray | None, logit_scale: float) -> None:
    item.probs = score(item.embedding[None, :], class_emb, logit_scale)[0]
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
    items = [Item(Path(p), media_kind(Path(p)) or "image") for p in paths]
    class_emb = classifier.encode_categories(categories)
    vocab_emb = classifier.vocabulary_embeddings()
    total = len(items)
    done = 0
    workers = workers or max(2, min(8, (os.cpu_count() or 4)))
    window = max(batch_size * 2, workers * 2)

    def load(item: Item):
        media = load_media(item.path, item.kind, video_frames)
        return media, [classifier.prepare(frame) for frame in media.frames]

    def finish(index: int, item: Item) -> None:
        nonlocal done
        done += 1
        if on_item:
            on_item(index, item)
        if on_progress:
            on_progress(done, total)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        queue: deque = deque()
        source = iter(enumerate(items))

        def refill() -> None:
            while len(queue) < window:
                nxt = next(source, None)
                if nxt is None:
                    return
                queue.append((nxt[0], nxt[1], pool.submit(load, nxt[1])))

        refill()
        while queue:
            if stop_event is not None and stop_event.is_set():
                for _, _, future in queue:
                    future.cancel()
                break
            # 收集一批畫面（影片會有多張），一次送進顯示卡
            batch: list[tuple[int, Item, list]] = []
            frame_count = 0
            while queue and frame_count < batch_size:
                index, item, future = queue.popleft()
                try:
                    media, tensors = future.result()
                except Exception as exc:  # noqa: BLE001 - 任何讀檔錯誤都只影響該檔案
                    item.status, item.error = ERROR, f"{type(exc).__name__}: {exc}"
                    finish(index, item)
                    continue
                item.date, item.thumbnail = media.date, media.thumbnail
                batch.append((index, item, tensors))
                frame_count += len(tensors)
            refill()
            if not batch:
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
                _apply_scores(item, class_emb, vocab_emb, classifier.logit_scale)
                finish(index, item)
    return items


def rescore(items: list[Item], classifier, categories: list[Category]) -> None:
    """分類清單改變後重新計算（不需要重新讀檔，只重算文字向量，很快）。"""
    class_emb = classifier.encode_categories(categories)
    vocab_emb = classifier.vocabulary_embeddings()
    names = {c.name for c in categories}
    for item in items:
        if item.embedding is not None:
            _apply_scores(item, class_emb, vocab_emb, classifier.logit_scale)
        if item.status == CONFIRMED and item.chosen not in names:
            item.status, item.chosen = PENDING, None


def accept_confident(items: list[Item], categories: list[Category], threshold: float) -> int:
    """把 AI 信心度達門檻、尚未確認的項目，全部直接採用 AI 的第一名。回傳處理數量。"""
    count = 0
    for item in items:
        if item.status == PENDING and item.analyzed:
            name, confidence = item.best(categories)
            if name and confidence >= threshold:
                item.status, item.chosen = CONFIRMED, name
                count += 1
    return count
