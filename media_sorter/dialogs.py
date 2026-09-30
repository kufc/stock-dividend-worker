"""對話框：確認視窗、整理紀錄、圖片放大檢視、分類設定、設定。"""

from __future__ import annotations

import os
import subprocess
import sys
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, simpledialog, ttk
from typing import Callable

from PIL import Image, ImageTk

from . import APP_NAME
from .config import DEFAULT_CATEGORIES, MODEL_CHOICES, Category
from .organizer import LogInfo, folder_key, list_logs

LOG_KIND_TEXT = {
    "active": "可處理",
    "undone": "複本已移除",
    "finished": "原檔已移除",
    "unreadable": "無法讀取",
}


def open_in_file_manager(path: Path) -> None:
    if sys.platform == "win32":
        os.startfile(path)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def ui_scale(widget: tk.Misc) -> float:
    """目前的 Windows 顯示縮放比例（100% = 1.0）。"""
    return float(widget.tk.call("tk", "scaling")) / (96 / 72)


def fit_to_screen(window: tk.Toplevel, width: int, height: int, min_width: int = 480, min_height: int = 360) -> None:
    """視窗大小（以 100% 縮放的像素計，會依縮放放大）不超過螢幕（留一點邊界），並放在父視窗中央。"""
    window.update_idletasks()
    scale = ui_scale(window)
    width, height, min_width, min_height = (round(n * scale) for n in (width, height, min_width, min_height))
    sw, sh = window.winfo_screenwidth(), window.winfo_screenheight()
    w, h = min(width, sw - 60), min(height, sh - 100)
    parent = window.master
    x = parent.winfo_rootx() + max(0, (parent.winfo_width() - w) // 2)
    y = parent.winfo_rooty() + max(0, (parent.winfo_height() - h) // 3)
    x, y = max(0, min(x, sw - w - 10)), max(0, min(y, sh - h - 50))
    window.geometry(f"{w}x{h}+{x}+{y}")
    window.minsize(min(min_width, w), min(min_height, h))


# ---------------------------------------------------------------------------- 確認視窗
def confirm_with_list(master, title: str, message: str, lines: list[str], ok_text: str, *,
                      warning: str | None = None, danger: bool = False, list_title: str | None = None) -> bool:
    """確認視窗：說明、（有需要時的）橘色警告、可搜尋可捲動的完整清單。回傳 True 才代表使用者按了確定。

    預設焦點在「取消」：按 Enter 或空白鍵不會直接執行有風險的操作。
    """
    dialog = tk.Toplevel(master)
    dialog.title(title)
    dialog.transient(master)
    result = {"ok": False}
    body = ttk.Frame(dialog, padding=14)
    body.pack(fill="both", expand=True)

    buttons = ttk.Frame(body)
    buttons.pack(side="bottom", fill="x", pady=(10, 0))
    cancel = ttk.Button(buttons, text="取消", command=lambda: choose(False))
    cancel.pack(side="right")
    ok = ttk.Button(buttons, text=ok_text, style="Danger.TButton" if danger else "Accent.TButton",
                    command=lambda: choose(True))
    ok.pack(side="right", padx=8)

    label = ttk.Label(body, text=message, justify="left", wraplength=640)
    label.pack(anchor="w", fill="x")
    if warning:
        box = ttk.Frame(body, style="Warn.TFrame", padding=8)
        box.pack(fill="x", pady=(8, 0))
        warn_label = ttk.Label(box, text=warning, style="Warn.TLabel", justify="left", wraplength=620)
        warn_label.pack(anchor="w", fill="x")
        box.bind("<Configure>", lambda e: warn_label.configure(wraplength=max(200, e.width - 30)))

    search_row = ttk.Frame(body)
    search_row.pack(fill="x", pady=(10, 4))
    ttk.Label(search_row, text=list_title or "檔案清單").pack(side="left")
    count_var = tk.StringVar()
    ttk.Label(search_row, textvariable=count_var, style="Hint.TLabel").pack(side="right")
    query = tk.StringVar()
    search = ttk.Entry(search_row, textvariable=query, width=24)
    search.pack(side="right", padx=8)
    ttk.Label(search_row, text="搜尋：").pack(side="right")

    frame = ttk.Frame(body)
    frame.pack(fill="both", expand=True)
    text = tk.Text(frame, wrap="none", height=10, relief="flat", borderwidth=1, highlightthickness=1)
    scroll_y = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
    scroll_x = ttk.Scrollbar(frame, orient="horizontal", command=text.xview)
    text.configure(yscrollcommand=scroll_y.set, xscrollcommand=scroll_x.set)
    scroll_x.pack(side="bottom", fill="x")
    scroll_y.pack(side="right", fill="y")
    text.pack(side="left", fill="both", expand=True)

    def fill(*_args) -> None:
        needle = query.get().strip().casefold()
        shown = [ln for ln in lines if not needle or needle in ln.casefold()]
        text.configure(state="normal")
        text.delete("1.0", "end")
        text.insert("1.0", "\n".join(shown))
        text.configure(state="disabled")
        count_var.set(f"顯示 {len(shown)}／{len(lines)}")

    query.trace_add("write", fill)
    fill()

    def choose(value: bool) -> None:
        result["ok"] = value
        dialog.destroy()

    dialog.protocol("WM_DELETE_WINDOW", lambda: choose(False))
    dialog.bind("<Escape>", lambda e: choose(False))
    body.bind("<Configure>", lambda e: label.configure(wraplength=max(240, e.width - 30)))
    fit_to_screen(dialog, 780, 620, 520, 420)
    dialog.grab_set()
    cancel.focus_set()
    master.wait_window(dialog)
    return result["ok"]


# ---------------------------------------------------------------------------- 問題說明
def show_problem(master, what: str, todo: str = "", details: str = "", title: str = "發生問題") -> None:
    """錯誤說明：先講「發生什麼事」與「可以怎麼做」，技術細節預設收起來，需要回報時再展開。"""
    dialog = tk.Toplevel(master)
    dialog.title(title)
    dialog.transient(master)
    body = ttk.Frame(dialog, padding=16)
    body.pack(fill="both", expand=True)
    buttons = ttk.Frame(body)
    buttons.pack(side="bottom", fill="x", pady=(12, 0))
    close = ttk.Button(buttons, text="關閉", style="Accent.TButton", command=dialog.destroy)
    close.pack(side="right")
    shown = {"on": False}
    text = tk.Text(body, height=8, wrap="word", relief="flat", borderwidth=1, highlightthickness=1)
    text.insert("1.0", details)
    text.configure(state="disabled")

    def toggle() -> None:
        shown["on"] = not shown["on"]
        if shown["on"]:
            text.pack(fill="both", expand=True, pady=(8, 0))
            toggle_btn.configure(text="隱藏技術細節")
        else:
            text.pack_forget()
            toggle_btn.configure(text="顯示技術細節")
        fit_to_screen(dialog, 640, 300 + (200 if shown["on"] else 0), 420, 200)

    toggle_btn = ttk.Button(buttons, text="顯示技術細節", command=toggle)
    if details:
        toggle_btn.pack(side="left")
    label = ttk.Label(body, text=what, justify="left", wraplength=560, font=("TkDefaultFont", 11, "bold"))
    label.pack(anchor="w", fill="x")
    if todo:
        ttk.Label(body, text=todo, justify="left", wraplength=560).pack(anchor="w", fill="x", pady=(8, 0))
    body.bind("<Configure>", lambda e: [w.configure(wraplength=max(240, e.width - 40))
                                        for w in body.winfo_children() if isinstance(w, ttk.Label)])
    dialog.bind("<Escape>", lambda e: dialog.destroy())
    fit_to_screen(dialog, 640, 300, 420, 200)
    dialog.grab_set()
    close.focus_set()
    master.wait_window(dialog)


# ---------------------------------------------------------------------------- 圖片放大檢視
class ImageViewer(tk.Toplevel):
    """放大檢視：符合視窗／100%／放大／縮小。影片只顯示代表畫面（沒有播放功能，所以不放播放鈕）。"""

    ZOOM_STEP = 1.25

    def __init__(self, master, title: str, note: str = ""):
        super().__init__(master)
        self.title(title)
        self.transient(master)
        self.image: Image.Image | None = None
        self.photo = None
        self.mode = "fit"  # fit 或 fixed
        self.zoom = 1.0

        bar = ttk.Frame(self, padding=(10, 8))
        bar.pack(side="top", fill="x")
        ttk.Button(bar, text="符合視窗", command=self.fit).pack(side="left")
        ttk.Button(bar, text="100%", command=lambda: self.set_zoom(1.0)).pack(side="left", padx=6)
        ttk.Button(bar, text="＋", width=3, command=lambda: self.set_zoom(self.zoom * self.ZOOM_STEP)).pack(side="left")
        ttk.Button(bar, text="－", width=3, command=lambda: self.set_zoom(self.zoom / self.ZOOM_STEP)).pack(
            side="left", padx=6)
        self.info_var = tk.StringVar(value=note)
        ttk.Label(bar, textvariable=self.info_var, style="Hint.TLabel").pack(side="left", padx=10)
        ttk.Button(bar, text="關閉", command=self.destroy).pack(side="right")

        area = ttk.Frame(self)
        area.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(area, background="#20232a", highlightthickness=0)
        sy = ttk.Scrollbar(area, orient="vertical", command=self.canvas.yview)
        sx = ttk.Scrollbar(area, orient="horizontal", command=self.canvas.xview)
        self.canvas.configure(yscrollcommand=sy.set, xscrollcommand=sx.set)
        sx.pack(side="bottom", fill="x")
        sy.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda e: self.render() if self.mode == "fit" else None)
        self.bind("<Escape>", lambda e: self.destroy())
        self.loading_id = self.canvas.create_text(0, 0, text="讀取中⋯", fill="#d7dae0", tags="msg")
        fit_to_screen(self, 1000, 720, 480, 360)

    def set_image(self, image: Image.Image | None, error: str = "") -> None:
        self.canvas.delete("msg")
        if image is None:
            self.canvas.create_text(self.canvas.winfo_width() // 2, self.canvas.winfo_height() // 2,
                                    text=error or "無法讀取這個檔案", fill="#d7dae0", tags="msg")
            return
        self.image = image
        self.render()

    def fit(self) -> None:
        self.mode = "fit"
        self.render()

    def set_zoom(self, zoom: float) -> None:
        self.mode, self.zoom = "fixed", max(0.05, min(zoom, 8.0))
        self.render()

    def render(self) -> None:
        if self.image is None:
            return
        cw, ch = max(1, self.canvas.winfo_width()), max(1, self.canvas.winfo_height())
        w, h = self.image.size
        scale = min(cw / w, ch / h, 1.0) if self.mode == "fit" else self.zoom
        if self.mode == "fit":
            self.zoom = scale
        size = (max(1, round(w * scale)), max(1, round(h * scale)))
        shown = self.image if size == self.image.size else self.image.resize(size, Image.LANCZOS)
        self.photo = ImageTk.PhotoImage(shown)
        self.canvas.delete("img")
        x, y = max(0, (cw - size[0]) // 2), max(0, (ch - size[1]) // 2)
        self.canvas.create_image(x, y, image=self.photo, anchor="nw", tags="img")
        self.canvas.configure(scrollregion=(0, 0, max(cw, size[0]), max(ch, size[1])))
        self.info_var.set(f"{w}×{h}　{round(scale * 100)}%")


# ---------------------------------------------------------------------------- 整理紀錄
class RecordsDialog(tk.Toplevel):
    """整理紀錄：選一批，再決定要移除複本、把原檔移到資源回收筒，或開啟輸出資料夾。"""

    def __init__(self, master, log_dir: Callable[[], Path], on_undo: Callable[[Path], None],
                 on_remove: Callable[[Path], None], on_open: Callable[[str], None],
                 on_set_aside: Callable[[Path], None]):
        super().__init__(master)
        self.title("整理紀錄")
        self.transient(master)
        self.log_dir = log_dir
        self.on_undo, self.on_remove, self.on_open, self.on_set_aside = on_undo, on_remove, on_open, on_set_aside
        self.infos: dict[str, LogInfo] = {}

        body = ttk.Frame(self, padding=12)
        body.pack(fill="both", expand=True)
        buttons = ttk.Frame(body)
        buttons.pack(side="bottom", fill="x", pady=(10, 0))
        ttk.Button(buttons, text="關閉", command=self.destroy).pack(side="right")
        ttk.Button(buttons, text="重新整理", command=self.refresh).pack(side="right", padx=8)

        # 危險操作放在最下面一列、靠右，和一般操作分開；預設不會取得焦點
        danger = ttk.Frame(body)
        danger.pack(side="bottom", fill="x", pady=(10, 0))
        self.remove_btn = ttk.Button(danger, text="將這批原檔移到回收筒…", style="Danger.TButton",
                                     command=lambda: self._act(self.on_remove))
        self.remove_btn.pack(side="right")
        self.undo_btn = ttk.Button(danger, text="移除這次建立的複本…", command=lambda: self._act(self.on_undo))
        self.undo_btn.pack(side="right", padx=8)

        actions = ttk.Frame(body)
        actions.pack(side="bottom", fill="x", pady=(10, 0))
        self.open_btn = ttk.Button(actions, text="開啟輸出資料夾", command=self._open)
        self.open_btn.pack(side="left")
        self.aside_btn = ttk.Button(actions, text="移到旁邊（無法讀取的紀錄）", command=self._set_aside)
        self.aside_btn.pack(side="left", padx=8)
        note = ttk.Label(body, style="Hint.TLabel", justify="left",
                         text="選一批整理紀錄再操作。「移除這次建立的複本」只會處理整理時建立的複本，原檔不動；"
                              "「將這批原檔移到回收筒」會把原檔送到 Windows 資源回收筒（需要複本仍然存在）。")
        note.pack(side="bottom", fill="x", anchor="w", pady=(8, 0))
        note.bind("<Configure>", lambda e: note.configure(wraplength=max(200, e.width - 10)))

        frame = ttk.Frame(body)
        frame.pack(fill="both", expand=True)
        columns = ("when", "source", "output", "count", "state")
        self.tree = ttk.Treeview(frame, columns=columns, show="headings", selectmode="browse")
        scale = ui_scale(self)
        for key, title, width in (("when", "時間", 150), ("source", "來源資料夾", 230), ("output", "輸出資料夾", 230),
                                  ("count", "檔案數", 80), ("state", "狀態", 130)):
            self.tree.heading(key, text=title)
            self.tree.column(key, width=round(width * scale), minwidth=round(60 * scale),
                             stretch=key in ("source", "output"),
                             anchor="center" if key in ("count", "state", "when") else "w")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._update_buttons())
        self.empty_var = tk.StringVar()
        self.refresh()
        fit_to_screen(self, 960, 540, 700, 420)

    def refresh(self) -> None:
        selected = self.tree.selection()
        self.tree.delete(*self.tree.get_children())
        self.infos = {}
        for info in list_logs(self.log_dir()):
            iid = str(info.path)
            self.infos[iid] = info
            state = LOG_KIND_TEXT[info.kind] + (f"（{info.problem}）" if info.problem and info.kind != "unreadable" else "")
            self.tree.insert("", "end", iid=iid, values=(info.when.strftime("%Y-%m-%d %H:%M"), info.source or "—",
                                                         info.output or "—", info.count, state))
        if selected and self.tree.exists(selected[0]):
            self.tree.selection_set(selected[0])
        elif self.tree.get_children():
            self.tree.selection_set(self.tree.get_children()[0])
        self._update_buttons()

    def _current(self) -> LogInfo | None:
        selection = self.tree.selection()
        return self.infos.get(selection[0]) if selection else None

    def _update_buttons(self) -> None:
        info = self._current()
        active = info is not None and info.kind == "active" and info.count > 0
        self.undo_btn.state(["!disabled"] if active else ["disabled"])
        self.remove_btn.state(["!disabled"] if active else ["disabled"])
        self.open_btn.state(["!disabled"] if info is not None and info.output else ["disabled"])
        self.aside_btn.state(["!disabled"] if info is not None and info.kind in ("unreadable", "active") else ["disabled"])

    def _act(self, callback: Callable[[Path], None]) -> None:
        info = self._current()
        if info is not None:
            callback(info.path)

    def _open(self) -> None:
        info = self._current()
        if info is not None and info.output:
            self.on_open(info.output)

    def _set_aside(self) -> None:
        info = self._current()
        if info is not None:
            self.on_set_aside(info.path)
            self.refresh()


# ---------------------------------------------------------------------------- 分類設定
class CategoryDialog(tk.Toplevel):
    """編輯分類清單：名稱＋給 AI 的描述（每行一個）。"""

    def __init__(self, master, categories: list[Category], on_save):
        super().__init__(master)
        self.title("分類設定")
        self.transient(master)
        self.on_save = on_save
        self.work = [Category(c.name, list(c.prompts)) for c in categories]
        self.origins: list[str | None] = [c.name for c in categories]  # 每一列原本的名稱（用來辨認「改名」）
        self.index: int | None = None

        bottom = ttk.Frame(self, padding=8)
        bottom.pack(side="bottom", fill="x")
        ttk.Button(bottom, text="還原預設分類", command=self.reset).pack(side="left")
        ttk.Button(bottom, text="取消", command=self.destroy).pack(side="right")
        ttk.Button(bottom, text="儲存", style="Accent.TButton", command=self.save).pack(side="right", padx=6)

        body = ttk.Frame(self, padding=8)
        body.pack(fill="both", expand=True)
        left = ttk.Frame(body)
        left.pack(side="left", fill="y")
        buttons = ttk.Frame(left)
        buttons.pack(side="bottom", fill="x", pady=4)
        ttk.Button(buttons, text="新增", width=6, command=self.add).pack(side="left")
        ttk.Button(buttons, text="刪除", width=6, command=self.remove).pack(side="left", padx=2)
        ttk.Button(buttons, text="↑", width=3, command=lambda: self.move(-1)).pack(side="left")
        ttk.Button(buttons, text="↓", width=3, command=lambda: self.move(1)).pack(side="left")
        self.listbox = tk.Listbox(left, width=18, height=12, exportselection=False)
        self.listbox.pack(side="top", fill="both", expand=True)
        self.listbox.bind("<<ListboxSelect>>", self.on_pick)

        right = ttk.Frame(body, padding=(12, 0, 0, 0))
        right.pack(side="left", fill="both", expand=True)
        ttk.Label(right, text="分類名稱（也會是資料夾名稱）：").pack(anchor="w")
        self.name_var = tk.StringVar()
        ttk.Entry(right, textvariable=self.name_var).pack(fill="x", pady=(0, 8))
        ttk.Label(right, text="給 AI 的描述（每行一個，中文或英文皆可；越具體越準，可留白）：").pack(anchor="w")
        hint = ttk.Label(
            right, style="Hint.TLabel", wraplength=440, justify="left",
            text="小技巧：英文描述通常最準，例如「a photo of a cat」。想區分的東西越相近（如貓 vs 老虎），描述要寫得越具體。",
        )
        hint.pack(side="bottom", anchor="w", pady=4)
        right.bind("<Configure>", lambda e: hint.configure(wraplength=max(200, e.width - 20)))
        self.prompts = tk.Text(right, height=8, wrap="word")
        self.prompts.pack(fill="both", expand=True)

        self.reload_list(0)
        fit_to_screen(self, 760, 500, 560, 380)
        self.grab_set()

    def reload_list(self, select: int | None) -> None:
        self.listbox.delete(0, "end")
        for c in self.work:
            self.listbox.insert("end", c.name)
        self.index = None
        if self.work and select is not None:
            select = max(0, min(select, len(self.work) - 1))
            self.listbox.selection_set(select)
            self.load(select)
        else:
            self.name_var.set("")
            self.prompts.delete("1.0", "end")

    def load(self, index: int) -> None:
        self.index = index
        self.name_var.set(self.work[index].name)
        self.prompts.delete("1.0", "end")
        self.prompts.insert("1.0", "\n".join(self.work[index].prompts))

    def commit(self) -> None:
        if self.index is None or self.index >= len(self.work):
            return
        name = self.name_var.get().strip() or self.work[self.index].name
        prompts = [line.strip() for line in self.prompts.get("1.0", "end").splitlines() if line.strip()]
        self.work[self.index] = Category(name, prompts)
        self.listbox.delete(self.index)
        self.listbox.insert(self.index, name)

    def on_pick(self, _event=None) -> None:
        selection = self.listbox.curselection()
        if not selection or selection[0] == self.index:
            return
        self.commit()
        self.load(selection[0])

    def add(self) -> None:
        self.commit()
        name = simpledialog.askstring("新增分類", "分類名稱：", parent=self)
        if name and name.strip():
            self.work.append(Category(name.strip(), []))
            self.origins.append(None)
            self.reload_list(len(self.work) - 1)

    def remove(self) -> None:
        if self.index is None:
            return
        del self.work[self.index]
        del self.origins[self.index]
        self.reload_list(self.index)

    def move(self, delta: int) -> None:
        if self.index is None:
            return
        self.commit()
        j = self.index + delta
        if 0 <= j < len(self.work):
            self.work[self.index], self.work[j] = self.work[j], self.work[self.index]
            self.origins[self.index], self.origins[j] = self.origins[j], self.origins[self.index]
            self.reload_list(j)

    def reset(self) -> None:
        if messagebox.askyesno("分類設定", "要還原成預設分類嗎？目前的自訂分類會被取代。", parent=self):
            self.work = [Category(c.name, list(c.prompts)) for c in DEFAULT_CATEGORIES]
            self.origins = [None] * len(self.work)
            self.reload_list(0)

    def save(self) -> None:
        self.commit()
        names = [c.name for c in self.work]
        duplicates = sorted({n for n in names if names.count(n) > 1})
        if duplicates:
            messagebox.showwarning("分類設定", f"分類名稱重複：{'、'.join(duplicates)}", parent=self)
            return
        if not self.work:
            messagebox.showwarning("分類設定", "至少需要一個分類。", parent=self)
            return
        by_folder: dict[str, list[str]] = {}
        for n in names:
            by_folder.setdefault(folder_key(n), []).append(n)
        clashes = [group for group in by_folder.values() if len(group) > 1]
        if clashes:
            pairs = "；".join("、".join(group) for group in clashes)
            messagebox.showwarning("分類設定", f"這些分類會用到同一個資料夾（只差大小寫或特殊符號），請改名：\n{pairs}",
                                   parent=self)
            return
        renames = {old: cat.name for old, cat in zip(self.origins, self.work, strict=True) if old and old != cat.name}
        self.destroy()
        self.on_save(self.work, renames)


# ---------------------------------------------------------------------------- 設定
class SettingsDialog(tk.Toplevel):
    def __init__(self, master, settings: dict, on_save):
        super().__init__(master)
        self.title("設定")
        self.transient(master)
        self.settings = dict(settings)
        self.on_save = on_save
        buttons = ttk.Frame(self, padding=(12, 0, 12, 12))
        buttons.pack(side="bottom", fill="x")
        ttk.Button(buttons, text="取消", command=self.destroy).pack(side="right")
        ttk.Button(buttons, text="儲存", style="Accent.TButton", command=self.save).pack(side="right", padx=8)
        body = ttk.Frame(self, padding=12)
        body.pack(fill="both", expand=True)

        labels = list(MODEL_CHOICES.values())
        keys = list(MODEL_CHOICES.keys())
        self.model_var = tk.StringVar(value=MODEL_CHOICES.get(self.settings["model"], labels[0]))
        self.frames_var = tk.IntVar(value=int(self.settings["video_frames"]))
        self.threshold_var = tk.IntVar(value=round(float(self.settings["confidence_threshold"]) * 100))
        self.batch_var = tk.IntVar(value=int(self.settings["batch_size"]))
        self.keys = dict(zip(labels, keys, strict=True))

        rows = [
            ("AI 模型：", ttk.Combobox(body, textvariable=self.model_var, values=labels, state="readonly", width=46)),
            ("影片取樣畫面數：", ttk.Spinbox(body, from_=1, to=32, textvariable=self.frames_var, width=8)),
            ("需要確認的門檻（%）：", ttk.Spinbox(body, from_=5, to=95, increment=5, textvariable=self.threshold_var,
                                       width=8)),
            ("每批處理張數：", ttk.Spinbox(body, from_=1, to=256, textvariable=self.batch_var, width=8)),
        ]
        hints = [
            "有 NVIDIA 顯示卡時會自動使用 CUDA 加速",
            "每支影片平均擷取幾張畫面來判斷，越多越準但越慢",
            "模型分數低於此值的項目會標成「需要確認」（這個分數不等於判斷正確的機率）",
            "顯示卡記憶體不足時請調低（例如 8）",
        ]
        for r, ((label, widget), hint) in enumerate(zip(rows, hints, strict=True)):
            ttk.Label(body, text=label).grid(row=r * 2, column=0, sticky="w", pady=(6, 0))
            widget.grid(row=r * 2, column=1, sticky="w", pady=(6, 0))
            ttk.Label(body, text=hint, style="Hint.TLabel", wraplength=round(460 * ui_scale(self)), justify="left").grid(
                row=r * 2 + 1, column=1, sticky="w")
        fit_to_screen(self, 720, 420, 560, 360)
        self.grab_set()

    def save(self) -> None:
        try:
            frames = max(1, min(32, int(self.frames_var.get())))
            threshold = max(5, min(95, int(self.threshold_var.get()))) / 100
            batch = max(1, min(256, int(self.batch_var.get())))
        except (tk.TclError, ValueError):
            messagebox.showwarning("設定", "請輸入有效的數字。", parent=self)
            return
        self.settings.update(
            model=self.keys.get(self.model_var.get(), "auto"),
            video_frames=frames, confidence_threshold=threshold, batch_size=batch,
        )
        self.destroy()
        self.on_save(self.settings)


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


__all__ = ["APP_NAME", "CategoryDialog", "ImageViewer", "RecordsDialog", "SettingsDialog", "confirm_with_list",
           "fit_to_screen", "open_in_file_manager", "show_problem"]
