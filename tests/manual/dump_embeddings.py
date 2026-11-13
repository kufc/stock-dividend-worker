"""把真模型算出來的向量存成 .npz，之後不用模型也能離線試各種判斷規則（不屬於自動測試）。

    pip install scikit-image scikit-learn huggingface_hub
    python tests/manual/dump_embeddings.py --preset standard --out tests/manual/embeddings_standard.npz

圖片來源：real_model_check.py 的那一批（真實照片＋合成的截圖、收據、海報、影片），
再加上從 Hugging Face 下載的 ImageNette 驗證集（10 類真實照片，每類取幾十張）。
ImageNette 裡的狗、教堂、垃圾車屬於預設分類；其他七類（魚、錄音機、電鋸、法國號、加油機、高爾夫球、
降落傘）不屬於任何預設分類，用來量「硬塞進最像的一類」有多嚴重、怎樣的規則抓得出來。

存檔內容：
  image_names, image_labels（預期分類；「（都不像）」代表不屬於任何預設分類）, image_emb
  frame_owner, frame_emb（影片每張取樣畫面的向量；frame_owner 是它屬於哪個 image_names 的位置）
  texts, text_emb（每一句文字各自的向量，離線可以自己組合成分類）, logit_scale, model
"""

from __future__ import annotations

import argparse
import io
import sys
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from prompt_candidates import CANDIDATES  # noqa: E402
from real_model_check import EXPECTED, NONE, build_images  # noqa: E402

from media_sorter.analysis import analyze  # noqa: E402
from media_sorter.config import DEFAULT_CATEGORIES  # noqa: E402
from media_sorter.vocabulary import COMMON_CATEGORIES, EXTRA_HINTS, VOCABULARY, category_hint  # noqa: E402

# ImageNette 的 10 個類別（依 WordNet 編號排序，也就是資料集的 label 0～9）→ 預設分類裡的預期答案
IMAGENETTE = [("tench", NONE), ("english_springer", "狗"), ("cassette_player", NONE), ("chain_saw", NONE),
              ("church", "城市建築"), ("french_horn", NONE), ("garbage_truck", "交通工具"), ("gas_pump", NONE),
              ("golf_ball", NONE), ("parachute", NONE)]
OTHER_TEXTS = ["其他", "其他東西", "雜物", "不屬於以上任何一類", "一張照片", "一張圖片", "other", "something else",
               "a photo", "a photo of something", "an object", "a random object", "miscellaneous"]


def add_imagenette(folder: Path, per_class: int) -> dict[str, str]:
    """下載 ImageNette 驗證集，每類存 per_class 張到 folder。回傳 {檔名: 預期分類}。"""
    import tarfile
    import urllib.request

    from huggingface_hub import hf_hub_download

    # frgfm/imagenette 沒有 parquet 檔：它的載入程式（imagenette.py）是抓 fast.ai 的壓縮檔，
    # 再用 metadata/…/val.txt 挑出驗證集；label 就是 WordNet 編號排序後的位置。這裡照做。
    repo, variant = "frgfm/imagenette", "imagenette2-320"
    with open(hf_hub_download(repo, f"metadata/{variant}/val.txt", repo_type="dataset"), encoding="utf-8") as f:
        keep = {line for line in f.read().split("\n") if line}
    wnids = sorted({name.split("/")[-2] for name in keep})
    if len(wnids) != len(IMAGENETTE):
        raise RuntimeError(f"{repo} 的驗證集類別數不是 {len(IMAGENETTE)}：{wnids}")
    archive = Path(tempfile.gettempdir()) / f"{variant}.tgz"
    if not archive.exists():
        partial = archive.with_suffix(".part")
        urllib.request.urlretrieve(f"https://s3.amazonaws.com/fast-ai-imageclas/{variant}.tgz", partial)
        partial.rename(archive)
    counts = [0] * len(IMAGENETTE)
    expected = {}
    with tarfile.open(archive) as tar:
        for member in tar:
            if member.name not in keep:
                continue
            label = wnids.index(member.name.split("/")[-2])
            if counts[label] >= per_class:
                continue
            name = f"inet_{IMAGENETTE[label][0]}_{counts[label]:03d}.jpg"
            Image.open(io.BytesIO(tar.extractfile(member).read())).convert("RGB").save(folder / name, quality=90)
            expected[name] = IMAGENETTE[label][1]
            counts[label] += 1
            if min(counts) >= per_class:
                break
    preferred = [f"{variant}.tgz"]
    print(f"ImageNette（{preferred[0]}）：每類 {counts}")
    return expected


def all_texts() -> list[str]:
    texts: list[str] = []
    for c in DEFAULT_CATEGORIES:
        texts += c.texts() + [f"a photo of {c.name}", f"{c.name}的圖片"]
    for zh, en in VOCABULARY:
        texts += [zh, en, f"a photo of {en}", f"一張{zh}的照片"]
    for name in list(COMMON_CATEGORIES) + list(EXTRA_HINTS):
        hint = category_hint(name)
        texts += [name, f"一張{name}的照片"] + ([hint, f"a photo of {hint}"] if hint else [])
    texts += OTHER_TEXTS
    for name, candidates in CANDIDATES.items():
        texts += [name, f"一張{name}的照片", f"{name}的圖片"] + candidates
    return list(dict.fromkeys(t for t in texts if t))


def add_extra_dir(extra_dir: Path, folder: Path) -> tuple[dict[str, str], dict[str, str]]:
    """extra_dir 底下每個子資料夾是一組圖片，資料夾名稱就是那一組的名稱：
    名稱是預設分類（例如「美食」）→ 預期答案就是那個分類；其他名稱（例如 none_meme）→ 不屬於任何預設分類。
    回傳 ({檔名: 預期分類}, {檔名: 組名})。"""
    defaults = {c.name for c in DEFAULT_CATEGORIES}
    expected, groups = {}, {}
    for sub in sorted(p for p in extra_dir.iterdir() if p.is_dir()):
        count = 0
        for source in sorted(sub.iterdir()):
            if source.suffix.lower() not in (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"):
                continue
            name = f"x_{sub.name}_{count:03d}.jpg"
            try:
                image = Image.open(source).convert("RGB")
                image.thumbnail((1024, 1024))
                image.save(folder / name, quality=90)
            except Exception as exc:  # noqa: BLE001 - 壞圖略過
                print(f"（略過 {source}：{exc}）")
                continue
            expected[name] = sub.name if sub.name in defaults else NONE
            groups[name] = sub.name
            count += 1
        print(f"額外圖片 {sub.name}：{count} 張")
    return expected, groups


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--preset", default="standard")
    parser.add_argument("--out", required=True)
    parser.add_argument("--per-class", type=int, default=40, help="ImageNette 每類取幾張（0＝不下載）")
    parser.add_argument("--extra-dir", default="", help="額外的圖片：每個子資料夾一組，資料夾名稱＝預期分類或 none_xxx")
    parser.add_argument("--fake", action="store_true", help="用測試的假模型跑（只檢查這支程式本身）")
    args = parser.parse_args()

    folder = Path(tempfile.mkdtemp(prefix="dump_embeddings_"))
    paths = build_images(folder)
    expected = dict(EXPECTED)
    groups: dict[str, str] = {}
    if args.extra_dir:
        extra_expected, groups = add_extra_dir(Path(args.extra_dir), folder)
        expected.update(extra_expected)
        paths += [folder / name for name in sorted(extra_expected)]
    if args.per_class > 0:
        try:
            extra = add_imagenette(folder, args.per_class)
            expected.update(extra)
            paths += [folder / name for name in sorted(extra)]
        except Exception as exc:  # noqa: BLE001 - 下載不到就只用內附的圖片，並把原因印出來
            print(f"（略過 ImageNette：{type(exc).__name__}: {exc}）")
    if args.fake:
        sys.path.insert(0, str(ROOT / "tests"))
        from fakes import FakeClassifier
        from media_sorter.classifier import normalize

        clf = FakeClassifier()
        clf.encode_texts = lambda texts: normalize(np.random.default_rng(0).random((len(texts), 3)))
        clf.model_name = "fake"
    else:
        from media_sorter.classifier import Classifier

        clf = Classifier(args.preset)
    items = [it for it in analyze(paths, clf, list(DEFAULT_CATEGORIES)) if it.embedding is not None]
    texts = all_texts()
    text_emb = clf.encode_texts(texts)
    frame_owner, frame_emb = [], []
    for position, item in enumerate(items):
        if item.frames is not None:
            frame_owner += [position] * len(item.frames)
            frame_emb.append(item.frames)
    dim = text_emb.shape[1]
    np.savez_compressed(
        args.out,
        image_names=np.array([it.path.name for it in items]),
        image_labels=np.array([expected.get(it.path.name, "?") for it in items]),
        image_groups=np.array([groups.get(it.path.name, "") for it in items]),
        image_emb=np.stack([it.embedding for it in items]).astype(np.float16),
        frame_owner=np.array(frame_owner, dtype=np.int32),
        frame_emb=(np.concatenate(frame_emb) if frame_emb else np.zeros((0, dim))).astype(np.float16),
        texts=np.array(texts),
        text_emb=text_emb.astype(np.float16),
        logit_scale=np.float32(clf.logit_scale),
        model=np.array(clf.model_name),
    )
    print(f"已存 {args.out}：{len(items)} 個檔案、{len(texts)} 句文字、向量 {dim} 維")


if __name__ == "__main__":
    main()
