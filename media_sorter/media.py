"""讀取圖片與影片：掃描資料夾、載入圖片、從影片平均擷取畫面、取得拍攝日期、產生縮圖。"""

from __future__ import annotations

import io
import os
import re
import stat
import sys
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
from PIL import Image, ImageFile, ImageOps

from .config import IMAGE_EXTS, VIDEO_EXTS

try:  # iPhone 的 HEIC 照片
    from pillow_heif import register_heif_opener

    register_heif_opener()
except ImportError:  # pragma: no cover - 選用功能
    pass

# 都是使用者自己的照片：放寬 Pillow 的「解壓縮炸彈」限制到 3 億像素（仍涵蓋 200MP 手機照片、全景照），
# 但不是無限制：超過的話 Pillow 會丟 DecompressionBombError，避免單一張圖就把記憶體吃光
Image.MAX_IMAGE_PIXELS = 300_000_000
# 傳輸中斷而不完整的 JPEG 仍讀出可用的部分（缺的部分補灰色），而不是整張當成讀取失敗
ImageFile.LOAD_TRUNCATED_IMAGES = True

THUMB_SIZE = 480
ANALYSIS_SIZE = 640
# 超過這麼多像素、又不是 JPEG／MPO（無法用 draft 縮小解碼）的圖片，同一時間只解碼一張，
# 避免好幾個執行緒同時把好幾張巨圖整張攤開在記憶體裡
LARGE_DECODE_PIXELS = 50_000_000
_decode_semaphore = threading.Semaphore(1)
EXIF_IFD = 0x8769
EXIF_DATE_TAGS = [(EXIF_IFD, 36867), (EXIF_IFD, 36868), (None, 306)]  # 拍攝時間、數位化時間、修改時間
DATE_RE = re.compile(r"(\d{4})[:\-/](\d{2})[:\-/](\d{2})[ T](\d{2}):(\d{2})(?::(\d{2}))?")
MIN_YEAR = 1990

# 掃描時跳過的系統／NAS 資料夾（不分大小寫）
SKIP_DIRS = {"$recycle.bin", "recycler", "system volume information", "@eadir", "#recycle", "#snapshot",
             ".@__thumb", "found.000"}
FILE_ATTRIBUTE_SYSTEM = getattr(stat, "FILE_ATTRIBUTE_SYSTEM", 0x4)
FILE_ATTRIBUTE_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


def media_kind(path: Path) -> str | None:
    ext = path.suffix.lower()
    if ext in IMAGE_EXTS:
        return "image"
    if ext in VIDEO_EXTS:
        return "video"
    return None


def _skip_entry(entry: os.DirEntry, is_dir: bool) -> bool:
    name = entry.name
    if name.startswith((".", "$")) or (is_dir and name.casefold() in SKIP_DIRS):
        return True
    if sys.platform == "win32":
        try:
            attrs = entry.stat(follow_symlinks=False).st_file_attributes
        except OSError:
            return True
        if attrs & FILE_ATTRIBUTE_SYSTEM:  # 系統檔（如 Folder.jpg、回收筒）
            return True
        if is_dir and attrs & FILE_ATTRIBUTE_REPARSE_POINT:  # 目錄連接點，避免重複掃描或無限迴圈
            return True
    return False


def scan_folder(folder: Path, recursive: bool = True, exclude: Path | None = None) -> list[Path]:
    """找出資料夾內所有支援的圖片與影片（依路徑排序）。

    exclude 資料夾（例如輸出資料夾）、系統資料夾（資源回收筒、NAS 縮圖）、隱藏與系統檔都會被跳過。
    """
    exclude_resolved = os.path.normcase(str(Path(exclude).resolve())) if exclude else None
    found: list[Path] = []
    stack = [Path(folder)]
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:  # 沒有權限的資料夾直接略過
            continue
        for entry in entries:
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
                is_file = entry.is_file()
            except OSError:
                continue
            if _skip_entry(entry, is_dir):
                continue
            path = Path(entry.path)
            if is_dir:
                if recursive and os.path.normcase(str(path.resolve())) != exclude_resolved:
                    stack.append(path)
            elif is_file and media_kind(path):
                found.append(path)
    return sorted(found, key=lambda p: str(p).lower())


def _parse_date(raw) -> datetime | None:
    """解析各種日期格式，並排除明顯錯誤的日期（例如 0000:00:00 或未來時間）。"""
    match = DATE_RE.search(str(raw))
    if not match:
        return None
    y, mo, d, h, mi, s = (int(g) if g else 0 for g in match.groups())
    try:
        parsed = datetime(y, mo, d, h, mi, s)
    except ValueError:
        return None
    if parsed.year < MIN_YEAR or parsed > datetime.now() + timedelta(days=2):
        return None
    return parsed


def _exif_date(img: Image.Image) -> datetime | None:
    try:
        exif = img.getexif()
        ifd = exif.get_ifd(EXIF_IFD)
    except Exception:  # noqa: BLE001 - 損壞的 EXIF 不影響讀圖
        return None
    for group, tag in EXIF_DATE_TAGS:
        raw = (ifd if group else exif).get(tag)
        parsed = _parse_date(raw) if raw else None
        if parsed:
            return parsed
    return None


def file_date(path: Path) -> datetime:
    return datetime.fromtimestamp(os.path.getmtime(path))


def to_rgb(img: Image.Image) -> Image.Image:
    """轉成 RGB：16 位元灰階正確縮放、透明背景鋪白色（而不是變成全白或全黑）。"""
    if img.mode.startswith("I") or img.mode == "F":
        arr = np.asarray(img).astype(np.float32)
        top = 65535.0 if img.mode.startswith("I;16") else float(arr.max() or 1.0)
        return Image.fromarray((arr * (255.0 / top)).clip(0, 255).astype(np.uint8)).convert("RGB")
    if img.mode in ("RGBA", "LA", "PA", "RGBa", "La") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        background = Image.new("RGB", rgba.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.getchannel("A"))
        return background
    return img if img.mode == "RGB" else img.convert("RGB")


def _bomb_message(exc: Exception) -> str:
    match = re.search(r"(\d+) pixels", str(exc))
    if match:
        megapixels = int(match.group(1)) / 1_000_000
        return f"圖片太大（約 {megapixels:.0f} 百萬像素），為避免記憶體不足已略過"
    return "圖片太大，為避免記憶體不足已略過"


def load_image(path: Path, max_side: int | None = None) -> tuple[Image.Image, datetime | None]:
    """載入圖片並依 EXIF 轉正，回傳 (RGB 圖片, EXIF 拍攝日期)。

    先縮小再轉正與轉色彩，避免在全尺寸影像上做多次複製（省記憶體也快很多）。
    超過解壓縮炸彈上限的圖片會顯示成易懂的中文錯誤，而不是整包 Pillow 的例外訊息。
    """
    try:
        opened = Image.open(path)
    except Image.DecompressionBombError as exc:
        raise ValueError(_bomb_message(exc)) from exc
    with opened as img:
        # 很大張又不是 JPEG／MPO（draft 對它們無效，只能整張解碼）：同一時間只讓一張在解碼，避免併發時記憶體暴增。
        # 尺寸只需要讀檔頭就知道，所以在做任何可能解碼的事之前就先上鎖——
        # 讀 EXIF 也可能觸發解碼（例如 eXIf 區塊放在影像資料之後的 PNG），轉正、轉色彩也會複製整張圖。
        needs_lock = img.size[0] * img.size[1] > LARGE_DECODE_PIXELS and img.format not in ("JPEG", "MPO")
        if needs_lock:
            _decode_semaphore.acquire()
        try:
            date = _exif_date(img)
            if max_side:
                img.draft("RGB", (max_side, max_side))  # JPEG／MPO 直接以縮小比例解碼；其他格式無作用
                img.thumbnail((max_side, max_side))
            else:
                img.load()
            img = to_rgb(ImageOps.exif_transpose(img))
        except Image.DecompressionBombError as exc:
            raise ValueError(_bomb_message(exc)) from exc
        finally:
            if needs_lock:
                _decode_semaphore.release()
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
    if parsed.year < MIN_YEAR or parsed > datetime.now() + timedelta(days=2):  # 例如 QuickTime 的 1904 年
        return None
    return parsed


def _video_stream(container):
    import av

    for stream in container.streams.video:
        if not stream.disposition & av.stream.Disposition.attached_pic:  # 跳過封面圖
            return stream
    raise ValueError("這個檔案沒有影像（可能是純錄音檔）")


def _stream_sar(stream):
    for owner in (stream, getattr(stream, "codec_context", None)):
        sar = getattr(owner, "sample_aspect_ratio", None)
        if sar:
            return sar
    return None


def _frame_to_image(frame, max_side: int, sar=None) -> Image.Image:
    """依像素長寬比（SAR）與旋轉資訊，把影片畫面轉成正確方向、比例的縮小圖片。"""
    width = frame.width
    if sar and sar.numerator and sar.denominator and sar != 1:
        width = round(width * sar.numerator / sar.denominator)
    scale = min(1.0, max_side / max(width, frame.height))
    w, h = max(2, round(width * scale)), max(2, round(frame.height * scale))
    img = frame.reformat(width=w, height=h, format="rgb24").to_image()
    rotation = getattr(frame, "rotation", 0) or 0  # 手機直式影片
    if rotation % 90 == 0 and rotation % 360:
        img = img.rotate(rotation, expand=True)
    return img


def sample_video_frames(path: Path, count: int = 8, max_side: int = ANALYSIS_SIZE) -> tuple[list[Image.Image], datetime | None]:
    """從影片中平均取 count 張畫面（避開頭尾），回傳 (畫面清單, 影片建立日期)。

    跳轉後會往後解碼到目標時間（而不是直接用前一個關鍵畫格），並略過重複的畫格。
    """
    import av

    frames: list[Image.Image] = []
    with av.open(str(path)) as container:
        date = _video_date(container)
        stream = _video_stream(container)
        stream.thread_type = "AUTO"
        sar = _stream_sar(stream)
        time_base = stream.time_base
        start = stream.start_time or 0
        end = None
        if stream.duration:
            end = start + stream.duration
        elif container.duration and time_base:
            end = start + int(container.duration / av.time_base / time_base)
        if end is None and time_base:  # 沒有長度資訊（例如直播錄下的 WebM）：只讀封包找出最後時間，不解碼
            last = None
            for packet in container.demux(stream):
                if packet.pts is not None:
                    last = packet.pts if last is None else max(last, packet.pts)
            end = last
            container.seek(0)

        if end is not None and time_base and end > start:
            span = end - start
            targets = [start + int(span * (0.05 + 0.9 * (i + 0.5) / count)) for i in range(count)]
            reseek_gap = int(2 / time_base)  # 下一個目標超過 2 秒遠才重新跳轉，否則繼續往後解碼
            decoder = None
            position = None
            previous_pts = None
            for target in targets:
                try:
                    if decoder is None or position is None or target - position > reseek_gap:
                        container.seek(target, stream=stream, backward=True, any_frame=False)
                        decoder = container.decode(stream)
                    frame = None
                    for candidate in decoder:
                        position = candidate.pts
                        if candidate.pts is None or candidate.pts >= target:
                            frame = candidate
                            break
                except av.error.FFmpegError:
                    decoder, frame = None, None
                if frame is None:
                    continue
                if frame.pts is None or frame.pts != previous_pts:
                    frames.append(_frame_to_image(frame, max_side, sar))
                    previous_pts = frame.pts

        if not frames:  # 無法跳轉：從頭依序解碼，平均保留畫面
            container.seek(0)
            kept: list = []
            for i, frame in enumerate(container.decode(stream)):
                if i % 15 == 0:
                    kept.append(_frame_to_image(frame, max_side, sar))
                    if len(kept) > count * 2:
                        kept = kept[::2]
            step = max(1, len(kept) // count)
            frames = kept[::step][:count]
    if not frames:
        raise ValueError("影片中讀不到任何畫面")
    return frames, date


def thumbnail_bytes(img: Image.Image, size: int = THUMB_SIZE) -> bytes:
    thumb = img.copy()
    thumb.thumbnail((size, size))
    buf = io.BytesIO()
    thumb.save(buf, format="JPEG", quality=82)
    return buf.getvalue()


@dataclass
class LoadedMedia:
    frames: list[Image.Image]
    date: datetime
    thumbnail: bytes  # 預覽用縮圖（避免預覽時再讀原檔，HEIC 尤其慢）


def load_media(path: Path, kind: str, video_frames: int = 8) -> LoadedMedia:
    if kind == "video":
        frames, date = sample_video_frames(path, video_frames)
        return LoadedMedia(frames, date or file_date(path), thumbnail_bytes(frames[len(frames) // 2]))
    img, date = load_image(path, max_side=ANALYSIS_SIZE)
    return LoadedMedia([img], date or file_date(path), thumbnail_bytes(img))


def load_preview(path: Path, kind: str, cached: bytes | None, size: int = THUMB_SIZE) -> Image.Image:
    if cached:
        return Image.open(io.BytesIO(cached)).convert("RGB")
    if kind == "video":
        frames, _ = sample_video_frames(path, 1, max_side=size)
        return frames[0]
    img, _ = load_image(path, max_side=size)
    return img
