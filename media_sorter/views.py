"""畫面：外框（步驟列、狀態列）與四個步驟的版面。

這個檔案只負責「長什麼樣子」與元件的排列；按鈕按下去做什麼都在 app.py。
每個步驟的底部操作列固定在視窗最下方，內容太多時只有中間的部分會捲動，
所以視窗再小，主要按鈕也不會被擠出視窗外。
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from .config import RENAME_MODES

STEP_NAMES = ["選擇資料夾", "確認分類", "預覽整理結果", "完成"]
FILTERS = ["全部", "待確認", "需檢查", "建議較明確", "已確認", "已略過", "讀取失敗"]
SUGGEST_FILTER_PREFIX = "建議分類："
SKIP = "__skip__"
_SCROLL_IGNORE = {"Treeview", "Text", "Listbox", "TCombobox", "TSpinbox"}


def autowrap(label: ttk.Label, margin: int = 0) -> ttk.Label:
    """文字寬度跟著容器變化（視窗縮小時自動換行，不會被切掉）。"""
    label.bind("<Configure>", lambda e: label.configure(wraplength=max(120, e.width - margin)))
    return label


class ScrollFrame(ttk.Frame):
    """可以上下捲動的區域（內容不夠高時不顯示捲軸）。內容請放在 .inner。"""

    def __init__(self, master, background: str):
        super().__init__(master)
        self.canvas = tk.Canvas(self, highlightthickness=0, borderwidth=0, background=background)
        self.vbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.inner = ttk.Frame(self.canvas)
        self._window = self.canvas.create_window(0, 0, window=self.inner, anchor="nw")
        self.canvas.configure(yscrollcommand=self._on_scroll)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.vbar.grid(row=0, column=1, sticky="ns")
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)
        self.inner.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self._window, width=e.width))
        self._wheel_bound = False
        self.bind("<Map>", self._bind_wheel)

    def _on_scroll(self, first: str, last: str) -> None:
        self.vbar.set(first, last)
        if float(first) <= 0.0 and float(last) >= 1.0:
            self.vbar.grid_remove()
        else:
            self.vbar.grid()

    def _bind_wheel(self, _event=None) -> None:
        if not self._wheel_bound:  # 全域綁定一次；滑鼠是否在這個區域內，在處理時判斷
            self._wheel_bound = True
            for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
                self.bind_all(sequence, self._wheel, add="+")

    def _pointer_inside(self, event) -> bool:
        try:
            widget = self.winfo_containing(event.x_root, event.y_root)
        except (KeyError, tk.TclError):
            return False
        while widget is not None and widget is not self:
            widget = getattr(widget, "master", None)
        return widget is self

    def _wheel(self, event) -> None:
        try:
            if not self.winfo_ismapped() or not self._pointer_inside(event):
                return
            if getattr(event.widget, "winfo_class", lambda: "")() in _SCROLL_IGNORE:
                return
            first, last = self.canvas.yview()
            if first <= 0.0 and last >= 1.0:
                return
            if getattr(event, "num", 0) == 4 or getattr(event, "delta", 0) > 0:
                self.canvas.yview_scroll(-3, "units")
            else:
                self.canvas.yview_scroll(3, "units")
        except tk.TclError:  # 視窗已經關閉
            pass

    def scroll_to_top(self) -> None:
        self.canvas.yview_moveto(0)


def card(parent, app, title: str | None = None, hint: str | None = None) -> ttk.Frame:
    """白底的內容區塊。"""
    px = app.theme.px
    frame = ttk.Frame(parent, style="Card.TFrame", padding=(px(18), px(14)))
    frame.pack(fill="x", pady=(0, px(12)))
    if title:
        ttk.Label(frame, text=title, style="CardHeading.TLabel").pack(anchor="w")
    if hint:
        autowrap(ttk.Label(frame, text=hint, style="CardHint.TLabel", justify="left")).pack(
            anchor="w", fill="x", pady=(px(2), 0))
    return frame


def bottom_bar(page, app) -> ttk.Frame:
    """步驟底部固定的操作列（永遠在視窗最下方）。"""
    px = app.theme.px
    ttk.Separator(page).pack(side="bottom", fill="x")
    bar = ttk.Frame(page, style="Bar.TFrame", padding=(px(20), px(10)))
    bar.pack(side="bottom", fill="x")
    return bar


# ---------------------------------------------------------------------------- 外框
class StepHeader(ttk.Frame):
    """最上方：程式名稱、步驟列、常用功能。"""

    def __init__(self, master, app):
        super().__init__(master, style="Header.TFrame")
        px = app.theme.px
        self.app = app
        top = ttk.Frame(self, style="Header.TFrame", padding=(px(18), px(8), px(10), 0))
        top.pack(fill="x")
        ttk.Label(top, text="AI 媒體分類器", style="AppTitle.TLabel").pack(side="left")
        self.help_btn = ttk.Button(top, text="說明", style="Header.TButton", command=app.open_help)
        self.settings_btn = ttk.Button(top, text="設定", style="Header.TButton", command=app.open_settings_dialog)
        self.category_btn = ttk.Button(top, text="分類設定", style="Header.TButton", command=app.open_category_dialog)
        self.records_btn = ttk.Button(top, text="整理紀錄", style="Header.TButton", command=app.open_records)
        for button in (self.help_btn, self.settings_btn, self.category_btn, self.records_btn):
            button.pack(side="right")

        steps = ttk.Frame(self, style="Header.TFrame", padding=(px(18), px(2), px(18), px(8)))
        steps.pack(fill="x")
        self.labels: list[ttk.Label] = []
        for i, name in enumerate(STEP_NAMES, 1):
            if i > 1:
                ttk.Label(steps, text="›", style="StepTodo.TLabel").pack(side="left", padx=px(8))
            label = ttk.Label(steps, text=f"{i}　{name}", style="StepTodo.TLabel", cursor="hand2")
            label.pack(side="left")
            label.bind("<Button-1>", lambda e, n=i: app.show_step(n))
            self.labels.append(label)
        ttk.Separator(self).pack(side="bottom", fill="x")

    def update_steps(self, current: int, available: set[int]) -> None:
        for i, label in enumerate(self.labels, 1):
            name = STEP_NAMES[i - 1]
            if i == current:
                label.configure(text=f"{i}　{name}", style="StepNow.TLabel", cursor="")
            elif i < current:
                label.configure(text=f"✓　{name}", style="StepDone.TLabel", cursor="hand2" if i in available else "")
            else:
                label.configure(text=f"{i}　{name}", style="StepTodo.TLabel", cursor="hand2" if i in available else "")

    def set_busy(self, busy: bool) -> None:
        for button in (self.records_btn, self.category_btn, self.settings_btn):
            button.state(["disabled"] if busy else ["!disabled"])


class StatusBar(ttk.Frame):
    def __init__(self, master, app):
        super().__init__(master, style="Bar.TFrame", padding=(app.theme.px(18), app.theme.px(4)))
        self.status_var = app.status_var
        ttk.Label(self, textvariable=self.status_var, style="BarHint.TLabel").pack(side="left", fill="x", expand=True)
        self.progress = ttk.Progressbar(self, mode="determinate", length=app.theme.px(220))
        self._shown = False

    def show_progress(self, shown: bool) -> None:
        if shown and not self._shown:
            self.progress.pack(side="right")
        elif not shown and self._shown:
            self.progress.stop()
            self.progress.pack_forget()
        self._shown = shown


# ---------------------------------------------------------------------------- 步驟 1：選擇資料夾
class FolderPage(ttk.Frame):
    def __init__(self, master, app):
        super().__init__(master)
        px = app.theme.px
        self.app = app
        bar = bottom_bar(self, app)
        self.start_btn = ttk.Button(bar, text="開始辨識", style="Accent.TButton", command=app.toggle_analysis)
        self.start_btn.pack(side="right")
        self.hint_var = tk.StringVar()
        autowrap(ttk.Label(bar, textvariable=self.hint_var, style="BarHint.TLabel", justify="left"), px(200)).pack(
            side="left", fill="x", expand=True, padx=(0, px(12)))

        self.scroll = ScrollFrame(self, app.theme.bg)
        self.scroll.pack(fill="both", expand=True)
        body = self.scroll.inner
        body.configure(padding=(px(24), px(18)))
        ttk.Label(body, text="選擇要整理的資料夾", style="Heading.TLabel").pack(anchor="w")
        autowrap(ttk.Label(body, style="Hint.TLabel", justify="left",
                           text="AI 會看每張照片與影片，建議它「看起來像什麼」；你確認之後，才會把檔案複製到分類資料夾。"),
                 ).pack(anchor="w", fill="x", pady=(px(4), px(14)))

        folder = card(body, app, "資料夾")
        row = ttk.Frame(folder, style="Card.TFrame")
        row.pack(fill="x", pady=(px(8), 0))
        self.entry = ttk.Entry(row, textvariable=app.folder_var)
        self.entry.pack(side="left", fill="x", expand=True)
        self.browse_btn = ttk.Button(row, text="選擇資料夾…", command=app.choose_folder)
        self.browse_btn.pack(side="left", padx=(px(8), 0))
        self.sub_check = ttk.Checkbutton(folder, text="包含子資料夾", variable=app.subfolders_var,
                                         style="Card.TCheckbutton")
        self.sub_check.pack(anchor="w", pady=(px(8), 0))

        found = card(body, app, "找到的檔案")
        self.found_body = ttk.Frame(found, style="Card.TFrame")
        self.found_body.pack(fill="x", pady=(px(8), 0))
        self.empty_var = tk.StringVar()
        self.empty_label = autowrap(ttk.Label(self.found_body, textvariable=self.empty_var, style="CardHint.TLabel",
                                              justify="left"))
        self.empty_label.pack(anchor="w", fill="x")
        self.numbers = ttk.Frame(self.found_body, style="Card.TFrame")
        self.count_vars = {key: tk.StringVar(value="0") for key in ("images", "videos", "other")}
        for col, (key, caption) in enumerate((("images", "圖片"), ("videos", "影片"), ("other", "不支援（會略過）"))):
            cell = ttk.Frame(self.numbers, style="Card.TFrame")
            cell.grid(row=0, column=col, sticky="w", padx=(0, px(36)))
            ttk.Label(cell, textvariable=self.count_vars[key], style="CardTitle.TLabel").pack(anchor="w")
            ttk.Label(cell, text=caption, style="CardHint.TLabel").pack(anchor="w")
        self.found_note_var = tk.StringVar()
        self.found_note = autowrap(ttk.Label(found, textvariable=self.found_note_var, style="CardHint.TLabel",
                                             justify="left"))
        self.found_note.pack(anchor="w", fill="x", pady=(px(8), 0))

        self.notice_frame = ttk.Frame(body, style="Warn.TFrame", padding=px(12))
        self.notice_var = tk.StringVar()
        autowrap(ttk.Label(self.notice_frame, textvariable=self.notice_var, style="Warn.TLabel", justify="left"),
                 px(24)).pack(fill="x")

        nxt = card(body, app, "接下來會發生什麼")
        autowrap(ttk.Label(nxt, style="Card.TLabel", justify="left", text=(
            "1. AI 逐一判斷每個檔案「看起來像什麼」（只是建議，不一定正確）\n"
            "2. 你逐一確認或修改分類\n"
            "3. 預覽整理結果之後，才會把檔案「複製」到分類資料夾——原檔不會被移動或修改"))
        ).pack(anchor="w", fill="x", pady=(px(6), 0))
        self.model_var = tk.StringVar()
        self.model_label = autowrap(ttk.Label(nxt, textvariable=self.model_var, style="CardHint.TLabel",
                                              justify="left"))
        self.model_label.pack(anchor="w", fill="x", pady=(px(10), 0))

    def show_notice(self, text: str) -> None:
        self.notice_var.set(text)
        if text:
            self.notice_frame.pack(fill="x", pady=(0, self.app.theme.px(12)), after=self.found_note.master)
        else:
            self.notice_frame.pack_forget()

    def show_counts(self, images: int, videos: int, other: int, note: str) -> None:
        self.empty_label.pack_forget()
        self.numbers.pack(anchor="w", fill="x")
        for key, value in (("images", images), ("videos", videos), ("other", other)):
            self.count_vars[key].set(f"{value:,}")
        self.found_note_var.set(note)

    def show_empty(self, text: str) -> None:
        self.numbers.pack_forget()
        self.empty_var.set(text)
        self.empty_label.pack(anchor="w", fill="x")
        self.found_note_var.set("")


# ---------------------------------------------------------------------------- 步驟 2：確認分類
class ReviewPage(ttk.Frame):
    """左邊檔案清單、右邊大預覽與候選分類；視窗太窄時改成兩個分頁。"""

    def __init__(self, master, app):
        super().__init__(master)
        px = app.theme.px
        self.app = app
        self.narrow = False

        bar = bottom_bar(self, app)
        self.back_btn = ttk.Button(bar, text="← 上一步", command=lambda: app.show_step(1))
        self.back_btn.pack(side="left")
        self.next_btn = ttk.Button(bar, text="下一步：預覽整理結果 →", style="Accent.TButton",
                                   command=lambda: app.show_step(3))
        self.next_btn.pack(side="right")
        self.count_var = tk.StringVar()
        autowrap(ttk.Label(bar, textvariable=self.count_var, style="BarHint.TLabel", justify="left"), px(40)).pack(
            side="left", fill="x", expand=True, padx=px(14))

        self.toolbar = ttk.Frame(self, padding=(px(16), px(6), px(16), 0))
        self.analysis_var = tk.StringVar()
        ttk.Label(self.toolbar, textvariable=self.analysis_var, style="Hint.TLabel").pack(side="left")
        self.stop_btn = ttk.Button(self.toolbar, text="停止辨識", command=app.stop_analysis)
        self.stop_btn.pack(side="right")

        self.body = ttk.Frame(self, padding=(px(12), px(8), px(12), px(8)))
        self.body.pack(fill="both", expand=True)
        self.paned = ttk.PanedWindow(self.body, orient="horizontal")
        self.notebook = ttk.Notebook(self.body)
        self.list_frame = ttk.Frame(self.body, style="Card.TFrame", padding=px(10))
        self.detail_frame = ttk.Frame(self.body, style="Card.TFrame", padding=px(10))
        self._build_list(self.list_frame)
        self._build_detail(self.detail_frame)
        self.set_narrow(False, force=True)

    # -- 左邊清單
    def _build_list(self, parent) -> None:
        app, px = self.app, self.app.theme.px
        row = ttk.Frame(parent, style="Card.TFrame")
        row.pack(fill="x")
        ttk.Label(row, text="搜尋", style="Card.TLabel").pack(side="left")
        self.search_entry = ttk.Entry(row, textvariable=app.search_var)
        self.search_entry.pack(side="left", fill="x", expand=True, padx=(px(6), 0))
        filter_row = ttk.Frame(parent, style="Card.TFrame")
        filter_row.pack(fill="x", pady=(px(6), px(6)))
        ttk.Label(filter_row, text="顯示", style="Card.TLabel").pack(side="left")
        self.filter_box = ttk.Combobox(filter_row, textvariable=app.filter_var, state="readonly", values=FILTERS,
                                       width=14)
        self.filter_box.pack(side="left", fill="x", expand=True, padx=(px(6), 0))
        self.filter_box.bind("<<ComboboxSelected>>", lambda e: app.refresh_tree())

        # 底部的批次操作列先排（視窗矮時才不會被清單擠掉）
        batch = self.batch_frame = ttk.Frame(parent, style="Card.TFrame")
        batch.pack(side="bottom", fill="x", pady=(px(8), 0))
        self.detail_btn = ttk.Button(parent, text="查看／確認選取的檔案 →", command=self.show_detail_tab)
        self.batch_label_var = tk.StringVar()
        ttk.Label(batch, textvariable=self.batch_label_var, style="CardHint.TLabel").pack(anchor="w")
        row1 = ttk.Frame(batch, style="Card.TFrame")
        row1.pack(fill="x", pady=(px(4), 0))
        self.batch_box = ttk.Combobox(row1, textvariable=app.batch_var, state="readonly", width=12)
        self.batch_box.pack(side="left", fill="x", expand=True)
        self.batch_apply_btn = ttk.Button(row1, text="套用此分類", command=app.apply_batch)
        self.batch_apply_btn.pack(side="left", padx=(px(6), 0))
        row2 = ttk.Frame(batch, style="Card.TFrame")
        row2.pack(fill="x", pady=(px(4), 0))
        self.batch_accept_btn = ttk.Button(row2, text="採用建議分類", command=app.accept_selected)
        self.batch_accept_btn.pack(side="left")
        self.batch_skip_btn = ttk.Button(row2, text="略過選取的", command=app.skip_selected)
        self.batch_skip_btn.pack(side="left", padx=(px(6), 0))
        self.batch_buttons = (self.batch_apply_btn, self.batch_accept_btn, self.batch_skip_btn)

        table = ttk.Frame(parent, style="Card.TFrame")
        table.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(table, columns=("suggest", "status"), show="tree headings", selectmode="extended",
                                 style="Thumb.Treeview")
        self.tree.heading("#0", text="檔案", command=lambda: app.sort_by("name"))
        self.tree.heading("suggest", text="建議分類", command=lambda: app.sort_by("suggest"))
        self.tree.heading("status", text="處理狀態", command=lambda: app.sort_by("status"))
        self.tree.column("#0", width=px(210), minwidth=px(120), stretch=True)
        self.tree.column("suggest", width=px(84), minwidth=px(60), stretch=False)
        self.tree.column("status", width=px(96), minwidth=px(70), stretch=False)
        scroll = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)
        theme = app.theme
        self.tree.tag_configure("confirmed", background=theme.ok_bg)
        self.tree.tag_configure("skipped", foreground="#8a919c")
        self.tree.tag_configure("error", foreground=theme.danger)
        self.tree.tag_configure("check", background=theme.warn_bg)
        self.tree.bind("<<TreeviewSelect>>", app.on_select)
        self.tree.bind("<Double-1>", lambda e: self.show_detail_tab())

    # -- 右邊預覽與候選分類
    def _build_detail(self, parent) -> None:
        app, px = self.app, self.app.theme.px
        # 底部的操作列固定不捲動
        actions = ttk.Frame(parent, style="Card.TFrame")
        actions.pack(side="bottom", fill="x", pady=(px(8), 0))
        self.shortcut_var = tk.StringVar(value="快速鍵：Enter 確認並看下一個　1／2／3 選擇建議　S 略過")
        autowrap(ttk.Label(actions, textvariable=self.shortcut_var, style="CardHint.TLabel", justify="left")).pack(
            anchor="w", fill="x", pady=(0, px(6)))
        buttons = ttk.Frame(actions, style="Card.TFrame")
        buttons.pack(fill="x")
        self.confirm_btn = ttk.Button(buttons, text="確認並看下一個", style="Accent.TButton", command=app.confirm_current)
        self.confirm_btn.pack(side="right")
        self.skip_btn = ttk.Button(buttons, text="略過此檔", command=app.skip_current)
        self.skip_btn.pack(side="right", padx=(0, px(8)))
        self.undo_btn = ttk.Button(buttons, text="撤回上一次確認", command=app.undo_last_confirm)
        self.undo_btn.pack(side="left")

        self.stack = ttk.Frame(parent, style="Card.TFrame")
        self.stack.pack(fill="both", expand=True)
        self.stack.grid_rowconfigure(0, weight=1)
        self.stack.grid_columnconfigure(0, weight=1)

        # 狀態一：還沒有選檔案
        self.placeholder = ttk.Frame(self.stack, style="Card.TFrame")
        self.placeholder.grid(row=0, column=0, sticky="nsew")
        self.placeholder_var = tk.StringVar(value="請從左邊的清單選一個檔案。")
        autowrap(ttk.Label(self.placeholder, textvariable=self.placeholder_var, style="CardHint.TLabel",
                           justify="center", anchor="center")).pack(expand=True, fill="x", padx=px(20))

        # 狀態二：全部確認完
        self.finished = ttk.Frame(self.stack, style="Card.TFrame")
        self.finished.grid(row=0, column=0, sticky="nsew")
        inner = ttk.Frame(self.finished, style="Card.TFrame")
        inner.pack(expand=True)
        ttk.Label(inner, text="✓　分類確認完成", style="CardTitle.TLabel").pack()
        self.finished_var = tk.StringVar()
        autowrap(ttk.Label(inner, textvariable=self.finished_var, style="Card.TLabel", justify="center",
                           anchor="center"), px(20)).pack(fill="x", pady=(px(8), 0))

        # 狀態三：目前檔案
        self.scroll = ScrollFrame(self.stack, app.theme.card)
        self.scroll.grid(row=0, column=0, sticky="nsew")
        self.scroll.inner.configure(style="Card.TFrame")
        d = self.scroll.inner
        d.grid_columnconfigure(0, weight=1)
        self.preview_frame = ttk.Frame(d, height=px(260), style="Preview.TFrame")
        self.preview_frame.grid(row=0, column=0, sticky="ew")
        self.preview_frame.pack_propagate(False)
        self.preview_label = ttk.Label(self.preview_frame, style="Preview.TLabel", anchor="center", cursor="hand2")
        self.preview_label.pack(fill="both", expand=True)
        self.preview_label.bind("<Double-Button-1>", lambda e: app.open_viewer())

        info = ttk.Frame(d, style="Card.TFrame")
        info.grid(row=1, column=0, sticky="ew", pady=(px(8), 0))
        info.grid_columnconfigure(0, weight=1)
        self.file_var = tk.StringVar()
        autowrap(ttk.Label(info, textvariable=self.file_var, style="Card.TLabel", justify="left",
                           font=app.theme.fonts["bold"]), px(100)).grid(row=0, column=0, sticky="ew")
        self.zoom_btn = ttk.Button(info, text="放大檢視", style="Link.TButton", command=app.open_viewer)
        self.zoom_btn.grid(row=0, column=1, sticky="e")
        self.info_var = tk.StringVar()
        autowrap(ttk.Label(info, textvariable=self.info_var, style="CardHint.TLabel", justify="left")).grid(
            row=1, column=0, columnspan=2, sticky="ew")

        self.notice_frame = ttk.Frame(d, style="Warn.TFrame", padding=px(8))
        self.notice_frame.grid(row=2, column=0, sticky="ew", pady=(px(8), 0))
        self.notice_var = tk.StringVar()
        autowrap(ttk.Label(self.notice_frame, textvariable=self.notice_var, style="Warn.TLabel", justify="left"),
                 px(20)).pack(fill="x")

        head = ttk.Frame(d, style="Card.TFrame")
        head.grid(row=3, column=0, sticky="ew", pady=(px(10), px(4)))
        ttk.Label(head, text="這看起來像：", style="CardHeading.TLabel").pack(side="left")
        self.clarity_var = tk.StringVar()
        ttk.Label(head, textvariable=self.clarity_var, style="CardHint.TLabel").pack(side="left", padx=px(8))
        self.candidates = ttk.Frame(d, style="Card.TFrame")
        self.candidates.grid(row=4, column=0, sticky="ew")
        self.candidates.grid_columnconfigure(0, weight=1)
        self.candidate_buttons: list[ttk.Button] = []
        for i in range(3):
            button = ttk.Button(self.candidates, style="Choice.TButton", command=lambda i=i: app.pick_suggestion(i))
            button.grid(row=i, column=0, sticky="ew", pady=(0, px(4)))
            self.candidate_buttons.append(button)

        other = ttk.Frame(d, style="Card.TFrame")
        other.grid(row=5, column=0, sticky="ew", pady=(px(4), 0))
        other.grid_columnconfigure(1, weight=1)
        ttk.Label(other, text="都不是？選其他分類", style="Card.TLabel").grid(row=0, column=0, sticky="w")
        self.other_box = ttk.Combobox(other, textvariable=app.other_var, width=16)
        self.other_box.grid(row=0, column=1, sticky="ew", padx=(px(8), px(8)))
        self.other_box.bind("<<ComboboxSelected>>", app.on_other_selected)
        self.other_box.bind("<Return>", app.on_other_selected)
        self.other_box.bind("<KeyRelease>", app.filter_other_list)
        ttk.Button(other, text="新增分類…", command=app.add_category_prompt).grid(row=0, column=2)
        self.other_hint_var = tk.StringVar()
        ttk.Label(other, textvariable=self.other_hint_var, style="CardHint.TLabel").grid(
            row=1, column=0, columnspan=3, sticky="w")

        self.tags_frame = ttk.Frame(d, style="Card.TFrame")
        self.tags_frame.grid(row=6, column=0, sticky="ew", pady=(px(8), 0))
        self.view_state = "placeholder"
        self.show_state("placeholder")
        self.detail_frame.bind("<Configure>", self._resize_preview)

    def _resize_preview(self, event=None) -> None:
        px = self.app.theme.px
        height = event.height if event is not None else self.detail_frame.winfo_height()
        self.preview_frame.configure(height=max(px(130), min(px(520), int(height * 0.34))))
        self.app.schedule_preview_render()

    def show_toolbar(self, shown: bool) -> None:
        if shown:
            self.toolbar.pack(side="top", fill="x", before=self.body)
        else:
            self.toolbar.pack_forget()

    def show_state(self, state: str) -> None:
        """state：detail（目前檔案）／placeholder／finished"""
        {"detail": self.scroll, "placeholder": self.placeholder, "finished": self.finished}[state].tkraise()
        self.view_state = state

    # -- 寬窄版面
    def set_narrow(self, narrow: bool, force: bool = False) -> None:
        if narrow == self.narrow and not force:
            return
        self.narrow = narrow
        for widget, children in ((self.paned, self.paned.panes), (self.notebook, self.notebook.tabs)):
            for pane in list(children()):
                try:
                    widget.forget(pane)
                except tk.TclError:
                    pass
            widget.pack_forget()
        if narrow:
            self.notebook.pack(fill="both", expand=True)
            self.notebook.add(self.list_frame, text="檔案清單")
            self.notebook.add(self.detail_frame, text="目前檔案")
            self.list_frame.lift(self.notebook)
            self.detail_frame.lift(self.notebook)
            self.detail_btn.pack(side="bottom", fill="x", pady=(self.app.theme.px(8), 0), after=self.batch_frame)
        else:
            self.detail_btn.pack_forget()
            self.paned.pack(fill="both", expand=True)
            self.paned.add(self.list_frame, weight=1)
            self.paned.add(self.detail_frame, weight=1)
            self.list_frame.lift(self.paned)
            self.detail_frame.lift(self.paned)

    def show_detail_tab(self) -> None:
        if self.narrow:
            self.notebook.select(self.detail_frame)

    def sash_ratio(self) -> float | None:
        if self.narrow:
            return None
        width = self.paned.winfo_width()
        try:
            position = self.paned.sashpos(0)
        except tk.TclError:
            return None
        return position / width if width > 50 else None

    def set_sash_ratio(self, ratio: float) -> None:
        if self.narrow:
            return
        width = self.paned.winfo_width()
        if width > 50:
            try:
                self.paned.sashpos(0, int(width * ratio))
            except tk.TclError:
                pass


# ---------------------------------------------------------------------------- 步驟 3：預覽整理結果
class PlanPage(ttk.Frame):
    def __init__(self, master, app):
        super().__init__(master)
        px = app.theme.px
        self.app = app
        bar = bottom_bar(self, app)
        self.back_btn = ttk.Button(bar, text="← 回到確認分類", command=lambda: app.show_step(2))
        self.back_btn.pack(side="left")
        self.copy_btn = ttk.Button(bar, text="開始複製", style="Accent.TButton", command=app.toggle_copy)
        self.copy_btn.pack(side="right")
        self.hint_var = tk.StringVar()
        autowrap(ttk.Label(bar, textvariable=self.hint_var, style="BarHint.TLabel", justify="left"), px(40)).pack(
            side="left", fill="x", expand=True, padx=px(14))

        self.scroll = ScrollFrame(self, app.theme.bg)
        self.scroll.pack(fill="both", expand=True)
        body = self.scroll.inner
        body.configure(padding=(px(24), px(18)))
        head = ttk.Frame(body)
        head.pack(fill="x")
        ttk.Label(head, text="預覽整理結果", style="Heading.TLabel").pack(anchor="w")
        self.intro = autowrap(ttk.Label(
            head, style="Hint.TLabel", justify="left",
            text="下面就是實際會做的事。按下「開始複製」之前，不會有任何檔案被建立或改動；原檔一律不會被移動或修改。"))
        self.intro.pack(anchor="w", fill="x", pady=(px(4), px(14)))
        self.problem_frame = ttk.Frame(head, style="Danger.TFrame", padding=px(12))
        self.problem_var = tk.StringVar()
        autowrap(ttk.Label(self.problem_frame, textvariable=self.problem_var, style="Danger.TLabel", justify="left"),
                 px(24)).pack(fill="x")

        # 視窗夠寬時分成左右兩欄：左邊設定，右邊整理前後對照
        self.cols = ttk.Frame(body)
        self.cols.pack(fill="both", expand=True)
        self.left = ttk.Frame(self.cols)
        self.right = ttk.Frame(self.cols)
        self.wide = None

        files = card(self.left, app, "要整理的檔案")
        self.summary_var = tk.StringVar()
        autowrap(ttk.Label(files, textvariable=self.summary_var, style="Card.TLabel", justify="left"),
                 ).pack(anchor="w", fill="x", pady=(px(6), 0))
        self.excluded_var = tk.StringVar()
        autowrap(ttk.Label(files, textvariable=self.excluded_var, style="CardHint.TLabel", justify="left"),
                 ).pack(anchor="w", fill="x", pady=(px(2), 0))
        self.include_check = ttk.Checkbutton(files, variable=app.include_pending_var, style="Card.TCheckbutton",
                                             command=app.refresh_plan)
        self.include_check.pack(anchor="w", pady=(px(6), 0))

        out = card(self.left, app, "輸出位置")
        row = ttk.Frame(out, style="Card.TFrame")
        row.pack(fill="x", pady=(px(8), 0))
        self.output_entry = ttk.Entry(row, textvariable=app.output_var)
        self.output_entry.pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="瀏覽…", command=app.choose_output).pack(side="left", padx=(px(8), 0))
        self.space_var = tk.StringVar()
        self.space_label = autowrap(ttk.Label(out, textvariable=self.space_var, style="CardHint.TLabel",
                                              justify="left"))
        self.space_label.pack(anchor="w", fill="x", pady=(px(6), 0))

        naming = card(self.left, app, "檔案名稱")
        self.radios: list[ttk.Radiobutton] = []
        grid = ttk.Frame(naming, style="Card.TFrame")
        grid.pack(fill="x", pady=(px(4), 0))
        for i, (key, (label, _pattern)) in enumerate(RENAME_MODES.items()):
            radio = ttk.Radiobutton(grid, text=label, value=key, variable=app.rename_mode_var,
                                    style="Card.TRadiobutton", command=app.refresh_plan)
            radio.grid(row=i // 2, column=i % 2, sticky="w", padx=(0, px(24)), pady=(px(4), 0))
            self.radios.append(radio)
        self.custom_row = ttk.Frame(naming, style="Card.TFrame")
        self.custom_row.pack(fill="x", padx=(px(24), 0), pady=(px(4), 0))
        self.pattern_entry = ttk.Entry(self.custom_row, textvariable=app.pattern_var)
        self.pattern_entry.pack(side="left", fill="x", expand=True)
        ttk.Label(self.custom_row, text="可用：{分類} {日期} {序號} {原檔名}", style="CardHint.TLabel").pack(
            side="left", padx=(px(8), 0))

        preview = card(self.right, app, "整理前後對照")
        self.counts_var = tk.StringVar()
        autowrap(ttk.Label(preview, textvariable=self.counts_var, style="Card.TLabel", justify="left")).pack(
            anchor="w", fill="x", pady=(px(4), px(6)))
        frame = ttk.Frame(preview, style="Card.TFrame")
        frame.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(frame, columns=("src", "dst"), show="headings", height=10, selectmode="browse")
        self.tree.heading("src", text="原檔名")
        self.tree.heading("dst", text="複製到（分類資料夾＼新檔名）")
        self.tree.column("src", width=px(200), stretch=True)
        self.tree.column("dst", width=px(240), stretch=True)
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree_note_var = tk.StringVar()
        ttk.Label(preview, textvariable=self.tree_note_var, style="CardHint.TLabel").pack(anchor="w", pady=(px(4), 0))
        self.set_wide(False)

    def set_wide(self, wide: bool) -> None:
        if wide == self.wide:
            return
        self.wide = wide
        gap = self.app.theme.px(8)
        for col, weight in ((0, 1), (1, 1 if wide else 0)):
            self.cols.grid_columnconfigure(col, weight=weight, uniform="plan" if wide else "")
        if wide:
            self.left.grid(row=0, column=0, sticky="new", padx=(0, gap))
            self.right.grid(row=0, column=1, sticky="new", padx=(gap, 0))
        else:
            self.left.grid(row=0, column=0, sticky="new", padx=0)
            self.right.grid(row=1, column=0, sticky="new", padx=0)

    def show_problem(self, text: str) -> None:
        self.problem_var.set(text)
        if text:
            self.problem_frame.pack(fill="x", pady=(0, self.app.theme.px(12)))
        else:
            self.problem_frame.pack_forget()


# ---------------------------------------------------------------------------- 步驟 4：完成
class DonePage(ttk.Frame):
    def __init__(self, master, app):
        super().__init__(master)
        px = app.theme.px
        self.app = app
        bar = bottom_bar(self, app)
        self.open_btn = ttk.Button(bar, text="開啟輸出資料夾", style="Accent.TButton", command=app.open_output)
        self.open_btn.pack(side="right")
        self.again_btn = ttk.Button(bar, text="整理其他資料夾", command=app.start_over)
        self.again_btn.pack(side="left")
        self.back_btn = ttk.Button(bar, text="回到確認分類", command=lambda: app.show_step(2))

        self.scroll = ScrollFrame(self, app.theme.bg)
        self.scroll.pack(fill="both", expand=True)
        body = self.scroll.inner
        body.configure(padding=(px(24), px(18)))
        self.banner = ttk.Frame(body, style="Ok.TFrame", padding=(px(18), px(14)))
        self.banner.pack(fill="x", pady=(0, px(12)))
        self.banner_title = tk.StringVar()
        self.banner_detail = tk.StringVar()
        self.banner_title_label = ttk.Label(self.banner, textvariable=self.banner_title, style="Ok.TLabel",
                                            font=app.theme.fonts["heading"])
        self.banner_title_label.pack(anchor="w")
        self.banner_detail_label = autowrap(ttk.Label(self.banner, textvariable=self.banner_detail, style="Ok.TLabel",
                                                      justify="left"), px(40))
        self.banner_detail_label.pack(anchor="w", fill="x", pady=(px(4), 0))

        self.result = card(body, app, "結果")
        self.result_var = tk.StringVar()
        autowrap(ttk.Label(self.result, textvariable=self.result_var, style="Card.TLabel", justify="left")).pack(
            anchor="w", fill="x", pady=(px(6), 0))

        self.fail_card = ttk.Frame(body, style="Card.TFrame", padding=(px(18), px(14)))
        ttk.Label(self.fail_card, text="需要處理的檔案", style="CardHeading.TLabel").pack(anchor="w")
        self.fail_hint_var = tk.StringVar()
        autowrap(ttk.Label(self.fail_card, textvariable=self.fail_hint_var, style="CardHint.TLabel", justify="left"),
                 ).pack(anchor="w", fill="x", pady=(px(2), px(6)))
        frame = ttk.Frame(self.fail_card, style="Card.TFrame")
        frame.pack(fill="x")
        self.fail_tree = ttk.Treeview(frame, columns=("name", "why"), show="headings", height=6, selectmode="browse")
        self.fail_tree.heading("name", text="檔案")
        self.fail_tree.heading("why", text="發生什麼事、可以怎麼做")
        self.fail_tree.column("name", width=px(200), stretch=False)
        self.fail_tree.column("why", width=px(420), stretch=True)
        sb = ttk.Scrollbar(frame, orient="vertical", command=self.fail_tree.yview)
        self.fail_tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.fail_tree.pack(side="left", fill="x", expand=True)
        self.fail_tree.bind("<<TreeviewSelect>>", app.on_fail_select)
        buttons = ttk.Frame(self.fail_card, style="Card.TFrame")
        buttons.pack(fill="x", pady=(px(8), 0))
        self.retry_btn = ttk.Button(buttons, text="重試", command=app.toggle_retry)
        self.retry_btn.pack(side="left")
        self.details_btn = ttk.Button(buttons, text="顯示技術細節", command=app.toggle_fail_details)
        self.details_btn.pack(side="left", padx=(px(8), 0))
        self.details_text = tk.Text(self.fail_card, height=4, wrap="word", relief="flat", borderwidth=1,
                                    highlightthickness=1, state="disabled")
        self.details_shown = False

        self.later = card(body, app, "之後的處理（可選）",
                          "整理只會「複製」檔案，原檔目前都還在原位。建議先打開輸出資料夾，確認複本沒問題。"
                          "要清掉原檔或撤銷這次整理，可以在這裡或「整理紀錄」處理；不處理也沒有關係。")
        later_row = ttk.Frame(self.later, style="Card.TFrame")
        later_row.pack(fill="x", pady=(px(8), 0))
        self.records_btn = ttk.Button(later_row, text="整理紀錄…", command=app.open_records)
        self.records_btn.pack(side="left")
        self.remove_btn = ttk.Button(later_row, text="將這批原檔移到回收筒…", style="Danger.TButton",
                                     command=app.remove_originals_here)
        self.remove_btn.pack(side="left", padx=(px(8), 0))

    def set_banner(self, kind: str, title: str, detail: str) -> None:
        style = {"ok": "Ok", "warn": "Warn", "danger": "Danger"}[kind]
        self.banner.configure(style=f"{style}.TFrame")
        self.banner_title_label.configure(style=f"{style}.TLabel")
        self.banner_detail_label.configure(style=f"{style}.TLabel")
        self.banner_title.set(title)
        self.banner_detail.set(detail)
