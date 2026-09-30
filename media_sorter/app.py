"""桌面視窗介面（Tkinter，Python 內建，不需額外安裝）。

流程：選資料夾 → 開始辨識（AI 判斷每個檔案看起來像什麼）→ 逐一或批次確認分類 → 開始整理（搬移／複製＋重新命名）。
"""

from __future__ import annotations

import gc
import os
import queue
import subprocess
import sys
import threading
import traceback
import tkinter as tk
from collections import Counter, OrderedDict
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk
from tkinter import font as tkfont

from PIL import Image, ImageTk

from . import APP_NAME, __version__
from .analysis import CONFIRMED, ERROR, PENDING, SKIPPED, Item, accept_confident, analyze, rescore
from .config import (
    DEFAULT_CATEGORIES,
    acquire_app_lock,
    LOG_DIR,
    MODEL_CHOICES,
    RENAME_PATTERN_PRESETS,
    Category,
    load_categories,
    load_settings,
    save_categories,
    save_settings,
)
from .media import file_date, load_preview, media_kind, scan_folder
from .organizer import (
    execute,
    folder_key,
    free_space_problem,
    latest_log,
    log_roots,
    plan_operations,
    read_log,
    remove_originals,
    set_aside_log,
    undo,
)
from .vocabulary import VOCABULARY

STATUS_TEXT = {PENDING: "待確認", CONFIRMED: "已確認", SKIPPED: "略過", ERROR: "無法讀取"}
KIND_TEXT = {"image": "圖片", "video": "影片"}
BASE_FILTERS = ["全部", "待確認", "低信心（建議檢查）", "已確認", "略過", "無法讀取"]
AI_FILTER_PREFIX = "AI 判斷為："
DEFAULT_OUTPUT_NAME = "已分類"
SKIP = "__skip__"
PREVIEW_W, PREVIEW_H = 480, 340
GC_INTERVAL_MS = 5000
COLUMNS = [  # (欄位, 標題, 寬度)
    ("name", "檔名", 240),
    ("kind", "類型", 50),
    ("ai", "AI 判斷", 110),
    ("conf", "信心", 60),
    ("final", "確認分類", 110),
    ("status", "狀態", 80),
]


def open_in_file_manager(path: Path) -> None:
    if sys.platform == "win32":
        os.startfile(path)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def confirm_with_list(master, title: str, message: str, lines: list[str], ok_text: str) -> bool:
    """確認視窗：上方說明，下方可捲動的完整清單（不省略），按「確定」才回傳 True。"""
    dialog = tk.Toplevel(master)
    dialog.title(title)
    dialog.transient(master)
    dialog.geometry("760x560")
    result = {"ok": False}
    body = ttk.Frame(dialog, padding=10)
    body.pack(fill="both", expand=True)
    ttk.Label(body, text=message, wraplength=720, justify="left").pack(anchor="w")
    frame = ttk.Frame(body)
    frame.pack(fill="both", expand=True, pady=8)
    text = tk.Text(frame, wrap="none", height=16)
    scroll_y = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
    scroll_x = ttk.Scrollbar(frame, orient="horizontal", command=text.xview)
    text.configure(yscrollcommand=scroll_y.set, xscrollcommand=scroll_x.set)
    scroll_y.pack(side="right", fill="y")
    scroll_x.pack(side="bottom", fill="x")
    text.pack(side="left", fill="both", expand=True)
    text.insert("1.0", "\n".join(lines))
    text.configure(state="disabled")
    buttons = ttk.Frame(body)
    buttons.pack(fill="x")

    def choose(ok: bool) -> None:
        result["ok"] = ok
        dialog.destroy()

    ttk.Button(buttons, text="取消", command=lambda: choose(False)).pack(side="right")
    ttk.Button(buttons, text=ok_text, style="Accent.TButton", command=lambda: choose(True)).pack(side="right", padx=6)
    dialog.protocol("WM_DELETE_WINDOW", lambda: choose(False))
    dialog.grab_set()
    master.wait_window(dialog)
    return result["ok"]


def _same_folder(a: str | Path, b: str | Path) -> bool:
    try:
        return os.path.normcase(str(Path(a).resolve())) == os.path.normcase(str(Path(b).resolve()))
    except (OSError, ValueError):
        return False


def _default_classifier_factory(preset: str):
    from .classifier import Classifier

    return Classifier(preset)


class App:
    def __init__(self, root: tk.Tk, classifier_factory=_default_classifier_factory):
        self.root = root
        self.classifier_factory = classifier_factory
        self.settings = load_settings()
        self.categories = load_categories()
        self.items: list[Item] = []
        self.classifier = None
        self.classifier_key = None
        self.queue: queue.Queue = queue.Queue()
        self.worker: threading.Thread | None = None
        self.stop_event = threading.Event()
        self.needs_rescore = False
        self.organizing = False  # 整理或復原進行中
        self._analysis_active = False  # 辨識中（包含背景已結束但訊息還沒處理完的時間）
        self._run_id = 0  # 每次背景工作的編號，用來丟掉上一輪殘留的訊息
        self._pos: dict[str, int] = {}  # 清單列 id → self.items 的位置
        self._counts_job = None
        self._error_dialog_open = False
        self.current: int | None = None
        self.sort_key: str | None = None
        self.sort_reverse = False
        self.preview_cache: OrderedDict[int, Image.Image] = OrderedDict()  # 以項目 uid 為鍵
        self._preview_loading: set[int] = set()
        self.preview_photo = None

        root.title(f"{APP_NAME} v{__version__}")
        root.geometry("1280x800")
        root.minsize(1000, 640)
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.report_callback_exception = self.on_tk_error
        self._setup_style()
        self._build_ui()
        self._bind_keys()
        self.refresh_filters()
        self.update_counts(force=True)
        self.show_preview(None)
        root.after(100, self._poll_queue)
        root.after(GC_INTERVAL_MS, self._collect_garbage)

    # ------------------------------------------------------------------ 介面建構
    def _setup_style(self) -> None:
        families = set(tkfont.families(self.root))
        for family in ("Microsoft JhengHei UI", "Microsoft JhengHei", "PingFang TC", "Noto Sans CJK TC",
                       "Noto Sans TC", "WenQuanYi Zen Hei"):
            if family in families:
                for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
                    tkfont.nametofont(name).configure(family=family, size=10)
                break
        default = tkfont.nametofont("TkDefaultFont")
        self.big_font = default.copy()
        self.big_font.configure(size=12, weight="bold")
        style = ttk.Style(self.root)
        if "vista" not in style.theme_names() and "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure("Treeview", rowheight=int(default.metrics("linespace") * 1.5))
        style.configure("Accent.TButton", font=self.big_font, padding=(12, 6))
        style.configure("Big.TLabel", font=self.big_font)
        style.configure("Hint.TLabel", foreground="#666666")

    def _build_ui(self) -> None:
        root = self.root
        pad = {"padx": 6, "pady": 4}

        # 第一列：來源資料夾
        top = ttk.Frame(root)
        top.pack(fill="x", **pad)
        ttk.Button(top, text="選擇資料夾…", command=self.choose_folder).pack(side="left")
        self.folder_var = tk.StringVar(value=self.settings.get("last_folder", ""))
        ttk.Entry(top, textvariable=self.folder_var).pack(side="left", fill="x", expand=True, padx=6)
        self.subfolders_var = tk.BooleanVar(value=self.settings.get("include_subfolders", True))
        ttk.Checkbutton(top, text="包含子資料夾", variable=self.subfolders_var).pack(side="left", padx=4)
        self.category_btn = ttk.Button(top, text="分類設定", command=self.open_category_dialog)
        self.category_btn.pack(side="left", padx=4)
        ttk.Button(top, text="⚙ 設定", command=self.open_settings_dialog).pack(side="left")

        # 第二列：開始辨識＋進度
        run = ttk.Frame(root)
        run.pack(fill="x", **pad)
        self.run_btn = ttk.Button(run, text="▶ 開始辨識", style="Accent.TButton", command=self.toggle_analysis)
        self.run_btn.pack(side="left")
        self.progress = ttk.Progressbar(run, mode="determinate", length=260)
        self.progress.pack(side="left", padx=10)
        self.status_var = tk.StringVar(value="請選擇資料夾後按「開始辨識」。")
        ttk.Label(run, textvariable=self.status_var).pack(side="left", fill="x", expand=True)

        # 底部：輸出設定（先放，視窗較矮時才不會被擠掉）
        self._build_output(root)

        # 中間：左邊清單、右邊預覽
        paned = ttk.PanedWindow(root, orient="horizontal")
        paned.pack(fill="both", expand=True, **pad)
        left = ttk.Frame(paned)
        right = ttk.Frame(paned, width=PREVIEW_W + 40)
        paned.add(left, weight=3)
        paned.add(right, weight=2)
        self._build_list(left)
        self._build_preview(right)

    def _build_list(self, parent: ttk.Frame) -> None:
        bar = ttk.Frame(parent)
        bar.pack(fill="x", pady=(0, 4))
        ttk.Label(bar, text="顯示：").pack(side="left")
        self.filter_var = tk.StringVar(value=BASE_FILTERS[0])
        self.filter_box = ttk.Combobox(bar, textvariable=self.filter_var, state="readonly", width=20)
        self.filter_box.pack(side="left")
        self.filter_box.bind("<<ComboboxSelected>>", lambda e: self.refresh_tree())
        self.count_var = tk.StringVar()
        ttk.Label(bar, textvariable=self.count_var, style="Hint.TLabel").pack(side="left", padx=8)

        table = ttk.Frame(parent)
        table.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(table, columns=[c[0] for c in COLUMNS], show="headings", selectmode="extended")
        for key, title, width in COLUMNS:
            self.tree.heading(key, text=title, command=lambda k=key: self.sort_by(k))
            anchor = "w" if key in ("name", "ai", "final") else "center"
            self.tree.column(key, width=width, stretch=(key == "name"), anchor=anchor)
        scroll = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="left", fill="y")
        self.tree.tag_configure(CONFIRMED, background="#e3f6e3")
        self.tree.tag_configure(SKIPPED, foreground="#999999")
        self.tree.tag_configure(ERROR, foreground="#c62828")
        self.tree.tag_configure("low", background="#fff5d6")
        self.tree.bind("<<TreeviewSelect>>", self.on_select)

        batch = ttk.Frame(parent)
        batch.pack(fill="x", pady=(4, 0))
        ttk.Label(batch, text="選取的項目 →").pack(side="left")
        self.batch_var = tk.StringVar()
        self.batch_box = ttk.Combobox(batch, textvariable=self.batch_var, state="readonly", width=14)
        self.batch_box.pack(side="left", padx=4)
        ttk.Button(batch, text="套用分類", command=self.apply_batch).pack(side="left")
        ttk.Button(batch, text="採用 AI 建議", command=self.accept_selected).pack(side="left", padx=4)
        ttk.Button(batch, text="略過", command=lambda: self.set_selected(SKIP)).pack(side="left")
        ttk.Button(batch, text="✔ 高信心的全部採用", command=self.accept_confident_all).pack(side="right")

    def _build_preview(self, parent: ttk.Frame) -> None:
        # 由下往上排：按鈕與選項固定顯示，預覽圖佔用剩下的空間（視窗小時自動縮小）
        self.tags_box = ttk.LabelFrame(parent, text="AI 還看到這些（點一下可新增為分類）：")
        self.tags_box.pack(side="bottom", fill="x", pady=(4, 0))

        self.confirm_btn = ttk.Button(parent, text="✔ 確認並下一個（Enter）　略過按 S",
                                      style="Accent.TButton", command=self.confirm_current)
        self.confirm_btn.pack(side="bottom", fill="x", pady=4)

        box = ttk.LabelFrame(parent, text="AI 覺得這看起來像：（按 1／2／3 直接選）")
        box.pack(side="bottom", fill="x", pady=(4, 0))
        self.choice_var = tk.StringVar()
        self.radios = []
        for _ in range(3):
            rb = ttk.Radiobutton(box, variable=self.choice_var)
            rb.pack(anchor="w", padx=6, pady=1)
            self.radios.append(rb)
        other = self.other_row = ttk.Frame(box)
        other.pack(fill="x", padx=6, pady=2)
        ttk.Label(other, text="都不是？改選：").pack(side="left")
        self.other_var = tk.StringVar()
        self.other_box = ttk.Combobox(other, textvariable=self.other_var, state="readonly", width=14)
        self.other_box.pack(side="left")
        self.other_box.bind("<<ComboboxSelected>>", lambda e: self.choice_var.set(self.other_var.get()))
        ttk.Button(other, text="＋ 新增分類…", command=self.add_category_prompt).pack(side="left", padx=4)
        ttk.Radiobutton(box, text="略過此檔案（不整理）", variable=self.choice_var, value=SKIP).pack(
            anchor="w", padx=6, pady=(2, 4))

        self.info_var = tk.StringVar()
        ttk.Label(parent, textvariable=self.info_var, style="Hint.TLabel", wraplength=PREVIEW_W).pack(side="bottom", anchor="w")
        self.file_var = tk.StringVar()
        ttk.Label(parent, textvariable=self.file_var, style="Big.TLabel", wraplength=PREVIEW_W).pack(side="bottom", anchor="w")

        frame = ttk.Frame(parent, width=PREVIEW_W, height=PREVIEW_H)
        frame.pack(side="top", fill="both", expand=True, pady=(0, 4))
        frame.pack_propagate(False)
        self.image_label = ttk.Label(frame, anchor="center", background="#20232a", foreground="#dddddd")
        self.image_label.pack(fill="both", expand=True)
        self._resize_job = None
        frame.bind("<Configure>", self._on_preview_resize)

    def _on_preview_resize(self, _event=None) -> None:
        if self._resize_job:
            self.root.after_cancel(self._resize_job)
        self._resize_job = self.root.after(150, self._render_preview_image)

    def _build_output(self, root: tk.Tk) -> None:
        out = ttk.LabelFrame(root, text="整理方式")
        out.pack(side="bottom", fill="x", padx=6, pady=(0, 6))
        row1 = ttk.Frame(out)
        row1.pack(fill="x", padx=6, pady=3)
        ttk.Label(row1, text="輸出資料夾：").pack(side="left")
        self.output_var = tk.StringVar(value=self.settings.get("output_dir", ""))
        ttk.Entry(row1, textvariable=self.output_var).pack(side="left", fill="x", expand=True)
        ttk.Button(row1, text="瀏覽…", command=self.choose_output).pack(side="left", padx=4)
        ttk.Label(row1, text=f"（空白 = 來源資料夾內的「{DEFAULT_OUTPUT_NAME}」）", style="Hint.TLabel").pack(side="left")

        row2 = ttk.Frame(out)
        row2.pack(fill="x", padx=6, pady=3)
        ttk.Label(row2, text="複製到分類資料夾（原檔不動）").pack(side="left")
        ttk.Separator(row2, orient="vertical").pack(side="left", fill="y", padx=8)
        self.rename_var = tk.BooleanVar(value=self.settings.get("rename", True))
        ttk.Checkbutton(row2, text="重新命名，樣式：", variable=self.rename_var).pack(side="left")
        self.pattern_var = tk.StringVar(value=self.settings.get("rename_pattern", RENAME_PATTERN_PRESETS[0]))
        ttk.Combobox(row2, textvariable=self.pattern_var, values=RENAME_PATTERN_PRESETS, width=22).pack(side="left", padx=4)
        ttk.Label(row2, text="可用：{分類} {日期} {序號} {原檔名}", style="Hint.TLabel").pack(side="left")
        self.organize_btn = ttk.Button(row2, text="開始整理 ▶", style="Accent.TButton", command=self.organize)
        self.organize_btn.pack(side="right")
        self.remove_btn = ttk.Button(row2, text="移除原檔…", command=self.remove_originals_last)
        self.remove_btn.pack(side="right", padx=6)
        self.undo_btn = ttk.Button(row2, text="↩ 復原上次整理", command=self.undo_last)
        self.undo_btn.pack(side="right")

    def _bind_keys(self) -> None:
        def guarded(func):
            def handler(event):
                if isinstance(event.widget, (tk.Entry, ttk.Entry, ttk.Combobox, tk.Text, tk.Spinbox)):
                    return None
                func(event)
                return "break"
            return handler

        self.root.bind("<Return>", guarded(lambda e: self.confirm_current()))
        for i in range(3):
            self.root.bind(str(i + 1), guarded(lambda e, i=i: self.pick_suggestion(i)))
        for key in ("s", "S"):
            self.root.bind(key, guarded(lambda e: (self.choice_var.set(SKIP), self.confirm_current())))
        self.tree.bind("<Control-a>", lambda e: (self.tree.selection_set(self.tree.get_children()), "break")[1])

    # ------------------------------------------------------------------ 項目與清單
    def _set_items(self, items: list[Item]) -> None:
        """換掉整份清單時一定要走這裡：清除選取、預覽與快取，避免指到別的檔案。"""
        self.items = items
        self._pos = {str(item.uid): i for i, item in enumerate(items)}
        self.preview_cache.clear()
        self.tree.selection_remove(self.tree.selection())
        self.show_preview(None)

    def _iid(self, index: int) -> str:
        return str(self.items[index].uid)

    def _index_of(self, iid: str) -> int | None:
        return self._pos.get(iid)

    def similarity_floor(self) -> float:
        return float(getattr(self.classifier, "similarity_floor", 0.0) or 0.0)

    def refresh_filters(self) -> None:
        names = [c.name for c in self.categories]
        self.filter_box["values"] = BASE_FILTERS + [AI_FILTER_PREFIX + n for n in names]
        if self.filter_var.get() not in self.filter_box["values"]:
            self.filter_var.set(BASE_FILTERS[0])
        self.batch_box["values"] = names
        self.other_box["values"] = names

    def _is_low(self, item: Item) -> bool:
        return item.is_low(self.categories, self.settings["confidence_threshold"], self.similarity_floor())

    def _matches(self, item: Item, flt: str) -> bool:
        if flt == "全部":
            return True
        if flt == "待確認":
            return item.status == PENDING
        if flt.startswith("低信心"):
            return self._is_low(item)
        if flt == "已確認":
            return item.status == CONFIRMED
        if flt == "略過":
            return item.status == SKIPPED
        if flt == "無法讀取":
            return item.status == ERROR
        if flt.startswith(AI_FILTER_PREFIX):
            return item.best(self.categories)[0] == flt[len(AI_FILTER_PREFIX):]
        return True

    def _row(self, item: Item) -> tuple[tuple, tuple]:
        name, conf = item.best(self.categories)
        if item.status == ERROR and not item.analyzed:
            ai, conf_text = "—", ""
        elif not item.analyzed:
            ai, conf_text = "辨識中…" if self.analysis_running() else "未辨識", ""
        else:
            ai, conf_text = name or "—", f"{conf:.0%}"
        final = item.chosen if item.status == CONFIRMED else ""
        values = (item.path.name, KIND_TEXT.get(item.kind, item.kind), ai, conf_text, final, STATUS_TEXT[item.status])
        tags = (item.status,) + (("low",) if self._is_low(item) else ())
        return values, tags

    def _sort_value(self, index: int):
        item = self.items[index]
        if self.sort_key == "conf":
            return item.best(self.categories)[1]
        values, _ = self._row(item)
        col = [c[0] for c in COLUMNS].index(self.sort_key)
        return str(values[col]).lower()

    def refresh_tree(self) -> None:
        selected = set(self.tree.selection())
        self.tree.delete(*self.tree.get_children())
        flt = self.filter_var.get()
        visible = [i for i, item in enumerate(self.items) if self._matches(item, flt)]
        if self.sort_key:
            visible.sort(key=self._sort_value, reverse=self.sort_reverse)
        for i in visible:
            values, tags = self._row(self.items[i])
            self.tree.insert("", "end", iid=self._iid(i), values=values, tags=tags)
        keep = [iid for iid in selected if self.tree.exists(iid)]
        if keep:
            self.tree.selection_set(keep)
        self.update_counts(force=True)

    def update_row(self, index: int) -> None:
        iid = self._iid(index)
        if self.tree.exists(iid):
            values, tags = self._row(self.items[index])
            self.tree.item(iid, values=values, tags=tags)

    def sort_by(self, key: str) -> None:
        if self.sort_key == key:
            self.sort_reverse = not self.sort_reverse
        else:
            self.sort_key, self.sort_reverse = key, False
        self.refresh_tree()

    def update_counts(self, force: bool = False) -> None:
        # 辨識進行中最多每 0.5 秒更新一次（大量檔案時計數本身也要時間）
        if not force and self._counts_job is not None:
            return
        if not force:
            self._counts_job = self.root.after(500, self._flush_counts)
            return
        self._flush_counts()

    def _flush_counts(self) -> None:
        self._counts_job = None
        c = Counter(item.status for item in self.items)
        low = sum(1 for item in self.items if self._is_low(item))
        self.count_var.set(
            f"共 {len(self.items)} 個｜已確認 {c[CONFIRMED]}｜待確認 {c[PENDING]}（低信心 {low}）｜"
            f"略過 {c[SKIPPED]}｜無法讀取 {c[ERROR]}"
        )

    # ------------------------------------------------------------------ 預覽與確認
    def on_select(self, _event=None) -> None:
        indices = self._selected_indices()
        self.show_preview(indices[0] if indices else None, len(indices))

    def _cache_preview(self, uid: int, img: Image.Image) -> None:
        self.preview_cache[uid] = img
        self.preview_cache.move_to_end(uid)
        while len(self.preview_cache) > 60:
            self.preview_cache.popitem(last=False)

    def _preview_image(self, index: int) -> Image.Image | None:
        """有縮圖就直接用；沒有就在背景執行緒讀檔（HEIC 等很慢的格式不會卡住視窗）。"""
        item = self.items[index]
        if item.uid in self.preview_cache:
            self.preview_cache.move_to_end(item.uid)
            return self.preview_cache[item.uid]
        if item.thumbnail:
            try:
                img = load_preview(item.path, item.kind, item.thumbnail)
            except Exception:  # noqa: BLE001
                return None
            self._cache_preview(item.uid, img)
            return img
        if item.uid not in self._preview_loading:
            self._preview_loading.add(item.uid)
            threading.Thread(target=self._preview_worker, args=(item.uid, item.path, item.kind), daemon=True).start()
        return None

    def _preview_worker(self, uid: int, path: Path, kind: str) -> None:
        try:
            img = load_preview(path, kind, None)
        except Exception:  # noqa: BLE001 - 預覽失敗只顯示文字
            img = None
        self.queue.put(("preview", uid, img))

    def _render_preview_image(self) -> None:
        self._resize_job = None
        if self.current is None or self.current >= len(self.items):
            return
        item = self.items[self.current]
        img = self._preview_image(self.current)
        if img is None:
            self.preview_photo = None
            loading = item.uid in self._preview_loading
            self.image_label.configure(image="", text="讀取預覽中⋯" if loading else "（無法預覽此檔案）")
            return
        width, height = self.image_label.winfo_width(), self.image_label.winfo_height()
        if width < 50 or height < 50:  # 視窗還沒排版完成
            width, height = PREVIEW_W, PREVIEW_H
        shown = img.copy()
        shown.thumbnail((width, height))
        self.preview_photo = ImageTk.PhotoImage(shown)
        self.image_label.configure(image=self.preview_photo, text="")

    def show_preview(self, index: int | None, selected_count: int = 1) -> None:
        self.current = index
        for child in self.tags_box.winfo_children():
            child.destroy()
        self.other_var.set("")
        if index is None:
            self.preview_photo = None
            self.image_label.configure(image="", text="（選擇左邊清單中的檔案來預覽）")
            self.file_var.set("")
            self.info_var.set("")
            for rb in self.radios:
                rb.pack_forget()
            self.choice_var.set("")
            return

        item = self.items[index]
        self._render_preview_image()

        extra = f"　（已選取 {selected_count} 個項目，確認時會一起套用）" if selected_count > 1 else ""
        self.file_var.set(item.path.name + extra)
        date = item.date.strftime("%Y-%m-%d %H:%M") if item.date else ""
        info = f"{KIND_TEXT.get(item.kind, '')}　{date}　{item.path.parent}"
        if item.error:
            info += f"\n讀取失敗：{item.error}"
        elif item.analyzed and item.top_similarity < self.similarity_floor():
            info += "\n⚠ 這個檔案跟每個分類都不太像，建議人工確認或新增分類。"
        self.info_var.set(info)

        suggestions = item.suggestions(self.categories, 3)
        for rb in self.radios:
            rb.pack_forget()
        for i, (name, prob) in enumerate(suggestions):
            bar = "█" * max(1, round(prob * 10))
            self.radios[i].configure(text=f"{i + 1}. {name}　{prob:.0%}　{bar}", value=name)
            self.radios[i].pack(anchor="w", padx=6, pady=1, before=self.other_row)

        if item.status == CONFIRMED:
            self.choice_var.set(item.chosen)
        elif item.status == SKIPPED:
            self.choice_var.set(SKIP)
        else:
            self.choice_var.set(suggestions[0][0] if suggestions else "")
        if self.choice_var.get() not in {s[0] for s in suggestions} | {SKIP, ""}:
            self.other_var.set(self.choice_var.get())

        existing = {c.name for c in self.categories}
        row = None
        for i, (name, prob) in enumerate(item.tags):
            if i % 3 == 0:
                row = ttk.Frame(self.tags_box)
                row.pack(fill="x", padx=4, pady=1)
            label = f"{name} {prob:.0%}" + ("（已有）" if name in existing else "")
            ttk.Button(row, text=label, command=lambda n=name: self.add_category_from_tag(n)).pack(side="left", padx=2)
        if not item.tags:
            ttk.Label(self.tags_box, text="（辨識後顯示）", style="Hint.TLabel").pack(anchor="w", padx=6)

    def _selected_indices(self) -> list[int]:
        indices = (self._index_of(iid) for iid in self.tree.selection())
        return [i for i in indices if i is not None]

    def set_selected(self, choice: str, indices: list[int] | None = None) -> None:
        indices = indices if indices is not None else self._selected_indices()
        for i in indices:
            item = self.items[i]
            if choice == SKIP:
                item.status, item.chosen = SKIPPED, None
            elif choice:
                item.status, item.chosen = CONFIRMED, choice
            self.update_row(i)
        self.update_counts(force=True)

    def _goto_next(self, after_iid: str) -> None:
        iids = self.tree.get_children()
        if not iids:
            return
        pos = iids.index(after_iid) + 1 if after_iid in iids else 0
        # 優先跳到下一個還沒確認的項目
        for iid in list(iids[pos:]) + list(iids[:pos]):
            index = self._index_of(iid)
            if index is not None and self.items[index].status == PENDING:
                self._select(iid)
                return
        if pos < len(iids):
            self._select(iids[pos])

    def _select(self, iid: str) -> None:
        self.tree.selection_set(iid)
        self.tree.focus(iid)
        self.tree.see(iid)

    def confirm_current(self) -> None:
        choice = self.choice_var.get()
        indices = self._selected_indices()
        if not indices or not choice:
            return
        iids_before = self.tree.get_children()
        selected_iids = [self._iid(i) for i in indices]
        last = max(selected_iids, key=lambda iid: iids_before.index(iid) if iid in iids_before else -1)
        after_last = iids_before[iids_before.index(last) + 1:] if last in iids_before else ()
        self.set_selected(choice, indices)
        flt = self.filter_var.get()
        if flt != "全部":  # 篩選條件下，確認後不再符合的列直接拿掉（不必重建整個清單）
            removed = [iid for i, iid in zip(indices, selected_iids, strict=True)
                       if not self._matches(self.items[i], flt)]
            if removed:
                self.tree.delete(*removed)
            if last not in removed:
                self._goto_next(last)
                return
            nxt = next((iid for iid in after_last if self.tree.exists(iid)), None)
            remaining = self.tree.get_children()
            if nxt is None and remaining:
                nxt = remaining[-1]
            if nxt is not None:
                self._select(nxt)
            else:
                self.show_preview(None)
            return
        self._goto_next(last)

    def pick_suggestion(self, rank: int) -> None:
        if self.current is None:
            return
        suggestions = self.items[self.current].suggestions(self.categories, 3)
        if rank < len(suggestions):
            self.choice_var.set(suggestions[rank][0])
            self.confirm_current()

    def apply_batch(self) -> None:
        name = self.batch_var.get()
        if not name:
            messagebox.showinfo(APP_NAME, "請先在左邊的下拉選單選擇要套用的分類。")
            return
        self.set_selected(name)
        self.refresh_tree()

    def accept_selected(self) -> None:
        for i in self._selected_indices():
            name, _ = self.items[i].best(self.categories)
            if name and self.items[i].status != ERROR:
                self.set_selected(name, [i])
        self.refresh_tree()

    def accept_confident_all(self) -> None:
        threshold = self.settings["confidence_threshold"]
        count = accept_confident(self.items, self.categories, threshold, self.similarity_floor())
        self.refresh_tree()
        messagebox.showinfo(APP_NAME, f"已直接採用 {count} 個 AI 有把握（信心度 ≥ {threshold:.0%}）的判斷。\n"
                                      "剩下的請在清單中逐一確認（可用「顯示：低信心」篩選）。")

    # ------------------------------------------------------------------ 分類管理
    def set_categories(self, categories: list[Category], renames: dict[str, str] | None = None) -> None:
        """套用新的分類清單。renames 是 {舊名稱: 新名稱}，改名時已確認的項目會跟著改，不會被清掉。"""
        if renames:
            for item in self.items:
                if item.status == CONFIRMED and item.chosen in renames:
                    item.chosen = renames[item.chosen]
        self.categories = categories
        save_categories(categories)
        self.refresh_filters()
        reverted = 0
        if self.analysis_running():
            self.needs_rescore = True  # 辨識結束後再統一重算（分數以名稱對應，期間顯示仍然正確）
        elif self.classifier is not None and any(item.embedding is not None for item in self.items):
            reverted = self._rescore()
        else:
            names = {c.name for c in categories}
            for item in self.items:
                if item.status == CONFIRMED and item.chosen not in names:
                    item.status, item.chosen = PENDING, None
                    reverted += 1
        self.refresh_tree()
        self.show_preview(self.current)
        if reverted:
            messagebox.showinfo(APP_NAME, f"有 {reverted} 個已確認的項目，因為分類被刪除而改回「待確認」。")

    def _rescore(self) -> int:
        self.root.configure(cursor="watch")
        self.root.update_idletasks()
        try:
            reverted = rescore(self.items, self.classifier, self.categories)
        finally:
            self.root.configure(cursor="")
        self.status_var.set("分類已更新，AI 判斷已重新計算。")
        return reverted

    def add_category(self, name: str, prompts: list[str]) -> bool:
        name = name.strip()
        if not name:
            return False
        if name in {c.name for c in self.categories}:
            return True
        clash = next((c.name for c in self.categories if folder_key(c.name) == folder_key(name)), None)
        if clash:
            messagebox.showwarning(APP_NAME, f"「{name}」和現有的分類「{clash}」會用到同一個資料夾，請換個名稱。")
            return False
        self.set_categories(self.categories + [Category(name, prompts)])
        return True

    def add_category_prompt(self) -> None:
        name = simpledialog.askstring(APP_NAME, "新分類的名稱（例如：寶寶、旅行、車子）：", parent=self.root)
        if name and self.add_category(name, []):
            self.choice_var.set(name.strip())
            self.other_var.set(name.strip())

    def add_category_from_tag(self, name: str) -> None:
        if name not in {c.name for c in self.categories}:
            if not messagebox.askyesno(APP_NAME, f"要新增分類「{name}」嗎？\n新增後 AI 會重新判斷所有檔案。"):
                return
            english = dict(VOCABULARY).get(name)
            if not self.add_category(name, [f"a photo of {english}"] if english else []):
                return
        self.choice_var.set(name)
        self.other_var.set(name)

    def open_category_dialog(self) -> None:
        CategoryDialog(self.root, self.categories, self.set_categories)

    def open_settings_dialog(self) -> None:
        SettingsDialog(self.root, self.settings, self.on_settings_saved)

    def on_settings_saved(self, settings: dict) -> None:
        model_changed = settings["model"] != self.settings["model"]
        self.settings = settings
        save_settings(self.settings)
        if model_changed and self.items:
            messagebox.showinfo(APP_NAME, "已更換 AI 模型，請重新按「開始辨識」讓新模型重新判斷。")
        self.refresh_tree()

    # ------------------------------------------------------------------ 辨識（背景執行緒）
    def analysis_running(self) -> bool:
        return self._analysis_active

    def choose_folder(self) -> None:
        folder = filedialog.askdirectory(initialdir=self.folder_var.get() or None, title="選擇要分類的資料夾")
        if folder:
            self.folder_var.set(os.path.normpath(folder))

    def choose_output(self) -> None:
        folder = filedialog.askdirectory(initialdir=self.output_var.get() or self.folder_var.get() or None,
                                         title="選擇輸出資料夾")
        if folder:
            self.output_var.set(os.path.normpath(folder))

    def source_dir(self) -> Path:
        return Path(self.folder_var.get().strip()).absolute()

    def output_dir(self) -> Path:
        """輸出資料夾一律換成絕對路徑；只打資料夾名稱時放在來源資料夾裡（不會跑到程式資料夾）。"""
        custom = self.output_var.get().strip()
        if not custom:
            return self.source_dir() / DEFAULT_OUTPUT_NAME
        path = Path(custom)
        return path if path.is_absolute() else (self.source_dir() / path).absolute()

    def _set_busy(self, busy: bool) -> None:
        """整理或復原進行中時，停用會動到檔案或清單的按鈕。"""
        state = ["disabled"] if busy else ["!disabled"]
        for button in (self.organize_btn, self.run_btn, self.undo_btn, self.remove_btn, self.category_btn):
            button.state(state)

    def toggle_analysis(self) -> None:
        if self.analysis_running():
            self.stop_event.set()
            self.status_var.set("正在停止⋯")
            return
        if self.organizing:
            return
        folder = self.source_dir()
        if not folder.is_dir():
            messagebox.showwarning(APP_NAME, "請先選擇一個存在的資料夾。")
            return
        if not self.categories:
            messagebox.showwarning(APP_NAME, "請先在「分類設定」新增至少一個分類。")
            return
        if len(self.categories) < 3 and not messagebox.askokcancel(
            APP_NAME,
            f"目前只有 {len(self.categories)} 個分類。AI 一定會把每個檔案歸到最像的分類，"
            "分類太少時，不相關的照片也會被歸進去。\n\n建議至少設定 3 個分類（可以加一個「其他」）。要繼續嗎？",
        ):
            return
        if any(item.status in (CONFIRMED, SKIPPED) for item in self.items):
            if not messagebox.askyesno(APP_NAME, "重新辨識會清除目前清單中的確認結果，確定要繼續嗎？"):
                return
        self.settings["last_folder"] = str(folder)
        self.settings["include_subfolders"] = self.subfolders_var.get()
        save_settings(self.settings)
        self._set_items([])
        self.refresh_tree()
        self.stop_event = threading.Event()  # 每一輪用新的停止旗標，舊的背景工作不會影響新的一輪
        while not self.queue.empty():  # 丟掉上一輪殘留的訊息
            self.queue.get_nowait()
        self._run_id += 1
        self._analysis_active = True
        self.needs_rescore = False
        self.run_btn.configure(text="■ 停止")
        self.organize_btn.state(["disabled"])
        self.undo_btn.state(["disabled"])
        self.remove_btn.state(["disabled"])
        self.worker = threading.Thread(
            target=self._analysis_worker,
            args=(self._run_id, self.stop_event, folder, self.subfolders_var.get(), self.output_dir(),
                  list(self.categories), dict(self.settings)),
            daemon=True,
        )
        self.worker.start()

    def _analysis_worker(self, run_id: int, stop_event: threading.Event, folder: Path, recursive: bool,
                         output: Path, categories, settings) -> None:
        def post(msg: tuple) -> None:
            self.queue.put((msg[0], run_id, *msg[1:]))

        try:
            post(("status", "正在尋找圖片與影片⋯"))
            paths = scan_folder(folder, recursive=recursive, exclude=output)
            post(("scanned", paths))
            if not paths:
                post(("done", "這個資料夾裡沒有找到支援的圖片或影片。"))
                return
            if self.classifier is None or self.classifier_key != settings["model"]:
                post(("status", "正在載入 AI 模型⋯（第一次使用需下載模型，可能要幾分鐘）"))
                self.classifier = None
                self.classifier = self.classifier_factory(settings["model"])
                self.classifier_key = settings["model"]
            device = getattr(self.classifier, "device_description", "")
            post(("status", f"辨識中⋯　使用：{device}"))
            analyze(
                paths, self.classifier, categories,
                video_frames=int(settings["video_frames"]),
                batch_size=int(settings["batch_size"]),
                on_item=lambda i, item: post(("item", i, item)),
                on_progress=lambda done, total: post(("progress", done, total)),
                stop_event=stop_event,
            )
            if stop_event.is_set():
                post(("done", "已停止。已辨識的項目可以先確認與整理。"))
            else:
                post(("done", f"辨識完成！共 {len(paths)} 個檔案，使用：{device}。請確認分類後按「開始整理」。"))
        except ImportError as exc:
            post(("error", f"缺少必要的套件（{exc}），請重新執行 install.bat。", traceback.format_exc()))
        except Exception as exc:  # noqa: BLE001 - 背景錯誤要回報到介面
            post(("error", f"{type(exc).__name__}: {exc}", traceback.format_exc()))

    def _organize_worker(self, run_id: int, ops, source_dir: Path, output_dir: Path) -> None:
        try:
            result = execute(ops, LOG_DIR, source_dir=source_dir, output_dir=output_dir,
                             on_progress=lambda i, n: self.queue.put(("progress", run_id, i, n)))
            self.queue.put(("organized", run_id, ops, result))
        except Exception as exc:  # noqa: BLE001
            self.queue.put(("error", run_id, f"{type(exc).__name__}: {exc}", traceback.format_exc()))

    def _log_worker(self, run_id: int, log: Path, func, kind: str) -> None:
        """在背景執行「復原」或「移除原檔」。"""
        try:
            self.queue.put((kind, run_id, func(log)))
        except Exception as exc:  # noqa: BLE001
            self.queue.put(("error", run_id, f"{type(exc).__name__}: {exc}", traceback.format_exc()))

    def _collect_garbage(self) -> None:
        """只在主執行緒回收循環參照。

        Tk 物件（例如關掉的對話框裡的 StringVar）若在背景執行緒被回收，整個程式會當掉
        （Tcl_AsyncDelete），所以 main() 關掉自動回收，改成在這裡定期回收。
        """
        gc.collect()
        self.root.after(GC_INTERVAL_MS, self._collect_garbage)

    def _poll_queue(self) -> None:
        try:
            self._drain_queue()
        finally:  # 就算處理訊息時出錯，也要繼續接收背景工作的訊息
            self.root.after(100, self._poll_queue)

    def _drain_queue(self) -> None:
        changed = False
        for _ in range(500):
            try:
                msg = self.queue.get_nowait()
            except queue.Empty:
                break
            if msg[0] != "preview" and msg[1] != self._run_id:
                continue  # 上一輪留下來的訊息
            args = msg[1:] if msg[0] == "preview" else msg[2:]
            try:
                changed = self._handle_message(msg[0], *args) or changed
            except Exception:  # noqa: BLE001 - 一則訊息出錯不影響其他訊息
                self.log_error(traceback.format_exc())
        if changed:
            self.update_counts()
            children = self.tree.get_children()
            if self.current is None and children:
                self._select(children[0])
            elif self.current is not None and self.items[self.current].analyzed and not self.radios[0].winfo_ismapped():
                self.show_preview(self.current, len(self.tree.selection()))

    def _handle_message(self, kind: str, *args) -> bool:
        if kind == "status":
            self.status_var.set(args[0])
        elif kind == "scanned":
            self._set_items([Item(p, media_kind(p) or "image") for p in args[0]])
            self.progress.configure(maximum=max(1, len(self.items)), value=0)
            self.refresh_tree()
        elif kind == "item":
            index, result = args
            item = self.items[index]
            item.merge_analysis(result)  # 保留使用者在辨識完成前就做的確認／略過
            self.preview_cache.pop(item.uid, None)
            self.update_row(index)
            return True
        elif kind == "progress":
            self.progress.configure(maximum=max(1, args[1]), value=args[0])
        elif kind == "done":
            self._finish_worker(args[0])
        elif kind == "organized":
            self._finish_organize(*args)
        elif kind == "undone":
            self._finish_undo(args[0])
        elif kind == "removed":
            self._finish_remove(args[0])
        elif kind == "preview":
            uid, img = args
            self._preview_loading.discard(uid)
            if img is not None:
                self._cache_preview(uid, img)
            if self.current is not None and self.current < len(self.items) and self.items[self.current].uid == uid:
                self._render_preview_image()
        elif kind == "error":
            message, details = args
            self.organizing = False
            self._set_busy(False)
            self._finish_worker("發生錯誤：" + message)
            self.log_error(details)
            self.root.after(0, lambda: self._show_error(f"發生錯誤：\n{message}\n\n詳細內容已記錄在 logs/error.log"))
        return False

    def _finish_worker(self, message: str) -> None:
        self._analysis_active = False
        self.status_var.set(message)
        self.run_btn.configure(text="▶ 開始辨識")
        if not self.organizing:
            self._set_busy(False)
        reverted = 0
        if self.needs_rescore and self.classifier is not None:
            self.needs_rescore = False
            reverted = self._rescore()
        self.refresh_tree()
        if self.current is not None:
            self.show_preview(self.current, len(self.tree.selection()))
        if reverted:
            messagebox.showinfo(APP_NAME, f"有 {reverted} 個已確認的項目，因為分類被刪除而改回「待確認」。")

    # ------------------------------------------------------------------ 整理檔案
    def organize(self) -> None:
        if self.analysis_running() or self.organizing:
            return
        floor, threshold = self.similarity_floor(), self.settings["confidence_threshold"]
        pending = [it for it in self.items if it.status == PENDING and it.analyzed]
        confident = [it for it in pending if not it.is_low(self.categories, threshold, floor)]
        include_pending = False
        if pending:
            low_note = f"\n（其中 {len(pending) - len(confident)} 個 AI 沒把握的黃色項目不會自動整理）" \
                if len(confident) < len(pending) else ""
            answer = messagebox.askyesnocancel(
                APP_NAME,
                f"還有 {len(pending)} 個項目尚未確認。\n\n"
                f"「是」：AI 有把握的 {len(confident)} 個依 AI 判斷一起整理{low_note}\n"
                "「否」：只整理已確認的項目\n"
                "「取消」：回去繼續確認",
            )
            if answer is None:
                return
            include_pending = answer
        chosen = [it for it in self.items if it.status == CONFIRMED]
        if include_pending:
            chosen += confident
        entries = []
        for it in chosen:
            category = it.final_category(self.categories)
            if not category:
                continue
            try:
                date = it.date or file_date(it.path)
            except OSError:  # 檔案已不在：執行時會回報
                date = datetime.now()
            entries.append((it.path.absolute(), category, date))
        if not entries:
            messagebox.showinfo(APP_NAME, "沒有可以整理的項目。請先辨識並確認分類。")
            return

        output = self.output_dir()
        pattern = self.pattern_var.get().strip() or RENAME_PATTERN_PRESETS[0]
        try:
            ops = plan_operations(entries, output, rename=self.rename_var.get(), pattern=pattern)
        except (OSError, RuntimeError) as exc:
            messagebox.showerror(APP_NAME, f"無法規劃整理方式：{exc}")
            return
        space = free_space_problem(ops)
        if space:
            messagebox.showwarning(APP_NAME, space)
            return
        counts = Counter(op.category for op in ops)
        lines = "\n".join(f"　{name}：{n} 個" for name, n in counts.most_common(12))
        if len(counts) > 12:
            lines += f"\n　⋯等 {len(counts)} 個分類"
        example = ops[0]
        if not messagebox.askokcancel(
            APP_NAME,
            f"即將把 {len(ops)} 個檔案複製到：\n{output}\n\n{lines}\n\n"
            f"範例：{example.src.name} → {example.dst.parent.name}\\{example.dst.name}\n\n"
            "原檔不會被移動或修改。確認複本沒問題後，可以再按「移除原檔」。確定要開始嗎？",
        ):
            return

        self.settings.update(
            output_dir=self.output_var.get().strip(),
            rename=self.rename_var.get(), rename_pattern=pattern,
        )
        save_settings(self.settings)
        self.status_var.set("正在複製檔案⋯")
        self.progress.configure(maximum=len(ops), value=0)
        self.organizing = True
        self._set_busy(True)
        self._run_id += 1
        threading.Thread(target=self._organize_worker, args=(self._run_id, ops, self.source_dir(), output),
                         daemon=True).start()

    def _finish_organize(self, ops, result) -> None:
        self.organizing = False
        self._set_busy(False)
        processed = {str(src) for src in result.done_sources}
        self._set_items([it for it in self.items if str(it.path.absolute()) not in processed])
        self.refresh_tree()
        output = ops[0].dst.parent.parent if ops else self.output_dir()
        message = (f"完成！已把 {result.done} 個檔案複製到分類資料夾，原檔都還在原位。\n\n"
                   "確認複本沒問題後，可以按「移除原檔」把原檔移到資源回收筒。")
        if result.aborted:
            message = f"整理中途停止：紀錄檔無法寫入。已複製 {result.done} 個檔案，原檔都還在原位。"
        if result.warnings:
            message += "\n\n注意：\n" + "\n".join(result.warnings[:8])
        if result.errors:
            details = "\n".join(f"{p.name}：{err}" for p, err in result.errors[:8])
            message += f"\n\n有 {len(result.errors)} 個檔案失敗：\n{details}"
        self.status_var.set(f"整理完成：{result.done} 個檔案 → {output}")
        if messagebox.askyesno(APP_NAME, message + "\n\n要開啟輸出資料夾嗎？"):
            try:
                open_in_file_manager(output)
            except OSError:
                pass

    def _pick_log(self, action_text: str) -> tuple[Path, list[dict], str] | None:
        if self.organizing or self.analysis_running():
            return None
        log = latest_log(LOG_DIR)
        if log is None:
            messagebox.showinfo(APP_NAME, f"目前沒有可以{action_text}的整理紀錄。")
            return None
        try:
            rows = read_log(log)
        except (OSError, ValueError) as exc:
            if messagebox.askyesno(
                APP_NAME,
                f"無法讀取整理紀錄「{log.name}」：\n{exc}\n\n"
                "要把這份紀錄移到旁邊（改名為「無法讀取_…」，內容保留）嗎？\n"
                "這樣才能處理更早的整理紀錄。檔案本身都不會被更動。",
            ):
                try:
                    moved = set_aside_log(log)
                    messagebox.showinfo(APP_NAME, f"已移到：{moved.name}")
                except OSError as move_exc:
                    messagebox.showerror(APP_NAME, f"無法移動紀錄檔：{move_exc}")
            return None
        if not rows:
            if messagebox.askyesno(
                APP_NAME,
                f"整理紀錄「{log.name}」裡沒有可以{action_text}的項目（可能是空的或只寫了一半）。\n\n"
                "要把這份紀錄移到旁邊（內容保留）嗎？這樣才能處理更早的整理紀錄。",
            ):
                try:
                    set_aside_log(log)
                except OSError as move_exc:
                    messagebox.showerror(APP_NAME, f"無法移動紀錄檔：{move_exc}")
            return None
        when = datetime.fromtimestamp(log.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
        return log, rows, when

    def _confirm_rows(self, rows: list[dict], key: str, question: str, notes: list[str], ok_text: str) -> bool:
        """列出這次要處理的「每一個」檔案讓使用者確認（不省略）。

        紀錄的來源資料夾跟目前選擇的資料夾不同時，最上面顯示警告：紀錄可能不是這次的，或被修改過。
        """
        source, output = log_roots(rows)
        header = []
        current = self.folder_var.get().strip()
        if current and not _same_folder(source, current):
            header.append(f"⚠注意：這份紀錄的來源資料夾（{source}）跟目前選擇的資料夾（{current}）不同，"
                          "請確認清單中的檔案是你要處理的。")
        header += [question, f"來源資料夾：{source}", f"輸出資料夾：{output}", *notes]
        base = source if key == "原始路徑" else output
        lines = []
        for row in rows:
            path = Path(row[key])
            try:
                lines.append(str(path.relative_to(base)))
            except ValueError:
                lines.append(str(path))
        return confirm_with_list(self.root, APP_NAME, "\n".join(header),
                                 [f"共 {len(lines)} 個檔案：", *lines], ok_text)

    def _start_log_task(self, log: Path, func, kind: str, status: str) -> None:
        self.organizing = True
        self._set_busy(True)
        self.status_var.set(status)
        self._run_id += 1
        threading.Thread(target=self._log_worker, args=(self._run_id, log, func, kind), daemon=True).start()

    def undo_last(self) -> None:
        picked = self._pick_log("復原")
        if picked is None:
            return
        log, rows, when = picked
        if not self._confirm_rows(
            rows, "新路徑", f"要復原 {when} 的整理嗎？下列 {len(rows)} 個複本會移到資源回收筒，原檔不受影響。",
            ["（原檔已經移除，或複本後來被修改過的，會保留複本不動）"], "復原",
        ):
            return
        self._start_log_task(log, undo, "undone", "正在復原⋯")

    def remove_originals_last(self) -> None:
        picked = self._pick_log("移除原檔")
        if picked is None:
            return
        log, rows, when = picked
        if not self._confirm_rows(
            rows, "原始路徑", f"要把 {when} 整理的下列 {len(rows)} 個原檔移到資源回收筒嗎？",
            ["・只有跟複本內容完全相同的原檔才會移除",
             "・資源回收筒裡的檔名會多一個「(整理前)」標記，還原後改回即可",
             "・移除原檔之後，這次整理就不能再用「復原上次整理」",
             "建議先打開輸出資料夾確認分類結果。"], "移到資源回收筒",
        ):
            return
        self._start_log_task(log, remove_originals, "removed", "正在把原檔移到資源回收筒⋯")

    def _report(self, title: str, result, retry_hint: str) -> str:
        message = title
        if result.warnings:
            message += "\n\n注意：\n" + "\n".join(result.warnings[:8])
        if result.errors:
            message += (f"\n\n{len(result.errors)} 個失敗（{retry_hint}）：\n"
                        + "\n".join(f"{p.name}：{e}" for p, e in result.errors[:8]))
        return message

    def _finish_undo(self, result) -> None:
        self.organizing = False
        self._set_busy(False)
        self.status_var.set(f"復原完成：{result.done} 個複本已移除")
        messagebox.showinfo(APP_NAME, self._report(
            f"已移除 {result.done} 個複本，原檔都在原位。", result, "再按一次「復原上次整理」會重試這些檔案")
            + "\n\n如需重新分類，請再按一次「開始辨識」。")

    def _finish_remove(self, result) -> None:
        self.organizing = False
        self._set_busy(False)
        self.status_var.set(f"已把 {result.done} 個原檔移到資源回收筒")
        messagebox.showinfo(APP_NAME, self._report(
            f"已把 {result.done} 個原檔移到資源回收筒（可從資源回收筒救回）。", result,
            "這些原檔都沒有被移除；排除原因後再按一次「移除原檔」會重試"))

    # ------------------------------------------------------------------ 其他
    def log_error(self, text: str) -> None:
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            with open(LOG_DIR / "error.log", "a", encoding="utf-8") as f:
                f.write(f"\n[{datetime.now():%Y-%m-%d %H:%M:%S}]\n{text}\n")
        except OSError:
            pass

    def _show_error(self, message: str) -> None:
        """同一時間只顯示一個錯誤對話框，避免錯誤對話框一直疊出來。"""
        if self._error_dialog_open:
            self.status_var.set(message.replace("\n", " ")[:200])
            return
        self._error_dialog_open = True
        try:
            messagebox.showerror(APP_NAME, message)
        finally:
            self._error_dialog_open = False

    def on_tk_error(self, exc_type, exc, tb) -> None:
        self.log_error("".join(traceback.format_exception(exc_type, exc, tb)))
        self._show_error(f"發生錯誤：{exc}\n\n詳細內容已記錄在 logs/error.log")

    def on_close(self) -> None:
        if self.organizing:
            messagebox.showinfo(APP_NAME, "正在處理檔案，請等待完成後再關閉程式。")
            return
        self.stop_event.set()
        self.settings.update(
            last_folder=self.folder_var.get().strip(), include_subfolders=self.subfolders_var.get(),
            output_dir=self.output_var.get().strip(),
            rename=self.rename_var.get(), rename_pattern=self.pattern_var.get().strip() or RENAME_PATTERN_PRESETS[0],
        )
        try:
            save_settings(self.settings)
        except OSError:
            pass
        self.root.destroy()


class CategoryDialog(tk.Toplevel):
    """編輯分類清單：名稱＋給 AI 的描述（每行一個）。"""

    def __init__(self, master, categories: list[Category], on_save):
        super().__init__(master)
        self.title("分類設定")
        self.geometry("720x480")
        self.transient(master)
        self.on_save = on_save
        self.work = [Category(c.name, list(c.prompts)) for c in categories]
        self.origins: list[str | None] = [c.name for c in categories]  # 每一列原本的名稱（用來辨認「改名」）
        self.index: int | None = None

        body = ttk.Frame(self, padding=8)
        body.pack(fill="both", expand=True)
        left = ttk.Frame(body)
        left.pack(side="left", fill="y")
        self.listbox = tk.Listbox(left, width=18, height=18, exportselection=False)
        self.listbox.pack(fill="y", expand=True)
        self.listbox.bind("<<ListboxSelect>>", self.on_pick)
        buttons = ttk.Frame(left)
        buttons.pack(fill="x", pady=4)
        ttk.Button(buttons, text="新增", width=6, command=self.add).pack(side="left")
        ttk.Button(buttons, text="刪除", width=6, command=self.remove).pack(side="left", padx=2)
        ttk.Button(buttons, text="↑", width=3, command=lambda: self.move(-1)).pack(side="left")
        ttk.Button(buttons, text="↓", width=3, command=lambda: self.move(1)).pack(side="left")

        right = ttk.Frame(body, padding=(12, 0, 0, 0))
        right.pack(side="left", fill="both", expand=True)
        ttk.Label(right, text="分類名稱（也會是資料夾名稱）：").pack(anchor="w")
        self.name_var = tk.StringVar()
        ttk.Entry(right, textvariable=self.name_var).pack(fill="x", pady=(0, 8))
        ttk.Label(right, text="給 AI 的描述（每行一個，中文或英文皆可；越具體越準，可留白）：").pack(anchor="w")
        self.prompts = tk.Text(right, height=10, wrap="word")
        self.prompts.pack(fill="both", expand=True)
        ttk.Label(
            right, style="Hint.TLabel", wraplength=440,
            text="小技巧：英文描述通常最準，例如「a photo of a cat」。想區分的東西越相近（如貓 vs 老虎），描述要寫得越具體。",
        ).pack(anchor="w", pady=4)

        bottom = ttk.Frame(self, padding=8)
        bottom.pack(fill="x")
        ttk.Button(bottom, text="還原預設分類", command=self.reset).pack(side="left")
        ttk.Button(bottom, text="取消", command=self.destroy).pack(side="right")
        ttk.Button(bottom, text="儲存", style="Accent.TButton", command=self.save).pack(side="right", padx=6)

        self.reload_list(0)
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


class SettingsDialog(tk.Toplevel):
    def __init__(self, master, settings: dict, on_save):
        super().__init__(master)
        self.title("設定")
        self.transient(master)
        self.resizable(False, False)
        self.settings = dict(settings)
        self.on_save = on_save
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
            ("低信心門檻（%）：", ttk.Spinbox(body, from_=5, to=95, increment=5, textvariable=self.threshold_var, width=8)),
            ("每批處理張數：", ttk.Spinbox(body, from_=1, to=256, textvariable=self.batch_var, width=8)),
        ]
        hints = [
            "有 NVIDIA 顯示卡時會自動使用 CUDA 加速",
            "每支影片平均擷取幾張畫面來判斷，越多越準但越慢",
            "AI 信心低於此值的項目會標黃色，建議人工確認",
            "顯示卡記憶體不足時請調低（例如 8）",
        ]
        for r, ((label, widget), hint) in enumerate(zip(rows, hints, strict=True)):
            ttk.Label(body, text=label).grid(row=r * 2, column=0, sticky="w", pady=(6, 0))
            widget.grid(row=r * 2, column=1, sticky="w", pady=(6, 0))
            ttk.Label(body, text=hint, style="Hint.TLabel").grid(row=r * 2 + 1, column=1, sticky="w")

        buttons = ttk.Frame(body)
        buttons.grid(row=len(rows) * 2, column=0, columnspan=2, sticky="e", pady=(12, 0))
        ttk.Button(buttons, text="儲存", command=self.save).pack(side="left", padx=4)
        ttk.Button(buttons, text="取消", command=self.destroy).pack(side="left")
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


def _enable_windows_dpi_awareness() -> None:
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            pass


def main() -> None:
    _enable_windows_dpi_awareness()
    gc.disable()  # 循環參照改由主執行緒定期回收（見 App._collect_garbage）
    lock = acquire_app_lock()  # 同時只允許開一個視窗（也讓安裝程式知道程式正在執行）
    root = tk.Tk()
    if lock is None:
        root.withdraw()
        messagebox.showinfo(APP_NAME, "AI 媒體分類器已經開著了（請看工作列）。")
        root.destroy()
        return
    App(root)
    root.mainloop()
    lock.close()
    # 背景還在讀檔的執行緒不必等它們結束（設定已在關閉視窗時儲存）
    if any(t.is_alive() for t in threading.enumerate() if t is not threading.main_thread()):
        sys.stdout.flush()
        os._exit(0)
