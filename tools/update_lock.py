"""重新產生 requirements.lock（含所有間接相依套件與每個檔案的 SHA-256）。需要 uv（pip install uv）。

    python tools/update_lock.py

鎖定的對象是「Windows、64 位元、Python 3.12」——也就是安裝程式內建的 Python。torch／torchvision 不寫進去
（它們依顯示卡從 PyTorch 的下載站安裝，版本與雜湊在 media_sorter/installer.py 的 TORCH_PINS），
但它們需要的其他套件（sympy、networkx⋯）會在裡面。
"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HEADER = """# 自動產生，請勿手改：python tools/update_lock.py
# 對象：Windows x64、Python 3.12（安裝程式內建的 Python）。安裝時用 pip --require-hashes --no-deps，
# 任何一個檔案的內容跟這裡記的 SHA-256 不同就會拒絕安裝。
"""


def main() -> int:
    target = ROOT / "requirements.lock"
    subprocess.run(["uv", "pip", "compile", str(ROOT / "requirements.txt"), "--generate-hashes",
                    "--python-platform", "windows", "--python-version", "3.12", "--no-header", "--no-annotate",
                    "--no-emit-package", "torch", "--no-emit-package", "torchvision", "-o", str(target), "-q"],
                   check=True, cwd=ROOT)
    body = target.read_text(encoding="utf-8")
    target.write_text(HEADER + body, encoding="utf-8", newline="\n")
    print(f"已更新 {target}（{body.count('==')} 個套件）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
