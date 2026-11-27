"""用事先算好的真模型向量（tests/manual/embeddings_*.npz）量預設分類的表現，不需要下載模型。

    python tools/evaluate.py                 印出結果（Markdown）
    python tools/evaluate.py --update-doc    把結果寫進 docs/evaluation.md 的「自動產生」區塊

兩批資料（兩個模型各一份向量）：
- dev      tests/manual/embeddings_<模型>.npz          1,243 張。描述與規則是看著這一批定的，成績偏樂觀，只當對照。
- holdout  tests/manual/embeddings_holdout_<模型>.npz  1,176 張。來源完全不同、在所有描述與門檻定案之後才收集；
           只用來報告，**不准拿它調任何東西**（見 tests/manual/holdout_sources.txt 開頭）。

量的東西跟程式實際的行為一致：用 media_sorter.analysis 的計分（_apply_scores）、「需檢查」的規則（Item.is_low）
與預設門檻（DEFAULT_SETTINGS["confidence_threshold"]）。向量檔裡每句描述各有一個向量，這裡照 Category.texts()
組成分類，所以預設分類的描述改了、向量檔沒重算時會直接報錯，不會悄悄用舊的。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from media_sorter.analysis import CONFIRMED, SKIPPED, Item, _apply_scores, learn_from_decisions  # noqa: E402
from media_sorter.classifier import OTHER_TEXTS, SIMILARITY_FLOOR, normalize  # noqa: E402
from media_sorter.config import DEFAULT_CATEGORIES, DEFAULT_SETTINGS, Category  # noqa: E402
from media_sorter.vocabulary import VOCABULARY  # noqa: E402

MANUAL = ROOT / "tests" / "manual"
DOC = ROOT / "docs" / "evaluation.md"
BEGIN, END = "<!-- BEGIN GENERATED: python tools/evaluate.py --update-doc -->", "<!-- END GENERATED -->"
NONE = "（都不像）"
PRESETS = {"standard": "標準", "accurate": "高精準"}
SETS = {"holdout": "embeddings_holdout_{}.npz", "dev": "embeddings_{}.npz", "holdout2": "embeddings_holdout2_{}.npz",
        "holdout2_video": "embeddings_holdout2_video_{}.npz"}
THRESHOLD = float(DEFAULT_SETTINGS["confidence_threshold"])
SWEEP = (0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9)


class Recorded:
    """假裝是 Classifier：文字向量直接查事先存好的結果。"""

    def __init__(self, path: Path):
        data = np.load(path)
        self.names = data["image_names"].tolist()
        self.labels = data["image_labels"].tolist()
        self.groups = data["image_groups"].tolist() if "image_groups" in data.files else [""] * len(self.names)
        self.images = normalize(data["image_emb"].astype(np.float64))
        self.frame_owner = data["frame_owner"]
        self.frames = normalize(data["frame_emb"].astype(np.float64)) if len(data["frame_emb"]) else data["frame_emb"]
        self.logit_scale = float(data["logit_scale"])
        self.model = str(data["model"])
        self._text = dict(zip(data["texts"].tolist(), normalize(data["text_emb"].astype(np.float64)), strict=True))

    def _group(self, texts) -> np.ndarray:
        missing = [t for t in texts if t not in self._text]
        if missing:
            raise KeyError(f"向量檔裡沒有這些描述（預設分類改過了？請重跑 dump_embeddings.py）：{missing[:3]}")
        return normalize(np.mean([self._text[t] for t in texts], axis=0))

    def encode_categories(self, categories):
        return np.stack([self._group(c.texts()) for c in categories])

    def vocabulary_embeddings(self):
        return np.stack([self._group([zh, en, f"a photo of {en}"]) for zh, en in VOCABULARY])

    def other_embedding(self):
        return self._group(list(OTHER_TEXTS))

    def items(self, categories) -> list[Item]:
        names = tuple(c.name for c in categories)
        class_emb, vocab_emb, other = self.encode_categories(categories), self.vocabulary_embeddings(), self.other_embedding()
        result = []
        for position, name in enumerate(self.names):
            video = name.endswith(".mp4")
            item = Item(Path(name), "video" if video else "image", embedding=self.images[position],
                        frames=self.frames[self.frame_owner == position] if video else None)
            _apply_scores(item, class_emb, names, vocab_emb, self.logit_scale, other)
            result.append(item)
        return result

    def answers(self) -> dict[str, str]:
        """每個檔案的正確答案：預設分類的名稱，或 NONE（不屬於任何預設分類）。標記是「?」的不計。"""
        answers = dict(zip(self.names, self.labels, strict=True))
        for name, group in zip(self.names, self.groups, strict=True):
            if group == "none_meme":  # 開發集收集時梗圖還不是預設分類；現在是
                answers[name] = "迷因梗圖"
        return answers


def evaluate(recorded: Recorded, threshold: float = THRESHOLD) -> dict:
    """回傳一份成績。名詞：
    - 封閉：只看「答案是某個預設分類」的檔案，每個檔案取第一名（不管有沒有把握）
    - 接受：第一名而且沒有被標成「需檢查」→ 會被自動整理進那個分類的資料夾
    - 未知：答案是 NONE 的檔案；它被「接受」進任何分類都是錯的
    """
    categories = [Category(c.name, list(c.prompts)) for c in DEFAULT_CATEGORIES]
    names = [c.name for c in categories]
    answers = recorded.answers()
    def settle(answer: str, predicted: str) -> str:
        """「兩可」的檔案（答案寫成 人像+美食）：AI 選了其中任何一個都算對，否則算在第一個答案頭上。"""
        parts = answer.split("+")
        return predicted if predicted in parts else parts[0]

    rows = [(settle(answers[it.path.name], it.best(categories)[0]), it.best(categories)[0],
             it.is_low(categories, threshold, SIMILARITY_FLOOR))
            for it in recorded.items(categories) if answers[it.path.name] != "?"]
    known = [r for r in rows if r[0] != NONE]
    unknown = [r for r in rows if r[0] == NONE]
    accepted = [r for r in rows if not r[2]]

    def prf(tp: int, predicted: int, actual: int) -> tuple[float, float, float]:
        precision = tp / predicted if predicted else float("nan")
        recall = tp / actual if actual else float("nan")
        f1 = 2 * precision * recall / (precision + recall) if tp else 0.0
        return precision, recall, f1

    per_class = {}
    for name in names:
        actual = sum(1 for r in known if r[0] == name)
        closed = prf(sum(1 for r in known if r[0] == name and r[1] == name),
                     sum(1 for r in known if r[1] == name), actual)
        # 開放：未知的檔案也算進來，而且要「被接受」才算分進這一類
        opened = prf(sum(1 for r in accepted if r[0] == name and r[1] == name),
                     sum(1 for r in accepted if r[1] == name), actual)
        per_class[name] = {"n": actual, "closed": closed, "open": opened}
    correct_known = [r for r in known if r[0] == r[1]]
    wrong_known = [r for r in known if r[0] != r[1]]
    return {
        "threshold": threshold,
        "n": len(rows), "n_known": len(known), "n_unknown": len(unknown),
        "per_class": per_class,
        "closed_accuracy": len(correct_known) / len(known),
        "closed_balanced": float(np.mean([v["closed"][1] for v in per_class.values()])),
        "closed_macro_f1": float(np.mean([v["closed"][2] for v in per_class.values()])),
        "open_macro_precision": float(np.nanmean([v["open"][0] for v in per_class.values()])),
        "open_macro_recall": float(np.mean([v["open"][1] for v in per_class.values()])),
        "open_macro_f1": float(np.mean([v["open"][2] for v in per_class.values()])),
        "flagged": sum(r[2] for r in rows) / len(rows),
        "accepted": len(accepted) / len(rows),
        # 「不進需檢查的部分」裡分錯的比例：分母是所有被接受的檔案，未知的檔案被接受也算錯
        "accepted_error": sum(1 for r in accepted if r[0] != r[1]) / len(accepted) if accepted else float("nan"),
        "accepted_error_known": (sum(1 for r in accepted if r[0] != NONE and r[0] != r[1])
                                 / max(1, sum(1 for r in accepted if r[0] != NONE))),
        "unknown_false_accept": sum(1 for r in unknown if not r[2]) / len(unknown) if unknown else float("nan"),
        "correct_flagged": sum(r[2] for r in correct_known) / max(1, len(correct_known)),
        "wrong_known_flagged": sum(r[2] for r in wrong_known) / max(1, len(wrong_known)),
        "n_wrong_known": len(wrong_known),
        "unknown_by_group": _unknown_groups(recorded, categories, answers, threshold),
    }


def _unknown_groups(recorded, categories, answers, threshold) -> dict[str, tuple[int, float]]:
    groups: dict[str, list[bool]] = {}
    for item, group in zip(recorded.items(categories), recorded.groups, strict=True):
        if answers[item.path.name] != NONE:
            continue
        key = group or item.path.name.rsplit("_", 1)[0]
        groups.setdefault(key, []).append(not item.is_low(categories, threshold, SIMILARITY_FLOOR))
    return {key: (len(values), sum(values) / len(values)) for key, values in sorted(groups.items())}


def learning_simulation(recorded: Recorded, threshold: float = THRESHOLD, rounds: int = 2) -> tuple[dict, dict]:
    """模擬「從修正學習」：假設使用者把「需檢查」裡的每個檔案都處理對了（屬於某個分類的指定正確分類，都不是的略過），
    程式用這些修正重看其餘的檔案（analysis.learn_from_decisions，就是產品用的那個函式），新被拉進「需檢查」的再處理一輪。
    回傳 (學習前, 學習後) 的指標。這是上限的估計：真實的使用者不一定看完、也可能改錯。"""
    categories = [Category(c.name, list(c.prompts)) for c in DEFAULT_CATEGORIES]
    answers = recorded.answers()
    items = [it for it in recorded.items(categories) if answers[it.path.name] != "?"]
    predicted = [it.best(categories)[0] for it in items]
    truth = [p if p in answers[it.path.name].split("+") else answers[it.path.name].split("+")[0]
             for it, p in zip(items, predicted)]

    def metrics(flags: list[bool]) -> dict:
        unknown = [f for f, t in zip(flags, truth) if t == NONE]
        right = [f for f, t, p in zip(flags, truth, predicted) if t == p]
        accepted = [(t, p) for f, t, p in zip(flags, truth, predicted) if not f]
        return {"flagged": sum(flags) / len(flags),
                "unknown_false_accept": sum(not f for f in unknown) / max(1, len(unknown)),
                "correct_flagged": sum(right) / max(1, len(right)),
                "accepted_error": sum(t != p for t, p in accepted) / max(1, len(accepted))}

    flags = [it.is_low(categories, threshold, SIMILARITY_FLOOR) for it in items]
    before = metrics(flags)
    for _ in range(rounds):
        for it, flagged, answer in zip(items, flags, truth):
            if flagged and it.status not in (CONFIRMED, SKIPPED):  # 使用者處理這個檔案
                if answer == NONE:
                    it.status = SKIPPED
                else:
                    it.chosen, it.status = answer, CONFIRMED
        learn_from_decisions(items, categories)
        flags = [f or it.neighbor_hint is not None for f, it in zip(flags, items)]
    return before, metrics(flags)


def pct(value: float) -> str:
    return "—" if value != value else f"{value * 100:.1f}%"


def load(which: str, preset: str) -> Recorded | None:
    path = MANUAL / SETS[which].format(preset)
    return Recorded(path) if path.exists() else None


def render() -> str:
    out: list[str] = []
    results = {(which, preset): evaluate(rec) for which in SETS for preset in PRESETS
               if (rec := load(which, preset)) is not None}

    def summary(which: str, title: str) -> None:
        out.append(f"### {title}\n")
        present = [p for p in PRESETS if (which, p) in results]
        if not present:
            out.append("（沒有這一批的向量檔）\n")
            return
        first = results[(which, present[0])]
        out.append(f"{first['n']:,} 張：{first['n_known']:,} 張屬於某個預設分類，{first['n_unknown']:,} 張不屬於任何預設分類。"
                   f"「需檢查」門檻 {round(first['threshold'] * 100)}。\n")
        out.append("| 指標 | " + " | ".join(PRESETS[p] + "模型" for p in present) + " |")
        out.append("| --- | " + " | ".join("---:" for _ in present) + " |")
        lines = [
            ("第一名正確率（只看有答案的檔案，不管有沒有把握）", "closed_accuracy"),
            ("各分類 recall 的平均（＝README 以前說的「各分類平均正確率」）", "closed_balanced"),
            ("Macro F1（只看有答案的檔案）", "closed_macro_f1"),
            ("Macro F1（含未知檔案；被標「需檢查」的不算分進去）", "open_macro_f1"),
            ("　其中 macro precision", "open_macro_precision"),
            ("　其中 macro recall", "open_macro_recall"),
            ("被標成「需檢查」的比例（全部檔案）", "flagged"),
            ("**「不進需檢查的部分」分錯的比例**（未知檔案被分進任何分類也算錯）", "accepted_error"),
            ("　同上，但只看有答案的檔案", "accepted_error_known"),
            ("**未知類別誤接受率**（不屬於任何分類、卻沒被標「需檢查」）", "unknown_false_accept"),
            ("分對的檔案被誤標「需檢查」", "correct_flagged"),
            ("分錯的檔案（有答案的）被標「需檢查」而攔下", "wrong_known_flagged"),
        ]
        for label, key in lines:
            out.append(f"| {label} | " + " | ".join(pct(results[(which, p)][key]) for p in present) + " |")
        out.append("")

    def per_class(which: str) -> None:
        present = [p for p in PRESETS if (which, p) in results]
        if not present:
            return
        out.append("每一類（P＝precision、R＝recall。「封閉」只看有答案的檔案、取第一名；"
                   "「開放」把未知檔案也算進來，而且被標「需檢查」的不算分進這一類）：\n")
        head = "| 分類 | 張數 | " + " | ".join(f"{PRESETS[p]}：封閉 P | 封閉 R | 開放 P | 開放 R" for p in present) + " |"
        out.append(head)
        out.append("| --- | ---: | " + " | ".join("---: | ---: | ---: | ---:" for _ in present) + " |")
        for name in results[(which, present[0])]["per_class"]:
            cells = []
            for p in present:
                row = results[(which, p)]["per_class"][name]
                cells.append(" | ".join(pct(v) for v in (row["closed"][0], row["closed"][1], row["open"][0], row["open"][1])))
            out.append(f"| {name} | {results[(which, present[0])]['per_class'][name]['n']} | " + " | ".join(cells) + " |")
        out.append("")
        out.append("未知檔案各組的誤接受率（沒被標「需檢查」的比例）：\n")
        out.append("| 組別 | 張數 | " + " | ".join(PRESETS[p] for p in present) + " |")
        out.append("| --- | ---: | " + " | ".join("---:" for _ in present) + " |")
        for group, (count, _) in results[(which, present[0])]["unknown_by_group"].items():
            out.append(f"| {group} | {count} | "
                       + " | ".join(pct(results[(which, p)]["unknown_by_group"][group][1]) for p in present) + " |")
        out.append("")

    def sweep(which: str) -> None:
        present = [p for p in PRESETS if load(which, p) is not None]
        if not present:
            return
        out.append("| 門檻 | " + " | ".join(f"{PRESETS[p]}：需檢查比例 | 接受部分分錯 | 未知誤接受 | 分對被誤標" for p in present) + " |")
        out.append("| ---: | " + " | ".join("---: | ---: | ---: | ---:" for _ in present) + " |")
        recorded = {p: load(which, p) for p in present}
        for threshold in SWEEP:
            cells = []
            for p in present:
                r = evaluate(recorded[p], threshold)
                cells.append(" | ".join(pct(r[k]) for k in ("flagged", "accepted_error", "unknown_false_accept", "correct_flagged")))
            mark = "**" if abs(threshold - THRESHOLD) < 1e-9 else ""
            out.append(f"| {mark}{round(threshold * 100)}{mark} | " + " | ".join(cells) + " |")
        out.append("")

    summary("holdout", "獨立測試集（holdout）——對外引用的數字")
    per_class("holdout")
    summary("dev", "開發集（dev）——描述與規則是看著它定的，只當對照")
    per_class("dev")
    out.append("### 從修正學習（模擬）\n")
    out.append("假設使用者把「需檢查」的檔案都處理對了，程式用這些修正重看其餘檔案（兩輪）。這是效果的上限估計。\n")
    out.append("| 資料 | 模型 | 需檢查：前 → 後 | 未知誤接受：前 → 後 | 接受部分分錯：前 → 後 | 分對被誤標：前 → 後 |")
    out.append("| --- | --- | ---: | ---: | ---: | ---: |")
    for which, label in (("holdout2", "第二份獨立測試集"), ("holdout", "第一份獨立測試集"), ("dev", "開發集")):
        for preset in PRESETS:
            rec = load(which, preset)
            if rec is None:
                continue
            before, after = learning_simulation(rec)
            cells = " | ".join(f"{pct(before[k])} → {pct(after[k])}"
                               for k in ("flagged", "unknown_false_accept", "accepted_error", "correct_flagged"))
            out.append(f"| {label} | {PRESETS[preset]} | {cells} |")
    out.append("")
    out.append("### 門檻的依據：在開發集上改變門檻\n")
    out.append("預設門檻是看這張表（與更早的 425 張）定的。粗體是目前的預設值。\n")
    sweep("dev")
    out.append("### 同一張表，換成獨立測試集（只是報告；沒有依它改門檻）\n")
    sweep("holdout")
    return "\n".join(out).rstrip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--update-doc", action="store_true", help=f"把結果寫進 {DOC.relative_to(ROOT)} 的自動產生區塊")
    args = parser.parse_args()
    text = render()
    if not args.update_doc:
        sys.stdout.reconfigure(encoding="utf-8")
        print(text)
        return 0
    doc = DOC.read_text(encoding="utf-8")
    start, end = doc.index(BEGIN) + len(BEGIN), doc.index(END)
    DOC.write_text(doc[:start] + "\n\n" + text + "\n" + doc[end:], encoding="utf-8")
    print(f"已更新 {DOC}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
