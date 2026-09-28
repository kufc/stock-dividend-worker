"""讀取圖片與影片：掃描資料夾、載入圖片、從影片平均擷取畫面、取得拍攝日期、產生縮圖。"""

from __future__ import annotations

import io
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, ImageOps

from .config import IMAGE_EXTS, VIDEO_EXTS

try:  # iPhone 的 HEIC 照片
    from pillow_heif import register_heif_opener

    register_heif_opener()
except ImportError:  # pragma: no cover - 選用功能
    pass

THUMB_SIZE = 640
EXIF_DATETIME_ORIGINAL = 36867
EXIF_DATETIME = 306
EXIF_IFD = 0x8769


def media_kind(path: Path) -> str | None:
    ext = path.suffix.lower()
    if ext in IMAGE_EXTS:
        return "image"
    if ext in VIDEO_EXTS:
        return "video"
    return None


def scan_folder(folder: Path, recursive: bool = True, exclude: Path | None = None) -> list[Path]:
    """找出資料夾內所有支援的圖片與影片（依路徑排序）。exclude 資料夾（例如輸出資料夾）會被跳過。"""
    folder = Path(folder)
    exclude_resolved = Path(exclude).resolve() if exclude else None
    found: list[Path] = []
    for root, dirs, files in os.walk(folder):
        root_path = Path(root)
        if exclude_resolved is not None:
            dirs[:] = [d for d in dirs if (root_path / d).resolve() != exclude_resolved]
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for name in files:
            path = root_path / name
            if not name.startswith(".") and media_kind(path):
                found.append(path)
        if not recursive:
            break
    return sorted(found, key=lambda p: str(p).lower())


def _exif_date(img: Image.Image) -> datetime | None:
    try:
        exif = img.getexif()
        raw = exif.get_ifd(EXIF_IFD).get(EXIF_DATETIME_ORIGINAL) or exif.get(EXIF_DATETIME)
        if raw:
            return datetime.strptime(str(raw).strip("\x00 ")[:19], "%Y:%m:%d %H:%M:%S")
    except (ValueError, TypeError, KeyError, AttributeError):
        pass
    return None


def file_date(path: Path) -> datetime:
    return datetime.fromtimestamp(os.path.getmtime(path))


def load_image(path: Path, max_side: int | None = None) -> tuple[Image.Image, datetime | None]:
    """載入圖片並依 EXIF 轉正，回傳 (RGB 圖片, EXIF 拍攝日期)。"""
    with Image.open(path) as img:
        date = _exif_date(img)
        if max_side and img.format == "JPEG":
            img.draft("RGB", (max_side, max_side))  # 大幅加速大張 JPEG 的縮小載入
        img = ImageOps.exif_transpose(img)
        img = img.convert("RGB")
    if max_side:
        img.thumbnail((max_side, max_side))
    return img, date


def _video_date(container) -> datetime | None:
    raw = (container.metadata or {}).get("creation_time")
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).astimezone().replace(tzinfo=None)
    return parsed


def sample_video_frames(path: Path, count: int = 8, max_side: int = 640) -> tuple[list[Image.Image], datetime | None]:
    """從影片中平均取 count 張畫面（避開頭尾），回傳 (畫面清單, 影片建立日期)。"""
    import av

    frames: list[Image.Image] = []
    with av.open(str(path)) as container:
        date = _video_date(container)
        stream = container.streams.video[0]
        stream.thread_type = "AUTO"
        duration = None
        if stream.duration and stream.time_base:
            duration = float(stream.duration * stream.time_base)
        elif container.duration:
            duration = container.duration / av.time_base

        def grab(frame) -> None:
            img = frame.to_image()
            img.thumbnail((max_side, max_side))
            frames.append(img)

        if duration and duration > 0 and stream.time_base:
            start = float(stream.start_time * stream.time_base) if stream.start_time else 0.0
            for i in range(count):
                t = start + duration * (0.05 + 0.9 * (i + 0.5) / count)
                try:
                    container.seek(int(t / stream.time_base), stream=stream, backward=True, any_frame=False)
                    frame = next(container.decode(stream), None)
                except (av.error.FFmpegError, StopIteration):
                    frame = None
                if frame is not None:
                    grab(frame)

        if not frames:  # 無法跳轉（例如沒有時間資訊）：從頭依序解碼，取前面平均分布的畫面
            container.seek(0)
            for i, frame in enumerate(container.decode(stream)):
                if i % 15 == 0:
                    grab(frame)
                if len(frames) >= count:
                    break
    if not frames:
        raise ValueError("影片中讀不到任何畫面")
    return frames, date


def thumbnail_bytes(img: Image.Image, size: int = THUMB_SIZE) -> bytes:
    thumb = img.copy()
    thumb.thumbnail((size, size))
    buf = io.BytesIO()
    thumb.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


@dataclass
class LoadedMedia:
    frames: list[Image.Image]
    date: datetime
    thumbnail: bytes | None  # 影片才會預先存縮圖（避免預覽時重新解碼）


def load_media(path: Path, kind: str, video_frames: int = 8) -> LoadedMedia:
    if kind == "video":
        frames, date = sample_video_frames(path, video_frames)
        thumb = thumbnail_bytes(frames[len(frames) // 2])
        return LoadedMedia(frames, date or file_date(path), thumb)
    img, date = load_image(path, max_side=640)
    return LoadedMedia([img], date or file_date(path), None)


def load_preview(path: Path, kind: str, cached: bytes | None, size: int = THUMB_SIZE) -> Image.Image:
    if cached:
        return Image.open(io.BytesIO(cached)).convert("RGB")
    if kind == "video":
        frames, _ = sample_video_frames(path, 1, max_side=size)
        return frames[0]
    img, _ = load_image(path, max_side=size)
    return img
