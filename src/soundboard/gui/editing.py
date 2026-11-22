"""音效與群組的編輯動作：分組、收合、釘選、進場音效、改名、刪除、音量、顯示檔案位置。"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path
from tkinter import messagebox, simpledialog
from collections.abc import Callable
from typing import TYPE_CHECKING

from soundboard.clips import SOUND_MAX_DB, SOUND_MIN_DB, gain_factor
from soundboard.i18n import tr
from soundboard.library import RenameError, move_to_recycle_bin, rename_sound_file
from soundboard.models import AppSettings, SoundItem
from soundboard.theme import ERR, FG_MUTED, OK

if TYPE_CHECKING:
    from soundboard.gui.app import SoundboardApp

logger = logging.getLogger(__name__)

# 確認視窗最多列出幾個音效名稱（太多會超出螢幕）
DELETE_LIST_LIMIT = 12


class LibraryEditor:
    """修改音效庫設定（群組、釘選、進場音效）與音效檔本身的動作。

    這些動作會從首頁的工具列、右鍵選單與拖曳觸發，集中在這裡避免首頁元件同時負責資料修改。
    """

    def __init__(self, app: SoundboardApp) -> None:
        """
        Args:
            app (SoundboardApp): 主程式。
        """
        self.app = app
        # 刪除音效時使用的函式（測試時換成不碰資源回收筒的版本）
        self.trash: Callable[[Path], None] = move_to_recycle_bin

    @property
    def settings(self) -> AppSettings:
        """使用者設定。"""
        return self.app.settings

    # ------------------------------------------------------------------ 群組
    def ask_group_name(self, title: str, initial: str = "") -> str | None:
        """詢問群組名稱，重複或空白時請使用者重新輸入。

        Args:
            title (str): 對話框標題。
            initial (str): 預設文字。

        Returns:
            str | None: 名稱；使用者取消時為 None。
        """
        root = self.app.root
        while True:
            name = simpledialog.askstring(title, tr("群組名稱："), initialvalue=initial, parent=root)
            if name is None:
                return None
            name = name.strip()
            if not name:
                messagebox.showerror(title, tr("群組名稱不可空白"), parent=root)
            elif name != initial and name in self.settings.sound_groups:
                messagebox.showerror(title, tr("已有名為「{name}」的群組", name=name), parent=root)
            else:
                return name
            initial = name

    def new_group(self, keys: list[str] | None = None) -> None:
        """新增群組；有指定音效時直接把它們放進去。

        Args:
            keys (list[str] | None): 要放進新群組的音效。
        """
        name = self.ask_group_name(tr("新增群組"))
        if name is None:
            return
        self.settings.add_group(name)
        if keys:
            self.move_to_group(keys, name)
        else:
            self.after_groups_changed()
            self.app.set_status(tr("已新增群組「{name}」。在音效上按一下右鍵並選擇「🗂 移到群組」，或將音效拖曳至群組",
                                   name=name), OK)

    def move_to_group(self, keys: list[str], group: str | None, before: str | None = None) -> None:
        """把音效移到群組（None 表示未分組），並清除複選。

        Args:
            keys (list[str]): 要移動的音效。
            group (str | None): 目標群組。
            before (str | None): 插在這個音效前面；None 表示放到最後。
        """
        self.settings.move_to_group(keys, group, before)
        self.app.home.clear_selection()
        self.after_groups_changed()
        if group is not None:
            text = tr("已將 {count} 個音效移至「{group}」", count=len(keys), group=group)
        else:
            text = tr("已將 {count} 個音效移至未分組", count=len(keys))
        self.app.set_status(text, OK)

    def after_groups_changed(self) -> None:
        """群組內容變動後重新排列首頁、更新小視窗並存檔。"""
        self.app.home.layout(force=True, keep_scroll=True)
        self.app.refresh_overlay()
        self.app.save()

    def toggle_collapse(self, group_key: str) -> None:
        """收合／展開群組。

        Args:
            group_key (str): 群組名稱；空字串表示未分組。
        """
        collapsed = self.settings.collapsed_groups
        if group_key in collapsed:
            collapsed.remove(group_key)
        else:
            collapsed.append(group_key)
        self.app.home.layout(force=True, keep_scroll=True)
        self.app.save()

    def rename_group(self, group: str) -> None:
        """重新命名群組（不影響音效檔）。

        Args:
            group (str): 群組名稱。
        """
        name = self.ask_group_name(tr("重新命名群組"), group)
        if name is None or name == group:
            return
        self.settings.rename_group(group, name)
        self.after_groups_changed()

    def shift_group(self, group: str, offset: int) -> None:
        """調整群組順序。

        Args:
            group (str): 群組名稱。
            offset (int): -1 往上、1 往下。
        """
        self.settings.move_group(group, offset)
        self.after_groups_changed()

    def move_group_before(self, group: str, before: str | None) -> None:
        """把群組拖到另一個群組前面（首頁拖曳群組標題）。

        Args:
            group (str): 群組名稱。
            before (str | None): 插在這個群組前面；None 表示移到最後。
        """
        if self.settings.move_group_before(group, before):
            self.after_groups_changed()
            self.app.set_status(tr("🗂 已調整群組「{group}」的順序", group=group), OK)

    def delete_group(self, group: str) -> None:
        """刪除群組（確認後）；裡面的音效回到未分組，檔案不會被刪除。

        Args:
            group (str): 群組名稱。
        """
        if not messagebox.askyesno(tr("刪除群組"),
                                   tr("確定要刪除群組「{group}」嗎？\n群組內的音效將移至「未分組」，音效檔不會被刪除。",
                                      group=group),
                                   parent=self.app.root):
            return
        self.settings.delete_group(group)
        self.after_groups_changed()

    def set_all_collapsed(self, collapsed: bool) -> None:
        """全部收合或全部展開。

        Args:
            collapsed (bool): True 為全部收合。
        """
        self.settings.collapsed_groups = [*self.settings.sound_groups, ""] if collapsed else []
        self.after_groups_changed()

    # ------------------------------------------------------------------ 小視窗釘選
    def pin_keys(self, keys: list[str]) -> None:
        """把多個音效加入小視窗（已在小視窗的略過）。

        Args:
            keys (list[str]): 音效 key。
        """
        added = [k for k in keys if k not in self.settings.overlay_keys]
        self.settings.overlay_keys.extend(added)
        self.app.home.refresh_tile_texts()
        self.app.refresh_overlay()
        self.app.save()
        self.app.set_status(tr("已將 {count} 個音效加入浮動視窗", count=len(added)), OK)

    def toggle_pin(self, item: SoundItem) -> None:
        """把音效加入或移出小視窗。

        Args:
            item (SoundItem): 音效。
        """
        if item.key in self.settings.overlay_keys:
            self.settings.overlay_keys.remove(item.key)
            self.app.set_status(tr("已從浮動視窗移除「{name}」", name=item.display_name), FG_MUTED)
        else:
            self.settings.overlay_keys.append(item.key)
            if self.app.overlay.visible:
                text = tr("📌 已將「{name}」加入浮動視窗", name=item.display_name)
            else:
                text = tr("📌 已將「{name}」加入浮動視窗（按一下首頁工具列的「🪟 浮動視窗」即可顯示）",
                          name=item.display_name)
            self.app.set_status(text, OK)
        self.app.home.refresh_tile(item)
        self.app.refresh_overlay()
        self.app.save()

    # ------------------------------------------------------------------ 進場音效
    def set_join_sound(self, key: str | None) -> None:
        """設定（或取消）進場音效；設定時自動開啟進場音效功能。

        Args:
            key (str | None): 音效 key；None 表示取消。
        """
        self.settings.join_sound_key = key
        if key is not None:
            self.settings.join_sound_enabled = True
        self.app.home.refresh_tile_texts()
        self.app.settings_page.update_join_ui()
        self.app.save()
        if key is not None:
            name = self.app.items_by_key[key].display_name
            self.app.set_status(tr("🚪 加入 Discord 語音頻道時將播放「{name}」（可於「⚙ 設定 → 進場音效」調整延遲）",
                                   name=name), OK)

    # ------------------------------------------------------------------ 音效檔
    def rename(self, item: SoundItem) -> None:
        """重新命名音效：直接改硬碟上的檔名，設定（音量、快捷鍵、小視窗）跟著搬過去。

        Args:
            item (SoundItem): 音效。
        """
        root = self.app.root
        initial = item.display_name
        while True:
            name = simpledialog.askstring(
                tr("重新命名"),
                tr("目前檔案：{file}\n新名稱（將同時變更實際檔名，副檔名維持 {suffix}）：",
                   file=item.path.name, suffix=item.path.suffix),
                initialvalue=initial, parent=root)
            if name is None:
                return
            name = name.strip()
            try:
                new_path = rename_sound_file(item.path, name)
                break
            except RenameError as exc:
                # 保留使用者打的字，讓他修正後再試，不用整段重打
                messagebox.showerror(tr("無法重新命名"), str(exc), parent=root)
                initial = name
        old_key, old_path = item.key, item.path
        folder = Path(self.settings.sound_dir or os.getcwd())
        new_key = new_path.relative_to(folder).as_posix() if new_path.is_relative_to(folder) else new_path.name
        self.settings.rename_sound_key(old_key, new_key)
        self.app.cache.rename(old_path, new_path)
        item.key, item.path, item.display_name = new_key, new_path, new_path.stem
        logger.info("重新命名音效：%s → %s", old_path.name, new_path.name)
        self.app.after_items_changed()
        self.app.set_status(tr("已重新命名為「{name}」", name=new_path.name), OK)

    def delete_sounds(self, keys: list[str]) -> None:
        """確認後刪除音效：檔案移到資源回收筒，相關設定（快捷鍵、群組、小視窗、音量…）一併移除。

        移到資源回收筒失敗（例如網路磁碟）時，再詢問一次是否永久刪除。

        Args:
            keys (list[str]): 要刪除的音效 key。
        """
        app = self.app
        items = [app.items_by_key[k] for k in keys if k in app.items_by_key]
        if not items:
            return
        root = app.root
        if len(items) == 1:
            question = tr("確定要刪除音效「{name}」嗎？\n\n檔案：{path}\n\n"
                          "檔案將移至資源回收筒（可從資源回收筒還原），"
                          "其快速鍵、群組、浮動視窗與音量設定也將一併移除。",
                          name=items[0].display_name, path=items[0].path)
        else:
            names = "\n".join(f"・{it.display_name}" for it in items[:DELETE_LIST_LIMIT])
            if len(items) > DELETE_LIST_LIMIT:
                question = tr("確定要刪除這 {count} 個音效嗎？\n\n{names}\n…等，共 {count} 個\n\n"
                              "檔案將移至資源回收筒（可從資源回收筒還原），相關設定也將一併移除。",
                              count=len(items), names=names)
            else:
                question = tr("確定要刪除這 {count} 個音效嗎？\n\n{names}\n\n"
                              "檔案將移至資源回收筒（可從資源回收筒還原），相關設定也將一併移除。",
                              count=len(items), names=names)
        if not messagebox.askyesno(tr("刪除音效"), question, icon="warning", default="no", parent=root):
            return
        deleted: list[SoundItem] = []
        failed: list[tuple[SoundItem, str]] = []
        for item in items:
            self._release(item)
            try:
                self.trash(item.path)
            except FileNotFoundError:
                deleted.append(item)        # 檔案已經不在了，只要清掉設定
            except OSError as exc:
                failed.append((item, str(exc)))
            else:
                deleted.append(item)
        if failed:
            names = "\n".join(f"・{it.display_name}：{err}" for it, err in failed[:DELETE_LIST_LIMIT])
            if messagebox.askyesno(tr("無法移至資源回收筒"),
                                   tr("下列音效無法移至資源回收筒：\n\n{names}\n\n是否要永久刪除？此操作無法復原。",
                                      names=names),
                                   icon="warning", default="no", parent=root):
                for item, _err in failed:
                    try:
                        item.path.unlink()
                    except OSError as exc:
                        logger.warning("永久刪除失敗：%s：%s", item.path, exc)
                    else:
                        deleted.append(item)
        if not deleted:
            app.set_status(tr("未刪除任何音效"), FG_MUTED)
            return
        gone = {it.key for it in deleted}
        for item in deleted:
            self.settings.forget_sound_key(item.key)
            logger.info("已刪除音效：%s", item.path)
        app.items = [it for it in app.items if it.key not in gone]
        app.home.clear_selection()
        app.after_items_changed()
        kept = len(items) - len(deleted)
        # 「全部刪除」與「部分失敗」各寫成完整的句子，翻譯時才能自行調整語序
        if len(deleted) == 1 and kept:
            text = tr("🗑 已刪除「{name}」（可從資源回收筒還原）；{kept} 個未刪除",
                      name=deleted[0].display_name, kept=kept)
        elif len(deleted) == 1:
            text = tr("🗑 已刪除「{name}」（可從資源回收筒還原）", name=deleted[0].display_name)
        elif kept:
            text = tr("🗑 已刪除 {count} 個音效（可從資源回收筒還原）；{kept} 個未刪除", count=len(deleted), kept=kept)
        else:
            text = tr("🗑 已刪除 {count} 個音效（可從資源回收筒還原）", count=len(deleted))
        app.set_status(text, ERR if kept else OK)

    def _release(self, item: SoundItem) -> None:
        """刪除前停止播放、關閉它的編輯視窗並清掉快取，避免檔案被占用或之後又被播出來。"""
        app = self.app
        app.engine.stop(item.key)
        for editor in list(app.sound_editors):
            if editor.alive and editor.sound is not None and editor.sound.key == item.key:
                editor.close()
        app.cache.forget(item.path)

    def set_volume_db(self, item: SoundItem, gain_db: float) -> None:
        """設定單一音效的音量。

        Args:
            item (SoundItem): 音效。
            gain_db (float): 增益（dB），會限制在 ±80 dB、以 0.5 dB 為單位。
        """
        gain_db = round(min(max(gain_db, SOUND_MIN_DB), SOUND_MAX_DB) * 2) / 2
        # 設定檔沿用「倍率」格式，舊版的百分比音量設定不必轉換
        item.volume = 1.0 if gain_db == 0 else gain_factor(gain_db)
        current = self.app.items_by_key.get(item.key)
        if current is not None and current is not item:
            current.volume = item.volume
        if gain_db == 0:
            self.settings.sound_volumes.pop(item.key, None)
        else:
            self.settings.sound_volumes[item.key] = item.volume
        self.app.save()
        self.app.set_status(tr("🔊 「{name}」音量：{gain} dB", name=item.display_name, gain=f"{gain_db:+.1f}"), OK)

    @staticmethod
    def reveal(item: SoundItem) -> None:
        """在檔案總管中顯示音效檔。

        Args:
            item (SoundItem): 音效。
        """
        if sys.platform == "win32":
            subprocess.Popen(["explorer", "/select,", str(item.path)])
        else:
            subprocess.Popen(["xdg-open", str(item.path.parent)])
