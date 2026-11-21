"""Rebuild the independent HOLD-OUT image set described in holdout_sources.txt (not an automated test).

This set was collected AFTER all prompts / thresholds were fixed. It must never be used for tuning;
it is only for a final, one-off evaluation. Nothing in this script runs the classifier.

    pip install huggingface_hub pyarrow Pillow
    python tests/manual/collect_holdout.py --out /opt/holdout/images --sheets /opt/holdout/sheets

What it does
  1. Downloads single shards / files of public Hugging Face datasets (pinned to the commit hashes in REV).
  2. Picks images at random using only dataset labels / metadata, with fixed seeds
     (random.Random(f"{SEED}:{group}:{source}") - string seeds are stable across Python versions).
  3. Saves them as <out>/<group>/NNN.jpg (RGB JPEG, longer side <= 1024) plus <out>/manifest.tsv.
  4. Deletes the files listed in REMOVED (the by-eye cleaning done on contact sheets: only mislabelled,
     unrecognisable, or genuinely ambiguous images; see holdout_sources.txt for the reasons).
     Use --keep-removed to see the raw random selection.

Folder name == a default category -> that is the expected label; none_xxx -> belongs to no default category
(same convention as dump_embeddings.py --extra-dir).
"""

from __future__ import annotations

import argparse
import hashlib
import io
import random
import re
import tarfile
import xml.etree.ElementTree as ET
import zipfile
from collections import defaultdict
from pathlib import Path

SEED = 20261121

# dataset repo -> commit hash used when the set was built
REV = {
    "logasja/lfw": "0ee47979927a48dadf11083cb53b51439fa92dc9",
    "HuggingFaceM4/pascal_voc": "dbafdb9e1506c9c419c5c4672e409463cd21ba50",
    "lmms-lab/flickr30k": "765d117f3eec816f2bfdc2d73ebb50a6f77b86a4",
    "timm/oxford-iiit-pet": "089695c834a7deb60505b7cc506672db1c31a6aa",
    "larrygzlb1994/lhq_256": "ab9afad22fcca3ae55817ff0a2da3411857e0755",
    "sfarrukhm/intel-image-classification": "9746c77ca1603c13fa75a2cbed309a09874d6e47",
    "JamieSJS/r-paris": "bafc3a08fdffd72558021ce3a41250833d7e0e88",
    "JamieSJS/r-oxford": "d8daad98b4e4896a7f7fa1b3485a22420242d4fc",
    "chaeso/food_chinese_2017": "7b722853d985102b6ab6fe1dd7e10473945de4d0",
    "bharat-raghunathan/indian-foods-dataset": "ef509a8b4bfd46b76530ad329f81586faf904aad",
    "Matthijs/snacks": "575dff82356cede71dcf044da908aa7eab528c04",
    "mikehemberger/plantnet300K": "cee4086ba902c7663483de51d5e518be9f5fe26a",
    "creative-graphic-design/Rico": "c91e7d3c3d750ea3caae17573bd1250507966d6b",
    "Writer/omniact": "5e4bcb26745afc8d58ba9519ea9c9fb832386d26",
    "jsdnrs/ICDAR2019-SROIE": "bffe40c26759f3376ec2b3ae9031dbba54cd587c",
    "ds4sd/DocLayNet-v1.1": "5e89392376049f4d589ea339ed64468310ed5c3f",
    "kurumikz/AnimeOmni": "34794e1243d712a41ca5a9dc25b9ff4f591a16e2",
    "puruchinera/anime_wallpapers": "f1f3b0fc4ffebe626edde81772efa8072cb38265",
    "Leonardo6/memecap": "be95fd53069007446510cbb7540bba8d0d64bd98",
    "bentrevett/caltech-ucsd-birds-200-2011": "1ef09e021b0b65b40337f6f285909656f407f6e0",
    "lucabaggi/animal-wildlife": "ae76bc41797a7a0c9089e39704114f39961ba7d8",
    "keremberke/indoor-scene-classification": "030dcb9ec61c436299b1df10d90ae1cbe1d1b401",
    "HES-XPLAIN/SportsImageClassification": "2cdb92b3e2dcb5d1da1137a9ba32529c7db97937",
    "huggan/wikiart": "d559852d2b232e0fcf195e775866964f0564f2b5",
    "tanganke/dtd": "d2afa97d9f335b1a6b3b09c637aef667f98f966e",
    "blanchon/UC_Merced": "b79f9bfb55cd7731306d583c3e072c8c010bac61",
    "keremberke/chest-xray-classification": "15439bd777b2fb82f090c80e12d4da40c06522b4",
    "lmms-lab/ai2d": "c83a9b9692933aff8349157c88a413df9d02c4e5",
    "heshamdahy/stanford-products-images": "60733b2382ac4d8de517b2abc42bd24074cead25",
    "garythung/trashnet": "94cd17ebfd6702bf62281d3c89f22ff649815b46",
}

# By-eye cleaning (contact sheets), applied after the random selection. group -> file names that were deleted.
# Rule: only mislabelled, unrecognisable, or genuinely ambiguous between two default categories. Never "hard".
REMOVED: dict[str, list[str]] = {
    # two equally prominent faces; a skier who is small in a mountain landscape
    "人像": ["015.jpg", "050.jpg"],
    # people tiny in a balloon basket seen from the air; a dark crowd around a fire; three people seen from
    # behind in front of a painting; people from behind looking at a locomotive
    "合照": ["014.jpg", "036.jpg", "045.jpg", "058.jpg"],
    # dog tiny inside an arcade / building; a person walking a small dog at night, both tiny
    "狗": ["043.jpg", "055.jpg"],
    # LHQ is unlabelled: plant close-ups, a blurred trunk, a warehouse ceiling, a tree crown with blossom, a
    # ruin with columns, roof silhouettes, a stone chapel, pebbles; Intel 'sea': jumping dolphins
    "風景": ["015.jpg", "018.jpg", "022.jpg", "023.jpg", "024.jpg", "026.jpg", "030.jpg", "031.jpg", "055.jpg"],
    # Paris/Oxford corpus photos that do not show a building exterior: people, statues and reliefs, paintings,
    # interiors, a lawn, candles, a teddy bear, buses, a view straight down from a tower, a shop sign
    "城市建築": [
        "000.jpg", "001.jpg", "003.jpg", "009.jpg", "010.jpg", "011.jpg", "012.jpg", "013.jpg",
        "014.jpg", "015.jpg", "018.jpg", "019.jpg", "025.jpg", "026.jpg", "027.jpg", "028.jpg",
        "029.jpg", "030.jpg", "031.jpg", "033.jpg", "035.jpg", "038.jpg", "039.jpg", "040.jpg",
    ],
    # two images blurred beyond recognition; grapes on the vine; children eating ice cream (people are the subject)
    "美食": ["006.jpg", "008.jpg", "049.jpg", "051.jpg"],
    # a mural with a bicycle in front; view over a motorbike mirror onto a road; view out of a train window
    # onto tracks
    "交通工具": ["014.jpg", "050.jpg", "054.jpg"],
    # a manual page that is almost entirely a photo of an aircraft
    "文件收據": ["039.jpg"],
    # stop-motion puppets inside a drawing program; an unrecognisable blue ornament; two painted landscapes
    # without characters
    "動漫插畫": ["009.jpg", "015.jpg", "051.jpg", "058.jpg"],
    # oysters served on a plate (food); a person holding a snake
    "none_wildlife": ["031.jpg", "039.jpg"],
    # a kindergarten class sitting together; a cameraman filling the frame in a TV studio
    "none_indoor": ["013.jpg", "034.jpg"],
    # two close-up portraits of one athlete; a ski-jump hill and a surfing wave where the landscape dominates
    "none_sports": ["029.jpg", "031.jpg", "034.jpg", "041.jpg"],
    # foliage (2), a woman's face ('freckled'), a waffle
    "none_texture": ["011.jpg", "013.jpg", "042.jpg", "043.jpg"],
    # one airliner filling the frame
    "none_aerial": ["002.jpg", "003.jpg"],
    # unrecognisable close-ups of a surface
    "none_product": ["007.jpg", "038.jpg"],
}

IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp", ".bmp")


def rng(group: str, source: str) -> random.Random:
    return random.Random(f"{SEED}:{group}:{source}")


def dl(repo: str, filename: str) -> str:
    from huggingface_hub import hf_hub_download

    return hf_hub_download(repo, filename, repo_type="dataset", revision=REV[repo])


def repo_files(repo: str) -> list[tuple[str, int]]:
    """(path, size) of every file in the pinned revision, sorted by path."""
    from huggingface_hub import HfApi

    info = HfApi().dataset_info(repo, revision=REV[repo], files_metadata=True)
    return sorted((s.rfilename, s.size or 0) for s in info.siblings)


def as_bytes(cell) -> bytes:
    """An image cell of a parquet file: struct{bytes,path}, raw bytes, or a list of those (first one)."""
    if isinstance(cell, list):
        cell = cell[0]
    if isinstance(cell, dict):
        cell = cell["bytes"]
    return bytes(cell)


def parquet_meta(path: str, columns: list[str]) -> list[dict]:
    import pyarrow.parquet as pq

    pf = pq.ParquetFile(path)
    if not columns:
        return [{} for _ in range(pf.metadata.num_rows)]
    return pf.read(columns=columns).to_pylist()


def parquet_images(path: str, rows: list[int], column: str = "image") -> list[bytes]:
    """Image bytes of the given row numbers; only the row groups that are needed get read."""
    import pyarrow.parquet as pq

    pf = pq.ParquetFile(path)
    starts, total = [], 0
    for g in range(pf.metadata.num_row_groups):
        starts.append(total)
        total += pf.metadata.row_group(g).num_rows
    by_group: dict[int, list[int]] = defaultdict(list)
    for row in rows:
        g = max(i for i, s in enumerate(starts) if s <= row)
        by_group[g].append(row)
    found = {}
    for g, wanted in by_group.items():
        cells = pf.read_row_group(g, columns=[column])[column].to_pylist()
        for row in wanted:
            found[row] = as_bytes(cells[row - starts[g]])
    return [found[row] for row in rows]


def pick_parquet(group, repo, filename, n, *, column="image", meta=(), keep=None, key=None, per_key=None):
    """Random rows of one parquet shard.
    keep(meta_row) filters rows; with key(meta_row) + per_key the pick is `per_key` rows for every key value
    (n is ignored), otherwise n rows over all kept rows."""
    path = dl(repo, filename)
    rows_meta = parquet_meta(path, list(meta))
    kept = [i for i, m in enumerate(rows_meta) if keep is None or keep(m)]
    r = rng(group, f"{repo}/{filename}")
    if key is None:
        chosen = sorted(r.sample(kept, min(n, len(kept))))
    else:
        buckets: dict = defaultdict(list)
        for i in kept:
            buckets[key(rows_meta[i])].append(i)
        chosen = []
        for value in sorted(buckets):
            chosen += sorted(r.sample(buckets[value], min(per_key, len(buckets[value]))))
    images = parquet_images(path, chosen, column)
    return [(f"{repo}:{filename}#row{row}", data) for row, data in zip(chosen, images)]


def pick_per_class(group, source, by_class: dict[str, list[str]], per_class: int, extra: int = 0) -> list[str]:
    """per_class random names from every class (classes and names in sorted order), then `extra` more
    random names from everything that is left."""
    r = rng(group, source)
    chosen, rest = [], []
    for name in sorted(by_class):
        names = sorted(by_class[name])
        take = r.sample(names, min(per_class, len(names)))
        chosen += sorted(take)
        rest += [x for x in names if x not in take]
    if extra:
        chosen += sorted(r.sample(rest, min(extra, len(rest))))
    return chosen


def zip_classes(path: str, class_of) -> tuple[zipfile.ZipFile, dict[str, list[str]]]:
    archive = zipfile.ZipFile(path)
    by_class: dict[str, list[str]] = defaultdict(list)
    for name in archive.namelist():
        if name.startswith("__MACOSX") or not name.lower().endswith(IMAGE_SUFFIXES):
            continue
        label = class_of(name)
        if label:
            by_class[label].append(name)
    return archive, by_class


# ---------------------------------------------------------------- PASCAL VOC 2007 (person / group / vehicles)

VOC_VEHICLES = ["aeroplane", "bicycle", "boat", "bus", "car", "motorbike", "train"]
_voc_cache: dict = {}


def voc_annotations() -> dict[str, dict]:
    """{image id: {"w","h","objects":[(name, area_fraction, height_fraction, difficult)]}} for all of VOC2007."""
    if "ann" not in _voc_cache:
        ann = {}
        with tarfile.open(dl("HuggingFaceM4/pascal_voc", "voc2007.tar.gz")) as tar:
            for member in tar:
                if "/Annotations/" not in member.name or not member.name.endswith(".xml"):
                    continue
                root = ET.parse(tar.extractfile(member)).getroot()
                w, h = int(root.find("size/width").text), int(root.find("size/height").text)
                objects = []
                for obj in root.findall("object"):
                    box = obj.find("bndbox")
                    x0, y0, x1, y1 = (float(box.find(t).text) for t in ("xmin", "ymin", "xmax", "ymax"))
                    objects.append((obj.find("name").text, (x1 - x0) * (y1 - y0) / (w * h), (y1 - y0) / h,
                                    obj.find("difficult").text == "1"))
                ann[Path(member.name).stem] = {"objects": objects}
        _voc_cache["ann"] = ann
    return _voc_cache["ann"]


def voc_images(ids: list[str]) -> list[tuple[str, bytes]]:
    wanted = {f"VOC2007/JPEGImages/{i}.jpg": i for i in ids}
    found = {}
    with tarfile.open(dl("HuggingFaceM4/pascal_voc", "voc2007.tar.gz")) as tar:
        for member in tar:
            if member.name in wanted:
                found[wanted[member.name]] = tar.extractfile(member).read()
                if len(found) == len(wanted):
                    break
    return [(f"HuggingFaceM4/pascal_voc:voc2007.tar.gz:{i}.jpg", found[i]) for i in ids]


def voc_single_person(group, n):
    ok = [i for i, a in sorted(voc_annotations().items())
          if len(a["objects"]) == 1 and a["objects"][0][0] == "person" and not a["objects"][0][3]
          and 0.20 <= a["objects"][0][1] <= 0.85]
    return voc_images(sorted(rng(group, "voc2007").sample(ok, n)))


def voc_group(group, n):
    def is_group(a):
        objects = a["objects"]
        return (3 <= len(objects) <= 12 and all(o[0] == "person" for o in objects)
                and sum(o[2] >= 0.30 for o in objects) >= 3)
    ok = [i for i, a in sorted(voc_annotations().items()) if is_group(a)]
    return voc_images(sorted(rng(group, "voc2007").sample(ok, n)))


def voc_vehicles(group, per_class):
    by_class: dict[str, list[str]] = defaultdict(list)
    for i, a in sorted(voc_annotations().items()):
        names = {o[0] for o in a["objects"]}
        if len(names) == 1 and next(iter(names)) in VOC_VEHICLES and max(o[1] for o in a["objects"]) >= 0.25:
            by_class[next(iter(names))].append(i)
    return voc_images(pick_per_class(group, "voc2007", by_class, per_class))


# ---------------------------------------------------------------- the other sources

FLICKR_POSE = re.compile(r"\bpos(e|es|ing|ed)\b.*\b(picture|photo|photograph|portrait|camera)\b")
FLICKR_MANY = re.compile(r"\b(group|family|team|friends|people|men|women|girls|boys|children|kids|guys|ladies|adults"
                         r"|students|players|three|four|five|six|seven|eight|nine|ten)\b")
FLICKR_TWO = re.compile(r"\b(two|couple|a man and a woman|a woman and a man)\b")


def flickr_groups(group, n):
    """Flickr30k images where at least 2 of the 5 captions say several people are posing for a photo
    (and at most 1 caption says 'two' / 'couple')."""
    candidates = []
    for shard in range(9):
        filename = f"data/test-{shard:05d}-of-00009.parquet"
        for row, m in enumerate(parquet_meta(dl("lmms-lab/flickr30k", filename), ["caption"])):
            captions = [c.lower() for c in m["caption"]]
            if (sum(bool(FLICKR_POSE.search(c) and FLICKR_MANY.search(c)) for c in captions) >= 2
                    and sum(bool(FLICKR_TWO.search(c)) for c in captions) <= 1):
                candidates.append((filename, row))
    chosen = sorted(rng(group, "flickr30k").sample(candidates, min(n, len(candidates))))
    out = []
    for filename, row in chosen:
        data = parquet_images(dl("lmms-lab/flickr30k", filename), [row])[0]
        out.append((f"lmms-lab/flickr30k:{filename}#row{row}", data))
    return out


def repo_file_pick(group, repo, by_class: dict[str, list[str]], per_class, extra=0):
    names = pick_per_class(group, repo, by_class, per_class, extra)
    return [(f"{repo}:{name}", Path(dl(repo, name)).read_bytes()) for name in names]


def intel(group, classes, per_class):
    by_class: dict[str, list[str]] = defaultdict(list)
    for name, _ in repo_files("sfarrukhm/intel-image-classification"):
        parts = name.split("/")
        if len(parts) == 4 and parts[1] == "seg_test" and parts[2] in classes:
            by_class[parts[2]].append(name)
    return repo_file_pick(group, "sfarrukhm/intel-image-classification", by_class, per_class)


def landmarks(group, repo, n):
    """Revisited Oxford / Paris: corpus images that are a labelled positive for some OTHER query image
    (i.e. verified views of one of the landmark buildings, not the unlabelled distractor photos)."""
    import pyarrow.parquet as pq

    qrels = pq.ParquetFile(dl(repo, "qrels-00000-of-00001.parquet")).read().to_pylist()
    positive = {q["corpus-id"] for q in qrels if q["score"] > 0 and q["corpus-id"] != q["query-id"]}
    path = dl(repo, "corpus-00000-of-00001.parquet")
    first_row = {}
    for row, m in enumerate(parquet_meta(path, ["id"])):
        first_row.setdefault(m["id"], row)
    ids = sorted(rng(group, repo).sample(sorted(positive & set(first_row)), n))
    images = parquet_images(path, [first_row[i] for i in ids])
    return [(f"{repo}:corpus#{i}", data) for i, data in zip(ids, images)]


def snacks(group, per_class):
    archive, by_class = zip_classes(dl("Matthijs/snacks", "images.zip"),
                                    lambda n: n.split("/")[2] if n.startswith("data/test/") else None)
    names = pick_per_class(group, "Matthijs/snacks", by_class, per_class)
    return [(f"Matthijs/snacks:images.zip:{n}", archive.read(n)) for n in names]


def omniact(group, kind, n):
    archive, by_class = zip_classes(dl("Writer/omniact", "data.zip"),
                                    lambda name: "x" if name.startswith(f"data/data/{kind}/") else None)
    names = sorted(rng(group, f"omniact/{kind}").sample(sorted(by_class["x"]), n))
    return [(f"Writer/omniact:data.zip:{name}", archive.read(name)) for name in names]


def anime_omni(group, n):
    names = [name for name, _ in repo_files("kurumikz/AnimeOmni") if re.fullmatch(r"[\d-]+/data/image_\d+\.jpg", name)]
    chosen = sorted(rng(group, "AnimeOmni").sample(names, n))
    return [(f"kurumikz/AnimeOmni:{name}", Path(dl("kurumikz/AnimeOmni", name)).read_bytes()) for name in chosen]


def anime_wallpapers(group, n):
    """Top-level image files of at most 4 MB (the repo also has dated sub-folders, which are not used)."""
    names = [name for name, size in repo_files("puruchinera/anime_wallpapers")
             if "/" not in name and name.lower().endswith(IMAGE_SUFFIXES) and size <= 4_000_000]
    chosen = sorted(rng(group, "anime_wallpapers").sample(names, n))
    return [(f"puruchinera/anime_wallpapers:{name}", Path(dl("puruchinera/anime_wallpapers", name)).read_bytes())
            for name in chosen]


def plantnet(group, n, per_species=2):
    """Pl@ntNet-300K is long-tailed and the shard is sorted by species (a plain random pick gives mostly a
    handful of species), so: up to `per_species` random images of every species in the shard, then n of those."""
    filename = "data/test-00000-of-00008-ccdf9a8395f11a02.parquet"
    path = dl("mikehemberger/plantnet300K", filename)
    by_species: dict[int, list[int]] = defaultdict(list)
    for row, m in enumerate(parquet_meta(path, ["label"])):
        by_species[m["label"]].append(row)
    r = rng(group, "plantnet")
    pool = []
    for species in sorted(by_species):
        pool += r.sample(by_species[species], min(per_species, len(by_species[species])))
    rows = sorted(r.sample(sorted(pool), min(n, len(pool))))
    return [(f"mikehemberger/plantnet300K:{filename}#row{row}", data)
            for row, data in zip(rows, parquet_images(path, rows))]


INDOOR_SKIP = {"cloister", "florist", "greenhouse", "inside_bus", "inside_subway", "subway", "trainstation",
               "fastfood_restaurant", "restaurant", "restaurant_kitchen", "grocerystore", "pantry"}


def indoor(group, per_class, extra):
    archive, by_class = zip_classes(dl("keremberke/indoor-scene-classification", "data/test.zip"),
                                    lambda n: n.split("/")[0] if n.split("/")[0] not in INDOOR_SKIP else None)
    names = pick_per_class(group, "indoor", by_class, per_class, extra)
    return [(f"keremberke/indoor-scene-classification:data/test.zip:{n}", archive.read(n)) for n in names]


SPORTS_SKIP = {"formula 1 racing", "nascar racing", "motorcycle racing", "sidecar racing", "snowmobile racing",
               "hydroplane racing", "sailboat racing", "ice yachting", "track bicycle", "bmx", "bike polo",
               "water cycling", "mushing", "rowing", "canoe slamon"}


def sports(group, n_classes):
    archive, by_class = zip_classes(
        dl("HES-XPLAIN/SportsImageClassification", "SportsImageClassification.zip"),
        lambda n: n.split("/")[1] if n.startswith("train/") and n.split("/")[1] not in SPORTS_SKIP else None)
    r = rng(group, "sports-classes")
    classes = sorted(r.sample(sorted(by_class), n_classes))
    names = pick_per_class(group, "sports", {c: by_class[c] for c in classes}, 1)
    return [(f"HES-XPLAIN/SportsImageClassification:{n}", archive.read(n)) for n in names]


def xray(group, n):
    archive, by_class = zip_classes(dl("keremberke/chest-xray-classification", "data/test.zip"), lambda n: "x")
    names = sorted(rng(group, "xray").sample(sorted(by_class["x"]), n))
    return [(f"keremberke/chest-xray-classification:data/test.zip:{name}", archive.read(name)) for name in names]


def ai2d(group, n):
    """AI2D has several questions per diagram: keep the first row of every distinct image, then pick n."""
    import pyarrow.parquet as pq

    filename = "data/test-00000-of-00002.parquet"
    path = dl("lmms-lab/ai2d", filename)
    first_row: dict[str, int] = {}
    pf = pq.ParquetFile(path)
    row = 0
    for g in range(pf.metadata.num_row_groups):
        for cell in pf.read_row_group(g, columns=["image"])["image"].to_pylist():
            first_row.setdefault(hashlib.sha1(as_bytes(cell)).hexdigest(), row)
            row += 1
    rows = sorted(rng(group, "ai2d").sample(sorted(first_row.values()), n))
    return [(f"lmms-lab/ai2d:{filename}#row{r}", data) for r, data in zip(rows, parquet_images(path, rows))]


def online_products(group, per_class):
    """Stanford Online Products (eBay listing photos); class = folder; bicycles are not used.
    The repo holds only part of the original dataset, so the pick is made from the repo's own file listing."""
    by_class: dict[str, list[str]] = defaultdict(list)
    for name, _ in repo_files("heshamdahy/stanford-products-images"):
        if "/" in name and name.lower().endswith(IMAGE_SUFFIXES) and not name.startswith("bicycle"):
            by_class[name.split("/")[0]].append(name)
    return repo_file_pick(group, "heshamdahy/stanford-products-images", by_class, per_class)


def trashnet(group, per_class):
    archive, by_class = zip_classes(
        dl("garythung/trashnet", "dataset-resized.zip"),
        lambda n: n.split("/")[1] if n.split("/")[1] in ("cardboard", "glass", "metal", "plastic", "trash") else None)
    names = pick_per_class(group, "trashnet", by_class, per_class)
    return [(f"garythung/trashnet:dataset-resized.zip:{n}", archive.read(n)) for n in names]


WIKIART_GENRES = {0: "abstract_painting", 2: "genre_painting", 7: "religious_painting", 8: "sketch_and_study"}
DOCLAYNET = "data/test-00001-of-00002-b2396409212624ed.parquet"


def build(group: str) -> list[tuple[str, bytes]]:
    """All (source key, image bytes) of one group, in a fixed order."""
    g = group
    if g == "人像":
        return (pick_parquet(g, "logasja/lfw", "data/train-00000-of-00001.parquet", 30) + voc_single_person(g, 30))
    if g == "合照":
        return flickr_groups(g, 45) + voc_group(g, 20)
    if g in ("貓", "狗"):
        want = 0 if g == "貓" else 1
        return pick_parquet(g, "timm/oxford-iiit-pet", "data/test-00000-of-00001.parquet", 60,
                            meta=["label_cat_dog"], keep=lambda m: m["label_cat_dog"] == want)
    if g == "風景":
        return (pick_parquet(g, "larrygzlb1994/lhq_256", "data/train-00000-of-00021-60e9330dc194e473.parquet", 36)
                + intel(g, ("forest", "glacier", "mountain", "sea"), 6))
    if g == "城市建築":
        return (landmarks(g, "JamieSJS/r-paris", 25) + landmarks(g, "JamieSJS/r-oxford", 20)
                + intel(g, ("buildings", "street"), 15))
    if g == "美食":
        return (pick_parquet(g, "chaeso/food_chinese_2017", "data/test-00000-of-00003-ac558dbfa237ee1c.parquet", 22)
                + pick_parquet(g, "bharat-raghunathan/indian-foods-dataset",
                               "data/test-00000-of-00001-75236f054535aed8.parquet", 20)
                + snacks(g, 1))
    if g == "花草植物":
        return plantnet(g, 60)
    if g == "交通工具":
        return voc_vehicles(g, 9)
    if g == "螢幕截圖":
        return (pick_parquet(g, "creative-graphic-design/Rico",
                             "ui-screenshots-and-view-hierarchies/test-00000-of-00002.parquet", 25,
                             column="screenshot")
                + omniact(g, "desktop", 20) + omniact(g, "web", 15))
    if g == "文件收據":
        return (pick_parquet(g, "jsdnrs/ICDAR2019-SROIE", "data/test-00000-of-00001.parquet", 25)
                + pick_parquet(g, "ds4sd/DocLayNet-v1.1", DOCLAYNET, 0, meta=["metadata"],
                               key=lambda m: m["metadata"]["doc_category"], per_key=7))
    if g == "動漫插畫":
        return anime_omni(g, 40) + anime_wallpapers(g, 20)
    if g == "迷因梗圖":
        return pick_parquet(g, "Leonardo6/memecap", "data/train-00000-of-00003.parquet", 60, column="images")
    if g == "none_wildlife":
        return (pick_parquet(g, "bentrevett/caltech-ucsd-birds-200-2011", "data/test-00000-of-00002.parquet", 15)
                + pick_parquet(g, "lucabaggi/animal-wildlife", "data/test-00000-of-00001.parquet", 30,
                               meta=["label"], keep=lambda m: m["label"] not in (9, 18)))  # 9 = cat, 18 = dog
    if g == "none_indoor":
        return indoor(g, 1, 6)
    if g == "none_sports":
        return sports(g, 45)
    if g == "none_art":
        return pick_parquet(g, "huggan/wikiart", "data/train-00000-of-00072.parquet", 0, meta=["genre"],
                            keep=lambda m: m["genre"] in WIKIART_GENRES, key=lambda m: m["genre"], per_key=14)
    if g == "none_texture":
        return pick_parquet(g, "tanganke/dtd", "data/test-00000-of-00001.parquet", 0, meta=["label"],
                            key=lambda m: m["label"], per_key=1)
    if g == "none_aerial":
        return pick_parquet(g, "blanchon/UC_Merced", "data/train-00000-of-00001.parquet", 0, meta=["label"],
                            key=lambda m: m["label"], per_key=2)
    if g == "none_xray":
        return xray(g, 40)
    if g == "none_diagram":
        return ai2d(g, 45)
    if g == "none_product":
        return online_products(g, 4)
    if g == "none_objects":
        return trashnet(g, 9)
    raise ValueError(group)


GROUPS = ["人像", "合照", "貓", "狗", "風景", "城市建築", "美食", "花草植物", "交通工具", "螢幕截圖", "文件收據", "動漫插畫",
          "迷因梗圖", "none_wildlife", "none_indoor", "none_sports", "none_art", "none_texture", "none_aerial",
          "none_xray", "none_diagram", "none_product", "none_objects"]


def contact_sheet(folder: Path, out: Path, cell: int = 200, columns: int = 8) -> None:
    from PIL import Image, ImageDraw

    files = sorted(folder.glob("*.jpg"))
    rows = (len(files) + columns - 1) // columns
    sheet = Image.new("RGB", (columns * cell, max(rows, 1) * (cell + 16)), "white")
    draw = ImageDraw.Draw(sheet)
    for i, path in enumerate(files):
        image = Image.open(path).convert("RGB")
        image.thumbnail((cell, cell))
        x, y = (i % columns) * cell, (i // columns) * (cell + 16)
        sheet.paste(image, (x + (cell - image.width) // 2, y + 16 + (cell - image.height) // 2))
        draw.text((x + 4, y + 2), path.stem, fill="red")
    sheet.save(out, quality=85)


def main() -> None:
    from PIL import Image

    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True, help="output folder (outside the repo), e.g. /opt/holdout/images")
    parser.add_argument("--groups", nargs="*", default=GROUPS)
    parser.add_argument("--keep-removed", action="store_true", help="do not apply the by-eye REMOVED list")
    parser.add_argument("--sheets", default="", help="also write one contact-sheet JPEG per group into this folder")
    args = parser.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    manifest_path = out / "manifest.tsv"
    manifest = {}
    if manifest_path.exists():
        for line in manifest_path.read_text(encoding="utf-8").split("\n"):
            if line:
                group, name, key, state = line.split("\t")
                manifest[(group, name)] = (key, state)
    for group in args.groups:
        folder = out / group
        folder.mkdir(exist_ok=True)
        for old in folder.glob("*.jpg"):
            old.unlink()
        for key in [k for k in manifest if k[0] == group]:
            del manifest[key]
        removed = set() if args.keep_removed else set(REMOVED.get(group, []))
        kept = 0
        for number, (source, data) in enumerate(build(group)):
            name = f"{number:03d}.jpg"
            if name in removed:
                manifest[(group, name)] = (source, "removed")
                continue
            image = Image.open(io.BytesIO(data)).convert("RGB")
            image.thumbnail((1024, 1024))
            image.save(folder / name, quality=90)
            manifest[(group, name)] = (source, "kept")
            kept += 1
        print(f"{group}: {kept} kept, {len(removed)} removed", flush=True)
        if args.sheets:
            Path(args.sheets).mkdir(parents=True, exist_ok=True)
            contact_sheet(folder, Path(args.sheets) / f"{group}.jpg")
    order = {g: i for i, g in enumerate(GROUPS)}
    rows = sorted(manifest.items(), key=lambda x: (order[x[0][0]], x[0][1]))
    lines = [f"{g}\t{n}\t{k}\t{s}" for (g, n), (k, s) in rows]
    manifest_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
