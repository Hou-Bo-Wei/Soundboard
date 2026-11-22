"""片段／音效編輯器：選要哪些來源、各軌音量／降噪、修剪範圍、淡入淡出，然後存成新音效或另存。

有兩種用法：

* 回放片段（多音軌）：每個錄音來源一軌；
* 音效版上的音效（單一音軌，``sound=``）：音量範圍放寬到 ±80 dB，可以把音量直接套用到該音效，
  也可以只留中間一段、加淡入淡出後存成新音效或另存檔案。

每個來源（例如 Discord、YouTube、我的麥克風）是獨立的一軌，各自一條波形：

* 開關：這一軌要不要放進成品；
* 音量：-24～+30 dB，開啟時已依每軌的大小自動放大（回放錄到的聲音常比平常聽到的小 20 dB 以上）；
* 降噪：用 RNNoise 去掉該軌的鍵盤、風扇聲（只處理選取的範圍，在背景計算）。
"""

from __future__ import annotations

import logging
import os
import queue
import time
import tkinter as tk
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, ttk
from typing import TYPE_CHECKING

import numpy as np

from soundboard.clips import (
    COMPRESS_WARN_LEVEL, EXPORT_FORMATS, MAX_BOOST_DB, MIN_BOOST_DB, SOUND_MAX_DB, SOUND_MIN_DB, auto_gain_db,
    denoise_stereo, export_audio, fade_edges, gain_factor, limited_ratio, load_clip, mix, track_label, trim,
    unique_sound_path, volume_to_db, waveform_peaks,
)
from soundboard.gui.styles import FONT_BODY, FONT_BOLD, FONT_H2, FONT_SMALL
from soundboard.gui.widgets import RoundedButton, ToggleSwitch
from soundboard.i18n import N_, tr
from soundboard.library import validate_file_stem
from soundboard.replay import SAMPLE_RATE
from soundboard.theme import (
    ACCENT, ACCENT_HOVER, BG_DARK, BTN, BTN_HOVER, ERR, FG, FG_MUTED, FG_SUBTLE, INPUT, OK, SURFACE, WARN,
)

if TYPE_CHECKING:
    from soundboard.gui.app import SoundboardApp
    from soundboard.models import SoundItem

logger = logging.getLogger(__name__)

WAVE_WIDTH = 880             # 100% 縮放時的波形寬度
RULER_HEIGHT = 22           # 上方時間尺：在上面點或拖曳都是移動播放頭，做高一點比較好抓
LANE_HEIGHTS = {1: 150, 2: 80}   # 依音軌數決定每軌高度；3 軌以上用 LANE_HEIGHT_MANY
LANE_HEIGHT_MANY = 60
EDGE_GRAB_PX = 7             # 滑鼠離選取邊界這麼近時，拖曳會移動該邊界
PLAYHEAD_GRAB_PX = 9         # 滑鼠離播放頭（黃線）這麼近時可以抓住拖曳；黃線只有 2 px 寬，不放寬很難點中
CLICK_SLOP_PX = 4            # 按下後移動不到這麼多就算「點一下」（移動播放頭），超過才算拖曳平移畫面
MIN_VIEW_SECONDS = 0.2
ZOOM_STEP = 0.8              # Ctrl＋滾輪每一格把畫面縮成 80%（往回滾放大 1.25 倍）
MAX_PEAK_CACHE = 300
PLAY_KEY = "replay:editor"
# 「不分組」只是下拉選單的一個選項，不會存進設定檔；顯示與比對都用 tr(NO_GROUP)
NO_GROUP = N_("（不分組）")
TRACK_COLORS = ("#7a8cff", "#f0b232", "#3ba55d", "#eb459e", "#00a8fc", "#e67e22")
DISABLED_WAVE = "#4a4d55"
SELECTION_BG = "#353a63"
TICK_MS = 30
JOB_POLL_MS = 100
FADE_SECONDS: tuple[float, ...] = (0.0, 0.1, 0.3, 0.5, 1.0, 2.0)


def fade_label(seconds: float) -> str:
    """淡入淡出選項的顯示文字。

    Args:
        seconds (float): 淡化秒數；0 表示不淡化。

    Returns:
        str: 例如「無」、「0.5 秒」。
    """
    return tr("無") if seconds == 0 else tr("{seconds} 秒", seconds=f"{seconds:g}")


def format_seconds(value: float) -> str:
    """把秒數顯示成「分:秒.百分秒」。

    Args:
        value (float): 秒數。

    Returns:
        str: 例如 ``1:05.25``。
    """
    value = max(0.0, value)
    minutes, seconds = divmod(value, 60)
    return f"{int(minutes)}:{seconds:05.2f}"


def parse_seconds(text: str) -> float:
    """解析使用者輸入的時間（``65.2`` 或 ``1:05.2``）。

    Args:
        text (str): 輸入文字。

    Returns:
        float: 秒數。

    Raises:
        ValueError: 格式不正確時拋出。
    """
    text = text.strip()
    if ":" in text:
        minutes, _, seconds = text.partition(":")
        return int(minutes) * 60 + float(seconds)
    return float(text)


def format_factor(gain_db: float) -> str:
    """增益對應的倍率文字，例如 +28 dB → ``25``、-40 dB → ``0.01``。"""
    factor = gain_factor(gain_db)
    if factor >= 10:
        return f"{factor:,.0f}"
    if factor >= 0.1:
        return f"{factor:.1f}"
    return f"{factor:.2g}"


def format_gain(gain_db: float) -> str:
    """增益顯示文字，例如 ``+28.0 dB（×25）``。"""
    return f"{gain_db:+.1f} dB（×{format_factor(gain_db)}）"


@dataclass
class EditTrack:
    """編輯器中的一軌。

    Attributes:
        source (str): 來源代號。
        audio (np.ndarray): 整段 (frames, 2) float32。
        color (str): 波形顏色。
        enabled (bool): 是否放進成品。
        gain_db (float): 音量（dB）。
        denoise (bool): 是否降噪。
    """

    source: str
    audio: np.ndarray
    color: str
    enabled: bool = True
    gain_db: float = 0.0
    denoise: bool = False
    auto_db: float = 0.0
    name: str = ""
    widgets: dict[str, object] = field(default_factory=dict)

    @property
    def label(self) -> str:
        """顯示名稱（有指定 ``name`` 時用它，例如音效的名稱）。"""
        return self.name or track_label(self.source)


class ClipEditor:
    """片段編輯視窗。

    Args:
        app (SoundboardApp): 主程式（用來試聽、存成音效、重新整理音效清單）。
        path (Path): 片段資料夾（或舊版單一檔案）；編輯音效時為音效檔。
        sound (SoundItem | None): 要編輯的音效版音效；None 表示編輯回放片段。

    Raises:
        OSError: 片段沒有音軌或長度為 0。
        DecodeError: 音效檔無法解碼。
    """

    def __init__(self, app: SoundboardApp, path: Path, *, sound: SoundItem | None = None) -> None:
        self.app = app
        self.path = path
        self.sound = sound
        self.tracks: list[EditTrack] = []
        if sound is not None:
            # 音效版上的音效：音量範圍放寬（下載來的音效大小差很多），預設用它目前的個別音量
            loaded = [(sound.key, app.cache.get(sound.path))]
            self.min_db, self.max_db = SOUND_MIN_DB, SOUND_MAX_DB
        else:
            # 回放片段：錄到的聲音通常很小聲，開啟時每一軌自動放大
            loaded = load_clip(path)
            self.min_db, self.max_db = MIN_BOOST_DB, MAX_BOOST_DB
        for i, (source, audio) in enumerate(loaded):
            auto = auto_gain_db(audio)
            gain = volume_to_db(sound.volume) if sound is not None else auto
            self.tracks.append(EditTrack(source, audio, TRACK_COLORS[i % len(TRACK_COLORS)], gain_db=gain,
                                         auto_db=auto, name=sound.display_name if sound is not None else ""))
        if not self.tracks or len(self.tracks[0].audio) == 0:
            raise OSError(tr("沒有可編輯的音訊"))
        self.duration = len(self.tracks[0].audio) / SAMPLE_RATE
        self.sel = (0.0, self.duration)
        self.view = (0.0, self.duration)
        # 滑鼠拖曳中的動作："playhead"（拖播放頭）、"start"／"end"（拖選取邊界）、
        # "new"（Shift＋拖曳重新框選）、"pending"（還不知道是點一下或拖曳）、"pan"（平移畫面）
        self._drag: str | None = None
        self._drag_anchor = 0.0
        self._pan_view = (0.0, 0.0)
        self._resume_after_scrub = False
        self._play_started: float | None = None
        self._play_origin = 0.0                # 這次播放從片段的第幾秒開始
        self._play_end = 0.0                   # 播到第幾秒停
        self._press_x = 0
        # 播放頭（像剪輯軟體的游標）：點一下波形移到該處，播放從這裡開始；暫停時停在播到的位置
        self.cursor = 0.0
        self._job: str | None = None
        self._peaks: dict[tuple[int, float, float, int], np.ndarray] = {}
        # 降噪結果快取：(第幾軌, 起點取樣, 終點取樣, 強) → 降噪後的選取範圍
        self._denoised: dict[tuple[int, int, int, bool], np.ndarray] = {}
        self._jobs: queue.Queue[tuple[str, object]] = queue.Queue()
        self._pending: Callable[[], None] | None = None
        self._busy = False
        self._saving = False                   # 背景存檔中（避免連按兩次存出兩個檔）

        m = app.metrics
        n = len(self.tracks)
        self.width = m.px(WAVE_WIDTH)
        self.ruler = m.px(RULER_HEIGHT)
        self.lane = m.px(LANE_HEIGHTS.get(n, LANE_HEIGHT_MANY))
        self.height = self.ruler + self.lane * n
        self.top = tk.Toplevel(app.root, bg=SURFACE, padx=m.px(18), pady=m.px(14))
        self.top.title(tr("✂ 編輯音效 — {name}", name=sound.display_name) if sound is not None
                       else tr("✂ 編輯片段 — {name}", name=path.stem))
        self.top.resizable(False, False)
        self.top.protocol("WM_DELETE_WINDOW", self.close)
        self._build()
        self._redraw()
        self._sync_fields()

    # ================================================================== 介面
    def _btn(self, parent: tk.Misc, text: str, command: Callable[[], object], bg: str = BTN,
             hover: str = BTN_HOVER) -> RoundedButton:
        m = self.app.metrics
        return RoundedButton(parent, text, command, surface=SURFACE, bg=bg, hover=hover, radius=m.px(8),
                             padx=m.px(12), pady=m.px(6))

    def _build(self) -> None:
        m = self.app.metrics
        top = self.top
        title = self.sound.display_name if self.sound is not None else self.path.stem
        tk.Label(top, text=f"✂ {title}", font=FONT_H2, bg=SURFACE, fg=FG, anchor="w").pack(fill="x")
        length = format_seconds(self.duration)
        info = (tr("總長 {length}・音效", length=length) if self.sound is not None
                else tr("總長 {length}・{count} 軌", length=length, count=len(self.tracks)))
        usage = tr("按一下波形或上方時間尺可移動播放頭（黃線），黃線可直接拖曳；拖曳藍色邊線可調整保留範圍，"
                   "Shift＋拖曳可重新選取；Ctrl＋滾輪可縮放，拖曳空白處可平移畫面。")
        tk.Label(top, text=f"{info}　{usage}",
                 font=FONT_SMALL, bg=SURFACE, fg=FG_SUBTLE, anchor="w").pack(fill="x", pady=(m.px(2), m.px(10)))

        self.canvas = tk.Canvas(top, width=self.width, height=self.height, bg=BG_DARK, highlightthickness=0,
                                cursor="arrow")
        self.canvas.pack()
        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<Shift-ButtonPress-1>", lambda e: self._on_press(e, shift=True))
        self.canvas.bind("<B1-Motion>", self._on_motion)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<Motion>", self._on_hover)
        self.canvas.bind("<Control-MouseWheel>", self._on_zoom_wheel)
        self.canvas.bind("<MouseWheel>", self._on_pan_wheel)

        row = tk.Frame(top, bg=SURFACE)
        row.pack(fill="x", pady=(m.px(10), 0))
        self.start_var, self.end_var = tk.StringVar(), tk.StringVar()
        for label, var in ((tr("起點"), self.start_var), (tr("終點"), self.end_var)):
            tk.Label(row, text=label, font=FONT_BODY, bg=SURFACE, fg=FG).pack(side="left", padx=(0, m.px(6)))
            entry = ttk.Entry(row, textvariable=var, width=9, font=FONT_BODY)
            entry.pack(side="left", padx=(0, m.px(12)))
            entry.bind("<Return>", lambda _e: self._apply_fields())
            entry.bind("<FocusOut>", lambda _e: self._apply_fields())
        self.length_label = tk.Label(row, font=FONT_BODY, bg=SURFACE, fg=FG_MUTED)
        self.length_label.pack(side="left")
        self._btn(row, tr("↔ 顯示全部"), self.zoom_all).pack(side="right", padx=(m.px(6), 0))
        self.view_label = tk.Label(row, font=FONT_SMALL, bg=SURFACE, fg=FG_SUBTLE)
        self.view_label.pack(side="right", padx=(0, m.px(4)))

        self._build_tracks(top)
        self._build_post(top)

        row2 = tk.Frame(top, bg=SURFACE)
        row2.pack(fill="x", pady=(m.px(12), 0))
        self.play_btn = self._btn(row2, tr("▶ 播放（空白鍵）"), self.toggle_play, ACCENT, ACCENT_HOVER)
        self.play_btn.pack(side="left")
        self._btn(row2, tr("⏮ 從選取範圍開頭試聽"), self.play_selection).pack(side="left", padx=(m.px(6), 0))
        self.cursor_label = tk.Label(row2, font=FONT_BODY, bg=SURFACE, fg=WARN)
        self.cursor_label.pack(side="left", padx=(m.px(12), 0))
        self._btn(row2, tr("設為終點（O）⇥"), self.set_end_at_cursor).pack(side="right", padx=(m.px(6), 0))
        self._btn(row2, tr("⇤ 設為起點（I）"), self.set_start_at_cursor).pack(side="right")
        self.monitor_hint = tk.Label(top, font=FONT_SMALL, bg=SURFACE, fg=FG_SUBTLE, anchor="w")
        self.monitor_hint.pack(fill="x", pady=(m.px(4), 0))
        # 剪輯軟體的常用快捷鍵；焦點在輸入框時不攔截（才能正常打字）
        for key, action in (("<space>", self.toggle_play), ("<KeyPress-i>", self.set_start_at_cursor),
                            ("<KeyPress-o>", self.set_end_at_cursor), ("<Home>", lambda: self.set_cursor(0.0))):
            self.top.bind(key, lambda e, a=action: self._on_key(e, a))
        if not self.app.engine.monitor_active:
            self.monitor_hint.configure(text=tr("⚠ 尚未在設定頁選擇監聽裝置，試聽時不會有聲音"), fg=WARN)
        else:
            self.monitor_hint.configure(text=tr("試聽僅在本機播放，通話對象聽不到。在選取範圍內播放的聲音即為儲存後的結果；"
                                                "播放時按 I／O 可快速設定起點與終點"))

        tk.Frame(top, bg=INPUT, height=1).pack(fill="x", pady=m.px(14))
        row3 = tk.Frame(top, bg=SURFACE)
        row3.pack(fill="x")
        tk.Label(row3, text=tr("音效名稱"), font=FONT_BODY, bg=SURFACE, fg=FG).pack(side="left", padx=(0, m.px(6)))
        default_name = (tr("{name} 剪輯", name=self.sound.display_name) if self.sound is not None
                        else tr("回放 {time}", time=f"{datetime.now():%m%d-%H%M%S}"))
        self.name_var = tk.StringVar(value=default_name)
        ttk.Entry(row3, textvariable=self.name_var, width=24, font=FONT_BODY).pack(side="left")
        tk.Label(row3, text=tr("群組"), font=FONT_BODY, bg=SURFACE, fg=FG).pack(side="left", padx=(m.px(14), m.px(6)))
        group = self.app.settings.group_of(self.sound.key) if self.sound is not None else None
        self.group_var = tk.StringVar(value=group or tr(NO_GROUP))
        ttk.Combobox(row3, textvariable=self.group_var, values=[tr(NO_GROUP), *self.app.settings.sound_groups],
                     state="readonly", width=14, font=FONT_BODY).pack(side="left")
        self._btn(row3, tr("⬇ 另存檔案…"), self.export_dialog).pack(side="right", padx=(m.px(6), 0))
        self._btn(row3, tr("➕ 儲存為新音效"), self.save_as_sound, ACCENT, ACCENT_HOVER).pack(side="right")
        if self.sound is not None:
            row4 = tk.Frame(top, bg=SURFACE)
            row4.pack(fill="x", pady=(m.px(10), 0))
            self._btn(row4, tr("💾 將音量套用至此音效"), self.apply_volume, ACCENT, ACCENT_HOVER).pack(side="left")
            tk.Label(row4, text=tr("僅變更此音效播放時的音量，不修改原始檔案；修剪與淡入淡出請使用「儲存為新音效」或「另存檔案」"),
                     font=FONT_SMALL, bg=SURFACE, fg=FG_SUBTLE).pack(side="left", padx=(m.px(10), 0))

        self.status = tk.Label(top, font=FONT_SMALL, bg=SURFACE, fg=FG_MUTED, anchor="w")
        self.status.pack(fill="x", pady=(m.px(10), 0))

    def _build_tracks(self, parent: tk.Misc) -> None:
        m = self.app.metrics
        box = tk.Frame(parent, bg=SURFACE)
        box.pack(fill="x", pady=(m.px(12), 0))
        tk.Label(box, text=tr("🎚 音軌"), font=FONT_BOLD, bg=SURFACE, fg=FG, anchor="w").grid(
            row=0, column=0, columnspan=7, sticky="w", pady=(0, m.px(4)))
        for i, track in enumerate(self.tracks, start=1):
            enabled = tk.BooleanVar(value=track.enabled)
            if self.sound is None:
                switch = ToggleSwitch(box, enabled, lambda t=track: self._on_track_toggled(t), surface=SURFACE,
                                      metrics=m)
                switch.grid(row=i, column=0, sticky="w", pady=m.px(3))
            # 編輯單一音效時只有一軌，名稱已經在標題上，開關也沒有意義
            text = f"● {track.label}" if self.sound is None else tr("🔊 音量")
            name = tk.Label(box, text=text, font=FONT_BODY, bg=SURFACE, fg=track.color, anchor="w", width=12)
            name.grid(row=i, column=1, sticky="w", padx=(m.px(8), m.px(8)))
            gain = tk.DoubleVar(value=track.gain_db)
            # 音效模式的範圍是 160 dB，滑桿做長一點才好微調
            length = m.px(360 if self.sound is not None else 240)
            scale = ttk.Scale(box, from_=self.min_db, to=self.max_db, variable=gain, length=length,
                              command=lambda _v, t=track: self._on_gain_drag(t))
            scale.grid(row=i, column=2, sticky="w")
            # 拖曳中只重畫波形；放開才重新試聽，否則每移動一點就重播一次
            scale.bind("<ButtonRelease-1>", lambda _e: self._restart_if_playing(), add="+")
            cell = tk.Frame(box, bg=SURFACE)
            cell.grid(row=i, column=3, sticky="w", padx=(m.px(8), 0))
            # 直接輸入 dB（例如 -12、+6.5），按 Enter 或離開欄位生效；上下箭頭每次 0.5 dB
            db_var = tk.StringVar()
            spin = ttk.Spinbox(cell, textvariable=db_var, from_=self.min_db, to=self.max_db, increment=0.5,
                               width=6, font=FONT_BODY, command=lambda t=track: self._apply_db_entry(t))
            spin.pack(side="left")
            spin.bind("<Return>", lambda _e, t=track: self._apply_db_entry(t))
            spin.bind("<FocusOut>", lambda _e, t=track: self._apply_db_entry(t))
            value = tk.Label(cell, font=FONT_BODY, bg=SURFACE, fg=FG, width=10, anchor="w")
            value.pack(side="left", padx=(m.px(4), 0))
            self._btn(box, tr("✨ 自動"), lambda t=track: self.auto_gain(t)).grid(row=i, column=4, padx=(m.px(4), 0))
            denoise = tk.BooleanVar(value=track.denoise)
            dn = tk.Frame(box, bg=SURFACE)
            dn.grid(row=i, column=5, sticky="w", padx=(m.px(14), 0))
            ToggleSwitch(dn, denoise, lambda t=track: self._on_denoise_toggled(t), surface=SURFACE,
                         metrics=m).pack(side="left")
            tk.Label(dn, text=tr("🔇 降噪"), font=FONT_SMALL, bg=SURFACE, fg=FG_MUTED).pack(side="left",
                                                                                   padx=(m.px(6), 0))
            warn = tk.Label(box, font=FONT_SMALL, bg=SURFACE, fg=WARN, anchor="w")
            warn.grid(row=i, column=6, sticky="w", padx=(m.px(10), 0))
            track.widgets = {"enabled": enabled, "gain": gain, "value": value, "denoise": denoise, "warn": warn,
                             "name": name, "db": db_var}
            self._update_track_ui(track)

    def _build_post(self, parent: tk.Misc) -> None:
        m = self.app.metrics
        row = tk.Frame(parent, bg=SURFACE)
        row.pack(fill="x", pady=(m.px(10), 0))
        tk.Label(row, text=tr("✨ 後製"), font=FONT_BOLD, bg=SURFACE, fg=FG).pack(side="left", padx=(0, m.px(12)))
        # 下拉選單顯示翻譯後的文字，選到的文字再對回秒數（見 selected_audio）
        self._fade_choices = {fade_label(seconds): seconds for seconds in FADE_SECONDS}
        none = fade_label(0.0)
        self.fade_in_var, self.fade_out_var = tk.StringVar(value=none), tk.StringVar(value=none)
        for label, var in ((tr("淡入"), self.fade_in_var), (tr("淡出"), self.fade_out_var)):
            tk.Label(row, text=label, font=FONT_BODY, bg=SURFACE, fg=FG).pack(side="left", padx=(0, m.px(6)))
            box = ttk.Combobox(row, textvariable=var, values=list(self._fade_choices), state="readonly", width=7,
                               font=FONT_BODY)
            box.pack(side="left", padx=(0, m.px(14)))
            box.bind("<<ComboboxSelected>>", lambda _e: self._restart_if_playing())
        tk.Label(row, text=tr("降噪強度"), font=FONT_BODY, bg=SURFACE, fg=FG).pack(side="left", padx=(0, m.px(6)))
        # (標準, 強)：判斷強度時比對選到的是哪一個，不依賴文字內容（翻譯後開頭不一定是「強」）
        self._denoise_levels = (tr("標準"), tr("強（無語音時靜音）"))
        self.denoise_level_var = tk.StringVar(value=self._denoise_levels[0])
        box = ttk.Combobox(row, textvariable=self.denoise_level_var, values=list(self._denoise_levels),
                           state="readonly", width=16, font=FONT_BODY)
        box.pack(side="left")
        box.bind("<<ComboboxSelected>>", lambda _e: self._restart_if_playing())

    @property
    def alive(self) -> bool:
        """視窗是否還開著。"""
        try:
            return bool(self.top.winfo_exists())
        except tk.TclError:
            return False

    def focus(self) -> None:
        """把視窗拉到最前面。"""
        self.top.deiconify()
        self.top.lift()
        self.top.focus_force()

    def close(self) -> None:
        """關閉視窗並停止試聽。"""
        self.stop_play()
        self.top.destroy()

    # ================================================================== 音軌控制
    def _update_track_ui(self, track: EditTrack) -> None:
        w = track.widgets
        w["value"].configure(text=f"dB（×{format_factor(track.gain_db)}）",  # type: ignore[attr-defined]
                             fg=FG if track.enabled else FG_SUBTLE)
        w["db"].set(f"{track.gain_db:+.1f}")  # type: ignore[attr-defined]
        w["name"].configure(fg=track.color if track.enabled else FG_SUBTLE)  # type: ignore[attr-defined]
        a, b = self._sel_samples()
        ratio = limited_ratio(track.audio[a:b], track.gain_db) if track.enabled else 0.0
        w["warn"].configure(text=tr("⚠ 音量過大，部分片段將被大幅壓縮") if ratio > 0.002 else "")  # type: ignore[attr-defined]

    def set_gain(self, track: EditTrack, gain_db: float) -> None:
        """設定某一軌的音量。

        Args:
            track (EditTrack): 音軌。
            gain_db (float): 增益（dB），會限制在可調範圍內。
        """
        track.gain_db = round(min(max(gain_db, self.min_db), self.max_db) * 2) / 2
        track.widgets["gain"].set(track.gain_db)  # type: ignore[attr-defined]
        self._update_track_ui(track)
        self._redraw()

    def _apply_db_entry(self, track: EditTrack) -> None:
        text = str(track.widgets["db"].get()).replace("dB", "").replace("＋", "+").replace("－", "-").strip()  # type: ignore[attr-defined]
        try:
            value = float(text)
        except ValueError:
            self._set_status(tr("音量請輸入 {min}～{max} dB 之間的數字", min=f"{self.min_db:+.0f}",
                                max=f"{self.max_db:+.0f}"), ERR)
            self._update_track_ui(track)
            return
        if value == track.gain_db:
            return
        self.set_gain(track, value)
        self._restart_if_playing()

    def _on_gain_drag(self, track: EditTrack) -> None:
        value = float(track.widgets["gain"].get())  # type: ignore[attr-defined]
        track.gain_db = round(value * 2) / 2
        self._update_track_ui(track)
        self._redraw()

    def auto_gain(self, track: EditTrack) -> None:
        """依目前選取範圍自動調整某一軌的音量，並重新試聽。"""
        a, b = self._sel_samples()
        self.set_gain(track, auto_gain_db(track.audio[a:b]))
        self._restart_if_playing()

    def _on_track_toggled(self, track: EditTrack) -> None:
        track.enabled = bool(track.widgets["enabled"].get())  # type: ignore[attr-defined]
        self._update_track_ui(track)
        self._redraw()
        self._restart_if_playing()

    def set_track_enabled(self, track: EditTrack, enabled: bool) -> None:
        """開關某一軌（測試與程式呼叫用）。"""
        track.widgets["enabled"].set(enabled)  # type: ignore[attr-defined]
        self._on_track_toggled(track)

    def _on_denoise_toggled(self, track: EditTrack) -> None:
        track.denoise = bool(track.widgets["denoise"].get())  # type: ignore[attr-defined]
        self._restart_if_playing()

    def set_track_denoise(self, track: EditTrack, enabled: bool) -> None:
        """開關某一軌的降噪（測試與程式呼叫用）。"""
        track.widgets["denoise"].set(enabled)  # type: ignore[attr-defined]
        self._on_denoise_toggled(track)

    # ================================================================== 座標換算
    def _x_to_time(self, x: float) -> float:
        start, end = self.view
        return min(max(start + (x / self.width) * (end - start), 0.0), self.duration)

    def _time_to_x(self, t: float) -> float:
        start, end = self.view
        return (t - start) / (end - start) * self.width

    def _sel_samples(self) -> tuple[int, int]:
        return int(round(self.sel[0] * SAMPLE_RATE)), int(round(self.sel[1] * SAMPLE_RATE))

    # ================================================================== 選取
    def set_selection(self, start: float, end: float) -> None:
        """設定選取範圍（會自動排序並限制在片段長度內）。

        Args:
            start (float): 起點（秒）。
            end (float): 終點（秒）。
        """
        start, end = sorted((min(max(start, 0.0), self.duration), min(max(end, 0.0), self.duration)))
        self.sel = (start, end)
        self._redraw()
        self._sync_fields()

    def _sync_fields(self) -> None:
        start, end = self.sel
        self.start_var.set(format_seconds(start))
        self.end_var.set(format_seconds(end))
        self.length_label.configure(text=tr("長度 {seconds} 秒", seconds=f"{end - start:.2f}"))
        if self._drag is None:
            for track in self.tracks:
                self._update_track_ui(track)

    def _apply_fields(self) -> None:
        try:
            self.set_selection(parse_seconds(self.start_var.get()), parse_seconds(self.end_var.get()))
        except ValueError:
            self._set_status(tr("時間格式不正確，請輸入如 12.5 或 1:05.2 的格式"), ERR)
            self._sync_fields()

    def _hit(self, x: float, y: float) -> str:
        """滑鼠按在哪裡：播放頭、選取邊界，或空白處（"pending"）。"""
        px = self._time_to_x(self.position)
        m = self.app.metrics
        # 時間尺（含播放頭的把手）整條都可以抓播放頭，和剪輯軟體一樣
        if y < self.ruler:
            return "playhead"
        candidates = []
        if abs(x - px) <= m.px(PLAYHEAD_GRAB_PX):
            candidates.append((abs(x - px), 1, "playhead"))
        for name, t in (("start", self.sel[0]), ("end", self.sel[1])):
            distance = abs(x - self._time_to_x(t))
            if distance <= m.px(EDGE_GRAB_PX):
                # 一樣近時優先抓邊界：播放頭剛好壓在邊界上（按 I／O 之後）仍可從時間尺抓播放頭
                candidates.append((distance, 0, name))
        return min(candidates)[2] if candidates else "pending"

    def _on_press(self, event: tk.Event, shift: bool = False) -> None:
        self._press_x = event.x
        hit = self._hit(event.x, event.y)
        if shift and hit == "pending":
            self._drag = "new"
            self._drag_anchor = self._x_to_time(event.x)
            return
        self._drag = hit
        if hit == "playhead":
            # 播放中也能拖：先暫停，放開後從新位置繼續播
            self._resume_after_scrub = self.playing
            if self.playing:
                self.stop_play()
            self.set_cursor(self._x_to_time(event.x))
        elif hit == "pending":
            self._pan_view = self.view

    def _on_release(self, event: tk.Event) -> None:
        drag, self._drag = self._drag, None
        if drag == "pending":
            # 點一下空白處（沒有拖動）：移動播放頭，選取範圍不變
            self.set_cursor(self._x_to_time(event.x))
        elif drag == "playhead" and self._resume_after_scrub:
            self._resume_after_scrub = False
            self._prepare(lambda: self._start_play(self.cursor))
        self._sync_fields()

    def _on_motion(self, event: tk.Event) -> None:
        if self._drag == "pending" and abs(event.x - self._press_x) >= CLICK_SLOP_PX:
            self._drag = "pan"
        t = self._x_to_time(event.x)
        if self._drag == "playhead":
            self.set_cursor(t)
        elif self._drag == "pan":
            start, end = self._pan_view
            shift = (event.x - self._press_x) / self.width * (end - start)
            self._set_view(start - shift, end - start)
        elif self._drag == "start":
            self.set_selection(t, self.sel[1])
            if t > self.sel[1] - 1e-9:   # 拖過頭時兩邊互換，繼續拖的是新的終點
                self._drag = "end"
        elif self._drag == "end":
            self.set_selection(self.sel[0], t)
            if t < self.sel[0] + 1e-9:
                self._drag = "start"
        elif self._drag == "new":
            self.set_selection(self._drag_anchor, t)

    def _on_hover(self, event: tk.Event) -> None:
        hit = self._hit(event.x, event.y)
        if hit == "playhead":
            cursor = "hand2"
        elif hit in ("start", "end"):
            cursor = "sb_h_double_arrow"
        else:
            cursor = "fleur" if self.zoomed else "arrow"
        self.canvas.configure(cursor=cursor)

    # ================================================================== 縮放與平移
    @property
    def zoomed(self) -> bool:
        """畫面是否只顯示片段的一部分。"""
        return self.view[1] - self.view[0] < self.duration - 1e-6

    def _set_view(self, start: float, span: float) -> None:
        span = min(max(span, min(MIN_VIEW_SECONDS, self.duration)), self.duration)
        start = min(max(start, 0.0), self.duration - span)
        if (start, start + span) != self.view:
            self.view = (start, start + span)
            self._redraw()

    def zoom(self, factor: float, anchor: float | None = None) -> None:
        """縮放畫面，``anchor`` 那一秒在畫面上的位置保持不動。

        Args:
            factor (float): 新的顯示長度 = 目前長度 × factor（小於 1 為放大）。
            anchor (float | None): 固定不動的時間點（秒）；None 表示畫面中央。
        """
        start, end = self.view
        span = end - start
        anchor = (start + end) / 2 if anchor is None else anchor
        new_span = min(max(span * factor, min(MIN_VIEW_SECONDS, self.duration)), self.duration)
        self._set_view(anchor - (anchor - start) / span * new_span, new_span)

    def _on_zoom_wheel(self, event: tk.Event) -> str:
        # 以滑鼠所在的時間點為中心縮放，往上滾放大
        self.zoom(ZOOM_STEP ** (event.delta / 120), self._x_to_time(event.x))
        return "break"

    def _on_pan_wheel(self, event: tk.Event) -> str:
        # 放大後一般滾輪左右捲動畫面（一格捲 15% 畫面）
        start, end = self.view
        self._set_view(start - (event.delta / 120) * (end - start) * 0.15, end - start)
        return "break"

    def zoom_all(self) -> None:
        """顯示整個片段。"""
        self._set_view(0.0, self.duration)

    # ================================================================== 繪圖
    def _track_peaks(self, index: int) -> np.ndarray:
        key = (index, self.view[0], self.view[1], self.width)
        if key not in self._peaks:
            if len(self._peaks) > MAX_PEAK_CACHE:   # 連續縮放會產生很多種畫面，舊的丟掉即可
                self._peaks.clear()
            a, b = (int(t * SAMPLE_RATE) for t in self.view)
            self._peaks[key] = waveform_peaks(self.tracks[index].audio[a:b], self.width)
        return self._peaks[key]

    def _redraw(self) -> None:
        c = self.canvas
        c.delete("all")
        x0, x1 = (self._time_to_x(t) for t in self.sel)
        c.create_rectangle(x0, 0, x1, self.height, fill=SELECTION_BG, outline="")
        for i, track in enumerate(self.tracks):
            top = self.ruler + i * self.lane
            mid = top + self.lane / 2
            half = self.lane / 2 - 3
            # 波形顯示的是套用音量後的大小，放大後一眼就看得出會不會太大聲
            scaled = self._track_peaks(i) * gain_factor(track.gain_db)
            heights = np.sqrt(np.minimum(scaled, 1.0)) * half   # 平方根壓縮：小聲的語音也看得到形狀
            color = track.color if track.enabled else DISABLED_WAVE
            for x, (height, value) in enumerate(zip(heights, scaled, strict=True)):
                if height >= 0.5:
                    # 紅色：超過限制器能自然處理的範圍，會被壓得很重
                    fill = ERR if track.enabled and value > COMPRESS_WARN_LEVEL else color
                    c.create_line(x, mid - height, x, mid + height, fill=fill)
            c.create_line(0, mid, self.width, mid, fill=INPUT)
            if i:
                c.create_line(0, top, self.width, top, fill=SURFACE)
            c.create_text(6, top + 3, text=track.label, anchor="nw", font=FONT_SMALL,
                          fill=track.color if track.enabled else FG_SUBTLE)
        self._draw_ruler()
        view_start, view_end = self.view
        if self.zoomed:
            text = tr("顯示範圍 {start}–{end}（Ctrl＋滾輪縮放）", start=format_seconds(view_start),
                      end=format_seconds(view_end))
        else:
            text = tr("（Ctrl＋滾輪縮放）")
        self.view_label.configure(text=text)
        for x in (x0, x1):
            c.create_line(x, 0, x, self.height, fill=ACCENT, width=2)
        self._draw_playhead()

    def _draw_ruler(self) -> None:
        start, end = self.view
        span = end - start
        step = next((s for s in (0.1, 0.25, 0.5, 1, 2, 5, 10, 15, 30, 60) if span / s <= 12), 120)
        t = (start // step) * step
        while t <= end:
            x = self._time_to_x(t)
            # 太靠右的刻度文字會被裁掉一半，乾脆不畫
            if t >= start and x < self.width - self.app.metrics.px(40):
                self.canvas.create_line(x, 0, x, self.ruler * 0.4, fill=FG_SUBTLE)
                label = format_seconds(t) if step < 1 else f"{int(t // 60)}:{int(t % 60):02d}"
                self.canvas.create_text(x + 3, 1, text=label, anchor="nw", fill=FG_SUBTLE, font=FONT_SMALL)
            t += step

    def _draw_playhead(self) -> None:
        self.canvas.delete("playhead")
        position = self.position
        self.cursor_label.configure(text=tr("▼ 播放頭 {time}", time=format_seconds(position)))
        if not self.view[0] <= position <= self.view[1]:
            return
        px = self._time_to_x(position)
        size = self.app.metrics.px(6)
        self.canvas.create_line(px, 0, px, self.height, fill=WARN, width=2, tags="playhead")
        # 把手佔滿整條時間尺的高度，比原本的小三角形好抓
        self.canvas.create_polygon(px - size, 0, px + size, 0, px + size, self.ruler - size, px, self.ruler,
                                   px - size, self.ruler - size, fill=WARN, outline="", tags="playhead")

    # ================================================================== 成品（混音＋後製）
    def _denoise_key(self, index: int) -> tuple[int, int, int, bool]:
        a, b = self._sel_samples()
        return index, a, b, self.denoise_level_var.get() == self._denoise_levels[1]

    def _missing_denoise(self) -> list[tuple[int, int, int, bool]]:
        return [self._denoise_key(i) for i, t in enumerate(self.tracks)
                if t.enabled and t.denoise and self._denoise_key(i) not in self._denoised]

    def selected_audio(self) -> np.ndarray:
        """目前選取範圍的成品：開啟的音軌依各自音量混音、降噪、淡入淡出。

        需要的降噪結果必須已經算好（見 :meth:`_prepare`）。

        Returns:
            np.ndarray: (frames, 2) float32。

        Raises:
            ValueError: 選取範圍是空的、或沒有開啟任何音軌。
        """
        enabled = [(i, t) for i, t in enumerate(self.tracks) if t.enabled]
        if not enabled:
            raise ValueError(tr("請至少開啟一個音軌"))
        parts, gains = [], []
        for i, track in enabled:
            segment = trim(track.audio, *self.sel)
            if track.denoise:
                segment = self._denoised.get(self._denoise_key(i), segment)
            parts.append(segment)
            gains.append(track.gain_db)
        fade_in = self._fade_choices.get(self.fade_in_var.get(), 0.0)
        fade_out = self._fade_choices.get(self.fade_out_var.get(), 0.0)
        return fade_edges(mix(parts, gains), fade_in, fade_out)

    def _prepare(self, then: Callable[[], None]) -> None:
        """先在背景算好需要的降噪，再執行 ``then``（不需要降噪時立即執行）。"""
        missing = self._missing_denoise()
        if not missing:
            then()
            return
        if self._busy:
            self._pending = then
            return
        self._busy = True
        self._pending = then
        self._set_status(tr("🔇 正在降噪…（僅處理選取範圍）"), FG_MUTED)
        jobs = [(key, self.tracks[key[0]].audio[key[1]:key[2]]) for key in missing]

        def work() -> None:
            try:
                for key, audio in jobs:
                    self._jobs.put(("done", (key, denoise_stereo(audio, strong=key[3]))))
                self._jobs.put(("finished", None))
            except Exception as exc:  # noqa: BLE001 - 背景工作的錯誤要回報到視窗上
                self._jobs.put(("error", str(exc)))

        self.app.submit(work)
        self.top.after(JOB_POLL_MS, self._poll_jobs)

    def _poll_jobs(self) -> None:
        if not self.alive:
            return
        try:
            while True:
                kind, value = self._jobs.get_nowait()
                if kind == "done":
                    key, audio = value  # type: ignore[misc]
                    self._denoised[key] = audio
                elif kind == "error":
                    self._busy = False
                    self._pending = None
                    self._set_status(tr("降噪失敗：{error}", error=value), ERR)
                    return
                elif kind == "finished":
                    self._busy = False
                    self._set_status("")
                    then, self._pending = self._pending, None
                    if then is not None:
                        self._prepare(then)   # 處理期間選取範圍可能又變了，再檢查一次
                    return
        except queue.Empty:
            pass
        self.top.after(JOB_POLL_MS, self._poll_jobs)

    # ================================================================== 播放頭與播放
    @property
    def playing(self) -> bool:
        """是否正在播放。"""
        return self._play_started is not None

    @property
    def position(self) -> float:
        """播放頭目前的位置（秒）：播放中會跟著前進，停止時就是 :attr:`cursor`。"""
        if self._play_started is None:
            return self.cursor
        return min(self._play_origin + time.monotonic() - self._play_started, self._play_end)

    def set_cursor(self, t: float) -> None:
        """移動播放頭；播放中會直接跳到該處繼續播（像剪輯軟體一樣）。

        Args:
            t (float): 位置（秒），會限制在片段長度內。
        """
        self.cursor = min(max(t, 0.0), self.duration)
        if self.playing:
            self._prepare(lambda: self._start_play(self.cursor))
        else:
            self._draw_playhead()

    def set_start_at_cursor(self) -> None:
        """把選取的起點設在播放頭（快捷鍵 I）。"""
        position = self.position
        self.set_selection(position, max(self.sel[1], position))

    def set_end_at_cursor(self) -> None:
        """把選取的終點設在播放頭（快捷鍵 O）。"""
        position = self.position
        self.set_selection(min(self.sel[0], position), position)

    def _on_key(self, event: tk.Event, action: Callable[[], object]) -> str | None:
        # 在輸入框打字時（例如音效名稱有空白）不要觸發播放
        if isinstance(event.widget, (tk.Entry, ttk.Entry, ttk.Combobox)):
            return None
        action()
        return "break"

    def toggle_play(self) -> None:
        """從播放頭開始播放；播放中則暫停（播放頭停在播到的地方，再按會從那裡繼續）。"""
        if self.playing:
            self.stop_play()
            return
        start = self.cursor
        # 停在結尾（或選取範圍的終點）時再按播放：像剪輯軟體一樣從頭（選取開頭）重新播
        if start >= self.duration - 0.01:
            start = self.sel[0]
        elif abs(start - self.sel[1]) < 0.01 and self.sel[1] - self.sel[0] > 0.01:
            start = self.sel[0]
        self._prepare(lambda: self._start_play(start))

    def play_selection(self) -> None:
        """把播放頭移到選取開頭並播放（聽聽看成品）。"""
        self.stop_play()
        self.cursor = self.sel[0]
        self.toggle_play()

    def _audio_from(self, start: float) -> tuple[np.ndarray, float]:
        """從 ``start`` 秒開始要播放的音訊與播到哪裡停。

        播放頭在選取範圍內時播「成品」（含降噪、淡入淡出）到選取終點，聽到的就是存檔後的樣子；
        在範圍外則播各軌依目前音量混音的原始聲音，一直播到片段結尾。
        """
        a, b = self.sel
        if a <= start < b - 0.01:
            clip = self.selected_audio()
            return clip[int(round((start - a) * SAMPLE_RATE)):], b
        enabled = [t for t in self.tracks if t.enabled]
        if not enabled:
            raise ValueError(tr("請至少開啟一個音軌"))
        first = int(round(start * SAMPLE_RATE))
        return mix([t.audio[first:] for t in enabled], [t.gain_db for t in enabled]), self.duration

    def _start_play(self, start: float) -> None:
        try:
            audio, end = self._audio_from(start)
        except ValueError as exc:
            self._set_status(str(exc), ERR)
            return
        self.app.engine.stop(PLAY_KEY)
        if len(audio) == 0:
            self.stop_play()
            return
        self.app.engine.play(PLAY_KEY, audio, to_mic=False)   # 只有自己聽得到
        self._play_origin = start
        self._play_end = end
        self._play_started = time.monotonic()
        self.play_btn.set_text(tr("⏸ 暫停（空白鍵）"))
        if self._job is None:
            self._job = self.top.after(TICK_MS, self._tick)

    def _restart_if_playing(self) -> None:
        """改了音量、音軌或後製時，從目前位置用新的設定繼續播放。"""
        if not self.playing:
            return
        position = self.position
        self._prepare(lambda: self._start_play(position))

    def stop_play(self) -> None:
        """暫停播放：播放頭停在目前播到的位置。"""
        if self._play_started is not None:
            self.cursor = self.position
        self.app.engine.stop(PLAY_KEY)
        if self._job is not None:
            self.top.after_cancel(self._job)
            self._job = None
        if self._play_started is not None:
            self._play_started = None
            if self.alive:
                self.play_btn.set_text(tr("▶ 播放（空白鍵）"))
                self._draw_playhead()

    def _tick(self) -> None:
        self._job = None
        if self._play_started is None:
            return
        if self.position >= self._play_end or PLAY_KEY not in self.app.engine.active_keys():
            self.stop_play()
            return
        position = self.position
        start, end = self.view
        if self.zoomed and not start <= position <= end:
            # 放大時播放頭跑出畫面就翻到下一頁，和剪輯軟體一樣
            self._set_view(position - (end - start) * 0.05, end - start)
        self._draw_playhead()
        self._job = self.top.after(TICK_MS, self._tick)

    # ================================================================== 儲存
    def _set_status(self, text: str, color: str = FG_MUTED) -> None:
        if self.alive:      # 背景存檔完成時編輯器可能已經關了
            self.status.configure(text=text, fg=color)

    def save_as_sound(self) -> None:
        """把成品存進音效資料夾（.ogg），並出現在首頁（需要降噪時會先在背景處理）。"""
        error = validate_file_stem(self.name_var.get().strip())
        if error:
            self._set_status(error, ERR)
            return
        self._prepare(self._save_as_sound_now)

    def _save_as_sound_now(self) -> None:
        name = self.name_var.get().strip()
        try:
            clip = self.selected_audio()
        except ValueError as exc:
            self._set_status(str(exc), ERR)
            return
        folder = Path(self.app.settings.sound_dir or os.getcwd())
        path = unique_sound_path(folder, name)
        group = self.group_var.get()

        def done() -> None:
            # 回到主執行緒：加進群組、重新整理首頁（編輯器關掉了也要做，音效已經存好了）
            if group and group != tr(NO_GROUP):
                self.app.settings.move_to_group([path.relative_to(folder).as_posix()], group)
            self.app.rescan()
            self.app.save()
            self.last_saved = path
            msg = tr("✔ 已新增音效「{name}」（{seconds} 秒）", name=path.stem,
                     seconds=f"{len(clip) / SAMPLE_RATE:.1f}")
            self._set_status(msg, OK)
            self.app.set_status(msg, OK)
            logger.info("回放片段存成音效：%s", path)

        self._export_in_background(clip, path, done)

    def _export_in_background(self, clip: np.ndarray, path: Path, done: Callable[[], None]) -> None:
        """在背景編碼並寫檔（3 分鐘的 ogg 要好幾秒，放在主執行緒會讓視窗卡住），完成後在主執行緒呼叫 ``done``。"""
        if self._saving:
            self._set_status(tr("上一個檔案仍在儲存中，請稍候"), WARN)
            return
        self._saving = True
        self._set_status(tr("💾 正在儲存…（{seconds} 秒）", seconds=f"{len(clip) / SAMPLE_RATE:.0f}"), FG_MUTED)
        results: queue.Queue[str | None] = queue.Queue()

        def work() -> None:
            try:
                export_audio(clip, path)
                results.put(None)
            except Exception as exc:  # noqa: BLE001 - 任何錯誤都要回報到畫面上，否則會一直顯示「儲存中」
                logger.exception("存檔失敗：%s", path)
                results.put(str(exc))

        def poll() -> None:
            try:
                error = results.get_nowait()
            except queue.Empty:
                self.app.root.after(JOB_POLL_MS, poll)
                return
            self._saving = False
            if error is not None:
                self._set_status(tr("儲存失敗：{error}", error=error), ERR)
                self.app.set_status(tr("儲存失敗：{error}", error=error), ERR)
                return
            done()

        self.app.submit(work)
        # 用主視窗的計時器：存檔途中關掉編輯器，存好後仍會加進首頁
        self.app.root.after(JOB_POLL_MS, poll)

    def apply_volume(self) -> None:
        """把目前的音量設成這個音效的個別音量（之後在首頁、小視窗、快捷鍵播放都用這個音量）。"""
        if self.sound is None:
            return
        gain_db = self.tracks[0].gain_db
        self.app.editor.set_volume_db(self.sound, gain_db)
        self._set_status(tr("✔ 已套用：播放「{name}」時將調整 {gain} dB", name=self.sound.display_name,
                            gain=f"{gain_db:+.1f}"), OK)

    def export_dialog(self) -> None:
        """選擇位置與格式，另存成品。"""
        types = [(fmt, f"*{suffix}") for suffix, (fmt, _sub) in EXPORT_FORMATS.items()]
        target = filedialog.asksaveasfilename(parent=self.top, title=tr("另存檔案"), defaultextension=".ogg",
                                              initialfile=self.name_var.get().strip() or tr("回放"),
                                              filetypes=types)
        if target:
            self.export_to(Path(target))

    def export_to(self, path: Path) -> None:
        """把成品存到指定檔案（格式依副檔名；需要降噪時會先在背景處理）。

        Args:
            path (Path): 目標檔案；沒有支援的副檔名時自動加上 .ogg。
        """
        if path.suffix.lower() not in EXPORT_FORMATS:
            path = path.with_name(path.name + ".ogg")
        self._prepare(lambda: self._export_now(path))

    def _export_now(self, path: Path) -> None:
        try:
            clip = self.selected_audio()
        except ValueError as exc:
            self._set_status(str(exc), ERR)
            return

        def done() -> None:
            self.last_saved = path
            self._set_status(tr("✔ 已另存至：{path}", path=path), OK)

        self._export_in_background(clip, path, done)

    last_saved: Path | None = None
