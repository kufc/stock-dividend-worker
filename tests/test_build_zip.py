"""交付用的 ZIP 由 repo 裡的 tools/build_zip.py 產生，而且一定包含評測資料（第九輪審查：以前的 ZIP 少了 tests/manual）。"""

import shutil
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import build_zip  # noqa: E402

pytestmark = pytest.mark.skipif(not (shutil.which("git") and (ROOT / ".git").exists()), reason="需要 git 與 repo")


def test_every_required_file_is_tracked():
    tracked = set(build_zip.tracked_files())
    assert not [name for name in build_zip.REQUIRED if name not in tracked]
    assert not [name for name in tracked if name.startswith(("logs/", "analysis-cache/", ".venv/", "dist/"))]


def test_zip_contains_the_evaluation_data_and_only_tracked_files(tmp_path):
    target = build_zip.build(tmp_path, allow_dirty=True)
    with zipfile.ZipFile(target) as archive:
        names = archive.namelist()
        top = names[0].split("/")[0]
        inside = {name.split("/", 1)[1] for name in names}
        assert inside == {n for n in build_zip.tracked_files() if (ROOT / n).is_file()}
        for name in build_zip.REQUIRED:
            assert name in inside, name
        assert b"commit " in archive.comment
        assert archive.read(f"{top}/tests/manual/embeddings_holdout_standard.npz") == \
            (ROOT / "tests/manual/embeddings_holdout_standard.npz").read_bytes()
        bat = archive.read(f"{top}/install.bat")
        assert b"\r\n" in bat and b"\n" not in bat.replace(b"\r\n", b"")  # Windows 換行
        assert archive.testzip() is None
