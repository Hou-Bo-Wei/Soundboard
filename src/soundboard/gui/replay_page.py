"""回放錄音頁：選擇錄音來源（程式、我的麥克風）與保留長度、擷取最近的聲音、管理擷取到的片段。"""

from __future__ import annotations

import logging
import os
import queue
import tkinter as tk
from pathlib import Path
from collections.abc import Callable
from tkinter import filedialog, messagebox
from typing import TYPE_CHECKING

import numpy as np

from soundboard import process_capture
from soundboard.clips import (
    EXPORT_FORMATS, ClipInfo, auto_mix, clips_dir, delete_clip, export_audio, list_clips, load_clip, save_clip,
)
from soundboard.config import settings_path
from soundboard.gui.clip_editor import ClipEditor, format_seconds
from soundboard.gui.page_kit import CardPage
from soundboard.gui.presenters import LABEL_NONE, display_label, label_from_display
from soundboard.gui.styles import FONT_BODY, FONT_SMALL, FONT_SMALL_BOLD
from soundboard.gui.widgets import ToggleSwitch
from soundboard.i18n import tr
from soundboard.replay import MIC_SOURCE, REPLAY_SECONDS_CHOICES, ReplayRecorder, source_label
from soundboard.theme import ACCENT, ACCENT_HOVER, CARD, ERR, ERR_HOVER, FG, FG_MUTED, FG_SUBTLE, OK, WARN

if TYPE_CHECKING:
    from soundboard.gui.app import SoundboardApp

logger = logging.getLogger(__name__)

STATUS_TICK_MS = 1000
MAX_LISTED_CLIPS = 30
BYTES_PER_SECOND = process_capture.SAMPLE_RATE * process_capture.CHANNELS * 2   # int16
PREVIEW_PREFIX = "replay:clip:"


def seconds_label(seconds: int) -> str:
    """保留長度的顯示文字（30 秒、3 分鐘…）。

    Args:
        seconds (int): 秒數。

    Returns:
        str: 顯示文字。
    """
    if seconds < 60:
        return tr("{seconds} 秒", seconds=seconds)
    minutes = seconds // 60 if seconds % 60 == 0 else f"{seconds / 60:.1f}"
    return tr("{minutes} 分鐘", minutes=minutes)


class ReplayPage(CardPage):
    """回放錄音頁，同時擁有背景錄音器（:class:`ReplayRecorder`）。"""

    def __init__(self, parent: tk.Misc, app: SoundboardApp) -> None:
        """
        Args:
            parent (tk.Misc): 父元件（內容區）。
            app (SoundboardApp): 主程式。
        """
        super().__init__(parent, app, tr("回放錄音"),
                         tr("持續錄製所選來源最近一段時間的聲音（僅保留於記憶體），按一下「擷取」後才會分軌儲存為片段。"))
        self.settings = app.settings
        self.recorder: ReplayRecorder | None = None
        self._results: queue.Queue[tuple[str, object]] = queue.Queue()
        self._app_vars: dict[str, tk.BooleanVar] = {}
        self._playing_apps: set[str] = set()
        self._mic_tag: tk.Label | None = None
        self._listing = False
        self._editors: list[ClipEditor] = []
        self._capturing = False
        self.supported = process_capture.supported()
        self._build_recorder_card(self.body)
        self._build_apps_card(self.body)
        self._build_clips_card(self.body)
        self._apply_enabled(initial=True)
        self.refresh_apps()
        self.refresh_clips()
        self._tick()

    # ================================================================== 版面
    def _build_recorder_card(self, parent: tk.Misc) -> None:
        m = self.app.metrics
        grid = self._card(parent, tr("⏺ 即時回放"),
                          tr("開啟後持續錄製下方勾選的來源，僅保留最近一段時間，較舊的內容會自動捨棄。"
                             "每個來源各為一條音軌，編輯時可個別保留或調整音量。"))
        self.enabled_var = tk.BooleanVar(value=self.settings.replay_enabled and self.supported)
        self._switch(grid, 0, self.enabled_var, tr("開啟即時回放錄音"),
                     tr("錄音僅保留於記憶體，關閉程式後即清除；按一下「擷取」才會儲存為檔案"), self._apply_enabled)
        self._label(grid, 1, tr("保留長度"))
        row = tk.Frame(grid, bg=CARD)
        row.grid(row=1, column=1, columnspan=3, sticky="w", pady=m.px(5))
        self.length_var = tk.StringVar(value=seconds_label(self.settings.replay_seconds))
        self._combo(row, self.length_var, self._on_length_changed,
                    [seconds_label(s) for s in REPLAY_SECONDS_CHOICES], width=10).pack(side="left")
        self.memory_label = tk.Label(row, font=FONT_SMALL, bg=CARD, fg=FG_SUBTLE)
        self.memory_label.pack(side="left", padx=(m.px(10), 0))

        self._label(grid, 2, tr("狀態"))
        self.state_label = tk.Label(grid, font=FONT_BODY, bg=CARD, fg=FG_MUTED, anchor="w", justify="left")
        self.state_label.grid(row=2, column=1, columnspan=3, sticky="w", pady=m.px(5))

        buttons = tk.Frame(grid, bg=CARD)
        buttons.grid(row=3, column=0, columnspan=4, sticky="w", pady=(m.px(10), 0))
        self.capture_btn = self._button(buttons, "", self.capture, ACCENT, ACCENT_HOVER)
        self.capture_btn.pack(side="left")
        tk.Label(buttons, text=tr("也可在首頁的「⌨ 快速鍵」中為「⏺ 擷取回放錄音」設定全域快速鍵（遊戲中亦可使用）"),
                 font=FONT_SMALL, bg=CARD, fg=FG_SUBTLE).pack(side="left", padx=(m.px(12), 0))
        if not self.supported:
            self.state_label.configure(text=tr("⚠ 僅錄製特定程式的聲音需要 Windows 10 2004（19041）或以上版本"),
                                       fg=WARN)
        self._update_length_ui()

    def _build_apps_card(self, parent: tk.Misc) -> None:
        m = self.app.metrics
        grid = self._card(parent, tr("🎯 錄音來源"),
                          tr("可同時勾選多個來源，例如我的麥克風、Discord、瀏覽器（YouTube）。"
                             "勾選的來源在下次啟動程式時會自動開始錄製。"))
        self.apps_frame = tk.Frame(grid, bg=CARD)
        self.apps_frame.grid(row=0, column=0, columnspan=4, sticky="we")
        self.apps_hint = tk.Label(grid, font=FONT_SMALL, bg=CARD, fg=FG_SUBTLE, anchor="w", justify="left",
                                  text=tr("若清單中沒有要錄製的程式，請先讓該程式發出聲音（例如加入語音頻道或播放影片），"
                                          "再按一下「重新整理」。"))
        self.apps_hint.grid(row=1, column=0, columnspan=3, sticky="w", pady=(m.px(8), 0))
        self._button(grid, tr("🔄 重新整理"), self.refresh_apps).grid(row=1, column=3, sticky="e",
                                                               pady=(m.px(8), 0))

    def _build_clips_card(self, parent: tk.Misc) -> None:
        m = self.app.metrics
        grid = self._card(parent, tr("🎬 已擷取的片段"),
                          tr("▶ 試聽（僅本機監聽）　✂ 修剪後儲存為新音效　⬇ 另存新檔　🗑 刪除"))
        self.clips_frame = tk.Frame(grid, bg=CARD)
        self.clips_frame.grid(row=0, column=0, columnspan=4, sticky="we")
        self._button(grid, tr("📂 開啟片段資料夾"), self.open_folder).grid(row=1, column=0, sticky="w",
                                                                  pady=(m.px(10), 0))

    # ================================================================== 錄音器
    @property
    def seconds(self) -> int:
        """目前設定的保留秒數。"""
        return int(self.settings.replay_seconds)

    def _update_length_ui(self) -> None:
        mb = BYTES_PER_SECOND * self.seconds / 1024 / 1024
        self.memory_label.configure(text=tr("每個來源最多使用約 {size} MB 記憶體", size=f"{mb:.0f}"))
        self.capture_btn.set_text(tr("⏺ 擷取最近 {length}", length=seconds_label(self.seconds)))

    def _on_length_changed(self) -> None:
        text = self.length_var.get()
        seconds = next((s for s in REPLAY_SECONDS_CHOICES if seconds_label(s) == text), self.seconds)
        if seconds == self.seconds:
            return
        if self.recorder is not None and self.recorder.buffered_seconds() > 5 and not messagebox.askyesno(
                tr("變更保留長度"), tr("變更保留長度會清除目前已錄製的聲音。確定要變更嗎？"), parent=self):
            self.length_var.set(seconds_label(self.seconds))
            return
        self.settings.replay_seconds = seconds
        self.app.save()
        self._update_length_ui()
        if self.recorder is not None:
            self.recorder.configure(seconds, self.settings.replay_apps)

    def _apply_enabled(self, initial: bool = False) -> None:
        enabled = bool(self.enabled_var.get()) and self.supported
        if not initial:
            self.settings.replay_enabled = enabled
            self.app.save()
        if enabled and self.recorder is None:
            self.recorder = ReplayRecorder(self.seconds, self.settings.replay_apps, mic_hook=self._set_mic_sink)
            self.recorder.start()
            logger.info("回放錄音已開啟：%s，保留 %d 秒", self.settings.replay_apps, self.seconds)
        elif not enabled and self.recorder is not None:
            self.recorder.stop()
            self.recorder = None
            logger.info("回放錄音已關閉")
        self._update_state()

    def _set_mic_sink(self, sink: Callable[[np.ndarray], None] | None) -> None:
        # 接在音訊引擎的麥克風串流上：錄到的是降噪後、朋友實際聽到的你的聲音。
        # 引擎平常只在「直通到虛擬麥克風」等需要時才開麥克風，所以要請設定頁重新判斷一次
        self.app.engine.mic_record_sink = sink
        self.app.settings_page.refresh_mic_input()

    def _mic_hint(self) -> str:
        """「我的麥克風」那一列的說明：依設定頁選的真實麥克風。"""
        # 下拉選單上的文字可能已翻譯：先換回儲存值再比對，顯示時再翻譯（兩種形式都接受）
        label = label_from_display(self.app.settings_page.mic_input_label)
        if not label or label == LABEL_NONE:
            return tr("（⚠ 尚未在設定頁選擇實體麥克風）")
        return tr("（{device}；錄製降噪後、通話對象聽到的聲音）", device=display_label(label))

    def shutdown(self) -> None:
        """關閉程式時停止錄音與編輯器。"""
        for editor in self._editors:
            if editor.alive:
                editor.close()
        if self.recorder is not None:
            self.recorder.stop()
            self.recorder = None

    # ================================================================== 程式清單
    def refresh_apps(self) -> None:
        """在背景重新列出正在發出聲音的程式。"""
        if self._listing or not self.supported:
            self._rebuild_app_rows()
            return
        self._listing = True
        self.app.submit(lambda: self._results.put(("apps", process_capture.list_audio_apps())))

    def _rebuild_app_rows(self) -> None:
        m = self.app.metrics
        for child in self.apps_frame.winfo_children():
            child.destroy()
        chosen = {a.lower() for a in self.settings.replay_apps}
        names: dict[str, str] = {a.lower(): a for a in self.settings.replay_apps if a != MIC_SOURCE}
        for name in sorted(self._playing_apps, key=str.casefold):
            names.setdefault(name.lower(), name)
        self._app_vars = {}
        playing = {p.lower() for p in self._playing_apps}
        # 麥克風固定排第一個
        entries = [(MIC_SOURCE.lower(), MIC_SOURCE)] + sorted(names.items(), key=lambda kv: kv[1].casefold())
        for key, name in entries:
            row = tk.Frame(self.apps_frame, bg=CARD)
            row.pack(fill="x", pady=m.px(3))
            var = tk.BooleanVar(value=key in chosen)
            self._app_vars[name] = var
            switch = ToggleSwitch(row, var, lambda n=name: self._on_app_toggled(n), surface=CARD, metrics=m)
            switch.pack(side="left")
            shown = f"🎙 {source_label(name)}" if name == MIC_SOURCE else name
            label = tk.Label(row, text=shown, font=FONT_BODY, bg=CARD, fg=FG, cursor="hand2")
            label.pack(side="left", padx=(m.px(12), m.px(8)))
            label.bind("<Button-1>", lambda _e, s=switch: s.toggle())
            tag = tk.Label(row, font=FONT_SMALL, bg=CARD, fg=FG_SUBTLE)
            tag.pack(side="left")
            if name == MIC_SOURCE:
                tag.configure(text=self._mic_hint())
                self._mic_tag = tag
            else:
                tag.configure(text=tr("（有音訊工作階段）") if key in playing else tr("（目前未執行或沒有聲音）"))

    def _on_app_toggled(self, name: str) -> None:
        apps = [a for a in self.settings.replay_apps if a.lower() != name.lower()]
        if self._app_vars[name].get():
            apps.append(name)
        self.settings.replay_apps = apps
        self.app.save()
        if self.recorder is not None:
            self.recorder.configure(self.seconds, apps)
        self._update_state()

    # ================================================================== 狀態
    def _update_state(self) -> None:
        if not self.supported:
            return
        # 設定頁換了麥克風時，說明文字跟著更新（不必整個清單重建）
        if self._mic_tag is not None and self._mic_tag.winfo_exists():
            hint = self._mic_hint()
            if self._mic_tag.cget("text") != hint:
                self._mic_tag.configure(text=hint)
        if self.recorder is None:
            text, color = tr("未開啟"), FG_MUTED
        elif not self.settings.replay_apps:
            text, color = tr("⚠ 尚未勾選錄音來源"), WARN
        else:
            parts = []
            for status in self.recorder.status():
                mark = {"recording": tr("🔴 錄音中"), "waiting": tr("⏸ 等待中"),
                        "error": tr("⚠ 無法錄音")}[status.state]
                source = source_label(status.source)
                if status.state != "recording" and status.detail:
                    parts.append(tr("{source}：{state}（{detail}）", source=source, state=mark, detail=status.detail))
                else:
                    parts.append(tr("{source}：{state}", source=source, state=mark))
            buffered = self.recorder.buffered_seconds()
            parts.append(tr("目前可擷取 {available}／最長 {maximum}", available=format_seconds(buffered)[:-3],
                            maximum=seconds_label(self.seconds)))
            text = "\n".join(parts)
            recording = any(s.state == "recording" for s in self.recorder.status())
            color = OK if recording else WARN
        self.state_label.configure(text=text, fg=color)
        self.app.set_replay_chip(self._chip_text())

    def _chip_text(self) -> tuple[str, str] | None:
        if self.recorder is None:
            return None
        recording = [s.source for s in self.recorder.status() if s.state == "recording"]
        if len(recording) == 1:
            return tr("回放錄音：{source}", source=source_label(recording[0])), ERR
        if recording:
            # 側邊欄很窄，列出多個名稱會被斷行；詳細清單在回放錄音頁
            return tr("回放錄音：錄音中（{count} 個來源）", count=len(recording)), ERR
        return tr("回放錄音：等待來源"), FG_SUBTLE

    def _tick(self) -> None:
        try:
            while True:
                kind, value = self._results.get_nowait()
                if kind == "apps":
                    self._listing = False
                    self._playing_apps = set(value)  # type: ignore[arg-type]
                    self._rebuild_app_rows()
                elif kind == "saved":
                    self._capturing = False
                    path, seconds = value  # type: ignore[misc]
                    self.refresh_clips()
                    self.app.set_status(tr("⏺ 已擷取 {seconds} 秒的回放：{name}（可在「回放錄音」頁修剪）",
                                           seconds=f"{seconds:.0f}", name=path.name), OK)
                elif kind == "error":
                    self._capturing = False
                    self.app.set_status(tr("擷取失敗：{error}", error=value), ERR)
        except queue.Empty:
            pass
        self._editors = [e for e in self._editors if e.alive]
        self._update_state()
        self.after(STATUS_TICK_MS, self._tick)

    # ================================================================== 擷取
    def capture(self) -> bool:
        """把最近的聲音存成片段（在背景寫檔，不會卡住介面）。

        Returns:
            bool: 有開始儲存為 True。
        """
        if self.recorder is None:
            self.app.set_status(tr("⏺ 回放錄音未開啟（請在「回放錄音」頁開啟）"), WARN)
            return False
        if self._capturing:
            return False
        tracks = self.recorder.snapshot_tracks()
        if not tracks or not any(np.any(audio) for _src, audio in tracks):
            self.app.set_status(tr("⏺ 尚未錄到任何聲音"), WARN)
            return False
        self._capturing = True
        folder = clips_dir(settings_path())
        seconds = len(tracks[0][1]) / process_capture.SAMPLE_RATE

        def work() -> None:
            try:
                self._results.put(("saved", (save_clip(tracks, folder), seconds)))
            except (OSError, RuntimeError, ValueError) as exc:
                self._results.put(("error", str(exc)))

        self.app.submit(work)
        self.app.set_status(tr("⏺ 擷取中…"), FG_MUTED)
        return True

    # ================================================================== 片段清單
    def refresh_clips(self) -> None:
        """重新列出已擷取的片段。"""
        m = self.app.metrics
        for child in self.clips_frame.winfo_children():
            child.destroy()
        clips = list_clips(clips_dir(settings_path()))
        if not clips:
            tk.Label(self.clips_frame, text=tr("尚無片段。開啟回放錄音後按一下「擷取」，最近的聲音即會儲存於此。"),
                     font=FONT_BODY, bg=CARD, fg=FG_MUTED, anchor="w").pack(fill="x")
            return
        for clip in clips[:MAX_LISTED_CLIPS]:
            row = tk.Frame(self.clips_frame, bg=CARD)
            row.pack(fill="x", pady=m.px(3))
            tk.Label(row, text=f"{clip.created:%m/%d %H:%M:%S}", font=FONT_SMALL_BOLD, bg=CARD, fg=FG).pack(
                side="left")
            tk.Label(row, text=f"{clip.label}　{format_seconds(clip.seconds)[:-3]}", font=FONT_SMALL, bg=CARD,
                     fg=FG_MUTED).pack(side="left", padx=(m.px(10), 0))
            for text, command, bg, hover in (
                ("🗑", lambda c=clip: self.delete_clip(c), CARD, ERR_HOVER),
                ("⬇", lambda c=clip: self.download_clip(c), None, None),
                (tr("✂ 編輯"), lambda c=clip: self.edit_clip(c), ACCENT, ACCENT_HOVER),
                ("▶", lambda c=clip: self.preview_clip(c), None, None),
            ):
                btn = self._button(row, text, command) if bg is None else self._button(row, text, command, bg, hover)
                btn.pack(side="right", padx=(m.px(6), 0))
        if len(clips) > MAX_LISTED_CLIPS:
            tk.Label(self.clips_frame,
                     text=tr("…另有 {count} 個較舊的片段，請開啟資料夾查看", count=len(clips) - MAX_LISTED_CLIPS),
                     font=FONT_SMALL, bg=CARD, fg=FG_SUBTLE, anchor="w").pack(fill="x")

    def preview_clip(self, clip: ClipInfo) -> None:
        """試聽片段（只有自己聽得到）；正在播放時停止。"""
        key = PREVIEW_PREFIX + clip.path.name
        if key in self.app.engine.active_keys():
            self.app.engine.stop(key)
            return
        try:
            # 回放錄到的聲音常比平常聽到的小很多：每軌自動調整音量後再混音
            audio = auto_mix(load_clip(clip.path))
        except (OSError, RuntimeError) as exc:
            self.app.set_status(tr("無法讀取片段：{error}", error=exc), ERR)
            return
        self.app.engine.play(key, audio, to_mic=False)
        if not self.app.engine.monitor_active:
            self.app.set_status(tr("⚠ 尚未在設定頁選擇監聽裝置，試聽將不會有聲音"), WARN)
        else:
            self.app.set_status(tr("▶ 正在試聽 {name}（已自動調整音量；再按一下 ▶ 即可停止）", name=clip.path.name),
                                FG_MUTED)

    def edit_clip(self, clip: ClipInfo) -> ClipEditor | None:
        """開啟片段編輯器。"""
        for editor in self._editors:
            if editor.alive and editor.path == clip.path:
                editor.focus()
                return editor
        try:
            editor = ClipEditor(self.app, clip.path)
        except (OSError, RuntimeError) as exc:
            self.app.set_status(tr("無法開啟片段：{error}", error=exc), ERR)
            return None
        self._editors.append(editor)
        return editor

    def download_clip(self, clip: ClipInfo) -> Path | None:
        """把整個片段（所有音軌各自自動調整音量後混音）另存到使用者選的位置與格式。"""
        types = [(fmt, f"*{suffix}") for suffix, (fmt, _sub) in EXPORT_FORMATS.items()]
        target = filedialog.asksaveasfilename(parent=self, title=tr("另存片段"), defaultextension=".ogg",
                                              initialfile=clip.path.stem, filetypes=types)
        if not target:
            return None
        path = Path(target)
        if path.suffix.lower() not in EXPORT_FORMATS:
            path = path.with_name(path.name + ".ogg")
        try:
            export_audio(auto_mix(load_clip(clip.path)), path)
        except (OSError, RuntimeError, ValueError) as exc:
            self.app.set_status(tr("另存失敗：{error}", error=exc), ERR)
            return None
        self.app.set_status(tr("✔ 已另存：{path}", path=path), OK)
        return path

    def delete_clip(self, clip: ClipInfo) -> None:
        """確認後刪除片段。"""
        if not messagebox.askyesno(tr("刪除片段"), tr("確定要刪除「{name}」嗎？此操作無法復原。", name=clip.path.name),
                                   parent=self):
            return
        self.app.engine.stop(PREVIEW_PREFIX + clip.path.name)
        try:
            delete_clip(clip.path)
        except OSError as exc:
            self.app.set_status(tr("刪除失敗：{error}", error=exc), ERR)
            return
        self.refresh_clips()

    def open_folder(self) -> None:
        """在檔案總管開啟片段資料夾。"""
        folder = clips_dir(settings_path())
        folder.mkdir(parents=True, exist_ok=True)
        if hasattr(os, "startfile"):
            os.startfile(folder)  # noqa: S606 - 開啟自己的資料夾
