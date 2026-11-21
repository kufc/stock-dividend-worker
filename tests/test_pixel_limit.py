"""第九輪外部審查（P1）：3 億像素要是「硬上限」。

Pillow 的 MAX_IMAGE_PIXELS 超過只會警告，要超過兩倍才丟錯誤；原本的程式只依賴它，
所以 3～6 億像素的圖其實會被整張解開。現在每一條讀圖路徑都在解碼前檢查寬×高。
"""

import io
import warnings

import pytest
from PIL import Image, ImageFile

from helpers import make_image, make_video
from media_sorter import analysis, media
from fakes import FakeClassifier
from media_sorter.config import Category


@pytest.fixture
def small_limit(monkeypatch):
    """把上限調成 1000 像素。40×30＝1200 落在「1 倍～2 倍」之間：Pillow 自己只會警告、照樣解碼。"""
    monkeypatch.setattr(media.Image, "MAX_IMAGE_PIXELS", 1000)
    return 1000


@pytest.fixture
def no_decoding(monkeypatch):
    """任何解碼像素的動作都算失敗：證明是「解碼前」就拒絕。"""
    def forbidden(self, *args, **kwargs):
        raise AssertionError("不應該解碼")

    monkeypatch.setattr(ImageFile.ImageFile, "load", forbidden)


def test_product_limit_is_300_million_and_matches_pillow_setting():
    assert media.MAX_PIXELS == 300_000_000 == Image.MAX_IMAGE_PIXELS == media.pixel_limit()


def test_pillow_alone_would_have_decoded_an_image_between_one_and_two_times_the_limit(tmp_path, small_limit):
    """重現原本的問題：只靠 Pillow 的設定，1.2 倍上限的圖只有警告，照樣開得起來。"""
    path = make_image(tmp_path / "big.png", (1, 2, 3), size=(40, 30))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", Image.DecompressionBombWarning)
        with Image.open(path) as img:
            img.load()
            assert img.size == (40, 30)


@pytest.mark.parametrize("size,ok", [((25, 40), True), ((40, 30), False), ((1001, 1), False), ((100, 100), False)])
def test_limit_is_exact_and_checked_before_decoding(tmp_path, small_limit, no_decoding, size, ok):
    path = make_image(tmp_path / "x.png", (1, 2, 3), size=size)
    if ok:  # 剛好等於上限：通過檢查（接下來才會解碼，被 no_decoding 攔下）
        with pytest.raises(AssertionError, match="不應該解碼"):
            media.load_image(path)
        return
    with pytest.raises(media.TooLargeError, match="圖片太大.*上限"):
        media.load_image(path)


@pytest.mark.parametrize("suffix", [".png", ".jpg", ".bmp", ".gif", ".webp", ".tif"])
def test_every_image_route_rejects_oversized_files(tmp_path, small_limit, no_decoding, suffix):
    path = make_image(tmp_path / f"big{suffix}", (9, 9, 9), size=(40, 30))
    routes = {
        "辨識用的讀圖": lambda: media.load_image(path, max_side=media.ANALYSIS_SIZE),
        "辨識（load_media）": lambda: media.load_media(path, "image"),
        "預覽": lambda: media.load_preview(path, "image", None),
        "放大檢視": lambda: media.load_preview(path, "image", None, size=2400),
    }
    for name, route in routes.items():
        with pytest.raises(media.TooLargeError):
            route()
            pytest.fail(f"{name} 沒有拒絕")


def test_thumbnail_cache_goes_through_the_same_check(small_limit, no_decoding):
    buf = io.BytesIO()
    with pytest.MonkeyPatch.context() as mp:  # 存檔時暫時不限制
        mp.setattr(ImageFile.ImageFile, "load", ImageFile.ImageFile.load)
        Image.new("RGB", (40, 30)).save(buf, format="JPEG")
    with pytest.raises(media.TooLargeError):
        media.open_thumbnail(buf.getvalue())
    with pytest.raises(media.TooLargeError):
        media.load_preview(None, "image", buf.getvalue())


def test_oversized_video_frames_are_rejected_before_decoding(tmp_path, small_limit, monkeypatch):
    pytest.importorskip("av")
    path = make_video(tmp_path / "v.mp4", (200, 10, 10), frames=24, size=(64, 64))  # 4096 像素
    monkeypatch.setattr(media, "_frame_to_image", lambda *a, **k: pytest.fail("不應該解碼任何畫格"))
    for route in (lambda: media.sample_video_frames(path, 4), lambda: media.load_media(path, "video"),
                  lambda: media.load_preview(path, "video", None)):
        with pytest.raises(media.TooLargeError, match="影片畫面太大"):
            route()


def test_frame_larger_than_the_header_said_is_rejected(small_limit):
    class Frame:
        width, height = 64, 64

    with pytest.raises(media.TooLargeError):
        media._frame_to_image(Frame(), 640)


def test_analysis_reports_oversized_files_as_unreadable_and_continues(tmp_path, small_limit):
    big = make_image(tmp_path / "big.png", (250, 0, 0), size=(40, 30))
    ok = make_image(tmp_path / "ok.png", (250, 0, 0), size=(20, 20))
    items = analysis.analyze([big, ok], FakeClassifier(), [Category("紅"), Category("綠")], workers=2)
    assert items[0].status == analysis.ERROR and "圖片太大" in items[0].error
    assert items[1].analyzed


def test_no_decompression_warning_leaks_out(tmp_path, small_limit, recwarn):
    path = make_image(tmp_path / "big.png", (1, 2, 3), size=(40, 30))
    with pytest.raises(media.TooLargeError):
        media.load_image(path)
    assert not [w for w in recwarn if issubclass(w.category, Image.DecompressionBombWarning)]


def test_zoomed_view_never_renders_more_than_its_pixel_budget():
    from media_sorter import dialogs

    assert dialogs.ZOOM_MAX_PIXELS <= media.MAX_PIXELS
    w = h = 2400
    scale = 8.0
    if w * h * scale * scale > dialogs.ZOOM_MAX_PIXELS:
        scale = (dialogs.ZOOM_MAX_PIXELS / (w * h)) ** 0.5
    assert w * h * scale * scale <= dialogs.ZOOM_MAX_PIXELS * 1.001
