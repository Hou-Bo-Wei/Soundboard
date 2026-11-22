"""快捷鍵格式化與驗證的單元測試。"""

from __future__ import annotations

from soundboard.hotkeys import MOD_ALT, MOD_CONTROL, MOD_SHIFT, blocks_typing, format_hotkey, vk_name
from soundboard.models import AppSettings, Hotkey


def test_format_hotkey() -> None:
    assert format_hotkey(Hotkey(MOD_CONTROL | MOD_ALT, 0x31)) == "Ctrl+Alt+1"
    assert format_hotkey(Hotkey(0, 0x61)) == "Num1"
    assert format_hotkey(Hotkey(MOD_SHIFT, 0x7C)) == "Shift+F13"
    assert format_hotkey(None) == ""
    assert vk_name(0x41) == "A"


def test_blocks_typing_only_without_ctrl_alt() -> None:
    assert blocks_typing(Hotkey(0, 0x41))            # 單獨 A：會打不出字
    assert blocks_typing(Hotkey(MOD_SHIFT, 0x41))    # Shift+A 也是打字
    assert not blocks_typing(Hotkey(MOD_CONTROL, 0x41))
    assert not blocks_typing(Hotkey(0, 0x61))        # Num1
    assert not blocks_typing(Hotkey(0, 0x7C))        # F13


def test_hotkey_json_roundtrip_and_validation() -> None:
    hk = Hotkey(MOD_ALT, 0x70)
    assert Hotkey.from_json(hk.to_json()) == hk
    assert Hotkey.from_json("Ctrl+1") is None
    assert Hotkey.from_json([1, 999]) is None


def test_old_settings_file_still_loads() -> None:
    # 舊版設定檔沒有快捷鍵 / 小視窗欄位，必須能套用預設值
    s = AppSettings.from_dict({"sound_dir": "x", "unknown_field": 1})
    assert s.sound_hotkeys == {} and s.overlay_keys == [] and s.overlay_alpha == 0.85


def test_removed_action_hotkeys_are_not_registered() -> None:
    # 舊版設定檔可能還有已移除功能（變聲器）的快速鍵：不註冊，避免按下後毫無反應又佔用按鍵
    from types import SimpleNamespace

    from soundboard.gui.hotkey_controller import HotkeyController

    settings = AppSettings(action_hotkeys={"stop": [MOD_CONTROL, 0x31], "voice_fx": [MOD_CONTROL, 0x32]},
                           sound_hotkeys={"a.ogg": [MOD_CONTROL, 0x33]})
    controller = HotkeyController.__new__(HotkeyController)
    controller.app = SimpleNamespace(settings=settings)
    assert controller._all_targets() == ["action:stop", "sound:a.ogg"]
