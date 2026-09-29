"""設定檔與分類清單的讀寫。

使用者資料都放在程式資料夾（start.bat 旁邊），方便找到與備份：
- categories.json：分類清單（可在程式內「分類設定」編輯）
- settings.json：其他設定（模型、影片取樣張數、上次使用的資料夾⋯）
- logs/：每次整理的紀錄（可用來復原）與錯誤紀錄
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

APP_DIR = Path(os.environ.get("MEDIA_SORTER_HOME", Path(__file__).resolve().parent.parent))
CATEGORIES_FILE = APP_DIR / "categories.json"
SETTINGS_FILE = APP_DIR / "settings.json"
LOG_DIR = APP_DIR / "logs"

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp", ".tif", ".tiff", ".heic", ".heif"}
VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".wmv", ".m4v", ".webm", ".flv", ".3gp", ".mts", ".m2ts", ".mpg", ".mpeg"}

# 多語言 CLIP 模型，分類名稱可以直接用中文。
MODEL_PRESETS = {
    "standard": {
        "label": "標準（速度快，首次下載約 1.5 GB）",
        "arch": "xlm-roberta-base-ViT-B-32",
        "pretrained": "laion5b_s13b_b90k",
    },
    "accurate": {
        "label": "高精準（首次下載約 5 GB，建議顯示卡記憶體 6 GB 以上）",
        "arch": "xlm-roberta-large-ViT-H-14",
        "pretrained": "frozen_laion5b_s13b_b90k",
    },
}
MODEL_CHOICES = {
    "auto": "自動（依顯示卡記憶體選擇）",
    **{key: preset["label"] for key, preset in MODEL_PRESETS.items()},
}

RENAME_PATTERN_PRESETS = [
    "{分類}_{序號}",
    "{分類}_{日期}_{序號}",
    "{日期}_{分類}_{序號}",
    "{分類}_{原檔名}",
]

DEFAULT_SETTINGS = {
    "model": "auto",
    "video_frames": 8,
    "batch_size": 16,
    "confidence_threshold": 0.5,
    "include_subfolders": True,
    "action": "move",
    "rename": True,
    "rename_pattern": RENAME_PATTERN_PRESETS[0],
    "output_dir": "",
    "last_folder": "",
}


@dataclass
class Category:
    """一個分類。name 會用來當資料夾名稱與檔名；prompts 是給 AI 看的描述（中英文皆可，越具體越準）。"""

    name: str
    prompts: list[str] = field(default_factory=list)

    def texts(self) -> list[str]:
        texts = [p.strip() for p in self.prompts if p.strip()]
        texts.append(self.name)
        texts.append(f"一張{self.name}的照片")
        return list(dict.fromkeys(texts))

    def to_dict(self) -> dict:
        return {"name": self.name, "prompts": self.prompts}


DEFAULT_CATEGORIES = [
    Category("人像", ["人物照片", "自拍", "a photo of a person", "a selfie"]),
    Category("合照", ["一群人的合照", "a group photo of several people"]),
    Category("貓", ["貓咪", "a photo of a cat"]),
    Category("狗", ["狗狗", "a photo of a dog"]),
    Category("風景", ["自然風景", "山、海、天空", "a landscape photo of mountains, sea, sky or nature"]),
    Category("城市建築", ["街景與建築物", "a photo of city streets and buildings"]),
    Category("美食", ["食物與餐點", "a photo of food or a meal"]),
    Category("花草植物", ["花與植物", "a photo of flowers or plants"]),
    Category("交通工具", ["汽車、機車、火車、飛機", "a photo of a car, motorcycle, train or airplane"]),
    Category("螢幕截圖", ["手機或電腦的截圖", "a screenshot of a phone or computer screen"]),
    Category("文件收據", ["文件、收據、紙本文字", "a photo of a document, receipt or paper with text"]),
    Category("動漫插畫", ["卡通或動漫圖", "an anime or cartoon illustration"]),
]


def _read_json(path: Path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def load_categories(path: Path = CATEGORIES_FILE) -> list[Category]:
    data = _read_json(path)
    if not data or not data.get("categories"):
        return [Category(c.name, list(c.prompts)) for c in DEFAULT_CATEGORIES]
    categories = []
    for entry in data["categories"]:
        name = str(entry.get("name", "")).strip()
        if name:
            categories.append(Category(name, [str(p) for p in entry.get("prompts", [])]))
    return categories


def save_categories(categories: list[Category], path: Path = CATEGORIES_FILE) -> None:
    _write_json(path, {"categories": [c.to_dict() for c in categories]})


def _clamped_int(value, low: int, high: int, default: int) -> int:
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return default


def _clamped_float(value, low: float, high: float, default: float) -> float:
    try:
        return max(low, min(high, float(value)))
    except (TypeError, ValueError):
        return default


def load_settings(path: Path = SETTINGS_FILE) -> dict:
    """讀取設定；數值會夾在合理範圍內，型別不對或壞掉的值一律用預設值（避免例如 batch_size=0 讓辨識空轉）。"""
    settings = dict(DEFAULT_SETTINGS)
    data = _read_json(path)
    if isinstance(data, dict):
        settings.update({k: v for k, v in data.items() if k in DEFAULT_SETTINGS})
    settings["batch_size"] = _clamped_int(settings.get("batch_size"), 1, 256, DEFAULT_SETTINGS["batch_size"])
    settings["video_frames"] = _clamped_int(settings.get("video_frames"), 1, 32, DEFAULT_SETTINGS["video_frames"])
    settings["confidence_threshold"] = _clamped_float(
        settings.get("confidence_threshold"), 0.05, 0.95, DEFAULT_SETTINGS["confidence_threshold"])
    if settings.get("model") not in MODEL_CHOICES:
        settings["model"] = DEFAULT_SETTINGS["model"]
    if settings.get("action") not in ("move", "copy"):
        settings["action"] = DEFAULT_SETTINGS["action"]
    for key in ("include_subfolders", "rename"):
        if not isinstance(settings.get(key), bool):
            settings[key] = DEFAULT_SETTINGS[key]
    return settings


def save_settings(settings: dict, path: Path = SETTINGS_FILE) -> None:
    _write_json(path, settings)


def log_key_dir() -> Path:
    """整理紀錄簽章金鑰的存放位置：刻意放在程式資料夾（和 logs）以外。

    Windows：%LOCALAPPDATA%\\AIMediaSorter；其他系統：~/.local/share/ai-media-sorter。
    這樣只拿到（或同步、複製）程式資料夾與紀錄檔的人，無法偽造出能通過驗證的紀錄。
    """
    override = os.environ.get("MEDIA_SORTER_KEY_DIR")
    if override:
        return Path(override)
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "AIMediaSorter"
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "ai-media-sorter"


def log_signing_key() -> bytes:
    """讀取（第一次使用時建立）整理紀錄的簽章金鑰（32 位元組亂數）。"""
    import secrets

    path = log_key_dir() / "log-signing.key"
    for _ in range(2):
        try:
            key = bytes.fromhex(path.read_text(encoding="ascii").strip())
            if len(key) == 32:
                return key
            raise ValueError("金鑰長度不正確")
        except FileNotFoundError:
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:  # 另一個程序剛好同時建立：重新讀取
                continue
            with os.fdopen(fd, "w", encoding="ascii") as f:
                f.write(secrets.token_hex(32))
        except ValueError as exc:
            # 金鑰檔損壞：不自動覆蓋（否則舊紀錄全部失效且難以察覺），請使用者處理
            raise OSError(f"整理紀錄簽章金鑰檔損壞：{path}（{exc}）") from exc
    raise OSError(f"無法建立整理紀錄簽章金鑰：{path}")


def acquire_app_lock(lock_dir: Path | None = None):
    """取得「程式執行中」的鎖；已被另一個視窗（或安裝程式）持有時回傳 None。

    回傳的檔案物件要一直保留到程式結束（關閉就會釋放鎖）。
    """
    lock_dir = lock_dir or LOG_DIR
    try:
        lock_dir.mkdir(parents=True, exist_ok=True)
        handle = open(lock_dir / "app.lock", "a+")
    except OSError:
        return None
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle
