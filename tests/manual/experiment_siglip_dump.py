"""路線第 4 步的可行性試驗：把模型換成多語言的 SigLIP 2，在第一份獨立測試集的圖片上（現在當開發資料用）跟現行標準模型比。
比兩件事：(1) 預設分類的第一名分對率；(2) 同樣的需檢查量下，未知內容被接受的比例——SigLIP 對每個分類各自給分（sigmoid），
理論上比較適合判斷「都不是」。結果（不採用）記在 docs/evaluation.md。
用法：python tests/manual/experiment_siglip_dump.py ViT-B-16-SigLIP2-256 <collect_holdout.py 的輸出>/images out.npz
接著：python tests/manual/experiment_siglip_compare.py out.npz前處理（讀圖、長圖切方塊、多句描述取平均）跟產品一樣。"""
import sys, numpy as np, torch, open_clip
from pathlib import Path
name = sys.argv[1]
model, _, pre = open_clip.create_model_and_transforms(name, pretrained="webli"); model.eval()  # 先下載，再載入會切到離線的產品程式
tok = open_clip.get_tokenizer(name)
ROOT = Path(__file__).resolve().parents[2]; sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT/"tools"))
from media_sorter import media
from media_sorter.analysis import square_crops
from media_sorter.classifier import normalize, OTHER_TEXTS
from media_sorter.config import DEFAULT_CATEGORIES
cats = list(DEFAULT_CATEGORIES); names = [c.name for c in cats]
@torch.inference_mode()
def text(ts):
    return normalize(model.encode_text(tok(ts)).float().numpy().astype(np.float64))
C = np.stack([normalize(text(c.texts()).mean(0)) for c in cats]); O = normalize(text(list(OTHER_TEXTS)).mean(0))
scale = float(model.logit_scale.exp()); bias = float(getattr(model, "logit_bias", torch.tensor(0.0)))
files = sorted(p for p in Path(sys.argv[2]).glob("*/*") if p.is_file())
E, truth = [], []
with torch.inference_mode():
    for i, p in enumerate(files):
        img, _ = media.load_image(p, 640)
        t = torch.stack([pre(c) for c in square_crops(img)])
        E.append(normalize(normalize(model.encode_image(t).float().numpy().astype(np.float64)).mean(0)))
        truth.append(p.parent.name if p.parent.name in names else None)
        if i % 200 == 0: print(i, len(files), flush=True)
E = np.stack(E)
np.savez_compressed(sys.argv[3], E=E, C=C, O=O, scale=scale, bias=bias, truth=np.array([t or "" for t in truth]))
print("saved", scale, bias)
