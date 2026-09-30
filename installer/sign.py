"""為 Windows 執行檔加上數位簽章（建置安裝程式時由 NSIS 的 !finalize／!uninstfinalize 呼叫）。

用法：python sign.py 檔案.exe
設定都放在環境變數（密碼不會出現在指令列或建置紀錄裡）：
  MEDIA_SORTER_SIGN_PFX        程式碼簽章憑證（.pfx／.p12）
  MEDIA_SORTER_SIGN_PASSWORD   憑證密碼（可省略）
  MEDIA_SORTER_SIGN_TIMESTAMP  時間戳記伺服器（預設 http://timestamp.digicert.com；設成空字串則不加時間戳記）
  MEDIA_SORTER_SIGN_NAME       簽章裡顯示的程式名稱（預設「AI 媒體分類器」）
Windows 上使用 signtool（Windows SDK），其他系統使用 osslsigncode。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

DEFAULT_TIMESTAMP = "http://timestamp.digicert.com"


def build_command(target: Path, env: dict) -> tuple[list[str], Path | None]:
    """回傳 (簽章指令, 暫存輸出檔)；osslsigncode 不能原地簽，所以先輸出到暫存檔再換回去。"""
    pfx = env.get("MEDIA_SORTER_SIGN_PFX", "")
    if not pfx or not Path(pfx).is_file():
        raise SystemExit(f"找不到簽章憑證：MEDIA_SORTER_SIGN_PFX={pfx!r}")
    password = env.get("MEDIA_SORTER_SIGN_PASSWORD", "")
    timestamp = env.get("MEDIA_SORTER_SIGN_TIMESTAMP", DEFAULT_TIMESTAMP)
    name = env.get("MEDIA_SORTER_SIGN_NAME", "AI 媒體分類器")
    signtool = shutil.which("signtool") if os.name == "nt" else None
    if signtool:
        cmd = [signtool, "sign", "/fd", "SHA256", "/f", pfx, "/d", name]
        if password:
            cmd += ["/p", password]
        if timestamp:
            cmd += ["/tr", timestamp, "/td", "SHA256"]
        return cmd + [str(target)], None
    tool = shutil.which("osslsigncode")
    if not tool:
        raise SystemExit("需要 signtool（Windows SDK）或 osslsigncode（apt install osslsigncode）才能簽章")
    out = target.with_name(target.name + ".signed")
    cmd = [tool, "sign", "-pkcs12", pfx, "-h", "sha256", "-n", name]
    if password:
        cmd += ["-pass", password]
    if timestamp:
        cmd += ["-ts", timestamp]
    return cmd + ["-in", str(target), "-out", str(out)], out


def sign(target: Path, env: dict | None = None) -> None:
    env = dict(os.environ if env is None else env)
    cmd, out = build_command(target, env)
    result = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    if result.returncode != 0:
        secret = env.get("MEDIA_SORTER_SIGN_PASSWORD")
        detail = (result.stdout + result.stderr).replace(secret, "***") if secret else result.stdout + result.stderr
        raise SystemExit(f"簽章失敗：{target.name}\n{detail[-2000:]}")
    if out is not None:
        os.replace(out, target)
    print(f"已簽章：{target.name}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    sign(Path(sys.argv[1]))
