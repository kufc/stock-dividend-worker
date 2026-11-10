"""在「安裝好的」內建 Python 裡實際開啟視窗程式，跑一遍：辨識（假模型）→ 檢查分類 → 預覽 → 複製。
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
# Windows 專屬的部分：視窗最大化後仍在螢幕內、右鍵選單建得出來、框選與拖曳的事件處理跑得動
root.update()
print("window:", root.state(), root.winfo_width(), "x", root.winfo_height(), "screen", root.winfo_screenwidth(), "x",
      root.winfo_screenheight())
assert root.state() == "zoomed", root.state()
assert root.winfo_width() <= root.winfo_screenwidth() and root.winfo_height() <= root.winfo_screenheight()
bar = app.page2.next_btn
assert bar.winfo_rooty() + bar.winfo_height() <= root.winfo_screenheight(), "「下一步」按鈕超出螢幕"
grid = app.tree
grid.selection_set(grid.get_children()[0])
menu = app.build_context_menu()
assert menu is not None and menu.index("end") >= 8
from types import SimpleNamespace  # noqa: E402

x0, y0, x1, y1 = grid._cell_box(0)
below = SimpleNamespace(x=x0 + 5, y=grid.canvas.winfo_height() - 3, x_root=0, y_root=0)
if grid._pos_at(below.x, below.y) is None:  # 空白處往上框選第一列
    grid._on_press(below)
    grid._on_motion(SimpleNamespace(x=x1 + 5, y=y0 + 5, x_root=0, y_root=0))
    grid._on_release(SimpleNamespace(x=x1 + 5, y=y0 + 5, x_root=0, y_root=0))
    root.update()
    print("band selected:", len(grid.selection()))
    assert len(grid.selection()) >= 1
print("suggestions:", len(app.suggested_categories()))  # 只印數量：Wine 的主控台印不出中文
app.select_bucket("cat:紅")
app.tree.select_all()
app.assign("綠")  # 把 AI 放在「紅」的全部改成「綠」
assert app.page2.buckets.item("cat:綠", "values")[0] == "6" and not app.tree.get_children()
assert app.show_step(3)
app.start_copy()
pump(lambda: not app.organizing and app.step == 4)
out = src / "已分類"
copies = sorted(p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file())
print("copies:", len(copies))
assert len(copies) == 6 and all(c.startswith("綠/") for c in copies), copies
assert all((src / f"IMG_{i}.jpg").exists() for i in range(6)), "原檔要還在"
print("logs:", len(list(config.LOG_DIR.glob("*.csv"))))
print("SMOKE-OK")
app.shutdown()
root.destroy()
