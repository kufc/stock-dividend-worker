"""測試用的假模型：用圖片平均顏色當作向量，不需要下載真正的 AI 模型。"""

import numpy as np
from PIL import Image

from media_sorter.classifier import normalize
from media_sorter.vocabulary import VOCABULARY

COLOR_VECTORS = {"紅": [1, 0, 0], "綠": [0, 1, 0], "藍": [0, 0, 1]}


class FakeClassifier:
    device_description = "測試用假模型"
    logit_scale = 100.0

    def __init__(self, preset="auto"):
        self.preset = preset
        self.encoded_images = 0

    def prepare(self, image: Image.Image):
        return np.asarray(image.convert("RGB"), dtype=np.float32).mean(axis=(0, 1))

    def encode_prepared(self, tensors):
        self.encoded_images += len(tensors)
        return normalize(np.stack(tensors) + 1e-3)

    def encode_categories(self, categories):
        rng = np.random.default_rng(0)
        vectors = [COLOR_VECTORS.get(c.name, rng.random(3)) for c in categories]
        return normalize(np.asarray(vectors, dtype=np.float32))

    def vocabulary_embeddings(self):
        rng = np.random.default_rng(1)
        return normalize(rng.random((len(VOCABULARY), 3)))
