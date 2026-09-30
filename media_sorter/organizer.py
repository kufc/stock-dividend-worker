"""依分類整理檔案：一律「複製」到分類資料夾，原檔完全不動；確認沒問題後再另外「移除原檔」。

設計原則（刻意保持簡單，讓任何環節出錯都不會遺失資料）：
- 整理只會複製：原檔不動，所以整理本身不論在哪裡中斷都不會遺失任何檔案
- 絕不覆蓋既有檔案；複製先寫到不會撞名的暫存檔，完整寫完才改成正式檔名
- 「復原」只做一件事：把這次建立的複本移到資源回收筒，而且只有在原檔仍在、
  且兩者內容逐位元組相同時才會移除；原檔不在或內容不同就保留複本
- 「移除原檔」只移除跟複本內容逐位元組相同的原檔，而且只送到資源回收筒（可從 Windows 救回）
- 每一次移除都是「比對 → 先在同一個資料夾內改名 → 用『禁止其他程式寫入』的方式開啟兩份檔案 →
  握著它們再比對一次、送資源回收筒 → 最後才放開」：從比對到送出為止，沒有程式能修改任何一份；
  任何一份已被其他程式開著寫入時，開檔會失敗而保留檔案
  （限制：這個強制保護只有 Windows 有；其他系統只能做到改名與第二次比對）
- 紀錄記著這次整理的來源資料夾與輸出資料夾，每一列都必須落在這兩個資料夾內。
  注意：這些資料夾本身也寫在紀錄裡，能改紀錄的人可以一起改，所以這只是一致性檢查，不是安全邊界；
  真正的保護是「刪除前一定有另一份內容相同的檔案」與確認視窗列出的完整清單
- 因為每一次刪除都要求「另一份內容完全相同的檔案仍然存在」，紀錄檔就算寫到一半、
  被竄改或被重新放回，最壞情況也只是某個檔案沒被處理，不會造成資料遺失
"""

from __future__ import annotations

import codecs
import csv
import errno
import hashlib
import io
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from .config import IMAGE_EXTS, VIDEO_EXTS

try:  # 資源回收筒；沒有安裝時，復原改把複本搬到隔離資料夾，移除原檔則直接拒絕
    from send2trash import send2trash as _send2trash
except ImportError:  # pragma: no cover - 選用功能
    _send2trash = None

INVALID_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
LOG_FIELDS = ["動作", "原始路徑", "新路徑", "分類", "時間", "大小", "來源資料夾", "輸出資料夾"]
LOG_KIND_PREFIXES = {  # 檔名前綴 → 紀錄的種類
    "整理紀錄_": "active", "已復原_": "undone", "已完成_": "finished", "無法讀取_": "unreadable",
}
UNREADABLE_PREFIX = "無法讀取_"  # 壞掉的紀錄移到旁邊時用的前綴（不符合 LOG_NAME_RE，不會再被選中）
STAGE_MARK_ORIGINAL = "整理前"  # 移除原檔前的暫時檔名標記：IMG_1234 (整理前).jpg（進了資源回收筒也看得懂）
STAGE_MARK_COPY = "複本"
csv.field_size_limit(16 * 1024 * 1024)  # 預設 128 KB；路徑很長的紀錄不該被當成格式錯誤
LOG_PREFIX = "整理紀錄_"
LOG_NAME_RE = re.compile(r"整理紀錄_\d{8}_\d{6}(?:_\d{1,3})?\.csv")
UNDONE_PREFIX = "已復原_"
FINISHED_PREFIX = "已完成_"  # 原檔已全部移除的整理紀錄
QUARANTINE_DIR = "復原移除"  # 沒有資源回收筒可用時，復原移除的複本改放到這裡
SEQ = "{序號}"
MAX_NAME = 100  # 檔名（不含副檔名）上限，避免超過 Windows 路徑長度限制
PARTIAL_SUFFIX = ".partial"
SPACE_MARGIN = 200 * 1024 * 1024  # 複製前預留的磁碟空間


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
    """先複製到絕對不會跟既有檔案撞名的暫存檔，完整寫完才改成正式檔名；失敗只清掉自己建立的暫存檔。

    用 mkstemp「獨佔建立」取得保證不存在的檔名，避免目的地資料夾裡剛好已經有
    `<檔名>.partial`（例如使用者自己放的）時被覆蓋、失敗時又被誤刪。
    """
    fd, tmp_name = tempfile.mkstemp(dir=dst.parent, prefix=f"{dst.name}.", suffix=PARTIAL_SUFFIX)
    tmp = Path(tmp_name)
    try:
        os.close(fd)
        shutil.copyfile(src, tmp)
        shutil.copystat(src, tmp)
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
    stopped: bool = False  # 使用者要求停止（已完成的部分會保留在紀錄中）


# ---------------------------------------------------------------------------- 整理（只複製）
def free_space_problem(ops: list[PlannedOp]) -> str | None:
    """複製需要的空間超過目的地剩餘空間時，回傳給使用者看的說明；空間足夠回傳 None。"""
    if not ops:
        return None
    need = 0
    for op in ops:
        try:
            need += op.src.stat().st_size
        except OSError:
            pass
    target = ops[0].dst
    while not target.exists() and target.parent != target:
        target = target.parent
    try:
        free = shutil.disk_usage(target).free
    except OSError:
        return None
    if need + SPACE_MARGIN > free:
        return (f"輸出位置的磁碟空間不足：需要約 {need / 1024**3:.1f} GB，"
                f"剩餘 {free / 1024**3:.1f} GB。請清出空間或換一個輸出資料夾。")
    return None


def describe_error(exc: BaseException) -> str:
    """把技術性的例外轉成「發生什麼＋能怎麼做」的說明，技術細節放在括號裡供回報使用。"""
    code = getattr(exc, "errno", None)
    winerror = getattr(exc, "winerror", None)
    if code == errno.ENOSPC or winerror in (39, 112):
        hint = "輸出磁碟空間不足。請清出空間或改選其他位置，再重試失敗的檔案。"
    elif isinstance(exc, FileExistsError) or code == errno.EEXIST:
        hint = "目的地已經有同名檔案，為了安全沒有覆蓋。請改用不同的命名方式，或先處理那個檔案。"
    elif isinstance(exc, FileNotFoundError) or code == errno.ENOENT:
        hint = "找不到這個檔案（可能已被移動、改名或刪除）。"
    elif isinstance(exc, PermissionError) or code in (errno.EACCES, errno.EPERM) or winerror in (5, 32, 33):
        hint = "沒有權限，或檔案正被其他程式使用。請關閉正在使用它的程式後再重試。"
    else:
        hint = "無法處理這個檔案。"
    return f"{hint}（詳細：{exc}）"


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


def _canonical(path: Path) -> str:
    """資料夾比對用的標準形式（解析連結、Windows 不分大小寫）。"""
    return os.path.normcase(str(Path(path).resolve()))


def _is_within(path: Path, root: str) -> bool:
    canonical = _canonical(path)
    return canonical == root or canonical.startswith(root.rstrip("\\/") + os.sep)


def execute(
    ops: list[PlannedOp],
    log_dir: Path,
    *,
    source_dir: Path | None = None,
    output_dir: Path | None = None,
    on_progress: Callable[[int, int], None] | None = None,
    stop_event=None,
    append_to: Path | None = None,
) -> ExecuteResult:
    """把每個檔案複製到分類資料夾，原檔完全不動。每完成一個檔案就寫一列紀錄（供之後復原或移除原檔）。

    source_dir／output_dir 是這次整理的來源與輸出資料夾，會寫進每一列紀錄；
    之後復原或移除原檔時，只接受落在這兩個資料夾內的檔案。
    stop_event 被設定時，會在「完成目前這個檔案」之後停止，已完成的部分照常保留在紀錄中。
    append_to 是重試用：把結果接在既有的紀錄檔後面，讓同一次整理只有一份紀錄。
    """
    if ops:
        if source_dir is None:
            source_dir = Path(os.path.commonpath([str(op.src.parent) for op in ops]))
        if output_dir is None:
            output_dir = ops[0].dst.parent.parent
    source_root = _canonical(source_dir) if source_dir is not None else ""
    output_root = _canonical(output_dir) if output_dir is not None else ""
    if append_to is not None:
        log_path = Path(append_to)
        f = open(log_path, "a", newline="", encoding="utf-8-sig")  # 追加時 Python 不會再寫一次 BOM
    else:
        log_path, f = _open_new_log(Path(log_dir))
    errors: list[tuple[Path, str]] = []
    done = 0
    aborted = False
    stopped = False
    done_sources: list[Path] = []
    with f:
        writer = csv.writer(f)
        if append_to is None:
            writer.writerow(LOG_FIELDS)
            f.flush()
        for i, op in enumerate(ops, 1):
            if stop_event is not None and stop_event.is_set():
                stopped = True
                break
            try:
                if op.dst.exists():
                    raise FileExistsError(f"目的地已有同名檔案：{op.dst}")
                op.dst.parent.mkdir(parents=True, exist_ok=True)
                _copy_no_clobber(op.src, op.dst)
            except OSError as exc:
                errors.append((op.src, describe_error(exc)))
            else:
                try:
                    writer.writerow(["copy", str(op.src), str(op.dst), op.category,
                                     datetime.now().isoformat(timespec="seconds"), op.dst.stat().st_size,
                                     source_root, output_root])
                    f.flush()
                    os.fsync(f.fileno())
                except OSError as exc:
                    # 原檔沒動，只是多了一份沒被記錄的複本（不會遺失資料）；為免之後的複本都無法復原，停止整理
                    errors.append((op.src, f"已複製到 {op.dst}，但紀錄檔寫入失敗，已停止整理"
                                           f"（這一份複本需要手動刪除，原檔不受影響）：{exc}"))
                    aborted = True
                    break
                done += 1
                done_sources.append(op.src)
            if on_progress:
                on_progress(i, len(ops))
    if done == 0 and append_to is None:
        log_path.unlink(missing_ok=True)
        log_path = None
    return ExecuteResult(done, errors, log_path, [], aborted, done_sources, stopped)


# ---------------------------------------------------------------------------- 讀取紀錄
def latest_log(log_dir: Path) -> Path | None:
    """最新一份還沒處理完的整理紀錄。只接受程式自己產生的檔名格式（整理紀錄_日期_時間[_序號].csv）。"""
    logs = [p for p in Path(log_dir).glob(f"{LOG_PREFIX}*.csv") if LOG_NAME_RE.fullmatch(p.name) and p.is_file()]
    return max(logs, key=lambda p: (p.stat().st_mtime_ns, p.name)) if logs else None


def _decode_bytes(raw: bytes) -> str:
    """把紀錄檔的位元組轉成文字。寫入中斷可能切在一個中文字中間：只丟掉檔尾那個不完整的字。"""
    body = raw[len(codecs.BOM_UTF8):] if raw.startswith(codecs.BOM_UTF8) else raw
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError as exc:
        if exc.reason == "unexpected end of data" and exc.end == len(body):
            try:
                return body[:exc.start].decode("utf-8")
            except UnicodeDecodeError:
                pass
    for encoding in ("cp950", "mbcs"):  # 被 Excel 另存成 Big5 等編碼
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    raise ValueError("紀錄檔的編碼無法辨識")


class LogChangedError(ValueError):
    """紀錄檔在使用者確認之後被改變：執行的清單必須是使用者看過的那一份，所以拒絕執行。"""


@dataclass
class LogSnapshot:
    """使用者確認畫面所根據的那一份紀錄內容。執行時會比對 digest，確認過的跟執行的一定是同一份。"""

    path: Path
    digest: str
    rows: list[dict]
    skipped: int


def snapshot_log(log_path: Path) -> LogSnapshot:
    raw = Path(log_path).read_bytes()
    rows, skipped = _parse_log_bytes(raw)
    return LogSnapshot(Path(log_path), hashlib.sha256(raw).hexdigest(), rows, skipped)


def _load_log(log_path: Path, expected_digest: str | None) -> tuple[list[dict], int]:
    raw = Path(log_path).read_bytes()
    if expected_digest is not None and hashlib.sha256(raw).hexdigest() != expected_digest:
        raise LogChangedError("整理紀錄在你確認之後被改變了，為了安全沒有執行任何動作。請重新檢視後再操作。")
    return _parse_log_bytes(raw)


def read_log_rows(log_path: Path) -> tuple[list[dict], int]:
    """讀取紀錄，回傳 (格式完整的列, 無法讀取而略過的列數)。任何殘缺的列都只會被略過，不會丟例外。"""
    return _parse_log_bytes(Path(log_path).read_bytes())


def _parse_log_bytes(raw: bytes) -> tuple[list[dict], int]:
    text = _decode_bytes(raw)
    # newline="" 讓 csv 模組自己處理換行：被引號包住的欄位（例如含換行的分類名稱）才不會被切斷
    reader = csv.DictReader(io.StringIO(text, newline=""))
    try:
        if reader.fieldnames is None:
            return [], 0
        if not {"動作", "原始路徑", "新路徑", "來源資料夾", "輸出資料夾"} <= set(reader.fieldnames):
            raise ValueError("紀錄檔格式不正確（缺少必要欄位，可能是舊版本的紀錄）")
        rows, skipped = [], 0
        for row in reader:
            if all(isinstance(row.get(k), str) and row[k].strip()
                   for k in ("動作", "原始路徑", "新路徑", "來源資料夾", "輸出資料夾")):
                rows.append(row)
            elif any(isinstance(v, str) and v.strip() for k, v in row.items() if k is not None):
                skipped += 1
    except csv.Error as exc:
        raise ValueError(f"紀錄檔格式不正確：{exc}") from exc
    roots = {(row["來源資料夾"], row["輸出資料夾"]) for row in rows}
    if len(roots) > 1:
        raise ValueError("紀錄中的來源／輸出資料夾不一致（紀錄可能被修改過）")
    return rows, skipped


def log_roots(rows: list[dict]) -> tuple[str, str]:
    """這份紀錄的 (來源資料夾, 輸出資料夾)。"""
    if not rows:
        return "", ""
    return rows[0]["來源資料夾"], rows[0]["輸出資料夾"]


def set_aside_log(log_path: Path) -> Path:
    """把無法讀取的紀錄改名移到旁邊（保留內容供查看），之後「最新紀錄」就不會再選到它。"""
    log_path = Path(log_path)
    target = log_path.with_name(UNREADABLE_PREFIX + log_path.name)
    n = 2
    while target.exists():
        target = log_path.with_name(f"{UNREADABLE_PREFIX}{n}_{log_path.name}")
        n += 1
    os.replace(log_path, target)
    return target


def read_log(log_path: Path) -> list[dict]:
    return read_log_rows(log_path)[0]


def _validate_row(row: dict) -> str | None:
    """基本格式檢查。真正的安全保證來自「刪除前一定有另一份內容相同的檔案」，這裡只擋明顯不對的列。"""
    if row["動作"] != "copy":
        return f"不明的動作「{row['動作']}」，已略過"
    src, dst = Path(row["原始路徑"]), Path(row["新路徑"])
    if not src.is_absolute() or not dst.is_absolute():
        return "紀錄中的路徑不是絕對路徑，已略過"
    if os.path.normcase(str(src)) == os.path.normcase(str(dst)):
        return "原始路徑與新路徑相同，已略過"
    if src.suffix.lower() != dst.suffix.lower() or dst.suffix.lower() not in (IMAGE_EXTS | VIDEO_EXTS):
        return "副檔名不正確，已略過"
    try:
        if not _is_within(src, row["來源資料夾"]):
            return "原始路徑不在這次整理的來源資料夾內，已略過"
        if _canonical(dst.parent.parent) != os.path.normcase(row["輸出資料夾"]):
            return "新路徑不在這次整理的輸出資料夾內，已略過"
    except (OSError, ValueError):
        return "紀錄中的路徑無法解析，已略過"
    return None


def _open_deny_write(path: Path):
    """開檔讀取，同時禁止任何程式寫入這個檔案（Windows）。

    share mode 不含 FILE_SHARE_WRITE：只要已經有程式握著可寫入的控制代碼，開檔就會失敗
    （共用違規 WinError 32），我們就知道檔案正在被寫入；開成功之後、關閉之前，也沒有程式能再開寫入。
    其他系統沒有這種強制鎖，只是一般開檔。
    """
    if os.name != "nt":
        return open(path, "rb")
    import ctypes
    import msvcrt
    from ctypes import wintypes

    generic_read, file_share_read, file_share_delete, open_existing = 0x80000000, 0x1, 0x4, 3
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create_file.restype = wintypes.HANDLE
    handle = create_file(str(path), generic_read, file_share_read | file_share_delete, None, open_existing, 0x80, None)
    if handle is None or handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY)
    except OSError:
        kernel32.CloseHandle(wintypes.HANDLE(handle))
        raise
    return os.fdopen(fd, "rb")


def _same_content(a: Path, b: Path, chunk: int = 1 << 20, reader_a=None, reader_b=None) -> bool:
    """逐位元組比對兩個不同的檔案。

    - 不用 filecmp.cmp：它會依「大小＋修改時間」快取結果，內容改了但時間沒變時會沿用舊答案
    - 兩個路徑其實是同一個檔案（大小寫不同、硬連結、捷徑）時回傳 False：
      那不是「另一份」，刪掉其中一個就等於刪掉唯一的一份
    """
    if os.path.samefile(a, b):
        return False
    if a.stat().st_size != b.stat().st_size:
        return False
    # 呼叫端傳入的控制代碼（禁止寫入的保護）由呼叫端負責關閉：這裡絕不能提早關掉
    fa = reader_a if reader_a is not None else open(a, "rb")
    fb = reader_b if reader_b is not None else open(b, "rb")
    try:
        fa.seek(0)
        fb.seek(0)
        while True:
            block_a, block_b = fa.read(chunk), fb.read(chunk)
            if block_a != block_b:
                return False
            if not block_a:
                return True
    finally:
        if reader_a is None:
            fa.close()
        if reader_b is None:
            fb.close()


def _write_rows_atomic(path: Path, rows: list[dict]) -> None:
    """先寫暫存檔再原子性換檔：寫到一半失敗也不會動到原本的檔案。"""
    tmp_fd, tmp_name = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(tmp_fd, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=LOG_FIELDS, extrasaction="ignore", restval="")
            writer.writeheader()
            writer.writerows(rows)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise


def _finish_log(log_path: Path, remaining: list[dict], done_prefix: str, errors: list) -> None:
    """還有要重試的列就只留下那些列；全部處理完就把紀錄改名（之後不會再被選中）。"""
    try:
        if remaining:
            _write_rows_atomic(log_path, remaining)
        else:
            os.replace(log_path, log_path.with_name(log_path.name.replace(LOG_PREFIX, done_prefix, 1)))
    except OSError as exc:
        errors.append((log_path, f"無法更新紀錄檔（是否正被其他程式開啟？）：{exc}"))


# ---------------------------------------------------------------------------- 復原（移除複本）
def _to_quarantine(path: Path, quarantine_dir: Path) -> Path:
    quarantine_dir.mkdir(parents=True, exist_ok=True)
    dest, n = quarantine_dir / path.name, 2
    while True:
        try:
            _rename_no_clobber(path, dest)
            return dest
        except FileExistsError:
            dest = quarantine_dir / f"{path.stem} ({n}){path.suffix}"
            n += 1


def _stage_name(path: Path, mark: str) -> Path:
    staged, n = path.with_name(f"{path.stem} ({mark}){path.suffix}"), 2
    while staged.exists():
        staged = path.with_name(f"{path.stem} ({mark} {n}){path.suffix}")
        n += 1
    return staged


def _remove_verified(path: Path, reference: Path, mark: str, sink: Callable[[Path], Path | None],
                     precheck: bool = True) -> Path | None:
    """把 path 移除（交給 sink：資源回收筒或隔離資料夾），但只有在移除的那一刻它跟 reference 內容相同時。

    「比對完再移除」中間有空檔，其他程式可能剛好改寫其中一份，所以：
    1. 先把 path 在同一個資料夾內改名：之後照原路徑存檔的程式只會產生新檔，不會動到我們這一份
    2. 用「禁止其他程式寫入」的方式同時開啟兩份檔案（見 _open_deny_write）：
       任何一份已被其他程式開著寫入，這一步就會失敗（保留、改回原名）
    3. 握著這兩個控制代碼比對內容，而且一直握到 sink 完成才放開：
       從比對到送出為止，沒有程式能修改要移除的那一份或留下的那一份
    （強制的「禁止寫入」只有 Windows 有；其他系統只做得到改名與再次比對）
    任何一步失敗都把檔案改回原名、不移除。回傳 sink 的結果（隔離位置或 None）。
    """
    if precheck and not _same_content(path, reference):
        raise OSError(f"內容跟另一份不同，為了安全保留：{path}")
    staged = _stage_name(path, mark)
    try:
        _rename_no_clobber(path, staged)
    except OSError as exc:
        raise OSError(f"無法移除（檔案可能正被其他程式使用）：{path}（{exc}）") from exc
    try:
        try:
            guard = _open_deny_write(staged)
        except OSError as exc:
            raise OSError(f"檔案正被其他程式開啟寫入，為了安全保留：{path}（{exc}）") from exc
        with guard:
            try:
                reference_guard = _open_deny_write(reference)
            except OSError as exc:
                raise OSError(f"另一份（{reference}）正被其他程式開啟寫入，為了安全保留：{path}（{exc}）") from exc
            with reference_guard:
                if not _same_content(staged, reference, reader_a=guard, reader_b=reference_guard):
                    raise OSError(f"移除前內容被其他程式修改，已保留：{path}")
                return sink(staged)  # 兩個控制代碼都還握著：送出的內容 = 留下的內容
    except BaseException as exc:
        try:
            _rename_no_clobber(staged, path)
        except OSError:
            raise OSError(f"{exc}（檔案目前在：{staged}，請自行改回原檔名）") from exc
        raise

def undo(log_path: Path, expected_digest: str | None = None) -> ExecuteResult:
    """復原一次整理：把這次建立的複本移到資源回收筒，原檔不受影響。

    只有「原檔仍在、且與複本內容逐位元組相同」時才移除複本；
    原檔已經不在（例如已經移除原檔）或複本後來被修改過，一律保留複本並說明原因。
    只有暫時性的失敗（例如檔案被其他程式開著）會留在紀錄中，下次按復原會重試。
    """
    log_path = Path(log_path)
    rows, skipped = _load_log(log_path, expected_digest)
    quarantine_dir = log_path.parent / QUARANTINE_DIR / datetime.now().strftime("%Y%m%d_%H%M%S")
    errors: list[tuple[Path, str]] = []
    warnings: list[str] = []
    retry: list[dict] = []
    done = 0
    if skipped:
        warnings.append(f"紀錄中有 {skipped} 列不完整（可能寫到一半），已略過；原檔都不受影響")
    for row in rows:
        dst = Path(row["新路徑"])
        try:
            reason = _validate_row(row)
            if reason:
                warnings.append(f"{dst.name}：{reason}")
                continue
            src = Path(row["原始路徑"])
            if not dst.exists():
                continue  # 複本已經不在，沒有需要做的事
            if not src.exists():
                warnings.append(f"{dst.name}：原檔已不在（可能已移除原檔），保留這份複本")
                continue
            if not _same_content(src, dst):
                warnings.append(f"{dst.name}：複本跟原檔內容不同（整理後被修改過），為了安全保留複本")
                continue
            if _send2trash is not None:
                def sink(staged: Path) -> Path | None:
                    _send2trash(str(staged))
                    return None
            else:
                def sink(staged: Path) -> Path | None:
                    return _to_quarantine(staged, quarantine_dir)
            location = _remove_verified(dst, src, STAGE_MARK_COPY, sink, precheck=False)
            if location is not None:
                warnings.append(f"{dst.name}：沒有資源回收筒可用，已搬到隔離資料夾：{location}")
            done += 1
            _remove_if_empty(dst.parent)
        except Exception as exc:  # noqa: BLE001 - 任何錯誤都只影響這一個檔案
            errors.append((dst, str(exc) if isinstance(exc, OSError) else f"未預期的錯誤：{exc}"))
            retry.append(row)
    _finish_log(log_path, retry, UNDONE_PREFIX, errors)
    return ExecuteResult(done, errors, log_path, warnings)


# ---------------------------------------------------------------------------- 移除原檔
def remove_originals(log_path: Path, expected_digest: str | None = None) -> ExecuteResult:
    """確認複本沒問題後，把原檔移到資源回收筒（等於完成「搬移」）。

    只有「複本存在、且與原檔內容逐位元組相同」的原檔才會移除，而且一定是送到資源回收筒
    （可以從 Windows 的資源回收筒救回）；沒有資源回收筒功能時完全不動作。
    """
    if _send2trash is None:
        raise OSError("需要資源回收筒功能（send2trash 套件）才能移除原檔，請重新執行 install.bat")
    log_path = Path(log_path)
    rows, skipped = _load_log(log_path, expected_digest)
    errors: list[tuple[Path, str]] = []
    warnings: list[str] = []
    retry: list[dict] = []
    done = 0
    if skipped:
        warnings.append(f"紀錄中有 {skipped} 列不完整（可能寫到一半），已略過；那些原檔不會被移除")
    for row in rows:
        src = Path(row["原始路徑"])
        try:
            reason = _validate_row(row)
            if reason:
                warnings.append(f"{src.name}：{reason}")
                continue
            dst = Path(row["新路徑"])
            if not src.exists():
                continue  # 原檔已經不在，沒有需要做的事
            if not dst.exists():
                errors.append((src, f"找不到複本，為了安全不移除原檔：{dst}"))
                retry.append(row)
                continue
            if not _same_content(src, dst):
                errors.append((src, f"複本跟原檔內容不同，為了安全不移除原檔：{dst}"))
                retry.append(row)
                continue
            _remove_verified(src, dst, STAGE_MARK_ORIGINAL, lambda staged: _send2trash(str(staged)), precheck=False)
            done += 1
        except Exception as exc:  # noqa: BLE001 - 任何錯誤都只影響這一個檔案
            errors.append((src, str(exc) if isinstance(exc, OSError) else f"未預期的錯誤：{exc}"))
            retry.append(row)
    _finish_log(log_path, retry, FINISHED_PREFIX, errors)
    return ExecuteResult(done, errors, log_path, warnings)


def classify_rows(rows: list[dict], mode: str) -> list[tuple[dict, str | None]]:
    """確認視窗用的預檢：每一列「會不會被處理」。回傳 (列, 略過的原因)；原因是 None 代表會處理。

    mode：'undo'（移除複本）或 'remove'（移除原檔）。只檢查檔案是否存在與格式，內容比對留到真正執行時。
    """
    result: list[tuple[dict, str | None]] = []
    for row in rows:
        reason = _validate_row(row)
        if reason:
            result.append((row, f"略過：{reason.removesuffix('，已略過')}"))
            continue
        try:
            src_exists, dst_exists = Path(row["原始路徑"]).exists(), Path(row["新路徑"]).exists()
        except OSError:
            result.append((row, "略過：無法檢查這個檔案"))
            continue
        if mode == "undo":
            note = ("略過：複本已不在" if not dst_exists
                    else "保留：原檔已不在，複本是唯一的一份" if not src_exists else None)
        else:
            note = ("略過：原檔已不在" if not src_exists
                    else "保留原檔：找不到複本" if not dst_exists else None)
        result.append((row, note))
    return result


@dataclass
class LogInfo:
    """整理紀錄頁的一列。"""

    path: Path
    kind: str  # active（可處理）／undone（複本已移除）／finished（原檔已移除）／unreadable（無法讀取）
    when: datetime
    count: int
    source: str
    output: str
    problem: str | None = None


def list_logs(log_dir: Path) -> list[LogInfo]:
    """列出紀錄資料夾裡所有整理紀錄（新的在前）。讀不出來的紀錄也會列出，並註明原因。"""
    infos: list[LogInfo] = []
    for path in Path(log_dir).glob("*.csv"):
        kind = next((k for prefix, k in LOG_KIND_PREFIXES.items() if path.name.startswith(prefix)), None)
        if kind is None or not path.is_file():
            continue
        if kind != "unreadable" and not re.fullmatch(r"(整理紀錄|已復原|已完成)_\d{8}_\d{6}(?:_\d{1,3})?\.csv", path.name):
            continue
        try:
            when = datetime.fromtimestamp(path.stat().st_mtime)
        except OSError:
            continue
        count, source, output, problem = 0, "", "", None
        try:
            rows, skipped = read_log_rows(path)
            count = len(rows)
            source, output = log_roots(rows)
            if skipped:
                problem = f"有 {skipped} 列不完整，已略過"
            if not rows and kind != "unreadable":
                problem = problem or "紀錄是空的"
        except (OSError, ValueError) as exc:
            if kind == "active":
                kind = "unreadable"
            problem = str(exc)
        infos.append(LogInfo(path, kind, when, count, source, output, problem))
    return sorted(infos, key=lambda i: (i.when, i.path.name), reverse=True)


def _remove_if_empty(folder: Path) -> None:
    try:
        folder.rmdir()  # 只有空資料夾才會成功
    except OSError:
        pass
