"""設定值範圍、圖片大小上限與大圖解碼鎖的回歸測試（來自第二、三輪外部審查）。"""
import threading

import pytest
from PIL import Image

from fakes import FakeClassifier
from helpers import make_image
from media_sorter import media
from media_sorter.config import MODEL_CHOICES, load_settings, save_settings


def test_load_settings_clamps_invalid_values(tmp_path):
    path = tmp_path / "settings.json"
    save_settings({
        "batch_size": 0, "video_frames": 0, "confidence_threshold": 5,
        "model": "不存在的模型", "include_subfolders": "yes",
    }, path)

    settings = load_settings(path)

    assert settings["batch_size"] >= 1
    assert settings["video_frames"] >= 1
    assert 0.05 <= settings["confidence_threshold"] <= 0.95
    assert settings["model"] in MODEL_CHOICES
    assert isinstance(settings["include_subfolders"], bool)

    save_settings({"batch_size": -5, "video_frames": "abc"}, path)
    settings2 = load_settings(path)
    assert settings2["batch_size"] >= 1
    assert settings2["video_frames"] >= 1


def test_analyze_with_batch_size_zero_completes_quickly(tmp_path):
    from media_sorter.analysis import analyze
    from media_sorter.config import Category

    paths = [make_image(tmp_path / f"{i}.png", (250, 10, 10)) for i in range(4)]
    result: dict = {}
    worker = threading.Thread(target=lambda: result.update(items=analyze(
        paths, FakeClassifier(), [Category("紅")], batch_size=0, video_frames=0)))
    worker.start()
    worker.join(5)
    assert not worker.is_alive(), "analyze(batch_size=0) 卡住了"
    assert len(result["items"]) == 4 and all(it.analyzed for it in result["items"])


def test_max_image_pixels_is_capped_around_300_million():
    assert 250_000_000 < Image.MAX_IMAGE_PIXELS < 400_000_000


def test_oversized_image_reports_friendly_chinese_error(tmp_path, monkeypatch):
    from media_sorter import media

    monkeypatch.setattr(media.Image, "MAX_IMAGE_PIXELS", 100)  # 把門檻調到很小
    path = make_image(tmp_path / "big.png", (10, 20, 30), size=(64, 48))  # 64*48 遠超過門檻

    with pytest.raises(ValueError, match="圖片太大"):
        media.load_image(path)


def test_large_non_jpeg_image_uses_decode_semaphore(tmp_path, monkeypatch):
    from PIL import ImageFile

    from media_sorter import media

    monkeypatch.setattr(media, "LARGE_DECODE_PIXELS", 1000)  # 64*48=3072，會被視為「很大」
    path = make_image(tmp_path / "big.png", (10, 20, 30), size=(64, 48))
    # Pillow 讀取這張圖前後會呼叫好幾次 load()（讀 EXIF、轉色彩⋯），只有解碼那一次需要鎖住
    lock_states = []
    real_load = ImageFile.ImageFile.load  # PNG 等格式的 load() 是在 ImageFile 這個中介類別實作，不是 Image.Image

    def spy_load(self):
        lock_states.append(media._decode_semaphore._value == 0)
        return real_load(self)

    monkeypatch.setattr(ImageFile.ImageFile, "load", spy_load)

    media.load_image(path)

    assert any(lock_states)  # 至少有一次是在鎖住的狀態下解碼
    assert media._decode_semaphore._value == 1


def test_png_decode_happens_only_while_locked(tmp_path, monkeypatch):
    from PIL import ImageFile

    monkeypatch.setattr(media, "LARGE_DECODE_PIXELS", 1000)
    path = tmp_path / "big.png"
    img = Image.new("RGB", (64, 48), (10, 20, 30))
    exif = Image.Exif()
    exif[306] = "2020:01:02 03:04:05"
    img.save(path, exif=exif)  # PNG 的 eXIf 在影像資料之後時，讀 EXIF 會觸發解碼
    unlocked_decodes = []
    real_load = ImageFile.ImageFile.load

    def spy_load(self):
        if getattr(self, "tile", None):  # 還有沒解碼的資料 = 這次呼叫會真的解碼
            unlocked_decodes.append(media._decode_semaphore._value == 1)
        return real_load(self)

    monkeypatch.setattr(ImageFile.ImageFile, "load", spy_load)
    for max_side in (640, None):
        media.load_image(path, max_side=max_side)
    assert unlocked_decodes and not any(unlocked_decodes)


def test_threads_do_not_share_decode_slot_leak(tmp_path, monkeypatch):
    monkeypatch.setattr(media, "LARGE_DECODE_PIXELS", 1000)
    paths = [make_image(tmp_path / f"{i}.png", (i, 0, 0), size=(64, 48)) for i in range(6)]
    threads = [threading.Thread(target=media.load_image, args=(p, 640)) for p in paths]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert media._decode_semaphore._value == 1
