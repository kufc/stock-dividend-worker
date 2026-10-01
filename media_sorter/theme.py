"""外觀：簡潔的 Windows 工具風格。

淺灰背景、白色內容區、單一藍色主要操作；橘色表示需要注意，紅色只用在錯誤與移除。
所有尺寸都經過 Theme.px() 換算，Windows 縮放 100%～200% 都維持相同的相對大小。
"""

from __future__ import annotations

import tkinter as tk
from dataclasses import dataclass
from tkinter import font as tkfont
from tkinter import ttk

FONT_FAMILIES = ("Microsoft JhengHei UI", "Microsoft JhengHei", "PingFang TC", "Noto Sans CJK TC",
                 "Noto Sans TC", "WenQuanYi Zen Hei")
BASE_PT = 11  # 一般文字：11 點
POINTS_PER_PIXEL_AT_100 = 96 / 72  # Windows 100% 縮放時，Tk 的 scaling 值


@dataclass
class Theme:
    bg = "#f3f4f6"
    card = "#ffffff"
    text = "#1f2328"
    hint = "#5b6470"
    border = "#d0d5dd"
    accent = "#0f6cbd"
    accent_hover = "#0b5394"
    accent_soft = "#dbeafe"
    accent_disabled = "#9db7d3"
    warn = "#9a4a00"
    warn_bg = "#fff4e5"
    ok = "#1a7f37"
    ok_bg = "#e6f4ea"
    danger = "#b3261e"
    danger_bg = "#fdecea"
    preview_bg = "#20232a"
    scale: float = 1.0
    family: str = "TkDefaultFont"

    def px(self, n: float) -> int:
        """把「100% 縮放下的像素」換算成目前縮放的實際像素。"""
        return max(1, round(n * self.scale))


def setup_theme(root: tk.Misc) -> Theme:
    theme = Theme()
    theme.scale = float(root.tk.call("tk", "scaling")) / POINTS_PER_PIXEL_AT_100
    available = set(tkfont.families(root))
    theme.family = next((f for f in FONT_FAMILIES if f in available), tkfont.nametofont("TkDefaultFont").actual("family"))
    for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont", "TkFixedFont"):
        font = tkfont.nametofont(name)
        font.configure(family=theme.family if name != "TkFixedFont" else font.actual("family"), size=BASE_PT)
    base = tkfont.nametofont("TkDefaultFont")
    bold = base.copy()
    bold.configure(weight="bold")
    heading = base.copy()
    heading.configure(size=BASE_PT + 2, weight="bold")
    title = base.copy()
    title.configure(size=BASE_PT + 9, weight="bold")
    small = base.copy()
    small.configure(size=BASE_PT - 1)
    theme.fonts = {"base": base, "bold": bold, "heading": heading, "title": title, "small": small}  # type: ignore[attr-defined]

    # 下拉選單展開的清單是 Tk 內建的 Listbox，不吃 ttk 樣式，字體要另外指定（否則是很小的預設字）
    root.option_add("*TCombobox*Listbox.font", base)
    root.option_add("*TCombobox*Listbox.selectBackground", theme.accent)
    root.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")
    style = ttk.Style(root)
    style.theme_use("clam")
    px = theme.px
    linespace = base.metrics("linespace")
    root.configure(background=theme.bg)

    style.configure(".", background=theme.bg, foreground=theme.text, font=base, bordercolor=theme.border,
                    focuscolor=theme.accent, troughcolor="#e5e7eb")
    style.configure("TFrame", background=theme.bg)
    style.configure("Card.TFrame", background=theme.card)
    style.configure("Header.TFrame", background=theme.card)
    style.configure("Bar.TFrame", background=theme.card)
    style.configure("Warn.TFrame", background=theme.warn_bg)
    style.configure("Danger.TFrame", background=theme.danger_bg)
    style.configure("Ok.TFrame", background=theme.ok_bg)
    style.configure("Preview.TFrame", background=theme.preview_bg)

    style.configure("TLabel", background=theme.bg, foreground=theme.text)
    for prefix, bg in (("Card", theme.card), ("Header", theme.card), ("Bar", theme.card)):
        style.configure(f"{prefix}.TLabel", background=bg, foreground=theme.text)
        style.configure(f"{prefix}Hint.TLabel", background=bg, foreground=theme.hint)
    style.configure("Title.TLabel", font=title)
    style.configure("CardTitle.TLabel", background=theme.card, font=title)
    style.configure("Heading.TLabel", font=heading)
    style.configure("CardHeading.TLabel", background=theme.card, font=heading)
    style.configure("Hint.TLabel", foreground=theme.hint)
    style.configure("Small.TLabel", font=small, foreground=theme.hint)
    style.configure("Warn.TLabel", background=theme.warn_bg, foreground=theme.warn)
    style.configure("Danger.TLabel", background=theme.danger_bg, foreground=theme.danger)
    style.configure("Ok.TLabel", background=theme.ok_bg, foreground=theme.ok)
    style.configure("DangerText.TLabel", foreground=theme.danger)
    style.configure("AppTitle.TLabel", background=theme.card, font=heading)
    style.configure("StepDone.TLabel", background=theme.card, foreground=theme.ok)
    style.configure("StepTodo.TLabel", background=theme.card, foreground=theme.hint)
    style.configure("StepNow.TLabel", background=theme.card, foreground=theme.accent, font=bold)
    style.configure("Preview.TLabel", background=theme.preview_bg, foreground="#d7dae0")

    pad = (px(14), px(8))  # 按鈕高度約 36～40 個邏輯像素
    style.configure("TButton", padding=pad, background="#e8eaee", foreground=theme.text, bordercolor=theme.border,
                    relief="flat", borderwidth=1, width=0)  # width=0：依文字長度決定寬度（預設最小 11 個字元太寬）
    style.map("TButton", background=[("disabled", "#eef0f3"), ("active", "#dde1e7")],
              foreground=[("disabled", "#9aa1ab")])
    style.configure("Accent.TButton", padding=(px(18), px(9)), background=theme.accent, foreground="#ffffff",
                    bordercolor=theme.accent, font=bold)
    style.map("Accent.TButton", background=[("disabled", theme.accent_disabled), ("active", theme.accent_hover)],
              foreground=[("disabled", "#ffffff")], bordercolor=[("disabled", theme.accent_disabled)])
    style.configure("Danger.TButton", padding=pad, background=theme.danger_bg, foreground=theme.danger,
                    bordercolor=theme.danger)
    style.map("Danger.TButton", background=[("active", "#f9d7d3"), ("disabled", "#eef0f3")],
              foreground=[("disabled", "#9aa1ab")])
    # 分類按鈕：要一眼看出是按鈕、好按（高度約 44 個邏輯像素）；ChoiceOn 是 AI 建議的那一個
    choice_pad = (px(16), px(10))
    style.configure("Choice.TButton", padding=choice_pad, background="#f6f7f9", bordercolor="#b9c0cb", anchor="w")
    style.map("Choice.TButton", background=[("active", theme.accent_soft), ("disabled", "#f6f7f9")],
              foreground=[("disabled", "#9aa1ab")])
    style.configure("ChoiceOn.TButton", padding=choice_pad, background=theme.accent_soft, bordercolor=theme.accent,
                    font=bold, anchor="w")
    style.map("ChoiceOn.TButton", background=[("active", theme.accent_soft), ("disabled", theme.accent_soft)],
              foreground=[("disabled", "#9aa1ab")])
    style.configure("Link.TButton", padding=(px(8), px(4)), background=theme.card, foreground=theme.accent,
                    bordercolor=theme.card)
    style.map("Link.TButton", background=[("active", theme.accent_soft)])
    style.configure("Header.TButton", padding=(px(10), px(6)), background=theme.card, bordercolor=theme.card)
    style.map("Header.TButton", background=[("active", "#eef0f3")])

    for kind in ("TCheckbutton", "TRadiobutton"):
        style.configure(kind, background=theme.bg)
        style.configure(f"Card.{kind}", background=theme.card)
    style.configure("TEntry", padding=px(6), fieldbackground=theme.card)
    style.configure("TCombobox", padding=px(6), fieldbackground=theme.card, background="#e8eaee")
    style.map("TCombobox", fieldbackground=[("readonly", theme.card), ("disabled", "#eef0f3")],
              selectbackground=[("readonly", theme.card)], selectforeground=[("readonly", theme.text)])
    style.configure("TSpinbox", padding=px(4), fieldbackground=theme.card)
    style.configure("TLabelframe", background=theme.bg, bordercolor=theme.border)
    style.configure("TLabelframe.Label", background=theme.bg, foreground=theme.text, font=bold)
    style.configure("Card.TLabelframe", background=theme.card, bordercolor=theme.border)
    style.configure("Card.TLabelframe.Label", background=theme.card, foreground=theme.text, font=bold)

    style.configure("Treeview", background=theme.card, fieldbackground=theme.card, foreground=theme.text,
                    rowheight=max(int(linespace * 1.7), px(28)), bordercolor=theme.border)
    style.configure("Buckets.Treeview", rowheight=max(int(linespace * 2.0), px(36)))
    style.map("Treeview", background=[("selected", theme.accent)], foreground=[("selected", "#ffffff")])
    style.configure("Treeview.Heading", background="#e8eaee", foreground=theme.text, padding=(px(6), px(6)),
                    font=bold, relief="flat")
    style.map("Treeview.Heading", background=[("active", "#dde1e7")])
    style.configure("Horizontal.TProgressbar", background=theme.accent, troughcolor="#e5e7eb", thickness=px(10),
                    bordercolor=theme.bg, lightcolor=theme.accent, darkcolor=theme.accent)
    style.configure("TNotebook", background=theme.bg, bordercolor=theme.border)
    style.configure("TNotebook.Tab", padding=(px(16), px(8)), background="#e8eaee")
    style.map("TNotebook.Tab", background=[("selected", theme.card)], font=[("selected", bold)])
    style.configure("TPanedwindow", background=theme.bg)
    style.configure("Sash", sashthickness=px(8), handlesize=px(8), background=theme.border)
    style.configure("TSeparator", background=theme.border)
    return theme
