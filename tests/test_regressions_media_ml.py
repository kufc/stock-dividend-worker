"""對抗測試找到的媒體讀取與 AI 流程問題的回歸測試。"""

import threading
import time

import numpy as np
import pytest
from PIL import Image

from fakes import FakeClassifier
from helpers import make_image
from media_sorter import media
from media_sorter.analysis import (CONFIRMED, ERROR, PENDING, SKIPPED, Item, accept_confident, analyze,
                                   rescore, square_crops)
from media_sorter.classifier import resolve_preset
from media_sorter.config import Category

CATS = [Category("紅"), Category("綠"), Category("藍")]


def test_scan_skips_system_and_nas_folders(tmp_path):
    for rel in ["$RECYCLE.BIN/S-1-5/$R1.jpg", "@eaDir/a.jpg/SYNOPHOTO_THUMB_M.jpg",
                "System Volume Information/x.jpg", "#recycle/y.jpg", "ok/real.jpg"]:
        make_image(tmp_path / rel, "red")
    assert [p.name for p in media.scan_folder(tmp_path)] == ["real.jpg"]


def test_video_frames_are_spread_out_even_with_one_keyframe(tmp_path):
    av = pytest.importorskip("av")
    path = tmp_path / "longgop.mp4"
    with av.open(str(path), "w") as c:
        s = c.add_stream("mpeg4", rate=24)
        s.width = s.height = 64
        s.pix_fmt = "yuv420p"
        s.codec_context.gop_size = 1000
        for i in range(240):
            frame = av.VideoFrame.from_ndarray(np.full((64, 64, 3), [i, 0, 0], np.uint8), format="rgb24")
            for p in s.encode(frame):
                c.mux(p)
        for p in s.encode():
            c.mux(p)
    frames, _ = media.sample_video_frames(path, 8)
    reds = [np.asarray(f)[..., 0].mean() for f in frames]
    assert len(frames) == 8 and len({round(r) for r in reds}) == 8
    assert reds == sorted(reds) and reds[-1] - reds[0] > 150


def test_audio_only_file_gives_clear_error(tmp_path):
    av = pytest.importorskip("av")
    path = tmp_path / "voice.mp4"
    with av.open(str(path), "w") as c:
        s = c.add_stream("aac", rate=8000)
        for _ in range(5):
            frame = av.AudioFrame.from_ndarray(np.zeros((1, 1024), np.float32), format="fltp", layout="mono")
            frame.sample_rate = 8000
            for p in s.encode(frame):
                c.mux(p)
        for p in s.encode():
            c.mux(p)
    with pytest.raises(ValueError, match="沒有影像"):
        media.sample_video_frames(path, 4)


def test_16bit_and_transparent_images_convert_correctly(tmp_path):
    arr = np.full((32, 32), 30000, np.uint16)
    Image.fromarray(arr).save(tmp_path / "g16.png")
    img, _ = media.load_image(tmp_path / "g16.png", 640)
    assert 100 < np.asarray(img).mean() < 140  # 不再變成全白
    Image.new("RGBA", (20, 20), (0, 0, 0, 0)).save(tmp_path / "t.png")
    img, _ = media.load_image(tmp_path / "t.png", 640)
    assert img.getpixel((0, 0)) == (255, 255, 255)  # 透明背景鋪白色，不是黑色


def test_truncated_jpeg_and_huge_images_still_load(tmp_path):
    make_image(tmp_path / "full.jpg", "red", size=(400, 300))
    data = (tmp_path / "full.jpg").read_bytes()
    (tmp_path / "cut.jpg").write_bytes(data[: len(data) // 2])
    img, _ = media.load_image(tmp_path / "cut.jpg", 640)
    assert img.size == (400, 300)
    assert Image.MAX_IMAGE_PIXELS >= 250_000_000  # 200MP 手機照片不會被當成「解壓縮炸彈」


def test_bogus_exif_date_falls_back_to_other_tags(tmp_path):
    img = Image.new("RGB", (10, 10))
    exif = Image.Exif()
    exif.get_ifd(0x8769)[36867] = "0000:00:00 00:00:00"
    exif[306] = "2021-05-06 07:08"
    img.save(tmp_path / "a.jpg", exif=exif)
    _, date = media.load_image(tmp_path / "a.jpg")
    assert date is not None and (date.year, date.month, date.day, date.hour) == (2021, 5, 6, 7)


def test_long_images_are_split_into_square_crops():
    crops = square_crops(Image.new("RGB", (1080, 2400)))
    assert len(crops) == 3 and all(c.size == (1080, 1080) for c in crops)
    assert square_crops(Image.new("RGB", (800, 600))) == square_crops(Image.new("RGB", (800, 600)))[:1]


def _analyzed(tmp_path):
    paths = [make_image(tmp_path / f"{c}.png", rgb) for c, rgb in
             [("r", (250, 0, 0)), ("g", (0, 250, 0)), ("b", (0, 0, 250))]]
    return analyze(paths, FakeClassifier(), CATS)


def test_scores_follow_names_after_reorder_or_delete(tmp_path):
    items = _analyzed(tmp_path)
    reordered = [CATS[2], CATS[0], CATS[1]]
    assert [it.best(reordered)[0] for it in items] == ["紅", "綠", "藍"]
    without_blue = CATS[:2]
    assert items[2].best(without_blue)[0] in {"紅", "綠"}  # 不會 IndexError
    assert accept_confident(items, reordered, 0.5) == 3
    assert [it.chosen for it in items] == ["紅", "綠", "藍"]


def test_low_similarity_is_flagged_and_not_auto_accepted(tmp_path):
    items = _analyzed(tmp_path)
    items[0].top_similarity = 0.05  # 跟每個分類都不像
    assert items[0].is_low(CATS, threshold=0.5, similarity_floor=0.18)
    assert accept_confident(items, CATS, 0.5, similarity_floor=0.18) == 2
    assert items[0].status == PENDING


def test_merge_keeps_user_decisions(tmp_path):
    placeholder = Item(tmp_path / "a.png", "image", status=SKIPPED)
    result = _analyzed(tmp_path)[0]
    placeholder.merge_analysis(result)
    assert placeholder.status == SKIPPED and placeholder.analyzed


def test_nan_embedding_becomes_error(tmp_path):
    class NaNClassifier(FakeClassifier):
        def encode_prepared(self, tensors):
            return np.full((len(tensors), 3), np.nan, np.float32)

    items = analyze([make_image(tmp_path / "a.png", "red")], NaNClassifier(), CATS)
    assert items[0].status == ERROR and "NaN" in items[0].error


def test_rescore_is_vectorized_and_reports_reverted(tmp_path):
    items = _analyzed(tmp_path)
    items[2].status, items[2].chosen = CONFIRMED, "藍"
    assert rescore(items, FakeClassifier(), CATS[:2]) == 1
    assert items[0].probs.shape == (2,) and items[0].prob_names == ("紅", "綠")


def test_stop_is_responsive_with_slow_loads(tmp_path, monkeypatch):
    import media_sorter.analysis as analysis

    real = analysis.load_media
    monkeypatch.setattr(analysis, "load_media", lambda *a, **k: (time.sleep(0.5), real(*a, **k))[1])
    paths = [make_image(tmp_path / f"{i}.png", "red") for i in range(40)]
    stop = threading.Event()
    threading.Timer(0.3, stop.set).start()
    start = time.time()
    analyze(paths, FakeClassifier(), CATS, batch_size=4, stop_event=stop, workers=2)
    assert time.time() - start < 2.0


def test_auto_model_needs_enough_ram():
    assert resolve_preset("auto", "cuda", 12, ram_gb=32) == "accurate"
    assert resolve_preset("auto", "cuda", 12, ram_gb=8) == "standard"
    assert resolve_preset("auto", "cpu", None, ram_gb=64) == "standard"


def test_encode_learns_batch_size_after_oom():
    torch = pytest.importorskip("torch")
    from media_sorter.classifier import Classifier

    class Model:
        calls: list = []

        def encode_image(self, batch):
            self.calls.append(len(batch))
            if len(batch) > 2:
                raise torch.cuda.OutOfMemoryError("CUDA out of memory")
            return torch.ones(len(batch), 4)

    clf = Classifier.__new__(Classifier)
    clf._torch, clf.device, clf.dtype, clf.model = torch, "cpu", torch.float32, Model()
    clf._max_batch, clf.fell_back_to_fp32 = None, False
    out = clf.encode_prepared([torch.zeros(3, 2, 2)] * 8)
    assert out.shape == (8, 4) and clf._max_batch == 2
    Model.calls.clear()
    clf.encode_prepared([torch.zeros(3, 2, 2)] * 8)
    assert max(Model.calls) <= 2  # 之後直接用學到的安全大小


def test_nan_from_fp16_falls_back_to_fp32():
    torch = pytest.importorskip("torch")
    from media_sorter.classifier import Classifier

    class Model:
        dtype = torch.float16

        def float(self):
            self.dtype = torch.float32

        def encode_image(self, batch):
            value = float("nan") if self.dtype == torch.float16 else 1.0
            return torch.full((len(batch), 4), value)

    clf = Classifier.__new__(Classifier)
    clf._torch, clf.device, clf.dtype, clf.model = torch, "cpu", torch.float16, Model()
    clf._max_batch, clf.fell_back_to_fp32, clf._vocab_embeddings = None, False, None
    out = clf.encode_prepared([torch.zeros(3, 2, 2)] * 2)
    assert np.isfinite(out).all() and clf.fell_back_to_fp32
