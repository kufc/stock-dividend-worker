"""縮圖格：步驟 2 用來一眼看完一個分類裡的所有檔案，點選之後用下面的分類按鈕改分類。

畫在一個 Canvas 上，只畫看得到的那幾列（上萬個檔案也不會卡）。選取的操作方式跟檔案總管一樣：
點一下選取、Ctrl 點加選、Shift 點連選、Ctrl+A 全選、方向鍵移動、按兩下放大檢視。
提供跟 Treeview 相近的 selection()／selection_set()／focus()／see() 介面，程式其他部分不用管它怎麼畫。
"""

from __future__ import annotations

import math
import tkinter as tk
from dataclasses import dataclass
from tkinter import ttk
from typing import Callable

BADGES = {"low": ("？", "warn"), "confirmed": ("✓", "ok")}  # 縮圖右上角的小標記：沒把握／你指定的


@dataclass
class Cell:
    """一格要顯示的內容（由程式依檔案狀態決定）。"""

    iid: str
    text: str  # 縮圖下面的文字：分類名稱或狀態
    style: str = "normal"  # normal／low（AI 沒把握）／confirmed（你指定的）／skipped／error／pending（辨識中）
    video: bool = False


class ThumbGrid(ttk.Frame):
    def __init__(self, master, theme, thumb_px: int, *, cell_for: Callable[[str], Cell],
                 image_for: Callable[[str], object], on_select: Callable[[], None],
                 on_activate: Callable[[str], None]):
        super().__init__(master, style="Card.TFrame")
        self.theme = theme
        self.px = theme.px
        self.cell_for, self.image_for, self.on_select, self.on_activate = cell_for, image_for, on_select, on_activate
        self._iids: list[str] = []
        self._index: dict[str, int] = {}
        self._selected: set[str] = set()
        self._focus: str | None = None
        self._anchor: int | None = None
        self._cols = 1
        self._redraw_job = None
        self._drawn_images: list = []  # 畫在 Canvas 上的圖片要保留參照，否則會被回收而消失
        self.empty_text = ""
        self.canvas = tk.Canvas(self, highlightthickness=0, borderwidth=0, background=theme.card, takefocus=1,
                                yscrollincrement=self.px(30))
        self.vbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self._on_yscroll)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.vbar.grid(row=0, column=1, sticky="ns")
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)
        self.set_thumb_size(thumb_px)
        self._font = theme.fonts["base"]
        self._small = theme.fonts["small"]
        c = self.canvas
        c.bind("<Configure>", lambda e: self._layout())
        c.bind("<Button-1>", self._on_click)
        c.bind("<Control-Button-1>", lambda e: self._on_click(e, toggle=True))
        c.bind("<Shift-Button-1>", lambda e: self._on_click(e, extend=True))
        c.bind("<Double-Button-1>", self._on_double)
        c.bind("<MouseWheel>", self._on_wheel)
        c.bind("<Button-4>", lambda e: self._scroll_units(-3))
        c.bind("<Button-5>", lambda e: self._scroll_units(3))
        for key, (dr, dc) in {"Left": (0, -1), "Right": (0, 1), "Up": (-1, 0), "Down": (1, 0)}.items():
            c.bind(f"<Key-{key}>", lambda e, dr=dr, dc=dc: (self._move(dr, dc), "break")[1])
        c.bind("<Key-Home>", lambda e: (self._go(0), "break")[1])
        c.bind("<Key-End>", lambda e: (self._go(len(self._iids) - 1), "break")[1])
        c.bind("<Key-Prior>", lambda e: (self._page(-1), "break")[1])
        c.bind("<Key-Next>", lambda e: (self._page(1), "break")[1])
        c.bind("<Control-a>", lambda e: (self.select_all(), "break")[1])
        c.bind("<FocusIn>", lambda e: self._schedule_redraw())
        c.bind("<FocusOut>", lambda e: self._schedule_redraw())

    def focus_set(self) -> None:  # 鍵盤焦點要給 Canvas（方向鍵、Ctrl+A 綁在它上面）
        self.canvas.focus_set()

    def destroy(self) -> None:
        self._redraw_job = None
        if self._tclCommands:
            # 程式關閉時 after_idle 可能已經被統一取消過，留下的名稱不能再刪一次（否則 destroy 會出錯）
            self._tclCommands = [name for name in self._tclCommands if self.tk.call("info", "commands", name)]
        super().destroy()

    # ------------------------------------------------------------------ 大小與版面
    def set_thumb_size(self, thumb_px: int) -> None:
        self.thumb = thumb_px
        self.pad = self.px(6)
        self.label_h = self.px(24)
        self.cell_w = self.thumb + self.pad * 2
        self.cell_h = self.thumb + self.pad * 2 + self.label_h
        self._layout()

    def _layout(self) -> None:
        width = self.canvas.winfo_width()
        self._cols = max(1, (width - self.pad) // self.cell_w) if width > 1 else 1
        rows = math.ceil(len(self._iids) / self._cols) if self._iids else 0
        self.canvas.configure(scrollregion=(0, 0, max(width, 1), max(rows * self.cell_h + self.pad, 1)))
        self._schedule_redraw()

    def _on_yscroll(self, first: str, last: str) -> None:
        self.vbar.set(first, last)
        self._schedule_redraw()

    def _schedule_redraw(self) -> None:
        if self._redraw_job is None:
            self._redraw_job = self.after_idle(self._redraw)

    def _cell_box(self, pos: int) -> tuple[int, int, int, int]:
        row, col = divmod(pos, self._cols)
        x0 = self.pad + col * self.cell_w
        y0 = self.pad + row * self.cell_h
        return x0, y0, x0 + self.cell_w, y0 + self.cell_h

    def _visible_range(self) -> tuple[int, int]:
        c = self.canvas
        top = c.canvasy(0)
        bottom = top + max(c.winfo_height(), 1)
        first_row = max(0, int((top - self.pad) // self.cell_h))
        last_row = int((bottom - self.pad) // self.cell_h) + 1
        return first_row * self._cols, min(len(self._iids), (last_row + 1) * self._cols)

    def _redraw(self) -> None:
        self._redraw_job = None
        c = self.canvas
        c.delete("all")
        self._drawn_images = []
        if not self._iids:
            if self.empty_text:
                c.create_text(max(c.winfo_width(), 1) // 2, self.px(60), text=self.empty_text, font=self._font,
                              fill=self.theme.hint, width=max(c.winfo_width() - self.px(40), 100), justify="center")
            return
        start, end = self._visible_range()
        focused = self.canvas.focus_get() is self.canvas
        for pos in range(start, end):
            self._draw_cell(pos, focused)

    def _draw_cell(self, pos: int, focused: bool) -> None:
        c, t = self.canvas, self.theme
        iid = self._iids[pos]
        cell = self.cell_for(iid)
        x0, y0, x1, y1 = self._cell_box(pos)
        selected = iid in self._selected
        if selected:
            fill, outline, width = t.accent_soft, t.accent, 2
        elif cell.style == "low":
            fill, outline, width = t.warn_bg, t.border, 1
        else:
            fill, outline, width = t.card, t.border, 1
        c.create_rectangle(x0 + 1, y0 + 1, x1 - 2, y1 - 2, fill=fill, outline=outline, width=width)
        if focused and iid == self._focus:
            c.create_rectangle(x0 + 3, y0 + 3, x1 - 4, y1 - 4, outline=t.accent, dash=(3, 2))
        cx, cy = x0 + self.cell_w // 2, y0 + self.pad + self.thumb // 2
        image = self.image_for(iid)
        if image is not None:
            self._drawn_images.append(image)
            c.create_image(cx, cy, image=image)
        else:
            c.create_rectangle(cx - self.thumb // 2, cy - self.thumb // 2, cx + self.thumb // 2, cy + self.thumb // 2,
                               fill="#eef0f3", outline="")
            c.create_text(cx, cy, text="讀取失敗" if cell.style == "error" else "⋯", font=self._font, fill=t.hint)
        colors = {"confirmed": t.ok, "low": t.warn, "skipped": t.hint, "error": t.danger, "pending": t.hint}
        c.create_text(cx, y1 - self.pad - self.label_h // 2, text=cell.text, font=self._font,
                      fill=colors.get(cell.style, t.text), width=self.cell_w - self.pad * 2)
        if cell.video:
            bx, by = x0 + self.pad + 2, y0 + self.pad + 2
            c.create_rectangle(bx, by, bx + self.px(26), by + self.px(20), fill="#20232a", outline="")
            c.create_text(bx + self.px(13), by + self.px(10), text="▶", fill="#ffffff", font=self._small)
        badge = BADGES.get(cell.style)
        if badge:
            text, kind = badge
            bg, fg = (t.warn, "#ffffff") if kind == "warn" else (t.ok, "#ffffff")
            r = self.px(11)
            bx, by = x1 - self.pad - r - 2, y0 + self.pad + r + 2
            c.create_oval(bx - r, by - r, bx + r, by + r, fill=bg, outline="")
            c.create_text(bx, by, text=text, fill=fg, font=self._small)

    # ------------------------------------------------------------------ 內容
    def set_iids(self, iids: list[str], empty_text: str = "") -> None:
        """換成新的一組檔案；還在的選取與焦點會保留，捲動位置也盡量不動。"""
        if iids == self._iids:
            if empty_text != self.empty_text:
                self.empty_text = empty_text
                self._schedule_redraw()
            return
        self.empty_text = empty_text
        self._iids = list(iids)
        self._index = {iid: i for i, iid in enumerate(self._iids)}
        self._selected &= set(self._index)
        if self._focus not in self._index:
            self._focus = None
        if self._anchor is not None and self._anchor >= len(self._iids):
            self._anchor = None
        self._layout()

    def update_cell(self, iid: str) -> None:
        if iid in self._index:
            self._schedule_redraw()

    def redraw(self) -> None:
        self._schedule_redraw()

    # ------------------------------------------------------------------ 選取（跟 Treeview 相近的介面）
    def get_children(self) -> tuple[str, ...]:
        return tuple(self._iids)

    def exists(self, iid: str) -> bool:
        return iid in self._index

    def index(self, iid: str) -> int | None:
        return self._index.get(iid)

    def selection(self) -> tuple[str, ...]:
        return tuple(iid for iid in self._iids if iid in self._selected)

    def _as_list(self, iids) -> list[str]:
        if isinstance(iids, str):
            return [iids]
        return [iid for iid in iids if isinstance(iid, str)]

    def selection_set(self, *iids) -> None:
        wanted = [iid for arg in iids for iid in self._as_list(arg) if iid in self._index]
        self._selected = set(wanted)
        if wanted:
            self._focus = wanted[-1]
            self._anchor = self._index[wanted[-1]]
        self._changed()

    def selection_add(self, *iids) -> None:
        for arg in iids:
            for iid in self._as_list(arg):
                if iid in self._index:
                    self._selected.add(iid)
                    self._focus = iid
        self._changed()

    def selection_remove(self, *iids) -> None:
        for arg in iids:
            for iid in self._as_list(arg):
                self._selected.discard(iid)
        if self._focus not in self._selected:
            self._focus = next(iter(self.selection()), None)
        self._changed()

    def select_all(self) -> None:
        self.selection_set(self._iids)

    def focus(self, iid: str | None = None):
        if iid is None:
            return self._focus or ""
        if iid in self._index:
            self._focus = iid
            self._schedule_redraw()
        return None

    def see(self, iid: str) -> None:
        pos = self._index.get(iid)
        if pos is None:
            return
        self.canvas.update_idletasks()
        _, y0, _, y1 = self._cell_box(pos)
        c = self.canvas
        top = c.canvasy(0)
        height = max(c.winfo_height(), 1)
        total = max(1, int(float(c.cget("scrollregion").split()[3])))
        if y0 < top:
            c.yview_moveto(max(0, y0 - self.pad) / total)
        elif y1 > top + height:
            c.yview_moveto(max(0, y1 + self.pad - height) / total)

    def _changed(self) -> None:
        self._schedule_redraw()
        self.on_select()

    # ------------------------------------------------------------------ 滑鼠與鍵盤
    def _pos_at(self, x: int, y: int) -> int | None:
        cx, cy = self.canvas.canvasx(x), self.canvas.canvasy(y)
        col = int((cx - self.pad) // self.cell_w)
        row = int((cy - self.pad) // self.cell_h)
        if cx < self.pad or cy < self.pad or col >= self._cols or col < 0 or row < 0:
            return None
        pos = row * self._cols + col
        return pos if pos < len(self._iids) else None

    def _on_click(self, event, toggle: bool = False, extend: bool = False) -> str:
        self.canvas.focus_set()
        pos = self._pos_at(event.x, event.y)
        if pos is None:
            if not toggle and not extend:
                self._selected.clear()
                self._changed()
            return "break"
        iid = self._iids[pos]
        if toggle:
            if iid in self._selected:
                self._selected.discard(iid)
            else:
                self._selected.add(iid)
            self._anchor = pos
        elif extend and self._anchor is not None:
            lo, hi = sorted((self._anchor, pos))
            self._selected = set(self._iids[lo:hi + 1])
        else:
            self._selected = {iid}
            self._anchor = pos
        self._focus = iid
        self._changed()
        return "break"

    def _on_double(self, event) -> str:
        pos = self._pos_at(event.x, event.y)
        if pos is not None:
            self.on_activate(self._iids[pos])
        return "break"

    def _on_wheel(self, event) -> str:
        self._scroll_units(-3 if event.delta > 0 else 3)
        return "break"

    def _scroll_units(self, units: int) -> None:
        first, last = self.canvas.yview()
        if first <= 0.0 and last >= 1.0:
            return
        self.canvas.yview_scroll(units, "units")

    def _go(self, pos: int) -> None:
        if not self._iids:
            return
        pos = max(0, min(len(self._iids) - 1, pos))
        iid = self._iids[pos]
        self._selected = {iid}
        self._focus, self._anchor = iid, pos
        self.see(iid)
        self._changed()

    def _move(self, dr: int, dc: int) -> None:
        if not self._iids:
            return
        pos = self._index.get(self._focus or "", -1)
        if pos < 0:
            self._go(0)
            return
        target = pos + dr * self._cols + dc
        if 0 <= target < len(self._iids) or (dr and 0 <= target):
            self._go(target)

    def _page(self, direction: int) -> None:
        rows = max(1, self.canvas.winfo_height() // self.cell_h)
        self._move(direction * rows, 0)
