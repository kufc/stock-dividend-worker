"""設定檔與分類清單的讀寫。

使用者資料的位置：
- 用安裝程式（setup.exe）安裝時：程式在安裝資料夾，使用者資料放在 %LOCALAPPDATA%\\AI Media Sorter
- 免安裝（ZIP＋install.bat）時：放在程式資料夾（start.bat 旁邊），方便找到與備份
內容：
- categories.json：分類清單（可在程式內「分類設定」編輯）
- settings.json：其他設定（模型、影片取樣張數、上次使用的資料夾⋯）
- logs/：每次整理的紀錄（可用來復原）與錯誤紀錄
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INSTALLED = (ROOT / "installed.flag").is_file()  # 由安裝程式（setup.exe）安裝的版本
DATA_DIR_NAME = "AI Media Sorter"
DATA_MARKER = ".media-sorter-data"  # 資料資料夾裡的標記檔；解除安裝程式只會清理有這個標記的資料夾


def default_data_dir() -> Path:
    override = os.environ.get("MEDIA_SORTER_HOME")
    if override:
        return Path(override)
    if INSTALLED:
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / DATA_DIR_NAME
    return ROOT


APP_DIR = default_data_dir()
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
        # 在 Hugging Face 上的位置（判斷「已下載」與解除安裝時使用；要跟 open_clip 的設定一致，見 tests）
        "hf_repo": "laion/CLIP-ViT-B-32-xlm-roberta-base-laion5B-s13B-b90k",
        "tokenizer_repo": "xlm-roberta-base",
    },
    "accurate": {
        "label": "高精準（首次下載約 5 GB，建議顯示卡記憶體 6 GB 以上）",
        "arch": "xlm-roberta-large-ViT-H-14",
        "pretrained": "frozen_laion5b_s13b_b90k",
        "hf_repo": "laion/CLIP-ViT-H-14-frozen-xlm-roberta-large-laion5B-s13B-b90k",
        "tokenizer_repo": "xlm-roberta-large",
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

# 整理時的命名方式：(顯示名稱, 樣式)；樣式是 None 代表保留原檔名
RENAME_MODES = {
    "keep": ("保留原檔名", None),
    "category_seq": ("分類＋序號　例：貓_001.jpg", "{分類}_{序號}"),
    "date_category_seq": ("日期＋分類＋序號　例：20240503_貓_001.jpg", "{日期}_{分類}_{序號}"),
    "custom": ("自訂格式", None),
}
DEFAULT_SPLIT_RATIO = 0.42  # 「確認分類」畫面左邊清單佔的寬度比例

DEFAULT_SETTINGS = {
    "model": "auto",
    "video_frames": 8,
    "batch_size": 16,
    "confidence_threshold": 0.5,
    "include_subfolders": True,
    "rename_mode": "keep",
    "rename_pattern": RENAME_PATTERN_PRESETS[0],
    "split_ratio": DEFAULT_SPLIT_RATIO,
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


def ensure_data_dir(path: Path | None = None) -> None:
    """建立使用者資料夾並放入標記檔（資料夾就是程式資料夾本身時不需要）。失敗不影響使用。"""
    path = Path(path) if path is not None else APP_DIR
    if path == ROOT:
        return
    try:
        path.mkdir(parents=True, exist_ok=True)
        marker = path / DATA_MARKER
        if not marker.exists():
            marker.write_text("AI Media Sorter user data. Do not delete this file.\n", encoding="utf-8")
    except OSError:
        pass


def _read_json(path: Path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent == APP_DIR:
        ensure_data_dir()
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
        name = " ".join(str(entry.get("name", "")).split())  # 名稱中的換行、定位字元一律換成空白
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
    if settings.get("rename_mode") not in RENAME_MODES:
        settings["rename_mode"] = DEFAULT_SETTINGS["rename_mode"]
    if not isinstance(settings.get("rename_pattern"), str) or not settings["rename_pattern"].strip():
        settings["rename_pattern"] = DEFAULT_SETTINGS["rename_pattern"]
    settings["split_ratio"] = _clamped_float(settings.get("split_ratio"), 0.25, 0.75, DEFAULT_SPLIT_RATIO)
    for key in ("include_subfolders",):
        if not isinstance(settings.get(key), bool):
            settings[key] = DEFAULT_SETTINGS[key]
    return settings


def save_settings(settings: dict, path: Path = SETTINGS_FILE) -> None:
    _write_json(path, settings)


def acquire_app_lock(lock_dir: Path | None = None):
    """取得「程式執行中」的鎖；已被另一個視窗（或安裝程式）持有時回傳 None。

    回傳的檔案物件要一直保留到程式結束（關閉就會釋放鎖）。
    """
    lock_dir = lock_dir or LOG_DIR
    try:
        lock_dir.mkdir(parents=True, exist_ok=True)
        if lock_dir == LOG_DIR:
            ensure_data_dir()
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
