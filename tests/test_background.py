"""開機自動啟動與系統匣的單元測試。

登錄檔測試只寫 ``HKEY_CURRENT_USER\\Software\\SoundboardTest-*`` 暫存機碼，結束後刪除，
不會碰到真正的開機項目（Run 機碼）。
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

from soundboard import autostart
from soundboard.autostart import AutostartManager, command_target, launch_command
from soundboard.gui.presenters import background_note
from soundboard.models import AppSettings
from soundboard.theme import OK, WARN
from soundboard.tray import SingleInstance, TrayMenuItem, instance_id, window_title

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="只有 Windows 有登錄檔與系統匣")


@pytest.fixture
def manager():
    """指向暫存機碼的 AutostartManager，測試後整個刪除。"""
    import winreg

    base = rf"Software\SoundboardTest-{uuid.uuid4().hex[:8]}"
    yield AutostartManager(run_key=base + r"\Run", approved_key=base + r"\Approved")
    for sub in (r"\Run", r"\Approved", ""):
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, base + sub)
        except OSError:
            pass


def test_command_target_parses_quoted_path():
    assert command_target(r'"C:\My Apps\Soundboard.exe" --autostart') == Path(r"C:\My Apps\Soundboard.exe")
    assert command_target(None) is None
    assert command_target("") is None


def test_launch_command_has_flag():
    cmd = launch_command()
    assert cmd.endswith(autostart.AUTOSTART_FLAG)
    assert command_target(cmd) is not None


@windows_only
def test_enable_disable_roundtrip(manager):
    assert not manager.is_enabled()
    manager.enable()
    assert manager.registered_command() == launch_command()
    assert manager.is_enabled() and manager.registered_elsewhere() is None
    manager.disable()
    assert manager.registered_command() is None and not manager.is_enabled()
    manager.disable()   # 已經取消再取消不算錯誤


@windows_only
def test_task_manager_disabled_flag(manager):
    import winreg

    manager.enable()
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, manager.approved_key, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, manager.value_name, 0, winreg.REG_BINARY, b"\x03" + bytes(11))
    assert manager.disabled_in_task_manager() and not manager.is_enabled()
    manager.enable()    # 使用者在程式裡重新打開 → 清掉停用標記
    assert manager.is_enabled()


@windows_only
def test_repair_only_missing_target(manager, tmp_path):
    import winreg

    def register(command: str) -> None:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, manager.run_key, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, manager.value_name, 0, winreg.REG_SZ, command)

    other = tmp_path / "Other Soundboard.exe"
    other.write_bytes(b"")
    register(f'"{other}" --autostart')
    assert manager.registered_elsewhere() == other
    assert not manager.repair_if_stale(), "另一個還存在的 exe 不能被改掉"
    other.unlink()
    assert manager.repair_if_stale()
    assert manager.registered_command() == launch_command()


@windows_only
def test_single_instance_per_settings_file(tmp_path):
    ident = instance_id(tmp_path / "settings.json")
    first = SingleInstance(ident)
    assert first.acquire()
    assert not SingleInstance(ident).acquire()
    other = SingleInstance(instance_id(tmp_path / "other.json"))
    assert other.acquire(), "不同設定檔可以同時執行"
    first.release()
    other.release()
    assert SingleInstance(ident).acquire()


def test_instance_id_case_insensitive(tmp_path):
    assert instance_id(tmp_path / "Settings.json") == instance_id(tmp_path / "settings.json")
    assert window_title("abc").endswith("abc")


def test_menu_item_separator():
    assert TrayMenuItem().separator
    assert not TrayMenuItem("結束").separator


def test_new_settings_defaults_and_old_file():
    settings = AppSettings.from_dict({"sound_dir": "x"})
    assert settings.close_to_tray and settings.autostart_hidden and not settings.tray_hint_shown


def test_background_note():
    common = dict(supported=True, disabled_by_windows=False, elsewhere=None, tray_ok=True)
    assert background_note(registered=True, **common)[1] == OK
    assert background_note(registered=False, **common)[1] != WARN
    assert "工作管理員" in background_note(**{**common, "registered": True, "disabled_by_windows": True})[0]
    assert "另一個" in background_note(**{**common, "registered": True, "elsewhere": Path("C:/x.exe")})[0]
    assert "系統匣" in background_note(**{**common, "registered": False, "tray_ok": False})[0]
