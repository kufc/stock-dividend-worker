"""用「真正的模型」檢查分類結果（要下載模型，所以不屬於自動測試，pytest 不會收集這個檔案）。

    pip install scikit-image scikit-learn
    python tests/manual/real_model_check.py [--preset standard] [--out 結果.txt]

用 scikit-image／scikit-learn 內附的真實照片，加上幾張合成的聊天截圖、收據、海報，
走跟程式一樣的辨識流程，印出每張圖的前三名、相似度，以及幾種「分類描述寫法」的比較。
用途：調整預設分類的描述、「需檢查」的門檻（SIMILARITY_FLOOR、margin）時，有真實數字可以看。
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from media_sorter.analysis import analyze  # noqa: E402
from media_sorter.classifier import SIMILARITY_FLOOR, normalize, score  # noqa: E402
from media_sorter.config import DEFAULT_CATEGORIES, Category  # noqa: E402

NONE = "（都不像）"
# 使用者實際在用的分類清單（沒有「貓」「合照」，多了一個沒有描述的「廣告」）
USER_CATEGORIES = ["人像", "狗", "風景", "城市建築", "美食", "花草植物", "交通工具", "螢幕截圖", "文件收據", "動漫插畫", "廣告"]
# 檔名 → 預期分類（預設分類清單）；NONE 代表不屬於任何預設分類
EXPECTED = {
    "astronaut.png": "人像", "camera.png": "人像", "chelsea.png": "貓", "coffee.png": "美食",
    "rocket.jpg": "交通工具", "motorcycle_left.png": "交通工具", "page.png": "文件收據", "text.png": "文件收據",
    "china.jpg": "城市建築", "flower.jpg": "花草植物", "chat.png": "螢幕截圖", "settings.png": "螢幕截圖",
    "receipt.png": "文件收據", "cartoon.png": "動漫插畫", "poster.png": NONE, "meme.png": NONE,
    "brick.png": NONE, "gravel.png": NONE, "grass.png": "花草植物", "moon.png": NONE, "coins.png": NONE,
    "hubble_deep_field.jpg": NONE, "horse.png": NONE, "video_coffee.mp4": "美食", "video_mixed.mp4": NONE,
}
EXPECTED_USER = {**EXPECTED, "chelsea.png": NONE, "poster.png": "廣告"}


def _font(size: int):
    for name in ("msjh.ttc", "NotoSansCJK-Regular.ttc", "wqy-zenhei.ttc", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def synth_chat(path: Path) -> None:
    """LINE 風格的聊天截圖。"""
    img = Image.new("RGB", (540, 1100), (140, 171, 216))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, 540, 70], fill=(40, 50, 70))
    d.text((20, 20), "< 家族群組 (5)", fill="white", font=_font(26))
    y = 100
    for i, text in enumerate(["今天晚上吃什麼？", "火鍋好了", "幾點集合", "六點半在門口等", "OK 收到", "記得帶傘", "好的～", "等等見"]):
        mine = i % 3 == 1
        w = 40 + 26 * len(text)
        x0 = 540 - w - 20 if mine else 70
        if not mine:
            d.ellipse([15, y, 60, y + 45], fill=(230, 200, 160))
        d.rounded_rectangle([x0, y, x0 + w, y + 56], 22, fill=(133, 229, 91) if mine else "white")
        d.text((x0 + 18, y + 12), text, fill="black", font=_font(24))
        y += 110
    d.rectangle([0, 1030, 540, 1100], fill="white")
    d.text((70, 1050), "輸入訊息", fill=(160, 160, 160), font=_font(22))
    img.save(path)


def synth_settings(path: Path) -> None:
    """手機設定畫面的截圖。"""
    img = Image.new("RGB", (540, 960), (242, 242, 247))
    d = ImageDraw.Draw(img)
    d.text((20, 30), "設定", fill="black", font=_font(40))
    for i, text in enumerate(["Wi-Fi", "藍牙", "行動網路", "通知", "聲音與觸覺回饋", "螢幕顯示與亮度", "電池", "隱私權與安全性"]):
        y = 120 + i * 90
        d.rectangle([0, y, 540, y + 80], fill="white")
        d.rounded_rectangle([20, y + 18, 64, y + 62], 10, fill=[(0, 122, 255), (52, 199, 89), (255, 149, 0)][i % 3])
        d.text((84, y + 24), text, fill="black", font=_font(26))
        d.text((500, y + 24), ">", fill=(180, 180, 180), font=_font(26))
    img.save(path)


def synth_receipt(path: Path) -> None:
    img = Image.new("RGB", (480, 900), (250, 250, 245))
    d = ImageDraw.Draw(img)
    d.text((120, 30), "電子發票證明聯", fill="black", font=_font(32))
    d.text((150, 80), "AB-12345678", fill="black", font=_font(30))
    for i, (name, price) in enumerate([("鮮乳 936ml", 92), ("吐司", 45), ("茶葉蛋 x2", 26), ("衛生紙", 129), ("咖啡", 55)]):
        d.text((40, 180 + i * 60), name, fill="black", font=_font(24))
        d.text((360, 180 + i * 60), f"{price} TX", fill="black", font=_font(24))
    d.line([30, 500, 450, 500], fill="black", width=2)
    d.text((40, 520), "總計", fill="black", font=_font(28))
    d.text((340, 520), "$347", fill="black", font=_font(28))
    for x in range(60, 420, 9):
        d.rectangle([x, 640, x + (x % 4) + 1, 760], fill="black")
    img.save(path)


def synth_cartoon(path: Path) -> None:
    """扁平色塊的卡通臉（貼圖風格）。"""
    img = Image.new("RGB", (600, 600), (255, 236, 170))
    d = ImageDraw.Draw(img)
    d.ellipse([110, 110, 490, 490], fill=(255, 255, 255), outline="black", width=8)
    for x in (220, 380):
        d.ellipse([x - 28, 240, x + 28, 310], fill="black")
        d.ellipse([x - 10, 252, x + 6, 272], fill="white")
    d.arc([230, 300, 370, 410], 20, 160, fill="black", width=8)
    d.ellipse([150, 320, 210, 360], fill=(255, 170, 170))
    d.ellipse([390, 320, 450, 360], fill=(255, 170, 170))
    d.polygon([(150, 150), (190, 40), (260, 120)], fill="white", outline="black")
    d.polygon([(450, 150), (410, 40), (340, 120)], fill="white", outline="black")
    img.save(path)


def synth_poster(path: Path) -> None:
    img = Image.new("RGB", (600, 800), (220, 30, 40))
    d = ImageDraw.Draw(img)
    d.text((60, 60), "週年慶", fill="yellow", font=_font(110))
    d.text((60, 220), "全館 5 折起", fill="white", font=_font(80))
    d.rounded_rectangle([60, 380, 540, 520], 30, fill="yellow")
    d.text((90, 410), "SALE 50% OFF", fill=(220, 30, 40), font=_font(64))
    d.text((60, 600), "活動期間 11/1 - 11/30", fill="white", font=_font(36))
    d.text((60, 680), "滿千送百　買一送一", fill="white", font=_font(40))
    img.save(path)


def synth_meme(photo: Image.Image, path: Path) -> None:
    img = photo.convert("RGB").resize((600, 600))
    d = ImageDraw.Draw(img)
    for dx in (-3, 3):
        for dy in (-3, 3):
            d.text((40 + dx, 20 + dy), "星期一的我", fill="black", font=_font(64))
            d.text((40 + dx, 500 + dy), "不想上班", fill="black", font=_font(64))
    d.text((40, 20), "星期一的我", fill="white", font=_font(64))
    d.text((40, 500), "不想上班", fill="white", font=_font(64))
    img.save(path)


def make_video(frames: list[Image.Image], path: Path) -> None:
    import av

    with av.open(str(path), "w") as container:
        stream = container.add_stream("mpeg4", rate=12)
        stream.width, stream.height, stream.pix_fmt = 320, 240, "yuv420p"
        for frame in frames:
            for _ in range(12):
                for packet in stream.encode(av.VideoFrame.from_image(frame.convert("RGB").resize((320, 240)))):
                    container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)


def build_images(folder: Path) -> list[Path]:
    import skimage
    import sklearn

    paths = []
    sources = [(Path(skimage.__file__).parent / "data", [n for n in EXPECTED if n not in ("china.jpg", "flower.jpg")]),
               (Path(sklearn.__file__).parent / "datasets" / "images", ["china.jpg", "flower.jpg"])]
    for base, names in sources:
        for name in names:
            if (base / name).is_file():
                target = folder / name
                Image.open(base / name).convert("RGB").save(target)
                paths.append(target)
    for name, func in (("chat.png", synth_chat), ("settings.png", synth_settings), ("receipt.png", synth_receipt),
                       ("cartoon.png", synth_cartoon), ("poster.png", synth_poster)):
        func(folder / name)
        paths.append(folder / name)
    if (folder / "chelsea.png").exists():
        synth_meme(Image.open(folder / "chelsea.png"), folder / "meme.png")
        paths.append(folder / "meme.png")
    try:
        coffee, astronaut = Image.open(folder / "coffee.png"), Image.open(folder / "astronaut.png")
        make_video([coffee, coffee.rotate(8), coffee.crop((60, 40, 560, 380))], folder / "video_coffee.mp4")
        make_video([coffee, astronaut, Image.open(folder / "china.jpg")], folder / "video_mixed.mp4")
        paths += [folder / "video_coffee.mp4", folder / "video_mixed.mp4"]
    except Exception as exc:  # noqa: BLE001 - 沒有 av 或缺圖就不測影片
        print(f"（略過影片：{exc}）")
    return paths


# ---------------------------------------------------------------- 分類描述的幾種寫法
def texts_current(c: Category) -> list[str]:
    return c.texts()


def texts_no_bare_name(c: Category) -> list[str]:
    texts = [p.strip() for p in c.prompts if p.strip()]
    return list(dict.fromkeys(texts + [f"一張{c.name}的照片", f"{c.name}的圖片"]))


def texts_templates(c: Category) -> list[str]:
    out = []
    for phrase in [c.name] + [p.strip() for p in c.prompts if p.strip()]:
        ascii_only = all(ord(ch) < 128 for ch in phrase)
        out += [phrase, f"a photo of {phrase}" if ascii_only and not phrase.startswith("a ") else f"一張{phrase}的照片"]
    return list(dict.fromkeys(out))


def texts_without_hint(c: Category) -> list[str]:
    """不自動補英文說明（舊的做法）：只有使用者寫的說明、名稱、「一張⋯的照片」。"""
    texts = [p.strip() for p in c.prompts if p.strip()]
    return list(dict.fromkeys(texts + [c.name, f"一張{c.name}的照片"]))


def texts_english_only(c: Category) -> list[str]:
    """只用英文說明（沒有英文可用時維持目前的寫法）。"""
    english = [t for t in c.texts() if all(ord(ch) < 128 for ch in t)]
    return english or c.texts()


VARIANTS = {"目前的寫法": texts_current, "不自動補英文說明": texts_without_hint, "不放單獨的名稱": texts_no_bare_name,
            "每句都套樣板": texts_templates, "只用英文說明": texts_english_only}


def is_low(probs: np.ndarray, top_similarity: float, threshold: float = 0.5, margin: bool = False) -> bool:
    order = np.sort(probs)[::-1]
    best, second = float(order[0]), float(order[1]) if len(order) > 1 else 0.0
    if top_similarity < SIMILARITY_FLOOR:
        return True
    return best < threshold and (best - second) < threshold / 2 if margin else best < threshold


def report(title: str, items, categories: list[Category], clf, expected: dict, out) -> None:
    names = [c.name for c in categories]
    emb = np.stack([it.embedding for it in items])
    print(f"\n{'=' * 78}\n{title}（{len(categories)} 個分類）\n{'=' * 78}", file=out)
    for label, build in VARIANTS.items():
        class_emb = clf._encode_groups([build(c) for c in categories])
        probs = score(emb, class_emb, clf.logit_scale)
        sims = emb @ class_emb.T
        right = wrong = none_flagged = none_total = low_old = low_new = 0
        print(f"\n--- {label} ---", file=out)
        for row, item in enumerate(items):
            order = np.argsort(-probs[row])[:3]
            top = "、".join(f"{names[i]} {probs[row, i]:.0%}" for i in order)
            top_sim = float(sims[row].max())
            want = expected.get(item.path.name, "?")
            got = names[order[0]]
            low = is_low(probs[row], top_sim)
            low_old += is_low(probs[row], top_sim, margin=True)
            low_new += low
            if want == NONE:
                none_total += 1
                none_flagged += low
                mark = "需檢查✓" if low else "沒標出"
            else:
                right += got == want
                wrong += got != want
                mark = ("對" if got == want else "錯") + ("（需檢查）" if low else "")
            print(f"{item.path.name:24s} 預期 {want:6s} → {top:42s} 相似度 {top_sim:.3f}  {mark}", file=out)
        print(f"[小計] 有答案的：對 {right}／錯 {wrong}；不屬於任何分類的 {none_total} 張中標成需檢查 {none_flagged} 張；"
              f"需檢查總數：只看門檻（程式的規則）{low_new}、加上領先幅度條件 {low_old}（共 {len(items)} 張）", file=out)


def video_scoring(items, paths, categories, clf, out) -> None:
    """影片：比較「畫面向量平均後再比對」（目前）跟「每張畫面各自比對後平均機率」。"""
    from media_sorter.media import sample_video_frames

    names = [c.name for c in categories]
    class_emb = clf.encode_categories(categories)
    print(f"\n{'=' * 78}\n影片的兩種算法\n{'=' * 78}", file=out)
    for path in paths:
        if path.suffix != ".mp4":
            continue
        frames, _ = sample_video_frames(path, 8)
        frame_emb = clf.encode_images(frames)
        mean_emb = normalize(frame_emb.mean(axis=0))
        a = score(mean_emb[None, :], class_emb, clf.logit_scale)[0]
        b = score(frame_emb, class_emb, clf.logit_scale).mean(axis=0)
        fmt = lambda p: "、".join(f"{names[i]} {p[i]:.0%}" for i in np.argsort(-p)[:3])  # noqa: E731
        print(f"{path.name:20s} 向量平均：{fmt(a)}\n{'':20s} 機率平均：{fmt(b)}", file=out)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--preset", default="standard")
    parser.add_argument("--out", default="")
    parser.add_argument("--fake", action="store_true", help="用測試的假模型跑（只檢查這支程式本身）")
    args = parser.parse_args()
    out = open(args.out, "w", encoding="utf-8") if args.out else sys.stdout
    folder = Path(tempfile.mkdtemp(prefix="real_model_check_"))
    paths = build_images(folder)
    if args.fake:
        sys.path.insert(0, str(ROOT / "tests"))
        from fakes import FakeClassifier

        clf = FakeClassifier()
        clf._encode_groups = lambda groups: normalize(np.random.default_rng(0).random((len(groups), 3)))
        clf.encode_images = lambda images: clf.encode_prepared([clf.prepare(i) for i in images])
    else:
        from media_sorter.classifier import Classifier

        clf = Classifier(args.preset)
        print(f"模型：{clf.model_name}　裝置：{clf.device_description}　logit_scale={clf.logit_scale:.1f}　"
              f"SIMILARITY_FLOOR={SIMILARITY_FLOOR}", file=out)
    defaults = [Category(c.name, list(c.prompts)) for c in DEFAULT_CATEGORIES]
    items = [it for it in analyze(paths, clf, defaults) if it.embedding is not None]
    failed = sorted(set(p.name for p in paths) - {it.path.name for it in items})
    if failed:
        print(f"讀取失敗：{failed}", file=out)
    report("預設分類", items, defaults, clf, EXPECTED, out)
    by_name = {c.name: c for c in defaults}
    user = [Category(n, list(by_name[n].prompts) if n in by_name else []) for n in USER_CATEGORIES]
    report("使用者的分類清單（沒有貓／合照，多了沒有描述的「廣告」）", items, user, clf, EXPECTED_USER, out)
    print(f"\n{'=' * 78}\nAI 還看到（內建詞彙的前 5 名）\n{'=' * 78}", file=out)
    for it in items:
        print(f"{it.path.name:24s} " + "、".join(f"{n} {p:.0%}" for n, p in it.tags), file=out)
    if not args.fake:
        video_scoring(items, paths, defaults, clf, out)
    if args.out:
        out.close()
        print(f"結果已寫到 {args.out}")


if __name__ == "__main__":
    main()
