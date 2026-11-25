"""貼圖的動作：傳送到 Discord、複製到剪貼簿（都在背景執行緒做，結果交回主執行緒的狀態列）。"""

from __future__ import annotations

import logging
import shutil
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING

from soundboard import clipboard, discord_webhook, stickers
from soundboard.i18n import tr
from soundboard.theme import FG_MUTED, OK, WARN

if TYPE_CHECKING:
    from soundboard.gui.app import SoundboardApp

logger = logging.getLogger(__name__)

TEMP_FOLDER_NAME = "Soundboard_stickers"
TEMP_MAX_AGE_SECONDS = 24 * 3600


def temp_dir() -> Path:
    """轉換後的貼圖暫存資料夾（複製到剪貼簿時，貼上的程式要讀得到這個檔案）。"""
    return Path(tempfile.gettempdir()) / TEMP_FOLDER_NAME


def clean_temp(folder: Path, max_age: float = TEMP_MAX_AGE_SECONDS) -> None:
    """刪除暫存資料夾裡超過一天沒動過的檔案（太新的可能還在剪貼簿裡等著被貼上）。

    Args:
        folder (Path): 暫存資料夾。
        max_age (float): 保留的秒數。
    """
    cutoff = time.time() - max_age
    for path in folder.glob("*") if folder.is_dir() else []:
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            pass


class StickerActions:
    """傳送／複製貼圖。

    背景執行緒不能碰 Tk 元件，所以結果一律用 ``app.post_message`` 交給主執行緒顯示在狀態列。
    """

    def __init__(self, app: SoundboardApp) -> None:
        """
        Args:
            app (SoundboardApp): 主程式。
        """
        self.app = app
        # 不與音效解碼共用執行緒：上傳可能要等好幾秒，不能讓按音效變慢
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="sticker")

    def shutdown(self) -> None:
        """程式結束時停止背景工作。"""
        self._executor.shutdown(wait=False, cancel_futures=True)

    # ------------------------------------------------------------------ 傳送到 Discord
    def send(self, item: stickers.StickerItem) -> bool:
        """把貼圖傳到目前選擇的 Discord 頻道。

        Args:
            item (stickers.StickerItem): 貼圖。

        Returns:
            bool: 已開始傳送為 True；尚未設定 Webhook 時為 False（呼叫端可開啟設定視窗）。
        """
        settings = self.app.settings
        active = settings.active_webhook()
        if active is None:
            self.app.set_status(tr("⚠ 尚未設定 Discord Webhook，無法傳送。請按「⚙ Webhook 設定…」新增"), WARN)
            return False
        name, url = active
        username, max_edge = settings.discord_username, settings.sticker_send_size
        self.app.set_status(tr("⏳ 正在傳送「{name}」到 Discord…", name=item.name))
        self._executor.submit(self._send_worker, item, name, url, username, max_edge)
        return True

    def _send_worker(self, item: stickers.StickerItem, hook_name: str, url: str, username: str,
                     max_edge: int) -> None:
        post = self.app.post_message
        try:
            prepared = stickers.prepare_for_discord(
                item.path, max_edge=max_edge, progress=lambda text: post("status", f"{FG_MUTED}|{text}"))
            discord_webhook.send_file(url, prepared.filename, prepared.data, prepared.content_type,
                                      username=username)
        except (stickers.StickerError, discord_webhook.DiscordError) as exc:
            logger.warning("貼圖傳送失敗（%s → %s）：%s", item.key, discord_webhook.mask_webhook_url(url), exc)
            post("error", tr("❌ 傳送「{name}」失敗：{error}", name=item.name, error=exc))
            return
        except Exception as exc:  # noqa: BLE001 - 背景執行緒的例外不會顯示，一定要轉成狀態列訊息
            logger.exception("貼圖傳送發生未預期的錯誤")
            post("error", tr("❌ 傳送「{name}」失敗：{error}", name=item.name, error=exc))
            return
        logger.info("貼圖已傳送：%s → %s（%d bytes）", item.key, hook_name, len(prepared.data))
        size_kb = max(1, len(prepared.data) // 1024)
        post("status", f"{OK}|" + tr("✅ 已傳送「{name}」到 Discord（{hook}，{size} KB）",
                                      name=item.name, hook=hook_name, size=size_kb))

    # ------------------------------------------------------------------ 複製到剪貼簿
    def copy(self, item: stickers.StickerItem) -> None:
        """把貼圖檔放進剪貼簿，之後在 Discord 按 Ctrl+V 就能以自己的身分上傳。

        動態貼圖會先轉成 GIF（和直接傳送一樣的處理）。

        Args:
            item (stickers.StickerItem): 貼圖。
        """
        self.app.set_status(tr("⏳ 正在準備「{name}」…", name=item.name))
        self._executor.submit(self._copy_worker, item, self.app.settings.sticker_send_size)

    def _copy_worker(self, item: stickers.StickerItem, max_edge: int) -> None:
        post = self.app.post_message
        try:
            prepared = stickers.prepare_for_discord(item.path, max_edge=max_edge)
            if prepared.converted:
                folder = temp_dir()
                folder.mkdir(parents=True, exist_ok=True)
                clean_temp(folder)
                suffix = ".gif" if prepared.content_type == "image/gif" else ".png"
                target = stickers.unique_destination(folder, item.path.stem + suffix)
                target.write_bytes(prepared.data)
            else:
                target = item.path
            clipboard.copy_files([target])
        except (stickers.StickerError, clipboard.ClipboardError, OSError) as exc:
            logger.warning("貼圖複製失敗（%s）：%s", item.key, exc)
            post("error", tr("❌ 複製「{name}」失敗：{error}", name=item.name, error=exc))
            return
        post("status", f"{OK}|" + tr("📋 已複製「{name}」。到 Discord 的輸入框按 Ctrl+V 即可傳送", name=item.name))

    # ------------------------------------------------------------------ 匯入
    @staticmethod
    def import_files(files: list[tuple[Path, Path]]) -> int:
        """把貼圖檔複製進貼圖資料夾（不覆蓋同名檔案）。

        Args:
            files (list[tuple[Path, Path]]): （來源檔, 目的資料夾）。

        Returns:
            int: 成功複製的數量。
        """
        count = 0
        for source, folder in files:
            folder.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, stickers.unique_destination(folder, source.name))
            count += 1
        return count
