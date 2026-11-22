"""設定頁：音訊裝置、麥克風降噪、進場音效、開機與背景執行、介面語言與版本資訊。

這一頁同時負責裝置選擇的邏輯（開啟哪個裝置、裝置插拔後自動重新偵測），
因為那些邏輯與下拉選單的內容密不可分。
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import tkinter as tk
from collections.abc import Callable
from tkinter import messagebox, ttk
from typing import TYPE_CHECKING

from soundboard import devices
from soundboard.denoise import DenoiseLevel
from soundboard.engine import AudioEngine, EngineError
from soundboard.gui.presenters import (
    AUDIO_OK_TEXT, JOIN_DELAY_CHOICES, LABEL_DEFAULT, LABEL_NONE, AudioWarning, audio_warnings, background_note,
    display_label, label_for, label_from_display, stored_device, voice_status,
)
from soundboard.gui.page_kit import CardPage
from soundboard.gui.styles import FONT_BODY, FONT_SMALL, FONT_SMALL_BOLD
from soundboard.config import settings_path
from soundboard.i18n import AUTO, LANGUAGES, current_language, detect_system_language, tr
from soundboard.mictest import MicTestDialog
from soundboard.models import AudioDevice
from soundboard.version import APP_NAME, display_version
from soundboard.theme import ACCENT, ACCENT_HOVER, CARD, ERR, FG, FG_MUTED, FG_SUBTLE, OK, WARN

if TYPE_CHECKING:
    from soundboard.gui.app import SoundboardApp

logger = logging.getLogger(__name__)

DEVICE_WATCH_MS = 2000


class SettingsPage(CardPage):
    """設定頁。"""

    def __init__(self, parent: tk.Misc, app: SoundboardApp) -> None:
        """
        Args:
            parent (tk.Misc): 父元件（內容區）。
            app (SoundboardApp): 主程式。
        """
        super().__init__(parent, app, tr("設定"),
                         tr("設定音訊裝置、麥克風降噪、進場音效與開機啟動。音量請至「🎚 混音器」調整。所有變更皆會自動儲存。"))
        self.settings = app.settings
        self._outputs: list[AudioDevice] = []
        self._inputs: list[AudioDevice] = []
        # None = 正常；字串 = 系統預設是虛擬音源線，監聽已改導向的裝置名稱
        self._monitor_redirected: str | None = None
        self.warnings: list[AudioWarning] = []
        self._endpoint_signature = devices.active_endpoint_signature()
        self._pending_signature: frozenset[str] | None = None
        self._gate_state: bool | None = None
        self._mic_test: MicTestDialog | None = None

        body = self.body
        self._build_devices(body)
        self._build_mic(body)
        self._build_join(body)
        self._build_background(body)
        self._build_language(body)
        self._build_about(body)

    @property
    def engine(self) -> AudioEngine:
        """音訊引擎。"""
        return self.app.engine

    # ================================================================== 裝置
    def _build_devices(self, parent: tk.Misc) -> None:
        m = self.app.metrics
        grid = self._card(parent, tr("🎧 音訊裝置"),
                          tr("選擇監聽用的耳機、傳送給通話對象的虛擬麥克風，以及您使用的麥克風。"))
        self.hint_label = tk.Label(grid, bg=CARD, fg=WARN, anchor="w", justify="left", font=FONT_SMALL_BOLD)
        self.hint_label.grid(row=0, column=0, columnspan=4, sticky="we", pady=(0, m.px(8)))
        grid.bind("<Configure>", lambda e: self.hint_label.configure(wraplength=max(m.px(200), e.width - 8)),
                  add="+")

        self._label(grid, 1, tr("音訊 API"))
        self.api_var = tk.StringVar(value=self.settings.host_api)
        self._combo(grid, self.api_var, self._on_api_changed, devices.list_host_apis(), width=22).grid(
            row=1, column=1, sticky="w", pady=m.px(5))
        self._button(grid, tr("🔄 重新偵測裝置"), self.redetect).grid(row=1, column=2, sticky="e", padx=(m.px(8), 0))

        self.monitor_var = tk.StringVar()
        self.vmic_var = tk.StringVar()
        self.micin_var = tk.StringVar()
        # 下拉選單的變數存「顯示文字」（特殊選項已翻譯），讀取時一律用 label_from_display() 轉回儲存值
        rows = ((tr("🎧 監聽裝置（耳機／喇叭）"), self.monitor_var, self._on_monitor_changed),
                (tr("🎙 虛擬麥克風輸出"), self.vmic_var, self._on_vmic_changed),
                (tr("🗣 實體麥克風"), self.micin_var, self._on_micin_changed))
        combos: list[ttk.Combobox] = []
        for i, (text, var, callback) in enumerate(rows, start=2):
            self._label(grid, i, text)
            combo = self._combo(grid, var, callback)
            combo.grid(row=i, column=1, columnspan=2, sticky="we", pady=m.px(5))
            combos.append(combo)
        self.monitor_combo, self.vmic_combo, self.micin_combo = combos

        self.passthrough_var = tk.BooleanVar(value=self.settings.passthrough_enabled)
        self._switch(grid, 5, self.passthrough_var, tr("將麥克風聲音混入虛擬麥克風"),
                     tr("開啟時，通話對象可同時聽到您的聲音與音效；關閉時僅能聽到音效。"), self._on_passthrough_toggled)
        self.overlap_var = tk.BooleanVar(value=self.settings.allow_overlap)
        self._switch(grid, 6, self.overlap_var, tr("允許多個音效同時播放"),
                     tr("關閉時，播放新音效前會先停止正在播放的音效。"), self._on_overlap_toggled)

    def load_devices(self, first_run: bool = False) -> None:
        """重新填入裝置清單並開啟選擇的裝置。

        Args:
            first_run (bool): 是否為程式啟動時的第一次載入。
        """
        # 暫時沒接上的裝置會讓清單退回「系統預設」，先記下使用者原本的選擇，
        # 存檔時保留它，裝置接回來（自動偵測或下次啟動）才能自動選回去
        wanted = (self.settings.monitor_device, self.settings.virtual_mic_device, self.settings.mic_input_device)
        self._refresh_device_lists(first_run)
        self._apply_all_devices()
        out_names = {d.name for d in self._outputs}
        in_names = {d.name for d in self._inputs}
        for attr, name, pool in (("monitor_device", wanted[0], out_names),
                                 ("virtual_mic_device", wanted[1], out_names),
                                 ("mic_input_device", wanted[2], in_names)):
            # 監聽裝置不保留虛擬音源線（選了自己會聽不到，_refresh_device_lists 已刻意改掉）
            if attr == "monitor_device" and name and devices.is_virtual_cable(name):
                continue
            if name and name != LABEL_NONE and name not in pool:
                setattr(self.settings, attr, name)

    def _refresh_device_lists(self, first_run: bool = False) -> None:
        api = self.settings.host_api
        all_devs = devices.list_devices(api)
        self._outputs = [d for d in all_devs if d.is_output]
        self._inputs = [d for d in all_devs if d.is_input]
        out_names = [d.name for d in self._outputs]
        in_names = [d.name for d in self._inputs]

        # 監聽裝置不列出虛擬音源線：選了它聲音會流進麥克風，自己反而聽不到
        real_out_names = [n for n in out_names if not devices.is_virtual_cable(n)]

        default_text, none_text = display_label(LABEL_DEFAULT), display_label(LABEL_NONE)
        self.monitor_combo.configure(values=[default_text, none_text, *real_out_names])
        self.vmic_combo.configure(values=[none_text, *out_names])
        self.micin_combo.configure(values=[default_text, none_text, *in_names])

        # 第一次執行或之前選的裝置不見了，就自動尋找虛擬音源線
        # 使用者明確選了「不使用」就尊重它（例如只想自己試聽），不要再自動接回虛擬音源線
        if self.settings.virtual_mic_device not in (*out_names, LABEL_NONE):
            cable = devices.find_virtual_cable(self._outputs)
            if cable is not None or not first_run:
                self.settings.virtual_mic_device = cable.name if cable else None

        # 舊設定可能把監聽裝置設成虛擬音源線（自己會聽不到），自動改成實體耳機
        if self.settings.monitor_device and devices.is_virtual_cable(self.settings.monitor_device):
            fallback = devices.pick_monitor_device(self._outputs, self._mic_name())
            self.settings.monitor_device = fallback.name if fallback else None
            if fallback:
                self.app.set_status(tr("監聽裝置原先設為虛擬音源線，已自動改為「{device}」", device=fallback.name),
                                    WARN)

        self.monitor_var.set(display_label(label_for(self.settings.monitor_device, real_out_names, allow_default=True)))
        self.vmic_var.set(display_label(label_for(self.settings.virtual_mic_device, out_names, allow_default=False)))
        self.micin_var.set(display_label(label_for(self.settings.mic_input_device, in_names, allow_default=True)))

    def _resolve(self, label: str, devs: list[AudioDevice], want_output: bool) -> int | None:
        """把下拉選單的儲存值（LABEL_DEFAULT／LABEL_NONE／裝置名稱）轉成裝置編號；None 表示不使用。"""
        if label == LABEL_NONE:
            return None
        if label == LABEL_DEFAULT:
            return devices.default_device_index(want_output, self.settings.host_api)
        dev = devices.find_by_name(devs, label, want_output)
        return dev.index if dev else None

    def _apply_all_devices(self) -> None:
        self._on_monitor_changed(save=False)
        # 虛擬麥克風開好後會順便開啟真實麥克風（直通）
        self._on_vmic_changed(save=False)

    def _try(self, action: Callable[[], None], what: str) -> bool:
        try:
            action()
            return True
        except EngineError as exc:
            logger.warning("%s失敗：%s", what, exc)
            self.app.set_status(str(exc), ERR)
            return False

    def _mic_name(self) -> str | None:
        """目前選擇的真實麥克風名稱（系統預設時查出實際裝置）。"""
        name = self.settings.mic_input_device
        if name and name != LABEL_NONE:
            return name
        idx = devices.default_device_index(False, self.settings.host_api)
        return next((d.name for d in self._inputs if d.index == idx), None)

    def _on_monitor_changed(self, save: bool = True) -> None:
        label = label_from_display(self.monitor_var.get())
        self.settings.monitor_device = stored_device(label)
        idx = self._resolve(label, self._outputs, want_output=True)
        self._monitor_redirected = None
        default_dev = next((d for d in self._outputs if d.index == idx), None)
        if label == LABEL_DEFAULT and default_dev and devices.is_virtual_cable(default_dev.name):
            # Windows 預設播放裝置是虛擬音源線（裝 VB-CABLE 後常見），
            # 照預設播的話自己聽不到，所以改播到實體耳機
            fallback = devices.pick_monitor_device(self._outputs, self._mic_name())
            idx = fallback.index if fallback else None
            self._monitor_redirected = fallback.name if fallback else ""
        self._try(lambda: self.engine.set_monitor_device(idx), "開啟監聽裝置")
        self._after_device_change(save)

    def _on_vmic_changed(self, save: bool = True) -> None:
        label = label_from_display(self.vmic_var.get())
        # 設定檔中 None 表示「從未設定」（會自動尋找虛擬音源線），所以「不使用」要明確存下來；
        # 存的是 LABEL_NONE 原文而不是翻譯後的顯示文字，切換介面語言後舊設定檔才讀得回來
        self.settings.virtual_mic_device = label
        idx = self._resolve(label, self._outputs, want_output=True)
        self._try(lambda: self.engine.set_virtual_mic_device(idx), "開啟虛擬麥克風")
        # 虛擬麥克風變動時，麥克風直通也要跟著開關
        self._on_micin_changed(save=False)
        self._after_device_change(save)

    def _on_micin_changed(self, save: bool = True) -> None:
        label = label_from_display(self.micin_var.get())
        self.settings.mic_input_device = stored_device(label)
        engine = self.engine
        idx = self._resolve(label, self._inputs, want_output=False) if self._need_mic() else None
        self._try(lambda: engine.set_mic_input_device(idx), "開啟麥克風")
        self._after_device_change(save)

    def refresh_mic_input(self) -> None:
        """依目前的需求重新決定要不要開啟真實麥克風（回放錄音開始／停止錄麥克風時呼叫）。"""
        # 程式剛啟動、裝置清單還沒載入時先不處理：載入裝置時會一併判斷。
        # 需求沒變就不動，否則重開麥克風會讓直通給通話對象的聲音斷一下
        if not self._inputs or self._need_mic() == self.engine.mic_input_active:
            return
        self._on_micin_changed(save=False)

    def _need_mic(self) -> bool:
        """目前有沒有功能需要真實麥克風。"""
        engine = self.engine
        # 沒有虛擬麥克風或關閉直通時，就不必佔用麥克風；但回放錄音要錄「我的麥克風」時也要開
        return bool((engine.virtual_mic_active and self.passthrough_var.get())
                    or engine.mic_monitor_enabled or engine.mic_test_active
                    or engine.mic_record_sink is not None)

    @property
    def mic_input_label(self) -> str:
        """設定頁選的真實麥克風（儲存值：LABEL_DEFAULT、LABEL_NONE 或裝置名稱，判斷用；不隨語言改變）。"""
        return label_from_display(self.micin_var.get())

    @property
    def mic_input_display(self) -> str:
        """設定頁選的真實麥克風（下拉選單上顯示的文字，已翻譯，顯示用）。"""
        return self.micin_var.get()

    def _after_device_change(self, save: bool) -> None:
        self._update_hint()
        if save:
            self.app.save()

    def _on_api_changed(self) -> None:
        self.settings.host_api = self.api_var.get()
        self.engine.close()
        self.engine.host_api = self.settings.host_api
        self._refresh_device_lists()
        self._apply_all_devices()
        self.app.save()

    def redetect(self, message: str | None = None) -> None:
        """重新列舉音訊裝置（插拔耳機、新裝驅動後）。

        Args:
            message (str | None): 完成後顯示在狀態列的訊息（已翻譯）；None 使用預設訊息。
        """
        # PortAudio 只在初始化時列舉裝置，插拔耳機或新裝驅動後必須重新初始化
        import sounddevice as sd

        self.engine.close()
        sd._terminate()  # noqa: SLF001 - sounddevice 沒有公開的重新列舉 API
        sd._initialize()  # noqa: SLF001
        self._endpoint_signature = devices.active_endpoint_signature()
        self.load_devices()
        # 預設參數在模組載入時就決定，不能在那裡呼叫 tr()，所以在這裡補上預設訊息
        self.app.set_status(tr("已重新偵測音訊裝置") if message is None else message, OK)

    def start_device_watch(self) -> None:
        """開始定期檢查裝置插拔。"""
        self.app.root.after(DEVICE_WATCH_MS, self._watch_devices)

    def _watch_devices(self) -> None:
        # 每隔一段時間檢查有沒有裝置插拔，有的話自動重新列舉，使用者不用手動按「重新偵測」
        signature = devices.active_endpoint_signature()
        if signature is None or signature == self._endpoint_signature:
            self._pending_signature = None
        elif signature == self._pending_signature:
            # 連續兩次都看到同樣的變動才重新列舉：剛插上的裝置驅動可能還沒準備好，
            # 太早初始化 PortAudio 會漏掉它
            logger.info("偵測到音訊裝置變動，重新列舉")
            self._pending_signature = None
            self.redetect(tr("偵測到音訊裝置變動，已自動更新裝置清單"))
        else:
            self._pending_signature = signature
        self.app.root.after(DEVICE_WATCH_MS, self._watch_devices)

    def _update_hint(self) -> None:
        self.warnings = audio_warnings(self.engine.virtual_mic_active, self._monitor_redirected)
        if self.warnings:
            self.hint_label.configure(fg=WARN, text="\n".join(w.detail for w in self.warnings))
        else:
            self.hint_label.configure(fg=OK, text=tr(AUDIO_OK_TEXT))
        self.app.refresh_audio_status()

    def _on_passthrough_toggled(self) -> None:
        self.settings.passthrough_enabled = self.passthrough_var.get()
        self.engine.passthrough_enabled = self.settings.passthrough_enabled
        self._on_micin_changed()
        mixer = getattr(self.app, "mixer_page", None)
        if mixer is not None:
            mixer.sync()

    def set_passthrough(self, enabled: bool) -> None:
        """開關「將麥克風聲音混入虛擬麥克風」（混音器的麥克風靜音鈕）。"""
        self.passthrough_var.set(enabled)
        self._on_passthrough_toggled()

    def _on_overlap_toggled(self) -> None:
        self.settings.allow_overlap = self.overlap_var.get()
        self.app.save()

    # ================================================================== 麥克風與降噪
    def _build_mic(self, parent: tk.Misc) -> None:
        m = self.app.metrics
        supported = self.engine.denoise_supported()
        note = (tr("降噪僅處理麥克風聲音，不影響音效。請關閉 Discord 的「雜訊抑制」功能。")
                if supported else tr("⚠ 此電腦無法載入降噪元件，無法使用降噪功能。"))
        grid = self._card(parent, tr("🎙 麥克風與降噪"), note)
        self._label(grid, 0, tr("🔇 麥克風降噪"))
        self.denoise_var = tk.StringVar(value=self.app.denoise_level().label)
        combo = self._combo(grid, self.denoise_var, self._on_denoise_changed,
                            [lvl.label for lvl in DenoiseLevel],
                            width=max(16, max(len(lvl.label) for lvl in DenoiseLevel) + 2))
        combo.grid(row=0, column=1, sticky="w", pady=m.px(5))
        if not supported:
            combo.configure(state="disabled")
        # 「強」模式的即時指示：打字時若亮「傳送中」，代表鍵盤聲被當成人聲
        self.gate_label = tk.Label(grid, bg=CARD, fg=FG_MUTED, font=FONT_SMALL_BOLD)
        self.gate_label.grid(row=0, column=2, sticky="e")

        self._label(grid, 1, tr("麥克風測試"))
        test = tk.Frame(grid, bg=CARD)
        test.grid(row=1, column=1, columnspan=2, sticky="we", pady=m.px(5))
        self._button(test, tr("🎙 麥克風測試（波形／錄音比較）"), self.open_mic_test, bg=ACCENT,
                     hover=ACCENT_HOVER).pack(side="left")
        tk.Label(test, text=tr("僅本機監聽，通話對象聽不到"), font=FONT_SMALL, bg=CARD, fg=FG_SUBTLE).pack(
            side="left", padx=(m.px(10), 0))

    def _on_denoise_changed(self) -> None:
        # 下拉選單顯示的是翻譯後的名稱；存檔用 enum 的 value，不受語言影響
        label = self.denoise_var.get()
        level = next(lvl for lvl in DenoiseLevel if lvl.label == label)
        self.settings.denoise_level = level.value
        self.engine.denoise_level = level
        self.app.save()
        if self._mic_test is not None and self._mic_test.alive:
            self._mic_test.sync_level()
        if level is DenoiseLevel.OFF:
            text = tr("🔇 麥克風降噪：{level}", level=level.label)
        else:
            text = tr("🔇 麥克風降噪：{level}（可按一下「🎙 麥克風測試」檢視波形並比較錄音）", level=level.label)
        self.app.set_status(text, OK)

    def _set_denoise_level(self, level: DenoiseLevel) -> None:
        """麥克風測試視窗改了降噪強度：同步設定頁下拉選單與設定。"""
        self.denoise_var.set(level.label)
        self.settings.denoise_level = level.value
        self.engine.denoise_level = level
        self.app.save()

    def update_gate(self, gate: bool | None) -> None:
        """更新人聲閘門指示（「強」模式才有）。

        Args:
            gate (bool | None): True = 傳送中、False = 靜音、None = 沒有閘門。
        """
        if gate == self._gate_state:
            return
        self._gate_state = gate
        if gate is None:
            self.gate_label.configure(text="")
        else:
            self.gate_label.configure(text=tr("🎙 傳送中") if gate else tr("🔇 靜音（未偵測到人聲）"),
                                      fg=OK if gate else FG_MUTED)

    def open_mic_test(self) -> None:
        """開啟麥克風測試視窗（已開著就拉到前面）。"""
        if self._mic_test is not None and self._mic_test.alive:
            self._mic_test.focus()
            return
        self._mic_test = MicTestDialog(
            self.app.root, self.engine,
            get_level=self.app.denoise_level,
            set_level=self._set_denoise_level,
            set_active=self._set_mic_test_active,
            set_live_monitor=self._set_live_monitor,
        )

    @property
    def mic_test(self) -> MicTestDialog | None:
        """開著的麥克風測試視窗；沒開時為 None。"""
        return self._mic_test if self._mic_test is not None and self._mic_test.alive else None

    def _set_mic_test_active(self, active: bool) -> None:
        self.engine.mic_test_active = active
        if not active:
            # 關掉測試視窗時一併關閉即時試聽，避免之後一直聽到自己的聲音
            self.engine.mic_monitor_enabled = False
        self._on_micin_changed(save=False)

    def _set_live_monitor(self, enabled: bool) -> None:
        # 不存進設定：試聽只是暫時用來確認降噪效果，下次開啟不該突然聽到自己的聲音
        self.engine.mic_monitor_enabled = enabled
        self._on_micin_changed(save=False)
        if enabled:
            self.app.set_status(tr("👂 即時試聽中：耳機將播放降噪後的麥克風聲音（延遲約 30 ms）"), FG)

    # ================================================================== 進場音效
    def _build_join(self, parent: tk.Misc) -> None:
        m = self.app.metrics
        grid = self._card(parent, tr("🚪 進場音效"),
                          tr("加入 Discord 語音頻道時，自動播放指定的音效給通話對象（不受試聽模式影響）。"))
        self.join_var = tk.BooleanVar(value=self.settings.join_sound_enabled)
        self._switch(grid, 0, self.join_var, tr("加入 Discord 語音頻道時播放"), "", self._on_join_toggled)

        self._label(grid, 1, tr("進場音效"))
        self.join_name = tk.Label(grid, font=FONT_BODY, bg=CARD, fg=FG_MUTED, anchor="w", justify="left")
        self.join_name.grid(row=1, column=1, columnspan=3, sticky="we", pady=m.px(5))

        self._label(grid, 2, tr("延遲"))
        row = tk.Frame(grid, bg=CARD)
        row.grid(row=2, column=1, columnspan=3, sticky="we", pady=m.px(5))
        self.join_delay_var = tk.StringVar(value=f"{self.settings.join_sound_delay:g}")
        self._combo(row, self.join_delay_var, self._on_join_delay, JOIN_DELAY_CHOICES, width=5).pack(side="left")
        tk.Label(row, text=tr("秒"), font=FONT_BODY, bg=CARD, fg=FG).pack(side="left", padx=(m.px(6), m.px(14)))
        self._button(row, tr("▶ 試播"), self.app.play_join_sound).pack(side="left")

        self._label(grid, 3, tr("偵測狀態"))
        self.voice_label = tk.Label(grid, font=FONT_BODY, bg=CARD, fg=FG_MUTED, anchor="w")
        self.voice_label.grid(row=3, column=1, columnspan=3, sticky="we", pady=m.px(5))

    def _on_join_toggled(self) -> None:
        self.settings.join_sound_enabled = self.join_var.get()
        self.app.save()

    def _on_join_delay(self) -> None:
        try:
            self.settings.join_sound_delay = float(self.join_delay_var.get())
        except ValueError:
            return
        self.app.save()

    def update_join_ui(self) -> None:
        """更新進場音效名稱、開關與 Discord 語音偵測狀態。"""
        self.join_var.set(self.settings.join_sound_enabled)
        item = self.app.items_by_key.get(self.settings.join_sound_key or "")
        if item:
            self.join_name.configure(text=tr("「{name}」", name=item.display_name), fg=FG)
        else:
            self.join_name.configure(text=tr("尚未選擇。請在首頁的音效上按一下右鍵，選擇「🚪 設為進場音效」。"),
                                     fg=FG_MUTED)
        text, color = voice_status(self.app.voice_in_voice)
        self.voice_label.configure(text=text, fg=color)

    # ================================================================== 開機與背景執行
    def _build_background(self, parent: tk.Misc) -> None:
        m = self.app.metrics
        grid = self._card(parent, tr("🖥 開機與背景執行"),
                          tr("讓音效版在背景持續執行：關閉主視窗後，快速鍵、浮動視窗與進場音效仍可使用。"))
        self.autostart_var = tk.BooleanVar(value=False)
        self._switch(grid, 0, self.autostart_var, tr("開機時自動啟動音效版"),
                     tr("登入 Windows 後自動在背景啟動，無須每次手動開啟。"), self._on_autostart_toggled)
        self.autostart_hidden_var = tk.BooleanVar(value=self.settings.autostart_hidden)
        self._switch(grid, 1, self.autostart_hidden_var, tr("開機啟動時不顯示視窗，僅顯示於系統匣"),
                     tr("關閉此選項時，開機後將直接開啟主視窗。"), self._on_autostart_hidden_toggled)
        self.close_to_tray_var = tk.BooleanVar(value=self.settings.close_to_tray)
        self._switch(grid, 2, self.close_to_tray_var, tr("按一下 ✕ 關閉視窗時縮小至系統匣，並在背景繼續執行"),
                     tr("若要完全結束程式，請在系統匣的音效版圖示上按一下右鍵並選擇「結束音效版」，或按一下下方的按鈕。"),
                     self._on_close_to_tray_toggled)
        row = tk.Frame(grid, bg=CARD)
        row.grid(row=3, column=0, columnspan=4, sticky="we", pady=(m.px(8), 0))
        self._button(row, tr("⏻ 結束音效版"), self.app.on_close).pack(side="right")
        self.background_label = tk.Label(row, font=FONT_SMALL, bg=CARD, fg=FG_MUTED, anchor="w", justify="left")
        self.background_label.pack(side="left", fill="x", expand=True, padx=(0, m.px(12)))
        row.bind("<Configure>", lambda e: self.background_label.configure(
            wraplength=max(m.px(200), e.width - m.px(220))), add="+")

    def update_background_ui(self) -> None:
        """依登錄檔與系統匣的實際狀態更新開關與說明（使用者可能在工作管理員改過）。"""
        manager = self.app.autostart
        registered = manager.registered_command() is not None
        disabled = manager.disabled_in_task_manager()
        self.autostart_var.set(registered and not disabled)
        tray = getattr(self.app, "tray", None)
        text, color = background_note(supported=manager.supported, registered=registered,
                                      disabled_by_windows=disabled, elsewhere=manager.registered_elsewhere(),
                                      tray_ok=tray is not None and tray.running)
        self.background_label.configure(text=text, fg=color)

    def _on_autostart_toggled(self) -> None:
        manager = self.app.autostart
        want = self.autostart_var.get()
        try:
            if want:
                manager.enable()
            else:
                manager.disable()
        except OSError as exc:
            logger.warning("開機自動啟動設定失敗：%s", exc)
            self.app.set_status(tr("無法設定開機自動啟動：{error}", error=exc), ERR)
        else:
            self.app.set_status(tr("✔ 已啟用開機自動啟動") if want else tr("已停用開機自動啟動"),
                                OK if want else FG_MUTED)
        self.update_background_ui()

    def _on_autostart_hidden_toggled(self) -> None:
        self.settings.autostart_hidden = self.autostart_hidden_var.get()
        self.app.save()

    def _on_close_to_tray_toggled(self) -> None:
        self.settings.close_to_tray = self.close_to_tray_var.get()
        self.app.save()

    # ================================================================== 語言與版本
    def language_choices(self) -> list[tuple[str, str]]:
        """語言下拉選單的選項。

        Returns:
            list[tuple[str, str]]: (設定值, 顯示文字)；第一項為「跟隨系統」。
        """
        system = next(lang.native_name for lang in LANGUAGES if lang.code == detect_system_language())
        return [(AUTO, tr("跟隨系統（{language}）", language=system))] + [
            (lang.code, lang.native_name) for lang in LANGUAGES]

    def _build_language(self, parent: tk.Misc) -> None:
        grid = self._card(parent, tr("🌐 語言"), tr("變更介面語言後，需重新啟動音效版才會套用。"))
        self._label(grid, 0, tr("介面語言"))
        self._language_choices = self.language_choices()
        current = self.settings.language if self.settings.language in {c for c, _ in self._language_choices} else AUTO
        self.language_var = tk.StringVar(value=dict(self._language_choices)[current])
        combo = self._combo(grid, self.language_var, self._on_language_changed,
                            [label for _, label in self._language_choices], width=24)
        combo.grid(row=0, column=1, sticky="w", pady=self.app.metrics.px(5))

    def _on_language_changed(self) -> None:
        code = next((c for c, label in self._language_choices if label == self.language_var.get()), AUTO)
        if code == self.settings.language:
            return
        self.settings.language = code
        self.app.save()
        effective = code or detect_system_language()
        if effective == current_language():
            return
        if messagebox.askyesno(tr("變更語言"), tr("需重新啟動音效版才能套用新的介面語言。是否立即重新啟動？"),
                               parent=self.app.root):
            self.app.restart()
        else:
            self.app.set_status(tr("新的介面語言將於下次啟動音效版時套用"), FG_MUTED)

    def _build_about(self, parent: tk.Misc) -> None:
        m = self.app.metrics
        grid = self._card(parent, tr("ℹ 關於"))
        self._label(grid, 0, tr("版本"))
        tk.Label(grid, text=tr("{name} 版本 {version}", name=APP_NAME, version=display_version()), font=FONT_BODY,
                 bg=CARD, fg=FG, anchor="w").grid(row=0, column=1, sticky="w", pady=m.px(5))
        self._label(grid, 1, tr("設定檔"))
        row = tk.Frame(grid, bg=CARD)
        row.grid(row=1, column=1, sticky="we", pady=m.px(5))
        self._button(row, tr("📂 開啟設定資料夾"), self._open_settings_folder).pack(side="left")
        self.settings_path_label = tk.Label(row, text=str(settings_path()), font=FONT_SMALL, bg=CARD, fg=FG_SUBTLE,
                                            anchor="w")
        self.settings_path_label.pack(side="left", padx=(m.px(10), 0))

    @staticmethod
    def _open_settings_folder() -> None:
        folder = settings_path().parent
        if sys.platform == "win32":
            os.startfile(folder)  # noqa: S606 - 開啟使用者自己的設定資料夾
        else:
            subprocess.Popen(["xdg-open", str(folder)])
