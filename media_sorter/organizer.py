"""依分類搬移／複製檔案並重新命名；每次操作都寫一份紀錄檔（CSV），可一鍵復原。

安全原則：
- 絕不覆蓋既有檔案（目的地已存在就換名字或回報錯誤）
- 複製先寫到「不會跟任何檔案撞名」的暫存檔，完成後才改成正式檔名；失敗只刪掉自己建立的暫存檔
- 動作前先寫一列「pending」紀錄並確保寫入磁碟，完成後再寫一列確認紀錄；
  任何一次寫入失敗就立刻停止，這樣就算半途中斷，也知道哪些檔案可能已經動過、要復原到哪裡
- 復原複製時，原檔不見了就把複本搬回原位（而不是刪掉唯一的一份）；
  複本只有在跟目前的原檔內容完全相同時才刪除，否則保留並回報「原檔已變更」
- 紀錄檔重寫（例如復原後只留下失敗的列）一律先寫暫存檔再原子性地換檔，寫到一半失敗也不會動到原紀錄
- 紀錄的每一列都有 HMAC 簽章（金鑰放在程式資料夾以外），被竄改或偽造的列一律拒絕；
  另外也只接受合法格式、位於輸出資料夾內、副檔名相符的列，且刪除前一定會逐位元組核對內容
- 紀錄寫到一半（例如磁碟滿了只寫出半列）時，殘缺的列會被忽略，不影響其他列的復原
"""

from __future__ import annotations

import csv
import errno
import hmac
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from .config import IMAGE_EXTS, VIDEO_EXTS, log_signing_key

try:  # 選用套件：沒有安裝就搬到隔離資料夾，而不是直接刪除
    from send2trash import send2trash as _send2trash
except ImportError:  # pragma: no cover - 選用功能
    _send2trash = None

INVALID_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
LOG_FIELDS = ["動作", "原始路徑", "新路徑", "分類", "時間", "大小", "修改時間", "輸出資料夾", "簽章"]
SIGNED_FIELDS = LOG_FIELDS[:-1]
MOVE_ACTIONS = {"move", "pending-move"}
COPY_ACTIONS = {"copy", "pending-copy"}
VALID_ACTIONS = MOVE_ACTIONS | COPY_ACTIONS
LOG_PREFIX = "整理紀錄_"
UNDONE_PREFIX = "已復原_"
QUARANTINE_DIR = "復原移除"  # 沒有資源回收筒可用時，安全刪除的檔案改放到這裡
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


def _signature(values: list[str]) -> str:
    """一列紀錄的 HMAC-SHA256 簽章（涵蓋除了簽章本身以外的所有欄位）。"""
    message = "\x1f".join(values).encode("utf-8")
    return hmac.new(log_signing_key(), message, "sha256").hexdigest()


def _signed_row(values: list) -> list[str]:
    text = ["" if v is None else str(v) for v in values]
    return text + [_signature(text)]


def _row_signature_ok(row: dict) -> bool:
    expected = _signature(["" if row.get(k) is None else str(row.get(k)) for k in SIGNED_FIELDS])
    return hmac.compare_digest(str(row.get("簽章") or ""), expected)


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
    """執行搬移（move）或複製（copy）。

    採用「先記錄、再動作」：每個檔案實際搬移／複製前，先寫一列 pending 紀錄並確保寫入磁碟；
    成功後再寫一列確認紀錄。任一次寫入失敗都立刻停止整理（不會繼續動其他檔案），
    這樣無論在哪個環節中斷，都能從紀錄檔判斷哪些檔案已經動過、該怎麼復原。
    """
    if action not in ("move", "copy"):
        raise ValueError(f"未知的動作：{action}")
    log_path, f = _open_new_log(Path(log_dir))
    errors: list[tuple[Path, str]] = []
    warnings: list[str] = []
    done = 0
    any_written = False  # 有沒有任何一列 pending 紀錄成功寫入過（決定結束時要不要保留空紀錄檔）
    aborted = False
    done_sources: list[Path] = []
    with f:
        writer = csv.writer(f)
        writer.writerow(LOG_FIELDS)
        f.flush()
        for i, op in enumerate(ops, 1):
            output_dir = op.dst.parent.parent
            try:
                if op.dst.exists():
                    raise FileExistsError(f"目的地已有同名檔案：{op.dst}")
                op.dst.parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                errors.append((op.src, str(exc)))
                if on_progress:
                    on_progress(i, len(ops))
                continue

            try:
                writer.writerow(_signed_row([f"pending-{action}", op.src, op.dst, op.category,
                                             datetime.now().isoformat(timespec="seconds"), "", "", output_dir]))
                f.flush()
                os.fsync(f.fileno())
            except OSError as exc:
                errors.append((op.src, f"紀錄檔寫入失敗，已停止整理（這個檔案沒有被更動）：{exc}"))
                aborted = True
                break
            any_written = True

            logged_action = action
            op_error: OSError | None = None
            try:
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
                op_error = exc

            if op_error is not None:
                errors.append((op.src, str(op_error)))
                # 這一列 pending 紀錄留著不管：復原時發現新路徑不存在，會判斷成「沒有真的發生」而略過
                if on_progress:
                    on_progress(i, len(ops))
                continue

            try:
                st = op.dst.stat()
                writer.writerow(_signed_row([logged_action, op.src, op.dst, op.category,
                                             datetime.now().isoformat(timespec="seconds"), st.st_size,
                                             st.st_mtime_ns, output_dir]))
                f.flush()
                os.fsync(f.fileno())
            except OSError as exc:
                done += 1
                done_sources.append(op.src)
                aborted = True
                errors.append((op.src, f"檔案已{'搬移' if logged_action == 'move' else '複製'}成功，"
                                        f"但確認紀錄寫入失敗（已預先記錄，可以復原）：{exc}"))
                break

            done += 1
            done_sources.append(op.src)
            if on_progress:
                on_progress(i, len(ops))
    if done == 0 and not any_written:
        log_path.unlink(missing_ok=True)
        log_path = None
    return ExecuteResult(done, errors, log_path, warnings, aborted, done_sources)


# ---------------------------------------------------------------------------- 復原
def latest_log(log_dir: Path) -> Path | None:
    logs = [p for p in Path(log_dir).glob(f"{LOG_PREFIX}*.csv")]
    return max(logs, key=lambda p: (p.stat().st_mtime_ns, p.name)) if logs else None


def read_log(log_path: Path) -> list[dict]:
    """讀取紀錄檔。被 Excel 另存成 Big5（cp950）也讀得懂。

    同一個檔案可能有兩列（先寫的 pending 列、動作完成後補寫的確認列），
    這裡會依「新路徑」把它們合併成一列：有確認列就用確認列，否則保留 pending 列。
    """
    raw = Path(log_path).read_bytes()
    for encoding in ("utf-8-sig", "cp950", "mbcs"):
        try:
            text = raw.decode(encoding)
            break
        except (UnicodeDecodeError, LookupError):
            continue
    else:
        raise ValueError("紀錄檔的編碼無法辨識")
    raw_rows = list(csv.DictReader(text.splitlines()))
    required = {"動作", "原始路徑", "新路徑"}
    if raw_rows and not required <= set(raw_rows[0]):
        raise ValueError("紀錄檔格式不正確（缺少必要欄位）")
    merged: dict[str, dict] = {}
    order: list[str] = []
    for row in raw_rows:
        # 殘缺的列（例如寫到一半磁碟就滿了，只寫出「move,」）直接忽略：
        # 它的 pending 列在它之前已經完整寫入並同步到磁碟，復原會依那一列處理
        if not all(isinstance(row.get(k), str) and row.get(k).strip() for k in required):
            continue
        key = row["新路徑"]
        action = row.get("動作", "")
        existing = merged.get(key)
        if existing is None:
            order.append(key)
            merged[key] = row
        elif existing.get("動作", "").startswith("pending-") and not action.startswith("pending-"):
            merged[key] = row  # 確認列覆蓋先前的 pending 列
    return [merged[k] for k in order]


def _move_back(dst: Path, src: Path) -> None:
    src.parent.mkdir(parents=True, exist_ok=True)
    try:
        _rename_no_clobber(dst, src)
    except OSError as exc:
        if not _is_cross_device(exc):
            raise
        _copy_no_clobber(dst, src)
        os.unlink(dst)


def _delete(path: Path, quarantine_dir: Path) -> Path | None:
    """優先丟到資源回收筒（可救回）；沒有 send2trash 套件時，搬到隔離資料夾而不是直接刪除。

    回傳搬去的隔離位置；丟進資源回收筒成功則回傳 None。
    """
    if _send2trash is not None:
        _send2trash(str(path))
        return None
    quarantine_dir.mkdir(parents=True, exist_ok=True)
    dest, n = quarantine_dir / path.name, 2
    while True:
        try:
            _rename_no_clobber(path, dest)
            return dest
        except FileExistsError:
            dest = quarantine_dir / f"{path.stem} ({n}){path.suffix}"
            n += 1


def _same_content(a: Path, b: Path, chunk: int = 1 << 20) -> bool:
    """逐位元組比對兩個檔案。

    不用 filecmp.cmp：它會依「大小＋修改時間」快取比對結果，內容改了但時間沒變時會沿用舊答案。
    """
    if a.stat().st_size != b.stat().st_size:
        return False
    with open(a, "rb") as fa, open(b, "rb") as fb:
        while True:
            block_a, block_b = fa.read(chunk), fb.read(chunk)
            if block_a != block_b:
                return False
            if not block_a:
                return True


def _expected_output_dir(rows: list[dict]) -> str | None:
    """算出這份紀錄「應該」的輸出資料夾（用來擋下被竄改、指到資料夾外的列）。

    新格式的列都有「輸出資料夾」欄位；舊格式沒有時，退而求其次比對新路徑的上兩層資料夾，
    整份紀錄裡多數列一致的那個資料夾就當作標準答案。
    """
    votes: dict[str, int] = {}
    for row in rows:
        declared = row.get("輸出資料夾") or ""
        try:
            key = os.path.normcase(str(Path(declared).resolve())) if declared else \
                os.path.normcase(str(Path(row["新路徑"]).resolve().parent.parent))
        except (OSError, KeyError, ValueError, TypeError):
            continue
        votes[key] = votes.get(key, 0) + 1
    return max(votes, key=votes.get) if votes else None


def _row_output_ok(row: dict, expected: str | None) -> bool:
    if expected is None:
        return True
    try:
        actual = os.path.normcase(str(Path(row["新路徑"]).resolve().parent.parent))
    except (OSError, KeyError, ValueError, TypeError):
        return False
    return actual == expected


def _validate_row(row: dict, expected_output: str | None) -> str | None:
    """檢查一列紀錄是否可信；紀錄檔被竄改也不該讓復原去動任意的檔案。合格回傳 None。"""
    if not _row_signature_ok(row):
        return "紀錄簽章不符（紀錄可能被修改過，或不是這台電腦上的本程式產生的），為了安全已略過"
    action = row.get("動作", "")
    if action not in VALID_ACTIONS:
        return f"不明的動作「{action}」，為了安全已略過"
    try:
        src, dst = Path(row["原始路徑"]), Path(row["新路徑"])
    except (KeyError, TypeError):
        return "紀錄格式不正確"
    if not src.is_absolute() or not dst.is_absolute():
        return "紀錄中的路徑不是絕對路徑，為了安全已略過"
    if src == dst:
        return "原始路徑與新路徑相同，為了安全已略過"
    src_ext, dst_ext = src.suffix.lower(), dst.suffix.lower()
    if dst_ext != src_ext or dst_ext not in (IMAGE_EXTS | VIDEO_EXTS):
        return "新路徑的副檔名不正確，為了安全已略過"
    if not _row_output_ok(row, expected_output):
        return "新路徑不在這份紀錄的輸出資料夾內，為了安全已略過"
    return None


def undo(log_path: Path) -> ExecuteResult:
    """依紀錄檔復原：搬移的檔案搬回原位；複製出來的檔案移到資源回收筒。

    - 複製的原檔已經不在：把複本搬回原位（不會刪掉唯一的一份）
    - 複本跟目前的原檔內容不完全相同（原檔後來被改過或換過）：不刪除，列為失敗
    - 只有 pending、沒有確認列的檔案：依檔案實際位置判斷有沒有真的發生；確定沒發生才略過，
      無法確定（原位置與新位置都有檔案）就列為失敗、保留紀錄，絕不當作已復原
    - 全部成功才把紀錄改名為「已復原」；有失敗時紀錄只留下失敗的列，下次按復原會重試這些檔案
    """
    log_path = Path(log_path)
    rows = read_log(log_path)
    expected_output = _expected_output_dir(rows)
    quarantine_dir = log_path.parent / QUARANTINE_DIR / datetime.now().strftime("%Y%m%d_%H%M%S")
    errors: list[tuple[Path, str]] = []
    warnings: list[str] = []
    failed_rows: list[dict] = []
    done = 0
    for row in reversed(rows):
        reason = _validate_row(row, expected_output)
        if reason:
            errors.append((Path(str(row.get("新路徑") or "?")), reason))
            failed_rows.append(row)
            continue
        action = row["動作"]
        pending = action.startswith("pending-")
        src, dst = Path(row["原始路徑"]), Path(row["新路徑"])
        try:
            if action in MOVE_ACTIONS:
                if pending:
                    # 只有 pending 列：依檔案實際位置判斷搬移到底有沒有發生
                    if not dst.exists() and src.exists():
                        continue  # 沒有真的發生（檔案還在原位），略過（不算錯誤）
                    if not (dst.exists() and not src.exists()):
                        # 兩邊都有檔案或兩邊都沒有：無法確定，保留這一列讓使用者處理後重試，絕不當作完成
                        raise OSError(f"無法確定這個檔案是否已搬移（原位置與新位置"
                                      f"{'都有檔案' if dst.exists() else '都找不到檔案'}），請手動確認：{dst}")
                if not dst.exists():
                    raise FileNotFoundError(f"找不到檔案（可能已被移動或刪除）：{dst}")
                if src.exists():
                    raise FileExistsError(f"原位置已有同名檔案：{src}")
                _move_back(dst, src)
            else:
                if pending and not dst.exists():
                    continue  # 複製沒有真的完成，略過（不算錯誤）
                if not dst.exists():
                    raise FileNotFoundError(f"找不到檔案（可能已被移動或刪除）：{dst}")
                if not src.exists():
                    _move_back(dst, src)
                elif _same_content(src, dst):
                    location = _delete(dst, quarantine_dir)
                    if location is not None:
                        warnings.append(f"{dst.name}：沒有資源回收筒可用，已搬到隔離資料夾：{location}")
                else:
                    raise OSError(f"整理後原始檔案已變更，為了安全，保留複本：{dst}")
            done += 1
            _remove_if_empty(dst.parent)
        except OSError as exc:
            errors.append((dst, str(exc)))
            failed_rows.append(row)

    try:
        if errors:
            failed_rows.reverse()
            tmp_fd, tmp_name = tempfile.mkstemp(dir=log_path.parent, suffix=".tmp")
            tmp_path = Path(tmp_name)
            try:
                with os.fdopen(tmp_fd, "w", newline="", encoding="utf-8-sig") as f:
                    writer = csv.DictWriter(f, fieldnames=LOG_FIELDS, extrasaction="ignore")
                    writer.writeheader()
                    writer.writerows(failed_rows)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_path, log_path)  # 原子性換檔：寫到一半失敗也不會動到原紀錄
            except OSError:
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
                raise
        else:
            os.replace(log_path, log_path.with_name(log_path.name.replace(LOG_PREFIX, UNDONE_PREFIX, 1)))
    except OSError as exc:
        errors.append((log_path, f"無法更新紀錄檔（是否正被其他程式開啟？）：{exc}"))
    return ExecuteResult(done, errors, log_path, warnings)


def _remove_if_empty(folder: Path) -> None:
    try:
        folder.rmdir()  # 只有空資料夾才會成功
    except OSError:
        pass
