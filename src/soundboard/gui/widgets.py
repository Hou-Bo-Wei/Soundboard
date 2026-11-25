"""可重複使用的自訂元件：圓角按鈕、圓角卡片、開關、捲動區。

都只用純 Tk（Canvas 畫圖），不依賴額外套件：小視窗、全域快捷鍵與 PyInstaller 打包都以純 Tk 為前提。
"""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable
from tkinter import font as tkfont
from tkinter import ttk
from typing import Literal

from soundboard.gui.presenters import wrap_text
from soundboard.gui.styles import FONT_BODY, FONT_SMALL, Metrics
from soundboard.theme import FG, FG_MUTED, OK

ButtonLayout = Literal["center", "row", "tile"]
SWITCH_OFF = "#80848e"

_FONTS: dict[tuple[object, ...], tkfont.Font] = {}


def _font(spec: tuple[object, ...]) -> tkfont.Font:
    """取得（並快取）字型物件。

    每個按鈕都建一個 Font 會產生大量具名字型；同樣的字型共用一個物件即可。

    Args:
        spec (tuple[object, ...]): 字型設定，例如 ``("Microsoft JhengHei UI", 10, "bold")``。

    Returns:
        tkfont.Font: 字型物件。
    """
    if spec not in _FONTS:
        _FONTS[spec] = tkfont.Font(font=spec)
    return _FONTS[spec]


def line_height(spec: tuple[object, ...]) -> int:
    """某個字型的行高（像素，已含 DPI 縮放）。

    Args:
        spec (tuple[object, ...]): 字型設定。

    Returns:
        int: 行高。
    """
    return int(_font(spec).metrics("linespace"))


def rounded_rect_points(x1: float, y1: float, x2: float, y2: float, radius: float) -> list[float]:
    """圓角矩形的頂點，搭配 ``create_polygon(..., smooth=True)`` 使用。

    每個直邊的端點都重複一次：平滑曲線在重複的點上會變成直線，只有角落會被修圓。

    Args:
        x1 (float): 左。
        y1 (float): 上。
        x2 (float): 右。
        y2 (float): 下。
        radius (float): 圓角半徑（會自動限制在寬高的一半以內）。

    Returns:
        list[float]: 交錯的 x, y 座標。
    """
    r = max(0.0, min(radius, (x2 - x1) / 2, (y2 - y1) / 2))
    return [
        x1 + r, y1, x1 + r, y1, x2 - r, y1, x2 - r, y1, x2, y1,
        x2, y1 + r, x2, y1 + r, x2, y2 - r, x2, y2 - r, x2, y2,
        x2 - r, y2, x2 - r, y2, x1 + r, y2, x1 + r, y2, x1, y2,
        x1, y2 - r, x1, y2 - r, x1, y1 + r, x1, y1 + r, x1, y1,
    ]


class RoundedButton(tk.Canvas):
    """圓角按鈕（用 Canvas 繪製）。

    三種版面：``center``（文字置中，一般按鈕）、``row``（左標題右說明，導覽列與群組標題）、
    ``tile``（音效按鈕：名稱自動斷行置中，第二行小字貼底）。

    點擊在「放開且游標仍在按鈕上」時才觸發，與一般按鈕一致；
    拖曳開始時呼叫 :meth:`cancel_press` 就不會在放開時誤觸播放。
    """

    def __init__(
        self,
        parent: tk.Misc,
        text: str = "",
        command: Callable[[], object] | None = None,
        *,
        surface: str,
        bg: str,
        hover: str | None = None,
        fg: str = FG,
        font: tuple[object, ...] = FONT_BODY,
        subtext: str = "",
        subfg: str = FG_MUTED,
        subfont: tuple[object, ...] = FONT_SMALL,
        layout: ButtonLayout = "center",
        radius: int = 8,
        padx: int = 12,
        pady: int = 6,
        width: int | None = None,
        height: int | None = None,
        max_lines: int = 3,
    ) -> None:
        """
        Args:
            parent (tk.Misc): 父元件。
            text (str): 主文字。
            command (Callable[[], object] | None): 點擊時呼叫。
            surface (str): 父元件底色（畫在圓角外側，看起來才像圓角）。
            bg (str): 按鈕底色。
            hover (str | None): 滑鼠移入時的底色，None 表示不變。
            fg (str): 文字顏色。
            font (tuple[object, ...]): 主文字字型。
            subtext (str): 第二段文字（row 版面顯示在右側，其他版面顯示在下方）。
            subfg (str): 第二段文字顏色。
            subfont (tuple[object, ...]): 第二段文字字型。
            layout (ButtonLayout): 版面。
            radius (int): 圓角半徑（像素）。
            padx (int): 左右內距（像素）。
            pady (int): 上下內距（像素）。
            width (int | None): 固定寬度；None 依文字自動計算。
            height (int | None): 固定高度；None 依文字自動計算。
            max_lines (int): tile 版面主文字最多幾行。
        """
        super().__init__(parent, bg=surface, highlightthickness=0, bd=0, cursor="hand2", takefocus=0)
        self._text = text
        self._subtext = subtext
        self._command = command
        self._bg = bg
        self._hover = hover if hover is not None else bg
        self._fg = fg
        self._subfg = subfg
        self._font_spec = font
        self._subfont_spec = subfont
        self._layout = layout
        self._radius = radius
        self._padx = padx
        self._pady = pady
        self._fixed = (width, height)
        self._max_lines = max_lines
        self._outline: str | None = None
        self._inside = False
        self._armed = False
        self._shape: int | None = None
        self._fit_to_content()
        self.bind("<Configure>", lambda e: self._draw(e.width, e.height), add="+")
        self.bind("<Enter>", lambda _e: self._set_inside(True), add="+")
        self.bind("<Leave>", lambda _e: self._set_inside(False), add="+")
        self.bind("<ButtonPress-1>", self._on_press, add="+")
        self.bind("<ButtonRelease-1>", self._on_release, add="+")

    # ------------------------------------------------------------------ 公開 API
    @property
    def text(self) -> str:
        """主文字。"""
        return self._text

    @property
    def subtext(self) -> str:
        """第二段文字。"""
        return self._subtext

    def invoke(self) -> None:
        """模擬點擊（等同使用者按下並放開）。"""
        if self._command is not None:
            self._command()

    def cancel_press(self) -> None:
        """取消這次按下：放開滑鼠時不觸發點擊（拖曳開始時使用）。"""
        self._armed = False

    def set_text(self, text: str, subtext: str | None = None) -> None:
        """更新文字。

        Args:
            text (str): 主文字。
            subtext (str | None): 第二段文字；None 表示不變。
        """
        self._text = text
        if subtext is not None:
            self._subtext = subtext
        self._fit_to_content()
        self._redraw()

    def set_colors(self, bg: str, hover: str | None = None, fg: str | None = None) -> None:
        """更新顏色。

        Args:
            bg (str): 底色。
            hover (str | None): 滑鼠移入時的底色；None 表示與底色相同（例如播放中不要變色）。
            fg (str | None): 文字顏色；None 表示不變。
        """
        self._bg = bg
        self._hover = hover if hover is not None else bg
        if fg is not None:
            self._fg = fg
            self._redraw()
            return
        if self._shape is not None and self._outline is None:
            color = self._current_fill()
            self.itemconfigure(self._shape, fill=color, outline=color)
        else:
            self._redraw()

    def set_outline(self, color: str | None) -> None:
        """設定外框（None 表示沒有外框），用來標示被選取。

        Args:
            color (str | None): 外框顏色。
        """
        if color != self._outline:
            self._outline = color
            self._redraw()

    # ------------------------------------------------------------------ 繪製
    def _current_fill(self) -> str:
        return self._hover if self._inside else self._bg

    def _fit_to_content(self) -> None:
        width, height = self._fixed
        if width is not None and height is not None:
            self.configure(width=width, height=height)
            return
        font, subfont = _font(self._font_spec), _font(self._subfont_spec)
        lines = self._text.split("\n")
        text_w = max((font.measure(line) for line in lines), default=0)
        text_h = font.metrics("linespace") * len(lines)
        if self._subtext:
            if self._layout == "row":
                text_w += subfont.measure(self._subtext) + self._padx
            else:
                text_w = max(text_w, subfont.measure(self._subtext))
                text_h += subfont.metrics("linespace")
        self.configure(width=width if width is not None else text_w + 2 * self._padx,
                       height=height if height is not None else text_h + 2 * self._pady)

    def _redraw(self) -> None:
        width, height = self.winfo_width(), self.winfo_height()
        if width <= 1 or height <= 1:
            # 還沒顯示在畫面上：等 <Configure> 拿到真正大小再畫
            width, height = int(self.cget("width")), int(self.cget("height"))
        self._draw(width, height)

    def _draw(self, width: int, height: int) -> None:
        self.delete("all")
        fill = self._current_fill()
        inset = 1 if self._outline else 0
        points = rounded_rect_points(inset, inset, width - inset, height - inset, self._radius)
        self._shape = self.create_polygon(points, smooth=True, fill=fill, outline=self._outline or fill,
                                          width=2 if self._outline else 1)
        font, subfont = self._font_spec, self._subfont_spec
        if self._layout == "row":
            self.create_text(self._padx, height / 2, text=self._text, anchor="w", font=font, fill=self._fg)
            if self._subtext:
                self.create_text(width - self._padx, height / 2, text=self._subtext, anchor="e",
                                 font=subfont, fill=self._subfg)
            return
        sub_h = _font(subfont).metrics("linespace") if self._subtext else 0
        if self._layout == "tile":
            max_lines = self._max_lines - (1 if self._subtext else 0)
            title = wrap_text(self._text, _font(font).measure, width - 2 * self._padx, max(1, max_lines))
        else:
            title = self._text
        top, bottom = self._pady, height - self._pady - sub_h
        self.create_text(width / 2, (top + bottom) / 2, text=title, justify="center", font=font, fill=self._fg)
        if self._subtext:
            self.create_text(width / 2, height - self._pady, text=self._subtext, anchor="s",
                             font=subfont, fill=self._subfg)

    # ------------------------------------------------------------------ 事件
    def _set_inside(self, inside: bool) -> None:
        self._inside = inside
        if self._shape is not None:
            color = self._current_fill()
            self.itemconfigure(self._shape, fill=color, outline=self._outline or color)

    def _on_press(self, _event: tk.Event) -> None:
        self._armed = True

    def _on_release(self, event: tk.Event) -> None:
        armed, self._armed = self._armed, False
        # 和一般按鈕一樣：在按鈕外放開代表反悔，不觸發
        if armed and 0 <= event.x < self.winfo_width() and 0 <= event.y < self.winfo_height():
            self.invoke()


class RoundedFrame(tk.Canvas):
    """圓角卡片容器：把元件放在 :attr:`inner` 裡，高度會自動跟著內容。"""

    def __init__(self, parent: tk.Misc, *, surface: str, bg: str, radius: int, padding: int,
                 width: int = 1) -> None:
        """
        Args:
            parent (tk.Misc): 父元件。
            surface (str): 父元件底色。
            bg (str): 卡片底色。
            radius (int): 圓角半徑（像素）。
            padding (int): 內距（像素）。
            width (int): 要求的最小寬度；通常由 pack/grid 拉伸，所以預設很小，
                避免 Canvas 預設的 10 公分寬度把視窗撐大。
        """
        super().__init__(parent, bg=surface, highlightthickness=0, bd=0, height=2 * padding, width=width)
        self._bg = bg
        self._radius = radius
        self._padding = padding
        self.inner = tk.Frame(self, bg=bg)
        self._window = self.create_window(padding, padding, window=self.inner, anchor="nw")
        self._shape = self.create_polygon(0, 0, 0, 0, smooth=True, fill=bg, outline=bg)
        self.tag_lower(self._shape)
        self.inner.bind("<Configure>", self._fit_height, add="+")
        self.bind("<Configure>", self._on_resize, add="+")

    def set_color(self, bg: str) -> None:
        """更改卡片底色（內層元件的底色需由呼叫端自行更改）。

        Args:
            bg (str): 新底色。
        """
        self._bg = bg
        self.inner.configure(bg=bg)
        self.itemconfigure(self._shape, fill=bg, outline=bg)

    def _fit_height(self, _event: tk.Event) -> None:
        height = self.inner.winfo_reqheight() + 2 * self._padding
        if int(self.cget("height")) != height:
            self.configure(height=height)

    def _on_resize(self, event: tk.Event) -> None:
        self.itemconfigure(self._window, width=max(1, event.width - 2 * self._padding))
        self.coords(self._shape, *rounded_rect_points(0, 0, event.width, event.height, self._radius))


class ToggleSwitch(tk.Canvas):
    """開關（取代勾選框），狀態存在傳入的 BooleanVar。"""

    def __init__(self, parent: tk.Misc, variable: tk.BooleanVar, command: Callable[[], object] | None = None,
                 *, surface: str, metrics: Metrics) -> None:
        """
        Args:
            parent (tk.Misc): 父元件。
            variable (tk.BooleanVar): 開關狀態。
            command (Callable[[], object] | None): 使用者切換後呼叫。
            surface (str): 父元件底色。
            metrics (Metrics): 尺寸縮放。
        """
        # 不能叫 _w：那是 Tk 元件內部存放路徑名稱的屬性
        self._width, self._height = metrics.px(40), metrics.px(22)
        super().__init__(parent, width=self._width, height=self._height, bg=surface, highlightthickness=0, bd=0,
                         cursor="hand2", takefocus=0)
        self.variable = variable
        self._command = command
        trace = variable.trace_add("write", lambda *_a: self._draw())
        # 變數比元件活得久：元件銷毀時要移除監聽，否則之後改變數會畫到已銷毀的元件上
        self.bind("<Destroy>", lambda _e: variable.trace_remove("write", trace), add="+")
        self.bind("<Button-1>", lambda _e: self.toggle(), add="+")
        self._draw()

    def toggle(self) -> None:
        """切換狀態並通知呼叫端。"""
        self.variable.set(not self.variable.get())
        if self._command is not None:
            self._command()

    def _draw(self) -> None:
        self.delete("all")
        on = bool(self.variable.get())
        w, h = self._width, self._height
        self.create_polygon(rounded_rect_points(0, 0, w, h, h / 2), smooth=True,
                            fill=OK if on else SWITCH_OFF, outline=OK if on else SWITCH_OFF)
        margin = max(2, h // 8)
        d = h - 2 * margin
        x = w - margin - d if on else margin
        self.create_oval(x, margin, x + d, margin + d, fill="#ffffff", outline="#ffffff")


class ScrollArea(tk.Frame):
    """可垂直捲動的區域：把元件放在 :attr:`inner` 裡。"""

    def __init__(self, parent: tk.Misc, *, bg: str, border: int = 0,
                 on_resize: Callable[[int], None] | None = None,
                 on_scroll: Callable[[], None] | None = None) -> None:
        """
        Args:
            parent (tk.Misc): 父元件。
            bg (str): 底色。
            border (int): 外框粗細（預覽模式用綠框提示）。
            on_resize (Callable[[int], None] | None): 可視寬度改變時呼叫（參數為新寬度）。
            on_scroll (Callable[[], None] | None): 捲動位置改變時呼叫（虛擬捲動用）。
        """
        super().__init__(parent, bg=bg)
        self._on_resize = on_resize
        self._on_scroll = on_scroll
        self._virtual_height: int | None = None
        self.canvas = tk.Canvas(self, bg=bg, bd=0, highlightthickness=border,
                                highlightbackground=bg, highlightcolor=bg)
        self.scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview,
                                       style="Thin.Vertical.TScrollbar")
        self.canvas.configure(yscrollcommand=self._on_yscroll)
        self.scrollbar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.inner = tk.Frame(self.canvas, bg=bg)
        self._window = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", lambda _e: self._update_region(), add="+")
        self.canvas.bind("<Configure>", self._on_canvas_configure, add="+")

    def set_virtual_height(self, height: int | None) -> None:
        """改用指定的總高度當捲動範圍（虛擬捲動：內容由呼叫端直接畫在 :attr:`canvas` 上，不放進 :attr:`inner`）。

        Tk 的單一元件高度上限約 32767 像素，幾千張貼圖的清單比這更高，所以內容必須畫在 Canvas 上。

        Args:
            height (int | None): 總高度（像素）；None 恢復成依 :attr:`inner` 的大小捲動。
        """
        self._virtual_height = height
        self._update_region()

    def _update_region(self) -> None:
        if self._virtual_height is None:
            self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        else:
            self.canvas.configure(scrollregion=(0, 0, max(1, self.canvas.winfo_width()), self._virtual_height))

    def contains(self, widget: tk.Misc | None) -> bool:
        """某個元件是否位於這個捲動區內。

        Args:
            widget (tk.Misc | None): 要檢查的元件。

        Returns:
            bool: 在捲動區內時為 True。
        """
        while widget is not None:
            if widget is self.canvas:
                return True
            widget = widget.master
        return False

    def scroll(self, units: int) -> None:
        """捲動指定的單位數（正數往下）。

        Args:
            units (int): 捲動量。
        """
        self.canvas.yview_scroll(units, "units")

    def set_border_color(self, color: str) -> None:
        """設定外框顏色。

        Args:
            color (str): 顏色。
        """
        self.canvas.configure(highlightbackground=color, highlightcolor=color)

    def _on_yscroll(self, first: str, last: str) -> None:
        self.scrollbar.set(first, last)
        if self._on_scroll is not None:
            self._on_scroll()
        idle = float(first) <= 0.0 and float(last) >= 1.0
        style = "Idle.Thin.Vertical.TScrollbar" if idle else "Thin.Vertical.TScrollbar"
        if self.scrollbar.cget("style") != style:
            self.scrollbar.configure(style=style)

    def _on_canvas_configure(self, event: tk.Event) -> None:
        self.canvas.itemconfigure(self._window, width=event.width)
        if self._on_resize is not None:
            self._on_resize(event.width)
