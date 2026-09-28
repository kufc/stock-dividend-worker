from datetime import datetime

import pytest
from PIL import Image

from helpers import make_image, make_video
from media_sorter.media import load_image, load_media, load_preview, media_kind, sample_video_frames, scan_folder


def test_scan_folder_filters_and_excludes(tmp_path):
    make_image(tmp_path / "a.jpg", "red")
    make_image(tmp_path / "sub" / "b.PNG", "red")
    make_image(tmp_path / "已分類" / "c.jpg", "red")
    (tmp_path / "notes.txt").write_text("x")
    (tmp_path / "clip.MP4").write_bytes(b"")

    names = [p.name for p in scan_folder(tmp_path, recursive=True, exclude=tmp_path / "已分類")]
    assert names == ["a.jpg", "clip.MP4", "b.PNG"]
    assert [p.name for p in scan_folder(tmp_path, recursive=False)] == ["a.jpg", "clip.MP4"]


def test_media_kind():
    from pathlib import Path

    assert media_kind(Path("x.HEIC")) == "image"
    assert media_kind(Path("x.mov")) == "video"
    assert media_kind(Path("x.txt")) is None


def test_load_image_reads_exif_date_and_orientation(tmp_path):
    path = tmp_path / "photo.jpg"
    img = Image.new("RGB", (80, 40), "blue")
    exif = Image.Exif()
    exif[0x0112] = 6  # 需要旋轉 90 度
    exif.get_ifd(0x8769)[36867] = "2023:07:15 10:20:30"
    img.save(path, exif=exif)

    loaded, date = load_image(path)
    assert loaded.size == (40, 80)
    assert date == datetime(2023, 7, 15, 10, 20, 30)


def test_load_media_falls_back_to_file_date(tmp_path):
    path = make_image(tmp_path / "a.png", "green")
    media = load_media(path, "image")
    assert len(media.frames) == 1 and media.thumbnail is None
    assert abs((media.date - datetime.now()).total_seconds()) < 120


def test_sample_video_frames(tmp_path):
    path = make_video(tmp_path / "v.mp4", (0, 0, 255), frames=72)
    frames, _ = sample_video_frames(path, 6)
    assert 1 <= len(frames) <= 6
    r, g, b = frames[0].convert("RGB").resize((1, 1)).getpixel((0, 0))
    assert b > 150 and r < 80

    media = load_media(path, "video", video_frames=4)
    assert media.thumbnail
    assert load_preview(path, "video", media.thumbnail).size[0] > 0


def test_broken_video_raises(tmp_path):
    import av

    path = tmp_path / "broken.mp4"
    path.write_bytes(b"not a video")
    with pytest.raises(av.error.FFmpegError):
        sample_video_frames(path, 4)
