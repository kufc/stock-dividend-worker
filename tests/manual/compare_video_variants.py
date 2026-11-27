"""影片開發資料上比較畫格前處理（A4）與「都不是」的判斷方式（A5）。
不需要模型：python tests/manual/compare_video_variants.py（結果記在 docs/evaluation.md）。
事先定的採用條件：
 A4：第一名分對的比例比現行高 3 個百分點以上，而且同樣的需檢查量下未知誤接受不變差超過 2 個百分點。
 A5：同樣的需檢查量下（把現行做法的門檻調到相同比例）未知誤接受少 3 個百分點以上，分對被多標不多於 1 個百分點。"""
import sys, numpy as np
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "tools"))
import evaluate as ev
from media_sorter.classifier import normalize, score, combine_frames
from media_sorter.config import DEFAULT_CATEGORIES, Category
preset = sys.argv[1] if len(sys.argv) > 1 else "standard"
d = np.load(ROOT / "tests" / "manual" / f"embeddings_devvideo_{preset}.npz")
rec = ev.load("dev", preset)  # 只用它的文字向量
cats = [Category(c.name, list(c.prompts)) for c in DEFAULT_CATEGORIES]; names = [c.name for c in cats]
C, O, ls = rec.encode_categories(cats), rec.other_embedding(), rec.logit_scale
labels = d["labels"].tolist(); fo = d["frame_owner"]; co = d["crop_owner"]
center, pad, crops = (normalize(d[k].astype(np.float64)) for k in ("center", "pad", "crops"))
crop_frame = np.stack([normalize(crops[co == f].mean(axis=0)) for f in range(len(fo))])
truth = [l.split("+") if not l.startswith("none_") else None for l in labels]
known = np.array([t is not None for t in truth])
def run(frames_of, none_rule, thr=0.7):
    pred, conf, none = [], [], []
    for v in range(len(labels)):
        F = frames_of(v)
        p = score(F, C, ls, O).mean(axis=0)
        pred.append(names[int(p.argmax())]); conf.append(p.max())
        if none_rule == "mean":
            e = combine_frames(F); none.append(float(O @ e) > float((C @ e).max()))
        else:
            per = (F @ O) > (F @ C.T).max(axis=1)
            none.append(per.mean() >= 0.5 if none_rule == "majority" else per.any())
    conf, none = np.array(conf), np.array(none)
    right = np.array([t is not None and p in t for p, t in zip(pred, truth)])
    return pred, conf, none, right
def report(name, conf, none, right, thr=0.7):
    low = (conf < thr) | none
    return f"{name} | 第一名分對 {right[known].mean():.1%} | 需檢查 {low.mean():.1%} | 未知誤接受 {(~low[~known]).mean():.1%} | 分對被多標 {low[right].mean():.1%}", low.mean()
def matched(conf, none, right, rate):
    best = None
    for th in np.arange(0.5, 0.995, 0.005):
        low = (conf < th) | none
        if best is None or abs(low.mean() - rate) < abs(best[0] - rate): best = (low.mean(), (~low[~known]).mean(), low[right].mean(), th)
    return best
print(f"== {preset}：{len(labels)} 個影片，{known.sum()} 個有答案，{(~known).sum()} 個都不是；失敗 {d['failed'].tolist()}")
variants = {"現行（裁中間）": lambda v: center[fo == v], "每格切方塊、每格取平均向量": lambda v: crop_frame[fo == v],
            "每格切方塊、每塊都當一格": lambda v: crops[np.isin(co, np.where(fo == v)[0])], "補邊成正方形": lambda v: pad[fo == v]}
base = run(variants["現行（裁中間）"], "mean")
for name, f in variants.items():
    pred, conf, none, right = run(f, "mean")
    text, rate = report(name, conf, none, right); b = matched(base[1], base[2], base[3], rate)
    print(text, f"|| 現行在同樣需檢查量：未知誤接受 {b[1]:.1%}、分對被多標 {b[2]:.1%}")
    if name.startswith("現行"):
        per = {}
        for t, r in zip(truth, right):
            if t and len(t) == 1: per.setdefault(t[0], []).append(r)
        print("   各分類：", "、".join(f"{k} {np.mean(v):.0%}({len(v)})" for k, v in per.items()))
        wrong = {}
        for t, p, r in zip(truth, pred, right):
            if t and not r: wrong[(t[0], p)] = wrong.get((t[0], p), 0) + 1
        print("   常見的錯：", sorted(wrong.items(), key=lambda x: -x[1])[:6])
print("-- 都不是的判斷方式（前處理用現行）")
for rule in ("mean", "majority", "any"):
    pred, conf, none, right = run(variants["現行（裁中間）"], rule)
    text, rate = report({"mean": "現行：平均向量", "majority": "過半的格", "any": "任何一格"}[rule], conf, none, right)
    b = matched(base[1], base[2], base[3], rate)
    print(text, f"|| 現行在同樣需檢查量：未知誤接受 {b[1]:.1%}、分對被多標 {b[2]:.1%}")
