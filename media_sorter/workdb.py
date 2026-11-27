"""工作資料庫：把辨識結果與你的修改存在磁碟上，程式被關掉、當掉都能接續。

位置：使用者資料夾底下的 analysis-cache（config.WORK_DIR）
- work.sqlite3　每個檔案的辨識結果（向量、日期、縮圖檔名）與你指定的分類／略過
- thumbs/　　　縮圖（JPEG 檔）。以前縮圖的位元組都留在記憶體裡，十萬張就是好幾 GB；現在記憶體裡只有檔名

規則
- 每個檔案辨識完就寫入並 commit（checkpoint）；你改分類、略過、撤回，也是當下就寫入
- 一筆辨識結果只在「同一個檔案（路徑＋大小＋修改時間）、同一個模型與前處理版本（model_key）、
  影片的取樣張數也相同」時才會被沿用；任何一項不同就重新辨識
- 你的修改另外存一張表，也綁著檔案的大小與修改時間：檔案內容換過，舊的決定就不再套用
- 分類的分數不存：分數是向量跟「目前的分類清單」比出來的，接續時重算（很快），分類清單改了也不會用到舊分數
- 資料庫壞掉或版本不合：改名留在旁邊，重新建立一個空的；最壞情況只是要重新辨識，不影響任何照片
- 這裡存的是「衍生資料」：縮圖是你照片的縮小版，所以解除安裝時可以選擇一併移除（見 uninstaller）

耐久性：WAL 模式＋synchronous=NORMAL。程式被強制結束（當掉、工作管理員結束）不會遺失已經 commit 的資料；
整台電腦突然斷電時，最後幾筆可能沒寫到磁碟（資料庫本身不會壞），那幾個檔案下次會重新辨識。
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np

SCHEMA_VERSION = 1
DB_NAME = "work.sqlite3"
THUMB_DIR = "thumbs"
MARKER = ".media-sorter-work"  # 這個資料夾是本程式建立的（解除安裝程式只會清理有這個標記的資料夾）
DECISION_STATUSES = ("confirmed", "skipped")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS files (
    path_key     TEXT PRIMARY KEY,   -- 標準化的絕對路徑（Windows 不分大小寫）
    path         TEXT NOT NULL,
    size         INTEGER NOT NULL,
    mtime_ns     INTEGER NOT NULL,
    kind         TEXT NOT NULL,
    model_key    TEXT NOT NULL,      -- 模型＋版本＋前處理版本
    video_frames INTEGER NOT NULL,   -- 影片取樣張數（圖片是 0）
    media_date   TEXT,
    dim          INTEGER NOT NULL,
    embedding    BLOB NOT NULL,      -- float32
    frames       BLOB,               -- 影片每張畫面的向量 float32 (n, dim)
    thumb        TEXT,               -- thumbs/ 底下的相對路徑
    analyzed_at  TEXT NOT NULL,
    seen_at      TEXT NOT NULL,
    incomplete   INTEGER NOT NULL DEFAULT 0   -- 檔案看起來沒傳完（1.1 之後加的欄位；舊資料庫開啟時會補上）
);
CREATE TABLE IF NOT EXISTS decisions (
    path_key   TEXT PRIMARY KEY,
    size       INTEGER NOT NULL,
    mtime_ns   INTEGER NOT NULL,
    status     TEXT NOT NULL,        -- confirmed／skipped
    chosen     TEXT,
    updated_at TEXT NOT NULL
);
"""


def path_key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def file_fingerprint(path: Path) -> tuple[int, int] | None:
    """（大小, 修改時間）；檔案不在回傳 None。"""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return st.st_size, st.st_mtime_ns


@dataclass
class CachedAnalysis:
    embedding: np.ndarray
    frames: np.ndarray | None
    date: datetime | None
    thumb_file: Path | None
    incomplete: bool = False


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _is_temporary(exc: sqlite3.Error) -> bool:
    """「暫時打不開」而不是「壞掉」：被鎖住、忙碌中、開不了檔。"""
    text = str(exc).lower()
    return isinstance(exc, sqlite3.OperationalError) and any(
        word in text for word in ("locked", "busy", "unable to open", "disk i/o", "readonly", "read-only"))


class WorkStore:
    """一個資料夾裡的工作資料庫與縮圖快取。可以從背景執行緒（辨識）與主執行緒（你的修改）同時使用。"""

    def __init__(self, folder: Path):
        self.folder = Path(folder)
        self.thumbs = self.folder / THUMB_DIR
        self.db_path = self.folder / DB_NAME
        self._lock = threading.RLock()
        self.recreated_from: Path | None = None  # 舊資料庫壞掉時，被改名保留的那一份
        self.folder.mkdir(parents=True, exist_ok=True)
        self.thumbs.mkdir(exist_ok=True)
        marker = self.folder / MARKER
        if not marker.exists():
            marker.write_text("AI Media Sorter analysis cache. Safe to delete; files will be analysed again.\n",
                              encoding="utf-8")
        self._db = self._open()

    # ------------------------------------------------------------------ 開啟／關閉
    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.db_path, check_same_thread=False, timeout=30)
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.Error:
            db.close()  # Windows：連線沒關，檔案就不能改名或刪除
            raise
        return db

    def _open(self) -> sqlite3.Connection:
        db = None
        try:
            db = self._connect()
            db.executescript(_SCHEMA)
            row = db.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
            if row is None:
                db.execute("INSERT INTO meta VALUES ('schema', ?)", (str(SCHEMA_VERSION),))
                db.commit()
            elif row[0] != str(SCHEMA_VERSION):
                raise sqlite3.DatabaseError(f"schema {row[0]}")
            if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise sqlite3.DatabaseError("quick_check failed")
            # 之後才加的欄位：舊資料庫補上就好，不必重建（使用者改過的分類都留著）
            columns = {row[1] for row in db.execute("PRAGMA table_info(files)")}
            if "incomplete" not in columns:
                db.execute("ALTER TABLE files ADD COLUMN incomplete INTEGER NOT NULL DEFAULT 0")
                db.commit()
            return db
        except sqlite3.DatabaseError as exc:
            if db is not None:
                try:
                    db.close()  # 一定要先關：Windows 上開著的檔案不能改名（用 Wine 測出來的）
                except sqlite3.Error:
                    pass
            if _is_temporary(exc):
                # 只是暫時打不開（被別的程式鎖住、防毒掃描中）：資料庫沒壞，裡面有使用者改過的分類，不能擱置重來。
                # 往上丟，這次先不用資料庫（程式照常運作），下次開啟再試。
                raise
            return self._recreate()

    def _recreate(self) -> sqlite3.Connection:
        """資料庫讀不出來：改名保留，重新開始。只是快取，最壞情況是重新辨識。"""
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        aside = self.db_path.with_name(f"無法讀取_{stamp}_{DB_NAME}")
        os.replace(self.db_path, aside)  # 改不了名就往上丟：寧可這次不用資料庫，也不刪掉裡面可能還救得回的資料
        self.recreated_from = aside
        for suffix in ("-wal", "-shm"):
            Path(str(self.db_path) + suffix).unlink(missing_ok=True)
        db = self._connect()
        db.executescript(_SCHEMA)
        db.execute("INSERT OR REPLACE INTO meta VALUES ('schema', ?)", (str(SCHEMA_VERSION),))
        db.commit()
        return db

    def close(self) -> None:
        with self._lock:
            try:
                self._db.close()
            except sqlite3.Error:
                pass

    # ------------------------------------------------------------------ 縮圖（磁碟快取）
    def _thumb_name(self, key: str, fingerprint: tuple[int, int]) -> str:
        digest = hashlib.sha1(f"{key}|{fingerprint[0]}|{fingerprint[1]}".encode("utf-8", "surrogatepass")).hexdigest()
        return f"{digest[:2]}/{digest}.jpg"

    def _write_thumb(self, name: str, data: bytes) -> None:
        target = self.thumbs / name
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(f".{threading.get_ident()}.tmp")
        tmp.write_bytes(data)
        os.replace(tmp, target)

    def _drop_thumb(self, name: str | None) -> None:
        if name:
            try:
                (self.thumbs / name).unlink()
            except OSError:
                pass

    # ------------------------------------------------------------------ 辨識結果
    def lookup(self, path: Path, model_key: str, video_frames: int,
               fingerprint: tuple[int, int] | None = None) -> CachedAnalysis | None:
        """這個檔案沒變、模型與前處理也一樣 → 回傳上次的辨識結果；否則 None（要重新辨識）。"""
        fingerprint = fingerprint or file_fingerprint(path)
        if fingerprint is None:
            return None
        key = path_key(path)
        with self._lock:
            row = self._db.execute(
                "SELECT size, mtime_ns, model_key, video_frames, media_date, dim, embedding, frames, thumb, incomplete "
                "FROM files WHERE path_key=?", (key,)).fetchone()
            if row is None or (row[0], row[1]) != fingerprint or row[2] != model_key or row[3] != video_frames:
                return None
            self._db.execute("UPDATE files SET seen_at=? WHERE path_key=?", (_now(), key))
        dim = row[5]
        embedding = np.frombuffer(row[6], dtype=np.float32)
        if embedding.size != dim or not np.isfinite(embedding).all():
            return None
        frames = np.frombuffer(row[7], dtype=np.float32).reshape(-1, dim).copy() if row[7] else None
        thumb = self.thumbs / row[8] if row[8] else None
        if thumb is not None and not thumb.is_file():
            return None  # 縮圖檔被刪掉：整筆重做，預覽才不會是空的
        date = None
        if row[4]:
            try:
                date = datetime.fromisoformat(row[4])
            except ValueError:
                date = None
        return CachedAnalysis(embedding.copy(), frames, date, thumb, bool(row[9]))

    def save_analysis(self, path: Path, kind: str, model_key: str, video_frames: int, fingerprint: tuple[int, int],
                      embedding: np.ndarray, frames: np.ndarray | None, date: datetime | None,
                      thumbnail: bytes | None, incomplete: bool = False) -> Path | None:
        """存一個檔案的辨識結果（不 commit；呼叫端每完成一批呼叫 commit）。回傳縮圖檔的位置。"""
        key = path_key(path)
        name = None
        if thumbnail:
            name = self._thumb_name(key, fingerprint)
            self._write_thumb(name, thumbnail)
        embedding = np.ascontiguousarray(embedding, dtype=np.float32)
        frame_blob = np.ascontiguousarray(frames, dtype=np.float32).tobytes() if frames is not None else None
        now = _now()
        with self._lock:
            old = self._db.execute("SELECT thumb FROM files WHERE path_key=?", (key,)).fetchone()
            self._db.execute(
                "INSERT OR REPLACE INTO files (path_key, path, size, mtime_ns, kind, model_key, video_frames, media_date,"
                " dim, embedding, frames, thumb, analyzed_at, seen_at, incomplete) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (key, str(path), fingerprint[0], fingerprint[1], kind, model_key, video_frames,
                 date.isoformat() if date else None, int(embedding.size), embedding.tobytes(), frame_blob, name,
                 now, now, int(bool(incomplete))))
            # 檔案內容換過：舊的決定是針對舊內容做的，不再適用
            self._db.execute("DELETE FROM decisions WHERE path_key=? AND (size<>? OR mtime_ns<>?)",
                             (key, fingerprint[0], fingerprint[1]))
        if old and old[0] and old[0] != name:
            self._drop_thumb(old[0])
        return self.thumbs / name if name else None

    def commit(self) -> None:
        with self._lock:
            self._db.commit()

    # ------------------------------------------------------------------ 你的修改
    def save_decision(self, path: Path, status: str, chosen: str | None,
                      fingerprint: tuple[int, int] | None = None) -> None:
        """你指定了分類或略過 → 立刻寫入；改回「待確認」→ 刪掉那一筆。"""
        key = path_key(path)
        with self._lock:
            if status not in DECISION_STATUSES:
                self._db.execute("DELETE FROM decisions WHERE path_key=?", (key,))
            else:
                fingerprint = fingerprint or file_fingerprint(path)
                if fingerprint is None:
                    return
                self._db.execute("INSERT OR REPLACE INTO decisions VALUES (?,?,?,?,?,?)",
                                 (key, fingerprint[0], fingerprint[1], status, chosen, _now()))
            self._db.commit()

    def load_decision(self, path: Path, fingerprint: tuple[int, int] | None = None) -> tuple[str, str | None] | None:
        fingerprint = fingerprint or file_fingerprint(path)
        if fingerprint is None:
            return None
        with self._lock:
            row = self._db.execute("SELECT size, mtime_ns, status, chosen FROM decisions WHERE path_key=?",
                                   (path_key(path),)).fetchone()
        if row is None or (row[0], row[1]) != fingerprint or row[2] not in DECISION_STATUSES:
            return None
        return row[2], row[3]

    # ------------------------------------------------------------------ 維護
    def count_cached(self, folder: Path) -> int:
        """這個資料夾（含子資料夾）裡有多少檔案有辨識結果。介面用來說「會沿用上次的結果」。"""
        prefix = path_key(folder).rstrip("\\/") + os.sep
        escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM files WHERE path_key LIKE ? ESCAPE '\\'",
                                    (escaped + "%",)).fetchone()[0]

    def stats(self) -> tuple[int, int]:
        with self._lock:
            files = self._db.execute("SELECT COUNT(*) FROM files").fetchone()[0]
            decisions = self._db.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
        return files, decisions

    def prune(self, max_age_days: int = 365) -> int:
        """很久沒再掃到的檔案（例如資料夾已經刪掉）：移除紀錄與縮圖，避免快取無限長大。回傳移除的筆數。"""
        cutoff = datetime.fromtimestamp(datetime.now().timestamp() - max_age_days * 86400).isoformat(timespec="seconds")
        with self._lock:
            rows = self._db.execute("SELECT path_key, thumb FROM files WHERE seen_at < ?", (cutoff,)).fetchall()
            for key, thumb in rows:
                self._db.execute("DELETE FROM files WHERE path_key=?", (key,))
                self._db.execute("DELETE FROM decisions WHERE path_key=?", (key,))
                self._drop_thumb(thumb)
            self._db.commit()
        return len(rows)

    def clear(self) -> None:
        """清掉所有保存的辨識結果、你的修改與縮圖（使用者在「設定」裡要求時）。"""
        with self._lock:
            self._db.execute("DELETE FROM files")
            self._db.execute("DELETE FROM decisions")
            self._db.commit()
            self._db.execute("VACUUM")
        for sub in self.thumbs.iterdir() if self.thumbs.is_dir() else ():
            if sub.is_dir():
                for file in sub.iterdir():
                    try:
                        file.unlink()
                    except OSError:
                        pass
                try:
                    sub.rmdir()
                except OSError:
                    pass
