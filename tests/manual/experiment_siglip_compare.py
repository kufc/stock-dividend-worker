"""experiment_siglip_dump.py 的向量跟現行兩個模型比（第一份獨立測試集，當開發資料用）。結果記在 docs/evaluation.md。"""
import sys, numpy as np
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]; sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT/"tools"))
import evaluate as ev
from media_sorter.config import DEFAULT_CATEGORIES
names=[c.name for c in DEFAULT_CATEGORIES]
def table(tag, pred, known, truth, score_fn_list):
    right = np.array([k and p == t for p, k, t in zip(pred, known, truth)])
    per = [np.mean([right[i] for i in range(len(pred)) if truth[i] == n]) for n in names]
    print(f"{tag}: 第一名分對（各分類平均）{np.mean(per):.1%}，最差的一類 {names[int(np.argmin(per))]} {min(per):.1%}")
    for label, low_at in score_fn_list:
        for rate in (0.20, 0.283, 0.35):
            best = None
            for th in np.linspace(0, 1, 2001):
                low = low_at(th)
                if best is None or abs(low.mean() - rate) < abs(best[0] - rate): best = (low.mean(), (~low[~known]).mean(), low[right].mean())
            print(f"   {label} 需檢查 {best[0]:.1%} → 未知誤接受 {best[1]:.1%}、分對被多標 {best[2]:.1%}")
for preset in ("standard", "accurate"):
    r = ev.load("holdout", preset); cats=[ev.Category(c.name, list(c.prompts)) for c in DEFAULT_CATEGORIES]; ans = r.answers(); its = r.items(cats)
    pred=[i.best(cats)[0] for i in its]; conf=np.array([i.best(cats)[1] for i in its]); none=np.array([i.fits_nothing for i in its])
    truth=[ans[i.path.name] for i in its]; known=np.array([t!=ev.NONE for t in truth])
    table(f"現行 {preset}", pred, known, truth, [("門檻＋其他東西", lambda th: (conf<th)|none)])
for f in map(Path, sys.argv[1:]):
    d=np.load(f); E,C,O=d["E"],d["C"],d["O"]; scale,bias=float(d["scale"]),float(d["bias"]); truth=[t or ev.NONE for t in d["truth"].tolist()]
    known=np.array([t!=ev.NONE for t in truth]); sim=E@C.T
    z=scale*sim; P=np.exp(z-z.max(1,keepdims=True)); P/=P.sum(1,keepdims=True)
    pred=[names[i] for i in sim.argmax(1)]; none=(E@O)>sim.max(1); sig=1/(1+np.exp(-(scale*sim.max(1)+bias)))
    rank=np.argsort(np.argsort(sig))/len(sig)  # sigmoid 分數很小，用名次當門檻掃
    table(f.stem, pred, known, truth, [("同現行的規則（softmax 門檻＋其他東西）", lambda th: (P.max(1)<th)|none),
                                      ("每類各自給分（sigmoid 最高分低於門檻）", lambda th: rank<th),
                                      ("兩者並用（sigmoid 低，或比其他東西更像）", lambda th: (rank<th)|none)])
    print("   sigmoid 最高分的中位數：有答案", np.median(sig[known]).round(4), "未知", np.median(sig[~known]).round(4), " logit_scale", round(scale,1), "bias", round(bias,1))
