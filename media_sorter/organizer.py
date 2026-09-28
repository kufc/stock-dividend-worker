"""依分類搬移／複製檔案並重新命名；每次操作都寫一份紀錄檔（CSV），可一鍵復原。"""

from __future__ import annotations

import csv
import os
import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

INVALID_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
LOG_FIELDS = ["動作", "原始路徑", "新路徑", "分類", "時間"]
SEQ = "{序號}"


def safe_name(text: str, fallback: str = "未命名") -> str:
    """把文字轉成 Windows 可用的檔名／資料夾名稱。"""
    name = INVALID_CHARS.sub("_", text).strip().rstrip(". ")
    if not name:
        return fallback
    if name.split(".")[0].upper() in RESERVED_NAMES:
        name = "_" + name
    return name[:100]


def render_name(pattern: str, *, category: str, date: datetime, stem: str, seq: int, width: int) -> str:
    name = (pattern.replace("{分類}", category)
            .replace("{日期}", date.strftime("%Y%m%d"))
            .replace("{原檔名}", stem)
            .replace(SEQ, str(seq).zfill(width)))
    return safe_name(name, fallback=stem)


@dataclass
class PlannedOp:
    src: Path
    dst: Path
    category: str


def plan_operations(
    entries: list[tuple[Path, str, datetime]],
    output_dir: Path,
    rename: bool = True,
    pattern: str = "{分類}_{序號}",
) -> list[PlannedOp]:
    """決定每個檔案的目的地。entries 是 (檔案路徑, 分類, 日期)。

    - 每個分類一個子資料夾
    - 重新命名時依日期排序編號；和已存在的檔案撞名會自動往後編號
    """
    output_dir = Path(output_dir)
    groups: dict[str, list[tuple[Path, datetime]]] = {}
    for path, category, date in entries:
        groups.setdefault(category, []).append((Path(path), date))

    ops: list[PlannedOp] = []
    for category, files in groups.items():
        folder_name = safe_name(category)
        folder = output_dir / folder_name
        existing = {p.name.lower() for p in folder.iterdir()} if folder.is_dir() else set()
        used: set[str] = set(existing)
        counters: dict[str, int] = {}
        width = max(3, len(str(len(files))))
        files.sort(key=lambda f: (f[1], str(f[0]).lower()))
        for src, date in files:
            if rename:
                ext = src.suffix.lower()
                fields = dict(category=folder_name, date=date, stem=src.stem, width=width)
                # 樣式沒有 {序號} 時先試不加序號的名稱，撞名才在後面加上序號
                candidate = render_name(pattern, seq=0, **fields) + ext if SEQ not in pattern else None
                if candidate is None or candidate.lower() in used:
                    template = pattern if SEQ in pattern else pattern + "_" + SEQ
                    key = render_name(template, seq=0, **fields)  # 同一前綴（如同分類同日期）共用一組編號
                    seq = counters.get(key, 1 if SEQ in pattern else 2)
                    while True:
                        candidate = render_name(template, seq=seq, **fields) + ext
                        seq += 1
                        if candidate.lower() not in used:
                            break
                    counters[key] = seq
            else:
                candidate, n = src.name, 2
                while candidate.lower() in used:
                    candidate = f"{src.stem} ({n}){src.suffix}"
                    n += 1
            used.add(candidate.lower())
            ops.append(PlannedOp(src, folder / candidate, category))
    return ops


@dataclass
class ExecuteResult:
    done: int
    errors: list[tuple[Path, str]]
    log_path: Path | None


def execute(
    ops: list[PlannedOp],
    action: str,
    log_dir: Path,
    on_progress: Callable[[int, int], None] | None = None,
) -> ExecuteResult:
    """執行搬移（move）或複製（copy）。每完成一個檔案就寫入紀錄，中途失敗也能復原已完成的部分。"""
    if action not in ("move", "copy"):
        raise ValueError(f"未知的動作：{action}")
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"整理紀錄_{datetime.now():%Y%m%d_%H%M%S}.csv"
    errors: list[tuple[Path, str]] = []
    done = 0
    with open(log_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(LOG_FIELDS)
        for i, op in enumerate(ops, 1):
            try:
                if op.dst.exists():
                    raise FileExistsError(f"目的地已有同名檔案：{op.dst}")
                op.dst.parent.mkdir(parents=True, exist_ok=True)
                if action == "move":
                    shutil.move(str(op.src), str(op.dst))
                else:
                    shutil.copy2(op.src, op.dst)
                writer.writerow([action, str(op.src), str(op.dst), op.category, datetime.now().isoformat(timespec="seconds")])
                f.flush()
                done += 1
            except OSError as exc:
                errors.append((op.src, str(exc)))
            if on_progress:
                on_progress(i, len(ops))
    if done == 0:
        log_path.unlink(missing_ok=True)
        log_path = None
    return ExecuteResult(done, errors, log_path)


def latest_log(log_dir: Path) -> Path | None:
    logs = sorted(Path(log_dir).glob("整理紀錄_*.csv"))
    return logs[-1] if logs else None


def undo(log_path: Path) -> ExecuteResult:
    """依紀錄檔復原：搬移的檔案搬回原位、複製出來的檔案刪除。完成後紀錄檔改名為「已復原」。"""
    log_path = Path(log_path)
    with open(log_path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    errors: list[tuple[Path, str]] = []
    done = 0
    for row in reversed(rows):
        src, dst = Path(row["原始路徑"]), Path(row["新路徑"])
        try:
            if not dst.exists():
                raise FileNotFoundError(f"找不到檔案（可能已被移動或刪除）：{dst}")
            if row["動作"] == "move":
                if src.exists():
                    raise FileExistsError(f"原位置已有同名檔案：{src}")
                src.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(dst), str(src))
            else:
                dst.unlink()
            done += 1
            _remove_if_empty(dst.parent)
        except OSError as exc:
            errors.append((dst, str(exc)))
    os.replace(log_path, log_path.with_name(log_path.stem.replace("整理紀錄", "已復原") + log_path.suffix))
    return ExecuteResult(done, errors, log_path)


def _remove_if_empty(folder: Path) -> None:
    try:
        folder.rmdir()  # 只有空資料夾才會成功
    except OSError:
        pass
