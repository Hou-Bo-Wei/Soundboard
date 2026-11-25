"""貼圖：掃描、動態貼圖轉 GIF、縮小、Discord Webhook 上傳（用本機假伺服器）與剪貼簿格式。"""

from __future__ import annotations

import json
import struct
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

PIL = pytest.importorskip("PIL")
from PIL import Image  # noqa: E402

from soundboard import clipboard, discord_webhook, stickers  # noqa: E402


# ---------------------------------------------------------------- 測試用圖片
def _frame(color: tuple[int, int, int, int], size: int = 64) -> Image.Image:
    im = Image.new("RGBA", (size, size), (0, 0, 0, 0))     # 透明背景
    for x in range(size // 4, size * 3 // 4):
        for y in range(size // 4, size * 3 // 4):
            im.putpixel((x, y), color)
    return im


def make_png(path: Path, size: int = 64) -> Path:
    _frame((255, 0, 0, 255), size).save(path)
    return path


def make_animation(path: Path, fmt: str, n: int = 4, size: int = 64) -> Path:
    frames = [_frame((40 * i % 256, 100, 200, 255), size) for i in range(n)]
    frames[0].save(path, format=fmt, save_all=True, append_images=frames[1:], duration=80, loop=0, disposal=2)
    return path


# ---------------------------------------------------------------- 掃描
def test_scan_groups_by_series_and_sorts_naturally(tmp_path: Path) -> None:
    (tmp_path / "系列A").mkdir()
    for name in ("sticker10.png", "sticker2.png"):
        make_png(tmp_path / "系列A" / name)
    make_png(tmp_path / "root.png")
    (tmp_path / "notes.txt").write_text("x")
    items = stickers.scan_stickers(tmp_path)
    assert [(i.series, i.name) for i in items] == [("", "root"), ("系列A", "sticker2"), ("系列A", "sticker10")]
    assert items[1].key == "系列A/sticker2.png"
    assert stickers.scan_stickers(tmp_path / "不存在") == []


def test_inspect_detects_animation(tmp_path: Path) -> None:
    still = stickers.inspect_sticker(make_png(tmp_path / "a.png"))
    apng = stickers.inspect_sticker(make_animation(tmp_path / "b.png", "PNG"))
    webp = stickers.inspect_sticker(make_animation(tmp_path / "c.webp", "WEBP"))
    assert not still.animated and still.frames == 1
    assert apng.animated and apng.frames == 4
    assert webp.animated and webp.frames == 4


def test_inspect_rejects_garbage(tmp_path: Path) -> None:
    bad = tmp_path / "bad.png"
    bad.write_bytes(b"not an image")
    with pytest.raises(stickers.StickerError):
        stickers.inspect_sticker(bad)


def test_unique_destination(tmp_path: Path) -> None:
    (tmp_path / "a.png").write_bytes(b"1")
    assert stickers.unique_destination(tmp_path, "b.png") == tmp_path / "b.png"
    assert stickers.unique_destination(tmp_path, "a.png") == tmp_path / "a (2).png"
    (tmp_path / "a (2).png").write_bytes(b"1")
    assert stickers.unique_destination(tmp_path, "a.png") == tmp_path / "a (3).png"


# ---------------------------------------------------------------- 準備檔案
def test_static_png_is_sent_unchanged(tmp_path: Path) -> None:
    path = make_png(tmp_path / "貼圖一.png")
    prepared = stickers.prepare_for_discord(path)
    assert not prepared.converted
    assert prepared.data == path.read_bytes()
    assert prepared.content_type == "image/png"
    assert prepared.filename == "sticker.png"        # 中文檔名換成安全的 ASCII 檔名


def test_animated_gif_is_sent_unchanged(tmp_path: Path) -> None:
    path = make_animation(tmp_path / "wave.gif", "GIF")
    prepared = stickers.prepare_for_discord(path)
    assert not prepared.converted and prepared.filename == "wave.gif"
    assert prepared.data == path.read_bytes()


@pytest.mark.parametrize("fmt, name", [("PNG", "anim.png"), ("WEBP", "anim.webp")])
def test_apng_and_webp_are_converted_to_animated_gif(tmp_path: Path, fmt: str, name: str) -> None:
    path = make_animation(tmp_path / name, fmt, n=5)
    prepared = stickers.prepare_for_discord(path)
    assert prepared.converted and prepared.content_type == "image/gif" and prepared.filename.endswith(".gif")
    with Image.open(__import__("io").BytesIO(prepared.data)) as gif:
        assert gif.format == "GIF" and gif.n_frames == 5
        assert gif.info.get("loop") == 0
        assert "transparency" in gif.info                     # 透明背景有保留
        gif.seek(0)
        assert gif.convert("RGBA").getpixel((0, 0))[3] == 0   # 角落仍然透明


def test_max_edge_shrinks_static_and_animated(tmp_path: Path) -> None:
    still = stickers.prepare_for_discord(make_png(tmp_path / "s.png", 200), max_edge=100)
    assert still.converted
    with Image.open(__import__("io").BytesIO(still.data)) as im:
        assert im.size == (100, 100) and im.mode == "RGBA"
    anim = stickers.prepare_for_discord(make_animation(tmp_path / "a.gif", "GIF", size=200), max_edge=100)
    assert anim.converted
    with Image.open(__import__("io").BytesIO(anim.data)) as im:
        assert im.size == (100, 100) and im.n_frames == 4
    # 不會放大
    small = stickers.prepare_for_discord(make_png(tmp_path / "t.png", 50), max_edge=100)
    assert not small.converted


def test_oversize_animation_is_shrunk_until_it_fits(tmp_path: Path) -> None:
    path = make_animation(tmp_path / "big.png", "PNG", n=8, size=300)
    full = stickers.prepare_for_discord(path)
    limit = len(full.data) // 2
    prepared = stickers.prepare_for_discord(path, limit=limit)
    assert len(prepared.data) <= limit
    with Image.open(__import__("io").BytesIO(prepared.data)) as gif:
        assert max(gif.size) < 300 or gif.n_frames < 8


def test_impossible_limit_raises(tmp_path: Path) -> None:
    path = make_animation(tmp_path / "big.png", "PNG", size=100)
    with pytest.raises(stickers.StickerError):
        stickers.prepare_for_discord(path, limit=50)


def test_thumbnail_and_animation_frames(tmp_path: Path) -> None:
    thumb = stickers.load_thumbnail(make_png(tmp_path / "a.png", 200), 80)
    assert thumb.mode == "RGBA" and max(thumb.size) == 80
    frames = stickers.load_animation(make_animation(tmp_path / "b.webp", "WEBP", n=3, size=120), 60)
    assert len(frames) == 3 and all(max(f.size) == 60 and d >= 20 for f, d in frames)
    assert stickers.load_animation(tmp_path / "a.png", 60) == []


# ---------------------------------------------------------------- Webhook
VALID_URL = "https://discord.com/api/webhooks/123456789012345678/AbC-dEf_ghIJKLmnop"


def test_webhook_url_validation_and_masking() -> None:
    assert discord_webhook.normalize_webhook_url(f"  {VALID_URL}?wait=true ") == VALID_URL
    assert discord_webhook.normalize_webhook_url("https://discordapp.com/api/v10/webhooks/1/abc/") is not None
    assert discord_webhook.normalize_webhook_url("https://evil.example/api/webhooks/1/abc") is None
    assert discord_webhook.normalize_webhook_url("https://discord.com/channels/1/2") is None
    masked = discord_webhook.mask_webhook_url(VALID_URL)
    assert "AbC-dEf" not in masked and masked.endswith("nop") and "123456789012345678" in masked
    assert discord_webhook.mask_webhook_url("garbage") == "…"


class _Recorder(BaseHTTPRequestHandler):
    requests: list[dict] = []
    script: list[tuple[int, dict, dict]] = []     # 依序回應：(狀態碼, 標頭, JSON)

    def do_POST(self) -> None:  # noqa: N802
        body = self.rfile.read(int(self.headers["Content-Length"]))
        type(self).requests.append({"path": self.path, "headers": dict(self.headers), "body": body})
        status, headers, payload = type(self).script.pop(0) if type(self).script else (
            200, {}, {"id": "42", "attachments": [{"url": "https://cdn.discordapp.com/x/sticker.gif"}]})
        raw = json.dumps(payload).encode()
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *_args) -> None:
        pass


@pytest.fixture
def server(monkeypatch):
    _Recorder.requests = []
    _Recorder.script = []
    httpd = HTTPServer(("127.0.0.1", 0), _Recorder)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    # 測試用：放行本機網址（正式程式只接受 discord.com 的 Webhook）
    monkeypatch.setattr(discord_webhook, "normalize_webhook_url", lambda url: url.strip())
    yield f"http://127.0.0.1:{httpd.server_port}/api/webhooks/1/token"
    httpd.shutdown()


def test_send_file_builds_multipart_discord_expects(server: str) -> None:
    result = discord_webhook.send_file(server, "sticker.gif", b"GIF89a-data", "image/gif", username="我的音效版")
    assert result.message_id == "42" and result.url.endswith("sticker.gif")
    sent = _Recorder.requests[0]
    assert sent["path"].endswith("?wait=true")
    assert sent["headers"]["User-Agent"].startswith("SoundboardStickers")
    assert sent["headers"]["Content-Type"].startswith("multipart/form-data; boundary=")
    body = sent["body"]
    assert b'name="payload_json"' in body and b'name="files[0]"; filename="sticker.gif"' in body
    assert b"GIF89a-data" in body and b"Content-Type: image/gif" in body
    payload = json.loads(body.split(b"\r\n\r\n")[1].split(b"\r\n--")[0])
    assert payload["username"] == "我的音效版"
    assert payload["allowed_mentions"] == {"parse": []}                # 不會意外 @everyone
    assert payload["attachments"] == [{"id": 0, "filename": "sticker.gif"}]


def test_send_retries_once_when_rate_limited(server: str, monkeypatch) -> None:
    monkeypatch.setattr(discord_webhook.time, "sleep", lambda _s: None)
    _Recorder.script = [(429, {}, {"retry_after": 0.5}),
                        (200, {}, {"id": "7", "attachments": []})]
    assert discord_webhook.send_file(server, "a.png", b"x", "image/png").message_id == "7"
    assert len(_Recorder.requests) == 2


def test_long_rate_limit_gives_up(server: str) -> None:
    _Recorder.script = [(429, {}, {"retry_after": 60})]
    with pytest.raises(discord_webhook.DiscordError, match="限速"):
        discord_webhook.send_file(server, "a.png", b"x", "image/png")


@pytest.mark.parametrize("status, payload, expect", [
    (404, {"message": "Unknown Webhook", "code": 10015}, "無效或已被刪除"),
    (413, {"message": "Request entity too large", "code": 40005}, "檔案太大"),
    (500, {"message": "oops"}, "500"),
])
def test_send_errors_are_translated(server: str, status: int, payload: dict, expect: str) -> None:
    _Recorder.script = [(status, {}, payload)]
    with pytest.raises(discord_webhook.DiscordError, match=expect):
        discord_webhook.send_file(server, "a.png", b"x", "image/png")


def test_invalid_url_is_rejected_before_any_request() -> None:
    with pytest.raises(discord_webhook.DiscordError, match="格式不正確"):
        discord_webhook.send_file("https://example.com/hook", "a.png", b"x", "image/png")


def test_connection_failure_message() -> None:
    with pytest.raises(discord_webhook.DiscordError):
        discord_webhook._post("http://127.0.0.1:9/api/webhooks/1/t", b"{}", "application/json")


# ---------------------------------------------------------------- 剪貼簿
def test_dropfiles_layout(tmp_path: Path) -> None:
    a, b = tmp_path / "貼圖.gif", tmp_path / "b.png"
    data = clipboard.build_dropfiles([a, b])
    offset, _x, _y, nc, wide = struct.unpack("<IiiII", data[:20])
    assert (offset, nc, wide) == (20, 0, 1)
    text = data[20:].decode("utf-16-le")
    assert text == f"{a.resolve()}\0{b.resolve()}\0\0"


@pytest.mark.skipif(not __import__("sys").platform.startswith("win"), reason="只有 Windows 有剪貼簿檔案格式")
def test_copy_files_roundtrip_via_powershell(tmp_path: Path) -> None:
    import subprocess

    path = make_png(tmp_path / "clip.png")
    try:
        clipboard.copy_files([path])
    except clipboard.ClipboardError as exc:    # 無剪貼簿的環境（例如沒有桌面的 CI）
        pytest.skip(str(exc))
    out = subprocess.run(["powershell", "-NoProfile", "-Command",
                          "Add-Type -AssemblyName System.Windows.Forms;"
                          "[System.Windows.Forms.Clipboard]::GetFileDropList() -join '|'"],
                         capture_output=True, text=True, timeout=30).stdout.strip()
    assert out.lower() == str(path.resolve()).lower()


def test_copy_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(clipboard.ClipboardError):
        clipboard.copy_files([tmp_path / "nope.gif"])


# ---------------------------------------------------------------- 設定
def test_active_webhook_selection() -> None:
    from soundboard.models import AppSettings

    s = AppSettings()
    assert s.active_webhook() is None
    s.discord_webhooks = {"A": VALID_URL, "B": VALID_URL + "x"}
    assert s.active_webhook() == ("A", VALID_URL)          # 沒有指定時用第一個
    s.discord_webhook_active = "B"
    assert s.active_webhook() == ("B", VALID_URL + "x")
    s.discord_webhook_active = "已刪除"
    assert s.active_webhook() == ("A", VALID_URL)          # 指定的不存在時退回第一個


def test_old_settings_file_still_loads_and_webhook_roundtrips(tmp_path: Path) -> None:
    from soundboard.config import load_settings, save_settings
    from soundboard.models import AppSettings

    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"sound_dir": "D:/音效", "monitor_volume": 0.5}), encoding="utf-8")
    old = load_settings(path)
    assert old.discord_webhooks == {} and old.sticker_dir == "" and old.sticker_send_size == 0
    old.discord_webhooks = {"頻道": VALID_URL}
    old.sticker_send_size = 240
    save_settings(old, path)
    again = load_settings(path)
    assert again.discord_webhooks == {"頻道": VALID_URL} and again.sticker_send_size == 240
    assert isinstance(again, AppSettings)
