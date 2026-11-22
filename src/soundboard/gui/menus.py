"""首頁的右鍵選單：音效按鈕與群組標題。"""

from __future__ import annotations

import tkinter as tk
from typing import TYPE_CHECKING

from soundboard.i18n import tr
from soundboard.models import SoundItem
from soundboard.theme import ACCENT, FG, PANEL

if TYPE_CHECKING:
    from soundboard.gui.app import SoundboardApp


def _menu(parent: tk.Misc) -> tk.Menu:
    return tk.Menu(parent, tearoff=0, bg=PANEL, fg=FG, activebackground=ACCENT, activeforeground=FG, bd=0)


def _popup(menu: tk.Menu, event: tk.Event) -> None:
    try:
        menu.tk_popup(event.x_root, event.y_root)
    finally:
        menu.grab_release()


def show_sound_menu(app: SoundboardApp, event: tk.Event, item: SoundItem) -> None:
    """顯示音效按鈕的右鍵選單。

    在已複選的音效上按右鍵時，「移到群組」會套用到全部選取的音效。

    Args:
        app (SoundboardApp): 主程式。
        event (tk.Event): 滑鼠事件（決定選單位置）。
        item (SoundItem): 被按的音效。
    """
    editor, settings = app.editor, app.settings
    menu = _menu(app.root)
    menu.add_command(label=tr("▶ 播放（通話對象＋監聽）"), command=lambda: app.play(item))
    menu.add_command(label=tr("🎧 試聽（僅本機）　Shift＋左鍵"), command=lambda: app.play(item, to_mic=False))
    menu.add_separator()
    target = f"sound:{item.key}"
    hotkey = app.hotkeys.text_for_sound(item.key)
    if hotkey:
        edit_label = tr("⌨ 設定全域快速鍵…（目前：{hotkey}）", hotkey=hotkey)
    else:
        edit_label = tr("⌨ 設定全域快速鍵…（目前：無）")
    menu.add_command(label=edit_label, command=lambda: app.hotkeys.edit(target))
    if hotkey:
        menu.add_command(label=tr("⌨ 清除快速鍵"), command=lambda: app.hotkeys.clear(target))
    pinned = item.key in settings.overlay_keys
    menu.add_command(label=tr("📌 從浮動視窗移除") if pinned else tr("📌 加入浮動視窗"),
                     command=lambda: editor.toggle_pin(item))
    _add_group_submenu(app, menu, app.home.menu_targets(item))
    if item.key == settings.join_sound_key:
        menu.add_command(label=tr("🚪 取消進場音效"), command=lambda: editor.set_join_sound(None))
    else:
        menu.add_command(label=tr("🚪 設為進場音效（進入 Discord 語音時播放）"),
                         command=lambda: editor.set_join_sound(item.key))
    menu.add_separator()
    menu.add_command(label=tr("✏ 重新命名…（同時修改檔名）"), command=lambda: editor.rename(item))
    menu.add_command(label=tr("✂ 編輯／後製…（修剪、音量、淡入淡出、另存）"), command=lambda: app.open_sound_editor(item))
    menu.add_command(label=tr("📂 在檔案總管中顯示"), command=lambda: editor.reveal(item))
    menu.add_separator()
    targets = app.home.menu_targets(item)
    # 單數與複數各寫成完整的句子（翻譯時語序與詞形可能不同）
    if len(targets) > 1:
        delete_label = tr("🗑 刪除這 {count} 個音效…（檔案將移至資源回收筒）", count=len(targets))
    else:
        delete_label = tr("🗑 刪除音效…（檔案將移至資源回收筒）")
    menu.add_command(label=delete_label, command=lambda: editor.delete_sounds(targets))
    _popup(menu, event)


def _add_group_submenu(app: SoundboardApp, menu: tk.Menu, keys: list[str]) -> None:
    """在音效右鍵選單中加入「移到群組」子選單。"""
    editor, settings = app.editor, app.settings
    current = {settings.group_of(k) for k in keys}
    sub = _menu(menu)
    for name in settings.sound_groups:
        mark = "● " if current == {name} else "　 "
        sub.add_command(label=f"{mark}{name}", command=lambda g=name: editor.move_to_group(keys, g))
    if settings.sound_groups:
        sub.add_separator()
    sub.add_command(label=tr("＋ 新增群組…"), command=lambda: editor.new_group(keys))
    if current != {None}:
        sub.add_command(label=tr("↩ 移出群組（未分組）"), command=lambda: editor.move_to_group(keys, None))
    if len(keys) > 1:
        label = tr("🗂 移到群組（已選取 {count} 個）", count=len(keys))
    else:
        label = tr("🗂 移到群組")
    menu.add_cascade(label=label, menu=sub)


def show_group_menu(app: SoundboardApp, event: tk.Event, group: str | None) -> None:
    """顯示群組標題列的右鍵選單。

    Args:
        app (SoundboardApp): 主程式。
        event (tk.Event): 滑鼠事件。
        group (str | None): 群組名稱；None 表示「未分組」。
    """
    editor = app.editor
    menu = _menu(app.root)
    if group is not None:
        keys = [k for k in app.settings.sound_groups.get(group, []) if k in app.items_by_key]
        menu.add_command(label=tr("✏ 重新命名群組…"), command=lambda: editor.rename_group(group))
        menu.add_command(label=tr("⬆ 上移"), command=lambda: editor.shift_group(group, -1))
        menu.add_command(label=tr("⬇ 下移"), command=lambda: editor.shift_group(group, 1))
        menu.add_separator()
        menu.add_command(label=tr("📌 將整個群組加入浮動視窗"), state="normal" if keys else "disabled",
                         command=lambda: editor.pin_keys(keys))
        menu.add_command(label=tr("🗑 刪除群組（音效將移回未分組，檔案不會刪除）"),
                         command=lambda: editor.delete_group(group))
        menu.add_separator()
    menu.add_command(label=tr("＋ 新增群組…"), command=editor.new_group)
    menu.add_command(label=tr("全部展開"), command=lambda: editor.set_all_collapsed(False))
    menu.add_command(label=tr("全部收合"), command=lambda: editor.set_all_collapsed(True))
    _popup(menu, event)
