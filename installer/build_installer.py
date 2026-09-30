"""建置 Windows 安裝程式（setup.exe）。可以在 Linux 或 Windows 上執行。

需要：
  - NSIS 的 makensis（Linux：apt install nsis；Windows：安裝 NSIS 並加入 PATH）
  - 內建的 Python 執行環境（含 tkinter）。預設用 py-rattler 從 conda-forge 取得 Windows 版
    （pip install py-rattler），也可以用 --runtime 指定已經準備好的資料夾。

用法：
  python installer/build_installer.py [--out 輸出資料夾] [--runtime 已準備好的 Python 資料夾]
                                       [--publisher 發行者名稱] [--python 3.12]
輸出：AI-Media-Sorter-Setup-<版本>.exe
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

APP_FILES = ["requirements.txt", "README.md", "使用說明與免責聲明_Usage-and-Disclaimer.txt"]
# 內建 Python 執行環境裡用不到、可以刪掉以縮小安裝檔的東西
RUNTIME_TRIM_DIRS = ["conda-meta", "include", "libs", "Tools", "Lib/test", "Library/include", "Library/lib/pkgconfig",
                     "Library/lib/cmake", "Library/share", "etc", "share", "Lib/idlelib/idle_test"]
RUNTIME_PACKAGES = ["python 3.12.*", "tk", "pip", "vc14_runtime"]


def provision_runtime(target: Path, python_spec: str) -> list[str]:
    """用 py-rattler 從 conda-forge 取得 Windows 64 位元的 Python（含 tkinter 與 pip）。回傳套件清單（寫進安裝檔供查閱）。"""
    try:
        from rattler import Platform, install, solve
    except ImportError as exc:  # pragma: no cover
        raise SystemExit("找不到 py-rattler：請先執行 pip install py-rattler，或用 --runtime 指定已準備好的資料夾") from exc

    async def go() -> list[str]:
        specs = [f"python {python_spec}.*" if s.startswith("python") else s for s in RUNTIME_PACKAGES]
        records = await solve(["conda-forge"], specs, platforms=[Platform("win-64"), Platform("noarch")])
        await install(records, str(target), platform=Platform("win-64"))
        return sorted(f"{r.name.normalized} {r.version} ({r.file_name})" for r in records)

    return asyncio.run(go())


def slim_runtime(runtime: Path) -> None:
    for rel in RUNTIME_TRIM_DIRS:
        shutil.rmtree(runtime / rel, ignore_errors=True)
    for pattern in ("*.pdb", "*.lib", "*.pyc"):
        for path in runtime.rglob(pattern):
            if "site-packages" in path.parts and path.suffix == ".lib":
                continue
            path.unlink(missing_ok=True)
    for cache in runtime.rglob("__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)
    for tests in runtime.glob("Lib/site-packages/**/tests"):
        shutil.rmtree(tests, ignore_errors=True)


def stage(out: Path, runtime: Path, packages: list[str]) -> Path:
    staging = out / "stage"
    shutil.rmtree(staging, ignore_errors=True)
    (staging / "app").mkdir(parents=True)
    shutil.copytree(runtime, staging / "python", symlinks=False)
    shutil.copytree(ROOT / "media_sorter", staging / "app" / "media_sorter",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for name in APP_FILES:
        shutil.copy2(ROOT / name, staging / "app" / name)
    shutil.copy2(HERE / "setup-deps.cmd", staging / "app" / "setup-deps.cmd")
    shutil.copy2(ROOT / APP_FILES[-1], staging / "license.txt")  # 安裝程式「授權」頁用（檔名用英文，避開不同語系的檔名問題）
    (staging / "app" / "installed.flag").write_text("installed by setup.exe\n", encoding="ascii")
    (staging / "app" / "RUNTIME-PACKAGES.txt").write_text(
        "Bundled Python runtime (from conda-forge, win-64). Licenses: see python/LICENSE_PYTHON.txt and the\n"
        "license files of each package.\n\n" + "\n".join(packages) + "\n", encoding="utf-8")
    return staging


def main() -> int:
    parser = argparse.ArgumentParser(description="建置 AI 媒體分類器的 Windows 安裝程式")
    parser.add_argument("--out", default=str(ROOT / "dist"), help="輸出資料夾（預設 dist）")
    parser.add_argument("--runtime", help="已準備好的 Python 執行環境資料夾（含 python.exe 與 tkinter）")
    parser.add_argument("--python", default="3.12", help="內建 Python 的版本（預設 3.12）")
    parser.add_argument("--publisher", default="AI Media Sorter", help="顯示在「設定 → 應用程式」的發行者名稱")
    parser.add_argument("--makensis", default="makensis", help="makensis 的路徑")
    args = parser.parse_args()

    from media_sorter import __version__

    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    if args.runtime:
        runtime = Path(args.runtime).resolve()
        packages = ["(runtime supplied with --runtime)"]
    else:
        runtime = out / "runtime"
        shutil.rmtree(runtime, ignore_errors=True)
        print("取得內建 Python 執行環境（conda-forge）⋯")
        packages = provision_runtime(runtime, args.python)
    if not (runtime / "python.exe").is_file() or not (runtime / "pythonw.exe").is_file():
        raise SystemExit(f"{runtime} 裡找不到 python.exe／pythonw.exe")
    work = out / "runtime-slim"
    shutil.rmtree(work, ignore_errors=True)
    shutil.copytree(runtime, work)
    slim_runtime(work)
    staging = stage(out, work, packages)
    shutil.rmtree(work, ignore_errors=True)

    exe = out / f"AI-Media-Sorter-Setup-{__version__}.exe"
    cmd = [args.makensis, "-V2", f"-DVERSION={__version__}", f"-DSTAGE={staging}", f"-DOUTFILE={exe}",
           f"-DPUBLISHER={args.publisher}", str(HERE / "setup.nsi")]
    print(" ".join(cmd))
    env = dict(os.environ)
    if os.name != "nt":  # Linux 上讓 makensis 能讀寫中文檔名
        env["LC_ALL"] = "C.UTF-8"
    subprocess.run(cmd, check=True, cwd=HERE, env=env)
    print(f"\n完成：{exe}（{exe.stat().st_size / 1e6:.0f} MB）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
