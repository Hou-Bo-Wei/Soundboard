"""LINE 貼圖下載器（tools/line_sticker_dl）：文字貼圖的項目、line_meta.json、下載流程（假網路，不連線）。"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("PIL")
from PIL import Image  # noqa: E402

from soundboard import sticker_meta, stickers  # noqa: E402

TOOL = Path(__file__).resolve().parents[1] / "tools" / "line_sticker_dl" / "line_sticker_dl.py"


@pytest.fixture(scope="module")
def dl():
    spec = importlib.util.spec_from_file_location("line_sticker_dl", TOOL)
    module = importlib.util.module_from_spec(spec)
    sys.modules["line_sticker_dl"] = module
    spec.loader.exec_module(module)
    return module


def _png(color: tuple[int, int, int, int], size=(40, 30)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGBA", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


MESSAGE_META = {"packageId": 777, "stickerResourceType": "PER_STICKER_TEXT",
                "stickers": [{"id": 11, "customPlus": {"defaultText": "早安"}}, {"id": 12}]}
NAME_META = {"packageId": 888, "stickerResourceType": "NAME_TEXT", "stickers": [{"id": 21}]}


def test_text_items_from_meta(dl) -> None:
    message = dl.text_items_from_meta("777", MESSAGE_META)
    assert [d["textKind"] for d in message] == ["message", "message"]
    assert message[0]["defaultText"] == "早安" and message[1]["defaultText"] == ""
    assert message[0]["customApi"] == "https://store.line.me/overlay/sticker/777/11/iPhone/sticker.png?text="
    assert message[0]["staticUrl"].endswith("/sticker/11/iPhone/base/plus/sticker@2x.png")
    [name] = dl.text_items_from_meta("888", NAME_META)
    assert name["textKind"] == "name" and name["baseUrl"].endswith("/sticker/21/iPhone/base/sticker@2x.png")
    assert dl.text_items_from_meta("1", {"stickerResourceType": "STATIC", "stickers": [{"id": 1}]}) == []


def test_download_message_pack_writes_meta_readable_by_soundboard(dl, tmp_path: Path,
                                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    requested: list[str] = []

    def fake_get(url, retries=3, referer=None):
        requested.append(url)
        if url.startswith("https://store.line.me/stickershop/product/"):
            return b'<meta property="og:title" content="Test Pack - LINE stickers">'
        if url.endswith("productInfo.meta"):
            return json.dumps(MESSAGE_META).encode()
        if "/overlay/" in url:
            return _png((0, 0, 0, 0))
        return _png((0, 0, 255, 255))

    monkeypatch.setattr(dl, "http_get", fake_get)
    folder, ok, fail = dl.download_pack(("sticker", "777"), str(tmp_path), ["apng"], log=lambda _m: None)
    assert (ok, fail) == (2, 0)
    folder = Path(folder)
    assert sorted(p.name for p in folder.iterdir()) == [".line", "001.png", "002.png", "line_meta.json"]
    assert sorted(p.name for p in (folder / ".line").iterdir()) == [
        "001_base.png", "001_overlay.png", "002_base.png", "002_overlay.png"]
    meta = json.loads((folder / "line_meta.json").read_text(encoding="utf-8"))
    assert meta["resource_type"] == "PER_STICKER_TEXT" and meta["store_url"].endswith("/777/zh-Hant")

    # 音效版讀得懂：兩張都是訊息貼圖，第一張帶預設文字
    items = stickers.scan_stickers(tmp_path)
    assert [it.name for it in items] == ["001", "002"]
    assert items[0].text_spec.kind == "message" and items[0].default_text == "早安"
    assert items[0].text_spec.base == folder / ".line" / "001_base.png"

    # 再下載一次：檔案都在就略過，但 line_meta.json 仍會重寫
    (folder / "line_meta.json").unlink()
    count = len(requested)
    dl.download_pack(("sticker", "777"), str(tmp_path), ["apng"], log=lambda _m: None)
    assert (folder / "line_meta.json").is_file()
    assert not any("/sticker/" in u for u in requested[count:] if "productInfo" not in u)


def test_download_name_pack(dl, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_get(url, retries=3, referer=None):
        if url.startswith("https://store.line.me/"):
            return b'<meta property="og:title" content="Name Pack">'
        if url.endswith("productInfo.meta"):
            return json.dumps(NAME_META).encode()
        return _png((0, 255, 0, 255))

    monkeypatch.setattr(dl, "http_get", fake_get)
    folder, ok, _fail = dl.download_pack(("sticker", "888"), str(tmp_path), ["apng"], log=lambda _m: None)
    assert ok == 1
    metas = sticker_meta.load_series_meta(Path(folder))
    assert metas["001"].text.kind == "name" and metas["001"].text.base.name == "001_base.png"
