"""產生安裝程式與捷徑用的圖示 app.ico（簡單的原創圖形，不使用第三方素材）。python installer/make_icon.py"""

from pathlib import Path

from PIL import Image, ImageDraw


def icon(size: int) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    s = size
    d.rounded_rectangle((0, 0, s - 1, s - 1), radius=s // 5, fill=(15, 108, 189, 255))
    for dx, dy, col in ((0.18, 0.28, (219, 234, 254, 255)), (0.28, 0.22, (147, 197, 253, 255)),
                        (0.38, 0.16, (255, 255, 255, 255))):  # 三張疊起來的照片
        x0, y0 = s * dx, s * dy
        d.rounded_rectangle((x0, y0, x0 + s * 0.44, y0 + s * 0.44), radius=s * 0.05, fill=col)
    x0, y0 = s * 0.38, s * 0.16  # 最上面那張：一座山與太陽
    d.polygon([(x0 + s * 0.04, y0 + s * 0.40), (x0 + s * 0.17, y0 + s * 0.20), (x0 + s * 0.27, y0 + s * 0.32),
               (x0 + s * 0.33, y0 + s * 0.25), (x0 + s * 0.41, y0 + s * 0.40)], fill=(15, 108, 189, 255))
    d.ellipse((x0 + s * 0.28, y0 + s * 0.06, x0 + s * 0.37, y0 + s * 0.15), fill=(245, 158, 11, 255))
    d.line([(s * 0.30, s * 0.80), (s * 0.44, s * 0.90), (s * 0.72, s * 0.66)], fill=(255, 255, 255, 255),
           width=max(2, s // 14), joint="curve")  # 勾勾＝確認
    return img


if __name__ == "__main__":
    out = Path(__file__).with_name("app.ico")
    icon(256).save(out, sizes=[(n, n) for n in (16, 24, 32, 48, 64, 128, 256)])
    print("wrote", out)
