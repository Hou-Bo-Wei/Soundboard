"""全域快捷鍵的設定、註冊與觸發。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from soundboard.dialogs import HotkeyCaptureDialog, HotkeyRow, HotkeySettingsDialog
from soundboard.gui.presenters import ACTIONS
from soundboard.hotkeys import GlobalHotkeys, format_hotkey
from soundboard.i18n import tr
from soundboard.models import Hotkey
from soundboard.theme import FG_MUTED, OK, WARN

if TYPE_CHECKING:
    from soundboard.gui.app import SoundboardApp


class HotkeyController:
    """管理功能快捷鍵與音效快捷鍵。

    綁定對象以字串表示：``"action:stop"``、``"sound:<音效 key>"``。
    """

    def __init__(self, app: SoundboardApp) -> None:
        """
        Args:
            app (SoundboardApp): 主程式。
        """
        self.app = app
        self.hotkeys = GlobalHotkeys(self._on_global_hotkey)
        # 註冊 id → 綁定對象
        self._targets: dict[int, str] = {}
        self.failed: list[str] = []
        self._dialog: HotkeySettingsDialog | None = None

    # ------------------------------------------------------------------ 生命週期
    def start(self) -> None:
        """啟動快捷鍵執行緒並註冊設定中的快捷鍵。"""
        self.hotkeys.start()
        self.apply()

    def stop(self) -> None:
        """停止快捷鍵執行緒。"""
        self.hotkeys.stop()

    @property
    def has_bindings(self) -> bool:
        """目前是否有任何已註冊或已設定的音效快捷鍵（換資料夾後需要重新註冊）。"""
        return bool(self._targets or self.app.settings.sound_hotkeys)

    # ------------------------------------------------------------------ 查詢
    def get(self, target: str) -> Hotkey | None:
        """取得某個對象的快捷鍵。

        Args:
            target (str): 綁定對象。

        Returns:
            Hotkey | None: 快捷鍵；沒有設定時為 None。
        """
        kind, _, name = target.partition(":")
        store = self.app.settings.action_hotkeys if kind == "action" else self.app.settings.sound_hotkeys
        return Hotkey.from_json(store.get(name))

    def text_for_sound(self, key: str) -> str:
        """音效快捷鍵的顯示文字（沒有時為空字串）。

        Args:
            key (str): 音效 key。

        Returns:
            str: 例如 ``"Ctrl+Alt+1"``。
        """
        return format_hotkey(self.get(f"sound:{key}"))

    def label(self, target: str) -> str:
        """綁定對象的顯示名稱。

        Args:
            target (str): 綁定對象。

        Returns:
            str: 功能名稱或音效名稱。
        """
        kind, _, name = target.partition(":")
        if kind == "action":
            # ACTIONS 是模組常數（只標記），顯示時才翻譯；未知的功能代號原樣顯示
            return tr(ACTIONS[name]) if name in ACTIONS else name
        item = self.app.items_by_key.get(name)
        return item.display_name if item else name

    def _store(self, target: str, hotkey: Hotkey | None) -> None:
        kind, _, name = target.partition(":")
        store = self.app.settings.action_hotkeys if kind == "action" else self.app.settings.sound_hotkeys
        if hotkey is None:
            store.pop(name, None)
        else:
            store[name] = hotkey.to_json()

    def _all_targets(self) -> list[str]:
        settings = self.app.settings
        # 只註冊現有的功能：設定檔可能還留著已移除功能的快速鍵（例如舊版的變聲器開關）
        actions = [f"action:{a}" for a in settings.action_hotkeys if a in ACTIONS]
        return actions + [f"sound:{k}" for k in settings.sound_hotkeys]

    # ------------------------------------------------------------------ 修改
    def edit(self, target: str) -> None:
        """開啟按鍵擷取對話框，設定某個對象的快捷鍵。

        Args:
            target (str): 綁定對象，例如 ``"action:stop"``、``"sound:a.ogg"``。
        """
        # 擷取期間先取消所有全域快捷鍵，否則按下已註冊的組合鍵會被攔截、對話框收不到
        self.hotkeys.apply({})
        try:
            dialog = HotkeyCaptureDialog(self.app.root, self.label(target), self.get(target))
        finally:
            self.apply()
        if not dialog.changed:
            return
        if dialog.result is None:
            self.clear(target)
            return
        # 同一組按鍵只能綁一個對象：從其他對象身上移除
        moved_from = [t for t in self._all_targets() if t != target and self.get(t) == dialog.result]
        for other in moved_from:
            self._store(other, None)
        self._store(target, dialog.result)
        self._after_changed()
        name, keys = self.label(target), format_hotkey(dialog.result)
        if moved_from:
            others = "、".join(self.label(t) for t in moved_from)
            text = tr("⌨ 已將「{name}」的快速鍵設為 {hotkey}（已從「{others}」移除）",
                      name=name, hotkey=keys, others=others)
        else:
            text = tr("⌨ 已將「{name}」的快速鍵設為 {hotkey}", name=name, hotkey=keys)
        if not self.failed:
            self.app.set_status(text, OK)

    def clear(self, target: str) -> None:
        """清除某個對象的快捷鍵。

        Args:
            target (str): 綁定對象。
        """
        self._store(target, None)
        self._after_changed()
        self.app.set_status(tr("已清除「{name}」的快速鍵", name=self.label(target)), FG_MUTED)

    def _after_changed(self) -> None:
        self.apply()
        self.app.home.refresh_tile_texts()
        self.app.refresh_overlay()
        self.refresh_dialog()
        self.app.save()

    def apply(self) -> None:
        """依設定重新註冊所有全域快捷鍵。"""
        if not self.hotkeys.supported:
            return
        bindings: dict[int, Hotkey] = {}
        targets: dict[int, str] = {}
        for target in self._all_targets():
            kind, _, name = target.partition(":")
            hotkey = self.get(target)
            # 音效檔被刪除或換了資料夾時先不註冊，但保留設定以便之後檔案回來
            if hotkey is None or (kind == "sound" and name not in self.app.items_by_key):
                continue
            hotkey_id = len(bindings) + 1
            bindings[hotkey_id] = hotkey
            targets[hotkey_id] = target
        self._targets = targets
        failed = self.hotkeys.apply(bindings)
        self.failed = [f"{self.label(targets[i])}（{format_hotkey(bindings[i])}）" for i in failed]
        if self.failed:
            self.app.set_status(tr("⚠ 下列快速鍵已被其他程式使用，請改用其他按鍵組合：{hotkeys}",
                                   hotkeys="、".join(self.failed)), WARN)

    # ------------------------------------------------------------------ 觸發
    def _on_global_hotkey(self, hotkey_id: int) -> None:
        """快捷鍵被按下（在快捷鍵執行緒中執行，不可直接操作 Tk）。"""
        target = self._targets.get(hotkey_id)
        if target is None:
            return
        kind, _, name = target.partition(":")
        app = self.app
        if kind == "sound":
            item = app.items_by_key.get(name)
            if item is not None:
                # 直接在這條執行緒開始播放，不必等主執行緒輪詢，延遲最低
                text, color = app.trigger(item, to_mic=not app.preview_mode)
                app.post_message("status", f"{color}|⌨ {text}")
        elif name == "stop":
            app.engine.stop()
            # tr 只查字典，在快捷鍵執行緒呼叫也安全；⌨ 只是標示「由快捷鍵觸發」的圖示
            app.post_message("status", f"{FG_MUTED}|⌨ {tr('⏹ 已停止所有音效')}")
        else:
            # 切換小視窗 / 預覽模式會動到 Tk 元件，交回主執行緒處理
            app.post_message("action", name)

    # ------------------------------------------------------------------ 設定視窗
    def open_dialog(self) -> None:
        """開啟快捷鍵設定視窗（已開著就拉到前面）。"""
        if self._dialog and self._dialog.alive:
            self._dialog.focus()
            return
        self._dialog = HotkeySettingsDialog(
            self.app.root, self._rows, self.edit, self.clear,
            lambda: (tr("⚠ 下列快速鍵無法註冊（已被其他程式使用）：{hotkeys}", hotkeys="、".join(self.failed))
                     if self.failed else ""),
        )

    def _rows(self) -> tuple[list[HotkeyRow], list[HotkeyRow]]:
        actions = [HotkeyRow(f"action:{a}", self.label(f"action:{a}"), self.get(f"action:{a}")) for a in ACTIONS]
        sounds = [HotkeyRow(f"sound:{k}", self.label(f"sound:{k}"), Hotkey.from_json(v))
                  for k, v in self.app.settings.sound_hotkeys.items() if k in self.app.items_by_key]
        sounds.sort(key=lambda r: r.label.casefold())
        return actions, sounds

    def refresh_dialog(self) -> None:
        """設定視窗開著時重新整理內容。"""
        if self._dialog and self._dialog.alive:
            self._dialog.refresh()
