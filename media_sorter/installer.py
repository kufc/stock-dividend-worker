"""安裝程式（由 install.bat 以虛擬環境中的 Python 執行，只用標準函式庫）。

1. 用 nvidia-smi 偵測 NVIDIA 顯示卡，選擇對應的 PyTorch CUDA 版本
2. 安裝 PyTorch 與 requirements.txt 中的套件
3. 預先下載 AI 模型並做一次測試
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TORCH_INDEX = "https://download.pytorch.org/whl/{}"
# RTX 50 系列（Blackwell）需要 CUDA 12.8 以上；GTX 10 系列等較舊顯卡（運算能力 < 7.0）用 CUDA 12.6 版本。
CUDA_NEW = "cu128"
CUDA_OLD = "cu126"


def detect_nvidia() -> list[tuple[str, float, float]]:
    """回傳 [(顯示卡名稱, 運算能力, 記憶體 GB)]；沒有 NVIDIA 顯示卡則回傳空清單。"""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return []
    try:
        out = subprocess.run(
            [exe, "--query-gpu=name,compute_cap,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=30, check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return parse_nvidia_smi(out)


def parse_nvidia_smi(output: str) -> list[tuple[str, float, float]]:
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


def choose_torch_index(gpus: list[tuple[str, float, float]]) -> str | None:
    if not gpus:
        return None
    best_cc = max(cc for _, cc, _ in gpus)
    if 0 < best_cc < 7.0:
        return TORCH_INDEX.format(CUDA_OLD)
    return TORCH_INDEX.format(CUDA_NEW)


def pip(*args: str) -> bool:
    cmd = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", *args]
    print(">", " ".join(cmd), flush=True)
    return subprocess.call(cmd) == 0


def main() -> int:
    print("=" * 60)
    print(" AI 媒體分類器 - 安裝")
    print("=" * 60)
    print(f"Python {sys.version.split()[0]}  ({sys.executable})")
    if sys.version_info < (3, 10):
        print("需要 Python 3.10 以上版本，請到 https://www.python.org/downloads/ 安裝新版。")
        return 1

    gpus = detect_nvidia()
    index = choose_torch_index(gpus)
    if gpus:
        for name, cc, mem in gpus:
            print(f"偵測到 NVIDIA 顯示卡：{name}（運算能力 {cc}，{mem:.1f} GB）")
    else:
        print("沒有偵測到 NVIDIA 顯示卡（或驅動程式未安裝），將安裝 CPU 版本。")

    print("\n[1/3] 安裝 PyTorch（檔案較大，CUDA 版本約 2~3 GB，請耐心等候）⋯", flush=True)
    ok = pip("--upgrade", "torch", "torchvision", *(["--index-url", index] if index else []))
    if not ok and index:
        print("\n⚠ CUDA 版 PyTorch 安裝失敗，改裝 CPU 版本（之後仍可重新執行 install.bat）。")
        ok = pip("--upgrade", "torch", "torchvision")
    if not ok:
        print("\n✘ PyTorch 安裝失敗。若你的 Python 版本太新，請改裝 Python 3.12 後再試一次。")
        return 1

    print("\n[2/3] 安裝其他套件⋯", flush=True)
    if not pip("-r", str(ROOT / "requirements.txt")):
        print("\n✘ 套件安裝失敗，請檢查網路連線後重新執行 install.bat。")
        return 1

    print("\n[3/3] 下載 AI 模型並測試⋯", flush=True)
    code = subprocess.call([sys.executable, "-m", "media_sorter", "--download-model", "auto"], cwd=ROOT)
    if code != 0:
        print("\n⚠ 模型下載或測試失敗。程式仍可開啟，第一次辨識時會再嘗試下載。")

    check = (
        "import torch; ok = torch.cuda.is_available(); "
        "print('✔ 已啟用顯示卡加速：' + torch.cuda.get_device_name(0) if ok else "
        "'⚠ 目前使用 CPU 運算（較慢）。若有 NVIDIA 顯示卡，請更新顯示卡驅動程式後重新執行 install.bat。')"
    )
    subprocess.call([sys.executable, "-c", check])
    print("\n安裝完成！之後雙擊 start.bat 即可開啟程式。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
