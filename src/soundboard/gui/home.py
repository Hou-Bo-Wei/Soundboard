"""首頁：只放音效版本身（搜尋、群組區塊、音效按鈕與常用操作）。"""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from soundboard.grouping import Section, build_sections
from soundboard.gui.menus import show_group_menu, show_sound_menu
from soundboard.gui.presenters import AudioWarning, grid_columns, section_header_text, tile_text
from soundboard.gui.styles import FONT_BODY, FONT_BOLD, FONT_H1, FONT_SMALL, FONT_SMALL_BOLD
from soundboard.gui.widgets import RoundedButton, RoundedFrame, ScrollArea, line_height
from soundboard.i18n import tr
from soundboard.models import SoundItem
from soundboard.theme import (
    ACCENT, ACCENT_HOVER, BANNER_BG, BANNER_FG, BTN, BTN_HOVER, ERR, ERR_HOVER, FG, FG_MUTED, FG_SUBTLE,
    HEADER_HOVER, INPUT, PREVIEW, PREVIEW_HOVER, SURFACE,
)

if TYPE_CHECKING:
    from soundboard.gui.app import SoundboardApp

# 以下為 100% 縮放時的像素值
TILE_WIDTH = 170            # 每個音效按鈕佔用的寬度（含左右間距）
TILE_GAP = 5
PAGE_PADDING = 24
DRAG_THRESHOLD_PX = 8
AUTOSCROLL_MARGIN_PX = 30
MIN_SEARCH_WIDTH = 260      # 搜尋框至少要這麼寬，不夠時工具列按鈕換到下一行


@dataclass
class DragState:
    """首頁中正在拖曳的音效。

    Attributes:
        item (SoundItem): 按下滑鼠的那個音效。
        x (int): 按下時的螢幕 X 座標。
        y (int): 按下時的螢幕 Y 座標。
        active (bool): 是否已移動到足以算作拖曳。
        keys (list[str]): 實際要移動的音效（包含複選的其他音效）。
        ghost (tk.Toplevel | None): 跟著滑鼠的提示標籤。
        target (tuple[str | None, str | None] | None): 目前的放置目標（群組, 插在哪個音效前）。
    """

    item: SoundItem
    x: int
    y: int
    active: bool = False
    keys: list[str] = field(default_factory=list)
    ghost: tk.Toplevel | None = None
    target: tuple[str | None, str | None] | None = None


@dataclass
class GroupDragState:
    """首頁中正在拖曳的群組標題（用來調整群組順序）。

    Attributes:
        name (str): 被拖曳的群組。
        x (int): 按下時的螢幕 X 座標。
        y (int): 按下時的螢幕 Y 座標。
        active (bool): 是否已移動到足以算作拖曳。
        ghost (tk.Toplevel | None): 跟著滑鼠的提示標籤。
        before (str | None): 放開時要插在哪個群組前面；None 表示移到最後。
        valid (bool): 目前位置是否為有效的放置目標（放開後順序會改變）。
    """

    name: str
    x: int
    y: int
    active: bool = False
    ghost: tk.Toplevel | None = None
    before: str | None = None
    valid: bool = False


class HomePage(tk.Frame):
    """首頁：音效按鈕依群組分區顯示。"""

    def __init__(self, parent: tk.Misc, app: SoundboardApp) -> None:
        """
        Args:
            parent (tk.Misc): 父元件（內容區）。
            app (SoundboardApp): 主程式。
        """
        super().__init__(parent, bg=SURFACE)
        self.app = app
        m = app.metrics
        self.tiles: dict[str, RoundedButton] = {}
        # 按鈕路徑 → 音效 key、群組標題（"" = 未分組），拖曳時用來判斷滑鼠下的放置目標
        self._tile_keys: dict[str, str] = {}
        self._section_headers: dict[str, RoundedButton] = {}
        self._section_widgets: list[tk.Widget] = []
        # Ctrl＋左鍵複選的音效 key，用來一次移動多個音效到群組
        self._selected: set[str] = set()
        self._drag: DragState | None = None
        self._group_drag: GroupDragState | None = None
        # 拖曳群組時顯示插入位置的細線（放在按鈕區上，不參與 grid 排版）
        self._insert_line: tk.Frame | None = None
        self._columns = 0
        self._configured_columns = 0
        self._tile_w = m.px(TILE_WIDTH)
        self._gap = m.px(TILE_GAP)
        self._pad = m.px(PAGE_PADDING)
        self._toolbar_narrow: bool | None = None
        self._build_header()
        self._build_banner()
        self._build_toolbar()
        self._build_grid()

    # ================================================================== 版面
    def _button(self, parent: tk.Misc, text: str, command: Callable[[], object], bg: str = BTN,
                hover: str = BTN_HOVER) -> RoundedButton:
        m = self.app.metrics
        return RoundedButton(parent, text, command, surface=SURFACE, bg=bg, hover=hover,
                             radius=m.px(8), padx=m.px(12), pady=m.px(7))

    def _build_header(self) -> None:
        m = self.app.metrics
        head = tk.Frame(self, bg=SURFACE)
        head.pack(fill="x", padx=self._pad, pady=(m.px(20), m.px(10)))
        # 先放右側按鈕：pack 依順序分配空間，資料夾路徑很長時被截斷的是路徑而不是按鈕
        self._button(head, tr("🔄 重新整理"), self.app.rescan).pack(side="right")
        self._button(head, tr("📁 選擇資料夾"), self.app.choose_folder).pack(side="right", padx=(0, m.px(8)))
        titles = tk.Frame(head, bg=SURFACE)
        titles.pack(side="left", fill="x", expand=True)
        tk.Label(titles, text=tr("音效"), font=FONT_H1, bg=SURFACE, fg=FG, anchor="w").pack(anchor="w")
        self.folder_label = tk.Label(titles, font=FONT_SMALL, bg=SURFACE, fg=FG_SUBTLE, anchor="w")
        self.folder_label.pack(anchor="w", fill="x")

    def _build_banner(self) -> None:
        m = self.app.metrics
        self.banner = RoundedFrame(self, surface=SURFACE, bg=BANNER_BG, radius=m.px(8), padding=m.px(10))
        inner = self.banner.inner
        go = RoundedButton(inner, tr("前往設定 →"), lambda: self.app.show_page("settings"), surface=BANNER_BG,
                           bg=BANNER_FG, hover="#000000", fg=FG, font=FONT_SMALL_BOLD, radius=m.px(6),
                           padx=m.px(10), pady=m.px(4))
        go.pack(side="right")
        self.banner_label = tk.Label(inner, bg=BANNER_BG, fg=BANNER_FG, font=FONT_BOLD, anchor="w",
                                     justify="left", cursor="hand2")
        self.banner_label.pack(side="left", fill="x", expand=True)
        self.banner_label.bind("<Button-1>", lambda _e: self.app.show_page("settings"))
        self.banner.bind("<Configure>", lambda e: self.banner_label.configure(
            wraplength=max(m.px(200), e.width - go.winfo_reqwidth() - m.px(40))), add="+")

    def _build_toolbar(self) -> None:
        m = self.app.metrics
        self.toolbar = tk.Frame(self, bg=SURFACE)
        self.toolbar.pack(fill="x", padx=self._pad, pady=(m.px(4), m.px(10)))
        self.toolbar.columnconfigure(0, weight=1)

        self.search_box = RoundedFrame(self.toolbar, surface=SURFACE, bg=INPUT, radius=m.px(8),
                                       padding=m.px(7), width=m.px(MIN_SEARCH_WIDTH))
        inner = self.search_box.inner
        tk.Label(inner, text="🔍", bg=INPUT, fg=FG_MUTED, font=FONT_BODY).pack(side="left", padx=(m.px(4), 0))
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *_a: self._on_search_changed())
        self.search_entry = tk.Entry(inner, textvariable=self.search_var, bg=INPUT, fg=FG, insertbackground=FG,
                                     relief="flat", bd=0, highlightthickness=0, font=FONT_BODY)
        self.search_entry.pack(side="left", fill="x", expand=True, padx=(m.px(8), m.px(4)))
        # 提示文字疊在輸入框上（不放進輸入框本身，否則會被當成搜尋字串）
        self._placeholder = tk.Label(inner, text=tr("搜尋音效名稱…"), bg=INPUT, fg=FG_SUBTLE, font=FONT_BODY,
                                     cursor="xterm")
        self._placeholder.place(in_=self.search_entry, x=0, rely=0.5, anchor="w")
        self._placeholder.bind("<Button-1>", lambda _e: self.search_entry.focus_set())
        self.count_label = tk.Label(self.toolbar, bg=SURFACE, fg=FG_SUBTLE, font=FONT_SMALL)

        self.actions = tk.Frame(self.toolbar, bg=SURFACE)
        gap = m.px(6)
        self._button(self.actions, tr("🗂 新增群組"), self.app.editor.new_group).pack(side="left")
        self._button(self.actions, tr("⌨ 快速鍵"), self.app.hotkeys.open_dialog).pack(side="left", padx=(gap, 0))
        self.overlay_btn = self._button(self.actions, tr("🪟 浮動視窗：關"), self.app.toggle_overlay)
        self.overlay_btn.pack(side="left", padx=(m.px(18), 0))
        self.preview_btn = self._button(self.actions, tr("🎧 試聽：關"), self.app.toggle_preview)
        self.preview_btn.pack(side="left", padx=(gap, 0))
        self._button(self.actions, tr("⏹ 全部停止"), self.app.stop_all, bg=ERR, hover=ERR_HOVER).pack(
            side="left", padx=(gap, 0))
        self.toolbar.bind("<Configure>", lambda e: self._reflow_toolbar(e.width), add="+")
        self._reflow_toolbar(0)

    def _reflow_toolbar(self, width: int) -> None:
        """寬度不夠時把工具列按鈕移到搜尋框下一行，避免按鈕被切掉。"""
        m = self.app.metrics
        needed = self.actions.winfo_reqwidth() + m.px(MIN_SEARCH_WIDTH) + m.px(90)
        narrow = 1 < width < needed
        if narrow == self._toolbar_narrow:
            return
        self._toolbar_narrow = narrow
        self.search_box.grid(row=0, column=0, sticky="we")
        self.count_label.grid(row=0, column=1, padx=(m.px(10), m.px(10)))
        if narrow:
            self.actions.grid(row=1, column=0, columnspan=3, sticky="w", pady=(m.px(8), 0))
        else:
            self.actions.grid(row=0, column=2, sticky="e", pady=0)

    def _build_grid(self) -> None:
        m = self.app.metrics
        # 預覽模式時整個按鈕區加上綠框，一眼就知道現在按下去朋友聽不到
        border = m.px(3)
        self.scroll = ScrollArea(self, bg=SURFACE, border=border, on_resize=lambda _w: self.layout())
        self.scroll.pack(fill="both", expand=True, padx=(self._pad - border - self._gap, m.px(6)),
                         pady=(0, m.px(6)))
        self.grid_frame = self.scroll.inner

    # ================================================================== 外部更新
    def set_folder(self, path: str) -> None:
        """顯示目前的音效資料夾。

        Args:
            path (str): 資料夾路徑。
        """
        self.folder_label.configure(text=path)

    def set_warnings(self, warnings: list[AudioWarning]) -> None:
        """有音訊設定問題時在首頁顯示醒目的橫幅（點一下前往設定頁）。

        Args:
            warnings (list[AudioWarning]): 警告；空清單時隱藏橫幅。
        """
        if not warnings:
            self.banner.pack_forget()
            return
        self.banner_label.configure(text="⚠  " + "；".join(w.short for w in warnings))
        if not self.banner.winfo_ismapped():
            m = self.app.metrics
            self.banner.pack(fill="x", padx=self._pad, pady=(0, m.px(8)), before=self.toolbar)

    def set_preview(self, preview: bool) -> None:
        """更新預覽模式的按鈕與綠框。

        Args:
            preview (bool): 預覽模式是否開啟。
        """
        if preview:
            self.preview_btn.set_text(tr("🎧 試聽：開（僅本機）"))
            self.preview_btn.set_colors(PREVIEW, PREVIEW_HOVER)
            self.scroll.set_border_color(PREVIEW)
        else:
            self.preview_btn.set_text(tr("🎧 試聽：關"))
            self.preview_btn.set_colors(BTN, BTN_HOVER)
            self.scroll.set_border_color(SURFACE)
        self._toolbar_narrow = None
        self._reflow_toolbar(self.toolbar.winfo_width())

    def update_overlay_button(self, visible: bool) -> None:
        """更新「🪟 小視窗」按鈕。

        Args:
            visible (bool): 小視窗是否顯示中。
        """
        if visible:
            self.overlay_btn.set_text(tr("🪟 浮動視窗：開"))
            self.overlay_btn.set_colors(ACCENT, ACCENT_HOVER)
        else:
            self.overlay_btn.set_text(tr("🪟 浮動視窗：關"))
            self.overlay_btn.set_colors(BTN, BTN_HOVER)

    def set_playing(self, playing: dict[str, str], previous: dict[str, str]) -> None:
        """更新播放中按鈕的顏色。

        Args:
            playing (dict[str, str]): 播放中的音效 key → 底色（藍＝朋友聽得到，綠＝預覽）。
            previous (dict[str, str]): 上一次的狀態，只重畫有變動的按鈕。
        """
        for key, tile in self.tiles.items():
            if playing.get(key) != previous.get(key):
                self._color_tile(tile, playing.get(key))

    @staticmethod
    def _color_tile(tile: RoundedButton, color: str | None) -> None:
        # 播放中不做滑鼠移入變色，否則游標停在按鈕上時看不出它正在播放
        if color is None:
            tile.set_colors(BTN, BTN_HOVER)
        else:
            tile.set_colors(color)

    # ================================================================== 音效按鈕
    def rebuild_tiles(self) -> None:
        """依目前的音效清單重建所有按鈕。"""
        m = self.app.metrics
        for tile in self.tiles.values():
            tile.destroy()
        self.tiles.clear()
        self._tile_keys.clear()
        tile_h = 3 * line_height(FONT_BODY) + 2 * m.px(9)
        for item in self.app.items:
            tile = RoundedButton(
                self.grid_frame, command=lambda it=item: self._on_tile_click(it), surface=SURFACE, bg=BTN,
                hover=BTN_HOVER, layout="tile", subfg=FG_MUTED, radius=m.px(10), padx=m.px(10), pady=m.px(9),
                width=self._tile_w - 2 * self._gap, height=tile_h, max_lines=3,
            )
            tile.bind("<Button-3>", lambda e, it=item: show_sound_menu(self.app, e, it))
            # Shift／Ctrl＋左鍵比一般左鍵更明確，Tk 只會執行它們；回傳 break 讓按鈕不會被「按下」而觸發播放
            tile.bind("<Shift-Button-1>", lambda _e, it=item: (self.app.play(it, to_mic=False), "break")[1])
            tile.bind("<Control-Button-1>", lambda _e, it=item: (self.toggle_select(it), "break")[1])
            tile.bind("<ButtonPress-1>", lambda e, it=item: self._drag_press(e, it), add="+")
            tile.bind("<B1-Motion>", self._drag_motion, add="+")
            tile.bind("<ButtonRelease-1>", self._drag_release, add="+")
            self._tile_keys[str(tile)] = item.key
            self.tiles[item.key] = tile
            self._apply_tile_text(item)
            self._color_tile(tile, self.app.playing.get(item.key))
        self.layout(force=True)

    def _apply_tile_text(self, item: SoundItem) -> None:
        settings = self.app.settings
        selected = item.key in self._selected
        text = tile_text(item.display_name, selected=selected, join=item.key == settings.join_sound_key,
                         pinned=item.key in settings.overlay_keys, hotkey=self.app.hotkeys.text_for_sound(item.key))
        tile = self.tiles[item.key]
        tile.set_text(text.title, text.subtitle)
        tile.set_outline(ACCENT if selected else None)

    def refresh_tile(self, item: SoundItem) -> None:
        """更新單一按鈕的文字。

        Args:
            item (SoundItem): 音效。
        """
        if item.key in self.tiles:
            self._apply_tile_text(item)

    def refresh_tile_texts(self) -> None:
        """更新所有按鈕的文字（快捷鍵、釘選、進場音效、複選變動後）。"""
        for item in self.app.items:
            self.refresh_tile(item)

    def _on_search_changed(self) -> None:
        if self.search_var.get():
            self._placeholder.place_forget()
        else:
            self._placeholder.place(in_=self.search_entry, x=0, rely=0.5, anchor="w")
        self.layout(force=True)

    def layout(self, force: bool = False, keep_scroll: bool = False) -> None:
        """依視窗寬度、群組與搜尋重新排列按鈕。

        Args:
            force (bool): 欄數沒變也要重排（內容有變動時）。
            keep_scroll (bool): 保留目前的捲動位置。
        """
        columns = grid_columns(self.scroll.canvas.winfo_width(), self._tile_w)
        if columns == self._columns and not force:
            return
        self._columns = columns
        for tile in self.tiles.values():
            tile.grid_forget()
        for widget in self._section_widgets:
            widget.destroy()
        self._section_widgets.clear()
        self._section_headers.clear()

        query = self.search_var.get()
        settings = self.app.settings
        sections = build_sections(self.app.items, settings, query)
        # 只有「未分組」一個區塊時（還沒建立任何群組）不顯示標題
        show_headers = bool(settings.sound_groups)
        row = 0
        shown = 0
        for section in sections:
            if show_headers:
                self._section_widgets.append(self._section_header(section, row, columns, bool(query.strip())))
                row += 1
            if section.collapsed:
                continue
            for i, item in enumerate(section.items):
                self.tiles[item.key].grid(row=row + i // columns, column=i % columns,
                                          padx=self._gap, pady=self._gap, sticky="nsew")
            row += (len(section.items) + columns - 1) // columns
            shown += len(section.items)
        if not self.app.items or (query.strip() and not shown):
            self._section_widgets.append(self._empty_hint(row, columns, query.strip()))
        for c in range(columns):
            self.grid_frame.columnconfigure(c, minsize=self._tile_w - 2 * self._gap, weight=1)
        # 視窗縮小後多出來的欄位也要清掉，否則它們的最小寬度會把按鈕擠扁
        for c in range(columns, self._configured_columns):
            self.grid_frame.columnconfigure(c, minsize=0, weight=0)
        self._configured_columns = max(columns, self._configured_columns)
        self.count_label.configure(text=tr("{shown} / {total} 個音效", shown=shown, total=len(self.app.items)))
        if not keep_scroll:
            self.scroll.canvas.yview_moveto(0)

    def _empty_hint(self, row: int, columns: int, query: str) -> tk.Label:
        if query:
            text = tr("找不到符合「{query}」的音效", query=query)
        else:
            text = tr("此資料夾中沒有音效\n"
                      "請將音效檔放入資料夾後按一下「🔄 重新整理」，或按一下「📁 選擇資料夾」選擇其他資料夾")
        hint = tk.Label(self.grid_frame, text=text, bg=SURFACE, fg=FG_SUBTLE, font=FONT_BODY, justify="center")
        hint.grid(row=row, column=0, columnspan=columns, pady=self.app.metrics.px(60))
        return hint

    # ================================================================== 分組
    def _section_header(self, section: Section, row: int, columns: int, searching: bool) -> RoundedButton:
        """建立群組標題列：左鍵收合／展開，右鍵開啟群組選單。"""
        m = self.app.metrics
        left, right = section_header_text(section, searching)
        key = section.name if section.name is not None else ""
        header = RoundedButton(
            self.grid_frame, left, lambda: self.app.editor.toggle_collapse(key), surface=SURFACE, bg=SURFACE,
            hover=HEADER_HOVER, font=FONT_BOLD, subtext=right, subfg=FG_SUBTLE, subfont=FONT_SMALL_BOLD,
            layout="row", radius=m.px(6), padx=m.px(10), pady=m.px(6),
        )
        header.grid(row=row, column=0, columnspan=columns, sticky="we", padx=self._gap,
                    pady=(m.px(14) if row else m.px(2), m.px(2)))
        header.bind("<Button-3>", lambda e: show_group_menu(self.app, e, section.name))
        if section.name is not None and not searching:
            # 搜尋時部分群組被隱藏，插入位置會對不上，所以只在沒有搜尋時允許拖曳排序
            name = section.name
            header.bind("<ButtonPress-1>", lambda e: self._group_press(e, name), add="+")
            header.bind("<B1-Motion>", self._group_motion, add="+")
            header.bind("<ButtonRelease-1>", self._group_release, add="+")
            header.configure(cursor="fleur")
        self._section_headers[key] = header
        return header

    def _highlight_header(self, highlight: str | None) -> None:
        for key, header in self._section_headers.items():
            if key == highlight:
                header.set_colors(ACCENT)
            else:
                header.set_colors(SURFACE, HEADER_HOVER)

    def toggle_select(self, item: SoundItem) -> None:
        """Ctrl＋左鍵：把音效加入／移出複選，之後可以一次移到群組。

        Args:
            item (SoundItem): 音效。
        """
        if item.key in self._selected:
            self._selected.discard(item.key)
        else:
            self._selected.add(item.key)
        self.refresh_tile(item)
        if self._selected:
            self.app.set_status(tr("已選取 {count} 個音效。在任一選取的音效上按右鍵並選擇「🗂 移到群組」，"
                                   "或直接拖曳，即可一次移動；按 Esc 取消選取", count=len(self._selected)), FG)
        else:
            self.app.set_status(tr("已取消選取"))

    def selected_keys(self) -> list[str]:
        """目前複選的音效（依顯示順序）。"""
        return [it.key for it in self.app.items if it.key in self._selected]

    def clear_selection(self) -> None:
        """取消所有複選。"""
        if self._selected:
            self._selected.clear()
            self.refresh_tile_texts()

    def forget_missing_selection(self) -> None:
        """重新掃描後移除已不存在的音效。"""
        self._selected &= set(self.app.items_by_key)

    @property
    def selected(self) -> set[str]:
        """目前複選的音效 key（唯讀副本）。"""
        return set(self._selected)

    def menu_targets(self, item: SoundItem) -> list[str]:
        """右鍵選單與拖曳要套用的音效：在已選取的音效上操作時套用到全部選取的音效。

        Args:
            item (SoundItem): 被按的音效。

        Returns:
            list[str]: 音效 key（依顯示順序）。
        """
        if item.key in self._selected:
            return [it.key for it in self.app.items if it.key in self._selected]
        return [item.key]

    # ================================================================== 拖曳
    def _on_tile_click(self, item: SoundItem) -> None:
        self.app.play(item, to_mic=not self.app.preview_mode)

    def _drag_press(self, event: tk.Event, item: SoundItem) -> None:
        # 沒有群組時沒有地方可以拖，維持單純的點擊播放
        self._drag = DragState(item, event.x_root, event.y_root) if self.app.settings.sound_groups else None

    def _drag_motion(self, event: tk.Event) -> None:
        drag = self._drag
        if drag is None:
            return
        if not drag.active:
            # 移動超過幾個像素才算拖曳，避免點擊時手抖被當成拖曳
            if abs(event.x_root - drag.x) + abs(event.y_root - drag.y) < DRAG_THRESHOLD_PX:
                return
            drag.active = True
            drag.keys = self.menu_targets(drag.item)
            drag.ghost = self._make_drag_ghost(drag)
            # 已經算拖曳了：放開時不要再當成點擊播放
            self.tiles[drag.item.key].cancel_press()
        drag.ghost.geometry(f"+{event.x_root + 14}+{event.y_root + 10}")
        # 立刻套用新位置：不然標籤還停在上一個位置，滑鼠移動快時會被它蓋住而判斷不到放置目標
        drag.ghost.update_idletasks()
        self._autoscroll(event.y_root)
        drag.target = self.drop_target(event.x_root, event.y_root, drag.keys)
        self._highlight_header(None if drag.target is None else (drag.target[0] or ""))

    def _drag_release(self, _event: tk.Event) -> None:
        drag, self._drag = self._drag, None
        if drag is None or not drag.active:
            return
        drag.ghost.destroy()
        self._highlight_header(None)
        if drag.target is None:
            self.app.set_status(tr("請將音效拖曳到群組標題或群組內的音效上再放開，即可移入該群組"))
            return
        group, before = drag.target
        self.app.editor.move_to_group(drag.keys, group, before)

    def _make_drag_ghost(self, drag: DragState) -> tk.Toplevel:
        """跟著滑鼠移動的半透明標籤，顯示正在拖的音效。"""
        if len(drag.keys) == 1:
            return self._make_ghost(f"🎵 {drag.item.display_name}")   # 音效名稱是使用者資料，不翻譯
        return self._make_ghost(tr("🎵 {count} 個音效", count=len(drag.keys)))

    def _make_ghost(self, text: str) -> tk.Toplevel:
        """建立跟著滑鼠移動的半透明提示標籤。"""
        ghost = tk.Toplevel(self.app.root)
        ghost.overrideredirect(True)
        ghost.attributes("-topmost", True)
        ghost.attributes("-alpha", 0.85)
        tk.Label(ghost, text=text, bg=ACCENT, fg=FG, padx=10, pady=4, font=FONT_BOLD).pack()
        return ghost

    def drop_target(self, x: int, y: int, keys: list[str]) -> tuple[str | None, str | None] | None:
        """滑鼠下方的放置目標。

        Args:
            x (int): 螢幕 X 座標。
            y (int): 螢幕 Y 座標。
            keys (list[str]): 正在拖曳的音效。

        Returns:
            tuple[str | None, str | None] | None: (群組, 插在哪個音效前面)；不是有效目標時為 None。
        """
        root = self.app.root
        try:
            widget = root.winfo_containing(x, y)
        except KeyError:
            return None
        # 只接受主視窗內的元件（排除拖曳中的提示標籤、小視窗等其他頂層視窗）
        if widget is None or widget.winfo_toplevel() is not root:
            return None
        for key, header in self._section_headers.items():
            if widget is header:
                return (key or None, None)
        if widget is self.grid_frame:
            # 放在按鈕之間的空白處：算進滑鼠上方最近的那個群組
            above = [(h.winfo_rooty(), key) for key, h in self._section_headers.items() if h.winfo_rooty() <= y]
            return (max(above)[1] or None, None) if above else None
        target_key = self._tile_keys.get(str(widget))
        if target_key is None or target_key in keys:
            return None
        group = self.app.settings.group_of(target_key)
        # 未分組依名稱排序，沒有「插在誰前面」的概念
        return (group, target_key if group is not None else None)

    # ================================================================== 拖曳群組（調整順序）
    def _group_press(self, event: tk.Event, name: str) -> None:
        self._group_drag = GroupDragState(name, event.x_root, event.y_root)

    def _group_motion(self, event: tk.Event) -> None:
        drag = self._group_drag
        if drag is None:
            return
        if not drag.active:
            if abs(event.x_root - drag.x) + abs(event.y_root - drag.y) < DRAG_THRESHOLD_PX:
                return
            drag.active = True
            drag.ghost = self._make_ghost(f"🗂 {drag.name}")
            header = self._section_headers.get(drag.name)
            if header is not None:
                # 已經算拖曳了：放開時不要再當成點擊（收合／展開）
                header.cancel_press()
        drag.ghost.geometry(f"+{event.x_root + 14}+{event.y_root + 10}")
        drag.ghost.update_idletasks()
        self._autoscroll(event.y_root)
        drag.before, line_y = self.group_insert_position(drag.name, event.y_root)
        names = list(self.app.settings.sound_groups)
        drag.valid = names.index(drag.name) + 1 != (names.index(drag.before) if drag.before else len(names))
        self._show_insert_line(line_y if drag.valid else None)

    def _group_release(self, _event: tk.Event) -> None:
        drag, self._group_drag = self._group_drag, None
        if drag is None or not drag.active:
            return
        if drag.ghost is not None:
            drag.ghost.destroy()
        self._show_insert_line(None)
        if drag.valid:
            self.app.editor.move_group_before(drag.name, drag.before)

    def group_insert_position(self, name: str, y_root: int) -> tuple[str | None, int]:
        """拖曳群組時，依滑鼠高度算出插入位置。

        滑鼠越過某個群組標題的中線，就算插在那個群組之後；所以拖到某群組的音效上方時，
        會放在該群組後面，跟畫面上看到的位置一致。

        Args:
            name (str): 正在拖曳的群組。
            y_root (int): 滑鼠的螢幕 Y 座標。

        Returns:
            tuple[str | None, int]: (插在哪個群組前面，None = 最後, 插入線在按鈕區內的 Y 座標)。
        """
        headers = [(g, h) for g, h in self._section_headers.items() if g and g != name]
        origin = self.grid_frame.winfo_rooty()
        for group, header in headers:
            top = header.winfo_rooty()
            if y_root < top + header.winfo_height() / 2:
                return group, top - origin - self._gap
        # 放在所有群組之後：插入線畫在「未分組」標題上方，沒有的話畫在最下面
        ungrouped = self._section_headers.get("")
        if ungrouped is not None:
            return None, ungrouped.winfo_rooty() - origin - self._gap
        return None, self.grid_frame.winfo_height() - self._gap

    def _show_insert_line(self, y: int | None) -> None:
        if y is None:
            if self._insert_line is not None:
                self._insert_line.place_forget()
            return
        if self._insert_line is None or not self._insert_line.winfo_exists():
            self._insert_line = tk.Frame(self.grid_frame, bg=ACCENT, height=self.app.metrics.px(3))
        self._insert_line.place(x=self._gap, y=max(0, y), relwidth=1.0, width=-2 * self._gap,
                                height=self.app.metrics.px(3))
        self._insert_line.lift()

    def _autoscroll(self, y_root: int) -> None:
        canvas = self.scroll.canvas
        top = canvas.winfo_rooty()
        bottom = top + canvas.winfo_height()
        if y_root < top + AUTOSCROLL_MARGIN_PX:
            canvas.yview_scroll(-1, "units")
        elif y_root > bottom - AUTOSCROLL_MARGIN_PX:
            canvas.yview_scroll(1, "units")

    @property
    def section_headers(self) -> dict[str, RoundedButton]:
        """目前畫面上的群組標題（"" = 未分組）。"""
        return dict(self._section_headers)
