"""連網政策：辨識時「嚴格離線」；只有使用者明確同意下載或修復模型時才連網。

模型（CLIP）與斷詞器（tokenizer）都在本機執行，照片不會上傳。會連網的只有「取得模型檔」這件事，
而且有三種明確的狀態，彼此不會自動切換：

1. 嚴格離線（預設，辨識時一律如此）
   載入模型前先核對這台電腦上的模型檔（固定的版本、每個檔案的大小與 SHA-256）。
   缺檔、大小不對、雜湊不對、記憶體不足、套件版本不合……任何錯誤都只會回報原因與修法，**絕不會**自己打開網路重試。
2. 使用者明確同意後下載（download_preset）
   只在這個函式執行期間允許連網，只下載 config.MODEL_PRESETS 列出的檔案、固定的 commit，下載完核對雜湊，然後回到離線。
3. 使用者明確要求修復（repair_preset）
   重新計算每個檔案的雜湊，刪掉不對的，再走第 2 種流程把缺的補齊。

另外一律關閉使用統計（HF_HUB_DISABLE_TELEMETRY）。Hugging Face 的元件預設每次載入都會連到 huggingface.co
確認有沒有新版本；離線模式加上「直接給本機檔案路徑」讓它完全沒有理由連網（測試會把 socket 擋掉來證明）。

這個模組只用標準函式庫（下載時才載入 huggingface_hub），必須在載入 huggingface_hub／transformers 之前匯入。
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .config import MODEL_PRESETS, MODEL_STAMP_FILE

os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
os.environ.setdefault("DO_NOT_TRACK", "1")

HASH_CHUNK = 4 * 1024 * 1024


def hf_cache_dir(environ: dict | None = None) -> Path:
    """Hugging Face 的模型快取位置（跟 huggingface_hub 的規則相同）。"""
    env = os.environ if environ is None else environ
    if env.get("HF_HUB_CACHE"):
        return Path(env["HF_HUB_CACHE"])
    if env.get("HF_HOME"):
        return Path(env["HF_HOME"]) / "hub"
    if env.get("XDG_CACHE_HOME"):
        return Path(env["XDG_CACHE_HOME"]) / "huggingface" / "hub"
    return Path.home() / ".cache" / "huggingface" / "hub"


def cache_folder(repo_id: str) -> str:
    return "models--" + repo_id.strip("/").replace("/", "--")


def snapshot_dir(repo_id: str, revision: str, cache: Path | None = None) -> Path:
    """這個模型「固定版本」的檔案所在的資料夾（Hugging Face 快取的標準位置）。"""
    return (cache or hf_cache_dir()) / cache_folder(repo_id) / "snapshots" / revision


# ---------------------------------------------------------------------------- 離線開關
def set_offline(offline: bool) -> None:
    """切換離線模式。環境變數給之後才載入的元件；已載入的 huggingface_hub 直接改它的設定值。"""
    value = "1" if offline else "0"
    os.environ["HF_HUB_OFFLINE"] = value
    os.environ["TRANSFORMERS_OFFLINE"] = value
    constants = sys.modules.get("huggingface_hub.constants")
    if constants is not None:
        constants.HF_HUB_OFFLINE = offline


def is_offline() -> bool:
    return os.environ.get("HF_HUB_OFFLINE") == "1"


set_offline(True)  # 預設就是離線：沒有任何程式路徑會「順便」連網

# ---------------------------------------------------------------------------- 第二道攔截：直接擋連線
# 環境變數只對「會看它」的元件有效。視窗程式啟動時另外裝上這一道：本行程任何對外的 socket 連線都會被拒絕，
# 只有 download_preset 執行期間（使用者同意下載）才放行。連到本機（127.0.0.1、::1、localhost）不擋。
_guard = {"installed": False, "allowed": 0, "blocked": []}
LOCAL_HOSTS = ("127.", "::1", "localhost", "0.0.0.0")


class NetworkBlocked(OSError):
    """離線模式下有程式碼想連網，被擋下來了。"""


def _is_local(address) -> bool:
    if not isinstance(address, tuple) or not address:
        return True  # 不是 IP 連線（例如本機的 Unix socket）
    host = str(address[0])
    return host.startswith(LOCAL_HOSTS) or host == ""


def install_network_guard() -> None:
    """裝上「離線時擋掉所有對外連線」的保護（視窗程式啟動時呼叫一次；重複呼叫沒有作用）。"""
    if _guard["installed"]:
        return
    import socket

    real_connect, real_connect_ex = socket.socket.connect, socket.socket.connect_ex

    def check(address) -> None:
        if _guard["allowed"] <= 0 and not _is_local(address):
            _guard["blocked"].append(str(address[0]))
            raise NetworkBlocked(f"離線模式：已擋下對 {address[0]} 的連線（只有你同意下載或修復模型時才會連網）")

    def connect(self, address):
        check(address)
        return real_connect(self, address)

    def connect_ex(self, address):
        check(address)
        return real_connect_ex(self, address)

    socket.socket.connect, socket.socket.connect_ex = connect, connect_ex
    _guard.update(installed=True, real=(real_connect, real_connect_ex))


def remove_network_guard() -> None:
    """拿掉保護（測試用）。"""
    if _guard["installed"]:
        import socket

        socket.socket.connect, socket.socket.connect_ex = _guard["real"]
        _guard.update(installed=False, allowed=0)


def blocked_connections() -> list[str]:
    return list(_guard["blocked"])


@contextmanager
def _network_allowed():
    """只有 download_preset 會用：這段期間允許連網，離開時一定回到離線（就算下載失敗）。"""
    set_offline(False)
    _guard["allowed"] += 1
    try:
        yield
    finally:
        _guard["allowed"] -= 1
        set_offline(True)


# ---------------------------------------------------------------------------- 模型檔清單與核對
@dataclass(frozen=True)
class ModelFile:
    repo: str
    revision: str
    name: str
    size: int
    sha256: str

    def path(self, cache: Path | None = None) -> Path:
        return snapshot_dir(self.repo, self.revision, cache) / self.name


def preset_files(preset: str) -> list[ModelFile]:
    info = MODEL_PRESETS[preset]
    files = []
    for kind in ("hf", "tokenizer"):
        for name, (size, digest) in info[f"{kind}_files"].items():
            files.append(ModelFile(info[f"{kind}_repo"], info[f"{kind}_revision"], name, size, digest))
    return files


def sha256_of(path: Path, on_bytes: Callable[[int], None] | None = None) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while block := f.read(HASH_CHUNK):
            digest.update(block)
            if on_bytes:
                on_bytes(len(block))
    return digest.hexdigest()


def _load_stamps(stamp_file: Path | None) -> dict:
    try:
        with open(stamp_file or MODEL_STAMP_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_stamps(stamps: dict, stamp_file: Path | None) -> None:
    target = stamp_file or MODEL_STAMP_FILE
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps(stamps, indent=1), encoding="utf-8")
        os.replace(tmp, target)
    except OSError:
        pass  # 記不下來只是下次要重算雜湊


def _stamp_key(file: ModelFile, path: Path) -> str:
    return f"{file.sha256}|{os.path.normcase(str(path))}"


def _stamp_value(path: Path) -> list[int]:
    st = path.stat()
    return [st.st_size, st.st_mtime_ns]


def check_file(file: ModelFile, cache: Path | None = None, *, deep: bool = False, stamps: dict | None = None,
               on_bytes: Callable[[int], None] | None = None) -> str | None:
    """核對一個模型檔。回傳問題的說明；沒問題回傳 None。

    一定檢查：檔案存在、大小正確。雜湊（SHA-256）：deep=True 時一定重算；否則只有「這個檔案（同樣大小與修改時間）
    之前算過而且正確」的紀錄（stamps）存在時才略過，沒有紀錄就現在算——所以從來沒核對過的檔案不會被載入。
    """
    path = file.path(cache)
    try:
        size = path.stat().st_size
    except OSError:
        return f"缺少 {file.name}"
    if size != file.size:
        return f"{file.name} 不完整（大小 {size:,}，應為 {file.size:,} 位元組；可能下載中斷）"
    key = _stamp_key(file, path)
    if not deep and stamps is not None and stamps.get(key) == _stamp_value(path):
        return None
    try:
        actual = sha256_of(path, on_bytes)
    except OSError as exc:
        return f"{file.name} 讀不出來（{exc}）"
    if actual != file.sha256:
        if stamps is not None:
            stamps.pop(key, None)
        return f"{file.name} 內容不正確（檔案損壞或被更改）"
    if stamps is not None:
        stamps[key] = _stamp_value(path)
    return None


def check_preset(preset: str, cache: Path | None = None, *, deep: bool = False, stamp_file: Path | None = None,
                 on_bytes: Callable[[int], None] | None = None) -> list[str]:
    """核對這個模型需要的每一個檔案，回傳問題清單（空的＝完整、可以離線載入）。不連網。"""
    stamps = _load_stamps(stamp_file)
    before = dict(stamps)
    problems = [p for p in (check_file(f, cache, deep=deep, stamps=stamps, on_bytes=on_bytes)
                            for f in preset_files(preset)) if p]
    if stamps != before:
        _save_stamps(stamps, stamp_file)
    return problems


def quick_missing(preset: str, cache: Path | None = None) -> list[ModelFile]:
    """只看存在與大小（不算雜湊，很快）：還需要下載的檔案。介面顯示「已下載／還要下載多少」用。"""
    missing = []
    for file in preset_files(preset):
        try:
            if file.path(cache).stat().st_size != file.size:
                missing.append(file)
        except OSError:
            missing.append(file)
    return missing


def preset_cached(preset: str, cache: Path | None = None) -> bool:
    """模型的每個檔案都在、大小正確（雜湊留到載入前核對）。"""
    return not quick_missing(preset, cache)


def download_bytes(preset: str, cache: Path | None = None) -> int:
    return sum(f.size for f in quick_missing(preset, cache))


class ModelNotReady(RuntimeError):
    """離線模式下模型不能用（還沒下載、不完整或損壞）。程式不會自己連網；由使用者決定要不要下載／修復。"""

    def __init__(self, preset: str, problems: list[str], cache: Path | None = None):
        self.preset = preset
        self.problems = list(problems)
        self.download_bytes = download_bytes(preset, cache)
        label = MODEL_PRESETS[preset]["label"]
        super().__init__(
            f"AI 模型「{label}」還不能使用：{'；'.join(self.problems)}。"
            "程式處於離線模式，不會自行連網。請在程式裡同意下載模型，或到「設定」按「檢查並修復 AI 模型…」。")


def require_ready(preset: str, cache: Path | None = None, stamp_file: Path | None = None) -> None:
    """載入模型前呼叫：強制離線，並核對模型檔；有問題就丟 ModelNotReady（不連網、不重試）。"""
    set_offline(True)
    problems = check_preset(preset, cache, stamp_file=stamp_file)
    if problems:
        raise ModelNotReady(preset, problems, cache)


def prepare_for(preset: str, cache: Path | None = None, stamp_file: Path | None = None) -> bool:
    """強制離線，回傳模型是否已經完整可用。不論答案是什麼都不會打開網路。"""
    set_offline(True)
    return not check_preset(preset, cache, stamp_file=stamp_file)


# ---------------------------------------------------------------------------- 下載與修復（使用者明確要求時才呼叫）
def _remove_file(path: Path, cache: Path | None = None) -> None:
    """刪掉一個壞掉的模型檔：快取裡的檔案可能是指向 blobs 的連結，兩邊都要刪，不然重新下載時會直接沿用壞的。
    連結指到快取資料夾以外的地方時（有人把模型檔連到別處）只拆連結，不碰外面的檔案。"""
    try:
        target = path.resolve() if path.is_symlink() else None
        root = (cache or hf_cache_dir()).resolve()
        if target is not None and root not in target.parents:
            target = None
    except OSError:
        target = None
    for victim in (path, target):
        if victim is not None:
            try:
                victim.unlink()
            except OSError:
                pass


def download_preset(preset: str, cache: Path | None = None, *, stamp_file: Path | None = None,
                    on_progress: Callable[[str, int, int], None] | None = None, fetch=None) -> list[str]:
    """【只在使用者明確同意後呼叫】下載這個模型缺少的檔案（固定的 commit），下載完核對雜湊。回傳下載了哪些檔案。

    on_progress(檔名, 第幾個, 共幾個)。fetch 是測試用的替身；預設用 huggingface_hub.hf_hub_download。
    這個函式結束時（不論成功或失敗）一定回到離線模式。
    """
    wanted = [f for f in preset_files(preset)
              if check_file(f, cache, stamps=_load_stamps(stamp_file)) is not None]
    for file in wanted:  # 大小不對或內容不對的舊檔先刪掉，確保拿到的是重新下載的
        if file.path(cache).exists() or file.path(cache).is_symlink():
            _remove_file(file.path(cache), cache)
    done = []
    with _network_allowed():
        if fetch is None and wanted:
            from huggingface_hub import hf_hub_download

            def fetch(file: ModelFile) -> None:
                hf_hub_download(repo_id=file.repo, filename=file.name, revision=file.revision,
                                cache_dir=str(cache) if cache else None)
        for number, file in enumerate(wanted, 1):
            if on_progress:
                on_progress(file.name, number, len(wanted))
            fetch(file)
            done.append(file.name)
    problems = check_preset(preset, cache, deep=bool(wanted), stamp_file=stamp_file)
    if problems:
        raise ModelNotReady(preset, [f"下載後核對失敗：{p}" for p in problems], cache)
    return done


def repair_preset(preset: str, cache: Path | None = None, *, stamp_file: Path | None = None,
                  on_progress: Callable[[str, int, int], None] | None = None, fetch=None,
                  allow_download: bool = True) -> tuple[list[str], list[str]]:
    """【只在使用者明確要求修復時呼叫】重新計算每個檔案的雜湊；壞的刪掉，再重新下載。

    回傳 (發現的問題, 重新下載的檔案)。allow_download=False 時只檢查、不連網也不刪檔。
    """
    set_offline(True)
    problems = check_preset(preset, cache, deep=True, stamp_file=stamp_file)
    if not problems or not allow_download:
        return problems, []
    return problems, download_preset(preset, cache, stamp_file=stamp_file, on_progress=on_progress, fetch=fetch)
