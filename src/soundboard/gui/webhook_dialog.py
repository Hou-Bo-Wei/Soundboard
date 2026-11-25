"""Discord Webhook 設定對話框：新增、修改、刪除與測試傳送目標。"""

from __future__ import annotations

import logging
import queue
import threading
import tkinter as tk
from collections.abc import Callable
from tkinter import messagebox
from typing import TYPE_CHECKING

from soundboard import discord_webhook
from soundboard.i18n import tr
from soundboard.theme import (
    BG, BG_DARK, BTN, BTN_HOVER, ERR, FG, FG_MUTED, FONT_FAMILY, OK, PANEL, WARN, ACCENT,
)

if TYPE_CHECKING:
    from soundboard.gui.app import SoundboardApp

logger = logging.getLogger(__name__)

FONT = (FONT_FAMILY, 10)
FONT_SMALL = (FONT_FAMILY, 9)


def _button(parent: tk.Misc, text: str, command: Callable[[], None], bg: str = BTN) -> tk.Button:
    return tk.Button(parent, text=text, command=command, bg=bg, fg=FG, activebackground=BTN_HOVER,
                     activeforeground=FG, relief="flat", bd=0, padx=12, pady=5, cursor="hand2", font=FONT_SMALL)


class WebhookDialog:
    """強制回應對話框：管理傳貼圖用的 Discord Webhook。

    變更會立即寫入設定並儲存；關閉視窗時呼叫 ``on_change`` 讓貼圖頁更新。
    """

    def __init__(self, app: SoundboardApp, on_change: Callable[[], None]) -> None:
        """建立並顯示對話框，直到使用者關閉才返回。

        Args:
            app (SoundboardApp): 主程式。
            on_change (Callable[[], None]): 設定有變動時呼叫（關閉視窗時）。
        """
        self.app = app
        self.settings = app.settings
        self._on_change = on_change
        self._changed = False
        self._results: queue.Queue[tuple[bool, str]] = queue.Queue()
        self._names: list[str] = []

        root = app.root
        self.top = tk.Toplevel(root, bg=BG, padx=20, pady=16)
        self.top.title(tr("Discord Webhook 設定"))
        self.top.transient(root)
        self.top.minsize(560, 0)

        tk.Label(self.top, text=tr("傳送貼圖的 Discord 頻道"), bg=BG, fg=FG,
                 font=(FONT_FAMILY, 11, "bold")).pack(anchor="w")
        tk.Label(self.top, bg=BG, fg=FG_MUTED, font=FONT_SMALL, justify="left", text=tr(
            "到 Discord 頻道：⚙ 編輯頻道 → 整合 → Webhook → 新 Webhook → 複製 Webhook 網址，貼在下方。\n"
            "Webhook 網址等同頻道的發言密碼，只會存在你這台電腦的設定檔，請不要分享給別人。")
        ).pack(anchor="w", pady=(2, 10))

        self.listbox = tk.Listbox(self.top, height=4, bg=BG_DARK, fg=FG, selectbackground=ACCENT,
                                  selectforeground=FG, relief="flat", bd=0, highlightthickness=0, font=FONT,
                                  activestyle="none", exportselection=False)
        self.listbox.pack(fill="x")
        self.listbox.bind("<<ListboxSelect>>", lambda _e: self._load_selected())

        form = tk.Frame(self.top, bg=BG)
        form.pack(fill="x", pady=(10, 0))
        form.columnconfigure(1, weight=1)
        self.name_var = tk.StringVar()
        self.url_var = tk.StringVar()
        self.user_var = tk.StringVar(value=self.settings.discord_username)
        self._entry(form, 0, tr("名稱"), self.name_var)
        self.url_entry = self._entry(form, 1, tr("Webhook 網址"), self.url_var, show="•")
        self.show_var = tk.BooleanVar(value=False)
        tk.Checkbutton(form, text=tr("顯示網址"), variable=self.show_var, command=self._toggle_show, bg=BG, fg=FG_MUTED,
                       selectcolor=BG_DARK, activebackground=BG, activeforeground=FG, font=FONT_SMALL,
                       bd=0, highlightthickness=0).grid(row=1, column=2, padx=(8, 0))
        self._entry(form, 2, tr("傳送者名稱（選填）"), self.user_var)

        buttons = tk.Frame(self.top, bg=BG)
        buttons.pack(fill="x", pady=(12, 0))
        _button(buttons, tr("儲存"), self._save, bg=ACCENT).pack(side="left")
        _button(buttons, tr("刪除"), self._delete).pack(side="left", padx=(8, 0))
        _button(buttons, tr("傳送測試訊息"), self._test).pack(side="left", padx=(8, 0))
        _button(buttons, tr("關閉"), self._close).pack(side="right")

        self.message = tk.Label(self.top, text="", bg=BG, fg=FG_MUTED, font=FONT_SMALL, anchor="w", justify="left",
                                wraplength=520)
        self.message.pack(fill="x", pady=(10, 0))

        self.top.protocol("WM_DELETE_WINDOW", self._close)
        self._refresh_list(select=self.settings.active_webhook()[0] if self.settings.active_webhook() else None)
        self.top.grab_set()
        self.top.lift()
        self.top.focus_force()
        self.top.after(100, self._poll)
        root.wait_window(self.top)

    # ------------------------------------------------------------------ 版面
    def _entry(self, form: tk.Frame, row: int, label: str, var: tk.StringVar, show: str = "") -> tk.Entry:
        tk.Label(form, text=label, bg=BG, fg=FG, font=FONT, anchor="w").grid(row=row, column=0, sticky="w",
                                                                              pady=4, padx=(0, 12))
        entry = tk.Entry(form, textvariable=var, bg=BG_DARK, fg=FG, insertbackground=FG, relief="flat", bd=0,
                         highlightthickness=1, highlightbackground=PANEL, highlightcolor=ACCENT, font=FONT, show=show)
        entry.grid(row=row, column=1, sticky="we", ipady=4)
        return entry

    def _toggle_show(self) -> None:
        self.url_entry.configure(show="" if self.show_var.get() else "•")

    def _say(self, text: str, color: str = FG_MUTED) -> None:
        self.message.configure(text=text, fg=color)

    # ------------------------------------------------------------------ 清單
    def _refresh_list(self, select: str | None = None) -> None:
        self._names = list(self.settings.discord_webhooks)
        self.listbox.delete(0, "end")
        for name in self._names:
            mark = "●" if self.settings.active_webhook() and self.settings.active_webhook()[0] == name else "○"
            masked = discord_webhook.mask_webhook_url(self.settings.discord_webhooks[name])
            self.listbox.insert("end", f"{mark}  {name}    {masked}")
        if select in self._names:
            self.listbox.selection_set(self._names.index(select))
            self._load_selected()
        elif not self._names:
            self.name_var.set(tr("我的頻道"))
            self.url_var.set("")

    def _load_selected(self) -> None:
        selection = self.listbox.curselection()
        if not selection:
            return
        name = self._names[selection[0]]
        self.name_var.set(name)
        self.url_var.set(self.settings.discord_webhooks[name])

    # ------------------------------------------------------------------ 動作
    def _save(self) -> None:
        name = self.name_var.get().strip()
        url = discord_webhook.normalize_webhook_url(self.url_var.get())
        if not name:
            self._say(tr("請輸入名稱"), ERR)
            return
        if url is None:
            self._say(tr("Webhook 網址格式不正確（應為 https://discord.com/api/webhooks/…）"), ERR)
            return
        selection = self.listbox.curselection()
        renamed_from = self._names[selection[0]] if selection else None
        if renamed_from and renamed_from != name:
            # 改名：保留清單順序，並讓「目前使用」跟著新名稱
            self.settings.discord_webhooks = {(name if k == renamed_from else k): v
                                              for k, v in self.settings.discord_webhooks.items()}
            if self.settings.discord_webhook_active == renamed_from:
                self.settings.discord_webhook_active = name
        self.settings.discord_webhooks[name] = url
        self.settings.discord_webhook_active = name
        self.settings.discord_username = self.user_var.get().strip()
        self._persist()
        self._refresh_list(select=name)
        self._say(tr("已儲存，之後傳送的貼圖會送到「{name}」", name=name), OK)

    def _delete(self) -> None:
        selection = self.listbox.curselection()
        if not selection:
            self._say(tr("請先在上方清單選擇要刪除的項目"), WARN)
            return
        name = self._names[selection[0]]
        if not messagebox.askyesno(tr("刪除 Webhook"), tr("確定要刪除「{name}」嗎？\n（只會從這個程式移除，Discord 上的 Webhook 不受影響）",
                                                       name=name), parent=self.top):
            return
        del self.settings.discord_webhooks[name]
        if self.settings.discord_webhook_active == name:
            self.settings.discord_webhook_active = None
        self._persist()
        self._refresh_list()
        self._say(tr("已刪除「{name}」", name=name), FG_MUTED)

    def _test(self) -> None:
        url = discord_webhook.normalize_webhook_url(self.url_var.get())
        if url is None:
            self._say(tr("Webhook 網址格式不正確（應為 https://discord.com/api/webhooks/…）"), ERR)
            return
        username = self.user_var.get().strip()
        self._say(tr("⏳ 正在傳送測試訊息…"))

        def work() -> None:
            try:
                discord_webhook.send_text(url, tr("✅ 音效版貼圖連線測試成功"), username=username)
            except discord_webhook.DiscordError as exc:
                self._results.put((False, str(exc)))
            except Exception as exc:  # noqa: BLE001 - 背景執行緒的例外要轉成畫面訊息
                logger.exception("測試傳送失敗")
                self._results.put((False, str(exc)))
            else:
                self._results.put((True, tr("✅ 測試訊息已送到頻道，請到 Discord 確認")))

        threading.Thread(target=work, daemon=True, name="webhook-test").start()

    def _poll(self) -> None:
        if not self.top.winfo_exists():
            return
        try:
            while True:
                ok, text = self._results.get_nowait()
                self._say(text, OK if ok else ERR)
        except queue.Empty:
            pass
        self.top.after(100, self._poll)

    def _persist(self) -> None:
        self._changed = True
        self.app.save()

    def _close(self) -> None:
        # 改了「傳送者名稱」但沒按儲存時也一併保存
        username = self.user_var.get().strip()
        if username != self.settings.discord_username:
            self.settings.discord_username = username
            self._persist()
        self.top.destroy()
        if self._changed:
            self._on_change()
