"""畫面：外框（步驟列、狀態列）與四個步驟的版面。

這個檔案只負責「長什麼樣子」與元件的排列；按鈕按下去做什麼都在 app.py。
每個步驟的底部操作列固定在視窗最下方，內容太多時只有中間的部分會捲動，
所以視窗再小，主要按鈕也不會被擠出視窗外。
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from .config import RENAME_MODES
from .grid import ThumbGrid

STEP_NAMES = ["選擇資料夾", "檢查分類", "預覽整理結果", "完成"]
LOW_MODES = [("suggest", "依 AI 的建議整理"), ("unsorted", "放到「未分類」資料夾，之後自己看"), ("leave", "這次不整理")]
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
                           text="AI 會看每張照片與影片，先把它們分好類；你檢查過之後，才會把檔案複製到分類資料夾。"),
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
            "1. AI 判斷每個檔案「看起來像什麼」，先幫你分好（只是建議，不一定正確）\n"
            "2. 你一個分類一個分類看縮圖，把分錯的選起來改掉；AI 沒把握的會集中在「需檢查」\n"
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


class WrapFrame(ttk.Frame):
    """把按鈕由左到右排，放不下就自動換到下一列（分類很多時也不會被切掉）。

    用 place 擺絕對位置：每個按鈕只佔自己的寬度（用 grid 會讓同一欄的按鈕一樣寬，中間出現怪縫），
    可用寬度一律以外層容器為準，所以不會因為自己被撐寬而以為放得下。
    """

    def __init__(self, master, gap: int, **kwargs):
        super().__init__(master, **kwargs)
        self.gap = gap
        self.widgets: list = []
        self._width = 0
        master.bind("<Configure>", self._on_configure, add="+")
        self.bind("<Configure>", self._on_configure, add="+")

    def add(self, widget) -> None:
        self.widgets.append(widget)
        self.reflow()

    def clear(self) -> None:
        for widget in self.widgets:
            widget.destroy()
        self.widgets = []
        self.reflow()

    def _on_configure(self, _event=None) -> None:
        width = self.master.winfo_width()
        if width != self._width:
            self._width = width
            self.reflow()

    def reflow(self) -> None:
        width = max(self._width, 1)
        x = y = row_h = 0
        for widget in self.widgets:
            w, h = widget.winfo_reqwidth(), widget.winfo_reqheight()
            if x and x + w > width:
                x, y = 0, y + row_h + self.gap
                row_h = 0
            widget.place(x=x, y=y)
            x += w + self.gap
            row_h = max(row_h, h)
        self.configure(height=(y + row_h) if self.widgets else 1)


# ---------------------------------------------------------------------------- 步驟 2：檢查分類
class ReviewPage(ttk.Frame):
    """左邊是分類清單（含數量），中間是該分類的縮圖格，右邊是目前檔案的大預覽。

    AI 已經把每個檔案分好；使用者只要看縮圖、把分錯的選起來、按下面的分類按鈕改掉。
    """

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
        self.body.grid_rowconfigure(0, weight=1)
        self.body.grid_columnconfigure(1, weight=1)
        self.bucket_frame = ttk.Frame(self.body, style="Card.TFrame", padding=px(8))
        self.grid_frame = ttk.Frame(self.body, style="Card.TFrame", padding=px(10))
        self.detail_frame = ttk.Frame(self.body, style="Card.TFrame", padding=px(10), width=px(380))
        self.detail_frame.grid_propagate(False)
        self.detail_frame.pack_propagate(False)
        self.bucket_frame.grid(row=0, column=0, sticky="nsw", padx=(0, px(8)))
        self.grid_frame.grid(row=0, column=1, sticky="nsew")
        self.detail_frame.grid(row=0, column=2, sticky="nsew", padx=(px(8), 0))
        self._build_buckets(self.bucket_frame)
        self._build_grid(self.grid_frame)
        self._build_detail(self.detail_frame)

    # -- 左邊：分類清單
    def _build_buckets(self, parent) -> None:
        app, px = self.app, self.app.theme.px
        ttk.Label(parent, text="分類", style="CardHeading.TLabel").pack(anchor="w", padx=px(4))
        self.bucket_hint = autowrap(ttk.Label(parent, text="點一個分類，看 AI 放進去的檔案", style="CardHint.TLabel",
                                              justify="left"))
        self.bucket_hint.pack(anchor="w", fill="x", padx=px(4), pady=(0, px(6)))
        ttk.Button(parent, text="分類設定…", command=app.open_category_dialog).pack(side="bottom", fill="x",
                                                                                 pady=(px(8), 0))
        frame = ttk.Frame(parent, style="Card.TFrame")
        frame.pack(fill="both", expand=True)
        self.buckets = ttk.Treeview(frame, columns=("count",), show="tree", selectmode="browse",
                                    style="Buckets.Treeview", takefocus=1)
        self.buckets.column("#0", width=px(150), minwidth=px(100), stretch=True)
        self.buckets.column("count", width=px(56), minwidth=px(40), anchor="e", stretch=False)
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.buckets.yview)
        self.buckets.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.buckets.pack(side="left", fill="both", expand=True)
        theme = app.theme
        self.buckets.tag_configure("low", foreground=theme.warn, font=theme.fonts["bold"])
        self.buckets.tag_configure("dim", foreground="#8a919c")
        self.buckets.tag_configure("error", foreground=theme.danger)
        self.buckets.bind("<<TreeviewSelect>>", app.on_bucket_select)

    # -- 中間：縮圖格與分類按鈕
    def _build_grid(self, parent) -> None:
        app, px = self.app, self.app.theme.px
        head = ttk.Frame(parent, style="Card.TFrame")
        head.pack(fill="x")
        self.grid_title_var = tk.StringVar()
        ttk.Label(head, textvariable=self.grid_title_var, style="CardHeading.TLabel").pack(side="left")
        size = ttk.Frame(head, style="Card.TFrame")
        size.pack(side="right")
        ttk.Label(size, text="縮圖", style="CardHint.TLabel").pack(side="left", padx=(0, px(4)))
        self.size_buttons = {}
        for key, label in (("small", "小"), ("medium", "中"), ("large", "大")):
            button = ttk.Button(size, text=label, style="Link.TButton", command=lambda k=key: app.set_thumb_size(k))
            button.pack(side="left")
            self.size_buttons[key] = button
        self.select_all_btn = ttk.Button(head, text="全選", style="Link.TButton", command=lambda: app.tree.select_all())
        self.select_all_btn.pack(side="right", padx=(0, px(12)))
        self.grid_hint_var = tk.StringVar()
        self.grid_hint = autowrap(ttk.Label(parent, textvariable=self.grid_hint_var, style="CardHint.TLabel",
                                            justify="left"))
        self.grid_hint.pack(anchor="w", fill="x", pady=(0, px(6)))

        # 底部的操作列先排（視窗矮時才不會被縮圖格擠掉）
        actions = ttk.Frame(parent, style="Card.TFrame")
        actions.pack(side="bottom", fill="x", pady=(px(8), 0))
        self.sel_var = tk.StringVar()
        autowrap(ttk.Label(actions, textvariable=self.sel_var, style="Card.TLabel", font=app.theme.fonts["bold"],
                           justify="left")).pack(anchor="w", fill="x", pady=(0, px(4)))
        self.category_bar = WrapFrame(actions, px(6), style="Card.TFrame")
        self.category_bar.pack(fill="x")
        self.category_buttons: list[ttk.Button] = []
        row = ttk.Frame(actions, style="Card.TFrame")
        row.pack(fill="x", pady=(px(4), 0))
        self.accept_btn = ttk.Button(row, text="採用 AI 建議", style="Accent.TButton", command=app.accept_selected)
        self.accept_btn.pack(side="left")
        self.skip_btn = ttk.Button(row, text="略過，不整理", command=app.skip_selected)
        self.skip_btn.pack(side="left", padx=(px(8), 0))
        self.new_btn = ttk.Button(row, text="新增分類…", command=app.add_category_prompt)
        self.new_btn.pack(side="left", padx=(px(8), 0))
        self.undo_btn = ttk.Button(row, text="撤回上一步", command=app.undo_last_confirm)
        self.undo_btn.pack(side="right")
        self.shortcut_var = tk.StringVar(value="快速鍵：數字鍵選分類　Enter 採用 AI 建議　S 略過　Ctrl+Z 撤回　空白鍵放大檢視")
        self.shortcut_label = autowrap(ttk.Label(actions, textvariable=self.shortcut_var, style="CardHint.TLabel",
                                                 justify="left"))
        self.shortcut_label.pack(anchor="w", fill="x", pady=(px(4), 0))
        self.short = False
        self.action_buttons = (self.accept_btn, self.skip_btn)

        self.thumb_grid = ThumbGrid(parent, app.theme, app.thumb_px(), cell_for=app.cell_for, image_for=app.image_for,
                              on_select=app.on_select, on_activate=lambda iid: app.open_viewer())
        self.thumb_grid.pack(fill="both", expand=True)

    def rebuild_category_buttons(self, names: list[str]) -> None:
        app = self.app
        self.category_bar.clear()
        self.category_buttons = []
        for i, name in enumerate(names):
            key = f"{i + 1} " if i < 9 else ("0 " if i == 9 else "")
            button = ttk.Button(self.category_bar, text=f"{key}{name}", style="Choice.TButton",
                                command=lambda n=name: app.assign(n))
            self.category_bar.add(button)
            self.category_buttons.append(button)

    # -- 右邊：目前檔案
    def _build_detail(self, parent) -> None:
        app, px = self.app, self.app.theme.px
        self.stack = ttk.Frame(parent, style="Card.TFrame")
        self.stack.pack(fill="both", expand=True)
        self.stack.grid_rowconfigure(0, weight=1)
        self.stack.grid_columnconfigure(0, weight=1)

        self.placeholder = ttk.Frame(self.stack, style="Card.TFrame")
        self.placeholder.grid(row=0, column=0, sticky="nsew")
        self.placeholder_var = tk.StringVar(value="點一個縮圖，這裡會顯示大圖與 AI 的判斷。")
        autowrap(ttk.Label(self.placeholder, textvariable=self.placeholder_var, style="CardHint.TLabel",
                           justify="center", anchor="center")).pack(expand=True, fill="x", padx=px(16))

        d = self.detail = ttk.Frame(self.stack, style="Card.TFrame")
        d.grid(row=0, column=0, sticky="nsew")
        # 預覽佔面板高度的四成（至少 150），下面的文字放不下時從最底下（AI 還看到）開始被切掉
        self.preview_frame = ttk.Frame(d, style="Preview.TFrame", height=px(240))
        self.preview_frame.pack(side="top", fill="x")
        self.preview_frame.pack_propagate(False)
        lower = self.lower = ttk.Frame(d, style="Card.TFrame")
        lower.pack(side="top", fill="both", expand=True)
        d.bind("<Configure>", self._resize_preview)
        self.preview_label = ttk.Label(self.preview_frame, style="Preview.TLabel", anchor="center", cursor="hand2")
        self.preview_label.pack(fill="both", expand=True)
        self.preview_label.bind("<Double-Button-1>", lambda e: app.open_viewer())

        self.file_var = tk.StringVar()
        autowrap(ttk.Label(lower, textvariable=self.file_var, style="Card.TLabel", justify="left",
                           font=app.theme.fonts["bold"])).pack(anchor="w", fill="x", pady=(px(6), 0))
        self.info_var = tk.StringVar()
        autowrap(ttk.Label(lower, textvariable=self.info_var, style="CardHint.TLabel", justify="left")).pack(
            anchor="w", fill="x")
        self.zoom_btn = ttk.Button(lower, text="放大檢視（或按兩下縮圖）", style="Link.TButton", command=app.open_viewer)
        self.zoom_btn.pack(anchor="w", pady=(px(2), 0))
        self.notice_frame = ttk.Frame(lower, style="Warn.TFrame", padding=px(8))
        self.notice_var = tk.StringVar()
        autowrap(ttk.Label(self.notice_frame, textvariable=self.notice_var, style="Warn.TLabel", justify="left"),
                 px(20)).pack(fill="x")
        self.suggest_head = ttk.Label(lower, text="AI 覺得像：", style="CardHeading.TLabel")
        self.suggest_head.pack(anchor="w", pady=(px(8), px(2)))
        self.candidates = ttk.Frame(lower, style="Card.TFrame")
        self.candidates.pack(fill="x")
        self.candidate_buttons: list[ttk.Button] = []
        for i in range(3):
            button = ttk.Button(self.candidates, style="Choice.TButton", command=lambda i=i: app.pick_suggestion(i))
            button.pack(fill="x", pady=(0, px(4)))
            self.candidate_buttons.append(button)
        self.score_var = tk.StringVar()
        autowrap(ttk.Label(lower, textvariable=self.score_var, style="CardHint.TLabel", justify="left")).pack(
            anchor="w", fill="x")
        self.tags_head = ttk.Label(lower, text="AI 還看到（點一下可以當作新分類）：", style="CardHint.TLabel")
        self.tags_head.pack(anchor="w", pady=(px(8), 0))
        self.tags_frame = WrapFrame(lower, px(4), style="Card.TFrame")
        self.tags_frame.pack(fill="x")
        self.view_state = "placeholder"
        self.show_state("placeholder")

    def _resize_preview(self, event) -> None:
        self.preview_frame.configure(height=max(self.app.theme.px(150), int(event.height * 0.4)))
        self.app.schedule_preview_render()

    def show_toolbar(self, shown: bool) -> None:
        if shown:
            self.toolbar.pack(side="top", fill="x", before=self.body)
        else:
            self.toolbar.pack_forget()

    def show_state(self, state: str) -> None:
        """state：detail（目前檔案）／placeholder（沒有選取）"""
        {"detail": self.detail, "placeholder": self.placeholder}[state].tkraise()
        self.view_state = state

    def show_notice(self, text: str) -> None:
        self.notice_var.set(text)
        if text:
            self.notice_frame.pack(fill="x", pady=(self.app.theme.px(6), 0), before=self.suggest_head)
        else:
            self.notice_frame.pack_forget()

    # -- 寬窄版面：窄的時候收起右邊的預覽（按兩下縮圖仍可放大檢視）；矮的時候收起提示文字，把高度留給縮圖
    def set_narrow(self, narrow: bool, force: bool = False) -> None:
        if narrow == self.narrow and not force:
            return
        self.narrow = narrow
        if narrow:
            self.detail_frame.grid_remove()
        else:
            self.detail_frame.grid()

    def set_short(self, short: bool) -> None:
        if short == self.short:
            return
        self.short = short
        px = self.app.theme.px
        if short:
            self.shortcut_label.pack_forget()
            self.grid_hint.pack_forget()
            self.bucket_hint.pack_forget()
            self.tags_head.pack_forget()
            self.tags_frame.pack_forget()
        else:
            self.tags_frame.pack(fill="x")
            self.app.show_detail(self.app.current, len(self.app.tree.selection()) or 1)
            self.shortcut_label.pack(anchor="w", fill="x", pady=(px(4), 0))
            self.grid_hint.pack(anchor="w", fill="x", pady=(0, px(6)), before=self.thumb_grid)
            self.bucket_hint.pack(anchor="w", fill="x", padx=px(4), pady=(0, px(6)), after=self.bucket_hint.master.winfo_children()[0])


# ---------------------------------------------------------------------------- 步驟 3：預覽整理結果
class PlanPage(ttk.Frame):
    def __init__(self, master, app):
        super().__init__(master)
        px = app.theme.px
        self.app = app
        bar = bottom_bar(self, app)
        self.back_btn = ttk.Button(bar, text="← 回到檢查分類", command=lambda: app.show_step(2))
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
        self.low_frame = ttk.Frame(files, style="Card.TFrame")
        self.low_var = tk.StringVar()
        autowrap(ttk.Label(self.low_frame, textvariable=self.low_var, style="Card.TLabel", justify="left")).pack(
            anchor="w", fill="x", pady=(px(8), px(2)))
        self.low_radios: list[ttk.Radiobutton] = []
        for key, label in LOW_MODES:
            radio = ttk.Radiobutton(self.low_frame, text=label, value=key, variable=app.low_mode_var,
                                    style="Card.TRadiobutton", command=app.refresh_plan)
            radio.pack(anchor="w", padx=(px(12), 0), pady=(px(2), 0))
            self.low_radios.append(radio)

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
        self.back_btn = ttk.Button(bar, text="回到檢查分類", command=lambda: app.show_step(2))

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
