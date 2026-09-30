"""解除安裝程式（由 uninstall.bat 執行，只用標準函式庫）。

安全原則
- 只會刪除下面「清單」裡的項目，而且每一項在刪除前都會再檢查一次「確實是程式資料夾底下、
  名稱完全符合」；找不到就不動。不接受任何自訂的刪除路徑。
- 絕對不會動：你的照片與影片、整理輸出的資料夾、資源回收筒、`logs\\復原移除` 裡的檔案，
  以及程式資料夾以外的任何東西（AI 模型快取除外，而且只刪本程式用的那兩個模型、需要你另外同意）。
- 是「連結」（符號連結／目錄連接點）的項目只移除連結本身，不會跟著刪到連結指向的內容。
- 程式還開著的時候拒絕執行；要刪的東西無法確認是這個程式的資料夾時拒絕執行。
- 會列出每一項會做什麼、多大，最後要你輸入 Y 才會開始；預設只移除「程式運作環境」（.venv），
  設定、整理紀錄、AI 模型都要你各自同意。
- 任何一項失敗都會如實報告並以非 0 結束，不會假裝成功。
"""

from __future__ import annotations

import argparse
import os
import shutil
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parent.parent
DATA_MARKER = ".media-sorter-data"  # 使用者資料夾裡的標記檔（跟 config.DATA_MARKER 相同；這裡不 import config 以免依賴它）
EXIT_OK = 0
EXIT_FAILED = 1
EXIT_RUNNING = 42  # --check-running：程式正在執行
EXIT_REMOVE_VENV_LATER = 10  # 正在用 .venv 裡的 Python 執行：請呼叫端（uninstall.bat）在結束後移除 .venv

# 程式資料夾裡「有可能是這個程式」的標記檔案：缺任何一個就拒絕執行（避免在錯的資料夾裡刪東西）
MARKER_FILES = ("media_sorter/__init__.py", "media_sorter/uninstaller.py", "install.bat", "start.bat")
SETTINGS_FILES = ("settings.json", "categories.json")
# logs 資料夾裡這個程式產生的檔案；`復原移除`（沒有資源回收筒時被搬開的複本）是你的檔案，永遠不刪
LOG_FILE_PATTERNS = ("整理紀錄_*.csv", "已復原_*.csv", "已完成_*.csv", "無法讀取_*.csv", "error.log", "console.log",
                     "app.lock")
QUARANTINE_DIR = "復原移除"
ACTIVE_LOG_PATTERN = "整理紀錄_*.csv"  # 還沒處理完（可以移除複本／處理原檔）的整理紀錄
# 本程式使用的 AI 模型（Hugging Face 快取資料夾名稱）；快取是跟其他程式共用的，所以只刪這兩個
MODEL_CACHE_DIRS = (
    "models--laion--CLIP-ViT-B-32-xlm-roberta-base-laion5B-s13B-b90k",
    "models--laion--CLIP-ViT-H-14-frozen-xlm-roberta-large-laion5B-s13B-b90k",
)


@dataclass
class Target:
    key: str            # venv／settings／logs／model
    label: str
    note: str
    paths: list[Path]   # 這一項要刪的路徑（每個都會在刪除前重新驗證）
    base: Path          # 這些路徑必須直接位於這個資料夾底下
    size: int = 0
    default: bool = False  # 沒有明確指定時是否移除
    only_files: bool = False  # True：只刪檔案（logs），不遞迴刪資料夾


# ---------------------------------------------------------------------------- 檢查
def _is_link(path: Path) -> bool:
    """符號連結或 Windows 目錄連接點（junction）。"""
    try:
        if path.is_symlink():
            return True
        attrs = getattr(os.lstat(path), "st_file_attributes", 0)
        return bool(attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    except OSError:
        return False


def validate_root(root: Path) -> str | None:
    """確認 root 真的是這個程式的資料夾；不是就回傳原因（拒絕執行）。"""
    try:
        root = Path(root).resolve()
    except OSError as exc:
        return f"無法確認程式資料夾：{exc}"
    if root == root.parent:
        return f"這看起來不是程式資料夾（{root}）。"
    home = Path.home().resolve()
    if root == home or root in home.parents:
        return f"程式資料夾不應該是使用者資料夾或它的上層（{root}）。"
    missing = [name for name in MARKER_FILES if not (root / name).is_file()]
    if missing:
        return f"在 {root} 找不到程式檔案（{'、'.join(missing)}），不確定這是本程式的資料夾，所以不會刪除任何東西。"
    return None


def validate_data_dir(data_dir: Path) -> str | None:
    """安裝版的使用者資料夾：必須真的存在、有本程式的標記檔，而且不是危險的位置。"""
    try:
        data_dir = Path(data_dir).resolve()
    except OSError as exc:
        return f"無法確認資料夾：{exc}"
    if not data_dir.is_dir():
        return f"找不到資料夾：{data_dir}"
    home = Path.home().resolve()
    if data_dir == data_dir.parent or data_dir == home or data_dir in home.parents:
        return f"這不是本程式的資料夾（{data_dir}）。"
    if not (data_dir / DATA_MARKER).is_file():
        return f"{data_dir} 裡沒有本程式的標記檔（{DATA_MARKER}），不確定是本程式的資料夾，所以不會刪除任何東西。"
    return None


def app_is_running(root: Path) -> bool:
    """程式開著時會鎖住 logs\\app.lock（跟視窗程式、install.bat 用的是同一個鎖）。沒有鎖檔就代表沒開；不會為了檢查而建立資料夾。

    root 是放 logs 資料夾的地方（免安裝版是程式資料夾，安裝版是使用者資料夾）。"""
    lock = root / "logs" / "app.lock"
    if not lock.is_file():
        return False
    try:
        handle = open(lock, "a+")
    except OSError:
        return True  # 打不開（例如被鎖住或權限不足），保守起見當作正在使用
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return False
    except OSError:
        return True
    finally:
        handle.close()


def hf_cache_dir(environ: dict | None = None) -> Path:
    env = os.environ if environ is None else environ
    if env.get("HF_HUB_CACHE"):
        return Path(env["HF_HUB_CACHE"])
    if env.get("HF_HOME"):
        return Path(env["HF_HOME"]) / "hub"
    if env.get("XDG_CACHE_HOME"):
        return Path(env["XDG_CACHE_HOME"]) / "huggingface" / "hub"
    return Path.home() / ".cache" / "huggingface" / "hub"


def dir_size(path: Path, limit_seconds: float = 20.0) -> int:
    """資料夾大小（不跟隨連結）；太久就先回傳目前累計的數字。"""
    import time

    end = time.time() + limit_seconds
    total = 0
    stack = [path]
    while stack and time.time() < end:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        else:
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def fmt_size(n: int) -> str:
    if n >= 1024**3:
        return f"{n / 1024**3:.1f} GB"
    if n >= 1024**2:
        return f"{n / 1024**2:.0f} MB"
    return "不到 1 MB" if n else "0"


def running_inside_venv(root: Path, prefix: str | None = None) -> bool:
    """目前執行這個程式的 Python 是否就是 .venv 裡的（那樣的話 .venv 沒辦法自己刪自己）。"""
    venv = (root / ".venv").resolve()
    for candidate in ([prefix] if prefix is not None else [sys.prefix, sys.executable]):
        try:
            resolved = Path(candidate).resolve()
        except OSError:
            continue
        if resolved == venv or venv in resolved.parents:
            return True
    return False


# ---------------------------------------------------------------------------- 清單
def count_active_logs(data: Path) -> int:
    logs = Path(data) / "logs"
    return len(list(logs.glob(ACTIVE_LOG_PATTERN))) if logs.is_dir() else 0


def collect(root: Path, cache_dir: Path | None = None, data_dir: Path | None = None,
            include_venv: bool = True) -> list[Target]:
    """root：程式資料夾；data_dir：使用者資料所在（免安裝版就是 root）。"""
    root = Path(root)
    data = Path(data_dir) if data_dir is not None else root
    targets: list[Target] = []
    venv = root / ".venv"
    if include_venv and (venv.exists() or _is_link(venv)):
        targets.append(Target("venv", "程式運作環境（.venv：Python 與 PyTorch 等套件）",
                              "解除安裝的主體；之後要再用，重新執行 install.bat 即可。", [venv], root,
                              0 if _is_link(venv) else dir_size(venv), default=True))
    settings = [data / name for name in SETTINGS_FILES if (data / name).is_file()]
    if settings:
        targets.append(Target("settings", "你的設定與分類清單（" + "、".join(p.name for p in settings) + "）",
                              "刪除後，重新安裝會回到預設分類與設定。", settings, data,
                              sum(p.stat().st_size for p in settings)))
    logs = data / "logs"
    log_files = sorted({p for pattern in LOG_FILE_PATTERNS for p in logs.glob(pattern) if p.is_file()}) \
        if logs.is_dir() else []
    if log_files:
        active = count_active_logs(data)
        note = "這些是每次整理的紀錄與錯誤紀錄，不含你的照片。"
        if active:
            note += (f"\n    ⚠ 其中有 {active} 份整理紀錄還沒處理完；刪除後就無法再用本程式「移除這次建立的複本」或"
                     "「將這批原檔移到回收筒」（檔案本身不受影響）。")
        targets.append(Target("logs", f"整理紀錄與錯誤紀錄（logs 資料夾裡的 {len(log_files)} 個檔案）", note,
                              log_files, logs, sum(p.stat().st_size for p in log_files), only_files=True))
    cache = Path(cache_dir) if cache_dir is not None else hf_cache_dir()
    models = [cache / name for name in MODEL_CACHE_DIRS if (cache / name).exists() or _is_link(cache / name)]
    if models:
        targets.append(Target("model", "本程式使用的 AI 模型檔案（Hugging Face 快取）",
                              f"位置：{cache}\n    ⚠ 這個快取可能跟其他程式共用；刪除後下次使用要重新下載。只會刪本程式的 "
                              f"{len(models)} 個模型資料夾，不會動快取裡的其他東西。", models, cache,
                              sum(0 if _is_link(p) else dir_size(p) for p in models)))
    return targets


# ---------------------------------------------------------------------------- 刪除
def _make_writable_and_retry(func, path, exc_info) -> None:
    """Windows 上唯讀檔案無法刪除：取消唯讀後重試一次，還是不行才讓錯誤往上丟。"""
    os.chmod(path, stat.S_IWRITE)
    func(path)


def _rmtree(path: Path) -> None:
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=lambda func, p, exc: _make_writable_and_retry(func, p, exc))
    else:
        shutil.rmtree(path, onerror=_make_writable_and_retry)


def _remove_one(path: Path, base: Path, only_file: bool) -> None:
    """刪一個項目。每次都重新驗證：直接位於 base 底下、不是 base 本身；連結只移除連結。"""
    if path.parent.resolve() != base.resolve() or path.name in ("", ".", ".."):
        raise PermissionError(f"拒絕刪除：{path} 不在預期的位置（{base}）")
    if _is_link(path):  # 只移除連結本身
        try:
            os.unlink(path)
        except (IsADirectoryError, PermissionError):
            os.rmdir(path)  # Windows 的目錄連接點
        return
    if not path.exists():
        return
    if path.is_dir():
        if only_file:
            raise PermissionError(f"拒絕刪除資料夾：{path}")
        _rmtree(path)
    else:
        try:
            path.unlink()
        except PermissionError:
            os.chmod(path, stat.S_IWRITE)
            path.unlink()


def remove_target(target: Target) -> list[str]:
    """刪除一項；回傳失敗訊息（空清單代表全部成功）。"""
    problems: list[str] = []
    for path in target.paths:
        try:
            _remove_one(path, target.base, target.only_files)
        except OSError as exc:
            problems.append(f"{path}：{exc}")
            continue
        if path.exists() or _is_link(path):
            problems.append(f"{path}：仍然存在（可能被其他程式使用中）")
    if target.key == "logs":  # 只有空的 logs 資料夾才會一起移除；有「復原移除」等東西就留著
        try:
            target.base.rmdir()
        except OSError:
            pass
    return problems


# ---------------------------------------------------------------------------- 主流程
def _print_plan(root: Path, targets: list[Target], say: Callable[[str], None], data: Path | None = None) -> None:
    say("")
    say(f"程式資料夾：{root}" if data is None else f"使用者資料夾：{data}")
    say("")
    say("【不會被動到】你的照片與影片、整理輸出的資料夾（例如「已分類」）、資源回收筒、")
    say("　　　　　　　logs\\復原移除 裡的檔案，以及程式資料夾以外的其他東西。")
    quarantine = (data or root) / "logs" / QUARANTINE_DIR
    if quarantine.is_dir():
        say(f"　　　　　　　（注意：{quarantine} 裡有復原時搬開的複本，這些是你的檔案，會保留。）")
    say("")
    say("【可以移除的項目】")
    for i, t in enumerate(targets, 1):
        say(f"  {i}. {t.label}　（大小：{fmt_size(t.size)}）")
        say(f"    {t.note}")


def run(argv: list[str] | None = None, *, root: Path | None = None, cache_dir: Path | None = None,
        prefix: str | None = None, input_func: Callable[[str], str] = input,
        say: Callable[[str], None] = print) -> int:
    parser = argparse.ArgumentParser(prog="media_sorter.uninstaller", description="解除安裝 AI 媒體分類器")
    parser.add_argument("--yes", action="store_true", help="不詢問，直接移除選定的項目（預設只有 .venv）")
    parser.add_argument("--dry-run", action="store_true", help="只列出會做什麼，不刪除任何東西")
    parser.add_argument("--remove-settings", action="store_true", help="一併移除設定與分類清單")
    parser.add_argument("--remove-logs", action="store_true", help="一併移除整理紀錄與錯誤紀錄")
    parser.add_argument("--remove-model", action="store_true", help="一併移除本程式使用的 AI 模型檔案")
    parser.add_argument("--data-only", action="store_true",
                        help="只處理使用者資料（設定、整理紀錄、AI 模型）；給安裝程式的解除安裝流程使用，需搭配 --data-dir")
    parser.add_argument("--data-dir", help="使用者資料夾（安裝版：%%LOCALAPPDATA%%\\AI Media Sorter）")
    parser.add_argument("--check-running", action="store_true", help="程式開著就回傳 42，否則回傳 0")
    parser.add_argument("--active-logs", action="store_true", help="以結束代碼回傳還沒處理完的整理紀錄數量（最多 100）")
    args = parser.parse_args(argv)

    if args.data_only or args.check_running or args.active_logs:
        return _run_data_mode(args, root, cache_dir, input_func, say)

    root = Path(root) if root is not None else ROOT
    say("=" * 60)
    say(" AI 媒體分類器 - 解除安裝")
    say("=" * 60)
    if (root / "installed.flag").is_file():
        say("\n✘ 這是用安裝程式安裝的版本，請從 Windows「設定 → 應用程式 → 已安裝的應用程式」或"
            "開始功能表的「解除安裝」來移除。")
        return EXIT_FAILED
    problem = validate_root(root)
    if problem:
        say(f"\n✘ {problem}")
        return EXIT_FAILED
    root = root.resolve()
    if app_is_running(root):
        say("\n✘ AI 媒體分類器正在執行中，請先關閉程式視窗再解除安裝。")
        return EXIT_FAILED

    targets = collect(root, cache_dir)
    if not targets:
        say("\n沒有找到需要移除的項目（可能已經解除安裝過了）。")
        return EXIT_OK
    _print_plan(root, targets, say)
    wanted = {"settings": args.remove_settings, "logs": args.remove_logs, "model": args.remove_model}
    chosen: list[Target] = []
    interactive = not args.yes and not args.dry_run
    say("")
    for t in targets:
        if t.default:
            chosen.append(t)
        elif interactive:
            try:
                answer = input_func(f"也要移除「{t.label.split('（')[0]}」嗎？（輸入 Y 移除，其他鍵保留）：").strip()
            except EOFError:
                answer = ""
            if answer.lower() == "y":
                chosen.append(t)
        elif wanted.get(t.key):
            chosen.append(t)

    say("")
    say("【這次會移除】" if not args.dry_run else "【（試跑）會移除】")
    for t in chosen:
        say(f"  ・{t.label}　（大小：{fmt_size(t.size)}）")
    kept = [t for t in targets if t not in chosen]
    for t in kept:
        say(f"  （保留）{t.label.split('（')[0]}")
    if args.dry_run:
        say("\n這是試跑，沒有刪除任何東西。")
        return EXIT_OK
    if not args.yes:
        try:
            answer = input_func("\n確定要開始嗎？（輸入 Y 開始，其他鍵取消）：").strip()
        except EOFError:
            answer = ""
        if answer.lower() != "y":
            say("已取消，沒有刪除任何東西。")
            return EXIT_FAILED

    failures: list[str] = []
    later = False
    for t in chosen:
        if t.key == "venv" and running_inside_venv(root, prefix):
            if _is_link(t.paths[0]):
                failures.append(f"{t.paths[0]}：是連結，且目前正用它裡面的 Python 執行；請關閉後手動刪除")
            else:
                later = True  # 由 uninstall.bat 在這個程式結束後移除
            continue
        say(f"移除：{t.label.split('（')[0]}⋯")
        problems = remove_target(t)
        failures += problems
        say("  ✔ 完成" if not problems else "  ✘ 有項目無法移除")

    say("")
    if failures:
        say("✘ 有些項目沒有移除成功（其餘已完成）：")
        for line in failures:
            say(f"  ・{line}")
        say("  請關閉可能正在使用這些檔案的程式（例如檔案總管視窗、防毒軟體掃描）後，再執行一次 uninstall.bat。")
        return EXIT_FAILED
    if later:
        say("其餘項目已移除；運作環境（.venv）會在這個視窗的程式結束後移除。")
        return EXIT_REMOVE_VENV_LATER
    say("✔ 解除安裝完成。")
    say("  程式資料夾本身（包含 install.bat、start.bat 與這個檔案）沒有被刪除，")
    say("  不需要時請自行刪除整個資料夾。你的照片與整理結果都沒有被動到。")
    return EXIT_OK


def _run_data_mode(args, root, cache_dir, input_func, say) -> int:
    """安裝版解除安裝時用：只處理使用者資料夾裡的東西（程式檔案由安裝程式自己移除）。"""
    root = Path(root) if root is not None else ROOT
    if not args.data_dir:
        say("✘ 需要 --data-dir。")
        return EXIT_FAILED
    data = Path(args.data_dir)
    if args.check_running or args.active_logs:
        # 只讀取資訊，不需要標記檔；資料夾不存在就代表沒有東西
        if args.check_running:
            return EXIT_RUNNING if app_is_running(data) else EXIT_OK
        return min(count_active_logs(data), 100)
    problem = validate_data_dir(data)
    if problem:
        say(f"\n✘ {problem}")
        return EXIT_FAILED
    data = data.resolve()
    if app_is_running(data):
        say("\n✘ AI 媒體分類器正在執行中，請先關閉程式視窗。")
        return EXIT_FAILED
    targets = collect(root, cache_dir, data_dir=data, include_venv=False)
    if not targets:
        say("\n沒有找到需要移除的使用者資料。")
        return EXIT_OK
    wanted = {"settings": args.remove_settings, "logs": args.remove_logs, "model": args.remove_model}
    chosen = [t for t in targets if wanted.get(t.key)]
    if args.dry_run:
        say("這是試跑，會移除：" + ("、".join(t.label for t in chosen) or "（無）"))
        return EXIT_OK
    failures: list[str] = []
    for t in chosen:
        say(f"移除：{t.label.split('（')[0]}⋯")
        failures += remove_target(t)
    if failures:
        say("✘ 有些項目沒有移除成功：")
        for line in failures:
            say(f"  ・{line}")
        return EXIT_FAILED
    # 沒有其他東西了才順便移除標記檔與（空的）資料夾；還有使用者的檔案（例如復原移除）就整個保留
    remaining = [p for p in data.iterdir() if p.name != DATA_MARKER]
    if not remaining:
        try:
            (data / DATA_MARKER).unlink()
            data.rmdir()
        except OSError:
            pass
    say("✔ 完成。")
    return EXIT_OK


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    return run()


if __name__ == "__main__":
    sys.exit(main())
