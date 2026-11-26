"""文字貼圖與關鍵字：line_meta.json 讀取、掃描時附上資訊、關鍵字比對與存檔、文字位置判斷、畫字（不連網）。"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

pytest.importorskip("PIL")
from PIL import Image, ImageDraw  # noqa: E402

from soundboard import sticker_meta, sticker_text, stickers  # noqa: E402
from soundboard.gui import sticker_actions  # noqa: E402

SIZE = (120, 100)


# ---------------------------------------------------------------- 測試用的 LINE 系列資料夾
def _base() -> Image.Image:
    im = Image.new("RGBA", SIZE, (0, 0, 0, 0))
    ImageDraw.Draw(im).ellipse((30, 40, 90, 95), fill=(255, 255, 255, 255), outline=(0, 0, 0, 255), width=3)
    return im


def _with_block(im: Image.Image, box: tuple[int, int, int, int], color=(20, 20, 20, 255)) -> Image.Image:
    out = im.copy()
    ImageDraw.Draw(out).rectangle(box, fill=color)
    return out


def make_series(root: Path) -> Path:
    """一個系列：001 訊息貼圖、002 名字貼圖、003 一般貼圖、003_popup 特效層。"""
    folder = root / "測試系列_12345"
    (folder / ".line").mkdir(parents=True)
    base = _base()
    overlay = _with_block(Image.new("RGBA", SIZE, (0, 0, 0, 0)), (40, 8, 80, 24), (200, 30, 30, 255))
    base.save(folder / ".line" / "001_base.png")
    overlay.save(folder / ".line" / "001_overlay.png")
    composite = base.copy()
    composite.alpha_composite(overlay)
    composite.save(folder / "001.png")
    base.save(folder / ".line" / "002_base.png")
    _with_block(base, (44, 10, 76, 20)).save(folder / "002.png")       # 「****」的位置
    base.save(folder / "003.png")
    base.save(folder / "003_popup.png")
    meta = {"format": 1, "kind": "sticker", "package_id": "12345", "title": "測試系列",
            "resource_type": "PER_STICKER_TEXT", "store_url": "https://store.line.me/stickershop/product/12345/zh-Hant",
            "stickers": [
                {"stem": "001", "id": "9001", "default_text": "早安",
                 "text": {"kind": "message", "base": ".line/001_base.png", "overlay": ".line/001_overlay.png",
                          "api": "https://store.line.me/overlay/sticker/12345/9001/iPhone/sticker.png?text="}},
                {"stem": "002", "id": "9002", "default_text": "",
                 "text": {"kind": "name", "base": ".line/002_base.png"}},
                {"stem": "003", "id": "9003", "default_text": ""},
            ]}
    (folder / "line_meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return folder


@pytest.fixture()
def series(tmp_path: Path) -> list[stickers.StickerItem]:
    make_series(tmp_path)
    return stickers.scan_stickers(tmp_path)


def _by_name(items: list[stickers.StickerItem], name: str) -> stickers.StickerItem:
    return next(it for it in items if it.name == name)


# ---------------------------------------------------------------- 掃描與 meta
def test_scan_attaches_meta_and_skips_aux_files(series: list[stickers.StickerItem]) -> None:
    assert [it.name for it in series] == ["001", "002", "003"]     # 沒有 .line 底圖與 _popup 特效層
    msg, name, plain = series
    assert msg.text_spec.kind == "message" and msg.default_text == "早安"
    assert msg.text_spec.api.endswith("?text=") and msg.text_spec.referer.startswith("https://store.line.me/")
    assert name.text_spec.kind == "name" and name.text_spec.sample == name.path
    assert plain.meta.sticker_id == "9003" and plain.text_spec is None


def test_broken_meta_is_ignored(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    _base().save(tmp_path / "a" / "001.png")
    (tmp_path / "a" / "line_meta.json").write_text("{壞掉", encoding="utf-8")
    [item] = stickers.scan_stickers(tmp_path)
    assert item.meta is None


# ---------------------------------------------------------------- 關鍵字
def test_parse_keywords_splits_and_dedupes() -> None:
    assert sticker_meta.parse_keywords(" 早安, 你好、早安；Hi  hi\n謝謝 ") == ["早安", "你好", "Hi", "謝謝"]
    assert sticker_meta.parse_keywords("") == []


@pytest.mark.parametrize(("query", "expected"), [
    ("早安", True), ("大家早安", True), ("GOOD", True), ("早安！", True), ("晚安", False), ("", False),
    ("早", False),              # 只打一個字時必須完全相同，否則幾千張貼圖會跳出一大堆
    ("安", True),               # 一個字的關鍵字「安」完全相同
    ("安安你好", False),        # 一個字的關鍵字「安」不因為被包含就符合
])
def test_keyword_matches(query: str, expected: bool) -> None:
    assert sticker_meta.keyword_matches(query, ["早安", "good morning", "安"]) is expected


def test_normalize_term_drops_punctuation_and_case() -> None:
    assert sticker_meta.normalize_term(" OK! ") == "ok"
    assert sticker_meta.normalize_term("哩賀~") == "哩賀"
    assert sticker_meta.normalize_term("★亮金金★") == "亮金金"
    assert sticker_meta.normalize_term("good night") == "goodnight"


@pytest.mark.parametrize(("query", "term", "score"), [
    ("晚安", "晚安", 100),
    ("好啦再見", "再見", 64),       # 打的字以關鍵字結尾：60 + 2×2
    ("再見", "再見啦", 50),         # 關鍵字以打的字開頭
    ("放假", "等待放假", 40),       # 關鍵字包含打的字
    ("今天好累喔", "好累", 34),     # 打的字包含關鍵字：30 + 2×2
    ("早", "早安", 0),              # 一個字只接受完全相同
    ("早早早", "早", 0),            # 一個字的關鍵字不因為被包含就符合
    ("吃飯", "睡覺", 0),
])
def test_term_score_table(query: str, term: str, score: int) -> None:
    assert sticker_meta.term_score(query, term) == score


def test_match_score_prefers_text_on_image() -> None:
    on_image = sticker_meta.SearchTerms.build("再見!", ["掰掰"])
    synonym = sticker_meta.SearchTerms.build("", ["再見", "掰掰"])
    q = sticker_meta.normalize_term("再見")
    assert sticker_meta.match_score(q, on_image) == 110          # 圖上的字比中再加 10 分
    assert sticker_meta.match_score(q, synonym) == 100
    assert sticker_meta.match_score(sticker_meta.normalize_term("bye"), on_image) == 0


def test_load_series_tags(tmp_path: Path) -> None:
    (tmp_path / sticker_meta.TAGS_FILENAME).write_text(json.dumps({
        "001": {"t": " 哩賀~ ", "k": ["你好", " ", "hi"]},
        "002": ["生氣", "哼"],                  # 只有關鍵字的簡寫
        "003": "壞掉的項目",
    }, ensure_ascii=False), encoding="utf-8")
    tags = sticker_meta.load_series_tags(tmp_path)
    assert tags["001"] == sticker_meta.StickerTags("哩賀~", ("你好", "hi"))
    assert tags["002"] == sticker_meta.StickerTags("", ("生氣", "哼"))
    assert "003" not in tags
    (tmp_path / sticker_meta.TAGS_FILENAME).write_text("{壞掉", encoding="utf-8")
    assert sticker_meta.load_series_tags(tmp_path) == {}


def test_scan_attaches_tags(tmp_path: Path) -> None:
    (tmp_path / "組_123").mkdir()
    for stem in ("001", "002"):
        _base().save(tmp_path / "組_123" / f"{stem}.png")
    (tmp_path / "組_123" / sticker_meta.TAGS_FILENAME).write_text(
        json.dumps({"001": {"t": "早安", "k": ["morning"]}}, ensure_ascii=False), encoding="utf-8")
    first, second = stickers.scan_stickers(tmp_path)
    assert first.tags == sticker_meta.StickerTags("早安", ("morning",))
    assert second.tags is None


def test_keyword_store_roundtrip(tmp_path: Path) -> None:
    store = sticker_meta.KeywordStore.load(tmp_path)
    assert store.get("a/001.png") == []
    store.set("a/001.png", ["早安", "你好"])
    store.set("b/002.png", ["掰掰"])
    store.set("b/002.png", [])             # 清空等於刪除
    again = sticker_meta.KeywordStore.load(tmp_path)
    assert again.words == {"a/001.png": ["早安", "你好"]}
    (tmp_path / sticker_meta.KEYWORDS_FILENAME).write_text("not json", encoding="utf-8")
    assert sticker_meta.KeywordStore.load(tmp_path).words == {}


# ---------------------------------------------------------------- 文字位置
def test_layout_from_message_overlay(series: list[stickers.StickerItem]) -> None:
    layout = sticker_text.find_layout(_by_name(series, "001").text_spec)
    assert layout.box == (40, 8, 81, 25)
    assert layout.color == (200, 30, 30)
    assert layout.max_width >= 41


def test_layout_from_name_difference(series: list[stickers.StickerItem]) -> None:
    layout = sticker_text.find_layout(_by_name(series, "002").text_spec)
    left, top, right, bottom = layout.box
    assert 40 <= left <= 44 and 77 <= right <= 81   # 「****」的範圍（44～76）加上一點邊
    assert top < 10 and bottom > 21                 # 高度放大（星號比中文字矮）
    assert layout.color == (20, 20, 20)
    assert layout.max_width == right - left         # 名字不往左右延伸


# ---------------------------------------------------------------- 畫字
def _ink_inside(image: Image.Image, box: tuple[int, int, int, int]) -> int:
    crop = image.crop(box)
    return sum(1 for p in sticker_text._pixels(crop) if p[3] > 0 and p[:3] != (255, 255, 255))


def test_render_name_locally(series: list[stickers.StickerItem]) -> None:
    spec = _by_name(series, "002").text_spec
    image, by_line = sticker_text.render(spec, "小明")
    assert not by_line and image.size == SIZE
    assert _ink_inside(image, (38, 0, 84, 30)) > 20         # 名字畫在原本「****」的位置


def test_render_message_with_line_overlay(series: list[stickers.StickerItem], tmp_path: Path,
                                          monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sticker_text, "_overlay_cache_dir", lambda: tmp_path / "cache")
    spec = _by_name(series, "001").text_spec
    calls: list[tuple[str, str]] = []
    overlay = _with_block(Image.new("RGBA", (SIZE[0] * 2, SIZE[1] * 2), (0, 0, 0, 0)), (0, 0, 19, 19), (0, 0, 255, 255))
    buffer = io.BytesIO()
    overlay.save(buffer, format="PNG")

    def fake_fetch(url: str, referer: str) -> bytes:
        calls.append((url, referer))
        return buffer.getvalue()

    image, by_line = sticker_text.render(spec, "早安 你好", fetch=fake_fetch)
    assert by_line and image.getpixel((5, 5)) == (0, 0, 255, 255)      # 圖層縮到底圖大小再疊上去
    assert calls == [(spec.api + "%E6%97%A9%E5%AE%89%20%E4%BD%A0%E5%A5%BD", spec.referer)]
    sticker_text.render(spec, "早安 你好", fetch=fake_fetch)
    assert len(calls) == 1                                               # 第二次用快取


def test_render_message_falls_back_when_offline(series: list[stickers.StickerItem], tmp_path: Path,
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sticker_text, "_overlay_cache_dir", lambda: tmp_path / "cache")

    def offline(_url: str, _referer: str) -> bytes:
        raise OSError("沒有網路")

    image, by_line = sticker_text.render(_by_name(series, "001").text_spec, "晚安", fetch=offline)
    assert not by_line
    assert _ink_inside(image, (30, 0, 90, 34)) > 20


def test_render_empty_text_uses_default(series: list[stickers.StickerItem]) -> None:
    image, _ = sticker_text.render(_by_name(series, "001").text_spec, "   ", fetch=None)
    assert _ink_inside(image, (30, 0, 90, 34)) > 20


def test_materialize_returns_original_or_rendered(series: list[stickers.StickerItem], tmp_path: Path,
                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sticker_actions, "temp_dir", lambda: tmp_path / "tmp")
    monkeypatch.setattr(sticker_text, "fetch_overlay", lambda *_a: (_ for _ in ()).throw(OSError("offline")))
    monkeypatch.setattr(sticker_text, "_overlay_cache_dir", lambda: tmp_path / "cache")
    plain, name = _by_name(series, "003"), _by_name(series, "002")
    assert sticker_actions.materialize(plain, "早安") == plain.path            # 一般貼圖不套字
    assert sticker_actions.materialize(name, "") == name.path                 # 沒有文字用原圖
    out = sticker_actions.materialize(name, "小明")
    assert out.parent == tmp_path / "tmp" / "text" and out.suffix == ".png"
    assert Image.open(out).size == SIZE
    assert sticker_actions.display_name(name, "小明") == "002「小明」"
    assert sticker_actions.display_name(plain, "小明") == "003"
