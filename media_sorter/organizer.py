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
- 先驗證簽章、再合併同一檔案的多列：壞掉的列永遠不能取代有效的列；任何一列出錯都只影響那一列
- 每批紀錄有隨機批次編號，已復原的項目記錄在金鑰旁的清單，舊紀錄被放回來也不會再執行一次
"""

from __future__ import annotations

import codecs
import csv
import errno
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from .config import IMAGE_EXTS, VIDEO_EXTS, log_key_dir, log_signing_key

try:  # 選用套件：沒有安裝就搬到隔離資料夾，而不是直接刪除
    from send2trash import send2trash as _send2trash
except ImportError:  # pragma: no cover - 選用功能
    _send2trash = None

INVALID_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
LOG_FIELDS = ["動作", "原始路徑", "新路徑", "分類", "時間", "大小", "修改時間", "輸出資料夾", "批次", "簽章"]
SIGNED_FIELDS = LOG_FIELDS[:-1]
SIGNATURE_RE = re.compile(r"[0-9a-f]{64}")
BATCH_RE = re.compile(r"[0-9a-f]{16}")
PARTIAL_SIGNATURE_RE = re.compile(r"[0-9a-f]{0,63}")
UNDONE_REGISTRY = "undone-rows.txt"  # 已處理項目（已復原或確定沒發生）的清單，放在金鑰旁邊，防止舊紀錄被重放
REJECTED_PREFIX = "無法驗證的列_"  # 另存檔刻意不以「整理紀錄_」開頭，永遠不會被當成可復原的紀錄
LOG_NAME_RE = re.compile(r"整理紀錄_\d{8}_\d{6}(?:_\d{1,3})?\.csv")
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
    """一列紀錄的 HMAC-SHA256 簽章（涵蓋除了簽章本身以外的所有欄位）。

    用 JSON 陣列當作簽章內容，欄位邊界是明確的；若用分隔字元串接，
    欄位內含分隔字元時就能在不需要金鑰的情況下「搬動欄位邊界」而簽章不變。
    """
    message = json.dumps(list(values), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hmac.new(log_signing_key(), message, "sha256").hexdigest()


def _signed_row(values: list) -> list[str]:
    text = ["" if v is None else str(v) for v in values]
    return text + [_signature(text)]


def _row_signature_ok(row: dict) -> bool:
    """簽章是否正確。任何格式不對（缺欄位、非十六進位、含中文⋯）都只回傳 False，絕不丟例外。"""
    signature = row.get("簽章")
    if not isinstance(signature, str) or not SIGNATURE_RE.fullmatch(signature):
        return False
    values = [row.get(k) for k in SIGNED_FIELDS]
    if not all(isinstance(v, str) for v in values):  # 欄位不足 = 寫到一半的列
        return False
    return hmac.compare_digest(signature.encode("ascii"), _signature(values).encode("ascii"))


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
    batch = secrets.token_hex(8)  # 這一批的隨機編號（簽章的一部分），用來防止已復原的紀錄被重放
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
                                             datetime.now().isoformat(timespec="seconds"), "", "", output_dir,
                                             batch]))
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
                                             st.st_mtime_ns, output_dir, batch]))
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
    """最新一份還能復原的整理紀錄。只接受程式自己產生的檔名格式（整理紀錄_日期_時間[_序號].csv），
    其他任何檔案（另存的無法驗證列、使用者自己複製的檔案⋯）都不會被選到。"""
    logs = [p for p in Path(log_dir).glob(f"{LOG_PREFIX}*.csv") if LOG_NAME_RE.fullmatch(p.name) and p.is_file()]
    return max(logs, key=lambda p: (p.stat().st_mtime_ns, p.name)) if logs else None


@dataclass
class LogEntry:
    """紀錄中的一個檔案：row 是用來復原的那一列（有確認列就用確認列），raw 是這個檔案所有簽章有效的列。"""

    row: dict
    raw: list[dict]


def _decode_bytes(raw: bytes) -> str:
    """把紀錄檔的位元組轉成文字。

    寫入中斷可能切在一個中文字（UTF-8 多位元組）的中間：這時只丟掉檔尾那個不完整的字，
    前面完整的內容照常使用（殘缺的最後一列之後會被判定為「寫到一半」而忽略）。
    """
    body = raw[len(codecs.BOM_UTF8):] if raw.startswith(codecs.BOM_UTF8) else raw
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError as exc:
        # 只有「檔尾一個字不完整」才截掉（中間壞掉的位元組不算，交給下面的其他編碼處理）
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


def _decode_log(log_path: Path) -> list[dict]:
    text = _decode_bytes(Path(log_path).read_bytes())
    # newline="" 讓 csv 模組自己處理換行：被引號包住的欄位（例如含換行的分類名稱）才不會被切斷
    rows = list(csv.DictReader(io.StringIO(text, newline="")))
    required = {"動作", "原始路徑", "新路徑"}
    if rows and not required <= set(rows[0]):
        raise ValueError("紀錄檔格式不正確（缺少必要欄位）")
    return rows


def _looks_truncated(row: dict) -> bool:
    """這一列看起來是不是「寫到一半」：只可能是一列的前半段——欄位不足，或批次完整但簽章只寫出一部分。

    欄位齊全卻沒有合法批次編號的列（例如偽造的列）不算，會照常回報為無法驗證。
    """
    if any(row.get(k) is None for k in LOG_FIELDS):
        return True
    return bool(BATCH_RE.fullmatch(row["批次"]) and PARTIAL_SIGNATURE_RE.fullmatch(row["簽章"]))


def read_log_entries(log_path: Path) -> tuple[list[LogEntry], list[dict], bool]:
    """讀取紀錄檔，回傳 (可以復原的項目, 無法驗證的列, 最後一列是否寫到一半而被忽略)。
    被 Excel 另存成 Big5（cp950）也讀得懂。

    先逐列驗證簽章，只有簽章正確的列才參與合併：同一個檔案（同批次、同新路徑）
    有確認列就用確認列，否則用 pending 列。寫到一半或被竄改的列永遠不能取代有效的列；
    若同一個檔案已經有有效的列，這種殘缺列直接視為「寫到一半」而忽略。
    """
    groups: dict[tuple[str, str], list[dict]] = {}
    invalid: list[dict] = []
    truncated_tail = False
    decoded = _decode_log(log_path)
    for index, row in enumerate(decoded):
        if _row_signature_ok(row):
            groups.setdefault((row["批次"], row["新路徑"]), []).append(row)
            continue
        if not any(isinstance(v, str) and v.strip() for k, v in row.items() if k is not None):
            continue  # 空白列
        if index == len(decoded) - 1 and _looks_truncated(row):
            # 寫入中斷只會發生在最後一列（execute 遇到寫入失敗就停止），而且它之前的 pending 列已經同步到磁碟
            truncated_tail = True
            continue
        invalid.append(row)
    entries = []
    for rows in groups.values():
        confirmed = [r for r in rows if not r["動作"].startswith("pending-")]
        entries.append(LogEntry(confirmed[-1] if confirmed else rows[-1], rows))
    valid_targets = {key[1] for key in groups}
    rejected = [r for r in invalid if r.get("新路徑") not in valid_targets]
    return entries, rejected, truncated_tail


def read_log(log_path: Path) -> list[dict]:
    """讀取紀錄檔中可以復原的項目（每個檔案一列）。"""
    return [entry.row for entry in read_log_entries(log_path)[0]]


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
    if not str(row.get("批次") or "").strip():
        return "紀錄缺少批次編號，為了安全已略過"
    src_ext, dst_ext = src.suffix.lower(), dst.suffix.lower()
    if dst_ext != src_ext or dst_ext not in (IMAGE_EXTS | VIDEO_EXTS):
        return "新路徑的副檔名不正確，為了安全已略過"
    if not _row_output_ok(row, expected_output):
        return "新路徑不在這份紀錄的輸出資料夾內，為了安全已略過"
    return None


def _registry_id(row: dict) -> str:
    material = json.dumps([row["批次"], row["原始路徑"], row["新路徑"]], ensure_ascii=False)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _load_registry() -> set[str]:
    try:
        return set((log_key_dir() / UNDONE_REGISTRY).read_text(encoding="ascii").split())
    except FileNotFoundError:
        return set()


def _remember_undone(entry_id: str) -> None:
    path = log_key_dir() / UNDONE_REGISTRY
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="ascii") as f:
        f.write(entry_id + "\n")
        f.flush()
        os.fsync(f.fileno())


def _undo_one(row: dict, quarantine_dir: Path, warnings: list[str]) -> bool:
    """復原一個檔案。回傳 True = 已復原；False = 確定沒發生過、不需要處理。無法處理時丟 OSError。"""
    action = row["動作"]
    pending = action.startswith("pending-")
    src, dst = Path(row["原始路徑"]), Path(row["新路徑"])
    if action in MOVE_ACTIONS:
        if pending:
            # 只有 pending 列：依檔案實際位置判斷搬移到底有沒有發生
            if not dst.exists() and src.exists():
                return False  # 沒有真的發生（檔案還在原位）
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
            return False  # 複製沒有真的完成
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
    _remove_if_empty(dst.parent)
    return True


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


def undo(log_path: Path) -> ExecuteResult:
    """依紀錄檔復原：搬移的檔案搬回原位；複製出來的檔案移到資源回收筒。

    - 複製的原檔已經不在：把複本搬回原位（不會刪掉唯一的一份）
    - 複本跟目前的原檔內容不完全相同（原檔後來被改過或換過）：不刪除，列為失敗
    - 只有 pending、沒有確認列的檔案：依檔案實際位置判斷有沒有真的發生；確定沒發生才略過，
      無法確定（原位置與新位置都有檔案）就列為失敗、保留紀錄，絕不當作已復原
    - 已經處理過的項目（已復原或確定沒發生；舊紀錄被放回來時）一律略過，不會再動檔案
    - 任何一列出錯（包括意料之外的錯誤）都只影響那一列，其他列照常復原
    - 沒有失敗的項目就把紀錄改名為「已復原」；有失敗時紀錄只留下失敗項目的所有有效列，下次按復原會重試
    - 無法驗證的列另存到「<紀錄>.無法驗證的列.csv」並回報，不會卡住之後的復原
    """
    log_path = Path(log_path)
    entries, rejected, truncated_tail = read_log_entries(log_path)
    rows = [entry.row for entry in entries]
    expected_output = _expected_output_dir(rows)
    quarantine_dir = log_path.parent / QUARANTINE_DIR / datetime.now().strftime("%Y%m%d_%H%M%S")
    registry = _load_registry()
    errors: list[tuple[Path, str]] = []
    warnings: list[str] = []
    failed: list[LogEntry] = []
    done = 0

    if truncated_tail:
        warnings.append("紀錄最後一列寫入不完整（當時可能磁碟已滿），已忽略；對應的檔案依之前完整寫入的紀錄處理")
    for target in dict.fromkeys(str(row.get("新路徑") or "?") for row in rejected):  # 每個檔案只回報一次
        errors.append((Path(target),
                       "紀錄簽章不符（紀錄可能被修改過、寫入不完整，或不是這台電腦上的本程式產生的），為了安全已略過"))

    for entry in reversed(entries):
        row = entry.row
        dst = Path(str(row.get("新路徑") or "?"))
        try:
            reason = _validate_row(row, expected_output)
            if reason:
                errors.append((dst, reason))
                failed.append(entry)
                continue
            entry_id = _registry_id(row)
            if entry_id in registry:
                warnings.append(f"{dst.name}：這個項目已經處理過（可能是舊紀錄被放回），已略過")
                continue
            # 每個離開這份紀錄的項目（已復原，或確定沒發生過）都要登記為「已處理」，
            # 否則舊紀錄被放回來時，會在檔案位置改變後被重新判斷而誤動到別的檔案
            if _undo_one(row, quarantine_dir, warnings):
                done += 1
                try:
                    _remember_undone(entry_id)
                except OSError as exc:
                    warnings.append(f"{dst.name}：已復原，但無法記錄到已處理清單（{exc}）")
            else:
                try:
                    _remember_undone(entry_id)
                except OSError as exc:  # 無法登記就不能封存：留在紀錄裡，下次再判斷
                    errors.append((dst, f"確定沒有搬移過，但無法記錄到已處理清單，暫不封存（{exc}）"))
                    failed.append(entry)
        except OSError as exc:
            errors.append((dst, str(exc)))
            failed.append(entry)
        except Exception as exc:  # noqa: BLE001 - 意料之外的錯誤也只影響這一列
            errors.append((dst, f"未預期的錯誤（{type(exc).__name__}: {exc}），這個檔案沒有被處理"))
            failed.append(entry)

    try:
        if rejected:
            side = log_path.with_name(REJECTED_PREFIX + log_path.name)
            with open(side, "a", newline="", encoding="utf-8-sig") as f:
                writer = csv.DictWriter(f, fieldnames=LOG_FIELDS, extrasaction="ignore", restval="")
                if f.tell() == 0:
                    writer.writeheader()
                writer.writerows(rejected)
            warnings.append(f"無法驗證的 {len(rejected)} 列已另存到：{side.name}")
        if failed:
            failed.reverse()
            _write_rows_atomic(log_path, [raw for entry in failed for raw in entry.raw])
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
