"""打包給使用者／審查者的免安裝版 ZIP。

    python tools/build_zip.py [--out dist]      →  dist/AI-Media-Sorter-<版本>.zip

內容＝git 追蹤的每一個檔案（`git ls-files`），不多不少：
- 包含 tests/、tests/manual/（評測用的向量檔、來源清單、產生向量的腳本）、docs/、tools/、installer/，
  所以拿到 ZIP 的人可以跑完整的測試，也可以用 tools/evaluate.py 重現 README 的數字。
  （以前是用一支不在 repo 裡的臨時腳本打包，把 tests/manual/ 排除了，審查者因此無法重現「97%」。）
- 不包含沒被追蹤的東西：.venv、個人設定、整理紀錄、工作資料庫、模型檔、建置輸出。
- 有還沒 commit 的修改時拒絕打包（加 --allow-dirty 才行），確保 ZIP 對得上某一個 commit；commit 編號寫在 ZIP 的註解裡。
- .bat／.cmd／.txt 依 .gitattributes 轉成 Windows 換行（跟在 Windows 上 checkout 的結果一樣）。
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CRLF_SUFFIXES = {".bat", ".cmd", ".txt"}  # 跟 .gitattributes 的 eol=crlf 相同
REQUIRED = [  # 少了任何一個就不是完整的交付（tests/test_build_zip.py 也會檢查）
    "README.md", "install.bat", "start.bat", "uninstall.bat", "requirements.txt", "requirements.lock",
    "media_sorter/__init__.py", "docs/evaluation.md", "docs/roadmap.md", "tools/evaluate.py", "tools/build_zip.py",
    "tests/test_real_embeddings.py", "tests/manual/embeddings_standard.npz", "tests/manual/embeddings_accurate.npz",
    "tests/manual/embeddings_holdout_standard.npz", "tests/manual/embeddings_holdout_accurate.npz",
    "tests/manual/photo_sources.txt", "tests/manual/holdout_sources.txt", "tests/manual/dump_embeddings.py",
    "tests/manual/collect_holdout.py", "tests/manual/real_model_check.py", "tests/manual/prompt_candidates.py",
]


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, check=True, capture_output=True, text=True,
                          encoding="utf-8").stdout


def tracked_files() -> list[str]:
    return [name for name in git("ls-files", "-z").split("\0") if name]


def to_crlf(data: bytes) -> bytes:
    return data.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")


def build(out_dir: Path, allow_dirty: bool = False) -> Path:
    sys.path.insert(0, str(ROOT))
    from media_sorter import __version__

    dirty = git("status", "--porcelain").strip()
    if dirty and not allow_dirty:
        raise SystemExit("有還沒 commit 的修改，ZIP 會對不上任何一個 commit。請先 commit，或加 --allow-dirty：\n" + dirty)
    files = tracked_files()
    missing = [name for name in REQUIRED if name not in files]
    if missing:
        raise SystemExit("這些檔案沒有被 git 追蹤，ZIP 會不完整：" + "、".join(missing))
    commit = git("rev-parse", "HEAD").strip()
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"AI-Media-Sorter-{__version__}.zip"
    top = f"AI-Media-Sorter-{__version__}"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        archive.comment = f"commit {commit}{' (with uncommitted changes)' if dirty else ''}".encode()
        for name in sorted(files):
            path = ROOT / name
            if not path.is_file():
                continue
            data = path.read_bytes()
            if path.suffix.lower() in CRLF_SUFFIXES:
                data = to_crlf(data)
            archive.writestr(zipfile.ZipInfo(f"{top}/{name}", date_time=(2020, 1, 1, 0, 0, 0)), data,
                             zipfile.ZIP_DEFLATED)
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--out", default=str(ROOT / "dist"))
    parser.add_argument("--allow-dirty", action="store_true", help="有未 commit 的修改也打包（ZIP 註解會註明）")
    args = parser.parse_args()
    target = build(Path(args.out), args.allow_dirty)
    with zipfile.ZipFile(target) as archive:
        count = len(archive.namelist())
    print(f"{target}  ({target.stat().st_size / 1e6:.1f} MB, {count} files)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
