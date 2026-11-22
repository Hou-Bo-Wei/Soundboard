"""🎚 混音器頁：用一條條通道調整朋友聽到的每個聲音，並把 Chrome 等程式的聲音也送給朋友。

通道：總音量、音效版、我的麥克風，以及使用者加入的程式。程式通道用 :mod:`soundboard.app_audio`
只擷取該程式的聲音混進虛擬麥克風；你自己照常從喇叭聽到，不受這裡的推桿影響。
"""

from __future__ import annotations

import tkinter as tk
from pathlib import Path
from typing import TYPE_CHECKING

from soundboard import process_capture
from soundboard.app_audio import app_key, is_blocked
from soundboard.gui.channel_strip import STRIP_WIDTH, ChannelStrip
from soundboard.gui.page_kit import CardPage
from soundboard.gui.presenters import LABEL_NONE, display_label, label_from_display
from soundboard.gui.styles import FONT_BODY, FONT_H2, FONT_SMALL
from soundboard.i18n import N_, tr
from soundboard.theme import ACCENT, ACCENT_HOVER, CARD, ERR, FG, FG_MUTED, FG_SUBTLE, OK, SURFACE, WARN

if TYPE_CHECKING:
    from soundboard.gui.app import SoundboardApp

MASTER = "@master"
SOUNDS = "@sounds"
MIC = "@mic"
MONITOR = "@monitor"
TICK_MS = 80
STATUS_EVERY = 6            # 每幾次 tick 更新一次狀態文字
METER_DECAY = 0.8           # 音量表落下的速度（每次 tick 乘上這個值）
STRIP_GAP = 10
# (通道 key, 標題, 圖示, 顏色)；標題在模組載入時還沒決定語言，只用 N_ 標記，建立通道時再 tr
FIXED_CHANNELS: tuple[tuple[str, str, str, str], ...] = (
    (MASTER, N_("總音量"), "🎚", "#7b86f7"),
    (SOUNDS, N_("音效"), "🔊", "#2dd4a8"),
    (MIC, N_("麥克風"), "🎙", "#f5a524"),
    (MONITOR, N_("監聽"), "🎧", "#c0c6d0"),
)
APP_COLORS: tuple[str, ...] = ("#38b6ff", "#f0569b", "#a774ff", "#4ade80", "#ff8a5b")
_BROWSERS = ("chrome", "msedge", "firefox", "opera", "brave", "vivaldi", "arc")
_MUSIC = ("spotify", "music", "itunes", "foobar", "aimp", "kkbox", "deezer", "tidal")
_VIDEO = ("vlc", "potplayer", "mpc", "mpv", "netflix", "obs")


def app_title(exe: str) -> str:
    """程式通道的標題（``chrome.exe`` → ``Chrome``）。"""
    stem = Path(exe).stem
    return stem[:1].upper() + stem[1:] if stem.islower() else stem


def app_icon(exe: str) -> str:
    """依程式類型挑圖示。"""
    name = exe.lower()
    if any(k in name for k in _BROWSERS):
        return "🌐"
    if any(k in name for k in _MUSIC):
        return "🎵"
    if any(k in name for k in _VIDEO):
        return "🎬"
    return "🖥"


def route_status_text(state: str, detail: str = "") -> tuple[str, str]:
    """程式通道的狀態文字與顏色。

    Args:
        state (str): :class:`soundboard.app_audio.RouteStatus` 的 state。
        detail (str): 補充說明。

    Returns:
        tuple[str, str]: (文字, 顏色)。
    """
    if state == "sending":
        return tr("● 傳送給對方"), OK
    if state == "waiting":
        return tr("○ 程式未執行"), FG_SUBTLE
    if state == "unsupported":
        return tr("⚠ 需要 Windows 10 2004 或以上"), WARN
    # detail 是已翻譯的錯誤訊息（見 process_capture.CaptureError）
    return (f"⚠ {detail}" if detail else tr("⚠ 無法擷取")), ERR


class MixerPage(CardPage):
    """混音器頁。"""

    def __init__(self, parent: tk.Misc, app: SoundboardApp) -> None:
        """
        Args:
            parent (tk.Misc): 父元件（內容區）。
            app (SoundboardApp): 主程式。
        """
        super().__init__(parent, app, tr("混音器"),
                         tr("在此調整所有音量。「🎧 監聽」為本機耳機中的音效音量，其餘通道皆為通話對象聽到的音量；"
                            "按一下「＋ 加入程式」可將 Chrome、Spotify 等程式的聲音一併傳送給通話對象。"))
        self.settings = app.settings
        m = app.metrics
        self.notice = tk.Label(self.body, text="", font=FONT_BODY, bg=SURFACE, fg=WARN, anchor="w", justify="left")
        self.notice.pack(fill="x", pady=(0, m.px(10)))
        self.body.bind("<Configure>", lambda e: self.notice.configure(wraplength=max(m.px(200), e.width - m.px(8))),
                       add="+")
        self.grid_frame = tk.Frame(self.body, bg=SURFACE)
        self.grid_frame.pack(fill="x")
        self.grid_frame.bind("<Configure>", lambda e: self._reflow(e.width))
        self.strips: dict[str, ChannelStrip] = {}
        self._levels: dict[str, float] = {}
        self._cells: list[tk.Widget] = []
        self._columns = 0
        self._ticks = 0
        self._pending_apps: list[str] | None = None
        self.add_var = tk.StringVar()
        self.rebuild()
        self.after(TICK_MS, self._tick)

    # ================================================================== 版面
    def rebuild(self) -> None:
        """依設定重建所有通道（加入或移除程式後）。"""
        for cell in self._cells:
            cell.destroy()
        self._cells = []
        self.strips = {}
        m = self.app.metrics
        values = self._fixed_values()
        muted = self._fixed_muted()
        for key, title, icon, color in FIXED_CHANNELS:
            self._add_strip(ChannelStrip(
                self.grid_frame, key=key, title=tr(title), icon=icon, color=color, value=values[key],
                muted=muted[key], info=self._fixed_info(key), metrics=m, on_change=self._on_change,
                on_release=self._on_release, on_mute=self._on_mute))
        for i, exe in enumerate(self.settings.mixer_apps):
            key = app_key(exe)
            self._add_strip(ChannelStrip(
                self.grid_frame, key=exe, title=app_title(exe), icon=app_icon(exe),
                color=APP_COLORS[i % len(APP_COLORS)],
                value=round(self.settings.mixer_volumes.get(key, 1.0) * 100),
                muted=key in self.settings.mixer_muted, info=tr("{exe}\n本機播放不受此推桿影響", exe=exe), metrics=m,
                on_change=self._on_change, on_release=self._on_release, on_mute=self._on_mute,
                on_remove=self.remove_app))
        self._cells.append(self._build_add_card())
        self._columns = 0
        self._reflow(self.grid_frame.winfo_width())
        self.update_status()

    def _add_strip(self, strip: ChannelStrip) -> None:
        self.strips[strip.key] = strip
        self._cells.append(strip)

    def _reflow(self, width: int) -> None:
        m = self.app.metrics
        cell = m.px(STRIP_WIDTH + STRIP_GAP)
        columns = max(1, (max(width, 1) + m.px(STRIP_GAP)) // cell)
        if columns == self._columns:
            return
        self._columns = columns
        # 每條通道同寬（否則會依標題長短忽寬忽窄）
        for c in range(len(self._cells) + 1):
            self.grid_frame.columnconfigure(c, minsize=m.px(STRIP_WIDTH) if c < columns else 0,
                                            uniform="strip" if c < columns else "")
        for i, widget in enumerate(self._cells):
            widget.grid(row=i // columns, column=i % columns, sticky="nsew",
                        padx=(0, m.px(STRIP_GAP)), pady=(0, m.px(STRIP_GAP)))

    def _build_add_card(self) -> tk.Frame:
        m = self.app.metrics
        card = tk.Frame(self.grid_frame, bg=CARD, width=m.px(STRIP_WIDTH))
        header = tk.Frame(card, bg="#1e1f22")
        header.pack(fill="x")
        tk.Label(header, text=tr("＋ 加入程式"), font=FONT_H2, bg="#1e1f22", fg=FG, anchor="w").pack(
            side="left", padx=m.px(10), pady=m.px(8))
        body = tk.Frame(card, bg=CARD)
        body.pack(fill="both", expand=True, padx=m.px(10), pady=m.px(10))
        tk.Label(body, text=tr("正在發出聲音的程式："), font=FONT_SMALL, bg=CARD, fg=FG_MUTED, anchor="w").pack(fill="x")
        self.add_combo = self._combo(body, self.add_var, lambda: None, width=14)
        self.add_combo.configure(state="normal")       # 也可以直接打執行檔名稱（程式還沒開時）
        self.add_combo.pack(fill="x", pady=(m.px(4), m.px(8)))
        row = tk.Frame(body, bg=CARD)
        row.pack(fill="x")
        self._button(row, tr("加入"), self.add_app, bg=ACCENT, hover=ACCENT_HOVER).pack(side="left")
        self._button(row, "🔄", self.refresh_app_list).pack(side="left", padx=(m.px(6), 0))
        tk.Label(body, text=tr("若清單中沒有該程式，請先讓程式發出聲音（例如播放影片），再按一下 🔄。\n\n"
                               "無法加入 Discord：通話對象會聽到自己的回音。"),
                 font=FONT_SMALL, bg=CARD, fg=FG_SUBTLE, anchor="nw", justify="left",
                 wraplength=m.px(STRIP_WIDTH - 24)).pack(fill="x", pady=(m.px(12), 0))
        return card

    # ================================================================== 數值
    def _fixed_values(self) -> dict[str, float]:
        s = self.settings
        return {MASTER: round(s.mixer_volumes.get(MASTER, 1.0) * 100), SOUNDS: round(s.mic_volume * 100),
                MIC: round(s.passthrough_volume * 100), MONITOR: round(s.monitor_volume * 100)}

    def _fixed_muted(self) -> dict[str, bool]:
        s = self.settings
        return {MASTER: MASTER in s.mixer_muted, SOUNDS: SOUNDS in s.mixer_muted, MIC: not s.passthrough_enabled,
                MONITOR: MONITOR in s.mixer_muted}

    def _fixed_info(self, key: str) -> str:
        if key == MASTER:
            return tr("通話對象聽到的所有聲音\n（音效＋麥克風＋程式）")
        if key == SOUNDS:
            return tr("從首頁、浮動視窗、快速鍵\n播放的音效")
        # 設定頁下拉選單的文字可能已翻譯：先換回儲存值再比對，顯示時再翻譯（兩種形式都接受）
        if key == MONITOR:
            device = display_label(label_from_display(self.app.settings_page.monitor_var.get()))
            return tr("{device}\n僅本機監聽的音效音量", device=device)
        label = label_from_display(self.app.settings_page.mic_input_label)
        if label == LABEL_NONE:
            return tr("未選擇麥克風\n（請至 ⚙ 設定選擇）")
        return tr("{device}\n（降噪後）", device=display_label(label))

    def sync(self) -> None:
        """設定頁改了音量或麥克風開關時，同步到推桿。"""
        values, muted = self._fixed_values(), self._fixed_muted()
        for key in (MASTER, SOUNDS, MIC, MONITOR):
            strip = self.strips.get(key)
            if strip is not None:
                strip.set_value(values[key])
                strip.set_muted(muted[key])

    # ================================================================== 操作
    def _on_change(self, key: str, percent: float) -> None:
        factor = percent / 100
        s = self.settings
        if key == MASTER:
            s.mixer_volumes[MASTER] = factor
            self.app.apply_engine_gains()
        elif key in (SOUNDS, MIC, MONITOR):
            if key == SOUNDS:
                s.mic_volume = factor
            elif key == MIC:
                s.passthrough_volume = factor
            else:
                s.monitor_volume = factor
            self.app.apply_engine_gains()
        else:
            s.mixer_volumes[app_key(key)] = factor
            self.app.app_router.set_gain(key, factor)

    def _on_release(self, _key: str) -> None:
        self.app.save()

    def _on_mute(self, key: str, muted: bool) -> None:
        if key == MIC:
            self.app.settings_page.set_passthrough(not muted)
            return
        store = self.settings.mixer_muted
        name = key if key.startswith("@") else app_key(key)
        if muted and name not in store:
            store.append(name)
        elif not muted and name in store:
            store.remove(name)
        if key.startswith("@"):
            self.app.apply_engine_gains()
        else:
            self.app.app_router.set_muted(key, muted)
        self.app.save()

    def add_app(self) -> None:
        """把下拉選單選的程式加入混音器。"""
        exe = self.add_var.get().strip()
        if not exe:
            self.app.set_status(tr("請先選擇或輸入要加入的程式"), WARN)
            return
        if not exe.lower().endswith(".exe"):
            exe += ".exe"
        if is_blocked(exe):
            self.app.set_status(tr("無法加入 Discord：通話對象的聲音會被傳回，造成回音"), ERR)
            return
        if app_key(exe) in {app_key(a) for a in self.settings.mixer_apps}:
            self.app.set_status(tr("「{name}」已在混音器中", name=app_title(exe)), FG_MUTED)
            return
        self.settings.mixer_apps.append(exe)
        self.add_var.set("")
        self.app.apply_mixer()
        self.app.save()
        self.rebuild()
        self.app.set_status(tr("🎚 已加入「{name}」，其聲音將傳送給通話對象", name=app_title(exe)), OK)

    def remove_app(self, exe: str) -> None:
        """把程式移出混音器（不再送給朋友）。"""
        key = app_key(exe)
        s = self.settings
        s.mixer_apps = [a for a in s.mixer_apps if app_key(a) != key]
        s.mixer_volumes.pop(key, None)
        if key in s.mixer_muted:
            s.mixer_muted.remove(key)
        self.app.apply_mixer()
        self.app.save()
        self.rebuild()
        self.app.set_status(tr("已移除「{name}」，通話對象將不再聽到其聲音", name=app_title(exe)), FG_MUTED)

    def refresh_app_list(self) -> None:
        """在背景重新列出正在發出聲音的程式。"""
        def work() -> None:
            self._pending_apps = process_capture.list_audio_apps()

        self.app.submit(work)

    # ================================================================== 更新
    def on_show(self) -> None:
        """切換到這一頁時呼叫。"""
        self.refresh_app_list()
        self.sync()
        self.update_status()

    def update_status(self) -> None:
        """更新提示文字與各通道的狀態。"""
        engine = self.app.engine
        router = self.app.app_router
        notices = []
        if not engine.virtual_mic_active:
            notices.append(tr("⚠ 尚未設定虛擬麥克風，通話對象無法聽到此處的任何聲音。"
                              "請至「⚙ 設定 → 虛擬麥克風輸出」選擇 CABLE Input。"))
        if not router.supported:
            notices.append(tr("⚠ 此電腦的 Windows 版本過舊（需要 Windows 10 2004 或以上），"
                              "無法將其他程式的聲音傳送給通話對象。"))
        self.notice.configure(text="\n".join(notices))
        self.notice.pack_configure(pady=(0, self.app.metrics.px(10) if notices else 0))
        s = self.settings
        fixed = {
            MASTER: (tr("● 對方聽得到"), OK) if engine.virtual_mic_active else (tr("○ 虛擬麥克風未啟用"), WARN),
            SOUNDS: (tr("🎧 試聽模式：僅本機監聽"), WARN) if self.app.preview_mode else (tr("● 傳送給對方"), OK),
            MIC: ((tr("○ 已關閉"), FG_SUBTLE) if not s.passthrough_enabled else
                  (tr("○ 未選擇麥克風"), WARN)
                  if label_from_display(self.app.settings_page.mic_input_label) == LABEL_NONE else
                  (tr("● 傳送給對方"), OK)),
            MONITOR: (tr("● 僅本機監聽"), OK) if engine.monitor_active else (tr("○ 未選擇監聽裝置"), WARN),
        }
        for key, (text, color) in fixed.items():
            strip = self.strips.get(key)
            if strip is not None:
                if strip.muted:
                    text, color = tr("🔇 已靜音"), ERR
                strip.set_status(text, color)
        for key in (MIC, MONITOR):
            strip = self.strips.get(key)
            if strip is not None:
                strip.set_info(self._fixed_info(key))
        for exe in s.mixer_apps:
            strip = self.strips.get(exe)
            if strip is None:
                continue
            status = router.status(exe)
            text, color = route_status_text(status.state, status.detail)
            if strip.muted and status.state == "sending":
                text, color = tr("🔇 已靜音"), ERR
            strip.set_status(text, color)

    def _tick(self) -> None:
        if not self.winfo_exists():
            return
        if self._pending_apps is not None:
            apps = [a for a in self._pending_apps if not is_blocked(a)]
            self._pending_apps = None
            self.add_combo.configure(values=apps)
        if self.app.current_page == "mixer" and self.app.window_visible:
            levels = self.app.engine.levels
            raw = {MASTER: levels["master"], SOUNDS: levels["sounds"], MIC: levels["mic"],
                   MONITOR: levels["monitor"]}
            for exe in self.settings.mixer_apps:
                raw[exe] = self.app.app_router.level(exe)
            for key, strip in self.strips.items():
                shown = max(raw.get(key, 0.0), self._levels.get(key, 0.0) * METER_DECAY)
                self._levels[key] = shown if shown > 1e-4 else 0.0
                strip.fader.set_level(self._levels[key])
            self._ticks += 1
            if self._ticks % STATUS_EVERY == 0:
                self.update_status()
        self.after(TICK_MS, self._tick)


__all__ = ["MixerPage", "app_icon", "app_title", "route_status_text", "MASTER", "SOUNDS", "MIC", "MONITOR"]
