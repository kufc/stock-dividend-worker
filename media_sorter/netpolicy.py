"""連網政策：模型已經在這台電腦上時，完全不連網。

模型（CLIP）與斷詞器（tokenizer）都在本機執行，照片不會上傳。但 Hugging Face 的元件預設每次載入
都會先連到 huggingface.co 確認有沒有新版本，也會送使用統計；防毒軟體（例如卡巴斯基）每次都會攔到。

做法：
- 一律關閉使用統計（HF_HUB_DISABLE_TELEMETRY）。
- 這次要用的模型與斷詞器都已在快取裡 → 離線模式載入，不發出任何網路請求。
- 還沒下載（第一次使用）或快取不完整 → 才允許連網下載。
這個模組只用標準函式庫，必須在載入 huggingface_hub／transformers 之前呼叫。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from .config import MODEL_PRESETS

os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
os.environ.setdefault("DO_NOT_TRACK", "1")


def hf_cache_dir(environ: dict | None = None) -> Path:
    """Hugging Face 的模型快取位置（跟 huggingface_hub 的規則相同）。"""
    env = os.environ if environ is None else environ
    if env.get("HF_HUB_CACHE"):
        return Path(env["HF_HUB_CACHE"])
    if env.get("HF_HOME"):
        return Path(env["HF_HOME"]) / "hub"
    if env.get("XDG_CACHE_HOME"):
        return Path(env["XDG_CACHE_HOME"]) / "huggingface" / "hub"
    return Path.home() / ".cache" / "huggingface" / "hub"


def cache_folder(repo_id: str) -> str:
    return "models--" + repo_id.strip("/").replace("/", "--")


def repo_cached(repo_id: str, cache: Path | None = None) -> bool:
    """快取裡有這個模型的檔案（snapshots 底下至少一個版本有檔案）。"""
    snapshots = (cache or hf_cache_dir()) / cache_folder(repo_id) / "snapshots"
    try:
        return snapshots.is_dir() and any(p.is_file() or p.is_symlink() for p in snapshots.glob("*/*"))
    except OSError:
        return False


def preset_cached(preset: str, cache: Path | None = None) -> bool:
    info = MODEL_PRESETS[preset]
    return repo_cached(info["hf_repo"], cache) and repo_cached(info["tokenizer_repo"], cache)


def set_offline(offline: bool) -> None:
    """切換離線模式。環境變數給之後才載入的元件；已載入的 huggingface_hub 直接改它的設定值。"""
    value = "1" if offline else "0"
    os.environ["HF_HUB_OFFLINE"] = value
    os.environ["TRANSFORMERS_OFFLINE"] = value
    constants = sys.modules.get("huggingface_hub.constants")
    if constants is not None:
        constants.HF_HUB_OFFLINE = offline


def prepare_for(preset: str, cache: Path | None = None) -> bool:
    """載入模型前呼叫：已下載就離線（回傳 True），否則允許連網下載（回傳 False）。"""
    offline = preset_cached(preset, cache)
    set_offline(offline)
    return offline
