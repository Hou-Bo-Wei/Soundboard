"""用 Discord Webhook 傳送檔案到頻道（只用標準庫，不需要機器人帳號或 token 登入）。

Webhook 網址（``https://discord.com/api/webhooks/<id>/<secret>``）等同於「能在那個頻道發言的密碼」：
    * 只存在使用者自己的設定檔（``%APPDATA%\\Soundboard\\settings.json``），不會寫進程式碼或儲存庫。
    * 顯示在畫面與紀錄時一律遮蔽（:func:`mask_webhook_url`）。
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass

from soundboard.i18n import tr

logger = logging.getLogger(__name__)

_WEBHOOK_RE = re.compile(
    r"^https://(?:(?:canary|ptb)\.)?(?:discord|discordapp)\.com/api(?:/v\d+)?/webhooks/(?P<id>\d+)/(?P<token>[\w-]+)/?$")
USER_AGENT = "SoundboardStickers (https://github.com/Hou-Bo-Wei/Soundboard, 0.1)"
REQUEST_TIMEOUT = 30.0
MAX_CONTENT_LENGTH = 2000   # Discord 一則訊息的字數上限
MAX_RETRY_WAIT = 10.0       # 被限速時最多願意等幾秒再重試一次


class DiscordError(Exception):
    """傳送到 Discord 失敗（訊息已翻譯，可直接顯示給使用者）。"""


@dataclass(frozen=True)
class SendResult:
    """傳送成功後 Discord 回傳的資訊。

    Attributes:
        message_id (str): 訊息編號。
        url (str): 附件在 Discord CDN 上的網址（可能為空字串）。
    """

    message_id: str
    url: str


def normalize_webhook_url(url: str) -> str | None:
    """檢查並整理 Webhook 網址。

    Args:
        url (str): 使用者貼上的網址（前後空白會被去掉；``?wait=true`` 之類的查詢參數會被拿掉）。

    Returns:
        str | None: 正規化後的網址；格式不正確時為 None。
    """
    url = url.strip().split("?", 1)[0].rstrip("/")
    match = _WEBHOOK_RE.match(url)
    return url if match else None


def mask_webhook_url(url: str) -> str:
    """遮蔽網址中的密鑰，只留頻道識別碼與末四碼（顯示與紀錄用）。

    Args:
        url (str): Webhook 網址。

    Returns:
        str: 遮蔽後的文字。
    """
    match = _WEBHOOK_RE.match(url.strip().split("?", 1)[0].rstrip("/"))
    if match is None:
        return "…"
    token = match["token"]
    return f"…/webhooks/{match['id']}/••••{token[-4:]}"


def _multipart(fields: dict[str, str], file_field: str, filename: str, data: bytes,
               content_type: str) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n'
                     f"Content-Type: application/json\r\n\r\n{value}\r\n".encode())
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'
                 f"Content-Type: {content_type}\r\n\r\n".encode() + data + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def _error_message(status: int, body: bytes) -> str:
    """把 Discord 的錯誤回應轉成使用者看得懂的說明。"""
    detail = ""
    code = None
    try:
        payload = json.loads(body.decode("utf-8", "replace"))
        detail = str(payload.get("message", ""))
        code = payload.get("code")
    except (ValueError, AttributeError):
        detail = body.decode("utf-8", "replace")[:120]
    if status in (401, 404) or code == 10015:
        return tr("Webhook 無效或已被刪除，請到 Discord 頻道設定重新建立並更新網址")
    if status == 413 or code == 40005:
        return tr("檔案太大，超過這個伺服器的上傳上限")
    if status == 429:
        return tr("傳得太快被 Discord 暫時限速，請稍後再試")
    if status == 403:
        return tr("沒有權限（Discord 拒絕了這個 Webhook）：{detail}", detail=detail)
    return tr("Discord 回應錯誤 {status}：{detail}", status=status, detail=detail or "?")


def _post(url: str, body: bytes, content_type: str) -> dict:
    request = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": content_type, "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        payload = exc.read()
        if exc.code == 429:
            try:
                wait = float(json.loads(payload.decode("utf-8", "replace")).get("retry_after", 1.0))
            except (ValueError, AttributeError):
                wait = 1.0
            raise _RateLimited(wait, payload) from exc
        raise DiscordError(_error_message(exc.code, payload)) from exc
    except urllib.error.URLError as exc:
        raise DiscordError(tr("無法連線到 Discord：{error}", error=exc.reason)) from exc
    except TimeoutError as exc:
        raise DiscordError(tr("連線 Discord 逾時，請檢查網路")) from exc
    except OSError as exc:
        raise DiscordError(tr("無法連線到 Discord：{error}", error=exc)) from exc
    try:
        return json.loads(raw.decode("utf-8")) if raw else {}
    except ValueError:
        return {}


class _RateLimited(Exception):
    def __init__(self, wait: float, body: bytes) -> None:
        super().__init__(wait)
        self.wait = wait
        self.body = body


def _post_with_retry(url: str, body: bytes, content_type: str) -> dict:
    """送出請求；被限速（429）且等待時間不長時等一下重試一次。"""
    try:
        return _post(url, body, content_type)
    except _RateLimited as limited:
        if limited.wait > MAX_RETRY_WAIT:
            raise DiscordError(_error_message(429, limited.body)) from None
        logger.info("Discord 限速，%.1f 秒後重試", limited.wait)
        time.sleep(limited.wait + 0.1)
        try:
            return _post(url, body, content_type)
        except _RateLimited as again:
            raise DiscordError(_error_message(429, again.body)) from None


def send_file(webhook_url: str, filename: str, data: bytes, content_type: str, *, username: str = "",
              content: str = "") -> SendResult:
    """用 Webhook 把一個檔案傳到頻道。

    Args:
        webhook_url (str): Webhook 網址。
        filename (str): 檔名（建議只用英數字，副檔名決定 Discord 如何顯示）。
        data (bytes): 檔案內容。
        content_type (str): MIME 類型。
        username (str): 訊息顯示的發送者名稱；空字串使用 Webhook 本身的名稱。
        content (str): 附在圖片上的文字；空字串表示只傳圖片。

    Returns:
        SendResult: Discord 回傳的訊息資訊。

    Raises:
        DiscordError: 網址格式不對、網路問題或 Discord 拒絕時拋出。
    """
    url = normalize_webhook_url(webhook_url)
    if url is None:
        raise DiscordError(tr("Webhook 網址格式不正確（應為 https://discord.com/api/webhooks/…）"))
    payload: dict[str, object] = {"allowed_mentions": {"parse": []},
                                  "attachments": [{"id": 0, "filename": filename}]}
    if username.strip():
        payload["username"] = username.strip()[:80]
    if content:
        payload["content"] = content[:2000]
    body, ctype = _multipart({"payload_json": json.dumps(payload, ensure_ascii=False)}, "files[0]", filename, data,
                             content_type)
    # wait=true：等 Discord 真的建立訊息才回應，這樣「成功」才是真的成功
    reply = _post_with_retry(f"{url}?wait=true", body, ctype)
    attachments = reply.get("attachments") or [{}]
    return SendResult(message_id=str(reply.get("id", "")), url=str(attachments[0].get("url", "")))


def send_text(webhook_url: str, text: str, *, username: str = "") -> SendResult:
    """用 Webhook 傳一則純文字訊息（貼圖頁的「傳送文字」與測試連線）。

    不會觸發 @everyone、@here 或提及任何人（allowed_mentions 設為空），避免打字時不小心通知整個伺服器。

    Args:
        webhook_url (str): Webhook 網址。
        text (str): 文字內容。
        username (str): 訊息顯示的發送者名稱；空字串使用 Webhook 本身的名稱。

    Returns:
        SendResult: Discord 回傳的訊息資訊。

    Raises:
        DiscordError: 網址格式不對、網路問題或 Discord 拒絕時拋出。
    """
    url = normalize_webhook_url(webhook_url)
    if url is None:
        raise DiscordError(tr("Webhook 網址格式不正確（應為 https://discord.com/api/webhooks/…）"))
    payload: dict[str, object] = {"content": text[:MAX_CONTENT_LENGTH], "allowed_mentions": {"parse": []}}
    if username.strip():
        payload["username"] = username.strip()[:80]
    reply = _post_with_retry(f"{url}?wait=true", json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                             "application/json")
    return SendResult(message_id=str(reply.get("id", "")), url="")
