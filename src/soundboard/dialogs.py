"""快捷鍵相關對話框。"""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable
from dataclasses import dataclass
from tkinter import messagebox

from soundboard.hotkeys import MODIFIER_VKS, blocks_typing, current_modifiers, format_hotkey
from soundboard.i18n import tr
from soundboard.models import Hotkey
from soundboard.theme import BG, BG_DARK, BTN, BTN_HOVER, BTN_PLAYING, FG, FG_MUTED, FONT_FAMILY, WARN

VK_ESCAPE = 0x1B


def _button(parent: tk.Misc, text: str, command: Callable[[], None], bg: str = BTN) -> tk.Button:
    return tk.Button(parent, text=text, command=command, bg=bg, fg=FG, activebackground=BTN_HOVER,
                     activeforeground=FG, relief="flat", bd=0, padx=10, pady=4, cursor="hand2",
                     font=(FONT_FAMILY, 9))


class HotkeyCaptureDialog:
    """強制回應對話框：請使用者按下想要的按鍵組合。

    Attributes:
        result (Hotkey | None): 使用者選擇的快捷鍵；按「清除」時為 None。
        changed (bool): 使用者是否做了選擇（False 表示取消）。
    """

    def __init__(self, parent: tk.Misc, target_name: str, current: Hotkey | None) -> None:
        """建立並顯示對話框，直到使用者完成或取消才返回。

        Args:
            parent (tk.Misc): 父視窗。
            target_name (str): 要設定快捷鍵的對象名稱（顯示用）。
            current (Hotkey | None): 目前的快捷鍵。
        """
        self.result: Hotkey | None = current
        self.changed = False

        self.top = tk.Toplevel(parent, bg=BG, padx=20, pady=16)
        self.top.title(tr("設定全域快速鍵"))
        self.top.resizable(False, False)
        self.top.transient(parent)

        tk.Label(self.top, text=tr("設定「{name}」的快速鍵", name=target_name), bg=BG, fg=FG,
                 font=(FONT_FAMILY, 11, "bold")).pack(anchor="w")
        current_text = format_hotkey(current)
        if current_text:
            current_text = tr("目前：{hotkey}", hotkey=current_text)
        else:
            current_text = tr("目前：（無）")
        tk.Label(self.top, text=current_text, bg=BG, fg=FG_MUTED,
                 font=(FONT_FAMILY, 9)).pack(anchor="w", pady=(2, 10))
        self.preview = tk.Label(self.top, text=tr("請按下按鍵組合…"), bg=BG_DARK, fg=FG, width=28, pady=14,
                                font=(FONT_FAMILY, 14, "bold"))
        self.preview.pack(fill="x")
        tk.Label(self.top, justify="left", bg=BG, fg=FG_MUTED, font=(FONT_FAMILY, 9), text=tr(
            "建議使用 Ctrl／Alt 組合鍵（例如 Ctrl+Alt+1）、數字鍵盤（Num1）或 F13–F24。\n"
            "音效版會攔截快速鍵，遊戲將收不到該按鍵，請避免使用遊戲的操作鍵。\n"
            "按 Esc 取消。")).pack(anchor="w", pady=(10, 10))
        row = tk.Frame(self.top, bg=BG)
        row.pack(fill="x")
        _button(row, tr("清除快速鍵"), self._clear).pack(side="left")
        _button(row, tr("取消"), self.top.destroy).pack(side="right")

        self.top.bind("<KeyPress>", self._on_key)
        self.top.bind("<KeyRelease>", lambda _e: self._show_modifiers())
        self.top.grab_set()
        self.top.lift()
        self.top.focus_force()
        # 視窗剛建立時 Windows 可能還沒把焦點交給它，稍後再搶一次，確保按鍵會送到這裡
        self.top.after(80, self.top.focus_force)
        parent.wait_window(self.top)

    def _show_modifiers(self) -> None:
        text = format_hotkey(Hotkey(current_modifiers(), 0)).rsplit("+", 1)[0] if current_modifiers() else ""
        self.preview.configure(text=f"{text}+…" if text else tr("請按下按鍵組合…"))

    def _on_key(self, event: tk.Event) -> str:
        # Windows 上 Tk 的 keycode 就是虛擬鍵碼，可以直接交給 RegisterHotKey
        vk = int(event.keycode)
        mods = current_modifiers()
        if vk == VK_ESCAPE and not mods:
            self.top.destroy()
            return "break"
        if vk in MODIFIER_VKS or not 0 < vk < 256:
            self._show_modifiers()
            return "break"
        hotkey = Hotkey(mods, vk)
        self.preview.configure(text=format_hotkey(hotkey), fg=BTN_PLAYING)
        if blocks_typing(hotkey) and not messagebox.askyesno(
            tr("確定要使用此按鍵嗎？"),
            tr("「{hotkey}」未搭配 Ctrl 或 Alt。\n"
               "設定後，在任何地方（包括聊天與遊戲）都將無法輸入此字元。\n\n確定要使用嗎？",
               hotkey=format_hotkey(hotkey)),
            icon="warning", parent=self.top,
        ):
            self.preview.configure(text=tr("請按下按鍵組合…"), fg=FG)
            return "break"
        self.result = hotkey
        self.changed = True
        self.top.destroy()
        return "break"

    def _clear(self) -> None:
        self.result = None
        self.changed = True
        self.top.destroy()


@dataclass(frozen=True)
class HotkeyRow:
    """快捷鍵設定清單中的一列。

    Attributes:
        target (str): 綁定對象 id，例如 ``"action:stop"``、``"sound:xxx.ogg"``。
        label (str): 顯示名稱。
        hotkey (Hotkey | None): 目前的快捷鍵。
    """

    target: str
    label: str
    hotkey: Hotkey | None


class HotkeySettingsDialog:
    """列出所有快捷鍵的設定視窗（非強制回應，可以邊開邊用）。"""

    def __init__(
        self,
        parent: tk.Misc,
        rows: Callable[[], tuple[list[HotkeyRow], list[HotkeyRow]]],
        on_edit: Callable[[str], None],
        on_clear: Callable[[str], None],
        warnings: Callable[[], str],
    ) -> None:
        """
        Args:
            parent (tk.Misc): 父視窗。
            rows (Callable): 回傳（功能快捷鍵列、音效快捷鍵列）。
            on_edit (Callable[[str], None]): 使用者要修改某個對象的快捷鍵。
            on_clear (Callable[[str], None]): 使用者要清除某個對象的快捷鍵。
            warnings (Callable[[], str]): 回傳目前的警告文字（例如註冊失敗的快捷鍵）。
        """
        self._rows = rows
        self._on_edit = on_edit
        self._on_clear = on_clear
        self._warnings = warnings
        self.top = tk.Toplevel(parent, bg=BG, padx=16, pady=12)
        self.top.title(tr("全域快速鍵"))
        self.top.transient(parent)
        self.top.minsize(460, 200)
        self.content = tk.Frame(self.top, bg=BG)
        self.content.pack(fill="both", expand=True)
        self.refresh()

    @property
    def alive(self) -> bool:
        """視窗是否仍開著。"""
        return bool(self.top.winfo_exists())

    def focus(self) -> None:
        """把視窗拉到最前面。"""
        self.top.deiconify()
        self.top.lift()
        self.top.focus_force()

    def refresh(self) -> None:
        """依目前設定重繪清單。"""
        if not self.alive:
            return
        for child in self.content.winfo_children():
            child.destroy()
        actions, sounds = self._rows()
        self._section(tr("功能"), actions)
        self._section(tr("音效（在首頁的音效上按右鍵並選擇「⌨ 設定全域快速鍵」即可新增）"), sounds,
                      empty=tr("尚未設定任何音效快速鍵"))
        tk.Label(self.content, text=tr("快速鍵在遊戲中同樣有效（支援無邊框視窗與全螢幕）。"),
                 bg=BG, fg=FG_MUTED, font=(FONT_FAMILY, 9)).pack(anchor="w", pady=(10, 0))
        warning = self._warnings()
        if warning:
            tk.Label(self.content, text=warning, bg=BG, fg=WARN, justify="left", wraplength=440,
                     font=(FONT_FAMILY, 9)).pack(anchor="w", pady=(6, 0))

    def _section(self, title: str, rows: list[HotkeyRow], empty: str = "") -> None:
        tk.Label(self.content, text=title, bg=BG, fg=FG_MUTED, font=(FONT_FAMILY, 9, "bold")).pack(
            anchor="w", pady=(8, 2))
        box = tk.Frame(self.content, bg=BG_DARK, padx=8, pady=6)
        box.pack(fill="x")
        box.columnconfigure(0, weight=1)
        if not rows and empty:
            tk.Label(box, text=empty, bg=BG_DARK, fg=FG_MUTED, font=(FONT_FAMILY, 9)).grid(row=0, column=0, sticky="w")
        for i, row in enumerate(rows):
            tk.Label(box, text=row.label, bg=BG_DARK, fg=FG, anchor="w", font=(FONT_FAMILY, 10)).grid(
                row=i, column=0, sticky="w", pady=2)
            tk.Label(box, text=format_hotkey(row.hotkey) or "—", bg=BG_DARK,
                     fg=BTN_PLAYING if row.hotkey else FG_MUTED, width=16,
                     font=(FONT_FAMILY, 10, "bold")).grid(row=i, column=1, padx=8)
            _button(box, tr("設定"), lambda t=row.target: self._on_edit(t)).grid(row=i, column=2, padx=2)
            clear = _button(box, tr("清除"), lambda t=row.target: self._on_clear(t))
            clear.grid(row=i, column=3, padx=2)
            if row.hotkey is None:
                clear.configure(state="disabled")
