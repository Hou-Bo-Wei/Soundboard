"""混音器的通道條：標題、狀態、垂直推桿＋音量表、百分比、靜音鈕、說明框。"""

from __future__ import annotations

import math
import tkinter as tk
from collections.abc import Callable

from soundboard.gui.styles import FONT_BOLD, FONT_H2, FONT_SMALL, FONT_SMALL_BOLD, Metrics
from soundboard.gui.widgets import RoundedButton
from soundboard.i18n import SOURCE_LANGUAGE, current_language, tr
from soundboard.theme import BG_DARK, BTN, BTN_HOVER, CARD, ERR, ERR_HOVER, FG, FG_MUTED, FG_SUBTLE, OK, WARN

MAX_PERCENT = 200
WHEEL_STEP = 5
# 通道下方說明框的行數；英文等語言的字較長，多留一行才不會被切掉
INFO_LINES = 3
STRIP_WIDTH = 168       # 100% 縮放時的通道寬度
FADER_HEIGHT = 250
STRIP_HEADER = "#1e1f22"
TRACK = "#3a3c42"
METER_BG = "#1a1b1e"
# 音量表：-60 dB 到 0 dB；-12 dB 以上變黃、-3 dB 以上變紅（接近被限幅）
METER_FLOOR_DB = -60.0
METER_WARN_DB = -12.0
METER_HOT_DB = -3.0


def value_to_fraction(percent: float, maximum: float = MAX_PERCENT) -> float:
    """音量百分比 → 推桿位置（0 = 最下面，1 = 最上面）。"""
    return min(max(percent / maximum, 0.0), 1.0)


def fraction_to_value(fraction: float, maximum: float = MAX_PERCENT) -> float:
    """推桿位置 → 音量百分比（對齊到整數）。"""
    return float(round(min(max(fraction, 0.0), 1.0) * maximum))


def meter_fraction(peak: float) -> float:
    """峰值（0–1 以上）→ 音量表高度比例（以 dB 刻度，小聲時也看得出在動）。"""
    if peak <= 0:
        return 0.0
    db = 20 * math.log10(peak)
    return min(max((db - METER_FLOOR_DB) / -METER_FLOOR_DB, 0.0), 1.0)


def meter_color(peak: float) -> str:
    """音量表顏色。"""
    if peak <= 0:
        return OK
    db = 20 * math.log10(peak)
    return ERR if db >= METER_HOT_DB else WARN if db >= METER_WARN_DB else OK


class VerticalFader(tk.Canvas):
    """垂直推桿（左）＋音量表（右）。

    拖曳或點一下設定音量；滾輪每格 5%；雙擊回到 100%。
    """

    def __init__(self, parent: tk.Misc, *, color: str, value: float, metrics: Metrics,
                 on_change: Callable[[float], None], on_release: Callable[[], None]) -> None:
        """
        Args:
            parent (tk.Misc): 父元件。
            color (str): 通道顏色（推桿下方的填色）。
            value (float): 起始音量（百分比）。
            metrics (Metrics): 縮放。
            on_change (Callable[[float], None]): 音量改變時呼叫（拖曳中會連續呼叫）。
            on_release (Callable[[], None]): 放開滑鼠或滾輪調整後呼叫（用來存檔）。
        """
        m = metrics
        super().__init__(parent, width=m.px(STRIP_WIDTH - 24), height=m.px(FADER_HEIGHT), bg=CARD,
                         highlightthickness=0, bd=0, cursor="sb_v_double_arrow")
        self.m = m
        self.color = color
        self.value = value
        self.level = 0.0
        self.on_change = on_change
        self.on_release = on_release
        self._pad = m.px(12)
        self.bind("<Configure>", lambda _e: self.redraw())
        self.bind("<Button-1>", self._drag)
        self.bind("<B1-Motion>", self._drag)
        self.bind("<ButtonRelease-1>", lambda _e: self.on_release())
        self.bind("<Double-Button-1>", lambda _e: self._set(100.0, release=True))
        self.bind("<MouseWheel>", self._wheel)

    # ------------------------------------------------------------------ 座標
    def _span(self) -> tuple[int, int]:
        height = max(self.winfo_height(), int(self.cget("height")))
        return self._pad, height - self._pad

    def _y_of(self, percent: float) -> float:
        top, bottom = self._span()
        return bottom - value_to_fraction(percent) * (bottom - top)

    def _percent_at(self, y: float) -> float:
        top, bottom = self._span()
        return fraction_to_value((bottom - y) / max(1, bottom - top))

    # ------------------------------------------------------------------ 互動
    def _drag(self, event: tk.Event) -> None:
        self._set(self._percent_at(event.y))

    def _wheel(self, event: tk.Event) -> str:
        step = WHEEL_STEP if event.delta > 0 else -WHEEL_STEP
        self._set(min(MAX_PERCENT, max(0.0, self.value + step)), release=True)
        return "break"      # 不要同時捲動整頁

    def _set(self, percent: float, release: bool = False) -> None:
        if percent != self.value:
            self.value = percent
            self.redraw()
            self.on_change(percent)
        if release:
            self.on_release()

    def set_value(self, percent: float) -> None:
        """從外部設定音量（不觸發 callback）。"""
        self.value = percent
        self.redraw()

    def set_level(self, peak: float) -> None:
        """更新音量表（只重畫音量表）。"""
        if abs(peak - self.level) < 1e-4:
            return
        self.level = peak
        self._draw_meter()

    # ------------------------------------------------------------------ 繪製
    def redraw(self) -> None:
        """重畫整個推桿。"""
        self.delete("all")
        m = self.m
        width = max(self.winfo_width(), int(self.cget("width")))
        top, bottom = self._span()
        cx = width // 2 - m.px(10)
        self._meter_x = cx + m.px(26)
        # 刻度：0、100%、200%；100% 是原音量，畫長一點
        for pct in (0, 50, 100, 150, 200):
            y = self._y_of(pct)
            half = m.px(12) if pct == 100 else m.px(6)
            self.create_line(cx - half - m.px(10), y, cx - m.px(10), y, fill=FG_SUBTLE if pct == 100 else TRACK)
        self.create_text(cx - m.px(26), self._y_of(100), text="100", anchor="e", fill=FG_SUBTLE, font=FONT_SMALL)
        self.create_line(cx, top, cx, bottom, fill=TRACK, width=m.px(4), capstyle="round")
        y = self._y_of(self.value)
        self.create_line(cx, y, cx, bottom, fill=self.color, width=m.px(4), capstyle="round")
        hw, hh = m.px(13), m.px(8)
        self.create_rectangle(cx - hw, y - hh, cx + hw, y + hh, fill="#f2f3f5", outline="#c9cbd1")
        self.create_line(cx - m.px(5), y, cx + m.px(5), y, fill=self.color, width=m.px(2))
        self._draw_meter()

    def _draw_meter(self) -> None:
        self.delete("meter")
        m = self.m
        top, bottom = self._span()
        x = getattr(self, "_meter_x", 0)
        w = m.px(6)
        self.create_rectangle(x, top, x + w, bottom, fill=METER_BG, outline="", tags="meter")
        frac = meter_fraction(self.level)
        if frac > 0:
            y = bottom - frac * (bottom - top)
            self.create_rectangle(x, y, x + w, bottom, fill=meter_color(self.level), outline="", tags="meter")


class ChannelStrip(tk.Frame):
    """混音器的一條通道。

    Attributes:
        key (str): 通道代號（``@master``、``@sounds``、``@mic`` 或程式的執行檔名稱）。
    """

    def __init__(self, parent: tk.Misc, *, key: str, title: str, icon: str, color: str, value: float,
                 muted: bool, info: str, metrics: Metrics,
                 on_change: Callable[[str, float], None], on_release: Callable[[str], None],
                 on_mute: Callable[[str, bool], None], on_remove: Callable[[str], None] | None = None) -> None:
        """
        Args:
            parent (tk.Misc): 父元件。
            key (str): 通道代號。
            title (str): 標題。
            icon (str): 標題前的圖示。
            color (str): 通道顏色。
            value (float): 起始音量（百分比）。
            muted (bool): 是否靜音。
            info (str): 下方說明框的文字。
            metrics (Metrics): 縮放。
            on_change (Callable[[str, float], None]): 拖曳推桿時呼叫 (key, 百分比)。
            on_release (Callable[[str], None]): 調整結束時呼叫（存檔）。
            on_mute (Callable[[str, bool], None]): 切換靜音時呼叫。
            on_remove (Callable[[str], None] | None): 有值時標題列顯示 ✕，點了呼叫。
        """
        m = metrics
        super().__init__(parent, bg=CARD, width=m.px(STRIP_WIDTH))
        self.key = key
        self.muted = muted
        self._on_mute = on_mute
        self._color = color
        self.m = m

        header = tk.Frame(self, bg=STRIP_HEADER)
        header.pack(fill="x")
        tk.Label(header, text=f"{icon} {title}", font=FONT_H2, bg=STRIP_HEADER, fg=color, anchor="w").pack(
            side="left", padx=(m.px(10), 0), pady=m.px(8))
        if on_remove is not None:
            close = tk.Label(header, text="✕", font=FONT_BOLD, bg=STRIP_HEADER, fg=FG_SUBTLE, cursor="hand2")
            close.pack(side="right", padx=m.px(8))
            close.bind("<Button-1>", lambda _e: on_remove(key))
            close.bind("<Enter>", lambda _e: close.configure(fg=ERR))
            close.bind("<Leave>", lambda _e: close.configure(fg=FG_SUBTLE))
        self.status = tk.Label(self, text="", font=FONT_SMALL, bg=BG_DARK, fg=FG_MUTED, anchor="w",
                               padx=m.px(10), pady=m.px(4))
        self.status.pack(fill="x")

        self.fader = VerticalFader(self, color=color, value=value, metrics=m,
                                   on_change=lambda v: self._changed(v, on_change),
                                   on_release=lambda: on_release(key))
        self.fader.pack(pady=(m.px(10), 0))
        self.percent = tk.Label(self, text="", font=FONT_BOLD, bg=CARD, fg=FG)
        self.percent.pack(pady=(m.px(2), m.px(4)))
        self.mute_btn = RoundedButton(self, "", self.toggle_mute, surface=CARD, bg=BTN, hover=BTN_HOVER,
                                      font=FONT_SMALL_BOLD, radius=m.px(6), padx=m.px(10), pady=m.px(4),
                                      width=m.px(96))
        self.mute_btn.pack()
        self.info = tk.Label(self, text=info, font=FONT_SMALL, bg=BG_DARK, fg=FG_SUBTLE, anchor="nw",
                             justify="left", wraplength=m.px(STRIP_WIDTH - 28), padx=m.px(8), pady=m.px(6),
                             height=INFO_LINES if current_language() == SOURCE_LANGUAGE else INFO_LINES + 1)
        self.info.pack(fill="x", padx=m.px(8), pady=m.px(10))
        self._show_value(value)
        self._show_mute()

    def _changed(self, value: float, callback: Callable[[str, float], None]) -> None:
        self._show_value(value)
        callback(self.key, value)

    def _show_value(self, value: float) -> None:
        self.percent.configure(text=f"{value:.0f}%")

    def _show_mute(self) -> None:
        if self.muted:
            self.mute_btn.set_text(tr("🔇 已靜音"))
            self.mute_btn.set_colors(ERR, ERR_HOVER)
        else:
            self.mute_btn.set_text(tr("🔊 靜音"))
            self.mute_btn.set_colors(BTN, BTN_HOVER)

    def toggle_mute(self) -> None:
        """切換靜音。"""
        self.set_muted(not self.muted)
        self._on_mute(self.key, self.muted)

    def set_muted(self, muted: bool) -> None:
        """從外部設定靜音狀態（不觸發 callback）。"""
        self.muted = muted
        self._show_mute()

    def set_value(self, value: float) -> None:
        """從外部設定音量（不觸發 callback）。"""
        self.fader.set_value(value)
        self._show_value(value)

    def set_status(self, text: str, color: str = FG_MUTED) -> None:
        """設定標題下方的狀態文字。"""
        if self.status.cget("text") != text or self.status.cget("fg") != color:
            self.status.configure(text=text, fg=color)

    def set_info(self, text: str) -> None:
        """設定下方說明框文字。"""
        if self.info.cget("text") != text:
            self.info.configure(text=text)
