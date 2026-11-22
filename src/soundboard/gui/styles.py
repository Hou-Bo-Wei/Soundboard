"""字型、尺寸縮放與 ttk 樣式設定。"""

from __future__ import annotations

import tkinter as tk
from dataclasses import dataclass
from tkinter import ttk

from soundboard.theme import (
    ACCENT, ACCENT_HOVER, BG, BG_DARK, BTN, BTN_HOVER, BTN_PLAYING, CARD, FG, FG_MUTED, FONT_FAMILY, INPUT,
    SURFACE,
)

FONT_BODY = (FONT_FAMILY, 10)
FONT_BOLD = (FONT_FAMILY, 10, "bold")
FONT_SMALL = (FONT_FAMILY, 9)
FONT_SMALL_BOLD = (FONT_FAMILY, 9, "bold")
FONT_H1 = (FONT_FAMILY, 18, "bold")
FONT_H2 = (FONT_FAMILY, 12, "bold")
FONT_BRAND = (FONT_FAMILY, 13, "bold")

# 以 96 DPI（100%）為基準的版面尺寸；實際使用時乘上 Metrics.scale
BASE_DPI = 96.0


@dataclass(frozen=True)
class Metrics:
    """像素尺寸縮放。

    程式開啟了 DPI 感知（SetProcessDpiAwareness），Tk 的字型（以點為單位）會自動放大，
    但 padx、寬度等像素值不會；不跟著縮放的話，150% 螢幕上文字變大、按鈕卻沒變，會擠成一團。

    Attributes:
        scale (float): 縮放倍率（100% = 1.0）。
    """

    scale: float = 1.0

    @classmethod
    def from_root(cls, root: tk.Misc) -> Metrics:
        """依螢幕 DPI 建立縮放設定。

        Args:
            root (tk.Misc): 任一 Tk 元件。

        Returns:
            Metrics: 縮放設定（不會小於 1.0）。
        """
        return cls(max(1.0, root.winfo_fpixels("1i") / BASE_DPI))

    def px(self, value: float) -> int:
        """把 100% 基準的像素值換成目前螢幕的像素值。

        Args:
            value (float): 100% 時的像素值。

        Returns:
            int: 縮放後的像素值。
        """
        return int(round(value * self.scale))


def setup_style(root: tk.Tk, metrics: Metrics) -> None:
    """設定整個程式共用的 ttk 樣式（深色）。

    對話框（快捷鍵、麥克風測試）也會用到這些樣式，所以預設樣式維持以 BG 為底色。

    Args:
        root (tk.Tk): 根視窗。
        metrics (Metrics): 尺寸縮放。
    """
    root.configure(bg=SURFACE)
    style = ttk.Style(root)
    style.theme_use("clam")
    style.configure(".", background=BG, foreground=FG, fieldbackground=INPUT, bordercolor=BG_DARK,
                    font=FONT_BODY)
    style.configure("TLabel", background=BG, foreground=FG)
    style.configure("Muted.TLabel", foreground=FG_MUTED)
    style.configure("TFrame", background=BG)
    style.configure("TCheckbutton", background=BG, foreground=FG, indicatorbackground=INPUT,
                    indicatorforeground=FG)
    style.map("TCheckbutton", background=[("active", BG)],
              indicatorbackground=[("selected", ACCENT), ("active", BTN)])
    style.configure("TButton", background=BTN, foreground=FG, borderwidth=0, padding=(10, 5))
    style.map("TButton", background=[("active", BTN_HOVER)])

    # 下拉選單：扁平、無立體邊框；唯讀時不要把文字反白（看起來像被選取而不是設定值）
    pad = (metrics.px(8), metrics.px(5))
    style.configure("TCombobox", fieldbackground=INPUT, background=INPUT, foreground=FG, arrowcolor=FG_MUTED,
                    bordercolor=INPUT, lightcolor=INPUT, darkcolor=INPUT, padding=pad, arrowsize=metrics.px(14))
    style.map("TCombobox",
              fieldbackground=[("readonly", INPUT), ("disabled", CARD)],
              foreground=[("disabled", FG_MUTED), ("readonly", FG)],
              selectbackground=[("readonly", INPUT)], selectforeground=[("readonly", FG)],
              background=[("active", BTN)], bordercolor=[("focus", ACCENT)])
    style.configure("TEntry", fieldbackground=INPUT, foreground=FG, insertcolor=FG)

    # 音量滑桿：深色軌道＋主題色滑塊
    style.configure("Horizontal.TScale", background=ACCENT, troughcolor=INPUT, bordercolor=CARD,
                    lightcolor=ACCENT, darkcolor=ACCENT, sliderlength=metrics.px(18), gripcount=0)
    style.map("Horizontal.TScale", background=[("active", ACCENT_HOVER)])

    # 細捲軸：拿掉上下箭頭，只留滑塊，比 Windows 傳統捲軸更不搶眼
    style.layout("Thin.Vertical.TScrollbar", [
        ("Vertical.Scrollbar.trough", {"sticky": "ns", "children": [
            ("Vertical.Scrollbar.thumb", {"expand": "1", "sticky": "nswe"})]})])
    style.configure("Thin.Vertical.TScrollbar", troughcolor=SURFACE, background=BG_DARK, bordercolor=SURFACE,
                    lightcolor=BG_DARK, darkcolor=BG_DARK, gripcount=0, arrowsize=metrics.px(8))
    style.map("Thin.Vertical.TScrollbar", background=[("active", BTN)])
    # 內容不需要捲動時把滑塊畫成和底色一樣（不用隱藏元件，避免寬度變動造成按鈕重排來回跳動）
    style.configure("Idle.Thin.Vertical.TScrollbar", troughcolor=SURFACE, background=SURFACE,
                    bordercolor=SURFACE, lightcolor=SURFACE, darkcolor=SURFACE, gripcount=0)
    style.map("Idle.Thin.Vertical.TScrollbar", background=[("active", SURFACE)])
    style.configure("Vertical.TScrollbar", background=BTN, troughcolor=BG_DARK, arrowcolor=FG)

    # Combobox 的下拉清單是傳統 Tk Listbox，需要用 option database 設定顏色
    root.option_add("*TCombobox*Listbox.background", INPUT)
    root.option_add("*TCombobox*Listbox.foreground", FG)
    root.option_add("*TCombobox*Listbox.selectBackground", BTN_PLAYING)
    root.option_add("*TCombobox*Listbox.font", FONT_BODY)
