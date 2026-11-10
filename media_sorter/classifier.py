"""AI 辨識核心：載入多語言 CLIP 模型（有 NVIDIA 顯示卡時用 CUDA 加速），
把圖片／影片畫面與分類描述都轉成向量，再比對「最像哪個分類」。"""

from __future__ import annotations

import os
import numpy as np

from . import netpolicy
from .config import MODEL_PRESETS, Category
from .vocabulary import VOCABULARY

os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

ACCURATE_MIN_VRAM_GB = 5.5
ACCURATE_MIN_RAM_GB = 12  # 載入高精準模型時系統記憶體也要夠
TEXT_BATCH = 64
# 「與最像分類的原始相似度」低於此值 → 視為「哪個分類都不太像」。實測（tests/manual/real_model_results.txt）
# 這個數字分不出來：分對的照片相似度 0.10～0.25，不屬於任何分類的也是 0.12～0.25；以前設 0.18，
# 結果大半分對的檔案都被標成「需檢查」。所以預設關掉（0），「需檢查」只看模型分數。
SIMILARITY_FLOOR = 0.0
# 「什麼都不是」的描述。檔案跟這個描述比跟任何一個分類都還像 → 可能不屬於任何分類，放進「需檢查」。
# 用 425 張真實照片實測（tests/manual/embeddings_standard.npz）：不屬於任何分類的照片，被標出來的比例從
# 31% 提高到 53%，分對的照片被誤標的比例不變（0.8%）。試過的其他寫法（英文、「其他」、「一張照片」）誤標都比較多。
OTHER_TEXTS = ("其他東西", "雜物")
# 「常出現但還沒有分類」的建議：詞彙跟檔案目前所在分類的文字相似度高於此值，就當作那個分類已經涵蓋它
# （例如「汽車」之於「交通工具」），不再建議。同一批實測：教堂／汽車／公車被濾掉，魚／樂器／球類運動留下。
TAG_COVERED_SIMILARITY = 0.82


def total_ram_gb() -> float | None:
    try:
        if os.name == "nt":
            import ctypes

            class MemoryStatus(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

            status = MemoryStatus()
            status.dwLength = ctypes.sizeof(MemoryStatus)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
            return status.ullTotalPhys / 1024**3
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1024**3
    except (AttributeError, OSError, ValueError):
        return None


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


def resolve_preset(choice: str, device: str, vram_gb: float | None, ram_gb: float | None = None) -> str:
    if choice in MODEL_PRESETS:
        return choice
    enough_ram = ram_gb is None or ram_gb >= ACCURATE_MIN_RAM_GB
    if device == "cuda" and vram_gb and vram_gb >= ACCURATE_MIN_VRAM_GB and enough_ram:
        return "accurate"
    return "standard"


def _is_oom(exc: BaseException) -> bool:
    import torch

    if isinstance(exc, torch.cuda.OutOfMemoryError):
        return True
    text = str(exc).lower()
    return isinstance(exc, RuntimeError) and ("out of memory" in text or "alloc_failed" in text)


def normalize(vectors: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(vectors, axis=-1, keepdims=True)
    return vectors / np.clip(norms, 1e-12, None)


def combine_frames(frame_embeddings: np.ndarray) -> np.ndarray:
    """多張畫面的向量取平均（細長圖片切成的幾塊、或代表整段影片的向量）。"""
    return normalize(frame_embeddings.mean(axis=0))


def score_frames(frame_embeddings: np.ndarray, class_embeddings: np.ndarray, logit_scale: float) -> np.ndarray:
    """影片：每張畫面各自比對後，把機率平均 (C,)。

    內容一致的影片跟「向量平均」結果一樣；畫面內容差很多的影片，向量平均會得到一個很有把握的怪答案，
    機率平均則會老實地顯示成幾個分類各占一些（實測見 tests/manual/real_model_results.txt）。
    """
    return score(frame_embeddings, class_embeddings, logit_scale).mean(axis=0)


def score(media_embeddings: np.ndarray, class_embeddings: np.ndarray, logit_scale: float) -> np.ndarray:
    """回傳每個檔案屬於各分類的機率 (N, C)。"""
    logits = logit_scale * media_embeddings @ class_embeddings.T
    logits -= logits.max(axis=-1, keepdims=True)
    exp = np.exp(logits)
    return exp / exp.sum(axis=-1, keepdims=True)


class Classifier:
    def __init__(self, preset: str = "auto", *, arch: str | None = None, pretrained: str | None = None,
                 device: str | None = None):
        import torch

        self._torch = torch
        if device is None:
            device, self.device_description, vram_gb = detect_device()
        else:
            self.device_description, vram_gb = device, None
        self.device = device
        if arch is None:
            self.preset = resolve_preset(preset, device, vram_gb, total_ram_gb())
            arch = MODEL_PRESETS[self.preset]["arch"]
            pretrained = MODEL_PRESETS[self.preset]["pretrained"]
        else:
            self.preset = arch
        self.model_name = arch

        # 模型已經在這台電腦上 → 離線載入，完全不連網；還沒下載才連網（見 netpolicy）
        self.loaded_offline = netpolicy.prepare_for(self.preset) if self.preset in MODEL_PRESETS else False
        import open_clip

        # 顯示卡上用半精度（fp16）：記憶體減半、速度更快，辨識結果幾乎相同。
        precision = "pure_fp16" if device == "cuda" else "fp32"

        def load():
            model, _, preprocess = open_clip.create_model_and_transforms(
                arch, pretrained=pretrained, device=device, precision=precision
            )
            return model, preprocess, open_clip.get_tokenizer(arch)

        try:
            model, preprocess, tokenizer = load()
        except Exception:
            if not self.loaded_offline:
                raise
            # 快取不完整（例如上次下載中斷）：改為連網下載補齊
            self.loaded_offline = False
            netpolicy.set_offline(False)
            model, preprocess, tokenizer = load()
        model.eval()
        self.model = model
        self.preprocess = preprocess
        self.tokenizer = tokenizer
        self.dtype = next(model.parameters()).dtype
        self.logit_scale = min(float(model.logit_scale.exp().item()), 100.0)
        self.similarity_floor = SIMILARITY_FLOOR if pretrained else 0.0
        self._vocab_embeddings: np.ndarray | None = None
        self._other_embedding: np.ndarray | None = None
        self._category_cache: dict[tuple, np.ndarray] = {}  # 分類描述 → 向量（同一份清單不用重算）
        self._max_batch: int | None = None  # 顯示卡記憶體不足後學到的安全批次大小
        self.fell_back_to_fp32 = False

    def prepare(self, image):
        """圖片前處理（縮放、裁切、正規化）。可在多個執行緒同時呼叫。"""
        return self.preprocess(image)

    def _use_fp32(self) -> bool:
        """半精度算出 NaN（部分 GTX 16 系列等顯示卡會發生）時，改用 fp32 重算。"""
        if self.fell_back_to_fp32 or self.dtype == self._torch.float32:
            return False
        self.model.float()
        self.dtype = self._torch.float32
        self.fell_back_to_fp32 = True
        self._vocab_embeddings = None
        self._other_embedding = None
        self._category_cache.clear()
        return True

    def encode_prepared(self, tensors: list) -> np.ndarray:
        if self._max_batch and len(tensors) > self._max_batch:
            step = self._max_batch
            return np.concatenate([self.encode_prepared(tensors[i:i + step]) for i in range(0, len(tensors), step)])
        torch = self._torch
        oom = False
        try:
            batch = torch.stack(tensors).to(self.device, dtype=self.dtype)
            with torch.inference_mode():
                feats = self.model.encode_image(batch).float().cpu().numpy()
        except (torch.cuda.OutOfMemoryError, RuntimeError) as exc:
            if not _is_oom(exc) or len(tensors) == 1:
                raise
            oom = True
        if oom:  # 在 except 區塊外處理，失敗那批的記憶體才會真的被釋放
            batch = None
            if self.device == "cuda":
                torch.cuda.empty_cache()
            self._max_batch = max(1, len(tensors) // 2)
            return self.encode_prepared(tensors)
        if not np.isfinite(feats).all() and self._use_fp32():
            return self.encode_prepared(tensors)
        return normalize(feats)

    def encode_images(self, images: list) -> np.ndarray:
        return self.encode_prepared([self.prepare(img) for img in images])

    def encode_texts(self, texts: list[str]) -> np.ndarray:
        torch = self._torch
        chunks = []
        for i in range(0, len(texts), TEXT_BATCH):
            tokens = self.tokenizer(texts[i:i + TEXT_BATCH]).to(self.device)
            with torch.inference_mode():
                chunks.append(self.model.encode_text(tokens).float().cpu().numpy())
        result = np.concatenate(chunks)
        if not np.isfinite(result).all() and self._use_fp32():
            return self.encode_texts(texts)
        return normalize(result)

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
        key = tuple(tuple(c.texts()) for c in categories)
        if key not in self._category_cache:
            if len(self._category_cache) >= 4:
                self._category_cache.pop(next(iter(self._category_cache)))
            self._category_cache[key] = self._encode_groups([list(texts) for texts in key])
        return self._category_cache[key]

    def other_embedding(self) -> np.ndarray:
        """「什麼都不是」的向量（見 OTHER_TEXTS）。"""
        if self._other_embedding is None:
            self._other_embedding = self._encode_groups([list(OTHER_TEXTS)])[0]
        return self._other_embedding

    def vocabulary_embeddings(self) -> np.ndarray:
        if self._vocab_embeddings is None:
            self._vocab_embeddings = self._encode_groups(
                [[zh, en, f"a photo of {en}"] for zh, en in VOCABULARY]
            )
        return self._vocab_embeddings


def model_status(choice: str = "auto") -> tuple[str, bool | None] | None:
    """給介面顯示用：(模型名稱, 是否已下載)。已下載＝這台電腦的 Hugging Face 快取裡已有模型與斷詞器。

    不載入 huggingface_hub（也就不會連網）；判斷不出來就回傳 None，介面就不顯示這一行。
    """
    try:
        device, _, vram_gb = detect_device()
        preset = resolve_preset(choice, device, vram_gb, total_ram_gb())
        return MODEL_PRESETS[preset]["label"], netpolicy.preset_cached(preset)
    except Exception:  # noqa: BLE001 - 只是提示資訊，任何原因失敗都不影響使用
        return None


def download_model(choice: str = "auto") -> None:
    """安裝時預先下載模型並做一次辨識測試，確認顯示卡可以正常運作。"""
    from PIL import Image

    device, description, vram_gb = detect_device()
    preset = resolve_preset(choice, device, vram_gb, total_ram_gb())
    print(f"運算裝置：{description}")
    print(f"模型：{MODEL_PRESETS[preset]['label']}")
    print("下載並載入模型中（第一次需要一些時間）⋯")
    clf = Classifier(preset)
    image_vec = clf.encode_images([Image.new("RGB", (224, 224), (200, 120, 40))])
    text_vec = clf.encode_texts(["測試", "test"])
    assert image_vec.shape[1] == text_vec.shape[1]
    if not (np.isfinite(image_vec).all() and np.isfinite(text_vec).all()):
        raise RuntimeError("模型計算結果異常（NaN），請回報你的顯示卡型號")
    if clf.fell_back_to_fp32:
        print("注意：這張顯示卡的半精度計算不穩定，已自動改用 fp32（速度較慢但結果正確）。")
    print("模型測試成功！")
