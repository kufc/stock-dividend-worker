"""程式進入點：python -m media_sorter

  python -m media_sorter                    開啟視窗程式
  python -m media_sorter --download-model   預先下載 AI 模型並測試顯示卡（install.bat 會自動執行）
"""

from __future__ import annotations

import argparse
import sys
import traceback
from datetime import datetime


def _report_startup_error(text: str) -> None:
    from .config import LOG_DIR

    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_DIR / "error.log", "a", encoding="utf-8") as f:
            f.write(f"\n[{datetime.now():%Y-%m-%d %H:%M:%S}] 啟動失敗\n{text}\n")
    except OSError:
        pass
    try:  # 用 pythonw 啟動時看不到主控台，所以跳出視窗告知
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("AI 媒體分類器", "程式無法啟動，請重新執行 install.bat。\n\n" + text[-1500:])
        root.destroy()
    except Exception:  # noqa: BLE001
        print(text, file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="media_sorter", description="AI 媒體分類器")
    parser.add_argument("--download-model", nargs="?", const="auto", metavar="MODEL",
                        help="預先下載模型並測試（auto / standard / accurate）")
    args = parser.parse_args(argv)

    if args.download_model:
        from .classifier import download_model

        download_model(args.download_model)
        return 0

    try:
        from .app import main as run_app

        run_app()
    except Exception:  # noqa: BLE001
        _report_startup_error(traceback.format_exc())
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
