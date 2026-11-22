"""螢幕角落的半透明小音效版（置頂、不搶焦點）。"""

from __future__ import annotations

import ctypes
import sys
import tkinter as tk
from collections.abc import Callable

from soundboard.i18n import tr
from soundboard.models import AppSettings, SoundItem
from soundboard.theme import BG, BG_DARK, BTN, BTN_HOVER, FG, FG_MUTED, FONT_FAMILY, PREVIEW

ALPHA_CHOICES: tuple[int, ...] = (30, 50, 70, 85, 100)
COLUMN_CHOICES: tuple[int, ...] = (1, 2, 3, 4, 5)
MAX_NAME_CHARS = 12
EDGE_MARGIN = 16

# Win32 視窗樣式
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080   # 不出現在工作列 / Alt+Tab
WS_EX_NOACTIVATE = 0x08000000   # 點擊不會奪走焦點：遊戲不會被切到背景


def _virtual_screen(widget: tk.Misc) -> tuple[int, int, int, int]:
    """取得所有螢幕合起來的範圍（多螢幕時座標可能為負）。

    Args:
        widget (tk.Misc): 任一 Tk 元件。

    Returns:
        tuple[int, int, int, int]: (left, top, width, height)。
    """
    if sys.platform == "win32":
        metric = ctypes.windll.user32.GetSystemMetrics
        return metric(76), metric(77), metric(78), metric(79)
    return 0, 0, widget.winfo_screenwidth(), widget.winfo_screenheight()


def _shorten(name: str, limit: int = MAX_NAME_CHARS) -> str:
    return name if len(name) <= limit else name[: limit - 1] + "…"


class OverlayWindow:
    """可拖曳的置頂小視窗，只顯示使用者挑選的音效。

    設定（位置、透明度、欄數）直接寫回傳入的 :class:`AppSettings`，
    由 ``on_changed`` 通知主程式存檔。
    """

    def __init__(
        self,
        master: tk.Tk,
        settings: AppSettings,
        *,
        on_play: Callable[[SoundItem, bool], None],
        on_stop: Callable[[], None],
        on_hidden: Callable[[], None],
        on_changed: Callable[[], None],
        on_group_changed: Callable[[], None],
    ) -> None:
        """
        Args:
            master (tk.Tk): 主視窗。
            settings (AppSettings): 使用者設定（會被直接修改）。
            on_play (Callable[[SoundItem, bool], None]): 播放音效；第二個參數 True 表示強制預覽。
            on_stop (Callable[[], None]): 全部停止。
            on_hidden (Callable[[], None]): 使用者從小視窗上把它關掉時呼叫。
            on_changed (Callable[[], None]): 位置或外觀改變，需要存檔時呼叫。
            on_group_changed (Callable[[], None]): 使用者切換了要顯示的群組
                （已寫入 ``settings.overlay_group``），主程式需重新提供音效。
        """
        self.settings = settings
        self._on_play = on_play
        self._on_stop = on_stop
        self._on_hidden = on_hidden
        self._on_changed = on_changed
        self._on_group_changed = on_group_changed
        self._buttons: dict[str, tk.Button] = {}
        self._colors: dict[str, str] = {}
        self._drag_offset: tuple[int, int] | None = None
        self._style_applied = False

        self.top = tk.Toplevel(master)
        self.top.withdraw()
        self.top.overrideredirect(True)  # 無標題列，看起來像遊戲內的浮動面板
        self.top.attributes("-topmost", True)
        self.top.configure(bg=BG_DARK, highlightthickness=1, highlightbackground=BTN)
        self._apply_alpha()

        self.header = tk.Frame(self.top, bg=BG, cursor="fleur")
        self.header.pack(fill="x")
        self.title = tk.Label(self.header, text=tr("🔊 音效版"), bg=BG, fg=FG, font=(FONT_FAMILY, 9, "bold"), padx=6)
        self.title.pack(side="left", pady=2)
        for text, command in (("✕", self.hide_by_user), ("⋮", self._show_menu), ("⏹", on_stop)):
            tk.Button(self.header, text=text, command=command, bg=BG, fg=FG_MUTED, activebackground=BTN_HOVER,
                      activeforeground=FG, relief="flat", bd=0, padx=6, cursor="hand2",
                      font=(FONT_FAMILY, 9)).pack(side="right")
        for widget in (self.header, self.title):
            widget.bind("<ButtonPress-1>", self._drag_start)
            widget.bind("<B1-Motion>", self._drag_move)
            widget.bind("<ButtonRelease-1>", self._drag_end)
            widget.bind("<Button-3>", lambda _e: self._show_menu())

        # 群組切換列：◀ 群組名稱 ▶；沒有任何群組時隱藏
        self.switcher = tk.Frame(self.top, bg=BG_DARK)
        arrow = {"bg": BG_DARK, "fg": FG, "activebackground": BTN_HOVER, "activeforeground": FG,
                 "relief": "flat", "bd": 0, "padx": 8, "cursor": "hand2", "font": (FONT_FAMILY, 9, "bold")}
        tk.Button(self.switcher, text="◀", command=lambda: self._cycle(-1), **arrow).pack(side="left")
        tk.Button(self.switcher, text="▶", command=lambda: self._cycle(1), **arrow).pack(side="right")
        self.source_label = tk.Label(self.switcher, bg=BG_DARK, fg=FG, cursor="hand2",
                                     font=(FONT_FAMILY, 9, "bold"))
        self.source_label.pack(side="left", fill="x", expand=True)
        self.source_label.bind("<Button-1>", lambda _e: self._show_group_menu())
        for widget in (self.switcher, self.source_label):
            widget.bind("<MouseWheel>", lambda e: (self._cycle(-1 if e.delta > 0 else 1), "break")[1])

        self.body = tk.Frame(self.top, bg=BG_DARK, padx=4, pady=4)
        self.body.pack(fill="both", expand=True)

    # ------------------------------------------------------------------ 顯示
    @property
    def visible(self) -> bool:
        """小視窗是否顯示中。"""
        return self.top.winfo_viewable() == 1

    def show(self) -> None:
        """顯示小視窗。"""
        self.top.deiconify()
        self.top.update_idletasks()
        if not self._style_applied:
            self._make_non_activating()
        self._place()
        self.top.lift()

    def hide(self) -> None:
        """隱藏小視窗。"""
        self.top.withdraw()

    def hide_by_user(self) -> None:
        """使用者按下 ✕。"""
        self.hide()
        self._on_hidden()

    def destroy(self) -> None:
        """銷毀視窗。"""
        self.top.destroy()

    def _make_non_activating(self) -> None:
        if sys.platform != "win32":
            return
        user32 = ctypes.windll.user32
        # winfo_id 是 Tk 內層視窗，真正的頂層視窗是它的父視窗
        hwnd = user32.GetParent(self.top.winfo_id()) or self.top.winfo_id()
        style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW)
        self._style_applied = True

    def _apply_alpha(self) -> None:
        alpha = min(1.0, max(0.3, float(self.settings.overlay_alpha)))
        self.top.attributes("-alpha", alpha)

    def _place(self) -> None:
        """套用儲存的位置；沒有或超出螢幕時放到右下角。"""
        self.top.update_idletasks()
        width, height = self.top.winfo_reqwidth(), self.top.winfo_reqheight()
        left, top, vw, vh = _virtual_screen(self.top)
        x, y = self.settings.overlay_x, self.settings.overlay_y
        if x is None or y is None:
            # 預設放在主螢幕右下角，避開工作列
            x = self.top.winfo_screenwidth() - width - EDGE_MARGIN
            y = self.top.winfo_screenheight() - height - 60
        # 換過螢幕配置後舊座標可能跑到畫面外，拉回可見範圍
        x = min(max(x, left), left + vw - width)
        y = min(max(y, top), top + vh - height)
        self.top.geometry(f"+{x}+{y}")

    def move_to_corner(self, corner: str) -> None:
        """移到主螢幕的某個角落。

        Args:
            corner (str): ``"br"``、``"bl"``、``"tr"``、``"tl"``（下右、下左、上右、上左）。
        """
        self.top.update_idletasks()
        width, height = self.top.winfo_reqwidth(), self.top.winfo_reqheight()
        sw, sh = self.top.winfo_screenwidth(), self.top.winfo_screenheight()
        x = sw - width - EDGE_MARGIN if corner.endswith("r") else EDGE_MARGIN
        y = sh - height - 60 if corner.startswith("b") else EDGE_MARGIN
        self.settings.overlay_x, self.settings.overlay_y = x, y
        self.top.geometry(f"+{x}+{y}")
        self._on_changed()

    # ------------------------------------------------------------------ 內容
    def set_items(self, items: list[SoundItem], hotkey_text: dict[str, str]) -> None:
        """重建小視窗上的音效按鈕。

        依 ``settings.overlay_group`` 顯示目前的來源名稱；有群組時才顯示切換列。

        Args:
            items (list[SoundItem]): 要顯示的音效（依顯示順序）。
            hotkey_text (dict[str, str]): 音效 key → 快捷鍵文字。
        """
        group = self.settings.overlay_group
        if self.settings.sound_groups:
            sources = [None, *self.settings.sound_groups]
            position = f"{sources.index(group) + 1}/{len(sources)}" if group in sources else ""
            title = tr("📌 釘選") if group is None else f"🗂 {_shorten(group, 10)}"
            self.source_label.configure(text=f"{title}  {position}")
            if not self.switcher.winfo_ismapped():
                self.switcher.pack(fill="x", before=self.body)
        else:
            self.switcher.pack_forget()
        for child in self.body.winfo_children():
            child.destroy()
        self._buttons.clear()
        self._colors.clear()
        if not items:
            hint = (tr("請在首頁的音效上按右鍵\n並選擇「📌 加入浮動視窗」") if group is None
                    else tr("此群組中沒有音效\n（請在首頁將音效拖曳到群組中）"))
            tk.Label(self.body, text=hint, bg=BG_DARK, fg=FG_MUTED,
                     font=(FONT_FAMILY, 9), padx=12, pady=10, justify="center").pack()
        columns = max(1, int(self.settings.overlay_columns))
        for i, item in enumerate(items):
            text = _shorten(item.display_name)
            if hotkey_text.get(item.key):
                text += f"\n{hotkey_text[item.key]}"
            btn = tk.Button(self.body, text=text, width=13, height=2, bg=BTN, fg=FG, activebackground=BTN_HOVER,
                            activeforeground=FG, relief="flat", bd=0, cursor="hand2", font=(FONT_FAMILY, 9),
                            command=lambda it=item: self._on_play(it, False))
            btn.bind("<Shift-Button-1>", lambda _e, it=item: (self._on_play(it, True), "break")[1])
            btn.grid(row=i // columns, column=i % columns, padx=2, pady=2)
            self._buttons[item.key] = btn
        if self.visible:
            self._place()

    def set_playing(self, colors: dict[str, str]) -> None:
        """更新播放中按鈕的顏色。

        Args:
            colors (dict[str, str]): 播放中的音效 key → 底色。
        """
        for key, btn in self._buttons.items():
            color = colors.get(key, BTN)
            if self._colors.get(key, BTN) != color:
                btn.configure(bg=color)
        self._colors = {k: v for k, v in colors.items() if k in self._buttons}

    def set_preview(self, preview: bool) -> None:
        """依預覽模式改變標題列，提醒現在朋友聽不到。

        Args:
            preview (bool): 預覽模式是否開啟。
        """
        color = PREVIEW if preview else BG
        self.header.configure(bg=color)
        self.title.configure(bg=color, text=tr("🎧 試聽中（僅本機）") if preview else tr("🔊 音效版"))
        for child in self.header.winfo_children():
            if isinstance(child, tk.Button):
                child.configure(bg=color)

    # ------------------------------------------------------------------ 群組切換
    def _cycle(self, step: int) -> None:
        if not self.settings.sound_groups:
            return
        self.settings.cycle_overlay_group(step)
        self._on_group_changed()

    def _select_group(self, group: str | None) -> None:
        self.settings.overlay_group = group
        self._on_group_changed()

    def _show_group_menu(self) -> None:
        menu = tk.Menu(self.top, tearoff=0, bg=BG, fg=FG, activebackground=BTN_HOVER, activeforeground=FG)
        current = self.settings.overlay_group
        menu.add_command(label=("● " if current is None else "　 ") + tr("📌 釘選的音效"),
                         command=lambda: self._select_group(None))
        for name in self.settings.sound_groups:
            menu.add_command(label=("● " if current == name else "　 ") + f"🗂 {name}",
                             command=lambda g=name: self._select_group(g))
        try:
            menu.tk_popup(self.top.winfo_pointerx(), self.top.winfo_pointery())
        finally:
            menu.grab_release()

    # ------------------------------------------------------------------ 拖曳 / 選單
    def _drag_start(self, event: tk.Event) -> None:
        self._drag_offset = (event.x_root - self.top.winfo_x(), event.y_root - self.top.winfo_y())

    def _drag_move(self, event: tk.Event) -> None:
        if self._drag_offset is None:
            return
        dx, dy = self._drag_offset
        self.top.geometry(f"+{event.x_root - dx}+{event.y_root - dy}")

    def _drag_end(self, _event: tk.Event) -> None:
        if self._drag_offset is None:
            return
        self._drag_offset = None
        self.settings.overlay_x, self.settings.overlay_y = self.top.winfo_x(), self.top.winfo_y()
        self._on_changed()

    def _show_menu(self) -> None:
        menu = tk.Menu(self.top, tearoff=0, bg=BG, fg=FG, activebackground=BTN_HOVER, activeforeground=FG)
        alpha_menu = tk.Menu(menu, tearoff=0, bg=BG, fg=FG, activebackground=BTN_HOVER)
        alpha_var = tk.IntVar(value=round(self.settings.overlay_alpha * 100))
        for pct in ALPHA_CHOICES:
            alpha_menu.add_radiobutton(label=f"{pct}%", value=pct, variable=alpha_var,
                                       command=lambda p=pct: self._set_alpha(p))
        col_menu = tk.Menu(menu, tearoff=0, bg=BG, fg=FG, activebackground=BTN_HOVER)
        col_var = tk.IntVar(value=self.settings.overlay_columns)
        for n in COLUMN_CHOICES:
            col_menu.add_radiobutton(label=tr("{count} 欄", count=n), value=n, variable=col_var,
                                     command=lambda c=n: self._set_columns(c))
        corner_menu = tk.Menu(menu, tearoff=0, bg=BG, fg=FG, activebackground=BTN_HOVER)
        for label, corner in ((tr("右下"), "br"), (tr("右上"), "tr"), (tr("左下"), "bl"), (tr("左上"), "tl")):
            corner_menu.add_command(label=label, command=lambda c=corner: self.move_to_corner(c))
        if self.settings.sound_groups:
            group_menu = tk.Menu(menu, tearoff=0, bg=BG, fg=FG, activebackground=BTN_HOVER)
            group_var = tk.StringVar(value=self.settings.overlay_group or "")
            group_menu.add_radiobutton(label=tr("📌 釘選的音效"), value="", variable=group_var,
                                       command=lambda: self._select_group(None))
            for name in self.settings.sound_groups:
                group_menu.add_radiobutton(label=f"🗂 {name}", value=name, variable=group_var,
                                           command=lambda g=name: self._select_group(g))
            menu.add_cascade(label=tr("顯示群組"), menu=group_menu)
        else:
            group_var = None
        menu.add_cascade(label=tr("不透明度"), menu=alpha_menu)
        menu.add_cascade(label=tr("每列按鈕數"), menu=col_menu)
        menu.add_cascade(label=tr("移到角落"), menu=corner_menu)
        menu.add_separator()
        menu.add_command(label=tr("⏹ 全部停止"), command=self._on_stop)
        menu.add_command(label=tr("✕ 隱藏浮動視窗"), command=self.hide_by_user)
        # 保留 Tk 變數的參照，否則選單顯示時會被垃圾回收而看不到勾選狀態
        menu._vars = (alpha_var, col_var, group_var)  # type: ignore[attr-defined]
        try:
            menu.tk_popup(self.top.winfo_pointerx(), self.top.winfo_pointery())
        finally:
            menu.grab_release()

    def _set_alpha(self, percent: int) -> None:
        self.settings.overlay_alpha = percent / 100
        self._apply_alpha()
        self._on_changed()

    def _set_columns(self, columns: int) -> None:
        self.settings.overlay_columns = columns
        self._regrid()
        self._on_changed()

    def _regrid(self) -> None:
        # 只是改欄數：重新排列現有按鈕即可，不必重建
        buttons = list(self._buttons.values())
        columns = max(1, self.settings.overlay_columns)
        for i, btn in enumerate(buttons):
            btn.grid_configure(row=i // columns, column=i % columns)
        self._place()
