"""程式進入點：python -m media_sorter

  python -m media_sorter                    開啟視窗程式
  python -m media_sorter --download-model   預先下載 AI 模型並測試顯示卡（install.bat 會自動執行）
"""

from __future__ import annotations

import argparse
import os
import sys
import traceback
from datetime import datetime


def _ensure_console_streams() -> None:
    """用 pythonw 啟動時沒有主控台，sys.stdout／stderr 是 None。

    很多套件（下載進度條、警告訊息）會直接寫入 stderr 而當掉，所以導向 logs/console.log。
    """
    if sys.stdout is not None and sys.stderr is not None:
        return
    from .config import LOG_DIR

    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        stream = open(LOG_DIR / "console.log", "a", encoding="utf-8", errors="replace", buffering=1)
    except OSError:
        stream = open(os.devnull, "w", encoding="utf-8")
    if sys.stdout is None:
        sys.stdout = stream
    if sys.stderr is None:
        sys.stderr = stream
    # 視窗程式看不到進度條，乾脆關掉（必須在 huggingface_hub 第一次被載入前設定）
    os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")


def _show_error_box(text: str) -> None:
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("AI 媒體分類器", text)
        root.destroy()
        return
    except Exception:  # noqa: BLE001 - 沒有 tkinter 時改用 Windows 原生對話框
        pass
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(None, text, "AI 媒體分類器", 0x10)
            return
        except (AttributeError, OSError):
            pass
    print(text, file=sys.stderr)


def _report_startup_error(text: str) -> None:
    from .config import LOG_DIR

    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_DIR / "error.log", "a", encoding="utf-8") as f:
            f.write(f"\n[{datetime.now():%Y-%m-%d %H:%M:%S}] 啟動失敗\n{text}\n")
    except OSError:
        pass
    from .config import INSTALLED

    fix = "請到開始功能表執行「修復」，或重新執行安裝程式" if INSTALLED else "請重新執行 install.bat"
    _show_error_box(f"程式無法啟動，{fix}。\n\n" + text[-1500:])


def main(argv: list[str] | None = None) -> int:
    _ensure_console_streams()
    parser = argparse.ArgumentParser(prog="media_sorter", description="AI 媒體分類器")
    parser.add_argument("--download-model", nargs="?", const="auto", metavar="MODEL",
                        help="預先下載模型並測試（auto / standard / accurate）")
    args = parser.parse_args(argv)

    if args.download_model:
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
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
