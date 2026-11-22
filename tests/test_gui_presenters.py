"""介面顯示邏輯（純函式）的測試：不需要開啟視窗。"""

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from soundboard import i18n
from soundboard.config import load_settings, save_settings
from soundboard.grouping import Section
from soundboard.gui.presenters import (
    LABEL_DEFAULT, LABEL_NONE, audio_warnings, display_label, grid_columns, label_for, label_from_display,
    section_header_text, stored_device, tile_text, voice_status, wrap_text,
)
from soundboard.gui.widgets import rounded_rect_points
from soundboard.theme import FG_MUTED, OK


def test_audio_warnings_none_when_all_good() -> None:
    assert audio_warnings(True, None) == []


def test_audio_warnings_virtual_mic_missing() -> None:
    warnings = audio_warnings(False, None)
    assert len(warnings) == 1
    assert "通話對象無法聽到" in warnings[0].short
    assert "VB-Audio" in warnings[0].detail


def test_audio_warnings_monitor_redirected() -> None:
    warnings = audio_warnings(True, "耳機")
    assert len(warnings) == 1 and "「耳機」" in warnings[0].detail
    assert "找不到其他播放裝置" in audio_warnings(True, "")[0].detail
    assert len(audio_warnings(False, "耳機")) == 2


def test_label_for_and_stored_device_roundtrip() -> None:
    names = ["耳機", "喇叭"]
    assert label_for(LABEL_NONE, names, allow_default=True) == LABEL_NONE
    assert label_for("耳機", names, allow_default=True) == "耳機"
    assert label_for("拔掉的裝置", names, allow_default=True) == LABEL_DEFAULT
    assert label_for("拔掉的裝置", names, allow_default=False) == LABEL_NONE
    assert label_for(None, names, allow_default=True) == LABEL_DEFAULT
    assert stored_device(LABEL_DEFAULT) is None
    assert stored_device(LABEL_NONE) == LABEL_NONE
    assert stored_device("耳機") == "耳機"


@pytest.fixture
def english(tmp_path: Path) -> Iterator[None]:
    """暫時切換成英文介面（用測試專用的翻譯檔），結束後切回繁體中文。"""
    (tmp_path / "en.json").write_text(json.dumps({
        LABEL_DEFAULT: "(System default)",
        LABEL_NONE: "(Not used)",
    }), encoding="utf-8")
    i18n.set_language("en", tmp_path)
    try:
        yield
    finally:
        i18n.set_language(i18n.SOURCE_LANGUAGE)


def test_special_labels_display_translated_but_store_original(english: None) -> None:
    assert display_label(LABEL_NONE) == "(Not used)"
    assert display_label(LABEL_DEFAULT) == "(System default)"
    assert display_label("耳機") == "耳機"   # 裝置名稱是使用者資料，不翻譯
    # 使用者在英文介面選了「(Not used)」：存進設定檔的仍是原本的繁體中文字串
    assert stored_device(label_from_display("(Not used)")) == "（不使用）"
    assert stored_device(label_from_display("(System default)")) is None
    assert label_from_display("耳機") == "耳機"
    # 尚未翻譯的原文也能辨識
    assert label_from_display(LABEL_NONE) == LABEL_NONE


def test_legacy_settings_file_not_used_still_recognized(tmp_path: Path, english: None) -> None:
    """舊版設定檔的「（不使用）」在任何介面語言下讀回都要被辨識為不使用，存回去也不變。"""
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"virtual_mic_device": "（不使用）", "mic_input_device": "（不使用）"},
                               ensure_ascii=False), encoding="utf-8")
    settings = load_settings(path)
    names = ["CABLE Input (VB-Audio Virtual Cable)", "耳機"]
    for value in (settings.virtual_mic_device, settings.mic_input_device):
        choice = label_for(value, names, allow_default=True)
        assert choice == LABEL_NONE
        shown = display_label(choice)
        assert shown == "(Not used)"
        # 下拉選單的顯示文字轉回儲存值後，仍是原本的字串
        assert stored_device(label_from_display(shown)) == "（不使用）"
    save_settings(settings, path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["virtual_mic_device"] == raw["mic_input_device"] == "（不使用）"


def test_tile_text_prefixes_and_hotkey() -> None:
    plain = tile_text("哈哈", selected=False, join=False, pinned=False, hotkey="")
    assert plain.title == "哈哈" and plain.subtitle == "" and plain.full == "哈哈"
    full = tile_text("哈哈", selected=True, join=True, pinned=True, hotkey="Ctrl+Alt+1")
    assert full.title == "☑ 🚪 📌 哈哈"
    assert full.subtitle == "⌨ Ctrl+Alt+1"
    assert full.full == "☑ 🚪 📌 哈哈\n⌨ Ctrl+Alt+1"


def test_section_header_text() -> None:
    section = Section("搞笑", [], total=5, collapsed=False)
    assert section_header_text(section, searching=False) == ("▼   🗂  搞笑", "5")
    collapsed = Section(None, [], total=3, collapsed=True)
    left, right = section_header_text(collapsed, searching=True)
    assert left.startswith("►") and "未分組" in left and right == "0 / 3"


def test_grid_columns() -> None:
    assert grid_columns(0, 170) == 1
    assert grid_columns(169, 170) == 1
    assert grid_columns(340, 170) == 2
    assert grid_columns(1000, 170) == 5


def test_voice_status() -> None:
    assert voice_status(None)[1] == FG_MUTED
    assert voice_status(True) == ("Discord 語音：🟢 通話中", OK)
    assert "未加入語音頻道" in voice_status(False)[0]


def _measure(text: str) -> int:
    return 10 * len(text)  # 每個字 10 像素


def test_wrap_text_by_character() -> None:
    assert wrap_text("一二三四五", _measure, 30, 3) == "一二三\n四五"
    assert wrap_text("短", _measure, 30, 3) == "短"
    assert wrap_text("第一行\n第二", _measure, 100, 3) == "第一行\n第二"


def test_wrap_text_truncates_with_ellipsis() -> None:
    result = wrap_text("一二三四五六七八九十", _measure, 30, 2)
    lines = result.split("\n")
    assert len(lines) == 2
    assert lines[-1].endswith("…") and all(_measure(line) <= 30 for line in lines)
    assert wrap_text("任何", _measure, 30, 0) == ""


def test_rounded_rect_points() -> None:
    points = rounded_rect_points(0, 0, 100, 40, 8)
    assert len(points) == 40
    xs, ys = points[0::2], points[1::2]
    assert min(xs) == 0 and max(xs) == 100 and min(ys) == 0 and max(ys) == 40
    # 半徑超過高度一半時會被限制，不會畫出反向的形狀
    clamped = rounded_rect_points(0, 0, 100, 10, 50)
    assert all(0 <= x <= 100 for x in clamped[0::2]) and all(0 <= y <= 10 for y in clamped[1::2])
