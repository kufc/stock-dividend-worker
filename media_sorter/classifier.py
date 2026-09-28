"""AI 辨識核心：載入多語言 CLIP 模型（有 NVIDIA 顯示卡時用 CUDA 加速），
把圖片／影片畫面與分類描述都轉成向量，再比對「最像哪個分類」。"""

from __future__ import annotations

import os

import numpy as np

from .config import MODEL_PRESETS, Category
from .vocabulary import VOCABULARY

os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

ACCURATE_MIN_VRAM_GB = 5.5
TEXT_BATCH = 64


def detect_device() -> tuple[str, str, float | None]:
    """回傳 (裝置名稱, 給使用者看的說明, 顯示卡記憶體 GB)。"""
    import torch

    if torch.cuda.is_available():
        props = torch.cuda.get_device_properties(0)
        mem_gb = props.total_memory / 1024**3
        return "cuda", f"顯示卡 {props.name}（{mem_gb:.1f} GB）", mem_gb
    mps = getattr(torch.backends, "mps", None)
    if mps is not None and mps.is_available():
        return "mps", "Apple 晶片 GPU", None
    return "cpu", "CPU（未偵測到可用的 NVIDIA 顯示卡，速度較慢）", None


def resolve_preset(choice: str, device: str, vram_gb: float | None) -> str:
    if choice in MODEL_PRESETS:
        return choice
    if device == "cuda" and vram_gb and vram_gb >= ACCURATE_MIN_VRAM_GB:
        return "accurate"
    return "standard"


def normalize(vectors: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(vectors, axis=-1, keepdims=True)
    return vectors / np.clip(norms, 1e-12, None)


def combine_frames(frame_embeddings: np.ndarray) -> np.ndarray:
    """影片的多張畫面向量取平均，代表整段影片。"""
    return normalize(frame_embeddings.mean(axis=0))


def score(media_embeddings: np.ndarray, class_embeddings: np.ndarray, logit_scale: float) -> np.ndarray:
    """回傳每個檔案屬於各分類的機率 (N, C)。"""
    logits = logit_scale * media_embeddings @ class_embeddings.T
    logits -= logits.max(axis=-1, keepdims=True)
    exp = np.exp(logits)
    return exp / exp.sum(axis=-1, keepdims=True)


class Classifier:
    def __init__(self, preset: str = "auto", *, arch: str | None = None, pretrained: str | None = None,
                 device: str | None = None):
        import open_clip
        import torch

        self._torch = torch
        if device is None:
            device, self.device_description, vram_gb = detect_device()
        else:
            self.device_description, vram_gb = device, None
        self.device = device
        if arch is None:
            self.preset = resolve_preset(preset, device, vram_gb)
            arch = MODEL_PRESETS[self.preset]["arch"]
            pretrained = MODEL_PRESETS[self.preset]["pretrained"]
        else:
            self.preset = arch
        self.model_name = arch

        # 顯示卡上用半精度（fp16）：記憶體減半、速度更快，辨識結果幾乎相同。
        precision = "pure_fp16" if device == "cuda" else "fp32"
        model, _, preprocess = open_clip.create_model_and_transforms(
            arch, pretrained=pretrained, device=device, precision=precision
        )
        model.eval()
        self.model = model
        self.preprocess = preprocess
        self.tokenizer = open_clip.get_tokenizer(arch)
        self.dtype = next(model.parameters()).dtype
        self.logit_scale = min(float(model.logit_scale.exp().item()), 100.0)
        self._vocab_embeddings: np.ndarray | None = None

    def prepare(self, image):
        """圖片前處理（縮放、裁切、正規化）。可在多個執行緒同時呼叫。"""
        return self.preprocess(image)

    def encode_prepared(self, tensors: list) -> np.ndarray:
        torch = self._torch
        batch = torch.stack(tensors).to(self.device, dtype=self.dtype)
        try:
            with torch.inference_mode():
                feats = self.model.encode_image(batch)
        except torch.cuda.OutOfMemoryError:
            if len(tensors) == 1:
                raise
            torch.cuda.empty_cache()
            half = len(tensors) // 2
            return np.concatenate([self.encode_prepared(tensors[:half]), self.encode_prepared(tensors[half:])])
        return normalize(feats.float().cpu().numpy())

    def encode_images(self, images: list) -> np.ndarray:
        return self.encode_prepared([self.prepare(img) for img in images])

    def encode_texts(self, texts: list[str]) -> np.ndarray:
        torch = self._torch
        chunks = []
        for i in range(0, len(texts), TEXT_BATCH):
            tokens = self.tokenizer(texts[i:i + TEXT_BATCH]).to(self.device)
            with torch.inference_mode():
                chunks.append(self.model.encode_text(tokens).float().cpu().numpy())
        return normalize(np.concatenate(chunks))

    def _encode_groups(self, groups: list[list[str]]) -> np.ndarray:
        """每組描述各自轉成向量後取平均（prompt ensemble），比單一描述更穩定。"""
        flat = [text for group in groups for text in group]
        embeddings = self.encode_texts(flat)
        result, start = [], 0
        for group in groups:
            result.append(normalize(embeddings[start:start + len(group)].mean(axis=0)))
            start += len(group)
        return np.stack(result)

    def encode_categories(self, categories: list[Category]) -> np.ndarray:
        return self._encode_groups([c.texts() for c in categories])

    def vocabulary_embeddings(self) -> np.ndarray:
        if self._vocab_embeddings is None:
            self._vocab_embeddings = self._encode_groups(
                [[zh, en, f"a photo of {en}"] for zh, en in VOCABULARY]
            )
        return self._vocab_embeddings


def download_model(choice: str = "auto") -> None:
    """安裝時預先下載模型並做一次辨識測試，確認顯示卡可以正常運作。"""
    from PIL import Image

    device, description, vram_gb = detect_device()
    preset = resolve_preset(choice, device, vram_gb)
    print(f"運算裝置：{description}")
    print(f"模型：{MODEL_PRESETS[preset]['label']}")
    print("下載並載入模型中（第一次需要一些時間）⋯")
    clf = Classifier(preset)
    image_vec = clf.encode_images([Image.new("RGB", (224, 224), (200, 120, 40))])
    text_vec = clf.encode_texts(["測試", "test"])
    assert image_vec.shape[1] == text_vec.shape[1]
    print("模型測試成功！")
