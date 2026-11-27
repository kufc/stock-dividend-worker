"""影片開發資料：每個影片取 8 格（跟產品一樣），每格算三種前處理的向量。
center＝現行（縮短邊＋裁中間）；crops＝像圖片一樣切成重疊的正方形；pad＝補邊成正方形（不裁掉任何東西）。
影片來自 collect_dev_video.py（開發資料，不是測試集；見 dev_video_sources.txt）。需要下載模型。
用法：python tests/manual/dump_video_variants.py standard <資料夾>/videos tests/manual/embeddings_devvideo_standard.npz"""
import sys
from pathlib import Path
import numpy as np
from PIL import Image
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from media_sorter import media
from media_sorter.analysis import square_crops
from media_sorter.classifier import Classifier

preset, root, out = sys.argv[1], Path(sys.argv[2]), sys.argv[3]
clf = Classifier(preset)

def pad(img):
    side = max(img.size)
    canvas = Image.new("RGB", (side, side), (0, 0, 0))
    canvas.paste(img, ((side - img.width) // 2, (side - img.height) // 2))
    return canvas

names, labels, sizes, failed = [], [], [], []
center, padded, crops, crop_owner, frame_owner = [], [], [], [], []
files = sorted(p for p in root.glob("*/*") if p.is_file() and p.suffix.lower() in media.VIDEO_EXTS)
for n, path in enumerate(files):
    try:
        frames, _ = media.sample_video_frames(path, 8)
    except Exception as exc:  # noqa: BLE001
        failed.append(f"{path.parent.name}/{path.name}: {exc}"); continue
    video = len(names)
    names.append(f"{path.parent.name}/{path.name}"); labels.append(path.parent.name); sizes.append(frames[0].size)
    center.append(clf.encode_images(frames)); padded.append(clf.encode_images([pad(f) for f in frames]))
    for f in frames:
        frame_index = len(frame_owner); frame_owner.append(video)
        parts = square_crops(f)
        crops.append(clf.encode_images(parts)); crop_owner += [frame_index] * len(parts)
    if n % 20 == 0:
        print(n, len(files), flush=True)
np.savez_compressed(out, names=np.array(names), labels=np.array(labels), sizes=np.array(sizes),
                    frame_owner=np.array(frame_owner), center=np.concatenate(center).astype(np.float16),
                    pad=np.concatenate(padded).astype(np.float16), crops=np.concatenate(crops).astype(np.float16),
                    crop_owner=np.array(crop_owner), failed=np.array(failed))
print("done", len(names), "failed", failed)
