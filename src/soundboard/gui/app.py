"""主視窗：左側導覽列＋內容區（首頁／回放錄音／設定頁）＋狀態列，以及播放、小視窗、背景訊息等核心流程。"""

from __future__ import annotations

import logging
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tkinter import filedialog, messagebox

import numpy as np

from soundboard import devices
from soundboard.app_audio import AppAudioRouter, app_key
from soundboard.autostart import RESTART_FLAG, AutostartManager
from soundboard.clips import render_gain
from soundboard.config import PREFERRED_HOST_API, is_frozen, resource_path, save_settings, settings_path
from soundboard.decoder import DecodeError, find_ffmpeg
from soundboard.denoise import DenoiseLevel
from soundboard.discord_voice import VoiceJoinDetector, VoiceWatcher
from soundboard.engine import AudioEngine
from soundboard.gui.clip_editor import ClipEditor
from soundboard.gui.editing import LibraryEditor
from soundboard.gui.home import HomePage
from soundboard.gui.hotkey_controller import HotkeyController
from soundboard.gui.mixer_page import MASTER, MONITOR, SOUNDS, MixerPage
from soundboard.gui.presenters import voice_status
from soundboard.gui.replay_page import ReplayPage
from soundboard.gui.settings_page import SettingsPage
from soundboard.gui.styles import FONT_BRAND, FONT_SMALL, FONT_SMALL_BOLD, Metrics, setup_style
from soundboard.gui.widgets import RoundedButton
from soundboard.i18n import N_, SOURCE_LANGUAGE, current_language, tr
from soundboard.library import ClipCache, scan_folder
from soundboard.models import AppSettings, SoundItem
from soundboard.overlay import OverlayWindow
from soundboard.theme import (
    BTN_PLAYING, ERR, FG, FG_MUTED, FG_SUBTLE, NAV_ACTIVE, NAV_HOVER, OK, PREVIEW, PREVIEW_HOVER, SIDEBAR,
    STATUS_BAR, SURFACE, WARN,
)
from soundboard.version import display_version
from soundboard.tray import SEPARATOR, TrayIcon, TrayMenuItem, instance_id

logger = logging.getLogger(__name__)

POLL_MS = 50
GAINED_CACHE_SIZE = 16      # 最多快取幾個「已套用個別音量」的音效
VOICE_WATCH_SECONDS = 1.0
SIDEBAR_WIDTH = 210          # 100% 縮放時的側邊欄寬度
DEFAULT_SIZE = (1180, 780)   # 100% 縮放時的預設視窗大小
MIN_SIZE = (860, 560)
# 只標記：模組載入時還沒決定介面語言，建立狀態列時才 tr
STATUS_HINT = N_("左鍵：傳送給通話對象並監聽　Shift＋左鍵：試聽（僅本機）　Ctrl＋左鍵：複選　"
                 "右鍵：更多選項　Ctrl+P：試聽模式　Esc：全部停止")
PAGES = ("home", "replay", "mixer", "settings")


class SoundboardApp:
    """音效版主程式。

    介面分成首頁（:class:`HomePage`，只放音效）與設定頁（:class:`SettingsPage`），
    這個類別負責兩頁共用的部分：音效庫、播放、預覽模式、小視窗、快捷鍵與背景執行緒訊息。
    """

    def __init__(self, root: tk.Tk, settings: AppSettings, *, autostart: AutostartManager | None = None,
                 tray: bool = True) -> None:
        """
        Args:
            root (tk.Tk): Tk 根視窗。
            settings (AppSettings): 使用者設定。
            autostart (AutostartManager | None): 開機自動啟動的登錄檔設定；None 用預設位置
                （測試時傳入暫存機碼，避免動到真正的開機項目）。
            tray (bool): 是否建立系統匣圖示。
        """
        self.root = root
        self.settings = settings
        self.metrics = Metrics.from_root(root)
        self.cache = ClipCache()
        # 套用個別音量（並經過峰值限制器）後的音訊；按鍵執行緒也會播放，所以加鎖
        self._gained: OrderedDict[tuple[str, float, int], np.ndarray] = OrderedDict()
        self._gained_lock = threading.Lock()
        self.sound_editors: list[ClipEditor] = []
        self.items: list[SoundItem] = []
        # 整個換掉 dict（而非原地修改），快捷鍵執行緒讀取時才不會看到改到一半的內容
        self.items_by_key: dict[str, SoundItem] = {}
        # key → 目前按鈕底色；藍色＝朋友聽得到，綠色＝只有自己聽得到（預覽）
        self.playing: dict[str, str] = {}
        # 預覽模式不寫入設定：避免下次開啟時忘了它還開著，以為朋友聽得到。
        # 用一般 bool 而非 Tk 變數，因為快捷鍵執行緒也要讀它
        self._preview_flag = False
        self._voice = VoiceJoinDetector()
        # 背景執行緒不能直接碰 Tk 元件，所以用 queue 把訊息交回主執行緒處理
        self._messages: queue.Queue[tuple[str, str]] = queue.Queue()
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="decode")
        self._scan_generation = 0
        self._page = ""
        # 一般 bool：系統匣執行緒建立選單時會讀它
        self.window_visible = True
        self.autostart = autostart or AutostartManager()
        if autostart is None and not os.environ.get("SOUNDBOARD_SETTINGS"):
            # exe 被搬到別的資料夾後，開機項目會指向不存在的檔案；只在用正式設定檔時修，測試用的設定檔不碰開機項目
            self.autostart.repair_if_stale()

        if settings.host_api not in devices.list_host_apis():
            settings.host_api = PREFERRED_HOST_API
        self.engine = AudioEngine(settings.host_api)
        # 混音器的程式聲音：監看執行緒接上／移除程式時，把整組來源交給引擎（整個換掉 dict）
        self.app_router = AppAudioRouter(lambda sources: setattr(self.engine, "live_sources", sources))
        self.apply_engine_gains()
        self.hotkeys = HotkeyController(self)
        self.editor = LibraryEditor(self)

        setup_style(root, self.metrics)
        self._build_shell()
        self.home = HomePage(self.content, self)
        self.settings_page = SettingsPage(self.content, self)
        self.replay_page = ReplayPage(self.content, self)
        self.mixer_page = MixerPage(self.content, self)
        self.overlay = OverlayWindow(
            root, settings,
            on_play=lambda item, force_preview: self.play(item, to_mic=not (force_preview or self.preview_mode)),
            on_stop=self.stop_all,
            on_hidden=self._on_overlay_hidden,
            on_changed=self.save,
            on_group_changed=self._on_overlay_group_changed,
        )
        self.show_page("home")
        self._update_voice_chip()
        self.settings_page.load_devices(first_run=True)
        self.rescan()
        self.hotkeys.start()
        if settings.overlay_visible:
            self.overlay.show()
        self.home.update_overlay_button(settings.overlay_visible)
        self._update_preview_ui()
        self.tray = self._start_tray() if tray else None
        self.apply_mixer()
        self.settings_page.update_background_ui()

        root.protocol("WM_DELETE_WINDOW", self.close_window)
        root.bind("<Escape>", lambda _e: (self.stop_all(), self.home.clear_selection()))
        root.bind("<Delete>", self._on_delete_key)
        root.bind("<Control-p>", lambda _e: self.toggle_preview())
        root.bind("<Control-P>", lambda _e: self.toggle_preview())  # 大寫鎖定時
        root.bind_all("<MouseWheel>", self._on_mousewheel)
        self._poll_job = root.after(POLL_MS, self._poll)
        self.settings_page.start_device_watch()
        # 背景執行緒只負責查詢，結果經由 queue 交回主執行緒（_poll）處理
        self._voice_watcher = VoiceWatcher(
            lambda r: self._messages.put(("voice", "?" if r is None else "1" if r else "0")),
            VOICE_WATCH_SECONDS)
        self._voice_watcher.start()

    # ================================================================== 外框
    def _build_shell(self) -> None:
        """建立視窗外框：左側導覽列、右側內容區、底部狀態列。"""
        m = self.metrics
        root = self.root
        root.title(tr("音效版 Soundboard"))
        width, height = (m.px(v) for v in DEFAULT_SIZE)
        # 高 DPI 的小螢幕上放大後可能比螢幕還大，限制在螢幕的九成以內
        width = min(width, int(root.winfo_screenwidth() * 0.9))
        height = min(height, int(root.winfo_screenheight() * 0.85))
        root.geometry(f"{width}x{height}")
        root.minsize(*(m.px(v) for v in MIN_SIZE))

        self.status = tk.Label(root, bg=STATUS_BAR, fg=FG_MUTED, anchor="w", padx=m.px(14), pady=m.px(5),
                               font=FONT_SMALL, text=tr(STATUS_HINT))
        self.status.pack(fill="x", side="bottom")

        sidebar = tk.Frame(root, bg=SIDEBAR, width=m.px(SIDEBAR_WIDTH))
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)
        brand = tk.Frame(sidebar, bg=SIDEBAR)
        brand.pack(fill="x", padx=m.px(16), pady=(m.px(20), m.px(18)))
        tk.Label(brand, text=tr("🔊 音效版"), font=FONT_BRAND, bg=SIDEBAR, fg=FG, anchor="w").pack(fill="x")
        # 中文介面在品牌名稱下補上英文名稱；其他語言的品牌名稱本身就是 Soundboard，只顯示版本
        subtitle = f"v{display_version()}"
        if current_language() == SOURCE_LANGUAGE:
            subtitle = f"Soundboard  {subtitle}"
        tk.Label(brand, text=subtitle, font=FONT_SMALL, bg=SIDEBAR, fg=FG_SUBTLE, anchor="w").pack(fill="x")

        self._nav: dict[str, RoundedButton] = {}
        for page, text in (("home", tr("🏠   首頁")), ("replay", tr("⏺   回放錄音")), ("mixer", tr("🎚   混音器")),
                           ("settings", tr("⚙   設定"))):
            btn = RoundedButton(sidebar, text, lambda p=page: self.show_page(p), surface=SIDEBAR, bg=SIDEBAR,
                                hover=NAV_HOVER, layout="row", subfg=WARN, subfont=FONT_SMALL_BOLD,
                                radius=m.px(6), padx=m.px(12), pady=m.px(8))
            btn.pack(fill="x", padx=m.px(10), pady=m.px(2))
            self._nav[page] = btn

        # 側邊欄底部的狀態燈：不管在哪一頁都看得到「朋友聽不聽得到」與 Discord 語音狀態
        chips = tk.Frame(sidebar, bg=SIDEBAR)
        chips.pack(side="bottom", fill="x", padx=m.px(16), pady=m.px(16))
        tk.Label(chips, text=tr("狀態"), font=FONT_SMALL_BOLD, bg=SIDEBAR, fg=FG_SUBTLE, anchor="w").pack(
            fill="x", pady=(0, m.px(4)))
        self._vmic_chip = self._chip(chips)
        self._voice_chip = self._chip(chips)
        self._preview_chip = self._chip(chips)
        self._replay_chip = self._chip(chips)
        self._set_chip(self._replay_chip, "", None)
        for chip, page in ((self._vmic_chip, "settings"), (self._replay_chip, "replay")):
            for widget in chip:
                widget.bind("<Button-1>", lambda _e, p=page: self.show_page(p))
                widget.configure(cursor="hand2")

        self.content = tk.Frame(root, bg=SURFACE)
        self.content.pack(side="left", fill="both", expand=True)

    def _chip(self, parent: tk.Misc) -> tuple[tk.Label, tk.Label]:
        """建立一個「● 文字」狀態燈，回傳（圓點, 文字）。"""
        row = tk.Frame(parent, bg=SIDEBAR)
        row.pack(fill="x", pady=self.metrics.px(2))
        dot = tk.Label(row, text="●", font=FONT_SMALL, bg=SIDEBAR, fg=FG_SUBTLE)
        dot.pack(side="left")
        text = tk.Label(row, font=FONT_SMALL, bg=SIDEBAR, fg=FG_MUTED, anchor="w", justify="left",
                        wraplength=self.metrics.px(SIDEBAR_WIDTH - 50))
        text.pack(side="left", fill="x", expand=True, padx=(self.metrics.px(6), 0))
        return dot, text

    @staticmethod
    def _set_chip(chip: tuple[tk.Label, tk.Label], text: str, color: str | None) -> None:
        dot, label = chip
        if color is None:
            dot.pack_forget()
            label.pack_forget()
            return
        if not dot.winfo_ismapped():
            dot.pack(side="left")
            label.pack(side="left", fill="x", expand=True)
        dot.configure(fg=color)
        label.configure(text=text)

    def show_page(self, page: str) -> None:
        """切換到首頁或設定頁。

        Args:
            page (str): ``"home"``、``"replay"`` 或 ``"settings"``。

        Raises:
            ValueError: 頁面名稱不正確時拋出。
        """
        if page not in PAGES:
            raise ValueError(f"未知的頁面：{page}")
        if page == self._page:
            return
        self._page = page
        for name, frame in (("home", self.home), ("replay", self.replay_page), ("mixer", self.mixer_page),
                            ("settings", self.settings_page)):
            if name == page:
                frame.pack(fill="both", expand=True)
            else:
                frame.pack_forget()
        for name, btn in self._nav.items():
            if name == page:
                btn.set_colors(NAV_ACTIVE, NAV_ACTIVE)
            else:
                btn.set_colors(SIDEBAR, NAV_HOVER)
        if page == "mixer":
            self.mixer_page.on_show()
        if page == "settings" and hasattr(self, "tray"):
            # 使用者可能在工作管理員停用了開機項目，每次打開設定頁都重新讀一次
            self.settings_page.update_background_ui()

    @property
    def current_page(self) -> str:
        """目前顯示的頁面（``"home"``、``"replay"`` 或 ``"settings"``）。"""
        return self._page

    def refresh_audio_status(self) -> None:
        """裝置狀態改變後，更新首頁橫幅、導覽列標記與側邊欄狀態燈。"""
        warnings = self.settings_page.warnings
        self.home.set_warnings(warnings)
        self._nav["settings"].set_text(self._nav["settings"].text, "⚠" if warnings else "")
        if self.engine.virtual_mic_active:
            self._set_chip(self._vmic_chip, tr("虛擬麥克風：運作中"), OK)
        else:
            self._set_chip(self._vmic_chip, tr("虛擬麥克風：未啟用（通話對象聽不到音效）"), WARN)

    def set_replay_chip(self, value: tuple[str, str] | None) -> None:
        """更新側邊欄的回放錄音狀態燈。

        Args:
            value (tuple[str, str] | None): (文字, 顏色)；None 表示隱藏（回放錄音沒開）。
        """
        if value is None:
            self._set_chip(self._replay_chip, "", None)
        else:
            self._set_chip(self._replay_chip, *value)

    def submit(self, work: Callable[[], object]) -> None:
        """在背景執行緒執行一件工作（結果請自行經由 queue 交回主執行緒）。

        Args:
            work (Callable[[], object]): 要執行的函式。
        """
        self._executor.submit(work)

    def _update_voice_chip(self) -> None:
        text, color = voice_status(self._voice.in_voice)
        self._set_chip(self._voice_chip, text.replace("🟢 ", ""), OK if color == OK else FG_SUBTLE)

    # ================================================================== 共用狀態
    @property
    def voice_in_voice(self) -> bool | None:
        """Discord 語音偵測結果：True = 通話中、False = 未在語音中、None = 無法偵測。"""
        return self._voice.in_voice

    def post_message(self, kind: str, message: str) -> None:
        """從背景執行緒送訊息給主執行緒（由 _poll 處理）。

        Args:
            kind (str): 訊息種類（error / status / action / voice）。
            message (str): 內容。
        """
        self._messages.put((kind, message))

    def denoise_level(self) -> DenoiseLevel:
        """目前的降噪強度（電腦不支援時一律為關閉）。

        Returns:
            DenoiseLevel: 降噪強度。
        """
        if not self.engine.denoise_supported():
            return DenoiseLevel.OFF
        try:
            return DenoiseLevel(self.settings.denoise_level)
        except ValueError:
            return DenoiseLevel.STANDARD

    def apply_engine_gains(self) -> None:
        """把設定中的音量、直通與降噪套用到音訊引擎。"""
        muted = set(self.settings.mixer_muted)
        self.engine.monitor_bus.gain = 0.0 if MONITOR in muted else self.settings.monitor_volume
        # 混音器的「音效版」靜音只影響朋友聽到的，自己聽的不變
        self.engine.mic_bus.gain = 0.0 if SOUNDS in muted else self.settings.mic_volume
        self.engine.master_gain = 0.0 if MASTER in muted else self.settings.mixer_volumes.get(MASTER, 1.0)
        self.engine.passthrough_gain = self.settings.passthrough_volume
        self.engine.passthrough_enabled = self.settings.passthrough_enabled
        self.engine.denoise_level = self.denoise_level()

    def restart(self) -> None:
        """重新啟動程式（切換介面語言後使用）。

        新的程式帶 ``--restart`` 啟動，它會等這個程式結束、釋放單一執行個體的鎖之後才繼續。
        """
        args = [sys.executable, RESTART_FLAG] if is_frozen() else [sys.executable, "-m", "soundboard", RESTART_FLAG]
        # 單一檔案的 exe 從自己再啟動自己時，PyInstaller 預設把新程式當成「同一個程式的子行程」、
        # 共用目前的解壓縮資料夾；舊程式結束時會刪掉那個資料夾，新程式就壞了。
        # 這個環境變數讓新程式當成獨立的程式、自己解壓縮（PyInstaller 6.9 以上）
        env = dict(os.environ, PYINSTALLER_RESET_ENVIRONMENT="1")
        try:
            subprocess.Popen(args, close_fds=True, env=env)
        except OSError as exc:
            self.set_status(tr("無法重新啟動：{error}", error=exc), ERR)
            return
        self.on_close()

    def apply_mixer(self) -> None:
        """把混音器裡的程式清單、音量與靜音套用到程式聲音路由。"""
        s = self.settings
        gains = {k: v for k, v in s.mixer_volumes.items() if not k.startswith("@")}
        muted = {k for k in s.mixer_muted if not k.startswith("@")}
        self.app_router.configure(s.mixer_apps, gains, {app_key(k) for k in muted})

    # ================================================================== 音效庫
    def choose_folder(self) -> None:
        """讓使用者選擇音效資料夾。"""
        folder = filedialog.askdirectory(initialdir=self.settings.sound_dir or os.getcwd(),
                                         title=tr("選擇音效資料夾"))
        if folder:
            self.settings.sound_dir = folder
            self.save()
            self.rescan()

    def rescan(self) -> None:
        """重新掃描音效資料夾並重建按鈕。"""
        folder = Path(self.settings.sound_dir or os.getcwd())
        self.home.set_folder(str(folder))
        self.cache.clear()
        with self._gained_lock:
            self._gained.clear()
        self.items = scan_folder(folder, self.settings.display_names, self.settings.sound_volumes)
        self.items_by_key = {item.key: item for item in self.items}
        self.home.forget_missing_selection()
        self.settings_page.update_join_ui()
        self.home.rebuild_tiles()
        self.refresh_overlay()
        if self.hotkeys.has_bindings:
            self.hotkeys.apply()

        # 切換資料夾時讓舊的預載工作提早結束
        self._scan_generation += 1
        gen = self._scan_generation
        self._executor.submit(
            self.cache.preload, list(self.items),
            lambda: gen != self._scan_generation,
            lambda item, msg: self._messages.put(("error", msg)),
        )
        if not self.items:
            # 兩種情況各寫成完整的句子，翻譯時才能調整語序
            if find_ffmpeg():
                self.set_status(tr("資料夾中沒有音效檔"), WARN)
            else:
                self.set_status(tr("資料夾中沒有音效檔（未偵測到 ffmpeg，m4a、aac、wma 等格式無法顯示）"), WARN)
        else:
            self.set_status(tr("已載入 {count} 個音效", count=len(self.items)), OK)

    def after_items_changed(self) -> None:
        """音效的 key 或名稱變動後，重建所有以 key 索引的介面與快捷鍵。"""
        self.items.sort(key=lambda it: it.display_name.casefold())
        self.items_by_key = {it.key: it for it in self.items}
        # 按鈕的事件綁定記住了舊 key，所以整批重建而不是只改文字
        self.home.rebuild_tiles()
        self.refresh_overlay()
        self.hotkeys.apply()
        self.hotkeys.refresh_dialog()
        self.settings_page.update_join_ui()
        self.save()

    # ================================================================== 進場音效
    def _on_voice_result(self, in_use: bool | None) -> None:
        """處理一次 Discord 語音偵測結果（主執行緒）。"""
        previous = self._voice.in_voice
        joined = self._voice.update(in_use, time.monotonic())
        if self._voice.in_voice != previous:
            logger.info("Discord 語音狀態：%s", {True: "通話中", False: "未在語音中", None: "無法偵測"}[self._voice.in_voice])
            self.settings_page.update_join_ui()
            self._update_voice_chip()
        if joined:
            logger.info("偵測到進入 Discord 語音")
            if self.settings.join_sound_enabled and self.settings.join_sound_key in self.items_by_key:
                delay_ms = int(max(0.0, self.settings.join_sound_delay) * 1000)
                self.root.after(delay_ms, lambda: self.play_join_sound(auto=True))

    def play_join_sound(self, auto: bool = False) -> None:
        """播放進場音效（設定頁「▶ 試播」或偵測到進入語音時）。

        Args:
            auto (bool): 是否為自動偵測觸發（狀態列顯示不同訊息）。
        """
        item = self.items_by_key.get(self.settings.join_sound_key or "")
        if item is None:
            self.set_status(tr("尚未設定進場音效。請在首頁的音效上按右鍵，選擇「🚪 設為進場音效」"), WARN)
            return
        # 進場音效就是要給大家聽的，所以不受預覽模式影響，一律送進虛擬麥克風
        self.play(item, to_mic=True)
        if auto:
            self.set_status(tr("🚪 偵測到已進入 Discord 語音，正在播放進場音效「{name}」", name=item.display_name), OK)

    # ================================================================== 預覽模式
    @property
    def preview_mode(self) -> bool:
        """預覽模式是否開啟（開啟時左鍵只播給自己）。"""
        return self._preview_flag

    def toggle_preview(self) -> None:
        """切換預覽模式。"""
        self._preview_flag = not self._preview_flag
        self._update_preview_ui()
        if self.preview_mode:
            self.set_status(tr("🎧 已開啟試聽模式：按一下音效僅在本機播放，通話對象聽不到（按 Ctrl+P 關閉）"),
                            PREVIEW_HOVER)
        else:
            self.set_status(tr("已關閉試聽模式：按一下音效會傳送給通話對象"), FG_MUTED)

    def _update_preview_ui(self) -> None:
        self.home.set_preview(self.preview_mode)
        self._set_chip(self._preview_chip, tr("試聽模式：僅本機監聽"), PREVIEW_HOVER if self.preview_mode else None)
        self.overlay.set_preview(self.preview_mode)

    # ================================================================== 播放
    def play(self, item: SoundItem, to_mic: bool = True) -> None:
        """播放音效。

        Args:
            item (SoundItem): 要播放的音效。
            to_mic (bool): 是否送進虛擬麥克風；False 為只有自己聽（試聽）。
        """
        text, color = self.trigger(item, to_mic)
        self.set_status(text, color)

    def trigger(self, item: SoundItem, to_mic: bool) -> tuple[str, str]:
        """開始播放（不碰任何 Tk 元件，快捷鍵執行緒也能安全呼叫）。

        Args:
            item (SoundItem): 要播放的音效。
            to_mic (bool): 是否送進虛擬麥克風。

        Returns:
            tuple[str, str]: 要顯示在狀態列的（文字, 顏色）。
        """
        if not to_mic and not self.engine.monitor_active:
            # tr 只查字典，快捷鍵執行緒呼叫也安全
            return tr("⚠ 尚未設定監聽裝置，無法試聽。請在「⚙ 設定」的「🎧 監聽裝置」選擇耳機或喇叭"), WARN
        if not self.settings.allow_overlap:
            self.engine.stop()
        clip = self.cache.peek(item.path)
        if clip is not None:
            self.engine.play(item.key, self.with_volume(item, clip), to_mic=to_mic)
        else:
            # 尚未預載完成：改到背景解碼，避免大檔案讓介面卡住
            self._executor.submit(self._load_and_play, item, to_mic)
        if to_mic and self.engine.virtual_mic_active:
            return tr("▶ {name}（通話對象＋監聽）", name=item.display_name), FG_MUTED
        return tr("🎧 {name}（試聽：僅本機）", name=item.display_name), PREVIEW_HOVER

    def _load_and_play(self, item: SoundItem, to_mic: bool) -> None:
        try:
            clip = self.cache.get(item.path)
        except DecodeError as exc:
            # 解碼失敗一定要讓使用者看到原因，不然只會覺得「按了沒反應」
            self._messages.put(("error", str(exc)))
            return
        self.engine.play(item.key, self.with_volume(item, clip), to_mic=to_mic)

    def with_volume(self, item: SoundItem, clip: np.ndarray) -> np.ndarray:
        """套用此音效的個別音量（±80 dB）。

        放大後超過滿刻度的峰值用和編輯器相同的峰值限制器處理，聽起來不會破音，也和編輯器試聽一致。
        結果會快取，連按同一個音效不必重算。

        Args:
            item (SoundItem): 音效。
            clip (np.ndarray): 解碼後的原始音訊。

        Returns:
            np.ndarray: 要播放的音訊。
        """
        if item.volume == 1.0:
            return clip
        key = (str(item.path), item.volume, id(clip))
        with self._gained_lock:
            cached = self._gained.get(key)
            if cached is not None:
                self._gained.move_to_end(key)
                return cached
        rendered = render_gain(clip, item.volume)
        with self._gained_lock:
            self._gained[key] = rendered
            while len(self._gained) > GAINED_CACHE_SIZE:
                self._gained.popitem(last=False)
        return rendered

    def open_sound_editor(self, item: SoundItem) -> None:
        """開啟音效編輯器（調音量、修剪、後製、另存）。"""
        for editor in self.sound_editors:
            if editor.alive and editor.sound is not None and editor.sound.key == item.key:
                editor.focus()
                return
        try:
            editor = ClipEditor(self, item.path, sound=item)
        except (OSError, RuntimeError, DecodeError) as exc:
            self.set_status(tr("無法開啟音效：{error}", error=exc), ERR)
            return
        self.sound_editors = [e for e in self.sound_editors if e.alive] + [editor]

    def stop_all(self) -> None:
        """停止所有音效。"""
        self.engine.stop()
        self.set_status(tr("⏹ 已停止所有音效"), FG_MUTED)

    # ================================================================== 小視窗
    def toggle_overlay(self) -> None:
        """顯示或隱藏螢幕角落的小視窗。"""
        if self.overlay.visible:
            self.overlay.hide()
            self.settings.overlay_visible = False
        else:
            self.overlay.show()
            self.settings.overlay_visible = True
        self.home.update_overlay_button(self.settings.overlay_visible)
        self.save()

    def _on_overlay_hidden(self) -> None:
        self.settings.overlay_visible = False
        self.home.update_overlay_button(False)
        self.save()

    def refresh_overlay(self) -> None:
        """依目前的釘選或群組重新提供小視窗要顯示的音效。"""
        group = self.settings.overlay_group
        if group is not None and group not in self.settings.sound_groups:
            # 群組被刪掉或改名（外部編輯設定檔）時退回釘選清單
            self.settings.overlay_group = group = None
        keys = self.settings.overlay_keys if group is None else self.settings.sound_groups[group]
        shown = [self.items_by_key[k] for k in keys if k in self.items_by_key]
        self.overlay.set_items(shown, {it.key: self.hotkeys.text_for_sound(it.key) for it in shown})
        self.overlay.set_playing(self.playing)
        self.overlay.set_preview(self.preview_mode)

    def _on_overlay_group_changed(self) -> None:
        self.refresh_overlay()
        self.save()

    def _cycle_overlay_group(self) -> None:
        """快捷鍵：小視窗切到下一個群組（小視窗隱藏時順便顯示出來）。"""
        if not self.settings.sound_groups:
            self.set_status(tr("尚未建立群組，無法切換浮動視窗顯示的群組"), WARN)
            return
        group = self.settings.cycle_overlay_group(1)
        self._on_overlay_group_changed()
        if not self.overlay.visible:
            self.toggle_overlay()
        if group is None:
            self.set_status(tr("⌨ 浮動視窗：📌 釘選"), FG)
        else:
            self.set_status(tr("⌨ 浮動視窗：🗂 {group}", group=group), FG)

    # ================================================================== 其他
    def open_mic_test(self) -> None:
        """開啟麥克風測試視窗（命令列 ``--mic-test`` 使用）。"""
        self.settings_page.open_mic_test()

    def _on_delete_key(self, event: tk.Event) -> None:
        # 只在首頁、有複選音效、而且不是在輸入框裡打字時才刪除（搜尋框裡按 Delete 是刪字）
        if self.current_page != "home" or isinstance(event.widget, (tk.Entry, tk.Text)):
            return
        keys = self.home.selected_keys()
        if keys:
            self.editor.delete_sounds(keys)

    def _on_mousewheel(self, event: tk.Event) -> None:
        # 只有游標在可捲動的區域時才捲動，避免在下拉選單等元件上滾輪時畫面亂跳
        try:
            widget = self.root.winfo_containing(event.x_root, event.y_root)
        except KeyError:
            # 游標在 ttk 下拉選單的清單上：那是 Tk 內部視窗，tkinter 找不到對應物件
            return
        for area in (self.home.scroll, self.replay_page.scroll, self.mixer_page.scroll,
                     self.settings_page.scroll):
            if area.contains(widget):
                area.scroll(int(-event.delta / 120))
                return

    def _poll(self) -> None:
        # 定期更新「播放中」的按鈕顏色，並處理背景執行緒送來的訊息
        to_friends = self.engine.mic_bus.active_keys()
        playing = {key: (BTN_PLAYING if key in to_friends else PREVIEW)
                   for key in self.engine.monitor_bus.active_keys() | to_friends}
        if playing != self.playing:
            self.home.set_playing(playing, self.playing)
            self.playing = playing
            self.overlay.set_playing(playing)
        self.settings_page.update_gate(self.engine.voice_gate_open)
        try:
            while True:
                kind, msg = self._messages.get_nowait()
                if kind == "error":
                    self.set_status(msg, ERR)
                elif kind == "status":
                    color, _, text = msg.partition("|")
                    self.set_status(text, color)
                elif kind == "action" and msg == "overlay":
                    self.toggle_overlay()
                elif kind == "action" and msg == "preview":
                    self.toggle_preview()
                elif kind == "action" and msg == "overlay_group":
                    self._cycle_overlay_group()
                elif kind == "action" and msg == "replay_capture":
                    self.replay_page.capture()
                elif kind == "action" and msg == "show_window":
                    self.show_window()
                elif kind == "action" and msg == "stop":
                    self.stop_all()
                elif kind == "action" and msg == "quit":
                    self.on_close()
                    return      # 視窗已銷毀，不能再排下一次輪詢
                elif kind == "voice":
                    self._on_voice_result(None if msg == "?" else msg == "1")
        except queue.Empty:
            pass
        self._poll_job = self.root.after(POLL_MS, self._poll)

    def set_status(self, text: str, color: str = FG_MUTED) -> None:
        """在狀態列顯示訊息。

        Args:
            text (str): 訊息。
            color (str): 文字顏色。
        """
        self.status.configure(text=text, fg=color)

    def save(self) -> None:
        """儲存設定（失敗時顯示在狀態列）。"""
        try:
            save_settings(self.settings)
        except OSError as exc:
            self.set_status(tr("無法儲存設定：{error}", error=exc), ERR)

    # ================================================================== 系統匣與背景執行
    def _start_tray(self) -> TrayIcon | None:
        icon = resource_path("soundboard.ico")
        tray = TrayIcon(instance_id(settings_path()), icon if icon.exists() else None,
                        on_activate=lambda: self._messages.put(("action", "show_window")),
                        menu=self._tray_menu)
        return tray if tray.start() else None

    def _tray_menu(self) -> list[TrayMenuItem]:
        # 在系統匣執行緒裡被呼叫：只讀一般的 Python 屬性，動作一律交給主執行緒的 _poll 處理
        def post(action: str) -> Callable[[], None]:
            return lambda: self._messages.put(("action", action))

        return [
            TrayMenuItem(tr("顯示音效版"), post("show_window"), default=True),
            TrayMenuItem(tr("浮動視窗"), post("overlay"), checked=self.settings.overlay_visible),
            TrayMenuItem(tr("試聽模式（僅本機監聽）"), post("preview"), checked=self._preview_flag),
            TrayMenuItem(tr("全部停止"), post("stop")),
            SEPARATOR,
            TrayMenuItem(tr("結束音效版"), post("quit")),
        ]

    @property
    def in_background(self) -> bool:
        """按 ✕ 時是否會縮到系統匣（而不是結束）。"""
        return self.settings.close_to_tray and self.tray is not None and self.tray.running

    def close_window(self) -> None:
        """按下主視窗的 ✕：預設縮到系統匣繼續執行；關掉這個選項或沒有系統匣圖示時直接結束。"""
        if self.in_background:
            self.hide_window()
        else:
            self.on_close()

    def hide_window(self) -> None:
        """隱藏主視窗，程式留在系統匣（快捷鍵、小視窗、進場音效照常運作）。"""
        self.root.withdraw()
        self.window_visible = False
        if not self.settings.tray_hint_shown and self.tray is not None:
            # 只提示一次：第一次按 ✕ 的人通常以為程式關掉了，之後找不到為什麼快捷鍵還有反應
            self.tray.notify(tr("音效版仍在背景執行"),
                             tr("快速鍵、浮動視窗與進場音效仍可使用。按一下通知區域的圖示即可開啟視窗；"
                                "若要結束程式，請在圖示上按右鍵並選擇「結束音效版」。"))
            self.settings.tray_hint_shown = True
        self.save()

    def show_window(self) -> None:
        """把主視窗叫回來並放到最前面（點系統匣圖示、或再開一次 exe 時）。"""
        root = self.root
        root.deiconify()
        if root.state() == "iconic":
            root.state("normal")
        root.lift()
        # 只呼叫 lift 時 Windows 常常只讓工作列按鈕閃爍；暫時置頂才會確實跳到最前面
        root.attributes("-topmost", True)
        root.after(150, lambda: root.winfo_exists() and root.attributes("-topmost", False))
        root.focus_force()
        self.window_visible = True

    def on_close(self) -> None:
        """完全結束程式：儲存設定、移除系統匣圖示並釋放音訊裝置。"""
        self._scan_generation += 1
        self.root.after_cancel(self._poll_job)
        self.save()
        self.hotkeys.stop()
        self._voice_watcher.stop()
        self.replay_page.shutdown()
        self.app_router.stop()
        mic_test = self.settings_page.mic_test
        if mic_test is not None:
            mic_test.close()
        self.engine.close()
        if self.tray is not None:
            self.tray.stop()
        self._executor.shutdown(wait=False, cancel_futures=True)
        self.root.destroy()


def run(settings: AppSettings, open_mic_test: bool = False, start_hidden: bool = False) -> None:
    """建立視窗並進入主迴圈。

    Args:
        settings (AppSettings): 使用者設定。
        open_mic_test (bool): 啟動後直接開啟麥克風測試視窗。
        start_hidden (bool): 不顯示主視窗，只放在系統匣（開機自動啟動時）。
    """
    if sys.platform == "win32":
        # 開啟 DPI 感知，高解析度螢幕上文字才不會模糊
        try:
            import ctypes

            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            pass
    root = tk.Tk()
    if start_hidden:
        root.withdraw()     # 在建立任何元件前就隱藏，開機時才不會閃一下主視窗
    icon = resource_path("soundboard.ico")
    if icon.exists():
        try:
            root.iconbitmap(default=str(icon))
        except tk.TclError:
            pass  # 圖示只是裝飾，載入失敗不影響功能
    # exe 版沒有主控台：介面事件中的例外改寫到紀錄檔，否則會無聲無息地消失
    root.report_callback_exception = lambda typ, val, tb: logger.error(  # type: ignore[method-assign]
        "介面事件發生錯誤", exc_info=(typ, val, tb))
    try:
        app = SoundboardApp(root, settings)
    except Exception as exc:  # noqa: BLE001 - 啟動失敗時至少讓使用者看到原因
        logger.exception("啟動失敗")
        messagebox.showerror(tr("音效版啟動失敗"), str(exc))
        root.destroy()
        raise
    if start_hidden:
        if app.tray is None:
            root.deiconify()    # 沒有系統匣圖示就沒有地方叫出視窗，只好直接顯示
        else:
            app.window_visible = False
            logger.info("開機自動啟動：在系統匣背景執行")
    if open_mic_test:
        root.after(500, app.open_mic_test)
    root.mainloop()
