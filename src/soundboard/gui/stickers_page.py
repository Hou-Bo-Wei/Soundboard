"""貼圖頁：貼圖縮圖牆（依系列分區），按一下複製到剪貼簿；右鍵可用 Webhook 傳送、編輯關鍵字、顯示檔案或刪除。

搜尋框的文字有三個用途：
    1. 關鍵字建議：符合貼圖關鍵字的貼圖排在最前面（仿 LINE 的貼圖建議）
    2. 文字貼圖：LINE 訊息貼圖與名字貼圖單獨成區，搜尋時直接把搜尋的文字套用上去，按一下就複製套好字的貼圖
    3. 一般的名稱／系列搜尋
"""

from __future__ import annotations

import bisect
import logging
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk
from typing import TYPE_CHECKING

from soundboard import sticker_text, stickers
from soundboard.config import default_sound_dir
from soundboard.gui.menus import _menu, _popup
from soundboard.gui.presenters import grid_columns
from soundboard.gui.sticker_actions import StickerActions
from soundboard.gui.styles import FONT_BODY, FONT_BOLD, FONT_H1, FONT_SMALL
from soundboard.gui.webhook_dialog import WebhookDialog
from soundboard.gui.widgets import RoundedButton, RoundedFrame, ScrollArea, rounded_rect_points
from soundboard.i18n import tr
from soundboard.library import move_to_recycle_bin
from soundboard.sticker_meta import KeywordStore, keyword_matches, parse_keywords
from soundboard.theme import (
    ACCENT, BTN, BTN_HOVER, CARD, ERR, FG, FG_MUTED, FG_SUBTLE, INPUT, OK, SURFACE, WARN,
)

if TYPE_CHECKING:
    from soundboard.gui.app import SoundboardApp

logger = logging.getLogger(__name__)

# 以下為 100% 縮放時的像素值
THUMB_BOX = 96              # 縮圖長邊
TILE_WIDTH = 132            # 每張貼圖佔用的寬度（含左右間距）
TILE_GAP = 5
CAPTION_HEIGHT = 20
PAGE_PADDING = 24
DRAIN_MS = 60               # 多久檢查一次背景執行緒做好的縮圖
ANIM_FALLBACK_MS = 100
FLASH_MS = 350
MIN_SEARCH_WIDTH = 260
HEADER_HEIGHT = 30
THUMB_CACHE_SIZE = 1500     # 最多快取幾張縮圖（每張約 40 KB）
DRAIN_BATCH = 16            # 每次最多處理幾張縮圖，避免一口氣塞滿而讓介面卡住
IMPORT_CHUNK = 25           # 匯入時每複製幾個檔案更新一次進度
BUFFER_SCREENS = 0.75       # 畫面上下各多準備幾個螢幕高度的貼圖，捲動時才不會看到空白
SEARCH_DEBOUNCE_MS = 250    # 打字停下來多久才重新排版（文字貼圖要連網畫字，不能每打一個字就送出）
TEXT_RENDER_WORKERS = 3


def send_size_label(edge: int) -> str:
    """「傳送大小」選項的顯示文字。

    Args:
        edge (int): 長邊像素；0 表示原始大小。

    Returns:
        str: 顯示文字。
    """
    return tr("原始大小") if edge <= 0 else tr("長邊 {edge} px", edge=edge)


def ellipsize(text: str, measure, width: int) -> str:
    """把文字截短到不超過指定像素寬度（太長時結尾加 ``…``）。

    Args:
        text (str): 原文。
        measure (Callable[[str], int]): 量字串像素寬度的函式。
        width (int): 寬度上限。

    Returns:
        str: 截短後的文字。
    """
    if measure(text) <= width:
        return text
    while text and measure(text + "…") > width:
        text = text[:-1]
    return text + "…"


class StickerTile(tk.Canvas):
    """一張貼圖的縮圖按鈕：圓角底、縮圖、名稱；動態貼圖右上角有 ▶ 標記，滑鼠移上去會播放。"""

    def __init__(self, parent: tk.Misc, item: stickers.StickerItem, *, width: int, height: int, box: int,
                 radius: int, caption_h: int, font_measure, text: str = "") -> None:
        """
        Args:
            parent (tk.Misc): 父元件。
            item (stickers.StickerItem): 貼圖。
            text (str): 文字貼圖目前套用的文字（空字串表示原圖）。
            width (int): 寬（像素）。
            height (int): 高（像素）。
            box (int): 縮圖區的長邊。
            radius (int): 圓角半徑。
            caption_h (int): 名稱列高度。
            font_measure (Callable[[str], int]): 量名稱寬度的函式。
        """
        super().__init__(parent, bg=SURFACE, highlightthickness=0, bd=0, width=width, height=height,
                         cursor="hand2", takefocus=0)
        self.item = item
        self.text = text
        self.window_id = 0      # 這張貼圖在 canvas 上的視窗項目編號（由頁面設定）
        self._center = (width / 2, (height - caption_h) / 2)
        self.info: stickers.StickerInfo | None = None
        self.photo: tk.PhotoImage | None = None
        self.hovering = False
        self.hover_token = 0
        self._frames: list[tuple[tk.PhotoImage, int]] = []
        self._shape = self.create_polygon(rounded_rect_points(0, 0, width, height, radius), smooth=True, fill=BTN,
                                          outline=BTN)
        cx, cy = self._center
        self._image = self.create_text(cx, cy, text="…", fill=FG_SUBTLE, font=FONT_BODY)
        self._badge = self.create_text(width - 8, 8, text="▶", fill=FG_MUTED, font=FONT_SMALL, anchor="ne", state="hidden")
        label = f"✏ {item.name}" if item.text_spec is not None else item.name
        caption = ellipsize(label, font_measure, width - 12)
        self.create_text(cx, height - caption_h / 2 - 2, text=caption, fill=FG_MUTED, font=FONT_SMALL)
        self._fill = BTN
        self.bind("<Enter>", lambda _e: self._set_hover(True))
        self.bind("<Leave>", lambda _e: self._set_hover(False))

    # ------------------------------------------------------------------ 外觀
    def _paint(self, color: str) -> None:
        self.itemconfigure(self._shape, fill=color, outline=color)

    def _set_hover(self, hover: bool) -> None:
        self.hovering = hover
        self._paint(BTN_HOVER if hover else BTN)

    def flash(self, color: str = ACCENT) -> None:
        """短暫換色，表示這張貼圖剛被按下。"""
        self._paint(color)
        self.after(FLASH_MS, lambda: self.winfo_exists() and self._set_hover(self.hovering))

    def set_image(self, photo: tk.PhotoImage | None, info: stickers.StickerInfo | None, failed: bool = False) -> None:
        """設定縮圖與貼圖資訊。

        Args:
            photo (tk.PhotoImage | None): 縮圖；None 表示沒有（載入失敗）。
            info (stickers.StickerInfo | None): 貼圖資訊。
            failed (bool): 檔案讀不出來。
        """
        self.info = info
        self.delete(self._image)
        if photo is not None:
            self.photo = photo
            self._image = self.create_image(*self._center, image=photo)
        else:
            self._image = self.create_text(*self._center, text="⚠" if failed else "…", fill=WARN, font=FONT_H1)
        self.itemconfigure(self._badge, state="normal" if info is not None and info.animated else "hidden")

    # ------------------------------------------------------------------ 動畫
    def start_animation(self, frames: list[tuple[tk.PhotoImage, int]]) -> None:
        """開始播放動畫（滑鼠移開時由頁面停止）。"""
        self._frames = frames
        self._step(0, self.hover_token)

    def _step(self, index: int, token: int) -> None:
        if not self.winfo_exists() or token != self.hover_token or not self._frames:
            return
        photo, duration = self._frames[index % len(self._frames)]
        self.itemconfigure(self._image, image=photo)
        self.after(duration, lambda: self._step(index + 1, token))

    def stop_animation(self) -> None:
        """停止動畫並回到第一格縮圖。"""
        self.hover_token += 1
        self._frames = []
        if self.photo is not None and self.winfo_exists():
            self.itemconfigure(self._image, image=self.photo)


class StickersPage(tk.Frame):
    """貼圖頁。"""

    def __init__(self, parent: tk.Misc, app: SoundboardApp) -> None:
        """
        Args:
            parent (tk.Misc): 父元件（內容區）。
            app (SoundboardApp): 主程式。
        """
        super().__init__(parent, bg=SURFACE)
        self.app = app
        m = app.metrics
        self.actions = StickerActions(app)
        self._tile_w = m.px(TILE_WIDTH)
        self._gap = m.px(TILE_GAP)
        self._pad = m.px(PAGE_PADDING)
        self._box = m.px(THUMB_BOX)
        self.items: list[stickers.StickerItem] = []
        self.tiles: dict[str, StickerTile] = {}
        # 縮圖快取：key → (PhotoImage 或 None, 貼圖資訊, 是否讀取失敗)；只快取看過的，上限 THUMB_CACHE_SIZE
        self._thumbs: OrderedDict[str, tuple[tk.PhotoImage | None, stickers.StickerInfo | None, bool]] = OrderedDict()
        self._pending: set[str] = set()
        # 虛擬捲動：只為畫面附近的列建立元件。列 = ("header", 文字) 或 ("tiles", [貼圖…])，_row_tops 是各列的 y
        self._rows: list[tuple[str, object]] = []
        self._row_tops: list[int] = []
        self._headers: dict[int, int] = {}      # 列編號 → canvas 文字項目
        self._hint: int | None = None
        self._columns = 0
        self._generation = 0
        self._importing = False
        self._measure = None
        self._tile_h = self._box + m.px(CAPTION_HEIGHT) + m.px(20)
        self._loaded = False
        self.keywords = KeywordStore(self.folder)
        self._query = ""            # 目前套用的搜尋文字（打字停下來後才更新）
        self._search_job: str | None = None
        # 縮圖由背景執行緒做（讀檔與縮放），PhotoImage 一律在主執行緒建立
        self._executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="thumb")
        # 文字貼圖要連網請 LINE 畫字（最多等好幾秒），另外用一組執行緒，不拖慢一般縮圖
        self._text_executor = ThreadPoolExecutor(max_workers=TEXT_RENDER_WORKERS, thread_name_prefix="sticker-text")
        self._ready: queue.Queue[tuple] = queue.Queue()
        self._pillow_ok = self._check_pillow()
        self._build_header()
        self._build_discord_card()
        self._build_toolbar()
        self._build_grid()
        self.refresh_discord()
        self._drain_job = self.after(DRAIN_MS, self._drain)

    @staticmethod
    def _check_pillow() -> bool:
        try:
            import PIL.ImageTk  # noqa: F401
        except ImportError:
            return False
        return True

    # ================================================================== 版面
    def _button(self, parent: tk.Misc, text: str, command, bg: str = BTN, hover: str = BTN_HOVER,
                surface: str = SURFACE) -> RoundedButton:
        m = self.app.metrics
        return RoundedButton(parent, text, command, surface=surface, bg=bg, hover=hover, radius=m.px(8),
                             padx=m.px(12), pady=m.px(7))

    def _build_header(self) -> None:
        m = self.app.metrics
        head = tk.Frame(self, bg=SURFACE)
        head.pack(fill="x", padx=self._pad, pady=(m.px(20), m.px(10)))
        self._button(head, tr("🔄 重新整理"), self.rescan).pack(side="right")
        self._button(head, tr("📁 選擇資料夾"), self.choose_folder).pack(side="right", padx=(0, m.px(8)))
        self.import_btn = self._button(head, tr("➕ 匯入貼圖"), self._show_import_menu)
        self.import_btn.pack(side="right", padx=(0, m.px(8)))
        titles = tk.Frame(head, bg=SURFACE)
        titles.pack(side="left", fill="x", expand=True)
        tk.Label(titles, text=tr("貼圖"), font=FONT_H1, bg=SURFACE, fg=FG, anchor="w").pack(anchor="w")
        self.folder_label = tk.Label(titles, font=FONT_SMALL, bg=SURFACE, fg=FG_SUBTLE, anchor="w")
        self.folder_label.pack(anchor="w", fill="x")

    def _build_discord_card(self) -> None:
        m = self.app.metrics
        card = RoundedFrame(self, surface=SURFACE, bg=CARD, radius=m.px(10), padding=m.px(12))
        card.pack(fill="x", padx=self._pad, pady=(0, m.px(10)))
        # 第一列：使用說明、貼圖大小、Webhook 設定；第二列：用 Webhook 傳送文字
        inner = tk.Frame(card.inner, bg=CARD)
        inner.pack(fill="x")
        self.discord_label = tk.Label(inner, bg=CARD, fg=FG_MUTED, font=FONT_BODY, anchor="w", justify="left")
        self.discord_label.pack(side="left", fill="x", expand=True)
        self._button(inner, tr("⚙ Webhook 設定…"), self.open_webhook_dialog, surface=CARD).pack(side="right")
        self.size_var = tk.StringVar()
        self.size_combo = ttk.Combobox(inner, textvariable=self.size_var, state="readonly", width=12, font=FONT_BODY,
                                       values=[send_size_label(e) for e in stickers.SEND_SIZE_CHOICES])
        self.size_combo.bind("<<ComboboxSelected>>", lambda _e: self._on_size_changed())
        self.size_combo.bind("<MouseWheel>", lambda e: (self.scroll.scroll(int(-e.delta / 120)), "break")[1])
        self.size_combo.pack(side="right", padx=(0, m.px(8)))
        tk.Label(inner, text=tr("貼圖大小"), bg=CARD, fg=FG_MUTED, font=FONT_SMALL).pack(side="right", padx=(0, m.px(6)))

        row = tk.Frame(card.inner, bg=CARD)
        row.pack(fill="x", pady=(m.px(8), 0))
        tk.Label(row, text=tr("📨 Webhook 傳送到"), bg=CARD, fg=FG_MUTED, font=FONT_BODY).pack(side="left")
        self.target_combo = ttk.Combobox(row, state="readonly", width=16, font=FONT_BODY)
        self.target_combo.bind("<<ComboboxSelected>>", lambda _e: self._on_target_changed())
        self.target_combo.bind("<MouseWheel>", lambda e: (self.scroll.scroll(int(-e.delta / 120)), "break")[1])
        self.target_combo.pack(side="left", padx=(m.px(8), m.px(10)))
        self._button(row, tr("傳送文字"), self.send_message, surface=CARD, bg=ACCENT, hover=ACCENT).pack(side="right")
        box = RoundedFrame(row, surface=CARD, bg=INPUT, radius=m.px(8), padding=m.px(6))
        box.pack(side="left", fill="x", expand=True, padx=(0, m.px(8)))
        self.message_var = tk.StringVar()
        self.message_entry = tk.Entry(box.inner, textvariable=self.message_var, bg=INPUT, fg=FG, insertbackground=FG,
                                      relief="flat", bd=0, highlightthickness=0, font=FONT_BODY)
        self.message_entry.pack(fill="x", expand=True, padx=m.px(4))
        self.message_entry.bind("<Return>", lambda _e: self.send_message())
        self._message_hint = tk.Label(box.inner, text=tr("輸入要傳到 Discord 頻道的文字，按 Enter 送出…"), bg=INPUT,
                                      fg=FG_SUBTLE, font=FONT_BODY, cursor="xterm")
        self._message_hint.place(in_=self.message_entry, x=0, rely=0.5, anchor="w")
        self._message_hint.bind("<Button-1>", lambda _e: self.message_entry.focus_set())
        self.message_var.trace_add("write", lambda *_a: self._update_message_hint())

    def _update_message_hint(self) -> None:
        if self.message_var.get():
            self._message_hint.place_forget()
        else:
            self._message_hint.place(in_=self.message_entry, x=0, rely=0.5, anchor="w")

    def send_message(self) -> None:
        """用目前選擇的 Webhook 把文字框的內容傳到 Discord；尚未設定 Webhook 時開啟設定視窗。"""
        text = self.message_var.get().strip()
        if not text:
            self.message_entry.focus_set()
            return
        if not self.actions.send_text(text, lambda ok: self._ready.put(("text_sent", ok, "", text, None))):
            self.open_webhook_dialog()

    def _build_toolbar(self) -> None:
        m = self.app.metrics
        bar = tk.Frame(self, bg=SURFACE)
        bar.pack(fill="x", padx=self._pad, pady=(m.px(2), m.px(8)))
        bar.columnconfigure(0, weight=1)
        search = RoundedFrame(bar, surface=SURFACE, bg=INPUT, radius=m.px(8), padding=m.px(7),
                              width=m.px(MIN_SEARCH_WIDTH))
        search.grid(row=0, column=0, sticky="we")
        tk.Label(search.inner, text="🔍", bg=INPUT, fg=FG_MUTED, font=FONT_BODY).pack(side="left", padx=(m.px(4), 0))
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *_a: self._on_search_changed())
        self.search_entry = tk.Entry(search.inner, textvariable=self.search_var, bg=INPUT, fg=FG, insertbackground=FG,
                                     relief="flat", bd=0, highlightthickness=0, font=FONT_BODY)
        self.search_entry.pack(side="left", fill="x", expand=True, padx=(m.px(8), m.px(4)))
        self._placeholder = tk.Label(search.inner, text=tr("輸入文字：搜尋關鍵字，並套用到文字貼圖…"), bg=INPUT,
                                     fg=FG_SUBTLE,
                                     font=FONT_BODY, cursor="xterm")
        self._placeholder.place(in_=self.search_entry, x=0, rely=0.5, anchor="w")
        self._placeholder.bind("<Button-1>", lambda _e: self.search_entry.focus_set())
        self.count_label = tk.Label(bar, bg=SURFACE, fg=FG_SUBTLE, font=FONT_SMALL)
        self.count_label.grid(row=0, column=1, padx=(m.px(10), 0))

    def _build_grid(self) -> None:
        self.scroll = ScrollArea(self, bg=SURFACE, on_resize=lambda _w: self.layout(), on_scroll=self._update_visible)
        m = self.app.metrics
        self.scroll.pack(fill="both", expand=True, padx=(self._pad - self._gap, m.px(6)), pady=(0, m.px(6)))

    # ================================================================== Discord 設定
    def refresh_discord(self) -> None:
        """依目前設定更新「傳送目標」與「傳送大小」。"""
        settings = self.app.settings
        names = list(settings.discord_webhooks)
        self.target_combo.configure(values=names)
        active = settings.active_webhook()
        if active is None:
            self.target_combo.set("")
            self.target_combo.configure(state="disabled")
        else:
            self.target_combo.configure(state="readonly")
            self.target_combo.set(active[0])
        self.discord_label.configure(
            text=tr("按一下貼圖即可複製，到 Discord 按 Ctrl+V 傳送；右鍵可改用 Webhook 傳送"), fg=FG_MUTED)
        self.size_var.set(send_size_label(settings.sticker_send_size))

    def _on_target_changed(self) -> None:
        self.app.settings.discord_webhook_active = self.target_combo.get() or None
        self.app.save()

    def _on_size_changed(self) -> None:
        index = self.size_combo.current()
        if 0 <= index < len(stickers.SEND_SIZE_CHOICES):
            self.app.settings.sticker_send_size = stickers.SEND_SIZE_CHOICES[index]
            self.app.save()

    def open_webhook_dialog(self) -> None:
        """開啟 Webhook 設定視窗。"""
        WebhookDialog(self.app, self.refresh_discord)

    # ================================================================== 資料夾與匯入
    @property
    def folder(self) -> Path:
        """目前的貼圖資料夾。"""
        configured = self.app.settings.sticker_dir
        return Path(configured) if configured else stickers.default_sticker_dir(default_sound_dir())

    def choose_folder(self) -> None:
        """讓使用者選擇貼圖資料夾（例如 LINE 貼圖下載器存放貼圖的資料夾）。"""
        start = self.folder if self.folder.is_dir() else Path(os.getcwd())
        chosen = filedialog.askdirectory(initialdir=str(start), title=tr("選擇貼圖資料夾"))
        if chosen:
            self.app.settings.sticker_dir = chosen
            self.app.save()
            self.rescan()

    def _show_import_menu(self) -> None:
        menu = _menu(self.app.root)
        menu.add_command(label=tr("匯入圖片檔…"), command=self.import_files)
        menu.add_command(label=tr("匯入整個資料夾（保留系列資料夾）…"), command=self.import_folder)
        x = self.import_btn.winfo_rootx()
        y = self.import_btn.winfo_rooty() + self.import_btn.winfo_height()
        try:
            menu.tk_popup(x, y)
        finally:
            menu.grab_release()

    def import_files(self) -> None:
        """選擇圖片檔，複製進貼圖資料夾。"""
        exts = " ".join(f"*{e}" for e in sorted(stickers.STICKER_EXTENSIONS))
        chosen = filedialog.askopenfilenames(title=tr("選擇要匯入的貼圖"),
                                             filetypes=[(tr("圖片"), exts), (tr("所有檔案"), "*.*")])
        if chosen:
            dest = self.folder
            self._start_import(lambda: [(Path(p), dest) for p in chosen])

    def import_folder(self) -> None:
        """選擇資料夾（例如 LINE 貼圖下載器的一個系列），連同子資料夾結構複製進貼圖資料夾。

        幾千張貼圖的資料夾光是列出檔案就要幾秒，所以整個流程（列出、複製）都在背景做。
        """
        chosen = filedialog.askdirectory(title=tr("選擇要匯入的貼圖資料夾"))
        if not chosen:
            return
        source, dest = Path(chosen), self.folder / Path(chosen).name

        def collect() -> list[tuple[Path, Path]]:
            return [(p, dest / p.parent.relative_to(source)) for p in sorted(source.rglob("*"))
                    if p.is_file() and p.suffix.lower() in stickers.STICKER_EXTENSIONS]

        self._start_import(collect)

    def _start_import(self, collect: Callable[[], list[tuple[Path, Path]]]) -> None:
        if self._importing:
            self.app.set_status(tr("正在匯入中，請等它完成"), WARN)
            return
        self._importing = True
        self.app.set_status(tr("⏳ 正在整理要匯入的貼圖…"), FG_MUTED)
        threading.Thread(target=self._import_worker, args=(collect, self.folder), daemon=True,
                         name="sticker-import").start()

    def _import_worker(self, collect: Callable[[], list[tuple[Path, Path]]], target: Path) -> None:
        """在背景列出並複製檔案（成千上萬張時不能卡住介面），完成後交回主執行緒重新整理。"""
        post = self.app.post_message
        count = 0
        error: OSError | None = None
        try:
            # 來源已經在貼圖資料夾裡就不用複製（否則會產生一堆「(2)」重複檔）；用字串比對，不逐一解析路徑
            root = os.path.normcase(os.path.abspath(target)) + os.sep
            files = [(s, d) for s, d in collect() if not os.path.normcase(os.path.abspath(s)).startswith(root)]
            total = len(files)
            if not files:
                post("status", f"{WARN}|" + tr("沒有可匯入的貼圖（支援 PNG／GIF／WebP／APNG／JPG）"))
            for i in range(0, total, IMPORT_CHUNK):
                count += self.actions.import_files(files[i:i + IMPORT_CHUNK])
                post("status", f"{FG_MUTED}|" + tr("⏳ 匯入貼圖中… {done} / {total}", done=count, total=total))
        except OSError as exc:
            error = exc
        self._ready.put(("imported", count, "", error, None))

    # ================================================================== 掃描與縮圖
    def on_show(self) -> None:
        """切換到貼圖頁時呼叫：第一次才掃描資料夾，不拖慢程式啟動。"""
        if not self._loaded:
            self.rescan()

    def rescan(self) -> None:
        """重新掃描貼圖資料夾（在背景做，幾千張也不卡），完成後重新排版。"""
        self._loaded = True
        folder = self.folder
        self.folder_label.configure(text=str(folder))
        self._generation += 1
        generation = self._generation
        self._thumbs.clear()
        self._pending.clear()
        self.app.set_status(tr("⏳ 正在讀取貼圖資料夾…"), FG_MUTED)

        def scan() -> None:
            items = stickers.scan_stickers(folder)
            self._ready.put(("scanned", generation, "", items, KeywordStore.load(folder)))

        self._executor.submit(scan)

    def _finish_scan(self, items: list[stickers.StickerItem]) -> None:
        self.items = items
        for key in list(self.tiles):
            self._drop_tile(key)
        self.layout(force=True)
        if self.items and not self._pillow_ok:
            self.app.set_status(tr("貼圖功能需要 Pillow 套件，請執行：pip install pillow"), ERR)
        else:
            texts = sum(1 for it in self.items if it.text_spec is not None)
            message = tr("已載入 {count} 張貼圖", count=len(self.items))
            if texts:
                message += tr("（其中 {count} 張可輸入文字）", count=texts)
            self.app.set_status(message, OK if self.items else WARN)

    def _make_tile(self, item: stickers.StickerItem) -> StickerTile:
        from tkinter import font as tkfont

        m = self.app.metrics
        if self._measure is None:
            self._measure = tkfont.Font(font=FONT_SMALL).measure
        text = self.display_text(item)
        tile = StickerTile(self.scroll.canvas, item, width=self._tile_w - 2 * self._gap, height=self._tile_h,
                           box=self._box, radius=m.px(10), caption_h=m.px(CAPTION_HEIGHT), font_measure=self._measure,
                           text=text)
        tile.bind("<ButtonRelease-1>", lambda e, t=tile: self._on_release(e, t), add="+")
        tile.bind("<Button-3>", lambda e, it=item: self._show_menu(e, it))
        tile.bind("<Enter>", lambda _e, t=tile: self._on_enter(t), add="+")
        tile.bind("<Leave>", lambda _e, t=tile: self._on_leave(t), add="+")
        self.tiles[item.key] = tile      # 先登記再送出縮圖工作：工作開始時會確認這張還在畫面上
        cache_key = self._thumb_key(item, text)
        cached = self._thumbs.get(cache_key)
        if cached is not None:
            self._thumbs.move_to_end(cache_key)
            tile.set_image(cached[0], cached[1], failed=cached[2])
        elif self._pillow_ok and cache_key not in self._pending:
            self._pending.add(cache_key)
            if text:
                self._text_executor.submit(self._text_thumb_job, item, text, self._generation)
            else:
                self._executor.submit(self._thumb_job, item, self._generation)
        return tile

    @staticmethod
    def _thumb_key(item: stickers.StickerItem, text: str) -> str:
        """縮圖快取的 key：文字貼圖每種文字各有一張縮圖。"""
        return f"{item.key}\0{text}" if text else item.key

    def display_text(self, item: stickers.StickerItem) -> str:
        """文字貼圖目前要套用的文字（搜尋框的文字）；一般貼圖或沒有搜尋時為空字串。"""
        return sticker_text.clean_text(self._query) if item.text_spec is not None else ""

    def _thumb_job(self, item: stickers.StickerItem, generation: int) -> None:
        if generation != self._generation or item.key not in self.tiles:
            # 已經重新掃描、或這張已經捲出畫面：不用做了（捲回來時會再要求）
            self._pending.discard(item.key)
            return
        try:
            info = stickers.inspect_sticker(item.path)
            image = stickers.load_thumbnail(item.path, self._box)
        except stickers.StickerError as exc:
            logger.warning("%s", exc)
            self._ready.put(("thumb", generation, item.key, None, None))
            return
        self._ready.put(("thumb", generation, item.key, image, info))

    def _text_thumb_job(self, item: stickers.StickerItem, text: str, generation: int) -> None:
        """套用文字的縮圖（訊息貼圖會連網請 LINE 畫字）。"""
        key = self._thumb_key(item, text)
        tile = self.tiles.get(item.key)
        if generation != self._generation or tile is None or tile.text != text:
            self._pending.discard(key)      # 已經改了搜尋文字或捲出畫面：不用畫了
            return
        try:
            image, _by_line = sticker_text.render(item.text_spec, text)
        except sticker_text.TextStickerError as exc:
            logger.warning("%s", exc)
            self._ready.put(("thumb", generation, key, None, None))
            return
        info = stickers.StickerInfo(image.width, image.height, 1, 0)
        image.thumbnail((self._box, self._box))
        self._ready.put(("thumb", generation, key, image, info))

    def _anim_job(self, item: stickers.StickerItem, token: int) -> None:
        try:
            frames = stickers.load_animation(item.path, self._box)
        except stickers.StickerError:
            return
        self._ready.put(("anim", token, item.key, frames, None))

    def _drain(self) -> None:
        """在主執行緒處理背景送回的結果：縮圖轉成 PhotoImage、動畫影格、匯入完成。"""
        delay = DRAIN_MS
        try:
            from PIL import ImageTk
        except ImportError:
            ImageTk = None      # 沒有 Pillow：仍要處理掃描與匯入，只是沒有縮圖

        try:
            for _ in range(DRAIN_BATCH):
                kind, tag, key, payload, info = self._ready.get_nowait()
                delay = 15      # 還有東西要處理就快一點回來，但每次只做一小批
                if kind == "scanned":
                    if tag == self._generation:
                        self.keywords = info
                        self._finish_scan(payload)
                elif kind == "text_sent":
                    # 成功才清空（失敗時保留文字，修正後可以直接再送一次）；送出期間又改了文字就不動
                    if tag and self.message_var.get().strip() == payload:
                        self.message_var.set("")
                elif kind == "imported":
                    self._finish_import(tag, info if info is not None else payload)
                elif kind == "thumb" and tag == self._generation and ImageTk is not None:
                    self._pending.discard(key)
                    photo = None if payload is None else ImageTk.PhotoImage(payload)
                    self._thumbs[key] = (photo, info, payload is None)
                    while len(self._thumbs) > THUMB_CACHE_SIZE:
                        self._thumbs.popitem(last=False)
                    tile = self.tiles.get(key.split("\0", 1)[0])
                    if tile is not None and self._thumb_key(tile.item, tile.text) == key:
                        tile.set_image(photo, info, failed=payload is None)
                elif kind == "anim" and ImageTk is not None:
                    tile = self.tiles.get(key)
                    if tile is not None and tag == tile.hover_token and tile.hovering and payload:
                        tile.start_animation([(ImageTk.PhotoImage(img), duration) for img, duration in payload])
        except queue.Empty:
            pass
        except tk.TclError:
            return      # 視窗關閉中
        self._drain_job = self.after(delay, self._drain)

    def _finish_import(self, count: int, error: OSError | None) -> None:
        self._importing = False
        if count == 0 and error is None:
            return      # 沒有東西匯入：保留 worker 已經顯示的說明
        self.rescan()
        if error is not None:
            self.app.set_status(tr("匯入貼圖失敗（已匯入 {count} 張）：{error}", count=count, error=error), ERR)
        else:
            self.app.set_status(tr("已匯入 {count} 張貼圖", count=count), OK)

    def shutdown(self) -> None:
        """程式結束時停止背景工作。"""
        self._generation += 1
        self.after_cancel(self._drain_job)
        self._executor.shutdown(wait=False, cancel_futures=True)
        self._text_executor.shutdown(wait=False, cancel_futures=True)
        self.actions.shutdown()

    # ================================================================== 互動
    def _on_enter(self, tile: StickerTile) -> None:
        info = tile.info
        if self._pillow_ok and info is not None and info.animated:
            tile.hover_token += 1
            self._executor.submit(self._anim_job, tile.item, tile.hover_token)

    @staticmethod
    def _on_leave(tile: StickerTile) -> None:
        tile.stop_animation()

    def _on_release(self, event: tk.Event, tile: StickerTile) -> None:
        # 和按鈕一致：在貼圖外放開代表反悔
        if 0 <= event.x < tile.winfo_width() and 0 <= event.y < tile.winfo_height():
            self.copy(tile.item)

    def _flash(self, item: stickers.StickerItem) -> None:
        tile = self.tiles.get(item.key)
        if tile is not None:
            tile.flash()

    def copy(self, item: stickers.StickerItem, text: str | None = None) -> None:
        """把貼圖複製到剪貼簿（文字貼圖套用搜尋框的文字），到 Discord 按 Ctrl+V 即可傳送。

        Args:
            item (stickers.StickerItem): 貼圖。
            text (str | None): 文字貼圖要套用的文字；None 表示用搜尋框的文字。
        """
        self._flash(item)
        self.actions.copy(item, self.display_text(item) if text is None else text)

    def send(self, item: stickers.StickerItem) -> None:
        """用 Webhook 傳送貼圖到 Discord；尚未設定 Webhook 時直接開啟設定視窗。"""
        self._flash(item)
        if not self.actions.send(item, self.display_text(item)):
            self.open_webhook_dialog()

    def copy_with_text(self, item: stickers.StickerItem) -> None:
        """詢問要套用的文字，再複製文字貼圖。"""
        text = simpledialog.askstring(
            tr("輸入貼圖文字"), tr("要套用在「{name}」上的文字（輸入 \\n 可換行）：", name=item.name),
            initialvalue=self.display_text(item) or item.default_text, parent=self.app.root)
        if text is not None and text.strip():
            self.copy(item, text.replace("\\n", "\n"))

    def edit_keywords(self, item: stickers.StickerItem) -> None:
        """編輯貼圖的關鍵字（在搜尋框輸入關鍵字時，這張貼圖會排在最前面）。"""
        current = ", ".join(self.keywords.get(item.key))
        builtin = "\n" + tr("內建關鍵字（訊息貼圖的預設文字）：{text}", text=item.default_text) if item.default_text else ""
        text = simpledialog.askstring(
            tr("編輯關鍵字"),
            tr("「{name}」的關鍵字，以逗號或空白分隔（例如：早安, 你好）：", name=item.name) + builtin,
            initialvalue=current, parent=self.app.root)
        if text is None:
            return
        words = parse_keywords(text)
        try:
            self.keywords.set(item.key, words)
        except OSError as exc:
            self.app.set_status(tr("無法儲存關鍵字：{error}", error=exc), ERR)
            return
        self.app.set_status(tr("已設定「{name}」的關鍵字：{words}", name=item.name, words=", ".join(words) or tr("（無）")),
                            OK)
        if self._query:
            self.layout(force=True)

    def keywords_of(self, item: stickers.StickerItem) -> tuple[str, ...]:
        """貼圖的所有關鍵字：使用者自訂的，加上訊息貼圖的預設文字。"""
        words = self.keywords.get(item.key)
        if item.default_text:
            words.append(item.default_text)
        return tuple(words)

    def _show_menu(self, event: tk.Event, item: stickers.StickerItem) -> None:
        menu = _menu(self.app.root)
        menu.add_command(label=tr("📋 複製到剪貼簿（到 Discord 按 Ctrl+V 傳送）"), command=lambda: self.copy(item))
        if item.text_spec is not None:
            menu.add_command(label=tr("✏ 輸入文字後複製…"), command=lambda: self.copy_with_text(item))
        menu.add_command(label=tr("📤 用 Webhook 傳送到 Discord"), command=lambda: self.send(item))
        menu.add_separator()
        menu.add_command(label=tr("🏷 編輯關鍵字…"), command=lambda: self.edit_keywords(item))
        menu.add_command(label=tr("📂 在檔案總管中顯示"), command=lambda: self.reveal(item))
        menu.add_command(label=tr("🗑 刪除貼圖…（檔案將移至資源回收筒）"), command=lambda: self.delete(item))
        _popup(menu, event)

    @staticmethod
    def reveal(item: stickers.StickerItem) -> None:
        """在檔案總管中顯示貼圖檔。"""
        if sys.platform == "win32":
            subprocess.Popen(["explorer", "/select,", str(item.path)])
        else:
            subprocess.Popen(["xdg-open", str(item.path.parent)])

    def delete(self, item: stickers.StickerItem) -> None:
        """把貼圖檔移到資源回收筒（需先確認）。"""
        if not messagebox.askyesno(tr("刪除貼圖"), tr("要把「{name}」移到資源回收筒嗎？", name=item.name),
                                   parent=self.app.root):
            return
        try:
            move_to_recycle_bin(item.path)
        except OSError as exc:
            self.app.set_status(tr("無法刪除：{error}", error=exc), ERR)
            return
        self.rescan()
        self.app.set_status(tr("已將「{name}」移到資源回收筒", name=item.name), OK)

    # ================================================================== 排版
    def _on_search_changed(self) -> None:
        if self.search_var.get():
            self._placeholder.place_forget()
        else:
            self._placeholder.place(in_=self.search_entry, x=0, rely=0.5, anchor="w")
        if self._search_job is not None:
            self.after_cancel(self._search_job)
        self._search_job = self.after(SEARCH_DEBOUNCE_MS, self._apply_search)

    def _apply_search(self) -> None:
        self._search_job = None
        query = self.search_var.get().strip()
        if query != self._query:
            self._query = query
            self.layout(force=True)

    def build_sections(self) -> list[tuple[str, list[stickers.StickerItem]]]:
        """依搜尋文字把貼圖分區（標題, 貼圖）。

        沒有搜尋時：一般貼圖依系列分區，文字貼圖的系列排在最後並加上「✏」。
        有搜尋時：關鍵字符合的貼圖 →（套用搜尋文字的）文字貼圖 → 名稱或系列符合的一般貼圖。

        Returns:
            list[tuple[str, list[stickers.StickerItem]]]: 各區的標題與貼圖。
        """
        query = self._query
        normal = [it for it in self.items if it.text_spec is None]
        texts = [it for it in self.items if it.text_spec is not None]

        def by_series(items: list[stickers.StickerItem], prefix: str = "") -> list[tuple[str, list]]:
            groups: dict[str, list[stickers.StickerItem]] = {}
            for item in items:
                groups.setdefault(item.series, []).append(item)
            return [(prefix + (stickers.series_title(series) or tr("（根目錄）")), members)
                    for series, members in groups.items()]

        if not query:
            return by_series(normal) + by_series(texts, "✏ ")
        q = query.casefold()
        matched = [it for it in self.items if keyword_matches(query, self.keywords_of(it))]
        matched_keys = {it.key for it in matched}
        sections: list[tuple[str, list]] = []
        if matched:
            sections.append((tr("💡 關鍵字「{query}」", query=query), matched))
        sections += by_series([it for it in texts if it.key not in matched_keys], "✏ ")
        named = [it for it in normal if it.key not in matched_keys
                 and (q in it.name.casefold() or q in it.series.casefold())]
        sections += by_series(named)
        return sections

    def layout(self, force: bool = False) -> None:
        """依視窗寬度與搜尋條件計算每一列的位置（依系列分區），再補上畫面附近的貼圖。

        只為「看得到的那幾列」建立元件（虛擬捲動），幾千張貼圖也能瞬間開啟。

        Args:
            force (bool): 欄數沒變也要重排（內容有變動時）。
        """
        columns = grid_columns(self.scroll.canvas.winfo_width(), self._tile_w)
        if columns == self._columns and not force:
            self._update_visible()
            return
        self._columns = columns
        m = self.app.metrics
        row_h = self._tile_h + 2 * self._gap
        sections = self.build_sections()
        shown = sum(len(members) for _title, members in sections)
        # 只有根目錄一區、而且沒有搜尋時不需要標題
        show_headers = len(sections) > 1 or bool(self._query) or any(
            members[0].series or members[0].text_spec is not None for _title, members in sections)
        rows: list[tuple[str, object]] = []
        tops: list[int] = []
        y = 0
        for title, members in sections:
            if show_headers:
                tops.append(y)
                rows.append(("header", f"{title}　{len(members)}"))
                y += m.px(HEADER_HEIGHT)
            for i in range(0, len(members), columns):
                tops.append(y)
                rows.append(("tiles", members[i:i + columns]))
                y += row_h
        self._rows, self._row_tops = rows, tops
        # 清掉舊的標題與提示；貼圖元件留著，_update_visible 會重新定位或回收
        canvas = self.scroll.canvas
        for item_id in self._headers.values():
            canvas.delete(item_id)
        self._headers.clear()
        if self._hint is not None:
            canvas.delete(self._hint)
            self._hint = None
        if not shown:
            self._hint = self._empty_hint()
            y = m.px(200)
        self.scroll.set_virtual_height(max(1, y))
        self.count_label.configure(text=tr("{shown} / {total} 張貼圖", shown=shown, total=len(self.items)))
        self.scroll.canvas.yview_moveto(0)
        self._update_visible()

    def _update_visible(self) -> None:
        """依目前捲動位置，建立／定位畫面附近的貼圖與標題，回收離得很遠的。"""
        if not self._row_tops:
            for key in list(self.tiles):
                self._drop_tile(key)
            return
        canvas = self.scroll.canvas
        view_h = max(canvas.winfo_height(), 1)
        top = canvas.canvasy(0) - view_h * BUFFER_SCREENS
        bottom = canvas.canvasy(0) + view_h * (1 + BUFFER_SCREENS)
        first = max(0, bisect.bisect_right(self._row_tops, top) - 1)
        last = bisect.bisect_right(self._row_tops, bottom)
        m = self.app.metrics
        wanted: dict[str, tuple[stickers.StickerItem, int, int]] = {}
        headers: set[int] = set()
        for index in range(first, min(last, len(self._rows))):
            kind, content = self._rows[index]
            y = self._row_tops[index]
            if kind == "header":
                headers.add(index)
                if index not in self._headers:
                    self._headers[index] = canvas.create_text(
                        self._gap + m.px(4), y + m.px(HEADER_HEIGHT) - m.px(4), text=content, font=FONT_BOLD,
                        fill=FG_MUTED, anchor="sw")
            else:
                for col, item in enumerate(content):
                    wanted[item.key] = (item, col * self._tile_w + self._gap, y + self._gap)
        # 不在畫面附近的、或文字貼圖要套用的文字變了：回收重做
        for key in [k for k, t in self.tiles.items() if k not in wanted or t.text != self.display_text(t.item)]:
            self._drop_tile(key)
        for index in [i for i in self._headers if i not in headers]:
            canvas.delete(self._headers.pop(index))
        for key, (item, x, y) in wanted.items():
            tile = self.tiles.get(key)
            if tile is None:
                tile = self._make_tile(item)
                tile.window_id = canvas.create_window(x, y, window=tile, anchor="nw")
            else:
                canvas.coords(tile.window_id, x, y)

    def _drop_tile(self, key: str) -> None:
        tile = self.tiles.pop(key)
        self.scroll.canvas.delete(tile.window_id)
        tile.destroy()

    def _empty_hint(self) -> int:
        if self._query:
            text = tr("找不到符合「{query}」的貼圖", query=self._query)
        else:
            text = tr("此資料夾中沒有貼圖\n"
                      "按一下「➕ 匯入貼圖」加入圖片，或按「📁 選擇資料夾」選擇已有貼圖的資料夾"
                      "（例如 LINE 貼圖下載器的輸出資料夾）")
        canvas = self.scroll.canvas
        return canvas.create_text(max(canvas.winfo_width(), 1) / 2, self.app.metrics.px(60), text=text,
                                  fill=FG_SUBTLE, font=FONT_BODY, justify="center", anchor="n")
