"""Rebuild the SECOND independent hold-out set ("holdout2") described in holdout2_sources.txt (not an automated test).

Collected on 2026-11-27, AFTER all prompts / thresholds / rules were fixed. It must never be used for tuning;
it is only for evaluation runs that are logged in docs/evaluation.md. Nothing in this script runs the classifier.

    pip install huggingface_hub pyarrow Pillow pillow-heif av
    python tests/manual/collect_holdout2.py --out /opt/holdout2 --sheets /opt/holdout2/sheets

What is different from collect_holdout.py (the first hold-out)
  * Files are saved with their ORIGINAL BYTES and their original extension (no re-encoding, no resizing), so EXIF
    orientation, transparency, HEIC, MPO, animated GIF, mislabelled extensions ... reach the app's real loaders.
  * Images that are genuinely between two default categories are KEPT, in a folder named with both labels joined
    by "+" (e.g. 人像+風景). Either answer counts as correct.
  * There is a video set as well: <out>/videos/<group>/..., next to <out>/images/<group>/...

What it does
  1. Downloads single shards / files of public Hugging Face datasets (pinned to the commit hashes in REV).
  2. Picks files at random using only dataset labels / metadata (folder names, file names, captions, hashtags),
     with fixed seeds: random.Random(f"{SEED}:{group}:{source}") - string seeds are stable across Python versions.
  3. Saves them as <out>/{images,videos}/<group>/NNN<original extension> plus manifest.tsv in each of the two
     folders (columns: final group, file, picked-for group, source key, state).
  4. Applies the by-eye review done on contact sheets (see holdout2_sources.txt for the reasons):
       REMOVED - deleted: mislabelled or unrecognisable, or belongs to no default category at all.
       MOVED   - the label given by the dataset metadata was wrong or incomplete: the file is stored under the
                 group it really belongs to (a default category, a "+" pair, or a none_ group) as
                 <picked-for group>_<NNN><ext>.
     Nothing was deleted or moved for looking "hard". Use --raw to see the raw random selection.

Folder name == a default category -> that is the expected label; "A+B" -> either A or B is correct;
none_xxx -> belongs to no default category (same convention as dump_embeddings.py --extra-dir --keep-original).
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import random
import re
import tarfile
from collections import defaultdict
from pathlib import Path

SEED = 20261127

# dataset repo -> commit hash used when the set was built
REV = {
    "Rabornkraken/xhs-travel-photos": "d51967c3f5daddb4fa2c9d727e4f9674bfe44233",
    "HuggingFaceM4/FairFace": "54d573cdb8b5af490ba8da9da2799628f6e5c496",
    "nobg/humans-rgba": "6724ee5a8ec0ae57bfe7cc8671f36d6d6737ec62",
    "pixparse/cc3m-wds": "46f3d69f840e59d77d52e8decfe5baec97e94c7f",
    "li-yan/cat-by-breed-v1": "80be8b7dbd91c947445da487d6e035997d39b1a0",
    "MYtechnology/real-dog-images-with-breed-and-metadata": "652f50aabd23b7447d2dc31f511c4d2582b8b3b2",
    "JapanDegitalMaterial/Scenery_of_japan": "4899b2bc26f23d878aaae6a7ad2808f5666c1f27",
    "Wu52F/Image2GPS_dataset": "7d6880dc7f37c494bfb0ce254e5d62545d65f78d",
    "hhhuang/TaiwanVQA": "78c8f9c25696df4ce58fb3fc81a4d03d39d85500",
    "TuyenTrungLe/vietnamese_food_images": "4285b8881f16b20bca8844655b5b247c3fb6eb1f",
    "Kos1976/1-flowers-50-commercial": "8cbbd24558193e86f7c2779815e24a9ada9d51e7",
    "Voxel51/FGVC-Aircraft": "bb367f92bf19d60f086b95b5aef117361201dfb5",
    "tanganke/stanford_cars": "9abf6cf7d6dfa7b95152a0d6e791ea9435b47a40",
    "atharparvezce/iOS-1K-Mobile-UI-Dataset": "c616b870539e5731a68e413806a68aeefefef803",
    "andersonbcdefg/osworld_screenshots": "c37867ddd9d684f7c6915987649c52d868b7867a",
    "silatus/1k_Website_Screenshots_and_Metadata": "f953fdcae786181a33ebce6f988ca9b84f3caa6e",
    "Francisco-Cruz/InvoicesReceiptsPT": "a3f7a8ad4b8cfd37495293153f14e22d072327ea",
    "IngCrowd/japanese-receipts-detailed": "e679e42ba2a408038d4151ced36eb6f32589422c",
    "nielsr/funsd": "7e7eeeedd84ce86540eb83cbbf7c75a3fcc7c7a5",
    "NoTeS-Bank/ICDAR_2025_Handwritten_Notes_Understanding_Challenge": "7dc719667f8e58eaae431c92513196a357352d10",
    "deepghs/subsplease_animes": "3ea49d3bbd26cacfa3c1f77daf5bc74c184e75c0",
    "LiuliFox/stickers": "fc08883a8f649147d80da5560001877123e01383",
    "nobg/anime-segmentation-rgba": "77482be5fd96f6ce9bd88e883ed2f0dd7ba5f72f",
    "neuralcatcher/hateful_memes": "d201c488dc7024623d1ecbcc987b3f132c4c2e12",
    "mrzjy/ChineseBQB": "99835ec5427e6825f00d276258e9dd50588d7be1",
    "pielet/cat-memes": "66e9a152ab94490cdd4d08e36efe58d67eb24ba0",
    "tzvc/organization-logos": "ef74fc26378d5f0826978942fc0920e8f1ca2ba5",
    "nadakandrew/booksdata": "74157f97a359d600bbb5b22cef1309134d22b82f",
    "marvy/book-covers": "d2ecc5e80b55aa0e1174393ffcd3a29978fe68df",
    "matthieulel/galaxy10_decals": "d1cfafdcce69ca7cd25aecce1a0b578cf07e6a30",
    "bloombio/phytoplankton-microscopy": "37d4c9ae30ba698c8edca4825409daa23586bd4b",
    "umesh16071973/Floorplan_Dataset_22022024": "08a06ff0f20f22fce1b408dc2c9860875b7f1753",
    "Voxel51/FloorPlanCAD": "b48e3b1932f987d053ab887f113b0a62959e943d",
    "paowick/Banknote_thai": "022c47774a521fa0c128e5d865166e7751c97ee7",
    "shravya11/mechanical-tools-dataset": "b32cae11fd75f7f612d99a2c692c4e95884448b1",
    "JackFurby/playing-cards": "966aa7cf31d55c255e5b05b545c9a5aafa28dabd",
    "Rezuwan/Parrot60_Dataset": "4c71e196bc69e29df64d03d9e61973f3f0a3d86b",
    "LakshC/SynthBench": "dbf1ced77bc0f83d9a55875503b1eb5bd8bb4c0a",
    "Shawn-Huang/CUFED-AlbumBench": "d1e9a034ed66a69bb4fb3a64d94fbe8d625f9dd7",
    "FastVideo/Mixkit-Src": "b5fff04254af8bbf716b1c166e85b8fe4000bb0e",
    "Shofo/shofo-tiktok-general-small": "93625fe8042be012276b2d88fd27860c4ee68200",
    "sayakpaul/ucf101-subset": "b9984b8d2a95e4a1879e1b071e9433858d0bc24a",
    "nateraw/kinetics-mini": "9f4ed38128a355c352527209101be3e326471816",
}

IMAGE_GROUPS = ["人像", "合照", "貓", "狗", "風景", "城市建築", "美食", "花草植物", "交通工具", "螢幕截圖", "文件收據", "動漫插畫",
                "迷因梗圖", "貓+迷因梗圖", "none_logo", "none_book", "none_galaxy", "none_microscopy", "none_floorplan",
                "none_banknote", "none_tools", "none_cards", "none_parrot", "none_gadget"]
VIDEO_GROUPS = ["人像", "合照", "貓", "狗", "風景", "城市建築", "美食", "花草植物", "交通工具", "none_sports", "none_music",
                "none_misc"]

# ---------------------------------------------------------------------------------------------------------------
# By-eye review (contact sheets), applied after the random selection. Keys are the names of the RAW selection
# (<picked-for group>/<NNN><ext>). See holdout2_sources.txt for the reason of every entry.
# REMOVED_*: deleted. MOVED_*: stored under another group as <picked-for group>_<NNN><ext>.
REMOVED_IMAGES: dict[str, list[str]] = {}
MOVED_IMAGES: dict[str, dict[str, str]] = {}
REMOVED_VIDEOS: dict[str, list[str]] = {}
MOVED_VIDEOS: dict[str, dict[str, str]] = {}

IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".heic", ".heif", ".tif", ".tiff")
VIDEO_SUFFIXES = (".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v")


def rng(group: str, source: str) -> random.Random:
    return random.Random(f"{SEED}:{group}:{source}")


def dl(repo: str, filename: str) -> str:
    from huggingface_hub import hf_hub_download

    return hf_hub_download(repo, filename, repo_type="dataset", revision=REV[repo])


_files_cache: dict[str, list[tuple[str, int]]] = {}


def repo_files(repo: str) -> list[tuple[str, int]]:
    """(path, size) of every file in the pinned revision, sorted by path."""
    if repo not in _files_cache:
        from huggingface_hub import HfApi

        info = HfApi().dataset_info(repo, revision=REV[repo], files_metadata=True)
        _files_cache[repo] = sorted((s.rfilename, s.size or 0) for s in info.siblings)
    return _files_cache[repo]


def sniff(data: bytes) -> str:
    """Extension for image bytes that come without a file name (parquet cells)."""
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data[:4] == b"GIF8":
        return ".gif"
    if data[4:8] == b"ftyp":
        return ".heic"
    raise ValueError("unknown image type")


def ext_of(name: str | None, data: bytes) -> str:
    """The extension the dataset gives the file (kept even when it does not match the content - a real folder
    has such files too); sniffed from the content only when there is no file name."""
    suffix = Path(name).suffix.lower() if name else ""
    return suffix if suffix in IMAGE_SUFFIXES + VIDEO_SUFFIXES else sniff(data)


class Parquet:
    """One parquet shard, read over HTTP range requests (only the columns / row groups that are needed)."""

    def __init__(self, repo: str, filename: str):
        import pyarrow.parquet as pq
        from huggingface_hub import HfFileSystem

        self.key = f"{repo}:{filename}"
        self.handle = HfFileSystem().open(f"datasets/{repo}@{REV[repo]}/{filename}", "rb")
        self.pf = pq.ParquetFile(self.handle)

    def meta(self, columns: list[str]) -> list[dict]:
        if not columns:
            return [{} for _ in range(self.pf.metadata.num_rows)]
        return self.pf.read(columns=columns).to_pylist()

    def cells(self, rows: list[int], column: str = "image") -> list[tuple[bytes, str | None]]:
        """(bytes, file name or None) of an image column for the given row numbers."""
        starts, total = [], 0
        for g in range(self.pf.metadata.num_row_groups):
            starts.append(total)
            total += self.pf.metadata.row_group(g).num_rows
        by_group: dict[int, list[int]] = defaultdict(list)
        for row in rows:
            by_group[max(i for i, s in enumerate(starts) if s <= row)].append(row)
        found = {}
        for g, wanted in by_group.items():
            cells = self.pf.read_row_group(g, columns=[column])[column].to_pylist()
            for row in wanted:
                cell = cells[row - starts[g]]
                if isinstance(cell, list):
                    cell = cell[0]
                found[row] = (bytes(cell["bytes"]), cell.get("path")) if isinstance(cell, dict) else (bytes(cell), None)
        return [found[row] for row in rows]


def pick_parquet(group, repo, filename, n, *, column="image", meta=(), keep=None, key=None, per_key=None):
    """Random rows of one parquet shard. keep(meta_row) filters rows; with key(meta_row) + per_key the pick is
    `per_key` rows for every key value (n is ignored), otherwise n rows over all kept rows."""
    pq_file = Parquet(repo, filename)
    rows_meta = pq_file.meta(list(meta))
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
    return [(f"{repo}:{filename}#row{row}", data, ext_of(name, data))
            for row, (data, name) in zip(chosen, pq_file.cells(chosen, column))]


def read_files(repo: str, names: list[str]) -> list[tuple[str, bytes, str]]:
    out = []
    for name in names:
        data = Path(dl(repo, name)).read_bytes()
        out.append((f"{repo}:{name}", data, ext_of(name, data)))
    return out


def pick_files(group, repo, n, *, match=None, suffixes=IMAGE_SUFFIXES, max_size=None, source=None):
    """n random files of the repo whose path satisfies match(path) (and the size limit)."""
    names = [name for name, size in repo_files(repo)
             if name.lower().endswith(suffixes) and (match is None or match(name))
             and (max_size is None or size <= max_size)]
    return read_files(repo, sorted(rng(group, source or repo).sample(names, min(n, len(names)))))


def pick_files_per_class(group, repo, per_class, class_of, *, suffixes=IMAGE_SUFFIXES, max_size=None, source=None):
    """per_class random files for every class; class_of(path) -> class name or None (not used)."""
    by_class: dict[str, list[str]] = defaultdict(list)
    for name, size in repo_files(repo):
        if name.lower().endswith(suffixes) and (max_size is None or size <= max_size):
            label = class_of(name)
            if label:
                by_class[label].append(name)
    r = rng(group, source or repo)
    chosen = []
    for label in sorted(by_class):
        chosen += sorted(r.sample(sorted(by_class[label]), min(per_class, len(by_class[label]))))
    return read_files(repo, chosen)


# ---------------------------------------------------------------- Xiaohongshu travel photos (selected by caption)

XHS = "Rabornkraken/xhs-travel-photos"
# The dataset has one machine-written English caption for 10,838 of its 28,005 images (captions.json; the key is
# the line number of the image in images.jsonl). Only the FIRST sentence of the caption is used for "what is the
# subject"; the exclusion patterns look at the whole caption.
XHS_TEXT = re.compile(r"\b(text|caption|captions|infographic|map|collage|screenshot|poster|illustration|itinerary|"
                      r"title|logo|watermark|overlay|sign|signs|menu|label|labels|words|chinese characters|split|"
                      r"grid|montage|diptych|triptych|panels?|drawing|painting|cartoon|anime|rendered)\b", re.I)
XHS_PEOPLE = re.compile(r"\b(woman|man|girl|boy|person|people|couple|tourists?|travell?ers?|child|children|kids?|"
                        r"figures?|men|women|friends|family|crowds?|lady|hikers?|monks?|visitors?|pedestrians?|"
                        r"locals?|vendors?|model|she|he|her|his|their|selfie|someone|hands?|legs|feet|silhouettes?|"
                        r"dancers?|swimmers?|surfers?|divers?|guests?|bride|groom|baby|riders?|drivers?|"
                        r"passengers?|fisherm[ae]n|worshippers?|shoppers?|diners?)\b", re.I)
XHS_MANY = re.compile(r"\b(two|three|four|five|six|group|couple|friends|people|crowds?|women|men|children|kids|"
                      r"family|tourists|travell?ers|girls|boys|monks|others|another|companions?)\b", re.I)
XHS_ONE = re.compile(r"^(a|an)\b[^.,;]{0,50}?\b(woman|man|girl|boy|lady)\b", re.I)
XHS_GROUP = re.compile(r"\b(two|three|four|five|six|a group of|group of)\b[^.,;]{0,40}?\b(women|men|friends|girls|"
                       r"boys|people|travell?ers|tourists|children|kids|ladies)\b|\ba (young |smiling |happy )?couple\b",
                       re.I)
XHS_LAND = re.compile(r"\b(mountains?|lake|waterfalls?|beach|ocean|sea|river|valley|forest|rice terraces|cliffs?|"
                      r"canyon|glacier|grasslands?|karst|peaks?|coastline|island|meadow|hills|dunes|lagoon|bay|"
                      r"jungle|landscape|seascape)\b", re.I)
XHS_BUILD = re.compile(r"\b(temple|temples|building|buildings|pagoda|palace|street|streets|city|cityscape|skyline|"
                       r"bridge|hotel|villa|resort|pool|church|mosque|tower|towers|house|houses|village|town|"
                       r"architecture|monastery|stupa|ruins?|skyscrapers?|cathedral|alley|alleyway|shrine|fortress|"
                       r"gate|gateway|facade|rooftops?|market|shops?|storefronts?|castle|pavilion|cabin|hut|huts|"
                       r"bungalows?|tent|tents|lighthouse|pier|dock|harbou?r|road|highway|railway|fence|wall)\b", re.I)
XHS_FOOD = re.compile(r"\b(dish|dishes|food|meal|noodles?|soup|dessert|desserts|plate|plates|bowl|bowls|drink|"
                      r"drinks|cocktail|coffee|fruit|fruits|breakfast|hot ?pot|barbecue|skewers|cuisine|seafood|"
                      r"cake|rice|curry|salad|platter|tea|juice|ice cream|pizza|burger|sushi|dumplings)\b", re.I)
XHS_VEHICLE = re.compile(r"\b(boat|boats|car|cars|motorbike|motorcycle|scooter|scooters|train|airplane|plane|"
                         r"aircraft|bus|ferry|yacht|tuk-tuk|jeep|bicycle|bicycles|ship|ships|truck|van|canoe|kayak|"
                         r"kayaks|sailboat|speedboat|longtail|helicopter|tram|suv|vehicle|vehicles)\b", re.I)
XHS_ANIMAL = re.compile(r"\b(cat|cats|dog|dogs|elephants?|monkeys?|birds?|horses?|yaks?|sheep|cows?|deer|fish|"
                        r"turtles?|animals?|camels?|goats?|buffalo|pandas?|tigers?|peacocks?)\b", re.I)
XHS_PLANT = re.compile(r"\b(flower|flowers|blossoms?|bouquet|petals|orchids?|lotus|frangipani)\b", re.I)
_xhs_cache: dict = {}


def xhs_captions() -> list[tuple[str, str, str]]:
    """(path in the repo, first sentence, whole caption) of every captioned image, in images.jsonl order."""
    if "captions" not in _xhs_cache:
        with open(dl(XHS, "images.jsonl"), encoding="utf-8") as f:
            paths = [json.loads(line)["hf_path"] for line in f if line.strip()]
        with open(dl(XHS, "captions.json"), encoding="utf-8") as f:
            captions = json.load(f)
        out = []
        for key in sorted(captions, key=int):
            text = captions[key].strip()
            out.append((paths[int(key)], re.split(r"(?<=[a-z])[.;] ", text, maxsplit=1)[0], text))
        _xhs_cache["captions"] = out
    return _xhs_cache["captions"]


def xhs(group: str, n: int, rule) -> list[tuple[str, bytes, str]]:
    """n random captioned images for which rule(first sentence, whole caption) is true."""
    ok = [path for path, first, text in xhs_captions() if not XHS_TEXT.search(text) and rule(first, text)]
    return read_files(XHS, sorted(rng(group, "xhs").sample(ok, min(n, len(ok)))))


def xhs_person(first, text):
    return bool(XHS_ONE.search(first)) and not XHS_MANY.search(text) and not XHS_ANIMAL.search(text)


def xhs_group(first, text):
    return bool(XHS_GROUP.search(first))


def xhs_landscape(first, text):
    return (bool(XHS_LAND.search(first)) and not XHS_PEOPLE.search(text) and not XHS_BUILD.search(text)
            and not XHS_FOOD.search(text) and not XHS_VEHICLE.search(text) and not XHS_ANIMAL.search(text))


def xhs_building(first, text):
    return (bool(XHS_BUILD.search(first)) and not XHS_PEOPLE.search(text) and not XHS_FOOD.search(text)
            and not XHS_VEHICLE.search(text) and not XHS_ANIMAL.search(text) and not XHS_LAND.search(first))


def xhs_food(first, text):
    return (bool(XHS_FOOD.search(first)) and not XHS_PEOPLE.search(text) and not XHS_LAND.search(first)
            and not XHS_ANIMAL.search(first))


def xhs_vehicle(first, text):
    return bool(XHS_VEHICLE.search(first)) and not XHS_PEOPLE.search(text) and not XHS_FOOD.search(text)


# ---------------------------------------------------------------- Conceptual Captions 3M (selected by caption)

CC3M = "pixparse/cc3m-wds"
CC3M_SHARDS = [f"cc3m-validation-{i:04d}.tar" for i in range(16)]  # the whole validation split
CC_DRAWN = re.compile(r"\b(illustration|vector|cartoon|drawing|silhouettes?|logo|icon|icons|painting|sketch|clip ?art|"
                      r"3d|render|animation|animated|poster|statue|sculpture|toy|doll|figurine)\b", re.I)
CC_GROUP_WHO = re.compile(r"\b(group of|family|friends|team|classmates|colleagues|bridesmaids|students|children|kids|"
                          r"girls|boys|women|men|people|couple|sisters|brothers|businesspeople|teenagers)\b", re.I)
CC_GROUP_POSE = re.compile(r"\b(pos(e|es|ing)|group (photo|portrait|shot|picture)|family (photo|portrait)|selfie|"
                           r"(smiling|smile) (at|for|to|into) (the )?camera|looking (at|into) (the )?camera|"
                           r"portrait of (a |an |the )?(\w+ )?(family|group|couple|friends|team))\b", re.I)
CC_PORTRAIT = re.compile(r"^(portrait|close up|close-up|closeup|headshot|profile) (portrait )?of (a|an) [^.]*?\b(man|"
                         r"woman|girl|boy|businessman|businesswoman|lady|guy|teenager|student|doctor|bride|"
                         r"groom)\b", re.I)
CC_WITH = re.compile(r"\b(and|with (her|his|a|an|their)|couple|friends|family|group|dog|cat|horse|baby)\b", re.I)
_cc_cache: dict = {}


def cc3m_index() -> list[tuple[str, str, str]]:
    """(shard, member name of the image, caption) for every image of the shards used, in archive order."""
    if "index" not in _cc_cache:
        index = []
        for shard in CC3M_SHARDS:
            captions, images = {}, {}
            with tarfile.open(dl(CC3M, shard)) as tar:
                for member in tar:
                    stem, _, suffix = member.name.rpartition(".")
                    if suffix == "txt":
                        captions[stem] = tar.extractfile(member).read().decode("utf-8", "replace").strip()
                    elif "." + suffix.lower() in IMAGE_SUFFIXES:
                        images[stem] = member.name
            index += [(shard, images[stem], captions[stem]) for stem in sorted(images) if stem in captions]
        _cc_cache["index"] = index
    return _cc_cache["index"]


def cc3m(group: str, n: int, rule) -> list[tuple[str, bytes, str]]:
    ok = [(shard, name) for shard, name, caption in cc3m_index() if not CC_DRAWN.search(caption) and rule(caption)]
    chosen = sorted(rng(group, "cc3m").sample(ok, min(n, len(ok))))
    out = []
    for shard in CC3M_SHARDS:
        wanted = {name for s, name in chosen if s == shard}
        if not wanted:
            continue
        found = {}
        with tarfile.open(dl(CC3M, shard)) as tar:
            for member in tar:
                if member.name in wanted:
                    found[member.name] = tar.extractfile(member).read()
        out += [(f"{CC3M}:{shard}:{name}", found[name], ext_of(name, found[name])) for name in sorted(wanted)]
    return out


def cc_group(caption):
    return bool(CC_GROUP_WHO.search(caption) and CC_GROUP_POSE.search(caption))


def cc_portrait(caption):
    return bool(CC_PORTRAIT.search(caption)) and not CC_WITH.search(caption)


# ---------------------------------------------------------------- TaiwanVQA (phone photos taken in Taiwan)

TAIWAN = "hhhuang/TaiwanVQA"
TAIWAN_SHARDS = [f"data/test-{i:05d}-of-00014.parquet" for i in range(14)]
_taiwan_cache: dict = {}


def taiwan(group: str, n: int, topic: str, sub_topic: str) -> list[tuple[str, bytes, str]]:
    """The benchmark has several questions per photo: first row of every image_id, then n random photos of one
    (topic, sub_topic)."""
    if "index" not in _taiwan_cache:
        index, seen = [], set()
        for shard in TAIWAN_SHARDS:
            for row, m in enumerate(Parquet(TAIWAN, shard).meta(["image_id", "topic", "sub_topic"])):
                if m["image_id"] not in seen:
                    seen.add(m["image_id"])
                    index.append((shard, row, m["topic"], m["sub_topic"]))
        _taiwan_cache["index"] = index
    ok = [(shard, row) for shard, row, t, s in _taiwan_cache["index"] if (t, s) == (topic, sub_topic)]
    chosen = sorted(rng(group, f"taiwanvqa/{topic}/{sub_topic}").sample(ok, min(n, len(ok))))
    out = []
    for shard in sorted({s for s, _ in chosen}):
        rows = [row for s, row in chosen if s == shard]
        for row, (data, name) in zip(rows, Parquet(TAIWAN, shard).cells(rows)):
            out.append((f"{TAIWAN}:{shard}#row{row}", data, ext_of(name, data)))
    return out


# ---------------------------------------------------------------- the other image sources

def hateful_memes(group, n):
    """Facebook Hateful Memes, dev_seen split, only memes labelled NOT hateful (label 0)."""
    repo = "neuralcatcher/hateful_memes"
    with open(dl(repo, "dev_seen.jsonl"), encoding="utf-8") as f:
        names = sorted({json.loads(line)["img"] for line in f if line.strip() and json.loads(line)["label"] == 0})
    return read_files(repo, sorted(rng(group, repo).sample(names, n)))


def chinese_bqb(group, n):
    """ChineseBQB (Chinese sticker / meme collection): n random rows of the first arrow file."""
    import pyarrow as pa

    repo, filename = "mrzjy/ChineseBQB", "data-00000-of-00002.arrow"
    table = pa.ipc.open_stream(dl(repo, filename)).read_all()
    rows = sorted(rng(group, repo).sample(range(table.num_rows), n))
    out = []
    for row in rows:
        cell = table.column("image")[row].as_py()
        name = f"x.{table.column('extension')[row].as_py().lstrip('.')}"
        out.append((f"{repo}:{filename}#row{row}", bytes(cell["bytes"]), ext_of(name, cell["bytes"])))
    return out


def notes(group, n):
    """Handwritten study notes (one page per image); Task_2 holds copies of the Task_1 images and is not used."""
    return pick_files(group, "NoTeS-Bank/ICDAR_2025_Handwritten_Notes_Understanding_Challenge", n,
                      match=lambda name: name.startswith("Task_1/Images/"))


CUFED_EVENTS = {"Wedding", "Graduation", "CasualFamilyGather", "GroupActivity", "Birthday", "Christmas", "Halloween"}


def cufed(group, n):
    """CUFED (personal Flickr albums of events). Only the album's event type is known, not what a photo shows:
    n random photos of albums whose event types are ALL in CUFED_EVENTS. This source is labelled by eye."""
    repo = "Shawn-Huang/CUFED-AlbumBench"
    with open(dl(repo, "event_type.json"), encoding="utf-8") as f:
        albums = {album for album, events in json.load(f).items() if set(events) <= CUFED_EVENTS}
    return pick_files(group, repo, n, match=lambda name: name.startswith("images/") and name.split("/")[1] in albums)


TOOL_CLASSES = ("gasoline_can", "hammer", "pliers", "rope", "screwdriver", "toolbox", "wrench")


def build_images(group: str) -> list[tuple[str, bytes, str]]:
    """All (source key, original bytes, extension) of one image group, in a fixed order."""
    g = group
    if g == "人像":
        return (xhs(g, 25, xhs_person)
                + pick_parquet(g, "HuggingFaceM4/FairFace", "0.25/validation-00000-of-00001-951dbd63c8724ee1.parquet", 15)
                + pick_parquet(g, "nobg/humans-rgba", "data/validation-00000-of-00047.parquet", 12, column="rgba")
                + cc3m(g, 12, cc_portrait))
    if g == "合照":
        return cc3m(g, 30, cc_group) + xhs(g, 20, xhs_group) + cufed(g, 50)
    if g == "貓":
        return pick_files(g, "li-yan/cat-by-breed-v1", 62)
    if g == "狗":
        return pick_files(g, "MYtechnology/real-dog-images-with-breed-and-metadata", 62,
                          match=lambda name: name.startswith("images/"))
    if g == "風景":
        return xhs(g, 35, xhs_landscape) + pick_files(g, "JapanDegitalMaterial/Scenery_of_japan", 59)
    if g == "城市建築":
        return (pick_files(g, "Wu52F/Image2GPS_dataset", 25, match=lambda name: "/00_Raw_images/" in name)
                + xhs(g, 30, xhs_building))
    if g == "美食":
        return (taiwan(g, 35, "Food", "Cuisine and Ingredients")
                + pick_parquet(g, "TuyenTrungLe/vietnamese_food_images", "data/test-00000-of-00002.parquet", 15)
                + xhs(g, 14, xhs_food))
    if g == "花草植物":
        return pick_files(g, "Kos1976/1-flowers-50-commercial", 32) + taiwan(g, 32, "Flora and Fauna", "Plants")
    if g == "交通工具":
        return (pick_files(g, "Voxel51/FGVC-Aircraft", 12, match=lambda name: name.startswith("data/"))
                + pick_parquet(g, "tanganke/stanford_cars", "data/test-00000-of-00002.parquet", 16)
                + taiwan(g, 18, "Transportation", "Transit Systems")
                + xhs(g, 22, xhs_vehicle))
    if g == "螢幕截圖":
        return (pick_files(g, "atharparvezce/iOS-1K-Mobile-UI-Dataset", 25, match=lambda name: name.startswith("images/"))
                + pick_files(g, "andersonbcdefg/osworld_screenshots", 20)
                + pick_files(g, "silatus/1k_Website_Screenshots_and_Metadata", 17))
    if g == "文件收據":
        return (pick_files(g, "Francisco-Cruz/InvoicesReceiptsPT", 20)
                + pick_files(g, "IngCrowd/japanese-receipts-detailed", 12)
                + pick_parquet(g, "nielsr/funsd", "data/test-00000-of-00001.parquet", 14)
                + notes(g, 16))
    if g == "動漫插畫":
        return (pick_files(g, "deepghs/subsplease_animes", 22, match=lambda name: name.startswith("images/"))
                + pick_files(g, "LiuliFox/stickers", 22)
                + pick_parquet(g, "nobg/anime-segmentation-rgba", "data/train/part-00000.parquet", 20,
                               column="target_rgba"))
    if g == "迷因梗圖":
        return hateful_memes(g, 32) + chinese_bqb(g, 32)
    if g == "貓+迷因梗圖":
        return pick_files(g, "pielet/cat-memes", 30, match=lambda name: name.startswith("images/"))
    if g == "none_logo":
        return pick_files(g, "tzvc/organization-logos", 42, match=lambda name: name.startswith("data/chunk_0/"))
    if g == "none_book":
        return (pick_files(g, "nadakandrew/booksdata", 29)
                + pick_parquet(g, "marvy/book-covers", "data/train-00000-of-00001.parquet", 14))
    if g == "none_galaxy":
        return pick_parquet(g, "matthieulel/galaxy10_decals", "data/test-00000-of-00001-cef82967b9d8e57e.parquet", 0,
                            meta=["label"], key=lambda m: m["label"], per_key=4)
    if g == "none_microscopy":
        return pick_parquet(g, "bloombio/phytoplankton-microscopy", "classification/validation-00000-of-00002.parquet", 42)
    if g == "none_floorplan":
        return (pick_files(g, "umesh16071973/Floorplan_Dataset_22022024", 21)
                + pick_files(g, "Voxel51/FloorPlanCAD", 21, match=lambda name: name.startswith("data/")))
    if g == "none_banknote":
        return pick_files_per_class(g, "paowick/Banknote_thai", 8,
                                    lambda name: name.split("/")[1] if name.startswith("validation/") else None)
    if g == "none_tools":
        return pick_files_per_class(g, "shravya11/mechanical-tools-dataset", 6,
                                    lambda name: name.split("/")[0] if name.split("/")[0] in TOOL_CLASSES else None)
    if g == "none_cards":
        return pick_files_per_class(g, "JackFurby/playing-cards", 21,
                                    lambda name: name.split("/")[1] if name.split("/")[1] in ("single", "three") else None)
    if g == "none_parrot":
        return pick_files(g, "Rezuwan/Parrot60_Dataset", 42, max_size=3_000_000)
    if g == "none_gadget":
        return pick_files_per_class(g, "LakshC/SynthBench", 7,
                                    lambda name: name.split("/")[2] if name.startswith("data/raw/") else None)
    raise ValueError(group)


# ---------------------------------------------------------------- videos

MIXKIT = "FastVideo/Mixkit-Src"


def mixkit(group: str, n: int, folders: tuple[str, ...], words: str, source: str,
           exclude: str = "") -> list[tuple[str, bytes, str]]:
    """Mixkit stock clips (6-second cuts, <folder>/mixkit-<title>-<id>_clip_<k>.mp4). The folder is a search word
    and is noisy, so the title must also contain one of `words`; at most ONE clip per title; files up to 6 MB."""
    wanted, unwanted = re.compile(rf"\b({words})\b"), re.compile(rf"\b({exclude})\b") if exclude else None
    by_title: dict[str, list[str]] = defaultdict(list)
    for name, size in repo_files(MIXKIT):
        folder, _, base = name.partition("/")
        if folder not in folders or not base.endswith(".mp4") or size > 6_000_000:
            continue
        title = re.sub(r"^mixkit-|-\d+_clip_\d+\.mp4$", "", base).replace("-", " ")
        if wanted.search(title) and not (unwanted and unwanted.search(title)):
            by_title[title].append(name)
    r = rng(group, f"mixkit/{source}")
    titles = sorted(r.sample(sorted(by_title), min(n, len(by_title))))
    return read_files(MIXKIT, [sorted(by_title[title])[0] for title in titles])


TIKTOK = "Shofo/shofo-tiktok-general-small"
_tiktok_cache: dict = {}


def tiktok(group: str, n: int, tags: str, exclude: str = "") -> list[tuple[str, bytes, str]]:
    """TikTok videos whose description carries one of the hashtags `tags`; not AI-generated, not an ad,
    5-60 seconds, file up to 8 MB. Hashtags are noisy, so this source is cleaned by eye more than the others."""
    if "rows" not in _tiktok_cache:
        import pyarrow.parquet as pq

        columns = ["file_name", "description", "duration_ms", "is_ai_generated", "is_ad"]
        _tiktok_cache["rows"] = pq.ParquetFile(dl(TIKTOK, "metadata.parquet")).read(columns=columns).to_pylist()
        _tiktok_cache["size"] = dict(repo_files(TIKTOK))
    wanted = re.compile(rf"#({tags})(?![a-z0-9_])")
    unwanted = re.compile(rf"#({exclude})(?![a-z0-9_])") if exclude else None
    ok = []
    for m in _tiktok_cache["rows"]:
        text = (m["description"] or "").lower()
        size = _tiktok_cache["size"].get(m["file_name"], 0)
        if (wanted.search(text) and not (unwanted and unwanted.search(text)) and not m["is_ai_generated"]
                and not m["is_ad"] and 5_000 <= (m["duration_ms"] or 0) <= 60_000 and 0 < size <= 8_000_000):
            ok.append(m["file_name"])
    return read_files(TIKTOK, sorted(rng(group, "tiktok").sample(sorted(ok), min(n, len(ok)))))


def ucf101(group: str, classes: tuple[str, ...], per_class: int) -> list[tuple[str, bytes, str]]:
    """UCF101 subset (10 classes, .avi 320x240): per_class random clips of each class, from DIFFERENT recording
    groups (the g01..g25 in the file name) where possible."""
    repo, archive = "sayakpaul/ucf101-subset", "UCF101_subset.tar.gz"
    r = rng(group, "ucf101")
    with tarfile.open(dl(repo, archive)) as tar:
        members = {m.name: m for m in tar if m.isfile() and m.name.endswith(".avi")}
        chosen = []
        for label in sorted(classes):
            by_recording: dict[str, list[str]] = defaultdict(list)
            for name in sorted(members):
                if name.split("/")[-2] == label:
                    by_recording[re.search(r"_g(\d+)_", name).group(1)].append(name)
            for recording in sorted(r.sample(sorted(by_recording), min(per_class, len(by_recording)))):
                chosen.append(r.choice(sorted(by_recording[recording])))
        return [(f"{repo}:{archive}:{name}", tar.extractfile(members[name]).read(), ".avi") for name in chosen]


def kinetics(group: str, classes: tuple[str, ...], per_class: int) -> list[tuple[str, bytes, str]]:
    return pick_files_per_class(group, "nateraw/kinetics-mini", per_class,
                                lambda name: name.split("/")[1] if name.split("/")[1] in classes else None,
                                suffixes=(".mp4",))


def build_videos(group: str) -> list[tuple[str, bytes, str]]:
    g = group
    if g == "人像":
        return mixkit(g, 16, ("Man", "Woman", "Girl"), "man|woman|girl|boy", "person",
                      exclude="and|couple|friends|group|people|women|men|girls|family|dog|cat|horse|car|city|"
                              "beach|sea|mountain|forest|food|eating|cooking|guitar|piano|dancing|soccer|basketball")
    if g == "合照":
        return mixkit(g, 18, ("Family", "Party", "People", "Couple"),
                      "family|friends|group of friends|group of people|couple")
    if g == "貓":
        return mixkit(g, 12, ("Cats", "Pets"), "cat|cats|kitten|kittens", "cat", exclude="dog|dogs") \
            + tiktok(g, 8, "cat|cats|catsoftiktok|kitten|kitty|catlover|gato|gatos|gatitos|gatito", exclude="dog|dogs|perro")
    if g == "狗":
        return mixkit(g, 12, ("Dogs", "Pets"), "dog|dogs|puppy|puppies", "dog", exclude="cat|cats") \
            + tiktok(g, 8, "dog|dogs|dogsoftiktok|puppy|doglover|perro|perros|perritos|perrito", exclude="cat|cats|gato")
    if g == "風景":
        return mixkit(g, 18, ("mountain", "forest", "beach", "sea", "sky"),
                      "mountain|mountains|forest|beach|sea|ocean|lake|waterfall|river|valley|landscape|coast|sunset",
                      "nature", exclude="man|woman|girl|boy|people|couple|family|friends|city|car|boat|ship|dog|"
                                        "surfer|surfers|surfing|person|tourists|road|house|building|bridge|drone")
    if g == "城市建築":
        return mixkit(g, 18, ("City", "Street"), "city|street|streets|building|buildings|skyline|downtown|skyscrapers",
                      "city", exclude="man|woman|girl|boy|couple|family|friends|car|cars|traffic|bus|train|bike|"
                                      "bicycle|dog|walking|running|dancing|skateboard|taxi|motorcycle")
    if g == "美食":
        return tiktok(g, 22, "food|foodtiktok|foodie|recipe|recipes|cooking|comida|receta|recetas|baking|mukbang|"
                             "foodtok|easyrecipe|recetasfaciles|cocina|dessert|postres")
    if g == "花草植物":
        return mixkit(g, 18, ("flower",), "flower|flowers|rose|roses|tulips|sunflower|sunflowers|plant|plants|"
                                         "daisies|blossom|blossoms|leaves|garden|lavender|poppies",
                      "flower", exclude="man|woman|girl|boy|couple|family|bee|bees|butterfly|bride|wedding|hands|hand")
    if g == "交通工具":
        return (mixkit(g, 14, ("Car", "Trains", "Airplane", "Bicycle", "Truck", "Motocycle"),
                       "car|cars|train|trains|airplane|plane|truck|motorcycle|bus|helicopter|tram|subway", "vehicle",
                       exclude="man|woman|girl|boy|couple|family|friends|drone|toy")
                + tiktok(g, 8, "car|cars|carsoftiktok|truck|trucks|moto|motorcycle|carcommunity|jdm|trucktok|biker"))
    if g == "none_sports":
        return (ucf101(g, ("Archery", "BalanceBeam", "BaseballPitch", "Basketball", "BasketballDunk", "BenchPress"), 3)
                + kinetics(g, ("bowling", "flying_kite", "high_jump"), 2))
    if g == "none_music":
        return (ucf101(g, ("BandMarching",), 4) + kinetics(g, ("marching",), 2)
                + mixkit(g, 10, ("Music",), "guitar|piano|drums|drum|violin|dj|saxophone|band|concert|keyboard|"
                                           "orchestra|cello|trumpet|microphone|vinyl|turntable", "music"))
    if g == "none_misc":
        return (mixkit(g, 6, ("fire",), "fire|flames|bonfire|campfire|fireplace|candle|candles", "fire",
                       exclude="man|woman|girl|boy|people|friends|firefighter|firefighters|fireworks")
                + mixkit(g, 5, ("smoke",), "smoke|ink|steam", "smoke", exclude="man|woman|girl|boy|smoking|people")
                + mixkit(g, 5, ("Fish", "Reptiles"), "fish|fishes|jellyfish|turtle|snake|lizard|iguana|crocodile|shark",
                         "aquatic", exclude="man|woman|fishing|fisherman|boat")
                + mixkit(g, 5, ("earth",), "earth|planet|globe|space|moon|stars|galaxy", "space")
                + tiktok(g, 10, "gaming|fortnite|minecraft|roblox|gta|drawing|diy|slime|satisfying|asmr|crochet|"
                                "woodworking|pottery|nails|nailart"))
    raise ValueError(group)


# ---------------------------------------------------------------- output

def thumbnail(path: Path, cell: int):
    """A picture of the file for the contact sheet (first frame of an animation, 3 frames of a video)."""
    from PIL import Image, ImageOps

    if path.suffix.lower() in VIDEO_SUFFIXES:
        import av

        frames = []
        with av.open(str(path)) as container:
            stream = container.streams.video[0]
            total = stream.frames or 0
            wanted = {0, total // 2, max(total - 5, 0)} if total else {0, 30, 90}
            for index, frame in enumerate(container.decode(stream)):
                if index in wanted:
                    frames.append(frame.to_image())
                if len(frames) == 3 or index > max(wanted):
                    break
        strip = Image.new("RGB", (cell, cell), "black")
        for i, frame in enumerate(frames):
            frame.thumbnail((cell, cell // 3))
            strip.paste(frame, ((cell - frame.width) // 2, i * (cell // 3)))
        return strip
    image = ImageOps.exif_transpose(Image.open(path))
    if getattr(image, "is_animated", False):
        image.seek(0)
    if image.mode in ("RGBA", "LA", "P"):
        rgba = image.convert("RGBA")
        image = Image.new("RGB", rgba.size, (255, 200, 255))  # transparency shows as pink on the sheet
        image.paste(rgba, mask=rgba.split()[3])
    image = image.convert("RGB")
    image.thumbnail((cell, cell))
    return image


def contact_sheet(folder: Path, out: Path, cell: int = 200, columns: int = 8) -> None:
    from PIL import Image, ImageDraw

    files = sorted(p for p in folder.iterdir() if p.is_file())
    rows = (len(files) + columns - 1) // columns
    sheet = Image.new("RGB", (columns * cell, max(rows, 1) * (cell + 16)), "white")
    draw = ImageDraw.Draw(sheet)
    for i, path in enumerate(files):
        x, y = (i % columns) * cell, (i // columns) * (cell + 16)
        try:
            image = thumbnail(path, cell)
            sheet.paste(image, (x + (cell - image.width) // 2, y + 16 + (cell - image.height) // 2))
        except Exception as exc:  # noqa: BLE001 - a file the viewer cannot open still gets its cell
            draw.text((x + 4, y + 40), f"unreadable:\n{str(exc)[:60]}", fill="black")
        draw.text((x + 4, y + 2), path.name[-30:], fill="red")
    sheet.save(out, quality=85)


def cached_build(kind: str, group: str, cache: Path | None):
    """build_images / build_videos, with the downloaded bytes of each group kept in --cache between runs."""
    build = build_images if kind == "images" else build_videos
    if cache is None:
        return build(group)
    folder = cache / kind / group
    index = folder / "index.json"
    if index.exists():
        return [(key, (folder / name).read_bytes(), ext)
                for key, name, ext in json.loads(index.read_text(encoding="utf-8"))]
    items = build(group)
    folder.mkdir(parents=True, exist_ok=True)
    listing = []
    for key, data, ext in items:
        name = hashlib.sha1(key.encode("utf-8")).hexdigest()
        (folder / name).write_bytes(data)
        listing.append((key, name, ext))
    index.write_text(json.dumps(listing, ensure_ascii=False), encoding="utf-8")
    return items


def write_set(kind: str, groups: list[str], all_groups: list[str], out: Path, removed: dict, moved: dict,
              raw: bool, sheets: Path | None, cache: Path | None) -> None:
    out.mkdir(parents=True, exist_ok=True)
    manifest_path = out / "manifest.tsv"
    manifest = {}
    if manifest_path.exists():
        for line in manifest_path.read_text(encoding="utf-8").split("\n"):
            if line:
                final, name, picked, key, state = line.split("\t")
                manifest[(picked, key)] = (final, name, state)
    touched = set()
    for group in groups:
        for key in [k for k in manifest if k[0] == group]:  # forget this group's earlier files, wherever they went
            final, name, _ = manifest.pop(key)
            if name and (out / final / name).exists():
                (out / final / name).unlink()
            touched.add(final)
        (out / group).mkdir(exist_ok=True)
        gone = set() if raw else set(removed.get(group, []))
        elsewhere = {} if raw else moved.get(group, {})
        kept = 0
        for number, (source, data, ext) in enumerate(cached_build(kind, group, cache)):
            name = f"{number:03d}{ext}"
            if name in gone:
                manifest[(group, source)] = (group, name, "removed")
                continue
            final = elsewhere.get(name, group)
            stored = name if final == group else f"{group}_{name}"
            (out / final).mkdir(exist_ok=True)
            (out / final / stored).write_bytes(data)
            manifest[(group, source)] = (final, stored, "kept" if final == group else "moved")
            touched.add(final)
            kept += 1
        print(f"{kind} {group}: {kept} kept ({len(elsewhere)} of them moved), {len(gone)} removed", flush=True)
    if sheets:
        sheets.mkdir(parents=True, exist_ok=True)
        for group in sorted(touched):
            if (out / group).is_dir():
                contact_sheet(out / group, sheets / f"{kind}_{group}.jpg")
    order = {g: i for i, g in enumerate(all_groups)}
    rows = sorted(((final, name, picked, key, state) for (picked, key), (final, name, state) in manifest.items()),
                  key=lambda x: (order.get(x[0], len(order)), x[0], x[1], x[3]))
    manifest_path.write_text("\n".join("\t".join(row) for row in rows) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True, help="output folder (outside the repo), e.g. /opt/holdout2")
    parser.add_argument("--images", nargs="*", default=None, help="image groups to (re)build; default: all (--images alone: none)")
    parser.add_argument("--videos", nargs="*", default=None, help="video groups to (re)build; default: all (--videos alone: none)")
    parser.add_argument("--raw", action="store_true", help="do not apply the by-eye REMOVED / MOVED lists")
    parser.add_argument("--sheets", default="", help="also write one contact-sheet JPEG per group into this folder")
    parser.add_argument("--cache", default="", help="keep the downloaded bytes of every group in this folder")
    args = parser.parse_args()
    try:
        import pillow_heif

        pillow_heif.register_heif_opener()
    except ImportError:
        pass
    out = Path(args.out)
    sheets = Path(args.sheets) if args.sheets else None
    cache = Path(args.cache) if args.cache else None
    images = IMAGE_GROUPS if args.images is None else args.images  # "--images" alone = no image group
    videos = VIDEO_GROUPS if args.videos is None else args.videos
    if images:
        write_set("images", images, IMAGE_GROUPS, out / "images", REMOVED_IMAGES, MOVED_IMAGES, args.raw, sheets, cache)
    if videos:
        write_set("videos", videos, VIDEO_GROUPS, out / "videos", REMOVED_VIDEOS, MOVED_VIDEOS, args.raw, sheets, cache)


if __name__ == "__main__":
    main()
