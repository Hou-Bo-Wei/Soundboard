"""麥克風測試視窗：即時波形（原音 vs 降噪後）與「錄一段、前後比較播放」。

用途：關掉 Discord 的雜訊抑制、改用音效版降噪後，使用者需要親眼看到、親耳聽到
鍵盤聲到底有沒有被濾掉，才能決定要用「標準」還是「強」。
"""

from __future__ import annotations

import time
import tkinter as tk
from collections.abc import Callable
from tkinter import ttk

import numpy as np

from soundboard.config import SAMPLE_RATE
from soundboard.denoise import DenoiseLevel, denoise_clip
from soundboard.engine import MIC_TAP_SECONDS, AudioEngine
from soundboard.i18n import tr
from soundboard.theme import BG, BG_DARK, BTN, BTN_HOVER, ERR, FG, FG_MUTED, FONT_FAMILY, PREVIEW, WARN

LIVE_SECONDS = 4            # 即時波形顯示最近幾秒
FLOOR_DB = -60.0            # 波形最底下代表的音量；比這更小聲的視為無聲
REFRESH_MS = 50
WAVE_WIDTH = 640
WAVE_HEIGHT = 90
RAW_COLOR = "#f0b232"       # 原音：黃色
CLEAN_COLOR = "#23a55a"     # 降噪後：綠色
PLAY_KEY_RAW = "__mictest_raw__"
PLAY_KEY_CLEAN = "__mictest_clean__"


def envelope(samples: np.ndarray, columns: int) -> np.ndarray:
    """把樣本切成 ``columns`` 段，取每段的峰值（畫波形用）。

    Args:
        samples (np.ndarray): 一維樣本。
        columns (int): 欄數（通常是畫布寬度）。

    Returns:
        np.ndarray: 長度為 ``columns`` 的峰值（0 以上）。
    """
    if columns <= 0:
        return np.zeros(0, np.float32)
    if len(samples) == 0:
        return np.zeros(columns, np.float32)
    usable = len(samples) // columns * columns
    if usable == 0:
        # 樣本比欄數還少：每個樣本拉寬成好幾欄
        idx = np.linspace(0, len(samples) - 1, columns).astype(int)
        return np.abs(samples[idx]).astype(np.float32)
    return np.abs(samples[:usable]).reshape(columns, -1).max(axis=1).astype(np.float32)


def db_fraction(peaks: np.ndarray, floor_db: float = FLOOR_DB) -> np.ndarray:
    """把峰值換成 0–1 的高度比例（dB 刻度）。

    用 dB 而不是線性刻度：鍵盤聲、背景雜音常常只有說話的 1/30，
    線性刻度下幾乎看不到，dB 刻度才看得出「雜音有沒有被濾掉」。

    Args:
        peaks (np.ndarray): 峰值（0–1）。
        floor_db (float): 代表高度 0 的音量。

    Returns:
        np.ndarray: 0–1 的高度比例。
    """
    db = 20 * np.log10(np.maximum(peaks, 1e-9))
    return np.clip((db - floor_db) / -floor_db, 0.0, 1.0)


def rms_db(samples: np.ndarray) -> float:
    """平均音量（dBFS）。

    Args:
        samples (np.ndarray): 樣本。

    Returns:
        float: dBFS；無聲時為 -120。
    """
    if len(samples) == 0:
        return -120.0
    rms = float(np.sqrt(np.mean(np.square(samples, dtype=np.float64))))
    return max(-120.0, 20 * np.log10(rms)) if rms > 0 else -120.0


def noise_floor_db(samples: np.ndarray, window: int = SAMPLE_RATE // 50, quantile: float = 0.2) -> float:
    """背景雜音音量（dBFS）：每 20 ms 一段算音量，取最安靜的那 20%。

    平均音量會被說話聲主導，看不出雜音有沒有變小；沒說話的空檔才代表背景雜音。

    Args:
        samples (np.ndarray): 樣本。
        window (int): 每段的樣本數。
        quantile (float): 取第幾百分位（0–1）。

    Returns:
        float: dBFS；無聲時為 -120。
    """
    usable = len(samples) // window * window
    if usable == 0:
        return rms_db(samples)
    frames = samples[:usable].reshape(-1, window).astype(np.float64)
    rms = np.sqrt(np.mean(np.square(frames), axis=1))
    level = float(np.quantile(rms, quantile))
    return max(-120.0, 20 * np.log10(level)) if level > 0 else -120.0


class WaveformView:
    """一條波形：標題、音量數字與畫布。"""

    def __init__(self, parent: tk.Misc, title: str, color: str) -> None:
        """
        Args:
            parent (tk.Misc): 父元件。
            title (str): 標題。
            color (str): 波形顏色。
        """
        self.color = color
        head = tk.Frame(parent, bg=BG)
        head.pack(fill="x", pady=(8, 2))
        tk.Label(head, text=title, bg=BG, fg=color, font=(FONT_FAMILY, 10, "bold")).pack(side="left")
        self.level = tk.Label(head, bg=BG, fg=FG_MUTED, font=(FONT_FAMILY, 9))
        self.level.pack(side="right")
        self.canvas = tk.Canvas(parent, width=WAVE_WIDTH, height=WAVE_HEIGHT, bg=BG_DARK, highlightthickness=0)
        self.canvas.pack(fill="x")
        mid = WAVE_HEIGHT / 2
        # 參考線：中線與 -20 / -40 dB
        for db in (-20, -40):
            frac = (db - FLOOR_DB) / -FLOOR_DB
            for y in (mid - mid * frac, mid + mid * frac):
                self.canvas.create_line(0, y, WAVE_WIDTH, y, fill=BTN, dash=(2, 4))
            self.canvas.create_text(4, mid - mid * frac, text=f"{db} dB", anchor="sw", fill=FG_MUTED,
                                    font=(FONT_FAMILY, 7))
        self.canvas.create_line(0, mid, WAVE_WIDTH, mid, fill=BTN)
        self._poly = self.canvas.create_polygon(0, mid, 0, mid, 0, mid, fill=color, outline=color)
        self._head = self.canvas.create_line(-5, 0, -5, WAVE_HEIGHT, fill=FG, width=2)

    def show(self, samples: np.ndarray, playhead: float | None = None, highlight: bool = False) -> None:
        """畫出波形。

        Args:
            samples (np.ndarray): 要顯示的樣本（整段縮放到畫布寬度）。
            playhead (float | None): 播放位置比例 0–1；None 表示不顯示。
            highlight (bool): 是否強調（正在播放這一條）。
        """
        width = max(10, self.canvas.winfo_width())
        heights = db_fraction(envelope(samples, width)) * (WAVE_HEIGHT / 2 - 2)
        mid = WAVE_HEIGHT / 2
        xs = np.arange(width, dtype=np.float32)
        top = np.column_stack((xs, mid - heights)).ravel()
        bottom = np.column_stack((xs[::-1], (mid + heights)[::-1])).ravel()
        self.canvas.coords(self._poly, *top.tolist(), *bottom.tolist())
        x = -5 if playhead is None else playhead * width
        self.canvas.coords(self._head, x, 0, x, WAVE_HEIGHT)
        self.canvas.configure(highlightthickness=2 if highlight else 0, highlightbackground=FG)
        floor = noise_floor_db(samples)
        avg = f"{rms_db(samples):.0f}"
        if floor <= -100:
            text = tr("平均音量 {avg} dB　｜　背景雜音：無聲", avg=avg)
        else:
            text = tr("平均音量 {avg} dB　｜　背景雜音 {floor} dB", avg=avg, floor=f"{floor:.0f}")
        self.level.configure(text=text)


class MicTestDialog:
    """麥克風測試視窗（非強制回應，可以邊開邊用主視窗）。"""

    def __init__(
        self,
        parent: tk.Misc,
        engine: AudioEngine,
        *,
        get_level: Callable[[], DenoiseLevel],
        set_level: Callable[[DenoiseLevel], None],
        set_active: Callable[[bool], None],
        set_live_monitor: Callable[[bool], None],
    ) -> None:
        """
        Args:
            parent (tk.Misc): 主視窗。
            engine (AudioEngine): 音訊引擎（讀取麥克風取樣、播放錄音）。
            get_level (Callable[[], DenoiseLevel]): 取得目前的降噪強度。
            set_level (Callable[[DenoiseLevel], None]): 修改降噪強度（主程式同步設定與介面）。
            set_active (Callable[[bool], None]): 視窗開啟／關閉時通知主程式（需要開啟麥克風）。
            set_live_monitor (Callable[[bool], None]): 開關「即時試聽」。
        """
        self.engine = engine
        self._get_level = get_level
        self._set_level = set_level
        self._set_active = set_active
        self._set_live_monitor = set_live_monitor
        self._recording_from: int | None = None
        self._clip_raw: np.ndarray | None = None
        self._clip_clean: np.ndarray | None = None
        self._clip_level: DenoiseLevel | None = None
        self._playing: tuple[str, float, float] | None = None   # (哪一段, 開始時間, 長度秒)
        self._job: str | None = None

        self.top = tk.Toplevel(parent, bg=BG, padx=16, pady=12)
        self.top.title(tr("🎙 麥克風測試"))
        self.top.resizable(False, False)
        self.top.protocol("WM_DELETE_WINDOW", self.close)
        self._build()
        engine.tap_raw.clear()
        engine.tap_processed.clear()
        set_active(True)
        self._tick()

    # ------------------------------------------------------------------ 介面
    def _build(self) -> None:
        top = self.top
        row = tk.Frame(top, bg=BG)
        row.pack(fill="x")
        tk.Label(row, text=tr("🔇 降噪"), bg=BG, fg=FG, font=(FONT_FAMILY, 10)).pack(side="left")
        self.level_var = tk.StringVar(value=self._get_level().label)
        box = ttk.Combobox(row, textvariable=self.level_var, state="readonly", width=14,
                           values=[lvl.label for lvl in DenoiseLevel])
        box.pack(side="left", padx=8)
        box.bind("<<ComboboxSelected>>", lambda _e: self._on_level())
        self.live_var = tk.BooleanVar(value=self.engine.mic_monitor_enabled)
        ttk.Checkbutton(row, text=tr("👂 即時試聽（以耳機播放降噪後的麥克風聲音）"), variable=self.live_var,
                        command=lambda: self._set_live_monitor(self.live_var.get())).pack(side="left", padx=8)

        self.mode_label = tk.Label(top, bg=BG, fg=FG_MUTED, anchor="w", font=(FONT_FAMILY, 9))
        self.mode_label.pack(fill="x", pady=(8, 0))
        self.raw_view = WaveformView(top, tr("原始聲音（麥克風收音）"), RAW_COLOR)
        self.clean_view = WaveformView(top, tr("降噪後（通話對象聽到的聲音）"), CLEAN_COLOR)

        buttons = tk.Frame(top, bg=BG)
        buttons.pack(fill="x", pady=(12, 0))
        style = {"fg": FG, "activebackground": BTN_HOVER, "activeforeground": FG, "relief": "flat", "bd": 0,
                 "padx": 12, "pady": 5, "cursor": "hand2", "font": (FONT_FAMILY, 10)}
        self.rec_btn = tk.Button(buttons, text=tr("⏺ 開始錄音"), bg=ERR, command=self._toggle_record, **style)
        self.rec_btn.pack(side="left")
        self.play_raw_btn = tk.Button(buttons, text=tr("▶ 播放原音"), bg=BTN, state="disabled",
                                      command=lambda: self._play("raw"), **style)
        self.play_raw_btn.pack(side="left", padx=(8, 0))
        self.play_clean_btn = tk.Button(buttons, text=tr("▶ 播放降噪後"), bg=BTN, state="disabled",
                                        command=lambda: self._play("clean"), **style)
        self.play_clean_btn.pack(side="left", padx=(8, 0))
        tk.Button(buttons, text=tr("⏹ 停止"), bg=BTN, command=self._stop_playback, **style).pack(side="left", padx=(8, 0))
        self.live_btn = tk.Button(buttons, text=tr("↩ 返回即時波形"), bg=BTN, state="disabled",
                                  command=self._back_to_live, **style)
        self.live_btn.pack(side="right")

        self.hint = tk.Label(top, bg=BG, fg=FG_MUTED, justify="left", anchor="w", font=(FONT_FAMILY, 9), text=tr(
            "建議錄音步驟：保持安靜 2 秒 → 敲擊鍵盤數下 → 說幾句話 → 邊說話邊打字（最長 {seconds} 秒）。\n"
            "錄音完成後可分別播放比較。變更上方的降噪強度會立即重新計算，無須重新錄音。播放僅限本機監聽。",
            seconds=MIC_TAP_SECONDS))
        self.hint.pack(fill="x", pady=(10, 0))
        self.warn = tk.Label(top, bg=BG, fg=WARN, justify="left", anchor="w", font=(FONT_FAMILY, 9))
        self.warn.pack(fill="x")

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

    def sync_level(self) -> None:
        """設定頁改了降噪強度時同步顯示（錄音會用新強度重算）。"""
        self.level_var.set(self._get_level().label)
        self._recompute_clean()

    def close(self) -> None:
        """關閉視窗並停止取樣與播放。"""
        if self._job is not None:
            self.top.after_cancel(self._job)
            self._job = None
        self._stop_playback()
        self._set_active(False)
        self.top.destroy()

    # ------------------------------------------------------------------ 動作
    def _on_level(self) -> None:
        level = next(lvl for lvl in DenoiseLevel if lvl.label == self.level_var.get())
        self._set_level(level)
        self._recompute_clean()

    def _toggle_record(self) -> None:
        if self._recording_from is None:
            self._stop_playback()
            self._clip_raw = self._clip_clean = None
            self._recording_from = self.engine.tap_raw.position
            self.rec_btn.configure(text=tr("⏹ 停止錄音"))
            for btn in (self.play_raw_btn, self.play_clean_btn, self.live_btn):
                btn.configure(state="disabled")
        else:
            self._finish_record()

    def _finish_record(self) -> None:
        start, self._recording_from = self._recording_from, None
        self.rec_btn.configure(text=tr("⏺ 重新錄音"))
        raw = self.engine.tap_raw.since(start or 0)
        if len(raw) < SAMPLE_RATE // 2:
            self.warn.configure(text=tr("⚠ 錄音時間過短（不足 0.5 秒），請重新錄音。"))
            return
        self._clip_raw = raw
        self._recompute_clean()
        for btn in (self.play_raw_btn, self.play_clean_btn, self.live_btn):
            btn.configure(state="normal")

    def _recompute_clean(self) -> None:
        if self._clip_raw is None:
            return
        level = self._get_level()
        if level == self._clip_level and self._clip_clean is not None:
            return
        self._stop_playback()
        self.mode_label.configure(text=tr("處理中…"))
        self.top.update_idletasks()
        try:
            self._clip_clean = denoise_clip(self._clip_raw, level)
        except RuntimeError:
            self._clip_clean = self._clip_raw.copy()
        self._clip_level = level

    def _play(self, which: str) -> None:
        clip = self._clip_raw if which == "raw" else self._clip_clean
        if clip is None:
            return
        self._stop_playback()
        stereo = np.repeat(clip[:, None], 2, axis=1).astype(np.float32)
        key = PLAY_KEY_RAW if which == "raw" else PLAY_KEY_CLEAN
        # 只送到自己的耳機：測試錄音不該讓通話對象聽到
        self.engine.play(key, stereo, 1.0, to_mic=False)
        self._playing = (which, time.monotonic(), len(clip) / SAMPLE_RATE)

    def _stop_playback(self) -> None:
        self.engine.stop(PLAY_KEY_RAW)
        self.engine.stop(PLAY_KEY_CLEAN)
        self._playing = None

    def _back_to_live(self) -> None:
        self._stop_playback()
        self._clip_raw = self._clip_clean = None
        self._clip_level = None
        self.rec_btn.configure(text=tr("⏺ 開始錄音"))
        for btn in (self.play_raw_btn, self.play_clean_btn, self.live_btn):
            btn.configure(state="disabled")

    # ------------------------------------------------------------------ 更新畫面
    def _tick(self) -> None:
        self._job = None
        if not self.alive:
            return
        self._update_warnings()
        if self._recording_from is not None:
            elapsed = (self.engine.tap_raw.position - self._recording_from) / SAMPLE_RATE
            self.mode_label.configure(text=tr("⏺ 錄音中… {elapsed} / {total} 秒", elapsed=f"{elapsed:4.1f}",
                                              total=MIC_TAP_SECONDS), fg=ERR)
            if elapsed >= MIC_TAP_SECONDS - 0.2:
                self._finish_record()
        if self._clip_raw is not None and self._clip_clean is not None:
            self._draw_clip()
        else:
            n = SAMPLE_RATE * LIVE_SECONDS
            if self._recording_from is None:
                self.mode_label.configure(text=tr("即時波形（最近 {seconds} 秒，dB 刻度：越高表示音量越大）",
                                                  seconds=LIVE_SECONDS), fg=FG_MUTED)
            self.raw_view.show(self.engine.tap_raw.latest(n))
            self.clean_view.show(self.engine.tap_processed.latest(n))
        self._job = self.top.after(REFRESH_MS, self._tick)

    def _draw_clip(self) -> None:
        assert self._clip_raw is not None and self._clip_clean is not None
        playhead: float | None = None
        which = None
        if self._playing is not None:
            which, started, length = self._playing
            playhead = (time.monotonic() - started) / length if length else 1.0
            if playhead >= 1.0:
                self._playing, playhead, which = None, None, None
        level = self._clip_level.label if self._clip_level else ""
        seconds = f"{len(self._clip_raw) / SAMPLE_RATE:.1f}"
        # 播放中與否、播哪一段是不同的句子，各自完整翻譯（不串接片段）
        if which == "raw":
            text = tr("錄音（{seconds} 秒，降噪：{level}）　▶ 正在播放原音", seconds=seconds, level=level)
        elif which == "clean":
            text = tr("錄音（{seconds} 秒，降噪：{level}）　▶ 正在播放降噪後的聲音", seconds=seconds, level=level)
        else:
            text = tr("錄音（{seconds} 秒，降噪：{level}）", seconds=seconds, level=level)
        self.mode_label.configure(text=text, fg=PREVIEW if which else FG_MUTED)
        self.raw_view.show(self._clip_raw, playhead if which == "raw" else None, which == "raw")
        self.clean_view.show(self._clip_clean, playhead if which == "clean" else None, which == "clean")

    def _update_warnings(self) -> None:
        warnings = []
        if not self.engine.mic_input_active:
            warnings.append(tr("⚠ 麥克風未開啟。請在「⚙ 設定 → 🎧 音訊裝置」的「🗣 實體麥克風」中選擇麥克風。"))
        if not self.engine.monitor_active:
            warnings.append(tr("⚠「🎧 監聽裝置」設為不使用，無法聽到播放與即時試聽。"))
        text = "\n".join(warnings)
        # 「錄音太短」的提示要保留到下一次開始錄音；用整句比對（翻譯後的文字開頭不一定相同）
        too_short = self.warn.cget("text") == tr("⚠ 錄音時間過短（不足 0.5 秒），請重新錄音。")
        if self.warn.cget("text") != text and not too_short:
            self.warn.configure(text=text)
        elif too_short and self._recording_from is not None:
            self.warn.configure(text=text)

