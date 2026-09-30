"""產生安裝程式用的圖片（簡單的原創圖形，不使用第三方素材）。python installer/make_icon.py

- app.ico：程式與捷徑的圖示
- wizard.bmp：安裝精靈歡迎頁／完成頁左側的圖（164×314）
- header.bmp：安裝精靈每一頁右上角的小圖（150×57）
"""

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


BLUE = (15, 108, 189)


def wizard() -> Image.Image:
    """歡迎頁左側：由上到下的藍色漸層，中間放圖示，下方幾條淡色的「照片卡片」。"""
    w, h = 164, 314
    img = Image.new("RGB", (w, h))
    d = ImageDraw.Draw(img)
    for y in range(h):
        t = y / (h - 1)
        d.line([(0, y), (w, y)], fill=(int(15 + 20 * t), int(108 - 50 * t), int(189 - 60 * t)))
    mark = icon(96)
    img.paste(mark, ((w - 96) // 2, 70), mark)
    for i in range(3):
        y = 200 + i * 26
        d.rounded_rectangle((28, y, w - 28, y + 16), radius=5, fill=(255, 255, 255) if i == 0 else (147, 197, 253))
    return img


def header() -> Image.Image:
    """每一頁右上角：白底、靠右放小圖示（與頁首的白色背景融合）。"""
    img = Image.new("RGB", (150, 57), (255, 255, 255))
    mark = icon(40)
    img.paste(mark, (150 - 40 - 10, (57 - 40) // 2), mark)
    return img


if __name__ == "__main__":
    here = Path(__file__).parent
    icon(256).save(here / "app.ico", sizes=[(n, n) for n in (16, 24, 32, 48, 64, 128, 256)])
    wizard().save(here / "wizard.bmp")
    header().save(here / "header.bmp")
    print("wrote app.ico, wizard.bmp, header.bmp")
