"""「卡片式」頁面的共用骨架（設定頁、回放錄音頁共用）。

每一頁都是：可捲動區域 → 大標題與說明 → 一張張圓角卡片，卡片內用 grid 排列
「左欄名稱＋右欄控制項」。這裡集中這些版面工具，兩頁外觀才會一致。
"""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable
from tkinter import ttk
from typing import TYPE_CHECKING

from soundboard.gui.styles import FONT_BODY, FONT_H1, FONT_H2, FONT_SMALL
from soundboard.gui.widgets import RoundedButton, RoundedFrame, ScrollArea, ToggleSwitch
from soundboard.theme import BTN, BTN_HOVER, CARD, FG, FG_SUBTLE, SURFACE

if TYPE_CHECKING:
    from soundboard.gui.app import SoundboardApp

LABEL_COLUMN_WIDTH = 230     # 100% 縮放時左側欄位名稱的寬度
PAGE_PADDING = 24


class CardPage(tk.Frame):
    """卡片式頁面的基底類別。

    子類別在 ``self.body`` 裡用 :meth:`_card` 建立卡片。
    """

    def __init__(self, parent: tk.Misc, app: SoundboardApp, title: str, subtitle: str) -> None:
        """
        Args:
            parent (tk.Misc): 父元件（內容區）。
            app (SoundboardApp): 主程式。
            title (str): 頁面大標題。
            subtitle (str): 標題下方的說明。
        """
        super().__init__(parent, bg=SURFACE)
        self.app = app
        m = app.metrics
        self.scroll = ScrollArea(self, bg=SURFACE)
        self.scroll.pack(fill="both", expand=True, padx=(m.px(PAGE_PADDING), m.px(6)), pady=(0, m.px(6)))
        self.body = tk.Frame(self.scroll.inner, bg=SURFACE)
        self.body.pack(fill="x", padx=(0, m.px(PAGE_PADDING - 6)))
        tk.Label(self.body, text=title, font=FONT_H1, bg=SURFACE, fg=FG, anchor="w").pack(
            fill="x", pady=(m.px(20), 0))
        sub = tk.Label(self.body, text=subtitle, font=FONT_SMALL, bg=SURFACE, fg=FG_SUBTLE, anchor="w",
                       justify="left")
        sub.pack(fill="x", pady=(m.px(2), m.px(16)))
        # 視窗變窄時說明自動換行，不會被切掉
        self.body.bind("<Configure>", lambda e: sub.configure(wraplength=max(m.px(200), e.width - m.px(8))),
                       add="+")

    def _card(self, parent: tk.Misc, title: str, description: str = "") -> tk.Frame:
        """建立一張圓角卡片，回傳放控制項的 grid 容器（第 1 欄會延展）。"""
        m = self.app.metrics
        card = RoundedFrame(parent, surface=SURFACE, bg=CARD, radius=m.px(10), padding=m.px(18))
        card.pack(fill="x", pady=(0, m.px(14)))
        inner = card.inner
        tk.Label(inner, text=title, font=FONT_H2, bg=CARD, fg=FG, anchor="w").pack(fill="x")
        if description:
            desc = tk.Label(inner, text=description, font=FONT_SMALL, bg=CARD, fg=FG_SUBTLE, anchor="w",
                            justify="left")
            desc.pack(fill="x", pady=(m.px(2), 0))
            inner.bind("<Configure>", lambda e: desc.configure(wraplength=max(m.px(200), e.width - m.px(8))),
                       add="+")
        grid = tk.Frame(inner, bg=CARD)
        grid.pack(fill="x", pady=(m.px(12), 0))
        grid.columnconfigure(0, minsize=m.px(LABEL_COLUMN_WIDTH))
        grid.columnconfigure(1, weight=1)
        return grid

    def _label(self, parent: tk.Misc, row: int, text: str) -> None:
        tk.Label(parent, text=text, font=FONT_BODY, bg=CARD, fg=FG, anchor="w").grid(
            row=row, column=0, sticky="w", pady=self.app.metrics.px(5))

    def _button(self, parent: tk.Misc, text: str, command: Callable[[], object], bg: str = BTN,
                hover: str = BTN_HOVER) -> RoundedButton:
        m = self.app.metrics
        return RoundedButton(parent, text, command, surface=CARD, bg=bg, hover=hover, radius=m.px(8),
                             padx=m.px(12), pady=m.px(6))

    def _combo(self, parent: tk.Misc, var: tk.StringVar, callback: Callable[[], None],
               values: list[str] | tuple[str, ...] = (), width: int | None = None) -> ttk.Combobox:
        combo = ttk.Combobox(parent, textvariable=var, state="readonly", values=list(values), font=FONT_BODY)
        if width is not None:
            combo.configure(width=width)
        combo.bind("<<ComboboxSelected>>", lambda _e: callback())
        # ttk 下拉選單預設用滾輪切換選項：在可捲動的頁面上，捲動經過時會不小心換掉選項，
        # 所以改成捲動整頁並擋掉預設行為
        combo.bind("<MouseWheel>", lambda e: (self.scroll.scroll(int(-e.delta / 120)), "break")[1])
        return combo

    def _switch(self, parent: tk.Misc, row: int, var: tk.BooleanVar, title: str, description: str,
                command: Callable[[], None]) -> None:
        """一列開關：左側開關，右側標題與說明（點文字也能切換）。"""
        m = self.app.metrics
        frame = tk.Frame(parent, bg=CARD)
        frame.grid(row=row, column=0, columnspan=4, sticky="we", pady=m.px(6))
        switch = ToggleSwitch(frame, var, command, surface=CARD, metrics=m)
        switch.pack(side="left", anchor="n", pady=(m.px(2), 0))
        texts = tk.Frame(frame, bg=CARD, cursor="hand2")
        texts.pack(side="left", fill="x", expand=True, padx=(m.px(12), 0))
        labels = [tk.Label(texts, text=title, font=FONT_BODY, bg=CARD, fg=FG, anchor="w", cursor="hand2")]
        if description:
            labels.append(tk.Label(texts, text=description, font=FONT_SMALL, bg=CARD, fg=FG_SUBTLE, anchor="w",
                                   cursor="hand2"))
        for label in labels:
            label.pack(fill="x")
            label.bind("<Button-1>", lambda _e: switch.toggle())
