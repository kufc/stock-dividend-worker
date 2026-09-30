"""安裝程式（由 install.bat 以虛擬環境中的 Python 執行，只用標準函式庫）。

1. 用 nvidia-smi 偵測 NVIDIA 顯示卡，選擇對應的 PyTorch CUDA 版本（已裝對的版本就不重裝）
2. 安裝 requirements.txt 中的套件（鎖定剛裝好的 PyTorch，避免被換成 CPU 版）
3. 檢查視窗元件（tkinter），寫入「安裝完成」標記
4. 預先下載 AI 模型並做一次測試（失敗也沒關係，第一次辨識時會再下載）
"""

from __future__ import annotations

import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INSTALL_MARKER = ROOT / ".venv" / "install-ok.txt"
TORCH_INDEX = "https://download.pytorch.org/whl/{}"
# RTX 50 系列（Blackwell）需要 CUDA 12.8 以上；運算能力 7.5 以下（GTX 10 系列、TITAN V 等）用 CUDA 12.6 版本。
CUDA_NEW, CUDA_NEW_VERSION = "cu128", "12.8"
CUDA_OLD, CUDA_OLD_VERSION = "cu126", "12.6"
MIN_DRIVER_MAJOR = 528  # CUDA 12.x 需要的最低驅動程式版本


def detect_nvidia() -> tuple[list[tuple[str, float, float]], str | None]:
    """回傳 ([(顯示卡名稱, 運算能力, 記憶體 GB)], 驅動程式版本)。沒有 NVIDIA 顯示卡則回傳 ([], None)。

    舊驅動不支援 compute_cap 欄位，所以分開查詢；查不到時運算能力記為 0（代表「驅動太舊」）。
    """
    exe = shutil.which("nvidia-smi")
    if not exe:
        return [], None

    def query(fields: str) -> str | None:
        try:
            return subprocess.run([exe, f"--query-gpu={fields}", "--format=csv,noheader,nounits"],
                                  capture_output=True, text=True, timeout=30, check=True).stdout
        except (OSError, subprocess.SubprocessError):
            return None

    base = query("name,driver_version,memory.total")
    if not base:
        return [], None
    caps = (query("compute_cap") or "").split()
    gpus, driver = [], None
    for i, line in enumerate(base.strip().splitlines()):
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 3:
            continue
        driver = parts[1]
        try:
            cc = float(caps[i]) if i < len(caps) else 0.0
        except ValueError:
            cc = 0.0
        try:
            mem = float(parts[2]) / 1024
        except ValueError:
            mem = 0.0
        gpus.append((parts[0], cc, mem))
    return gpus, driver


def parse_nvidia_smi(output: str) -> list[tuple[str, float, float]]:
    """解析「name, compute_cap, memory.total」格式（保留給測試與相容用途）。"""
    gpus = []
    for line in output.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 3:
            continue
        try:
            gpus.append((parts[0], float(parts[1]), float(parts[2]) / 1024))
        except ValueError:
            gpus.append((parts[0], 0.0, 0.0))
    return gpus


def choose_torch_build(gpus: list[tuple[str, float, float]]) -> tuple[str, str] | None:
    """回傳 (CUDA 版本代號, CUDA 版本字串)；沒有 NVIDIA 顯示卡回傳 None（安裝 CPU 版）。"""
    if not gpus:
        return None
    best_cc = max(cc for _, cc, _ in gpus)
    if 0 < best_cc < 7.5:
        return CUDA_OLD, CUDA_OLD_VERSION
    return CUDA_NEW, CUDA_NEW_VERSION


def choose_torch_index(gpus: list[tuple[str, float, float]]) -> str | None:
    build = choose_torch_build(gpus)
    return TORCH_INDEX.format(build[0]) if build else None


def driver_too_old(driver: str | None) -> bool:
    try:
        return driver is not None and int(driver.split(".")[0]) < MIN_DRIVER_MAJOR
    except ValueError:
        return False


def public_version(version: str) -> str:
    """去掉版本字串的本機標籤：2.14.0+cpu → 2.14.0。

    PyPI 上的 Windows 版 PyTorch 安裝後會回報「2.14.0+cpu」，但這個標籤只存在於本機；
    若把帶標籤的版本寫進 pip 的限制檔（torch==2.14.0+cpu），pip 會找不到符合的版本而無法安裝。
    只寫公開版本（torch==2.14.0）時，依 PEP 440 會比對到已安裝的 2.14.0+cpu／2.14.0+cu128。
    """
    return version.strip().split("+", 1)[0]


def torch_constraints(torch_version: str, torchvision_version: str) -> str:
    """限制檔內容：鎖定已裝好的 PyTorch 版本，避免其他套件把它換成別的版本（例如 CPU 版）。"""
    return f"torch=={public_version(torch_version)}\ntorchvision=={public_version(torchvision_version)}\n"


def pip(*args: str) -> bool:
    cmd = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", *args]
    print(">", " ".join(cmd), flush=True)
    return subprocess.call(cmd) == 0


def run_python(code: str) -> tuple[int, str, str]:
    """執行一小段 Python，回傳 (結束碼, 標準輸出, 錯誤輸出)。"""
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, encoding="utf-8",
                          errors="replace")
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def installed_torch() -> tuple[str, str | None] | None:
    """已安裝的 (torch 版本, CUDA 版本)；沒裝或壞掉回傳 None。"""
    code, out, _ = run_python("import torch, torchvision; print(torch.__version__); print(torch.version.cuda)")
    if code != 0 or len(out.splitlines()) < 2:
        return None
    lines = out.splitlines()[-2:]
    return lines[0], (None if lines[1] == "None" else lines[1])


def app_is_running() -> bool:
    """程式開著時會鎖住 logs/app.lock；重裝 PyTorch 時 DLL 被占用會失敗，所以先檢查。"""
    from .config import acquire_app_lock

    lock = acquire_app_lock()
    if lock is None:
        return True
    lock.close()
    return False


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    print("=" * 60)
    print(" AI 媒體分類器 - 安裝")
    print("=" * 60)
    print(f"Python {sys.version.split()[0]}  ({sys.executable})")
    if sys.version_info < (3, 10) or struct.calcsize("P") != 8:
        print("需要 64 位元的 Python 3.10 以上版本（建議 3.12）。請安裝後刪除 .venv 資料夾，再重新執行 install.bat。")
        return 1
    if app_is_running():
        print("✘ AI 媒體分類器正在執行中，請先關閉程式視窗再重新安裝。")
        return 1

    gpus, driver = detect_nvidia()
    build = choose_torch_build(gpus)
    if gpus:
        for name, cc, mem in gpus:
            cc_text = f"運算能力 {cc}" if cc else "運算能力未知"
            print(f"偵測到 NVIDIA 顯示卡：{name}（{cc_text}，{mem:.1f} GB，驅動程式 {driver}）")
        if driver_too_old(driver) or not any(cc for _, cc, _ in gpus):
            print("⚠ 顯示卡驅動程式太舊，請到 NVIDIA 官網更新驅動程式後重新執行 install.bat，否則可能只能用 CPU。")
    else:
        print("沒有偵測到 NVIDIA 顯示卡（或驅動程式未安裝），將安裝 CPU 版本。")

    print("\n[1/4] 安裝 PyTorch（CUDA 版本約 2~3 GB，請耐心等候）⋯", flush=True)
    current = installed_torch()
    wanted_cuda = build[1] if build else None
    if current and current[1] == wanted_cuda:
        print(f"已安裝相符的 PyTorch {current[0]}，略過。")
    else:
        index_args = ["--index-url", TORCH_INDEX.format(build[0])] if build else []
        ok = pip("--upgrade", "--no-cache-dir", "torch", "torchvision", *index_args)
        if not ok and build:
            print("\n⚠ CUDA 版 PyTorch 安裝失敗，改裝 CPU 版本（之後仍可重新執行 install.bat）。")
            ok = pip("--upgrade", "--no-cache-dir", "torch", "torchvision")
        if not ok:
            print("\n✘ PyTorch 安裝失敗。若你的 Python 版本太新，請改裝 Python 3.12，刪除 .venv 資料夾後再試一次。")
            return 1
        current = installed_torch()
        if current is None:
            print("\n✘ PyTorch 安裝後無法載入，請刪除 .venv 資料夾後重新執行 install.bat。")
            return 1

    print("\n[2/4] 安裝其他套件⋯", flush=True)
    code, versions, _ = run_python("import torch, torchvision; print(torch.__version__); print(torchvision.__version__)")
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as constraints:
        if code == 0:  # 鎖定剛裝好的 PyTorch，避免其他套件把它換成 CPU 版
            torch_v, vision_v = versions.splitlines()[-2:]
            constraints.write(torch_constraints(torch_v, vision_v))
    try:
        if not pip("-r", str(ROOT / "requirements.txt"), "-c", constraints.name):
            print("\n✘ 套件安裝失敗。可能是網路連線中斷，也可能是套件版本衝突；")
            print("  請把上面的錯誤訊息（特別是 ERROR 開頭的幾行）截圖回報，網路問題可直接重新執行 install.bat。")
            return 1
    finally:
        Path(constraints.name).unlink(missing_ok=True)

    print("\n[3/4] 檢查視窗元件⋯", flush=True)
    code, _, out = run_python("import tkinter; tkinter.Tk().destroy()")
    if code != 0:
        print("✘ 這個 Python 缺少視窗元件（tkinter）。")
        print("  請重新執行 Python 安裝程式 → Modify → 勾選「tcl/tk and IDLE」，完成後再執行 install.bat。")
        print(out[-500:])
        return 1
    INSTALL_MARKER.write_text(f"python={sys.version.split()[0]}\ntorch={current[0]}\ncuda={current[1]}\n",
                              encoding="utf-8")

    print("\n[4/4] 下載 AI 模型並測試⋯", flush=True)
    code = subprocess.call([sys.executable, "-m", "media_sorter", "--download-model", "auto"], cwd=ROOT)
    if code != 0:
        print("\n⚠ 模型下載或測試失敗。程式仍可開啟，第一次辨識時會再嘗試下載。")

    check = (
        "import torch\n"
        "try:\n"
        "    torch.ones(1, device='cuda').add_(1); torch.cuda.synchronize()\n"
        "    print('✔ 已啟用顯示卡加速：' + torch.cuda.get_device_name(0))\n"
        "except Exception:\n"
        "    print('⚠ 目前使用 CPU 運算（較慢）。若有 NVIDIA 顯示卡，請更新顯示卡驅動程式後重新執行 install.bat。')\n"
    )
    _, out, _ = run_python(check)
    print(out.splitlines()[-1] if out else "")
    print("\n安裝完成！之後雙擊 start.bat 即可開啟程式。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
