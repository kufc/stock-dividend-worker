"""依分類搬移／複製檔案並重新命名；每次操作都寫一份紀錄檔（CSV），可一鍵復原。

安全原則：
- 絕不覆蓋既有檔案（目的地已存在就換名字或回報錯誤）
- 複製先寫到「.partial」暫存檔，完成後才改成正式檔名；失敗就刪掉暫存檔
- 每完成一個檔案就寫入紀錄；紀錄寫不進去就立刻停止，避免出現無法復原的檔案
- 復原複製時，原檔不見了就把複本搬回原位（而不是刪掉唯一的一份）；複本被改過就不刪
"""

from __future__ import annotations

import csv
import errno
import os
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

INVALID_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
LOG_FIELDS = ["動作", "原始路徑", "新路徑", "分類", "時間", "大小", "修改時間"]
LOG_PREFIX = "整理紀錄_"
UNDONE_PREFIX = "已復原_"
SEQ = "{序號}"
MAX_NAME = 100  # 檔名（不含副檔名）上限，避免超過 Windows 路徑長度限制
PARTIAL_SUFFIX = ".partial"
WINERROR_NOT_SAME_DEVICE = 17


class LogWriteError(OSError):
    """紀錄檔寫入失敗：為了確保每個動過的檔案都能復原，整批作業會立即停止。"""


def safe_name(text: str, fallback: str = "未命名", max_len: int = MAX_NAME) -> str:
    """把文字轉成 Windows 可用的檔名／資料夾名稱。"""
    name = INVALID_CHARS.sub("_", text).strip().rstrip(". ")[:max_len].rstrip(". ")
    if not name:
        return fallback
    if name.split(".")[0].upper() in RESERVED_NAMES:
        name = "_" + name
    return name


def folder_key(category: str) -> str:
    """分類對應的資料夾（Windows 不分大小寫，所以用 casefold 比較）。"""
    return safe_name(category).casefold()


def render_name(pattern: str, *, category: str, date: datetime, stem: str, seq: int, width: int,
                max_len: int = MAX_NAME) -> str:
    """套用命名樣式。太長時只截短分類名稱與原檔名，序號一定完整保留（否則會一直撞名）。"""
    seq_text = str(seq).zfill(width)
    cat, stm = category, stem
    n_cat, n_stem = pattern.count("{分類}"), pattern.count("{原檔名}")
    while True:
        name = safe_name(
            pattern.replace("{分類}", cat).replace("{日期}", date.strftime("%Y%m%d"))
            .replace("{原檔名}", stm).replace(SEQ, seq_text),
            fallback=stem or "未命名", max_len=10_000,
        )
        excess = len(name) - max_len
        if excess <= 0:
            return name
        if n_stem and len(stm) > 1:
            stm = stm[:max(1, len(stm) - -(-excess // n_stem))]
        elif n_cat and len(cat) > 1:
            cat = cat[:max(1, len(cat) - -(-excess // n_cat))]
        else:  # 樣式本身的固定文字就太長：保留尾端（含序號）
            return name[-max_len:].lstrip(". ") or seq_text


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

    - 每個分類一個子資料夾（名稱只差大小寫或特殊字元的分類會共用同一個資料夾與編號）
    - 重新命名時依日期排序編號；和已存在的檔案撞名會自動往後編號
    """
    output_dir = Path(output_dir)
    groups: dict[str, list[tuple[Path, str, datetime]]] = {}
    folder_names: dict[str, str] = {}
    seen_src: set[str] = set()
    for path, category, date in entries:
        path = Path(path)
        if str(path).casefold() in seen_src:  # 同一個檔案不會被排兩次
            continue
        seen_src.add(str(path).casefold())
        key = folder_key(category)
        folder_names.setdefault(key, safe_name(category))
        groups.setdefault(key, []).append((path, category, date))

    ops: list[PlannedOp] = []
    for key, files in groups.items():
        folder_name = folder_names[key]
        folder = output_dir / folder_name
        used = {p.name.casefold() for p in folder.iterdir()} if folder.is_dir() else set()
        counters: dict[str, int] = {}
        width = max(3, len(str(len(files))))
        limit = len(used) + len(files) + 10
        files.sort(key=lambda f: (f[2], str(f[0]).lower()))
        for src, category, date in files:
            if rename:
                ext = src.suffix.lower()
                fields = dict(category=folder_name, date=date, stem=src.stem, width=width)
                # 樣式沒有 {序號} 時先試不加序號的名稱，撞名才在後面加上序號
                candidate = render_name(pattern, seq=0, **fields) + ext if SEQ not in pattern else None
                if candidate is None or candidate.casefold() in used:
                    template = pattern if SEQ in pattern else pattern + "_" + SEQ
                    prefix = render_name(template, seq=0, **fields)  # 同一前綴（如同分類同日期）共用一組編號
                    seq = counters.get(prefix, 1 if SEQ in pattern else 2)
                    for _ in range(limit):
                        candidate = render_name(template, seq=seq, **fields) + ext
                        seq += 1
                        if candidate.casefold() not in used:
                            break
                    else:
                        raise RuntimeError(f"無法替 {src.name} 產生不重複的檔名，請改用較短的命名樣式")
                    counters[prefix] = seq
            else:
                candidate, n = src.name, 2
                while candidate.casefold() in used:
                    candidate = f"{src.stem} ({n}){src.suffix}"
                    n += 1
            used.add(candidate.casefold())
            ops.append(PlannedOp(src, folder / candidate, category))
    return ops


# ---------------------------------------------------------------------------- 檔案操作（不覆蓋）
def _is_cross_device(exc: OSError) -> bool:
    return exc.errno == errno.EXDEV or getattr(exc, "winerror", None) == WINERROR_NOT_SAME_DEVICE


def _rename_no_clobber(src: Path, dst: Path) -> None:
    """同一磁碟內改名／搬移；目的地已存在時一定失敗（不會覆蓋）。"""
    if os.name == "nt":
        os.rename(src, dst)  # Windows 的 rename 本來就不會覆蓋
        return
    try:
        os.link(src, dst)  # 目的地存在時會失敗
    except OSError as exc:
        if exc.errno in (errno.EEXIST, errno.EXDEV):
            raise
        if dst.exists():  # 檔案系統不支援硬連結（如 exFAT）
            raise FileExistsError(errno.EEXIST, "目的地已有同名檔案", str(dst)) from exc
        os.rename(src, dst)
        return
    os.unlink(src)


def _copy_no_clobber(src: Path, dst: Path) -> None:
    """先複製到 .partial 暫存檔，完整寫完才改成正式檔名；失敗會清掉暫存檔。"""
    tmp = dst.with_name(dst.name + PARTIAL_SUFFIX)
    try:
        shutil.copy2(src, tmp)
        _rename_no_clobber(tmp, dst)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


@dataclass
class ExecuteResult:
    done: int
    errors: list[tuple[Path, str]]
    log_path: Path | None
    warnings: list[str] = field(default_factory=list)
    aborted: bool = False
    done_sources: list[Path] = field(default_factory=list)  # 成功處理的原始檔案


def _open_new_log(log_dir: Path):
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    for n in range(1000):
        path = log_dir / f"{LOG_PREFIX}{stamp}{'' if n == 0 else f'_{n}'}.csv"
        try:
            return path, open(path, "x", newline="", encoding="utf-8-sig")
        except FileExistsError:
            continue
    raise OSError("無法建立紀錄檔")


def execute(
    ops: list[PlannedOp],
    action: str,
    log_dir: Path,
    on_progress: Callable[[int, int], None] | None = None,
) -> ExecuteResult:
    """執行搬移（move）或複製（copy）。每完成一個檔案就寫入紀錄，中途失敗也能復原已完成的部分。"""
    if action not in ("move", "copy"):
        raise ValueError(f"未知的動作：{action}")
    log_path, f = _open_new_log(Path(log_dir))
    errors: list[tuple[Path, str]] = []
    warnings: list[str] = []
    done = 0
    rows = 0
    aborted = False
    done_sources: list[Path] = []
    with f:
        writer = csv.writer(f)
        writer.writerow(LOG_FIELDS)
        f.flush()
        for i, op in enumerate(ops, 1):
            logged_action = action
            try:
                if op.dst.exists():
                    raise FileExistsError(f"目的地已有同名檔案：{op.dst}")
                op.dst.parent.mkdir(parents=True, exist_ok=True)
                if action == "copy":
                    _copy_no_clobber(op.src, op.dst)
                else:
                    try:
                        _rename_no_clobber(op.src, op.dst)
                    except OSError as exc:
                        if not _is_cross_device(exc):
                            raise
                        _copy_no_clobber(op.src, op.dst)  # 跨磁碟：先完整複製再刪原檔
                        try:
                            os.unlink(op.src)
                        except OSError as unlink_exc:
                            logged_action = "copy"  # 原檔刪不掉：當作複製記錄，復原時會移除這份複本
                            warnings.append(f"{op.src.name}：已複製到新位置，但無法刪除原檔（{unlink_exc}）")
            except OSError as exc:
                errors.append((op.src, str(exc)))
            else:
                try:
                    st = op.dst.stat()
                    writer.writerow([logged_action, str(op.src), str(op.dst), op.category,
                                     datetime.now().isoformat(timespec="seconds"), st.st_size, st.st_mtime_ns])
                    f.flush()
                    os.fsync(f.fileno())
                except OSError as exc:
                    errors.append((op.src, f"紀錄檔寫入失敗，已停止整理：{exc}"))
                    aborted = True
                    break
                rows += 1
                done += 1
                done_sources.append(op.src)
            if on_progress:
                on_progress(i, len(ops))
    if rows == 0 and not aborted:
        log_path.unlink(missing_ok=True)
        log_path = None
    return ExecuteResult(done, errors, log_path, warnings, aborted, done_sources)


# ---------------------------------------------------------------------------- 復原
def latest_log(log_dir: Path) -> Path | None:
    logs = [p for p in Path(log_dir).glob(f"{LOG_PREFIX}*.csv")]
    return max(logs, key=lambda p: (p.stat().st_mtime_ns, p.name)) if logs else None


def read_log(log_path: Path) -> list[dict]:
    """讀取紀錄檔。被 Excel 另存成 Big5（cp950）也讀得懂。"""
    raw = Path(log_path).read_bytes()
    for encoding in ("utf-8-sig", "cp950", "mbcs"):
        try:
            text = raw.decode(encoding)
            break
        except (UnicodeDecodeError, LookupError):
            continue
    else:
        raise ValueError("紀錄檔的編碼無法辨識")
    rows = list(csv.DictReader(text.splitlines()))
    required = {"動作", "原始路徑", "新路徑"}
    if rows and not required <= set(rows[0]):
        raise ValueError("紀錄檔格式不正確（缺少必要欄位）")
    return rows


def _move_back(dst: Path, src: Path) -> None:
    src.parent.mkdir(parents=True, exist_ok=True)
    try:
        _rename_no_clobber(dst, src)
    except OSError as exc:
        if not _is_cross_device(exc):
            raise
        _copy_no_clobber(dst, src)
        os.unlink(dst)


def _delete(path: Path) -> None:
    """優先丟到資源回收筒（可救回），不行才直接刪除。"""
    try:
        from send2trash import send2trash

        send2trash(str(path))
    except ImportError:
        path.unlink()


def _unchanged(path: Path, row: dict) -> bool:
    try:
        st = path.stat()
        size, mtime = row.get("大小"), row.get("修改時間")
        if size and int(size) != st.st_size:
            return False
        if mtime and int(mtime) != st.st_mtime_ns:
            return False
    except (OSError, ValueError):
        return False
    return True


def undo(log_path: Path) -> ExecuteResult:
    """依紀錄檔復原：搬移的檔案搬回原位；複製出來的檔案移到資源回收筒。

    - 複製的原檔已經不在：把複本搬回原位（不會刪掉唯一的一份）
    - 複本在整理後被修改過：不刪除，列為失敗
    - 全部成功才把紀錄改名為「已復原」；有失敗時紀錄只留下失敗的列，下次按復原會重試這些檔案
    """
    log_path = Path(log_path)
    rows = read_log(log_path)
    errors: list[tuple[Path, str]] = []
    failed_rows: list[dict] = []
    done = 0
    for row in reversed(rows):
        src, dst = Path(row["原始路徑"]), Path(row["新路徑"])
        try:
            if not dst.exists():
                raise FileNotFoundError(f"找不到檔案（可能已被移動或刪除）：{dst}")
            if row["動作"] == "move" or not src.exists():
                if src.exists():
                    raise FileExistsError(f"原位置已有同名檔案：{src}")
                _move_back(dst, src)
            elif _unchanged(dst, row):
                _delete(dst)
            else:
                raise OSError(f"整理後這個檔案被修改過，為了安全沒有刪除：{dst}")
            done += 1
            _remove_if_empty(dst.parent)
        except OSError as exc:
            errors.append((dst, str(exc)))
            failed_rows.append(row)

    try:
        if errors:
            failed_rows.reverse()
            with open(log_path, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.DictWriter(f, fieldnames=LOG_FIELDS, extrasaction="ignore")
                writer.writeheader()
                writer.writerows(failed_rows)
        else:
            os.replace(log_path, log_path.with_name(log_path.name.replace(LOG_PREFIX, UNDONE_PREFIX, 1)))
    except OSError as exc:
        errors.append((log_path, f"無法更新紀錄檔（是否正被其他程式開啟？）：{exc}"))
    return ExecuteResult(done, errors, log_path)


def _remove_if_empty(folder: Path) -> None:
    try:
        folder.rmdir()  # 只有空資料夾才會成功
    except OSError:
        pass
