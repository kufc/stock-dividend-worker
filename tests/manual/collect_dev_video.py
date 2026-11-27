"""Rebuild the DEVELOPMENT set of videos described in dev_video_sources.txt (not an automated test).

This is a DEV set: it may be looked at and used to compare / tune video handling (frame sampling, how frame
results are combined, thresholds for videos). Numbers measured on it are NOT test accuracy and must never be
reported as such - the test set for videos is holdout2 (collect_holdout2.py), which shares no source with this set.
Nothing in this script runs the classifier.

    pip install huggingface_hub pyarrow Pillow av
    python tests/manual/collect_dev_video.py --out /opt/devvideo --sheets /opt/devvideo/sheets

What it does
  1. Reads the metadata of three public Hugging Face datasets (pinned to the commit hashes in REV): MSVD and
     MSR-VTT (YouTube clips with human captions; the two video zips are downloaded whole, 1.8 + 2.2 GB) and a
     captioned subset of Pexels stock videos (single files are fetched out of the tar shards with HTTP range
     requests; nothing else of the 134 GB is downloaded).
  2. Picks clips at random using only captions / category labels / sizes / durations, with fixed seeds:
     random.Random(f"{SEED}:{group}:{source}") - string seeds are stable across Python versions.
     A clip is used by ONE group only (groups are built in the order of GROUPS; a later group skips clips an
     earlier group took), so all groups are always built together.
  3. Saves them with their ORIGINAL BYTES and extension as <out>/videos/<group>/NNN<ext> and writes
     <out>/videos/manifest.tsv (columns: final group, file, picked-for group, source key, state).
  4. Applies the by-eye review done on contact sheets (reasons in dev_video_sources.txt):
       REMOVED - deleted: unrecognisable, drawn / game / screen content, or between "no default category" and a
                 default category, or between three categories.
       MOVED   - the caption-based label was wrong or incomplete: the file is stored under the group it really
                 belongs to (a default category, an "A+B" pair, or a none_ group) as <picked-for group>_<NNN><ext>.
     Use --raw to see the raw random selection.

Folder name == a default category -> that is the expected label; "A+B" -> either A or B is correct;
none_xxx -> belongs to no default category (same convention as collect_holdout2.py).
"""

from __future__ import annotations

import argparse
import io
import json
import random
import re
import zipfile
from collections import defaultdict
from pathlib import Path

SEED = 20261127

# dataset repo -> commit hash used when the set was built
REV = {
    "friedrichor/MSVD": "9aa95a3039c106ee9e0969d82d7ce5a9efab1196",
    "friedrichor/MSR-VTT": "c1af215a96934854f42683c19c51391aaee6f962",
    "zengxianyu/open-sora-pexels-subset": "c595493c834ec2c47ceee5d18ecca002aa646540",
}

# The groups that are BUILT, in build order (the folder a file is picked for). The by-eye review below moves some
# files into further folders that are not built directly (most of the "A+B" pairs).
GROUPS = ["貓", "狗", "人像+貓", "人像+狗", "合照", "人像", "美食", "花草植物", "交通工具", "風景", "城市建築",
          "none_sports", "none_music", "none_dance", "none_machine", "none_underwater", "none_wildlife"]

MIN_SECONDS, MAX_SECONDS = 2.0, 60.0
MAX_BYTES = 40_000_000

# ---------------------------------------------------------------------------------------------------------------
# By-eye review (contact sheets), applied after the random selection. Keys are the names of the RAW selection
# (<picked-for group>/<NNN><ext>). See dev_video_sources.txt for the reasons.
REMOVED: dict[str, list[str]] = {
    "人像+貓": ["007.avi"],
    "合照": ["001.mp4", "017.mp4", "022.mp4", "026.mp4"],
    "人像": ["005.mp4", "006.mp4", "009.mp4", "011.mp4", "018.mp4", "021.mp4", "024.mp4", "027.avi"],
    "美食": ["003.mp4"],
    "花草植物": ["002.mp4", "006.mp4", "007.mp4", "011.mp4"],
    "交通工具": ["004.mp4", "008.mp4", "009.mp4", "015.mp4"],
    "風景": ["022.mp4"],
    "城市建築": ["002.mp4", "021.mp4", "025.mp4"],
    "none_sports": ["002.mp4", "004.mp4"],
    "none_music": ["001.mp4"],
    "none_dance": ["011.mp4"],
    "none_machine": ["001.mp4", "002.mp4", "007.mp4", "009.mp4", "016.mp4", "017.mp4"],
    "none_underwater": ["007.mp4", "008.mp4", "011.mp4", "014.mp4"],
    "none_wildlife": ["003.avi", "005.avi", "010.mp4"],
}
MOVED: dict[str, dict[str, str]] = {
    "貓": {
        **dict.fromkeys(["010.mp4", "012.mp4", "016.mp4", "017.mp4"], "人像+貓"),
        **dict.fromkeys(["011.mp4"], "合照+貓"),
    },
    "狗": {
        **dict.fromkeys(["004.avi", "015.mp4", "016.mp4"], "人像+狗"),
        **dict.fromkeys(["009.mp4"], "合照+狗"),
    },
    "人像+貓": {
        **dict.fromkeys(["000.mp4", "001.mp4", "002.mp4", "003.mp4", "004.mp4", "006.avi"], "貓"),
    },
    "人像+狗": {
        **dict.fromkeys(["004.mp4", "007.avi"], "狗"),
    },
    "合照": {
        **dict.fromkeys(["007.mp4"], "人像+風景"),
        **dict.fromkeys(["014.mp4"], "合照+美食"),
        **dict.fromkeys(["025.mp4", "032.avi", "034.avi"], "人像"),
        **dict.fromkeys(["028.mp4"], "合照+城市建築"),
        **dict.fromkeys(["030.avi"], "合照+花草植物"),
        **dict.fromkeys(["033.avi"], "none_dance"),
    },
    "人像": {
        **dict.fromkeys(["025.avi"], "人像+風景"),
        **dict.fromkeys(["028.avi"], "合照"),
    },
    "美食": {
        **dict.fromkeys(["001.mp4", "005.mp4"], "花草植物"),
        **dict.fromkeys(["002.mp4"], "美食+花草植物"),
        **dict.fromkeys(["008.mp4", "016.mp4"], "人像"),
        **dict.fromkeys(["012.mp4", "014.mp4"], "合照+美食"),
        **dict.fromkeys(["013.mp4", "015.mp4", "017.mp4"], "人像+美食"),
    },
    "花草植物": {
        **dict.fromkeys(["000.mp4"], "城市建築"),
        **dict.fromkeys(["001.mp4"], "風景+花草植物"),
        **dict.fromkeys(["014.mp4"], "城市建築+花草植物"),
    },
    "交通工具": {
        **dict.fromkeys(["000.mp4", "011.mp4", "013.mp4", "016.mp4"], "人像+交通工具"),
        **dict.fromkeys(["002.mp4", "003.mp4"], "風景+交通工具"),
        **dict.fromkeys(["012.mp4", "022.mp4", "025.avi", "026.avi"], "合照+交通工具"),
        **dict.fromkeys(["014.mp4"], "人像"),
        **dict.fromkeys(["019.mp4"], "城市建築+交通工具"),
    },
    "風景": {
        **dict.fromkeys(["007.mp4"], "風景+花草植物"),
        **dict.fromkeys(["020.mp4"], "花草植物"),
        **dict.fromkeys(["021.mp4"], "人像+風景"),
    },
    "城市建築": {
        **dict.fromkeys(["004.mp4", "011.mp4"], "風景+城市建築"),
        **dict.fromkeys(["012.mp4", "016.mp4"], "城市建築+交通工具"),
        **dict.fromkeys(["024.mp4"], "合照+城市建築"),
    },
    "none_sports": {
        **dict.fromkeys(["014.mp4"], "風景"),
        **dict.fromkeys(["016.mp4", "019.mp4"], "人像"),
        **dict.fromkeys(["017.mp4"], "合照+風景"),
        **dict.fromkeys(["018.mp4"], "合照"),
    },
    "none_music": {
        **dict.fromkeys(["000.mp4"], "人像"),
    },
    "none_dance": {
        **dict.fromkeys(["010.mp4"], "none_sports"),
        **dict.fromkeys(["012.mp4"], "合照"),
        **dict.fromkeys(["014.mp4"], "人像"),
    },
    "none_machine": {
        **dict.fromkeys(["000.mp4"], "none_dance"),
        **dict.fromkeys(["004.mp4", "011.mp4"], "人像"),
        **dict.fromkeys(["006.mp4"], "人像+城市建築"),
        **dict.fromkeys(["010.mp4"], "合照"),
    },
    "none_underwater": {
        **dict.fromkeys(["001.mp4"], "美食+花草植物"),
        **dict.fromkeys(["012.mp4"], "none_wildlife"),
        **dict.fromkeys(["015.mp4"], "人像"),
    },
    "none_wildlife": {
        **dict.fromkeys(["001.avi", "002.avi", "008.mp4"], "none_sports"),
        **dict.fromkeys(["007.mp4"], "合照"),
    },
}

VIDEO_SUFFIXES = (".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v")


def rng(group: str, source: str) -> random.Random:
    return random.Random(f"{SEED}:{group}:{source}")


def dl(repo: str, filename: str) -> str:
    from huggingface_hub import hf_hub_download

    return hf_hub_download(repo, filename, repo_type="dataset", revision=REV[repo])


def seconds(data: bytes) -> float:
    """Duration of the video stream as PyAV reports it (0 when it cannot be read)."""
    import av

    try:
        with av.open(io.BytesIO(data)) as container:
            stream = container.streams.video[0]
            if stream.duration and stream.time_base:
                return float(stream.duration * stream.time_base)
            return float(container.duration or 0) / 1_000_000
    except Exception:  # noqa: BLE001 - an unreadable file is simply not selected
        return 0.0


def share(captions: list[str], pattern: re.Pattern) -> float:
    """Share of a clip's captions that match the pattern."""
    return sum(1 for c in captions if pattern.search(c)) / max(len(captions), 1)


def take(group: str, source: str, candidates: list[str], n: int, fetch, used: set[str], tag: str = "") -> list[tuple[str, bytes, str]]:
    """The first n clips, in a seeded random order of the (sorted) candidates, that no earlier group used, are
    not larger than MAX_BYTES and last MIN_SECONDS..MAX_SECONDS. fetch(candidate) -> (source key, bytes, ext).
    `tag` only changes the seed (used by the top-ups, which have rules of their own)."""
    order = sorted(candidates)
    rng(group, source + tag).shuffle(order)
    out = []
    for candidate in order:
        if len(out) == n:
            break
        if f"{source}:{candidate}" in used:
            continue
        key, data, ext = fetch(candidate)
        if len(data) > MAX_BYTES or not MIN_SECONDS <= seconds(data) <= MAX_SECONDS:
            continue
        used.add(f"{source}:{candidate}")
        out.append((key, data, ext))
    return out


# ---------------------------------------------------------------- word lists shared by the caption rules

DRAWN = re.compile(r"\b(cartoons?|animated|animation|anime|video ?games?|game ?play|gameplay|minecraft|computer game|"
                   r"cgi|3d|drawing|drawn|sketch|clip ?art|illustration|rendered|rendering|simulation|screen ?shot|"
                   r"slide ?show|slideshow|powerpoint|trailer|commercial|advertisement|logo|text|graphic|graphics|"
                   r"infographic|subtitles?|lyrics|toy|toys|lego|doll|dolls|puppets?|stuffed)\b", re.I)
PERSON = re.compile(r"\b(man|woman|girl|boy|guy|lady|person|someone|somebody|kid|child|baby|toddler|teenager|he|she|"
                    r"chef|cook|host|player|singer|dancer|driver|rider|owner|hands?|fingers?|his|her)\b", re.I)
PEOPLE = re.compile(r"\b(people|persons|men|women|girls|boys|guys|ladies|kids|children|babies|friends|family|couple|"
                    r"group|crowd|audience|team|teams|players|dancers|singers|band|students|two|three|four|five|"
                    r"several|some|they|their|together|each other|another|others)\b", re.I)
CAT = re.compile(r"\b(cat|cats|kitten|kittens|kitty)\b", re.I)
DOG = re.compile(r"\b(dog|dogs|puppy|puppies|pup)\b", re.I)
OTHER_ANIMAL = re.compile(r"\b(squirrels?|chipmunks?|monkeys?|pandas?|bears?|elephants?|tigers?|lions?|horses?|ponies|"
                          r"pony|birds?|parrots?|ducks?|rabbits?|bunny|bunnies|hamsters?|goats?|cows?|sheep|deer|"
                          r"foxes|fox|raccoons?|otters?|penguins?|owls?|eagles?|snakes?|lizards?|turtles?|tortoises?|"
                          r"frogs?|pigs?|piglets?|giraffes?|zebras?|kangaroos?|koalas?|gorillas?|chimpanzees?|"
                          r"chimps?|lemurs?|hedgehogs?|ferrets?|sloths?|camels?|llamas?|alpacas?|chickens?|hens?|"
                          r"roosters?|geese|goose|swans?|seals?|wolves|wolf|leopards?|cheetahs?|rhinos?|hippos?|"
                          r"crocodiles?|alligators?|insects?|butterfl(y|ies)|bees?|spiders?|ants?|animals?)\b", re.I)
SEA_ANIMAL = re.compile(r"\b(fish|fishes|sharks?|whales?|dolphins?|jellyfish|octopus|coral|corals|reef|sea turtles?|"
                        r"stingrays?|rays?|seahorses?|eels?|underwater|aquarium|scuba|divers?|diving|snorkel\w*)\b", re.I)
FOOD = re.compile(r"\b(food|dish|dishes|meal|recipe|cooking|cooks?|baking|bakes?|frying|fries|fried|grilling|"
                  r"boiling|slicing|slices?|chopping|chops?|peeling|peels?|mixing|stirring|stirs?|kitchen|"
                  r"ingredients?|noodles?|pasta|soup|salad|pizza|burgers?|sandwich(es)?|cakes?|cookies?|bread|"
                  r"dough|eggs?|meat|chicken|beef|pork|steak|shrimps?|rice|vegetables?|onions?|potato(es)?|"
                  r"tomato(es)?|garlic|carrots?|cheese|butter|sauce|chocolate|dessert|pastry|pastries|pan|pot|"
                  r"plate|bowl|coffee|tea|juice|cocktails?|drinks?|beverages?|fruits?|sushi|dumplings?)\b", re.I)
VEHICLE = re.compile(r"\b(car|cars|truck|trucks|motorcycles?|motorbikes?|bus|buses|trains?|locomotive|airplanes?|"
                     r"planes?|aircraft|jet|helicopters?|boats?|ships?|yachts?|sailboats?|ferry|bicycles?|bikes?|"
                     r"scooters?|vans?|suv|jeep|tractors?|trams?|subway|vehicles?|taxi|taxis)\b", re.I)
LAND = re.compile(r"\b(mountains?|lake|waterfalls?|beach|ocean|sea|river|valley|forest|woods|cliffs?|canyon|glacier|"
                  r"meadow|hills?|dunes|desert|coast|coastline|shore|shoreline|waves|sunset|sunrise|clouds|sky|"
                  r"landscape|seascape|countryside|fields?|snowy|island|stream|creek)\b", re.I)
BUILDING = re.compile(r"\b(city|cityscape|skyline|skyscrapers?|buildings?|towers?|street|streets|downtown|urban|"
                      r"bridge|church|cathedral|temple|mosque|castle|palace|architecture|houses?|village|town|"
                      r"alley|square|plaza|monument|facade|rooftops?)\b", re.I)
PLANT = re.compile(r"\b(flowers?|blossoms?|petals?|roses?|tulips?|sunflowers?|dais(y|ies)|lavender|orchids?|lotus|"
                   r"lil(y|ies)|popp(y|ies)|plants?|leaves|leaf|ferns?|cactus|cacti|succulents?|grass|wheat|moss|"
                   r"bamboo|trees?|branch|branches|foliage|garden|bloom\w*)\b", re.I)
SPORT = re.compile(r"\b(soccer|football|basketball|baseball|tennis|golf|hockey|rugby|cricket|volleyball|boxing|"
                   r"wrestling|wrestlers?|swimming|swimmers?|gymnast\w*|skateboard\w*|skiing|skiers?|snowboard\w*|"
                   r"surfing|surfers?|race|racing|running|runners?|sprint\w*|athletes?|match|goal|score[sd]?|"
                   r"ping pong|table tennis|badminton|karate|martial arts|judo|fencing|archery|bowling|"
                   r"weightlifting|track and field|pole vault|high jump|long jump|marathon|cycling|cyclists?|"
                   r"stadium|sports?|players?)\b", re.I)
MUSIC = re.compile(r"\b(sing|sings|singing|singers?|songs?|guitars?|piano|drums?|drummers?|violins?|cello|"
                   r"saxophone|trumpet|flute|band|bands|concert|orchestra|musicians?|guitarists?|pianists?|"
                   r"stage|performs?|performing|performance|microphone|music)\b", re.I)
DANCE = re.compile(r"\b(dance|dances|dancing|dancers?|ballet|ballerinas?|choreograph\w*)\b", re.I)
MACHINE = re.compile(r"\b(machines?|machinery|factory|industrial|assembly line|conveyor|robot|robots|robotic|gears?|"
                     r"engine|engines|turbines?|lathe|cnc|welding|welder|drill|drilling|excavators?|bulldozers?|"
                     r"cranes?|forklifts?|printing press|3d printer|printer|circuit|generator|pump|pistons?|"
                     r"mechanical|mechanism|manufacturing|sewing machine|loom)\b", re.I)


def none_of(captions: list[str], *patterns: re.Pattern, limit: float = 0.15) -> bool:
    return all(share(captions, p) <= limit for p in patterns)


# ---------------------------------------------------------------- MSR-VTT (YouTube clips, 20 human captions each)

MSRVTT = "friedrichor/MSR-VTT"
# category numbers of raw_data/category.txt
VTT = {"music": 0, "people": 1, "gaming": 2, "sports": 3, "news": 4, "education": 5, "tv": 6, "movie": 7,
       "animation": 8, "vehicles": 9, "howto": 10, "travel": 11, "science": 12, "animals": 13, "kids": 14,
       "documentary": 15, "food": 16, "cooking": 17, "beauty": 18, "ads": 19}
_cache: dict = {}


def msrvtt_meta() -> dict[str, tuple[int, float, list[str]]]:
    """video id -> (category number, clip length in seconds, the 20 captions)."""
    if "msrvtt" not in _cache:
        with open(dl(MSRVTT, "raw_data/MSRVTT_data.json"), encoding="utf-8") as f:
            data = json.load(f)
        captions: dict[str, list[str]] = defaultdict(list)
        for s in sorted(data["sentences"], key=lambda s: s["sen_id"]):
            captions[s["video_id"]].append(s["caption"])
        _cache["msrvtt"] = {v["video_id"]: (v["category"], v["end time"] - v["start time"], captions[v["video_id"]])
                            for v in data["videos"]}
    return _cache["msrvtt"]


def zip_of(repo: str, name: str) -> zipfile.ZipFile:
    if (repo, name) not in _cache:
        _cache[(repo, name)] = zipfile.ZipFile(dl(repo, name))
    return _cache[(repo, name)]


def msrvtt(group: str, n: int, rule, used: set[str], categories: tuple[str, ...] = (), tag: str = "") -> list:
    """n random MSR-VTT clips for which rule(captions) is true (and whose category is one of `categories`).
    Clips of the categories gaming / animation / advertisement are never used."""
    wanted = {VTT[c] for c in categories}
    banned = {VTT["gaming"], VTT["animation"], VTT["ads"]}
    ok = [vid for vid, (category, _, captions) in msrvtt_meta().items()
          if category not in banned and (not wanted or category in wanted)
          and share(captions, DRAWN) <= 0.1 and rule(captions)]

    def fetch(vid: str):
        return f"{MSRVTT}:MSRVTT_Videos.zip:video/{vid}.mp4", zip_of(MSRVTT, "MSRVTT_Videos.zip").read(f"video/{vid}.mp4"), ".mp4"

    return take(group, "msrvtt", ok, n, fetch, used, tag)


# ---------------------------------------------------------------- MSVD (YouTube clips, ~40 human captions each)

MSVD = "friedrichor/MSVD"


def msvd_meta() -> dict[str, list[str]]:
    """clip name (<youtube id>_<start>_<end>) -> its English captions."""
    if "msvd" not in _cache:
        captions: dict[str, list[str]] = defaultdict(list)
        with open(dl(MSVD, "raw_data/AllVideoDescriptions.txt"), encoding="utf-8") as f:
            for line in f:
                if line.strip() and not line.startswith("#"):
                    name, _, text = line.strip().partition(" ")
                    captions[name].append(text)
        present = {Path(i.filename).stem for i in zip_of(MSVD, "MSVD_Videos.zip").infolist()}
        _cache["msvd"] = {name: texts for name, texts in captions.items() if name in present}
    return _cache["msvd"]


def msvd(group: str, n: int, rule, used: set[str]) -> list:
    """n random MSVD clips for which rule(captions) is true."""
    ok = [name for name, captions in msvd_meta().items() if share(captions, DRAWN) <= 0.1 and rule(captions)]

    def fetch(name: str):
        member = f"YouTubeClips/{name}.avi"
        return f"{MSVD}:MSVD_Videos.zip:{member}", zip_of(MSVD, "MSVD_Videos.zip").read(member), ".avi"

    return take(group, "msvd", ok, n, fetch, used)


# ---------------------------------------------------------------- Pexels stock videos (machine captions)

PEXELS = "zengxianyu/open-sora-pexels-subset"
PEXELS_MAX_BYTES = 30_000_000  # keeps the set small; the general limit is MAX_BYTES


def pexels_meta() -> dict[str, tuple[str, int, int, str, str]]:
    """sample id -> (shard, byte offset of the file inside the shard, size, first sentence of the first caption,
    all captions joined). manifest.parquet lists the members of every tar shard in archive order, so the offset
    follows from the sizes (512-byte header, data padded to 512 bytes); fetch() checks the header."""
    if "pexels" not in _cache:
        import pyarrow.parquet as pq

        captions: dict[str, list[tuple[int, str]]] = defaultdict(list)
        for row in pq.ParquetFile(dl(PEXELS, "annotations/captions.parquet")).read().to_pylist():
            captions[row["sample_id"]].append((row["annotation_idx"], row["caption"].strip()))
        meta, offset = {}, defaultdict(int)
        for row in pq.ParquetFile(dl(PEXELS, "manifest.parquet")).read().to_pylist():
            shard, size = row["shard"], row["video_size_bytes"]
            texts = [text for _, text in sorted(captions[row["sample_id"]])]
            first = re.split(r"(?<=[a-z])\. ", texts[0], maxsplit=1)[0] if texts else ""
            meta[row["sample_id"]] = (shard, offset[shard], size, first, " ".join(texts))
            offset[shard] += 512 + (size + 511) // 512 * 512
        _cache["pexels"] = meta
    return _cache["pexels"]


def pexels(group: str, n: int, rule, used: set[str], tag: str = "") -> list:
    """n random Pexels clips for which rule(first sentence, all captions) is true; files up to PEXELS_MAX_BYTES."""
    from huggingface_hub import HfFileSystem

    meta = pexels_meta()
    ok = [sid for sid, (_, _, size, first, text) in meta.items()
          if 0 < size <= PEXELS_MAX_BYTES and first and not DRAWN.search(text) and rule(first, text)]
    fs = HfFileSystem()

    def fetch(sid: str):
        shard, offset, size, _, _ = meta[sid]
        path = f"datasets/{PEXELS}@{REV[PEXELS]}/videos/{shard}"
        blob = fs.cat_file(path, start=offset, end=offset + 512 + size)
        if blob[:100].rstrip(b"\0").decode() != f"{sid}.mp4":
            raise RuntimeError(f"unexpected tar member at {shard}@{offset}")
        return f"{PEXELS}:videos/{shard}:{sid}.mp4", blob[512:], ".mp4"

    return take(group, "pexels", ok, n, fetch, used, tag)


# ---------------------------------------------------------------- rules

def mostly(pattern: re.Pattern, minimum: float):
    return lambda captions: share(captions, pattern) >= minimum


def vtt_cat(c):
    return share(c, CAT) >= 0.6 and share(c, DOG) <= 0.1 and share(c, OTHER_ANIMAL) <= 0.2


def vtt_dog(c):
    return share(c, DOG) >= 0.6 and share(c, CAT) <= 0.1 and share(c, OTHER_ANIMAL) <= 0.2


VTT_ONE = re.compile(r"^(a|an|the)\b[^,]{0,30}?\b(man|woman|girl|boy|guy|lady|person|teenager)\b", re.I)
VTT_MANY = re.compile(r"\b(people|men|women|girls|boys|guys|ladies|kids|children|friends|family|couple|group|crowd|"
                      r"two|three|four|five|several|team|students|audience)\b", re.I)
VTT_GROUP = re.compile(r"\b(group of|two (men|women|girls|boys|guys|people|ladies|friends|kids|children)|"
                       r"three (men|women|girls|boys|guys|people|ladies|friends|kids|children)|friends|family|"
                       r"couple|a man and a woman|people (are|is) (sitting|standing|talking|posing|laughing|smiling))\b",
                       re.I)
BUSY = (CAT, DOG, OTHER_ANIMAL, FOOD, VEHICLE, SPORT, MUSIC, DANCE, MACHINE, SEA_ANIMAL)


def vtt_person(c):
    return share(c, VTT_ONE) >= 0.7 and share(c, VTT_MANY) <= 0.1 and none_of(c, *BUSY)


def vtt_group(c):
    return share(c, VTT_GROUP) >= 0.5 and none_of(c, *BUSY)


def vtt_food(c):
    return share(c, FOOD) >= 0.7 and none_of(c, CAT, DOG, OTHER_ANIMAL)


def vtt_vehicle(c):
    return share(c, VEHICLE) >= 0.7 and none_of(c, CAT, DOG, OTHER_ANIMAL, SPORT, FOOD)


def vtt_sport(c):
    return share(c, SPORT) >= 0.7 and none_of(c, CAT, DOG, MUSIC, DANCE, VEHICLE)


def vtt_music(c):
    return share(c, MUSIC) >= 0.7 and none_of(c, DANCE, SPORT, CAT, DOG)


def vtt_dance(c):
    return share(c, DANCE) >= 0.6 and none_of(c, SPORT, CAT, DOG)


def vtt_sea(c):
    return share(c, SEA_ANIMAL) >= 0.6 and none_of(c, FOOD, CAT, DOG)


def vtt_wild(c):
    return share(c, OTHER_ANIMAL) >= 0.7 and none_of(c, CAT, DOG, FOOD, SEA_ANIMAL, SPORT)


PX_NO_PEOPLE = re.compile(r"\b(man|woman|girl|boy|guy|lady|person|people|men|women|child|children|kids?|baby|couple|"
                          r"friends|family|individuals?|figures?|someone|pedestrians?|tourists?|crowds?|workers?|"
                          r"he|she|his|her|hands?|fingers?|model|dancers?|players?|athletes?|silhouettes?)\b", re.I)
PX_ONE = re.compile(r"\b(a|an|one|single)\b[^.,;]{0,40}?\b(woman|man|girl|boy|lady|guy|individual|person)\b", re.I)
PX_MANY = re.compile(r"\b(two|three|four|five|six|group|couple|friends|people|crowd|women|men|children|kids|family|"
                     r"girls|boys|individuals|others|another|together|each other|companions?|both|baby|child)\b", re.I)
PX_GROUP = re.compile(r"\b(two|three|four|five|six|a group of|group of|several)\b[^.,;]{0,40}?\b(women|men|friends|"
                      r"girls|boys|people|individuals|children|kids|ladies|colleagues|adults|persons)\b|"
                      r"\b(couple|family|friends)\b", re.I)
PX_PET = re.compile(r"\b(cat|cats|kitten|kittens|dog|dogs|puppy|puppies)\b", re.I)
PX_ANY_ANIMAL = re.compile(CAT.pattern + "|" + DOG.pattern + "|" + OTHER_ANIMAL.pattern + "|" + SEA_ANIMAL.pattern, re.I)


def px_cat(first, text):
    return bool(CAT.search(first)) and not DOG.search(text) and not PX_NO_PEOPLE.search(text)


def px_dog(first, text):
    return bool(DOG.search(first)) and not CAT.search(text) and not PX_NO_PEOPLE.search(text)


def px_person_cat(first, text):
    return bool(CAT.search(first) and PX_NO_PEOPLE.search(text)) and not DOG.search(text) and not PX_MANY.search(first)


def px_person_dog(first, text):
    return bool(DOG.search(first) and PX_ONE.search(first)) and not CAT.search(text) and not PX_MANY.search(first)


def px_person(first, text):
    return (bool(PX_ONE.search(first)) and not PX_MANY.search(text) and not PX_ANY_ANIMAL.search(text)
            and not any(p.search(first) for p in (FOOD, VEHICLE, SPORT, MUSIC, DANCE, MACHINE, LAND, BUILDING, PLANT)))


def px_group(first, text):
    return (bool(PX_GROUP.search(first)) and not PX_ANY_ANIMAL.search(text)
            and not any(p.search(first) for p in (FOOD, VEHICLE, SPORT, MUSIC, DANCE, MACHINE, LAND, BUILDING, PLANT)))


def px_food(first, text):
    return bool(FOOD.search(first)) and not PX_NO_PEOPLE.search(first) and not PX_ANY_ANIMAL.search(text) \
        and not any(p.search(first) for p in (LAND, BUILDING, VEHICLE, MACHINE))


def px_plant(first, text):
    return bool(re.search(r"\b(flowers?|blossoms?|petals?|roses?|tulips?|sunflowers?|dais(y|ies)|lavender|orchids?|"
                          r"lotus|popp(y|ies)|plants?|leaves|leaf|ferns?|cactus|succulents?|wheat|moss|bamboo)\b",
                          first, re.I)) and not PX_NO_PEOPLE.search(text) and not PX_ANY_ANIMAL.search(text) \
        and not any(p.search(first) for p in (FOOD, BUILDING, VEHICLE, MACHINE))


def px_vehicle(first, text):
    return bool(VEHICLE.search(first)) and not PX_NO_PEOPLE.search(first) and not PX_ANY_ANIMAL.search(text) \
        and not any(p.search(first) for p in (FOOD, SPORT, MACHINE))


def px_land(first, text):
    return bool(LAND.search(first)) and not PX_NO_PEOPLE.search(text) and not PX_ANY_ANIMAL.search(text) \
        and not any(p.search(text) for p in (BUILDING, VEHICLE, FOOD, MACHINE)) and not PLANT.search(first)


def px_building(first, text):
    return bool(BUILDING.search(first)) and not PX_NO_PEOPLE.search(first) and not PX_ANY_ANIMAL.search(text) \
        and not any(p.search(first) for p in (LAND, VEHICLE, FOOD, MACHINE, PLANT))


PX_SPORT = re.compile(r"\b(soccer|football|basketball|baseball|tennis|golf|hockey|rugby|volleyball|boxing|boxers?|"
                      r"wrestling|swimmers?|gymnast\w*|skateboard\w*|skiing|skiers?|snowboard\w*|surfing|surfers?|"
                      r"runners?|jogging|sprint\w*|athletes?|badminton|karate|martial arts|judo|fencing|archery|"
                      r"weightlifting|marathon|cyclists?|workout|exercis\w*|yoga|push-ups|gym)\b", re.I)


def px_sport(first, text):
    return bool(PX_SPORT.search(first) and PX_NO_PEOPLE.search(first)) and not PX_ANY_ANIMAL.search(text) \
        and not any(p.search(first) for p in (MUSIC, DANCE, FOOD))


def px_music(first, text):
    return bool(re.search(r"\b(guitars?|piano|drums?|violins?|cello|saxophone|trumpet|flute|band|concert|orchestra|"
                          r"musicians?|guitarists?|pianists?|singers?|singing)\b", first, re.I)
                and PX_NO_PEOPLE.search(first)) and not PX_ANY_ANIMAL.search(text) and not DANCE.search(text)


def px_dance(first, text):
    return bool(DANCE.search(first)) and not PX_ANY_ANIMAL.search(text) and not SPORT.search(first)


def px_machine(first, text):
    return bool(MACHINE.search(first)) and not PX_ANY_ANIMAL.search(text) \
        and not any(p.search(first) for p in (FOOD, VEHICLE, LAND, BUILDING, PLANT))


def px_sea(first, text):
    return bool(re.search(r"\b(fish|fishes|sharks?|jellyfish|octopus|coral|corals|reef|sea turtles?|stingrays?|"
                          r"seahorses?|underwater|aquarium)\b", first, re.I)) and not PX_NO_PEOPLE.search(first) \
        and not PX_PET.search(text) and not FOOD.search(first)


def px_wild(first, text):
    return bool(OTHER_ANIMAL.search(first)) and not PX_NO_PEOPLE.search(text) and not PX_PET.search(text) \
        and not SEA_ANIMAL.search(first) and not FOOD.search(first) and not re.search(r"\banimals?\b", first, re.I)


def build(group: str, used: set[str]) -> list[tuple[str, bytes, str]]:
    """All (source key, original bytes, extension) of one group, in a fixed order."""
    g = group
    if g == "貓":
        return (msvd(g, 9, lambda c: vtt_cat(c) and share(c, PERSON) <= 0.15, used) + msrvtt(g, 9, vtt_cat, used)
                + pexels(g, 9, px_cat, used))
    if g == "狗":
        return (msvd(g, 9, lambda c: vtt_dog(c) and share(c, PERSON) <= 0.15, used) + msrvtt(g, 9, vtt_dog, used)
                + pexels(g, 9, px_dog, used))
    if g == "人像+貓":
        return (pexels(g, 5, px_person_cat, used)
                + msvd(g, 4, lambda c: vtt_cat(c) and share(c, PERSON) >= 0.5 and share(c, PEOPLE) <= 0.15, used))
    if g == "人像+狗":
        return (pexels(g, 5, px_person_dog, used)
                + msvd(g, 4, lambda c: vtt_dog(c) and share(c, PERSON) >= 0.5 and share(c, PEOPLE) <= 0.15, used))
    if g == "合照":
        return (pexels(g, 18, px_group, used) + msrvtt(g, 12, vtt_group, used)
                + msvd(g, 5, lambda c: share(c, VTT_GROUP) >= 0.5 and none_of(c, *BUSY), used))
    if g == "人像":
        return (pexels(g, 15, px_person, used) + msrvtt(g, 10, vtt_person, used)
                + msvd(g, 5, lambda c: share(c, VTT_ONE) >= 0.7 and share(c, VTT_MANY) <= 0.1 and none_of(c, *BUSY), used))
    if g == "美食":
        return (pexels(g, 12, px_food, used) + msrvtt(g, 10, vtt_food, used, ("food", "cooking"))
                + msvd(g, 6, lambda c: share(c, FOOD) >= 0.8 and none_of(c, CAT, DOG, OTHER_ANIMAL), used))
    if g == "花草植物":
        return pexels(g, 24, px_plant, used)
    if g == "交通工具":
        return (pexels(g, 12, px_vehicle, used) + msrvtt(g, 12, vtt_vehicle, used, ("vehicles",))
                + msvd(g, 4, lambda c: share(c, VEHICLE) >= 0.8 and none_of(c, CAT, DOG, OTHER_ANIMAL), used))
    if g == "風景":
        return pexels(g, 22, px_land, used) + msrvtt(g, 4, lambda c: share(c, LAND) >= 0.7 and share(c, PERSON) <= 0.1
                                                     and share(c, VTT_MANY) <= 0.1 and none_of(c, *BUSY), used)
    if g == "城市建築":
        return pexels(g, 22, px_building, used) + msrvtt(g, 4, lambda c: share(c, BUILDING) >= 0.7
                                                         and share(c, PERSON) <= 0.1 and none_of(c, *BUSY), used)
    if g == "none_sports":
        return msrvtt(g, 14, vtt_sport, used, ("sports",)) + pexels(g, 6, px_sport, used)
    if g == "none_music":
        return (msrvtt(g, 9, vtt_music, used, ("music",)) + msvd(g, 5, vtt_music, used)
                + pexels(g, 4, px_music, used))
    if g == "none_dance":
        return msrvtt(g, 6, vtt_dance, used) + msvd(g, 4, vtt_dance, used) + pexels(g, 6, px_dance, used)
    if g == "none_machine":
        return pexels(g, 14, px_machine, used) + msrvtt(g, 4, lambda c: share(c, MACHINE) >= 0.6
                                                        and none_of(c, VEHICLE, FOOD, CAT, DOG), used)
    if g == "none_underwater":
        return pexels(g, 10, px_sea, used) + msrvtt(g, 6, vtt_sea, used)
    if g == "none_wildlife":
        return msvd(g, 7, vtt_wild, used) + msrvtt(g, 5, vtt_wild, used) + pexels(g, 6, px_wild, used)
    raise ValueError(group)


def build_top_up(group: str, used: set[str]) -> list[tuple[str, bytes, str]]:
    """More clips for a group that fell below 18 after the first by-eye review. Built AFTER all groups (so the
    earlier selection cannot change) and numbered after the group's first files."""
    g = group
    if g == "交通工具":
        return (pexels(g, 7, lambda first, text: bool(VEHICLE.search(first)) and not PX_NO_PEOPLE.search(text)
                       and not PX_ANY_ANIMAL.search(text)
                       and not any(p.search(first) for p in (FOOD, SPORT, MACHINE, LAND, BUILDING)), used, "/top-up")
                + msrvtt(g, 7, lambda c: share(c, VEHICLE) >= 0.8 and share(c, PERSON) <= 0.15
                         and share(c, VTT_MANY) <= 0.15 and none_of(c, CAT, DOG, OTHER_ANIMAL, SPORT, FOOD), used,
                         ("vehicles",), "/top-up"))
    raise ValueError(group)


TOP_UPS = ["交通工具"]


# ---------------------------------------------------------------- output

def frames_of(path: Path, count: int = 4):
    """`count` frames spread over the clip (by seeking), upright (the display matrix is applied)."""
    import av

    out = []
    with av.open(str(path)) as container:
        stream = container.streams.video[0]
        length = float(stream.duration * stream.time_base) if stream.duration else float(container.duration or 0) / 1e6
        for i in range(count):
            target = length * (i + 0.5) / count
            try:
                container.seek(int(target / stream.time_base) if stream.time_base else 0, stream=stream)
            except Exception:  # noqa: BLE001
                pass
            for frame in container.decode(stream):
                if frame.time is None or frame.time >= target - 0.5:
                    image = frame.to_image()
                    rotation = getattr(frame, "rotation", 0) or 0  # display matrix (PyAV >= 12)
                    out.append(image.rotate(rotation, expand=True) if rotation else image)
                    break
    return out


def contact_sheet(folder: Path, out: Path, cell: int = 200, columns: int = 2, count: int = 4) -> None:
    """One row per clip: `count` frames side by side; `columns` clips per sheet row."""
    from PIL import Image, ImageDraw

    files = sorted(p for p in folder.iterdir() if p.suffix.lower() in VIDEO_SUFFIXES)
    rows = (len(files) + columns - 1) // columns
    width = count * cell + 10
    sheet = Image.new("RGB", (columns * width, max(rows, 1) * (cell + 16)), "white")
    draw = ImageDraw.Draw(sheet)
    for i, path in enumerate(files):
        x, y = (i % columns) * width, (i // columns) * (cell + 16)
        try:
            for k, frame in enumerate(frames_of(path, count)):
                frame.thumbnail((cell, cell))
                sheet.paste(frame, (x + k * cell + (cell - frame.width) // 2, y + 16 + (cell - frame.height) // 2))
        except Exception as exc:  # noqa: BLE001 - a file the viewer cannot open still gets its cell
            draw.text((x + 4, y + 40), f"unreadable:\n{str(exc)[:60]}", fill="black")
        draw.text((x + 4, y + 2), path.name, fill="red")
    sheet.save(out, quality=85)


def cached_build(group: str, used: set[str], cache: Path | None, top_up: bool = False):
    """build() / build_top_up(), with the selected bytes of each group kept in --cache between runs."""
    build = build_top_up if top_up else globals()["build"]
    if cache is None:
        return build(group, used)
    folder = cache / (group + ("_top-up" if top_up else ""))
    index = folder / "index.json"
    if index.exists():
        listing = json.loads(index.read_text(encoding="utf-8"))
        used.update(listing["used"])
        return [(key, (folder / name).read_bytes(), ext) for key, name, ext in listing["items"]]
    before = set(used)
    items = build(group, used)
    folder.mkdir(parents=True, exist_ok=True)
    listing = []
    for number, (key, data, ext) in enumerate(items):
        name = f"{number:03d}{ext}"
        (folder / name).write_bytes(data)
        listing.append((key, name, ext))
    index.write_text(json.dumps({"used": sorted(used - before), "items": listing}, ensure_ascii=False), encoding="utf-8")
    return items


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True, help="output folder (outside the repo), e.g. /opt/devvideo")
    parser.add_argument("--raw", action="store_true", help="do not apply the by-eye REMOVED / MOVED lists")
    parser.add_argument("--sheets", default="", help="also write one contact-sheet JPEG per group into this folder")
    parser.add_argument("--cache", default="", help="keep the selected bytes of every group in this folder")
    args = parser.parse_args()
    out = Path(args.out) / "videos"
    cache = Path(args.cache) if args.cache else None
    if out.exists():
        for old in out.glob("*/*"):
            if old.suffix.lower() in VIDEO_SUFFIXES:
                old.unlink()
    out.mkdir(parents=True, exist_ok=True)
    used: set[str] = set()
    rows = []
    built = {group: cached_build(group, used, cache) for group in GROUPS}
    for group in TOP_UPS:
        built[group] = built[group] + cached_build(group, used, cache, top_up=True)
    for group in GROUPS:
        gone = set() if args.raw else set(REMOVED.get(group, []))
        elsewhere = {} if args.raw else MOVED.get(group, {})
        kept = 0
        for number, (source, data, ext) in enumerate(built[group]):
            name = f"{number:03d}{ext}"
            if name in gone:
                rows.append((group, name, group, source, "removed"))
                continue
            final = elsewhere.get(name, group)
            stored = name if final == group else f"{group}_{name}"
            (out / final).mkdir(exist_ok=True)
            (out / final / stored).write_bytes(data)
            rows.append((final, stored, group, source, "kept" if final == group else "moved"))
            kept += 1
        print(f"{group}: {kept} kept ({len(elsewhere)} of them moved), {len(gone)} removed", flush=True)
    for folder in out.iterdir():
        if folder.is_dir() and not any(folder.iterdir()):
            folder.rmdir()
    order = {g: i for i, g in enumerate(GROUPS)}
    rows.sort(key=lambda x: (order.get(x[0], len(order)), x[0], x[1], x[3]))
    (out / "manifest.tsv").write_text("\n".join("\t".join(row) for row in rows) + "\n", encoding="utf-8")
    if args.sheets:
        sheets = Path(args.sheets)
        sheets.mkdir(parents=True, exist_ok=True)
        for old in sheets.glob("*.jpg"):
            old.unlink()
        for folder in sorted(p for p in out.iterdir() if p.is_dir()):
            contact_sheet(folder, sheets / f"{folder.name}.jpg")


if __name__ == "__main__":
    main()
