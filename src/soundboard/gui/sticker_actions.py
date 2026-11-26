"""貼圖的動作：複製到剪貼簿、傳送到 Discord（都在背景執行緒做，結果交回主執行緒的狀態列）。

文字貼圖（LINE 訊息貼圖、名字貼圖）會先套用使用者輸入的文字，再複製或傳送產生的圖。
"""

from __future__ import annotations

import logging
import shutil
import tempfile
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING

from soundboard import clipboard, discord_webhook, sticker_text, stickers
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


def display_name(item: stickers.StickerItem, text: str) -> str:
    """狀態列顯示的貼圖名稱：文字貼圖加上套用的文字。"""
    return f"{item.name}「{text}」" if text and item.text_spec is not None else item.name


def materialize(item: stickers.StickerItem, text: str = "") -> Path:
    """取得要複製或傳送的檔案：一般貼圖就是原檔；文字貼圖套用文字後存成暫存 PNG。

    Args:
        item (stickers.StickerItem): 貼圖。
        text (str): 要套用的文字（只對文字貼圖有效；空字串表示用原圖）。

    Returns:
        Path: 檔案路徑。

    Raises:
        sticker_text.TextStickerError: 文字貼圖的底圖無法讀取時拋出。
        OSError: 無法寫入暫存檔時拋出。
    """
    spec = item.text_spec
    text = sticker_text.clean_text(text)
    if spec is None or not text:
        return item.path
    image, _by_line = sticker_text.render(spec, text)
    folder = temp_dir() / "text"
    clean_temp(folder)
    return sticker_text.save_png(image, folder, f"{item.path.parent.name[-12:]}_{item.name}")


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
    def send(self, item: stickers.StickerItem, text: str = "") -> bool:
        """把貼圖傳到目前選擇的 Discord 頻道。

        Args:
            item (stickers.StickerItem): 貼圖。
            text (str): 文字貼圖要套用的文字。

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
        self.app.set_status(tr("⏳ 正在傳送「{name}」到 Discord…", name=display_name(item, text)))
        self._executor.submit(self._send_worker, item, text, name, url, username, max_edge)
        return True

    def _send_worker(self, item: stickers.StickerItem, text: str, hook_name: str, url: str, username: str,
                     max_edge: int) -> None:
        post = self.app.post_message
        label = display_name(item, text)
        try:
            source = materialize(item, text)
            prepared = stickers.prepare_for_discord(
                source, max_edge=max_edge, progress=lambda msg: post("status", f"{FG_MUTED}|{msg}"))
            discord_webhook.send_file(url, prepared.filename, prepared.data, prepared.content_type,
                                      username=username)
        except (stickers.StickerError, sticker_text.TextStickerError, discord_webhook.DiscordError,
                OSError) as exc:
            logger.warning("貼圖傳送失敗（%s → %s）：%s", item.key, discord_webhook.mask_webhook_url(url), exc)
            post("error", tr("❌ 傳送「{name}」失敗：{error}", name=label, error=exc))
            return
        except Exception as exc:  # noqa: BLE001 - 背景執行緒的例外不會顯示，一定要轉成狀態列訊息
            logger.exception("貼圖傳送發生未預期的錯誤")
            post("error", tr("❌ 傳送「{name}」失敗：{error}", name=label, error=exc))
            return
        logger.info("貼圖已傳送：%s → %s（%d bytes）", item.key, hook_name, len(prepared.data))
        size_kb = max(1, len(prepared.data) // 1024)
        post("status", f"{OK}|" + tr("✅ 已傳送「{name}」到 Discord（{hook}，{size} KB）",
                                      name=label, hook=hook_name, size=size_kb))

    # ------------------------------------------------------------------ 傳送文字
    def send_text(self, text: str, on_done: Callable[[bool], None]) -> bool:
        """用目前選擇的 Webhook 傳一則純文字訊息到 Discord。

        Args:
            text (str): 訊息內容（Discord 上限 2000 字，超過的部分會被截掉）。
            on_done (Callable[[bool], None]): 完成時在背景執行緒呼叫（是否成功）；呼叫端要自己交回主執行緒。

        Returns:
            bool: 已開始傳送為 True；尚未設定 Webhook 時為 False（呼叫端可開啟設定視窗）。
        """
        settings = self.app.settings
        active = settings.active_webhook()
        if active is None:
            self.app.set_status(tr("⚠ 尚未設定 Discord Webhook，無法傳送。請按「⚙ Webhook 設定…」新增"), WARN)
            return False
        if len(text) > discord_webhook.MAX_CONTENT_LENGTH:
            self.app.set_status(tr("⚠ 文字超過 Discord 的 {limit} 字上限，超過的部分不會送出",
                                   limit=discord_webhook.MAX_CONTENT_LENGTH), WARN)
        name, url = active
        self.app.set_status(tr("⏳ 正在傳送文字到 Discord…"))
        self._executor.submit(self._text_worker, text, name, url, settings.discord_username, on_done)
        return True

    def _text_worker(self, text: str, hook_name: str, url: str, username: str,
                     on_done: Callable[[bool], None]) -> None:
        post = self.app.post_message
        ok = False
        try:
            discord_webhook.send_text(url, text, username=username)
            ok = True
        except discord_webhook.DiscordError as exc:
            logger.warning("文字傳送失敗（→ %s）：%s", discord_webhook.mask_webhook_url(url), exc)
            post("error", tr("❌ 傳送文字失敗：{error}", error=exc))
        except Exception as exc:  # noqa: BLE001 - 背景執行緒的例外不會顯示，一定要轉成狀態列訊息
            logger.exception("文字傳送發生未預期的錯誤")
            post("error", tr("❌ 傳送文字失敗：{error}", error=exc))
        if ok:
            logger.info("文字已傳送：%s（%d 字）", hook_name, len(text))
            post("status", f"{OK}|" + tr("✅ 已傳送文字到 Discord（{hook}）", hook=hook_name))
        on_done(ok)

    # ------------------------------------------------------------------ 複製到剪貼簿
    def copy(self, item: stickers.StickerItem, text: str = "") -> None:
        """把貼圖檔放進剪貼簿，之後在 Discord 按 Ctrl+V 就能以自己的身分上傳。

        PNG 與 GIF 都以「檔案」放進剪貼簿（等同在檔案總管按 Ctrl+C），貼到 Discord 時 GIF 仍會動；
        動態 APNG／WebP 會先轉成 GIF，文字貼圖會先套用文字。

        Args:
            item (stickers.StickerItem): 貼圖。
            text (str): 文字貼圖要套用的文字。
        """
        self.app.set_status(tr("⏳ 正在準備「{name}」…", name=display_name(item, text)))
        self._executor.submit(self._copy_worker, item, text, self.app.settings.sticker_send_size)

    def _copy_worker(self, item: stickers.StickerItem, text: str, max_edge: int) -> None:
        post = self.app.post_message
        label = display_name(item, text)
        try:
            source = materialize(item, text)
            prepared = stickers.prepare_for_discord(source, max_edge=max_edge)
            if prepared.converted:
                folder = temp_dir()
                folder.mkdir(parents=True, exist_ok=True)
                clean_temp(folder)
                suffix = ".gif" if prepared.content_type == "image/gif" else ".png"
                target = stickers.unique_destination(folder, source.stem + suffix)
                target.write_bytes(prepared.data)
            else:
                target = source
            clipboard.copy_files([target])
        except (stickers.StickerError, sticker_text.TextStickerError, clipboard.ClipboardError, OSError) as exc:
            logger.warning("貼圖複製失敗（%s）：%s", item.key, exc)
            post("error", tr("❌ 複製「{name}」失敗：{error}", name=label, error=exc))
            return
        except Exception as exc:  # noqa: BLE001 - 背景執行緒的例外不會顯示，一定要轉成狀態列訊息
            logger.exception("貼圖複製發生未預期的錯誤")
            post("error", tr("❌ 複製「{name}」失敗：{error}", name=label, error=exc))
            return
        post("status", f"{OK}|" + tr("📋 已複製「{name}」。到 Discord 的輸入框按 Ctrl+V 即可傳送", name=label))

    # ------------------------------------------------------------------ 匯入
    @staticmethod
    def import_files(files: list[tuple[Path, Path]]) -> int:
        """把貼圖檔複製進貼圖資料夾（不覆蓋同名檔案）。

        系列的資料檔（``line_meta.json``、``sticker_keywords.json``）則直接覆蓋：它們描述整組貼圖，
        重新匯入同一組時應該換成新的，而不是多一份讀不到的「(2)」。

        Args:
            files (list[tuple[Path, Path]]): （來源檔, 目的資料夾）。

        Returns:
            int: 成功複製的貼圖數量（不含資料檔）。
        """
        count = 0
        for source, folder in files:
            folder.mkdir(parents=True, exist_ok=True)
            if source.name in stickers.SERIES_DATA_FILES:
                shutil.copy2(source, folder / source.name)
                continue
            shutil.copy2(source, stickers.unique_destination(folder, source.name))
            count += 1
        return count
