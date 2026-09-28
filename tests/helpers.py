from pathlib import Path

from PIL import Image


def make_image(path: Path, color, size=(64, 48), exif_date: str | None = None) -> Path:
    img = Image.new("RGB", size, color)
    kwargs = {}
    if exif_date:
        exif = Image.Exif()
        exif.get_ifd(0x8769)[36867] = exif_date
        kwargs["exif"] = exif
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, **kwargs)
    return path


def make_video(path: Path, color, frames: int = 48, size=(64, 64), fps: int = 24) -> Path:
    import av

    path.parent.mkdir(parents=True, exist_ok=True)
    with av.open(str(path), "w") as container:
        stream = container.add_stream("mpeg4", rate=fps)
        stream.width, stream.height = size
        stream.pix_fmt = "yuv420p"
        for _ in range(frames):
            frame = av.VideoFrame.from_image(Image.new("RGB", size, color))
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    return path
