"""貼圖頁：貼圖縮圖牆（依系列分區），按一下傳送到 Discord；右鍵複製到剪貼簿、顯示檔案或刪除。"""

from __future__ import annotations

import logging
import os
import queue
import subprocess
import sys
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import TYPE_CHECKING

from soundboard import stickers
from soundboard.config import default_sound_dir
from soundboard.gui.menus import _menu, _popup
from soundboard.gui.presenters import grid_columns
from soundboard.gui.sticker_actions import StickerActions
from soundboard.gui.styles import FONT_BODY, FONT_BOLD, FONT_H1, FONT_SMALL
from soundboard.gui.webhook_dialog import WebhookDialog
from soundboard.gui.widgets import RoundedButton, RoundedFrame, ScrollArea, rounded_rect_points
from soundboard.i18n import tr
from soundboard.library import move_to_recycle_bin
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
                 radius: int, caption_h: int, font_measure) -> None:
        """
        Args:
            parent (tk.Misc): 父元件。
            item (stickers.StickerItem): 貼圖。
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
        caption = ellipsize(item.name, font_measure, width - 12)
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
        self.items: list[stickers.StickerItem] = []
        self.tiles: dict[str, StickerTile] = {}
        self._infos: dict[str, stickers.StickerInfo | None] = {}
        self._section_widgets: list[tk.Widget] = []
        self._columns = 0
        self._configured_columns = 0
        self._generation = 0
        self._tile_w = m.px(TILE_WIDTH)
        self._gap = m.px(TILE_GAP)
        self._pad = m.px(PAGE_PADDING)
        self._box = m.px(THUMB_BOX)
        self._loaded = False
        # 縮圖由背景執行緒做（讀檔與縮放），PhotoImage 一律在主執行緒建立
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="thumb")
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
        inner = card.inner
        self.discord_label = tk.Label(inner, bg=CARD, fg=FG_MUTED, font=FONT_BODY, anchor="w", justify="left")
        self.discord_label.pack(side="left", fill="x", expand=True)
        self._button(inner, tr("⚙ Webhook 設定…"), self.open_webhook_dialog, surface=CARD).pack(side="right")
        self.size_var = tk.StringVar()
        self.size_combo = ttk.Combobox(inner, textvariable=self.size_var, state="readonly", width=12, font=FONT_BODY,
                                       values=[send_size_label(e) for e in stickers.SEND_SIZE_CHOICES])
        self.size_combo.bind("<<ComboboxSelected>>", lambda _e: self._on_size_changed())
        self.size_combo.bind("<MouseWheel>", lambda e: (self.scroll.scroll(int(-e.delta / 120)), "break")[1])
        self.size_combo.pack(side="right", padx=(0, m.px(8)))
        tk.Label(inner, text=tr("傳送大小"), bg=CARD, fg=FG_MUTED, font=FONT_SMALL).pack(side="right", padx=(0, m.px(6)))
        self.target_combo = ttk.Combobox(inner, state="readonly", width=16, font=FONT_BODY)
        self.target_combo.bind("<<ComboboxSelected>>", lambda _e: self._on_target_changed())
        self.target_combo.bind("<MouseWheel>", lambda e: (self.scroll.scroll(int(-e.delta / 120)), "break")[1])
        self.target_combo.pack(side="right", padx=(m.px(8), m.px(14)))

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
        self._placeholder = tk.Label(search.inner, text=tr("搜尋貼圖或系列名稱…"), bg=INPUT, fg=FG_SUBTLE,
                                     font=FONT_BODY, cursor="xterm")
        self._placeholder.place(in_=self.search_entry, x=0, rely=0.5, anchor="w")
        self._placeholder.bind("<Button-1>", lambda _e: self.search_entry.focus_set())
        self.count_label = tk.Label(bar, bg=SURFACE, fg=FG_SUBTLE, font=FONT_SMALL)
        self.count_label.grid(row=0, column=1, padx=(m.px(10), 0))

    def _build_grid(self) -> None:
        self.scroll = ScrollArea(self, bg=SURFACE, on_resize=lambda _w: self.layout())
        m = self.app.metrics
        self.scroll.pack(fill="both", expand=True, padx=(self._pad - self._gap, m.px(6)), pady=(0, m.px(6)))
        self.grid_frame = self.scroll.inner

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
            self.discord_label.configure(text=tr("⚠ 尚未設定 Discord Webhook，請先按右側「⚙ Webhook 設定…」"), fg=WARN)
        else:
            self.target_combo.configure(state="readonly")
            self.target_combo.set(active[0])
            self.discord_label.configure(text=tr("按一下貼圖，傳送到 Discord 頻道："), fg=FG_MUTED)
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
        self._import([(Path(p), self.folder) for p in chosen])

    def import_folder(self) -> None:
        """選擇資料夾（例如 LINE 貼圖下載器的一個系列），連同子資料夾結構複製進貼圖資料夾。"""
        chosen = filedialog.askdirectory(title=tr("選擇要匯入的貼圖資料夾"))
        if not chosen:
            return
        source = Path(chosen)
        files = [(p, self.folder / source.name / p.parent.relative_to(source))
                 for p in sorted(source.rglob("*")) if p.is_file() and p.suffix.lower() in stickers.STICKER_EXTENSIONS]
        self._import(files)

    def _import(self, files: list[tuple[Path, Path]]) -> None:
        folder = self.folder.resolve()
        # 來源已經在貼圖資料夾裡就不用複製（否則會產生一堆「(2)」重複檔）
        files = [(s, d) for s, d in files if folder not in s.resolve().parents]
        if not files:
            self.app.set_status(tr("沒有可匯入的貼圖（支援 PNG／GIF／WebP／APNG／JPG）"), WARN)
            return
        try:
            count = self.actions.import_files(files)
        except OSError as exc:
            self.app.set_status(tr("匯入貼圖失敗：{error}", error=exc), ERR)
            return
        self.rescan()
        self.app.set_status(tr("已匯入 {count} 張貼圖", count=count), OK)

    # ================================================================== 掃描與縮圖
    def on_show(self) -> None:
        """切換到貼圖頁時呼叫：第一次才掃描資料夾，不拖慢程式啟動。"""
        if not self._loaded:
            self.rescan()

    def rescan(self) -> None:
        """重新掃描貼圖資料夾並重建縮圖牆。"""
        self._loaded = True
        folder = self.folder
        self.folder_label.configure(text=str(folder))
        self._generation += 1
        self.items = stickers.scan_stickers(folder)
        self.rebuild_tiles()
        if self.items and not self._pillow_ok:
            self.app.set_status(tr("貼圖功能需要 Pillow 套件，請執行：pip install pillow"), ERR)
        else:
            self.app.set_status(tr("已載入 {count} 張貼圖", count=len(self.items)), OK if self.items else WARN)

    def rebuild_tiles(self) -> None:
        """依目前的貼圖清單重建所有縮圖按鈕，並在背景產生縮圖。"""
        from tkinter import font as tkfont

        m = self.app.metrics
        for tile in self.tiles.values():
            tile.destroy()
        self.tiles.clear()
        self._infos.clear()
        width = self._tile_w - 2 * self._gap
        height = self._box + m.px(CAPTION_HEIGHT) + m.px(20)
        measure = tkfont.Font(font=FONT_SMALL).measure
        generation = self._generation
        for item in self.items:
            tile = StickerTile(self.grid_frame, item, width=width, height=height, box=self._box, radius=m.px(10),
                               caption_h=m.px(CAPTION_HEIGHT), font_measure=measure)
            tile.bind("<ButtonRelease-1>", lambda e, t=tile: self._on_release(e, t), add="+")
            tile.bind("<Button-3>", lambda e, it=item: self._show_menu(e, it))
            tile.bind("<Enter>", lambda _e, t=tile: self._on_enter(t), add="+")
            tile.bind("<Leave>", lambda _e, t=tile: self._on_leave(t), add="+")
            self.tiles[item.key] = tile
            if self._pillow_ok:
                self._executor.submit(self._thumb_job, item, generation)
        self.layout(force=True)

    def _thumb_job(self, item: stickers.StickerItem, generation: int) -> None:
        if generation != self._generation:
            return      # 已經重新掃描：這份結果沒人要了
        try:
            info = stickers.inspect_sticker(item.path)
            image = stickers.load_thumbnail(item.path, self._box)
        except stickers.StickerError as exc:
            logger.warning("%s", exc)
            self._ready.put(("thumb", generation, item.key, None, None))
            return
        self._ready.put(("thumb", generation, item.key, image, info))

    def _anim_job(self, item: stickers.StickerItem, token: int) -> None:
        try:
            frames = stickers.load_animation(item.path, self._box)
        except stickers.StickerError:
            return
        self._ready.put(("anim", token, item.key, frames, None))

    def _drain(self) -> None:
        """在主執行緒把背景做好的縮圖轉成 PhotoImage 放上去。"""
        if not self._pillow_ok:
            self._drain_job = self.after(DRAIN_MS, self._drain)
            return
        from PIL import ImageTk

        try:
            while True:
                kind, tag, key, payload, info = self._ready.get_nowait()
                tile = self.tiles.get(key)
                if tile is None:
                    continue
                if kind == "thumb" and tag == self._generation:
                    self._infos[key] = info
                    tile.set_image(None if payload is None else ImageTk.PhotoImage(payload), info,
                                   failed=payload is None)
                elif kind == "anim" and tag == tile.hover_token and tile.hovering and payload:
                    tile.start_animation([(ImageTk.PhotoImage(img), duration) for img, duration in payload])
        except queue.Empty:
            pass
        except tk.TclError:
            return      # 視窗關閉中
        self._drain_job = self.after(DRAIN_MS, self._drain)

    def shutdown(self) -> None:
        """程式結束時停止背景工作。"""
        self._generation += 1
        self.after_cancel(self._drain_job)
        self._executor.shutdown(wait=False, cancel_futures=True)
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
            self.send(tile.item)

    def send(self, item: stickers.StickerItem) -> None:
        """傳送貼圖到 Discord；尚未設定 Webhook 時直接開啟設定視窗。"""
        tile = self.tiles.get(item.key)
        if tile is not None:
            tile.flash()
        if not self.actions.send(item):
            self.open_webhook_dialog()

    def _show_menu(self, event: tk.Event, item: stickers.StickerItem) -> None:
        menu = _menu(self.app.root)
        menu.add_command(label=tr("📤 傳送到 Discord"), command=lambda: self.send(item))
        menu.add_command(label=tr("📋 複製到剪貼簿（到 Discord 按 Ctrl+V，以自己的身分傳送）"),
                         command=lambda: self.actions.copy(item))
        menu.add_separator()
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
        self.layout(force=True)

    def _visible_items(self) -> list[stickers.StickerItem]:
        query = self.search_var.get().strip().casefold()
        if not query:
            return self.items
        return [it for it in self.items if query in it.name.casefold() or query in it.series.casefold()]

    def layout(self, force: bool = False) -> None:
        """依視窗寬度與搜尋條件重新排列縮圖（依系列分區）。

        Args:
            force (bool): 欄數沒變也要重排（內容有變動時）。
        """
        columns = grid_columns(self.scroll.canvas.winfo_width(), self._tile_w)
        if columns == self._columns and not force:
            return
        self._columns = columns
        m = self.app.metrics
        for tile in self.tiles.values():
            tile.grid_forget()
        for widget in self._section_widgets:
            widget.destroy()
        self._section_widgets.clear()

        visible = self._visible_items()
        series_names = list(dict.fromkeys(it.series for it in visible))
        show_headers = len(series_names) > 1 or (series_names and series_names[0] != "")
        row = 0
        for series in series_names:
            members = [it for it in visible if it.series == series]
            if show_headers:
                header = tk.Label(self.grid_frame, text=f"{series or tr('（根目錄）')}　{len(members)}", font=FONT_BOLD,
                                  bg=SURFACE, fg=FG_MUTED, anchor="w")
                header.grid(row=row, column=0, columnspan=columns, sticky="we", padx=self._gap,
                            pady=(m.px(14) if row else m.px(2), m.px(2)))
                self._section_widgets.append(header)
                row += 1
            for i, item in enumerate(members):
                self.tiles[item.key].grid(row=row + i // columns, column=i % columns, padx=self._gap,
                                          pady=self._gap, sticky="nsew")
            row += (len(members) + columns - 1) // columns
        if not visible:
            self._section_widgets.append(self._empty_hint(row, columns))
        for c in range(columns):
            self.grid_frame.columnconfigure(c, minsize=self._tile_w - 2 * self._gap, weight=1)
        for c in range(columns, self._configured_columns):
            self.grid_frame.columnconfigure(c, minsize=0, weight=0)
        self._configured_columns = max(columns, self._configured_columns)
        self.count_label.configure(text=tr("{shown} / {total} 張貼圖", shown=len(visible), total=len(self.items)))
        self.scroll.canvas.yview_moveto(0)

    def _empty_hint(self, row: int, columns: int) -> tk.Label:
        if self.search_var.get().strip():
            text = tr("找不到符合「{query}」的貼圖", query=self.search_var.get().strip())
        else:
            text = tr("此資料夾中沒有貼圖\n"
                      "按一下「➕ 匯入貼圖」加入圖片，或按「📁 選擇資料夾」選擇已有貼圖的資料夾"
                      "（例如 LINE 貼圖下載器的輸出資料夾）")
        hint = tk.Label(self.grid_frame, text=text, bg=SURFACE, fg=FG_SUBTLE, font=FONT_BODY, justify="center")
        hint.grid(row=row, column=0, columnspan=columns, pady=self.app.metrics.px(60))
        return hint
