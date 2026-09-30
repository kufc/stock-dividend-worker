"""桌面視窗介面（Tkinter，Python 內建，不需額外安裝）。

四個步驟：選擇資料夾 → 確認分類 → 預覽整理結果 → 完成。
整理只會「複製」檔案，原檔完全不動；處理原檔（移到資源回收筒）是完成之後另外的、可選的動作。
"""

from __future__ import annotations

import gc
import io
import os
import queue
import shutil
import sys
import threading
import traceback
import tkinter as tk
from collections import Counter, OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

from PIL import Image, ImageTk

from . import APP_NAME, __version__
from .analysis import CONFIRMED, ERROR, PENDING, SKIPPED, Item, analyze, rescore
from .config import (
    LOG_DIR,
    RENAME_MODES,
    Category,
    acquire_app_lock,
    load_categories,
    load_settings,
    save_categories,
    save_settings,
)
from .dialogs import (
    CategoryDialog,
    ImageViewer,
    RecordsDialog,
    SettingsDialog,
    confirm_with_list,
    open_in_file_manager,
    show_problem,
)
from .media import FolderStats, file_date, folder_stats, load_preview, media_kind, scan_folder
from .organizer import (
    PlannedOp,
    classify_rows,
    execute,
    folder_key,
    free_space_problem,
    log_roots,
    plan_operations,
    remove_originals,
    set_aside_log,
    snapshot_log,
    undo,
)
from .theme import setup_theme
from .views import (
    FILTERS,
    SKIP,
    SUGGEST_FILTER_PREFIX,
    DonePage,
    FolderPage,
    PlanPage,
    ReviewPage,
    StatusBar,
    StepHeader,
)
from .vocabulary import VOCABULARY

DEFAULT_OUTPUT_NAME = "已分類"
HELP_FILE = Path(__file__).resolve().parent.parent / "使用說明與免責聲明_Usage-and-Disclaimer.txt"
GC_INTERVAL_MS = 5000
NARROW_WIDTH = 1100  # 100% 縮放下，視窗比這個窄就把清單與預覽改成分頁
WIDE_PLAN_WIDTH = 1200  # 「預覽整理結果」夠寬時分成左右兩欄
THUMB_PX = 40
MAX_THUMBS = 6000
MAX_PLAN_ROWS = 300
KIND_TEXT = {"image": "圖片", "video": "影片"}
NO_RUN_ID = {"preview", "folder", "model", "viewer"}  # 這幾種訊息不屬於某一輪工作
KEY_IGNORE = {"Entry", "TEntry", "TCombobox", "Text", "Spinbox", "TSpinbox"}
RETURN_IGNORE = KEY_IGNORE | {"TButton", "TCheckbutton", "TRadiobutton"}


@dataclass
class Plan:
    """步驟 3 顯示的、也是「開始複製」實際執行的整理計畫。"""

    ops: list[PlannedOp]
    source: Path
    output: Path
    total_bytes: int = 0
    problem: str | None = None
    pending_count: int = 0


@dataclass
class RunInfo:
    """一次整理（含重試）的累計結果，給「完成」頁顯示。"""

    ops: list[PlannedOp]
    source: Path
    output: Path
    done: int = 0
    done_sources: set = field(default_factory=set)
    failures: dict = field(default_factory=dict)  # 原檔路徑 → (Path, 說明)
    warnings: list = field(default_factory=list)
    log_path: Path | None = None
    stopped: bool = False
    aborted: bool = False

    def remaining_ops(self) -> list[PlannedOp]:
        return [op for op in self.ops if str(op.src) not in self.done_sources]


def _same_folder(a: str | Path, b: str | Path) -> bool:
    try:
        return os.path.normcase(str(Path(a).resolve())) == os.path.normcase(str(Path(b).resolve()))
    except (OSError, ValueError):
        return False


def _fmt_size(n: float) -> str:
    if n >= 1024**3:
        return f"{n / 1024**3:.1f} GB"
    if n < 1024**2:
        return "不到 1 MB"
    return f"{n / 1024**2:.0f} MB"


def _default_classifier_factory(preset: str):
    from .classifier import Classifier

    return Classifier(preset)


def _default_model_status(choice: str):
    from .classifier import model_status

    return model_status(choice)


class App:
    def __init__(self, root: tk.Tk, classifier_factory=_default_classifier_factory, model_status_func=None):
        self.root = root
        self.classifier_factory = classifier_factory
        self.model_status_func = model_status_func or _default_model_status
        self.settings = load_settings()
        self.categories = load_categories()
        self.items: list[Item] = []
        self.classifier = None
        self.classifier_key = None
        self.queue: queue.Queue = queue.Queue()
        self.worker: threading.Thread | None = None
        self.stop_event = threading.Event()
        self.copy_stop = threading.Event()
        self.needs_rescore = False
        self.organizing = False  # 複製、移除複本、移除原檔進行中
        self._analysis_active = False  # 辨識中（包含背景已結束但訊息還沒處理完的時間）
        self._run_id = 0  # 每次背景工作的編號，用來丟掉上一輪殘留的訊息
        self._pos: dict[str, int] = {}
        self._counts_job = self._search_job = self._folder_job = self._plan_job = self._render_job = None
        self._layout_job = None
        self._folder_token = 0
        self._error_dialog_open = False
        self.step = 1
        self.current: int | None = None
        self.sort_key: str | None = None
        self.sort_reverse = False
        self.preview_cache: OrderedDict[int, Image.Image] = OrderedDict()
        self._preview_loading: set[int] = set()
        self.preview_photo = None
        self.thumbs: dict[int, ImageTk.PhotoImage] = {}
        self.history: list[list[tuple[int, str, str | None]]] = []
        self.run_info: RunInfo | None = None
        self.plan: Plan | None = None
        self.stats: FolderStats | None = None
        self.stats_error = ""
        self.model_note = None
        self.records_win = None
        self.session_source: Path | None = None
        self._viewers: dict[int, ImageViewer] = {}
        self._viewer_seq = 0
        self._auto_switch = False
        self._last_default_output = ""
        self._progress = (0, 0)
        self._sash_ratio = float(self.settings["split_ratio"])
        self._candidate_names: list[str] = []

        self.theme = setup_theme(root)
        self.px = self.theme.px
        root.title(f"{APP_NAME} v{__version__}")
        self._init_geometry()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.report_callback_exception = self.on_tk_error

        self.status_var = tk.StringVar()
        self.folder_var = tk.StringVar(value=self.settings.get("last_folder", ""))
        self.subfolders_var = tk.BooleanVar(value=self.settings.get("include_subfolders", True))
        self.output_var = tk.StringVar(value=self.settings.get("output_dir", ""))
        self.rename_mode_var = tk.StringVar(value=self.settings["rename_mode"])
        self.pattern_var = tk.StringVar(value=self.settings["rename_pattern"])
        self.include_pending_var = tk.BooleanVar(value=False)
        self.filter_var = tk.StringVar(value=FILTERS[0])
        self.search_var = tk.StringVar()
        self.choice_var = tk.StringVar()
        self.other_var = tk.StringVar()
        self.batch_var = tk.StringVar()

        self._build_ui()
        self._bind_keys()
        self.folder_var.trace_add("write", self._on_folder_edit)
        self.subfolders_var.trace_add("write", self._on_folder_edit)
        self.search_var.trace_add("write", self._on_search_edit)
        self.output_var.trace_add("write", lambda *_: self.schedule_plan())
        self.pattern_var.trace_add("write", lambda *_: self.schedule_plan())
        self._sync_output_default()
        self.refresh_filters()
        self.update_counts(force=True)
        self.show_detail(None)
        self.show_step(1)
        self.refresh_folder_stats()
        self._start_model_status()
        root.after(100, self._poll_queue)
        root.after(GC_INTERVAL_MS, self._collect_garbage)

    # ------------------------------------------------------------------ 介面建構
    def _init_geometry(self, screen: tuple[int, int] | None = None) -> None:
        """初始視窗大小：不超過螢幕（測試可以指定假想的螢幕大小）。"""
        root, px = self.root, self.px
        sw, sh = screen or (root.winfo_screenwidth(), root.winfo_screenheight())
        width, height = min(px(1280), sw - 40), min(px(800), sh - 110)
        root.geometry(f"{width}x{height}+{max(0, (sw - width) // 2)}+{max(0, (sh - height) // 3)}")
        root.minsize(min(px(900), sw - 20), min(px(560), sh - 80))

    def _build_ui(self) -> None:
        self.header = StepHeader(self.root, self)
        self.header.pack(side="top", fill="x")
        self.status_bar = StatusBar(self.root, self)
        self.status_bar.pack(side="bottom", fill="x")
        self.progress = self.status_bar.progress
        self.content = ttk.Frame(self.root)
        self.content.pack(fill="both", expand=True)
        self.content.grid_rowconfigure(0, weight=1)
        self.content.grid_columnconfigure(0, weight=1)
        self.page1 = FolderPage(self.content, self)
        self.page2 = ReviewPage(self.content, self)
        self.page3 = PlanPage(self.content, self)
        self.page4 = DonePage(self.content, self)
        self.pages = {1: self.page1, 2: self.page2, 3: self.page3, 4: self.page4}
        for page in self.pages.values():
            page.grid(row=0, column=0, sticky="nsew")
        self.tree = self.page2.tree
        self.root.bind("<Configure>", self._on_root_configure)
        self.page2.paned.bind("<Configure>", self._on_paned_configure)
        self.page2.paned.bind("<ButtonRelease-1>", self._save_sash)

    def handle_key(self, key: str, widget_class: str) -> bool:
        """鍵盤快速鍵（只在「確認分類」步驟有效；在輸入欄位裡打字時不會觸發）。回傳有沒有處理這個按鍵。"""
        if self.step != 2:
            return False
        if widget_class in (RETURN_IGNORE if key == "Return" else KEY_IGNORE):
            return False
        if key == "Return":
            self.confirm_current()
        elif key in ("1", "2", "3"):
            self.pick_suggestion(int(key) - 1)
        elif key in ("s", "S"):
            self.skip_current()
        elif key == "ctrl-z":
            self.undo_last_confirm()
        else:
            return False
        return True

    def _bind_keys(self) -> None:
        def bind(sequence: str, key: str) -> None:
            self.root.bind(sequence, lambda e: "break" if self.handle_key(key, e.widget.winfo_class()) else None)

        bind("<Return>", "Return")
        for digit in "123":
            bind(digit, digit)
        for letter in "sS":
            bind(letter, letter)
        bind("<Control-z>", "ctrl-z")
        self.root.bind("<Control-f>", lambda e: self.page2.search_entry.focus_set() if self.step == 2 else None)
        self.tree.bind("<Control-a>", lambda e: (self.tree.selection_set(self.tree.get_children()), "break")[1])

    def _on_root_configure(self, event) -> None:
        if event.widget is not self.root:
            return
        if self._layout_job:
            self.root.after_cancel(self._layout_job)
        self._layout_job = self.root.after(120, self._apply_layout)

    def _apply_layout(self) -> None:
        self._layout_job = None
        width = self.root.winfo_width()
        self.page2.set_narrow(width < self.px(NARROW_WIDTH))
        self.page3.set_wide(width >= self.px(WIDE_PLAN_WIDTH))
        self.schedule_preview_render()

    def _on_paned_configure(self, _event=None) -> None:
        self.page2.set_sash_ratio(self._sash_ratio)

    def _save_sash(self, _event=None) -> None:
        ratio = self.page2.sash_ratio()
        if ratio is not None:
            self._sash_ratio = max(0.25, min(0.75, ratio))
            self.settings["split_ratio"] = self._sash_ratio

    # ------------------------------------------------------------------ 步驟切換
    def analysis_running(self) -> bool:
        return self._analysis_active

    def confirmed_count(self) -> int:
        return sum(1 for item in self.items if item.status == CONFIRMED and item.chosen)

    def step_available(self, n: int) -> bool:
        if self.organizing:
            return False
        if n == 1:
            return True
        if n == 2:
            return bool(self.items)
        if n == 3:
            return self.confirmed_count() > 0 and not self.analysis_running()
        return self.run_info is not None

    def show_step(self, n: int) -> bool:
        if not self.step_available(n):
            if n == 3 and self.analysis_running() and self.confirmed_count():
                self.status_var.set("辨識完成後才能預覽整理結果；現在可以先在這裡確認分類。")
            return False
        self.step = n
        self.pages[n].tkraise()
        if not self.analysis_running() and not self.organizing:
            self.status_var.set("")
        if n == 1:
            self._sync_output_default()
            self._update_page1()
        elif n == 2:
            self.refresh_tree()
            self._auto_select()
            self.tree.focus_set()
        elif n == 3:
            self._sync_output_default()
            self.refresh_plan()
        else:
            self.render_done()
        scroll = getattr(self.pages[n], "scroll", None)
        if scroll is not None and n != 2:
            scroll.scroll_to_top()
        self._refresh_nav()
        return True

    def _refresh_nav(self) -> None:
        available = {n for n in range(1, 5) if self.step_available(n)}
        self.header.update_steps(self.step, available)
        p = self.page2
        enable = self.confirmed_count() > 0 and not self.analysis_running() and not self.organizing
        p.next_btn.state(["!disabled"] if enable else ["disabled"])

    def _set_busy(self, busy: bool) -> None:
        """複製或處理紀錄進行中：停用會動到檔案或清單的操作。"""
        state = ["disabled"] if busy else ["!disabled"]
        self.header.set_busy(busy)
        for widget in (self.page1.start_btn, self.page3.back_btn, self.page3.include_check, self.page3.output_entry,
                       self.page3.pattern_entry, *self.page3.radios, self.page4.again_btn, self.page4.back_btn,
                       self.page4.records_btn, self.page4.remove_btn, self.page4.open_btn):
            widget.state(state)
        self.page2.next_btn.state(state)
        self.page2.back_btn.state(state)
        if not busy:
            self._refresh_step_controls()
        self._refresh_nav()

    def _refresh_step_controls(self) -> None:
        if self.step == 1:
            self._update_page1()
        elif self.step == 3:
            self.refresh_plan()
        elif self.step == 4:
            self.render_done()

    # ------------------------------------------------------------------ 步驟 1：選擇資料夾
    def source_dir(self) -> Path:
        return Path(self.folder_var.get().strip()).absolute()

    def _base_dir(self) -> Path | None:
        """整理時「來源資料夾」的基準：已經辨識過就固定用辨識當時的資料夾，不受輸入欄後來的修改影響。"""
        if self.session_source is not None and (self.items or self.run_info is not None):
            return self.session_source
        return self.source_dir() if self.folder_var.get().strip() else None

    def _default_output(self) -> Path | None:
        base = self._base_dir()
        return base / DEFAULT_OUTPUT_NAME if base is not None else None

    def output_dir(self) -> Path:
        """輸出資料夾一律換成絕對路徑；只打資料夾名稱時放在來源資料夾裡（不會跑到程式資料夾）。"""
        custom = self.output_var.get().strip()
        base = self._base_dir() or self.source_dir()
        if not custom:
            return base / DEFAULT_OUTPUT_NAME
        path = Path(custom)
        return path if path.is_absolute() else (base / path).absolute()

    def _sync_output_default(self) -> None:
        default = self._default_output()
        text = str(default) if default is not None else ""
        current = self.output_var.get().strip()
        if not current or current == self._last_default_output:
            if current != text:
                self.output_var.set(text)
        self._last_default_output = text

    def choose_folder(self) -> None:
        folder = filedialog.askdirectory(initialdir=self.folder_var.get() or None, title="選擇要分類的資料夾")
        if folder:
            self.folder_var.set(os.path.normpath(folder))

    def choose_output(self) -> None:
        folder = filedialog.askdirectory(initialdir=self.output_var.get() or self.folder_var.get() or None,
                                         title="選擇輸出資料夾")
        if folder:
            self.output_var.set(os.path.normpath(folder))

    def _on_folder_edit(self, *_args) -> None:
        self._sync_output_default()
        self.stats = None
        self.stats_error = ""
        self._folder_token += 1
        self._update_page1()
        if self._folder_job:
            self.root.after_cancel(self._folder_job)
        self._folder_job = self.root.after(350, self.refresh_folder_stats)

    def refresh_folder_stats(self) -> None:
        self._folder_job = None
        self._folder_token += 1
        self.stats, self.stats_error = None, ""
        text = self.folder_var.get().strip()
        folder = Path(text).absolute() if text else None
        if folder is not None and folder.is_dir():
            threading.Thread(target=self._stats_worker, daemon=True, args=(
                self._folder_token, folder, self.subfolders_var.get(), self.output_dir())).start()
        self._update_page1()

    def _stats_worker(self, token: int, folder: Path, recursive: bool, exclude: Path) -> None:
        try:
            stats, error = folder_stats(folder, recursive, exclude), ""
        except OSError as exc:
            stats, error = None, str(exc)
        self.queue.put(("folder", token, stats, error))

    def _start_model_status(self) -> None:
        def work(choice: str) -> None:
            try:
                status = self.model_status_func(choice)
            except Exception:  # noqa: BLE001 - 只是提示資訊
                status = None
            self.queue.put(("model", choice, status))

        threading.Thread(target=work, args=(self.settings["model"],), daemon=True).start()

    def _update_page1(self) -> None:
        p = self.page1
        text = self.folder_var.get().strip()
        is_dir = bool(text) and Path(text).is_dir()
        running = self._analysis_active
        supported = self.stats.supported if self.stats else 0
        if not text:
            p.show_empty("還沒有選擇資料夾。按「選擇資料夾…」，或把資料夾路徑貼在上面的欄位。")
        elif not is_dir:
            p.show_empty("找不到這個資料夾，請確認路徑，或按「選擇資料夾…」重新選擇。")
        elif self.stats_error:
            p.show_empty(f"無法讀取這個資料夾：{self.stats_error}")
        elif self.stats is None:
            p.show_empty("正在計算檔案數量⋯")
        else:
            s = self.stats
            note = "不支援的檔案不會被讀取、移動或修改。" + ("（已包含子資料夾）" if self.subfolders_var.get() else "")
            if s.supported == 0:
                note = "這個資料夾裡沒有支援的圖片或影片。" + note
            p.show_counts(s.images, s.videos, s.other, note)

        if running:
            p.start_btn.configure(text="停止辨識", style="TButton")
            p.start_btn.state(["!disabled"])
        else:
            p.start_btn.configure(text=f"開始辨識 {supported:,} 個檔案" if supported else "開始辨識",
                                  style="Accent.TButton")
            p.start_btn.state(["!disabled"] if (supported and not self.organizing) else ["disabled"])
        p.browse_btn.configure(style="TButton" if (is_dir and not running) else "Accent.TButton")
        for widget in (p.entry, p.browse_btn, p.sub_check):
            widget.state(["disabled"] if running else ["!disabled"])
        if running:
            p.hint_var.set("辨識進行中。可以按「停止辨識」，已辨識的檔案不會遺失。")
        elif not text:
            p.hint_var.set("先選擇一個資料夾。")
        elif not is_dir:
            p.hint_var.set("找不到資料夾。")
        elif self.stats is None and not self.stats_error:
            p.hint_var.set("正在計算檔案數量⋯")
        elif supported:
            hint = f"將辨識 {supported:,} 個檔案（圖片 {self.stats.images:,}、影片 {self.stats.videos:,}）。"
            if self.items:
                hint += "　要繼續上次的結果，請按上方的「2　確認分類」。"
            p.hint_var.set(hint)
        else:
            p.hint_var.set("沒有可以辨識的檔案。")
        if self.model_note is None:
            p.model_var.set("")
        else:
            label, cached = self.model_note
            if cached:
                p.model_var.set(f"AI 模型已經在這台電腦上，不需要再下載。（{label}）")
            elif cached is False:
                p.model_var.set(f"第一次辨識會先下載 AI 模型，需要網路連線，可能要幾分鐘；下載一次之後就不必再下載。（{label}）")
            else:
                p.model_var.set(label)

    # ------------------------------------------------------------------ 檔案清單
    def _set_items(self, items: list[Item]) -> None:
        """換掉整份清單時一定要走這裡：清除選取、預覽與快取，避免指到別的檔案。"""
        self.items = items
        self._pos = {str(item.uid): i for i, item in enumerate(items)}
        self.preview_cache.clear()
        self.thumbs.clear()
        self.history.clear()
        self.tree.selection_remove(self.tree.selection())
        self.show_detail(None)

    def _iid(self, index: int) -> str:
        return str(self.items[index].uid)

    def _index_of(self, iid: str) -> int | None:
        return self._pos.get(iid)

    def similarity_floor(self) -> float:
        return float(getattr(self.classifier, "similarity_floor", 0.0) or 0.0)

    def refresh_filters(self) -> None:
        names = [c.name for c in self.categories]
        p = self.page2
        p.filter_box["values"] = FILTERS + [SUGGEST_FILTER_PREFIX + n for n in names]
        if self.filter_var.get() not in p.filter_box["values"]:
            self.filter_var.set(FILTERS[0])
        p.batch_box["values"] = names
        p.other_box["values"] = names
        if self.batch_var.get() not in names:
            self.batch_var.set("")

    def _is_low(self, item: Item) -> bool:
        return item.is_low(self.categories, self.settings["confidence_threshold"], self.similarity_floor())

    def _matches(self, item: Item, flt: str, needle: str = "") -> bool:
        if needle:
            hay = f"{item.path.name} {item.best(self.categories)[0] or ''} {item.chosen or ''}".casefold()
            if needle not in hay:
                return False
        if flt == "全部":
            return True
        if flt == "待確認":
            return item.status == PENDING
        if flt == "需檢查":
            return self._is_low(item)
        if flt == "建議較明確":
            return item.status == PENDING and item.analyzed and not self._is_low(item)
        if flt == "已確認":
            return item.status == CONFIRMED
        if flt == "已略過":
            return item.status == SKIPPED
        if flt == "讀取失敗":
            return item.status == ERROR
        if flt.startswith(SUGGEST_FILTER_PREFIX):
            return item.best(self.categories)[0] == flt[len(SUGGEST_FILTER_PREFIX):]
        return True

    def _visible(self, item: Item) -> bool:
        return self._matches(item, self.filter_var.get(), self.search_var.get().strip().casefold())

    def _thumb(self, item: Item):
        photo = self.thumbs.get(item.uid)
        if photo is not None or not item.thumbnail or len(self.thumbs) >= MAX_THUMBS:
            return photo
        try:
            size = self.px(THUMB_PX)
            img = Image.open(io.BytesIO(item.thumbnail)).convert("RGB")
            img.thumbnail((size, size))
            canvas = Image.new("RGB", (size, size), "#ffffff")
            canvas.paste(img, ((size - img.width) // 2, (size - img.height) // 2))
            photo = ImageTk.PhotoImage(canvas)
        except Exception:  # noqa: BLE001 - 縮圖失敗只是不顯示
            return None
        self.thumbs[item.uid] = photo
        return photo

    def _blank_thumb(self):
        if not hasattr(self, "_blank"):
            size = self.px(THUMB_PX)
            self._blank = ImageTk.PhotoImage(Image.new("RGB", (size, size), "#ffffff"))
        return self._blank

    def _row(self, item: Item) -> tuple[tuple, tuple]:
        name, _score = item.best(self.categories)
        if item.status == ERROR:
            return ("—", "讀取失敗"), ("error",)
        if not item.analyzed:
            return ("—", "辨識中…" if self.analysis_running() else "未辨識"), ()
        suggest = name or "—"
        if item.status == CONFIRMED:
            return (suggest, f"已確認：{item.chosen}"), ("confirmed",)
        if item.status == SKIPPED:
            return (suggest, "已略過"), ("skipped",)
        if self._is_low(item):
            return (suggest, "需檢查"), ("check",)
        return (suggest, "待確認"), ()

    def _sort_value(self, index: int):
        item = self.items[index]
        if self.sort_key == "name":
            return item.path.name.lower()
        values, _ = self._row(item)
        return values[0 if self.sort_key == "suggest" else 1]

    def refresh_tree(self) -> None:
        selected = set(self.tree.selection())
        focused = self.tree.focus()
        self.tree.delete(*self.tree.get_children())
        flt, needle = self.filter_var.get(), self.search_var.get().strip().casefold()
        visible = [i for i, item in enumerate(self.items) if self._matches(item, flt, needle)]
        if self.sort_key:
            visible.sort(key=self._sort_value, reverse=self.sort_reverse)
        blank = self._blank_thumb()
        for i in visible:
            item = self.items[i]
            values, tags = self._row(item)
            self.tree.insert("", "end", iid=self._iid(i), text=item.path.name, values=values, tags=tags,
                             image=self._thumb(item) or blank)
        keep = [iid for iid in selected if self.tree.exists(iid)]
        if keep:
            self.tree.selection_set(keep)
        if focused and self.tree.exists(focused):
            self.tree.focus(focused)
        self.update_counts(force=True)
        self.on_select()

    def update_row(self, index: int) -> None:
        iid = self._iid(index)
        if self.tree.exists(iid):
            item = self.items[index]
            values, tags = self._row(item)
            self.tree.item(iid, values=values, tags=tags, image=self._thumb(item) or self._blank_thumb())

    def sort_by(self, key: str) -> None:
        if self.sort_key == key:
            self.sort_reverse = not self.sort_reverse
        else:
            self.sort_key, self.sort_reverse = key, False
        self.refresh_tree()

    def _on_search_edit(self, *_args) -> None:
        if self._search_job:
            self.root.after_cancel(self._search_job)
        self._search_job = self.root.after(200, self._run_search)

    def _run_search(self) -> None:
        self._search_job = None
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
        text = (f"共 {len(self.items)} 個｜已確認 {c[CONFIRMED]}｜待確認 {c[PENDING]}（需檢查 {low}）｜"
                f"已略過 {c[SKIPPED]}｜讀取失敗 {c[ERROR]}")
        if self.analysis_running():
            text += "｜辨識完成後才能進入下一步"
        elif not c[CONFIRMED]:
            text += "｜至少確認一個檔案才能進入下一步"
        self.page2.count_var.set(text)
        self._refresh_nav()

    # ------------------------------------------------------------------ 目前檔案：預覽與候選分類
    def _selected_indices(self) -> list[int]:
        indices = (self._index_of(iid) for iid in self.tree.selection())
        return [i for i in indices if i is not None]

    def on_select(self, _event=None) -> None:
        selection = self.tree.selection()
        indices = self._selected_indices()
        p = self.page2
        p.batch_label_var.set(f"已選取 {len(indices)} 個檔案" + ("" if indices else "（按住 Ctrl 或 Shift 可多選）"))
        for button in p.batch_buttons:
            button.state(["!disabled"] if indices and not self.organizing else ["disabled"])
        if not indices:
            self.show_detail(None)
            return
        focus = self.tree.focus()
        index = self._index_of(focus) if focus in selection else indices[-1]
        if index is None:
            index = indices[-1]
        if index != self.current or p.view_state != "detail":
            self.show_detail(index, len(indices))
        else:
            self._update_multi_note(len(indices))

    def _update_multi_note(self, count: int) -> None:
        if self.current is None:
            return
        item = self.items[self.current]
        date = item.date.strftime("%Y-%m-%d %H:%M") if item.date else ""
        info = f"{KIND_TEXT.get(item.kind, '')}　{date}　{item.path.parent}"
        if count > 1:
            info += f"\n已選取 {count} 個檔案；這裡的按鈕只處理目前這一個，要一次處理多個請用清單下方的操作。"
        self.page2.info_var.set(info)

    def schedule_preview_render(self) -> None:
        if self._render_job:
            self.root.after_cancel(self._render_job)
        self._render_job = self.root.after(120, self._render_preview_image)

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
        if item.uid not in self._preview_loading and item.status != ERROR:
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
        self._render_job = None
        label = self.page2.preview_label
        if self.current is None or self.current >= len(self.items):
            return
        item = self.items[self.current]
        img = self._preview_image(self.current)
        if img is None:
            self.preview_photo = None
            if item.status == ERROR:
                text = "（無法讀取這個檔案）"
            elif item.uid in self._preview_loading or not item.analyzed:
                text = "讀取預覽中⋯"
            else:
                text = "（無法預覽此檔案）"
            label.configure(image="", text=text)
            return
        frame = self.page2.preview_frame
        width, height = frame.winfo_width(), frame.winfo_height()
        if width < 50 or height < 50:  # 還沒排版完成
            width, height = self.px(480), self.px(300)
        shown = img.copy()
        shown.thumbnail((width, height))
        self.preview_photo = ImageTk.PhotoImage(shown)
        label.configure(image=self.preview_photo, text="")

    def _all_decided(self) -> bool:
        return bool(self.items) and not self.analysis_running() and not any(it.status == PENDING for it in self.items)

    def show_detail(self, index: int | None, selected_count: int = 1) -> None:
        p = self.page2
        self.current = index
        for child in p.tags_frame.winfo_children():
            child.destroy()
        self.other_var.set("")
        p.other_hint_var.set("")
        p.undo_btn.state(["!disabled"] if self.history and not self.organizing else ["disabled"])
        if index is None:
            self.preview_photo = None
            p.preview_label.configure(image="", text="")
            if self._all_decided():
                c = Counter(item.status for item in self.items)
                p.finished_var.set(f"已確認 {c[CONFIRMED]} 個　已略過 {c[SKIPPED]} 個　讀取失敗 {c[ERROR]} 個\n"
                                   "可以按下方「下一步：預覽整理結果」；想回頭檢查，也可以從左邊清單選檔案。")
                p.show_state("finished")
            else:
                if self.analysis_running():
                    p.placeholder_var.set("辨識還在進行中；已辨識好的檔案可以先選來確認。")
                elif self.items:
                    p.placeholder_var.set("請從左邊的清單選一個檔案。")
                else:
                    p.placeholder_var.set("還沒有檔案。請回到第 1 步選擇資料夾並開始辨識。")
                p.show_state("placeholder")
            self.choice_var.set("")
            self._candidate_names = []
            p.confirm_btn.state(["disabled"])
            p.skip_btn.state(["disabled"])
            return

        item = self.items[index]
        p.show_state("detail")
        self.schedule_preview_render()
        p.file_var.set(item.path.name)
        self._update_multi_note(selected_count)
        floor = self.similarity_floor()
        low = self._is_low(item)
        if item.error:
            note = f"讀取失敗：{item.error}\n這個檔案不會被整理，也不會被更動。"
        elif not item.analyzed:
            note = "這個檔案還在辨識中，請稍候。"
        elif item.top_similarity < floor:
            note = "這個檔案跟每個分類都不太像。建議自己看過再選分類，或新增一個分類。"
        elif low:
            note = "模型對這個檔案沒有把握，請自己看過再選。"
        else:
            note = ""
        p.notice_var.set(note)
        if note:
            p.notice_frame.grid()
        else:
            p.notice_frame.grid_remove()
        p.clarity_var.set("" if not item.analyzed or item.status == ERROR else ("需要確認" if low else "建議較明確"))

        suggestions = item.suggestions(self.categories, 3)
        self._candidate_names = [name for name, _ in suggestions]
        for i, button in enumerate(p.candidate_buttons):
            if i < len(suggestions):
                button.grid()
            else:
                button.grid_remove()
        if item.status == CONFIRMED and item.chosen:
            self.choice_var.set(item.chosen)
        elif item.status == PENDING and suggestions and not low:
            self.choice_var.set(suggestions[0][0])
        else:
            self.choice_var.set("")
        choice = self.choice_var.get()
        if choice and choice not in self._candidate_names:
            self.other_var.set(choice)
        self._refresh_choice_styles(item)

        existing = {c.name for c in self.categories}
        if item.tags:
            ttk.Label(p.tags_frame, text="AI 還看到（點一下可以選用或新增）：", style="CardHint.TLabel").grid(
                row=0, column=0, columnspan=3, sticky="w")
            for k, (name, _prob) in enumerate(item.tags[:6]):
                label = name + ("" if name in existing else "＋")
                ttk.Button(p.tags_frame, text=label, style="Link.TButton",
                           command=lambda n=name: self.add_category_from_tag(n)).grid(
                    row=1 + k // 3, column=k % 3, sticky="w")
        if item.analyzed and suggestions:
            scores = "、".join(f"{n} {s:.0%}" for n, s in suggestions)
            p.other_hint_var.set(f"模型分數：{scores}（僅供參考，不代表正確率）")

    def _refresh_choice_styles(self, item: Item | None = None) -> None:
        p = self.page2
        choice = self.choice_var.get()
        item = item if item is not None else (self.items[self.current] if self.current is not None else None)
        for i, button in enumerate(p.candidate_buttons):
            if i < len(self._candidate_names):
                name = self._candidate_names[i]
                on = name == choice
                button.configure(text=f"{'✓ ' if on else ''}{i + 1}　{name}" + ("　（AI 建議）" if i == 0 else ""),
                                 style="ChoiceOn.TButton" if on else "Choice.TButton")
        can_confirm = bool(choice) and choice != SKIP and item is not None and item.analyzed and \
            item.status != ERROR and not self.organizing
        p.confirm_btn.state(["!disabled"] if can_confirm else ["disabled"])
        p.confirm_btn.configure(text="確認並看下一個" if (choice or item is None) else "請先選一個分類")
        p.skip_btn.state(["!disabled"] if item is not None and not self.organizing else ["disabled"])

    def pick_suggestion(self, rank: int) -> None:
        """選擇第 rank 個候選分類（只是選取，按「確認並看下一個」才會確認）。"""
        if self.current is None or rank >= len(self._candidate_names):
            return
        self.choice_var.set(self._candidate_names[rank])
        self.other_var.set("")
        self._refresh_choice_styles()
        self.tree.focus_set()  # 焦點回到清單，接著按 Enter 就是「確認並看下一個」

    def on_other_selected(self, _event=None) -> None:
        text = self.other_var.get().strip()
        names = [c.name for c in self.categories]
        match = next((n for n in names if n == text), None) or next((n for n in names if n.casefold() == text.casefold()), None)
        if match is None:
            hits = [n for n in names if text.casefold() in n.casefold()] if text else []
            match = hits[0] if len(hits) == 1 else None
        p = self.page2
        if match is None:
            p.other_hint_var.set("找不到這個分類。可以按「新增分類…」建立它。" if text else "")
            return
        self.other_var.set(match)
        self.choice_var.set(match)
        p.other_hint_var.set("")
        self._refresh_choice_styles()
        self.tree.focus_set()

    def filter_other_list(self, event=None) -> None:
        if event is not None and event.keysym in ("Return", "Up", "Down", "Escape", "Tab", "Left", "Right"):
            return
        needle = self.other_var.get().strip().casefold()
        names = [c.name for c in self.categories]
        hits = [n for n in names if needle in n.casefold()] if needle else names
        self.page2.other_box["values"] = hits or names
        self.page2.other_hint_var.set(f"符合 {len(hits)} 個分類，按 Enter 或從清單選一個" if needle and hits
                                      else ("找不到符合的分類，可以按「新增分類…」" if needle else ""))

    # ------------------------------------------------------------------ 確認、略過、撤回
    def _decide(self, pairs: list[tuple[int, str]]) -> None:
        """套用一組決定（索引, 分類或 SKIP），並記下來讓「撤回上一次確認」可以還原。"""
        entry = []
        for index, choice in pairs:
            item = self.items[index]
            entry.append((item.uid, item.status, item.chosen))
            if choice == SKIP:
                item.status, item.chosen = SKIPPED, None
            else:
                item.status, item.chosen = CONFIRMED, choice
            self.update_row(index)
        if entry:
            self.history.append(entry)
            del self.history[:-200]

    def confirm_current(self) -> None:
        if self.current is None or self.organizing:
            return
        item = self.items[self.current]
        choice = self.choice_var.get()
        if not choice or choice == SKIP or not item.analyzed or item.status == ERROR:
            return
        index = self.current
        self._decide([(index, choice)])
        self._advance(index)

    def skip_current(self) -> None:
        if self.current is None or self.organizing:
            return
        index = self.current
        self._decide([(index, SKIP)])
        self._advance(index)

    def _advance(self, index: int) -> None:
        """決定之後跳到下一個「待確認且已辨識」的檔案；沒有了就顯示「分類確認完成」。"""
        iid = self._iid(index)
        before = list(self.tree.get_children())
        pos = before.index(iid) if iid in before else -1
        if pos >= 0 and not self._visible(self.items[index]):
            self.tree.delete(iid)  # 篩選條件下確認後不再符合的列直接拿掉
        after = [x for x in before[pos + 1:] if self.tree.exists(x)]
        earlier = [x for x in before[:max(pos, 0)] if self.tree.exists(x)]
        self.update_counts(force=True)
        for candidate in after + earlier:
            i = self._index_of(candidate)
            if i is not None and self.items[i].status == PENDING and self.items[i].analyzed:
                self._select(candidate)
                return
        self.tree.selection_remove(self.tree.selection())
        self.on_select()

    def _select(self, iid: str, take_focus: bool = True) -> None:
        self.tree.selection_set(iid)
        self.tree.focus(iid)
        self.tree.see(iid)
        self.on_select()
        if take_focus:
            self.tree.focus_set()  # 鍵盤操作（Enter、1／2／3、S）才會作用在清單上
        self.page2.show_detail_tab()  # 視窗很窄、清單與預覽是分頁時，自動切到「目前檔案」

    def _auto_select(self) -> None:
        if self.current is not None or self.step != 2:
            return
        for iid in self.tree.get_children():
            i = self._index_of(iid)
            if i is not None and self.items[i].status == PENDING and self.items[i].analyzed:
                self._select(iid, take_focus=False)  # 辨識時自動選取，不搶走使用者正在輸入的欄位
                return

    def undo_last_confirm(self) -> None:
        if not self.history or self.organizing:
            return
        entry = self.history.pop()
        restored = []
        for uid, status, chosen in entry:
            index = self._pos.get(str(uid))
            if index is None:
                continue
            item = self.items[index]
            item.status, item.chosen = status, chosen
            restored.append(index)
        self.refresh_tree()
        if restored:
            iid = self._iid(restored[0])
            if self.tree.exists(iid):
                self._select(iid)
        self.status_var.set(f"已撤回上一次的確認（{len(restored)} 個檔案）。")

    def apply_batch(self) -> None:
        indices = self._selected_indices()
        name = self.batch_var.get()
        if not indices:
            messagebox.showinfo(APP_NAME, "請先在左邊清單選取要處理的檔案（按住 Ctrl 或 Shift 可多選）。")
            return
        if not name:
            messagebox.showinfo(APP_NAME, "請先在下拉選單選擇要套用的分類。")
            return
        self._batch([(i, name) for i in indices if self.items[i].analyzed and self.items[i].status != ERROR])

    def accept_selected(self) -> None:
        pairs = []
        for i in self._selected_indices():
            name, _ = self.items[i].best(self.categories)
            if name and self.items[i].status != ERROR:
                pairs.append((i, name))
        self._batch(pairs)

    def skip_selected(self) -> None:
        self._batch([(i, SKIP) for i in self._selected_indices()])

    def _batch(self, pairs: list[tuple[int, str]]) -> None:
        if not pairs or self.organizing:
            return
        self._decide(pairs)
        self.status_var.set(f"已處理 {len(pairs)} 個檔案（可用「撤回上一次確認」還原）。")
        self.refresh_tree()

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
        self.history.clear()
        self.refresh_tree()
        self.show_detail(self.current, len(self.tree.selection()) or 1)
        if reverted:
            messagebox.showinfo(APP_NAME, f"有 {reverted} 個已確認的項目，因為分類被刪除而改回「待確認」。")

    def _rescore(self) -> int:
        self.root.configure(cursor="watch")
        self.root.update_idletasks()
        try:
            reverted = rescore(self.items, self.classifier, self.categories)
        finally:
            self.root.configure(cursor="")
        self.status_var.set("分類已更新，AI 建議已重新計算。")
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
            self._refresh_choice_styles()

    def add_category_from_tag(self, name: str) -> None:
        if name not in {c.name for c in self.categories}:
            if not messagebox.askyesno(APP_NAME, f"要新增分類「{name}」嗎？\n新增後 AI 會重新判斷所有檔案。"):
                return
            english = dict(VOCABULARY).get(name)
            if not self.add_category(name, [f"a photo of {english}"] if english else []):
                return
        self.choice_var.set(name)
        self.other_var.set(name)
        self._refresh_choice_styles()

    def open_category_dialog(self) -> None:
        CategoryDialog(self.root, self.categories, self.set_categories)

    def open_settings_dialog(self) -> None:
        SettingsDialog(self.root, self.settings, self.on_settings_saved)

    def on_settings_saved(self, settings: dict) -> None:
        model_changed = settings["model"] != self.settings["model"]
        self.settings.update(settings)
        save_settings(self.settings)
        if model_changed:
            self._start_model_status()
            if self.items:
                messagebox.showinfo(APP_NAME, "已更換 AI 模型，請重新按「開始辨識」讓新模型重新判斷。")
        self.refresh_tree()

    def open_help(self) -> None:
        if not HELP_FILE.exists():
            messagebox.showinfo(APP_NAME, f"找不到說明文件：{HELP_FILE.name}")
            return
        try:
            open_in_file_manager(HELP_FILE)
        except OSError as exc:
            show_problem(self.root, "無法開啟說明文件。", f"請直接用記事本開啟：{HELP_FILE}", str(exc))

    # ------------------------------------------------------------------ 放大檢視
    def open_viewer(self) -> None:
        if self.current is None or self.current >= len(self.items):
            return
        item = self.items[self.current]
        self._viewer_seq += 1
        vid = self._viewer_seq
        note = "影片只顯示代表畫面" if item.kind == "video" else ""
        viewer = ImageViewer(self.root, item.path.name, note)
        self._viewers[vid] = viewer
        viewer.bind("<Destroy>", lambda e, vid=vid: self._viewers.pop(vid, None) if e.widget is viewer else None)

        def work() -> None:
            try:
                img, error = load_preview(item.path, item.kind, None, size=2400), ""
            except Exception as exc:  # noqa: BLE001
                img, error = None, f"無法讀取這個檔案：{exc}"
            self.queue.put(("viewer", vid, img, error))

        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------------ 辨識（背景執行緒）
    def toggle_analysis(self) -> None:
        if self.analysis_running():
            self.stop_analysis()
        else:
            self.start_analysis()

    def stop_analysis(self) -> None:
        if self.analysis_running():
            self.stop_event.set()
            self.status_var.set("正在停止⋯")
            self.page2.stop_btn.state(["disabled"])

    def start_analysis(self) -> None:
        if self.analysis_running() or self.organizing:
            return
        text = self.folder_var.get().strip()
        folder = self.source_dir()
        if not text or not folder.is_dir():
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
        self.session_source = folder
        self.run_info = None
        self.plan = None
        self.include_pending_var.set(False)
        self._sync_output_default()
        self._set_items([])
        self.refresh_tree()
        self.stop_event = threading.Event()  # 每一輪用新的停止旗標，舊的背景工作不會影響新的一輪
        while not self.queue.empty():  # 丟掉上一輪殘留的訊息
            self.queue.get_nowait()
        self._run_id += 1
        self._analysis_active = True
        self.needs_rescore = False
        self._auto_switch = False
        self._progress = (0, 0)
        self.page1.show_notice("")
        self.page2.analysis_var.set("準備中⋯")
        self.page2.stop_btn.state(["!disabled"])
        self.page2.show_toolbar(True)
        self.status_bar.show_progress(True)
        self._progress_indeterminate()
        self._update_page1()
        self._refresh_nav()
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
                post(("done", f"辨識完成！共 {len(paths)} 個檔案，使用：{device}。請確認分類。"))
        except ImportError as exc:
            post(("error", f"缺少必要的套件（{exc}），請重新執行 install.bat。", traceback.format_exc()))
        except Exception as exc:  # noqa: BLE001 - 背景錯誤要回報到介面
            post(("error", f"{type(exc).__name__}: {exc}", traceback.format_exc()))

    def _progress_indeterminate(self) -> None:
        self.progress.configure(mode="indeterminate")
        self.progress.start(14)

    def _progress_value(self, done: int, total: int) -> None:
        self.progress.stop()
        self.progress.configure(mode="determinate", maximum=max(1, total), value=done)

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
        changed = refresh_detail = False
        for _ in range(500):
            try:
                msg = self.queue.get_nowait()
            except queue.Empty:
                break
            kind = msg[0]
            if kind not in NO_RUN_ID and msg[1] != self._run_id:
                continue  # 上一輪留下來的訊息
            args = msg[1:] if kind in NO_RUN_ID else msg[2:]
            try:
                result = self._handle_message(kind, *args)
            except Exception:  # noqa: BLE001 - 一則訊息出錯不影響其他訊息
                self.log_error(traceback.format_exc())
                continue
            if result == "item":
                changed = True
            elif result == "current":
                changed = refresh_detail = True
        if changed:
            self.update_counts()
            if self.step == 2:
                if refresh_detail and self.current is not None:
                    self.show_detail(self.current, len(self.tree.selection()) or 1)
                self._auto_select()

    def _handle_message(self, kind: str, *args):
        if kind == "status":
            self.status_var.set(args[0])
            if "載入 AI 模型" in args[0]:
                self._progress_indeterminate()
        elif kind == "scanned":
            self._set_items([Item(p, media_kind(p) or "image") for p in args[0]])
            self.refresh_tree()
            self._refresh_nav()
        elif kind == "item":
            index, result = args
            item = self.items[index]
            item.merge_analysis(result)  # 保留使用者在辨識完成前就做的確認／略過
            self.preview_cache.pop(item.uid, None)
            self.update_row(index)
            if not self._auto_switch:
                self._auto_switch = True
                if self.step == 1:
                    self.show_step(2)
            return "current" if index == self.current else "item"
        elif kind == "progress":
            done, total = args
            self._progress = (done, total)
            self._progress_value(done, total)
            if self.organizing:
                self.status_var.set(f"正在複製檔案⋯（{done}／{total}）")
            elif self._analysis_active:
                self.page2.analysis_var.set(f"辨識中：{done}／{total}")
        elif kind == "done":
            self._finish_worker(args[0])
        elif kind == "organized":
            self._finish_organize(*args)
        elif kind in ("undone", "removed"):
            self._finish_log_task(kind, args[0])
        elif kind == "folder":
            token, stats, error = args
            if token == self._folder_token:
                self.stats, self.stats_error = stats, error
                self._update_page1()
        elif kind == "model":
            choice, status = args
            if choice == self.settings["model"]:
                self.model_note = status
                self._update_page1()
        elif kind == "viewer":
            vid, img, error = args
            viewer = self._viewers.get(vid)
            if viewer is not None:
                try:
                    viewer.set_image(img, error)
                except tk.TclError:
                    pass
        elif kind == "preview":
            uid, img = args
            self._preview_loading.discard(uid)
            if img is not None:
                self._cache_preview(uid, img)
            if self.current is not None and self.current < len(self.items) and self.items[self.current].uid == uid:
                self._render_preview_image()
        elif kind == "error":
            message, details = args
            was_organizing = self.organizing
            self.organizing = False
            self.status_bar.show_progress(False)
            if self._analysis_active:
                self._finish_worker("發生錯誤：" + message)
            if was_organizing:
                self._set_busy(False)
            self.log_error(details)
            self.root.after(0, lambda: self._show_error(message, details))
        return None

    def _finish_worker(self, message: str) -> None:
        self._analysis_active = False
        self.status_var.set(message)
        self.status_bar.show_progress(self.organizing)
        self.page2.analysis_var.set(message)
        self.page2.show_toolbar(False)
        if not self.items:
            self.page1.show_notice(message)
        reverted = 0
        if self.needs_rescore and self.classifier is not None:
            self.needs_rescore = False
            reverted = self._rescore()
        self.refresh_tree()
        self._auto_select()
        if self.current is not None:
            self.show_detail(self.current, len(self.tree.selection()) or 1)
        else:
            self.show_detail(None)
        self._update_page1()
        self._refresh_nav()
        if reverted:
            messagebox.showinfo(APP_NAME, f"有 {reverted} 個已確認的項目，因為分類被刪除而改回「待確認」。")

    # ------------------------------------------------------------------ 步驟 3：預覽整理結果
    def schedule_plan(self) -> None:
        if self.step != 3:
            return
        if self._plan_job:
            self.root.after_cancel(self._plan_job)
        self._plan_job = self.root.after(250, self.refresh_plan)

    def _confident_pending(self) -> list[Item]:
        return [it for it in self.items if it.status == PENDING and it.analyzed and not self._is_low(it)]

    def _rename_settings(self) -> tuple[bool, str, str | None]:
        """回傳 (是否重新命名, 樣式, 問題說明)。"""
        mode = self.rename_mode_var.get()
        if mode == "keep":
            return False, "{分類}_{序號}", None
        if mode == "custom":
            pattern = self.pattern_var.get().strip()
            return True, pattern, None if pattern else "請輸入自訂的檔名格式，或改選其他命名方式。"
        return True, RENAME_MODES[mode][1] or "{分類}_{序號}", None

    def _compute_plan(self) -> Plan:
        output = self.output_dir()
        source = self._base_dir() or self.source_dir()
        chosen = [it for it in self.items if it.status == CONFIRMED and it.chosen]
        confident = self._confident_pending()
        if self.include_pending_var.get():
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
        plan = Plan([], source, output, pending_count=len(confident))
        rename, pattern, problem = self._rename_settings()
        if problem:
            plan.problem = problem
            return plan
        if not entries:
            return plan
        try:
            plan.ops = plan_operations(entries, output, rename=rename, pattern=pattern)
        except (OSError, RuntimeError) as exc:
            plan.problem = f"無法規劃整理方式：{exc}"
            return plan
        for op in plan.ops:
            try:
                plan.total_bytes += op.src.stat().st_size
            except OSError:
                pass
        plan.problem = free_space_problem(plan.ops)
        return plan

    def refresh_plan(self) -> None:
        self._plan_job = None
        if self.step != 3:
            return
        p = self.page3
        plan = self._compute_plan()
        self.plan = plan
        n = len(plan.ops)
        c = Counter(item.status for item in self.items)
        p.pattern_entry.state(["!disabled"] if self.rename_mode_var.get() == "custom" and not self.organizing
                              else ["disabled"])
        p.summary_var.set(f"已確認的 {self.confirmed_count()} 個檔案會複製到分類資料夾"
                          + (f"，另外含 {plan.pending_count} 個建議較明確的檔案。" if self.include_pending_var.get()
                             and plan.pending_count else "。"))
        pending_left = c[PENDING] - (plan.pending_count if self.include_pending_var.get() else 0)
        left = [text for text in (f"尚未確認 {pending_left} 個" if pending_left > 0 else "",
                                  f"已略過 {c[SKIPPED]} 個" if c[SKIPPED] else "",
                                  f"讀取失敗 {c[ERROR]} 個" if c[ERROR] else "") if text]
        p.excluded_var.set(("不會整理：" + "、".join(left) + "。") if left else "")
        if plan.pending_count:
            p.include_check.configure(text=f"同時整理 {plan.pending_count} 個「建議較明確」但還沒確認的檔案（依 AI 建議的分類）")
            if not p.include_check.winfo_ismapped():
                p.include_check.pack(anchor="w", pady=(self.px(6), 0))
        else:
            p.include_check.pack_forget()
            self.include_pending_var.set(False)

        categories = Counter(op.category for op in plan.ops)
        shown = "、".join(f"{name} {count} 個" for name, count in categories.most_common(10))
        if len(categories) > 10:
            shown += f"…等 {len(categories)} 個分類"
        p.counts_var.set(f"共 {n} 個檔案，分成 {len(categories)} 個分類資料夾：{shown}" if n else "目前沒有要整理的檔案。")
        p.tree.delete(*p.tree.get_children())
        for op in plan.ops[:MAX_PLAN_ROWS]:
            p.tree.insert("", "end", values=(op.src.name, f"{op.dst.parent.name}\\{op.dst.name}"))
        p.tree_note_var.set(f"只顯示前 {MAX_PLAN_ROWS} 個，共 {n} 個。" if n > MAX_PLAN_ROWS else "")

        free = self._free_space(plan)
        p.space_var.set(f"實際輸出位置：{plan.output}\n需要空間：約 {_fmt_size(plan.total_bytes)}"
                        + (f"　輸出位置剩餘：{_fmt_size(free)}" if free is not None else ""))
        p.show_problem(plan.problem or "")
        ok = bool(n) and not plan.problem and not self.organizing
        p.copy_btn.configure(text=f"開始複製 {n:,} 個檔案" if n else "開始複製")
        if not self.organizing:
            p.copy_btn.state(["!disabled"] if ok else ["disabled"])
        p.hint_var.set(f"會複製到：{plan.output}　原檔不會被更動。" if ok else
                       ("請先處理上面紅色的問題。" if plan.problem else "沒有要整理的檔案。"))

    @staticmethod
    def _free_space(plan: Plan) -> int | None:
        target = plan.output
        while not target.exists() and target.parent != target:
            target = target.parent
        try:
            return shutil.disk_usage(target).free
        except OSError:
            return None

    # ------------------------------------------------------------------ 複製
    def toggle_copy(self) -> None:
        if self.organizing:
            self.request_stop_copy()
        else:
            self.start_copy()

    def request_stop_copy(self) -> None:
        self.copy_stop.set()
        self.status_var.set("正在停止：會先完成目前這個檔案，已複製好的都會保留。")
        self.page3.copy_btn.state(["disabled"])
        self.page4.retry_btn.state(["disabled"])

    def start_copy(self) -> None:
        if self.analysis_running() or self.organizing:
            return
        self.refresh_plan()
        plan = self.plan
        if plan is None or not plan.ops or plan.problem:
            return
        self.settings.update(output_dir=self._output_setting(), rename_mode=self.rename_mode_var.get(),
                             rename_pattern=self.pattern_var.get().strip() or self.settings["rename_pattern"])
        try:
            save_settings(self.settings)
        except OSError:
            pass
        self.run_info = RunInfo(list(plan.ops), plan.source, plan.output)
        self._begin_copy(self.run_info.ops, None)

    def _output_setting(self) -> str:
        text = self.output_var.get().strip()
        default = self._default_output()
        return "" if default is not None and text == str(default) else text

    def toggle_retry(self) -> None:
        if self.organizing:
            self.request_stop_copy()
        else:
            self.retry_remaining()

    def retry_remaining(self) -> None:
        info = self.run_info
        if info is None or self.organizing or self.analysis_running():
            return
        ops = info.remaining_ops()
        if ops:
            append = info.log_path if info.log_path is not None and info.log_path.exists() else None
            self._begin_copy(ops, append)

    def _begin_copy(self, ops: list[PlannedOp], append_to: Path | None) -> None:
        info = self.run_info
        assert info is not None
        self.copy_stop = threading.Event()
        self.organizing = True
        self._run_id += 1
        self.status_var.set("正在複製檔案⋯")
        self.status_bar.show_progress(True)
        self._progress_value(0, len(ops))
        self._set_busy(True)
        for button, text in ((self.page3.copy_btn, "完成目前檔案後停止"), (self.page4.retry_btn, "完成目前檔案後停止")):
            button.configure(text=text)
            button.state(["!disabled"])
        threading.Thread(target=self._organize_worker, daemon=True, args=(
            self._run_id, ops, info.source, info.output, self.copy_stop, append_to, self.log_dir())).start()

    def _organize_worker(self, run_id: int, ops, source_dir: Path, output_dir: Path, stop_event, append_to,
                         log_dir: Path) -> None:
        try:
            result = execute(ops, log_dir, source_dir=source_dir, output_dir=output_dir, stop_event=stop_event,
                             append_to=append_to,
                             on_progress=lambda i, n: self.queue.put(("progress", run_id, i, n)))
            self.queue.put(("organized", run_id, ops, result))
        except Exception as exc:  # noqa: BLE001
            self.queue.put(("error", run_id, f"{type(exc).__name__}: {exc}", traceback.format_exc()))

    def _finish_organize(self, ops, result) -> None:
        info = self.run_info
        assert info is not None
        done = {str(src) for src in result.done_sources}
        info.done += result.done
        info.done_sources |= done
        for src, message in result.errors:
            info.failures[str(src)] = (src, message)
        for key in done:
            info.failures.pop(key, None)
        info.warnings += result.warnings
        if result.log_path is not None:
            info.log_path = result.log_path
        info.stopped, info.aborted = result.stopped, result.aborted
        self.organizing = False
        self.status_bar.show_progress(False)
        self._set_items([it for it in self.items if str(it.path.absolute()) not in done])
        self.refresh_tree()
        self._set_busy(False)
        self.status_var.set(f"複製完成：{info.done} 個檔案 → {info.output}")
        self.show_step(4)

    # ------------------------------------------------------------------ 步驟 4：完成
    def render_done(self) -> None:
        info = self.run_info
        p = self.page4
        if info is None:
            return
        total = len(info.ops)
        failed = len(info.failures)
        remaining = len(info.remaining_ops())
        unattempted = max(0, remaining - failed)
        if info.aborted:
            p.set_banner("danger", "整理中途停止",
                         f"紀錄檔無法寫入，為了安全已停止。已複製 {info.done} 個檔案；原檔都還在原位。")
        elif not failed and not unattempted:
            p.set_banner("ok", f"完成：已複製 {info.done} 個檔案",
                         f"原檔都還在原位，複本放在分類資料夾裡：{info.output}")
        elif info.stopped and not failed:
            p.set_banner("warn", f"已停止：已複製 {info.done}／{total} 個",
                         f"還有 {unattempted} 個還沒複製。可以按「繼續複製剩下的 {unattempted} 個」，已複製好的不會重做。")
        else:
            p.set_banner("warn", f"部分完成：已複製 {info.done}／{total} 個，{failed} 個失敗",
                         "失敗的檔案都沒有被更動。處理原因之後可以按「重試」，已複製好的不會重做。")
        lines = [f"複製成功：{info.done} 個"]
        if failed:
            lines.append(f"失敗：{failed} 個")
        if unattempted:
            lines.append(f"還沒複製：{unattempted} 個")
        lines.append(f"輸出位置：{info.output}")
        if info.log_path is not None:
            lines.append(f"整理紀錄：{info.log_path.name}（之後可用來移除複本或處理原檔）")
        if info.warnings:
            lines.append("注意：" + "；".join(info.warnings[:8]))
        p.result_var.set("\n".join(lines))

        p.fail_tree.delete(*p.fail_tree.get_children())
        if failed or unattempted:
            for i, (src, message) in enumerate(info.failures.values()):
                p.fail_tree.insert("", "end", iid=str(i), values=(src.name, message.split("（詳細：")[0]))
            p.fail_hint_var.set("選一列可以查看技術細節（回報問題時才需要）。" if failed else "")
            p.retry_btn.configure(text=f"重試失敗的 {remaining} 個" if failed else f"繼續複製剩下的 {remaining} 個")
            p.retry_btn.state(["disabled"] if self.organizing else ["!disabled"])
            p.fail_card.pack(fill="x", pady=(0, self.px(12)), after=p.result)
        else:
            p.fail_card.pack_forget()
        self._details_reset()

        has_log = info.log_path is not None and info.log_path.exists() and info.done > 0
        p.remove_btn.state(["!disabled"] if has_log and not self.organizing else ["disabled"])
        p.open_btn.state(["disabled"] if self.organizing else ["!disabled"])
        if self.items and not self.organizing:
            p.back_btn.configure(text=f"回到確認分類（還有 {len(self.items)} 個檔案）")
            p.back_btn.pack(side="left", padx=(self.px(8), 0))
        else:
            p.back_btn.pack_forget()

    def _details_reset(self) -> None:
        p = self.page4
        p.details_text.pack_forget()
        p.details_shown = False
        p.details_btn.configure(text="顯示技術細節")

    def on_fail_select(self, _event=None) -> None:
        info, p = self.run_info, self.page4
        selection = p.fail_tree.selection()
        if info is None or not selection:
            return
        entries = list(info.failures.values())
        index = int(selection[0])
        if index < len(entries):
            src, message = entries[index]
            p.details_text.configure(state="normal")
            p.details_text.delete("1.0", "end")
            p.details_text.insert("1.0", f"{src}\n{message}")
            p.details_text.configure(state="disabled")

    def toggle_fail_details(self) -> None:
        p = self.page4
        p.details_shown = not p.details_shown
        if p.details_shown:
            p.details_text.pack(fill="x", pady=(self.px(8), 0))
            p.details_btn.configure(text="隱藏技術細節")
            if not p.fail_tree.selection() and p.fail_tree.get_children():
                p.fail_tree.selection_set(p.fail_tree.get_children()[0])
            self.on_fail_select()
        else:
            self._details_reset()

    def open_folder(self, path: str | Path) -> None:
        try:
            open_in_file_manager(Path(path))
        except OSError as exc:
            show_problem(self._dialog_parent(), "無法開啟這個資料夾。", f"請自己用檔案總管前往：{path}", str(exc))

    def open_output(self) -> None:
        if self.run_info is not None:
            self.open_folder(self.run_info.output)

    def start_over(self) -> None:
        if self.organizing:
            return
        if self.items and not messagebox.askyesno(
            APP_NAME, f"還有 {len(self.items)} 個檔案沒有整理，離開後這些確認結果會清除。\n要整理其他資料夾嗎？"
        ):
            return
        self._set_items([])
        self.run_info = None
        self.plan = None
        self.session_source = None
        self.refresh_tree()
        self.page1.show_notice("")
        self.show_step(1)
        self.refresh_folder_stats()

    def remove_originals_here(self) -> None:
        info = self.run_info
        if info is not None and info.log_path is not None and info.log_path.exists():
            self.remove_originals_log(info.log_path)

    # ------------------------------------------------------------------ 整理紀錄：移除複本、處理原檔
    def log_dir(self) -> Path:
        return LOG_DIR

    def _dialog_parent(self):
        win = self.records_win
        if win is not None:
            try:
                if win.winfo_exists():
                    return win
            except tk.TclError:
                pass
        return self.root

    def open_records(self) -> None:
        if self.organizing:
            self.status_var.set("正在處理檔案，完成後才能開啟整理紀錄。")
            return
        win = self.records_win
        try:
            if win is not None and win.winfo_exists():
                win.lift()
                win.refresh()
                return
        except tk.TclError:
            pass
        self.records_win = RecordsDialog(self.root, self.log_dir, self.undo_log, self.remove_originals_log,
                                         self.open_folder, self.set_aside)

    def set_aside(self, path: Path) -> None:
        parent = self._dialog_parent()
        if not messagebox.askyesno(
            APP_NAME,
            f"要把整理紀錄「{Path(path).name}」移到旁邊嗎？\n會改名為「無法讀取_…」，內容保留；檔案本身都不會被更動。",
            parent=parent,
        ):
            return
        try:
            set_aside_log(Path(path))
        except OSError as exc:
            show_problem(parent, "無法移動這份紀錄。", "請確認紀錄檔沒有被其他程式開啟。", str(exc))

    def _review_log(self, path: Path, mode: str):
        """讀取紀錄、預檢，並讓使用者看過完整清單後確認。回傳確認過的紀錄快照，取消或不能處理回傳 None。"""
        parent = self._dialog_parent()
        if self.organizing or self.analysis_running():
            messagebox.showinfo(APP_NAME, "正在處理其他檔案，請等它完成後再操作。", parent=parent)
            return None
        try:
            snap = snapshot_log(Path(path))
        except (OSError, ValueError) as exc:
            show_problem(parent, "無法讀取這份整理紀錄。",
                         "可以在「整理紀錄」選這一份，按「移到旁邊」，再處理其他紀錄。檔案本身都不會被更動。", str(exc))
            return None
        if not snap.rows:
            messagebox.showinfo(APP_NAME, "這份紀錄裡沒有可以處理的項目（可能是空的或只寫了一半）。"
                                          "可以在「整理紀錄」按「移到旁邊」。", parent=parent)
            return None
        classified = classify_rows(snap.rows, mode)
        active = [row for row, note in classified if note is None]
        kept = len(classified) - len(active)
        source, output = log_roots(snap.rows)
        if not active:
            reasons = "\n".join(sorted({note for _, note in classified if note})[:6])
            messagebox.showinfo(APP_NAME, f"這份紀錄裡沒有符合條件、可以處理的檔案。\n\n{reasons}", parent=parent)
            return None
        when = datetime.fromtimestamp(Path(path).stat().st_mtime).strftime("%Y-%m-%d %H:%M")
        key = "新路徑" if mode == "undo" else "原始路徑"
        base = output if mode == "undo" else source
        lines = []
        for row, note in classified:
            target = Path(row[key])
            try:
                name = str(target.relative_to(base))
            except ValueError:
                name = str(target)
            lines.append(name if note is None else f"{name}　（{note}）")
        if mode == "undo":
            head = (f"要移除 {when} 這次整理建立的複本嗎？\n來源資料夾：{source}\n輸出資料夾：{output}\n\n"
                    f"會處理 {len(active)} 個複本（移到資源回收筒），\n原檔不會被動到。")
            ok_text, list_title = "移除這次建立的複本", "會移除的複本（含不處理的原因）"
        else:
            head = (f"要把 {when} 整理的原檔移到資源回收筒嗎？\n來源資料夾：{source}\n輸出資料夾：{output}\n\n"
                    f"會處理 {len(active)} 個原檔。\n"
                    "・只有跟複本內容完全相同的原檔才會移除，而且可以從資源回收筒救回\n"
                    "・資源回收筒裡的檔名會多一個「(整理前)」標記，還原後改回即可\n"
                    "・移除之後，這次整理就不能再用「移除這次建立的複本」還原\n"
                    "・建議先打開輸出資料夾確認結果")
            ok_text, list_title = "將原檔移到回收筒", "會移到資源回收筒的原檔（含不處理的原因）"
        if kept:
            head += f"\n\n另有 {kept} 個不符合條件，會保留不動。"
        warning = None
        current = self.folder_var.get().strip()
        if current and not _same_folder(source, current):
            warning = (f"這份紀錄的來源資料夾（{source}）跟你目前選擇的資料夾（{current}）不同。"
                       "請確認清單中的檔案就是你要處理的。")
        if not confirm_with_list(parent, APP_NAME, head, lines, ok_text, warning=warning, danger=True,
                                 list_title=list_title):
            return None
        return snap

    def undo_log(self, path: Path) -> None:
        snap = self._review_log(path, "undo")
        if snap is not None:
            self._start_log_task(snap, undo, "undone", "正在移除複本⋯")

    def remove_originals_log(self, path: Path) -> None:
        snap = self._review_log(path, "remove")
        if snap is not None:
            self._start_log_task(snap, remove_originals, "removed", "正在把原檔移到資源回收筒⋯")

    def _start_log_task(self, snap, func, kind: str, status: str) -> None:
        self.organizing = True
        self._run_id += 1
        self.status_var.set(status)
        self.status_bar.show_progress(True)
        self._progress_indeterminate()
        self._set_busy(True)
        threading.Thread(target=self._log_worker, daemon=True, args=(self._run_id, snap, func, kind)).start()

    def _log_worker(self, run_id: int, snap, func, kind: str) -> None:
        """在背景執行「移除複本」或「移除原檔」；只執行使用者確認過的那一份紀錄內容。"""
        try:
            self.queue.put((kind, run_id, func(snap.path, expected_digest=snap.digest)))
        except Exception as exc:  # noqa: BLE001
            self.queue.put(("error", run_id, str(exc) if isinstance(exc, ValueError) else f"{type(exc).__name__}: {exc}",
                            traceback.format_exc()))

    def _finish_log_task(self, kind: str, result) -> None:
        self.organizing = False
        self.status_bar.show_progress(False)
        self._set_busy(False)
        if kind == "undone":
            self.status_var.set(f"已移除 {result.done} 個複本")
            title = f"已移除 {result.done} 個複本，原檔都在原位。"
            hint = "再開啟一次「整理紀錄」重試這些檔案"
        else:
            self.status_var.set(f"已把 {result.done} 個原檔移到資源回收筒")
            title = f"已把 {result.done} 個原檔移到資源回收筒（可從資源回收筒救回）。"
            hint = "這些原檔都沒有被移除；排除原因後可以再試一次"
        message = title
        if result.warnings:
            message += "\n\n注意：\n" + "\n".join(result.warnings[:8])
        if result.errors:
            message += (f"\n\n{len(result.errors)} 個失敗（{hint}）：\n"
                        + "\n".join(f"{p.name}：{e}" for p, e in result.errors[:8]))
        win = self._dialog_parent()
        messagebox.showinfo(APP_NAME, message, parent=win)
        if self.records_win is not None:
            try:
                if self.records_win.winfo_exists():
                    self.records_win.refresh()
            except tk.TclError:
                pass
        self._refresh_step_controls()

    # ------------------------------------------------------------------ 其他
    def log_error(self, text: str) -> None:
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            with open(LOG_DIR / "error.log", "a", encoding="utf-8") as f:
                f.write(f"\n[{datetime.now():%Y-%m-%d %H:%M:%S}]\n{text}\n")
        except OSError:
            pass

    def _show_error(self, message: str, details: str = "") -> None:
        """同一時間只顯示一個錯誤視窗，避免錯誤視窗一直疊出來。"""
        if self._error_dialog_open:
            self.status_var.set(message.replace("\n", " ")[:200])
            return
        self._error_dialog_open = True
        try:
            show_problem(self._dialog_parent(), f"發生問題：{message}",
                         "詳細內容已記錄在 logs\\error.log。如果需要回報問題，請附上這個檔案。", details)
        finally:
            self._error_dialog_open = False

    def on_tk_error(self, exc_type, exc, tb) -> None:
        details = "".join(traceback.format_exception(exc_type, exc, tb))
        self.log_error(details)
        self._show_error(f"程式發生非預期的錯誤（{exc}）", details)

    def shutdown(self) -> None:
        """關閉前取消所有排程中的工作（避免視窗銷毀後還有計時器在跑）。"""
        self.stop_event.set()
        try:
            for job in self.root.tk.splitlist(self.root.tk.call("after", "info")):
                self.root.after_cancel(job)
        except tk.TclError:
            pass

    def on_close(self) -> None:
        if self.organizing:
            messagebox.showinfo(APP_NAME, "正在處理檔案，請等待完成後再關閉程式。")
            return
        self.stop_event.set()
        self._save_sash()
        self.settings.update(
            last_folder=self.folder_var.get().strip(), include_subfolders=self.subfolders_var.get(),
            output_dir=self._output_setting(), rename_mode=self.rename_mode_var.get(),
            rename_pattern=self.pattern_var.get().strip() or self.settings["rename_pattern"],
            split_ratio=self._sash_ratio,
        )
        try:
            save_settings(self.settings)
        except OSError:
            pass
        self.shutdown()
        self.root.destroy()


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
