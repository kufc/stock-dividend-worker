"""在「安裝好的」內建 Python 裡實際開啟視窗程式，跑一遍：辨識（假模型）→ 確認 → 預覽 → 複製。
由 test_in_wine.sh 在 Wine 裡執行（需要先把 pillow、numpy、av、send2trash 的 Windows wheel 解到 site-packages）。
"""

import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.getcwd())  # 工作資料夾＝安裝的 app 資料夾（捷徑用 -m 啟動時，Python 也是這樣找到程式）
sys.path.insert(0, os.environ["MEDIA_SORTER_TESTS"])  # 測試用的假模型與小工具（Z:\ 是 Wine 裡的 Linux 根目錄）

import tkinter as tk  # noqa: E402

from fakes import FakeClassifier  # noqa: E402
from helpers import make_image  # noqa: E402

from media_sorter import app as app_module  # noqa: E402
from media_sorter import config  # noqa: E402
from media_sorter.config import Category  # noqa: E402

assert config.INSTALLED, "應該是安裝版的資料位置"
src = Path(tempfile.mkdtemp()) / "照片"
for i in range(6):
    make_image(src / f"IMG_{i}.jpg", (250, 10, 10) if i % 2 else (10, 250, 10))
root = tk.Tk()
app = app_module.App(root, classifier_factory=FakeClassifier, model_status_func=lambda c: None)
app.set_categories([Category("紅"), Category("綠"), Category("藍")])


def pump(cond, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        root.update()
        if cond():
            return
        time.sleep(0.02)
    raise SystemExit("逾時")


app.folder_var.set(str(src))
pump(lambda: app.stats is not None)
print("images found:", app.stats.images)
app.start_analysis()
pump(lambda: app.items and all(i.analyzed for i in app.items) and not app.analysis_running())
print("step:", app.step, "analyzed:", len(app.items))
for _ in range(6):
    app.confirm_current()
assert app.show_step(3)
app.start_copy()
pump(lambda: not app.organizing and app.step == 4)
out = src / "已分類"
copies = sorted(p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file())
print("copies:", len(copies))
assert len(copies) == 6 and all((src / f"IMG_{i}.jpg").exists() for i in range(6)), "原檔要還在"
print("logs:", len(list(config.LOG_DIR.glob("*.csv"))))
print("SMOKE-OK")
app.shutdown()
root.destroy()
