"""查出 Hugging Face 上某個 commit 的模型檔大小與 SHA-256，印出可以貼進 media_sorter/config.py 的內容。會連網。

    python tools/update_model_pins.py              用 config.py 目前固定的 commit 重新核對
    python tools/update_model_pins.py --latest     改查各 repo 目前 main 的 commit（要換版本時）

換模型版本之後一定要：重跑 tests/manual/dump_embeddings.py（兩個模型、開發集與獨立測試集）、
python tools/evaluate.py --update-doc，並檢查 docs/evaluation.md 的數字；保存的辨識結果會因為版本不同自動重算。
"""

import argparse
import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--latest", action="store_true")
    args = parser.parse_args()
    from huggingface_hub import HfApi, hf_hub_download

    from media_sorter import netpolicy
    from media_sorter.config import MODEL_PRESETS

    netpolicy.set_offline(False)  # 這支工具的用途就是連網查詢
    api = HfApi()
    for preset, info in MODEL_PRESETS.items():
        for kind in ("hf", "tokenizer"):
            repo = info[f"{kind}_repo"]
            revision = api.model_info(repo).sha if args.latest else info[f"{kind}_revision"]
            print(f'# {preset}: "{kind}_repo": "{repo}"\n"{kind}_revision": "{revision}",\n"{kind}_files": {{')
            for name in info[f"{kind}_files"]:
                path = Path(hf_hub_download(repo, name, revision=revision))
                digest = hashlib.sha256()
                with open(path, "rb") as f:
                    while block := f.read(1 << 22):
                        digest.update(block)
                changed = "" if (path.stat().st_size, digest.hexdigest()) == tuple(info[f"{kind}_files"][name]) \
                    and revision == info[f"{kind}_revision"] else "  # 跟 config.py 不同"
                print(f'    "{name}": ({path.stat().st_size}, "{digest.hexdigest()}"),{changed}')
            print("},")
    return 0


if __name__ == "__main__":
    sys.exit(main())
