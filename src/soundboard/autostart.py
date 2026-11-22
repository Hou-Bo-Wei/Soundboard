"""開機自動啟動：在目前使用者的登錄檔「Run」機碼登記音效版。

用 ``HKEY_CURRENT_USER`` 而不是工作排程器或「啟動」資料夾捷徑：
不需要系統管理員權限、不必產生額外的檔案，Windows 的「工作管理員 → 啟動應用程式」也看得到、能停用。
"""

from __future__ import annotations

import logging
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path

from soundboard.config import is_frozen
from soundboard.i18n import tr

logger = logging.getLogger(__name__)

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
# 使用者在工作管理員停用啟動項目時，Windows 不會刪掉 Run 的值，而是記在這裡
APPROVED_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"
VALUE_NAME = "Soundboard"
AUTOSTART_FLAG = "--autostart"
# 切換語言後重新啟動：新的程式會等舊的結束（釋放單一執行個體的鎖）後才啟動
RESTART_FLAG = "--restart"
# StartupApproved 值的第一個位元組：偶數（0x02、0x06）＝啟用，奇數（0x03、0x07）＝已停用
_APPROVED_DISABLED_BIT = 0x01


def launch_command() -> str:
    """開機時要執行的指令（目前這個程式本身加上 ``--autostart``）。

    Returns:
        str: 完整指令，路徑已加上引號。
    """
    if is_frozen():
        return f'"{Path(sys.executable).resolve()}" {AUTOSTART_FLAG}'
    # 原始碼版：用 pythonw 才不會在登入時跳出黑色主控台視窗
    python = Path(sys.executable)
    pythonw = python.with_name("pythonw.exe")
    return f'"{pythonw if pythonw.exists() else python}" -m soundboard {AUTOSTART_FLAG}'


def command_target(command: str | None) -> Path | None:
    """取出指令要執行的程式路徑。

    Args:
        command (str | None): 登錄檔裡的指令。

    Returns:
        Path | None: 執行檔路徑；指令是空的或無法解析時為 None。
    """
    if not command:
        return None
    try:
        parts = shlex.split(command, posix=False)
    except ValueError:
        return None
    return Path(parts[0].strip('"')) if parts else None


@dataclass
class AutostartManager:
    """讀寫開機自動啟動的登錄檔設定。

    機碼位置可以替換，測試時改用暫存機碼，不會動到使用者真正的開機項目。

    Attributes:
        run_key (str): ``HKEY_CURRENT_USER`` 底下的 Run 機碼。
        approved_key (str): 記錄「工作管理員停用」狀態的機碼。
        value_name (str): 值的名稱。
    """

    run_key: str = RUN_KEY
    approved_key: str = APPROVED_KEY
    value_name: str = VALUE_NAME

    @property
    def supported(self) -> bool:
        """這個系統是否支援（只有 Windows）。"""
        return sys.platform == "win32"

    def registered_command(self) -> str | None:
        """登錄檔裡登記的指令。

        Returns:
            str | None: 指令；沒有登記或讀取失敗時為 None。
        """
        if not self.supported:
            return None
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.run_key) as key:
                value, _kind = winreg.QueryValueEx(key, self.value_name)
        except OSError:
            return None
        return str(value) if value else None

    def disabled_in_task_manager(self) -> bool:
        """使用者是否在「工作管理員 → 啟動應用程式」把它停用了。

        Returns:
            bool: 已停用時為 True。
        """
        if not self.supported:
            return False
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.approved_key) as key:
                value, _kind = winreg.QueryValueEx(key, self.value_name)
        except OSError:
            return False
        return isinstance(value, bytes) and bool(value) and bool(value[0] & _APPROVED_DISABLED_BIT)

    def is_enabled(self) -> bool:
        """開機時是否真的會啟動（有登記、而且沒有在工作管理員被停用）。

        Returns:
            bool: 會啟動時為 True。
        """
        return self.registered_command() is not None and not self.disabled_in_task_manager()

    def registered_elsewhere(self) -> Path | None:
        """登記的是不是另一個位置的程式（例如舊版 exe 還留在別的資料夾）。

        Returns:
            Path | None: 另一個程式的路徑；沒有登記或就是目前這個程式時為 None。
        """
        target = command_target(self.registered_command())
        current = command_target(launch_command())
        if target is None or current is None:
            return None
        return None if str(target).lower() == str(current).lower() else target

    def enable(self) -> None:
        """登記成開機自動啟動目前這個程式。

        使用者在程式裡主動打開時，也一併清掉工作管理員的「已停用」標記，否則打開了也不會生效。

        Raises:
            OSError: 無法寫入登錄檔時拋出。
        """
        if not self.supported:
            # 設定頁會把例外訊息顯示在狀態列，所以要翻譯
            raise OSError(tr("僅 Windows 支援開機自動啟動"))
        import winreg

        command = launch_command()
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, self.run_key, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, self.value_name, 0, winreg.REG_SZ, command)
        self._delete(self.approved_key)
        logger.info("已設定開機自動啟動：%s", command)

    def disable(self) -> None:
        """取消開機自動啟動。

        Raises:
            OSError: 無法寫入登錄檔時拋出（值本來就不存在不算錯誤）。
        """
        if not self.supported:
            return
        self._delete(self.run_key)
        self._delete(self.approved_key)
        logger.info("已取消開機自動啟動")

    def repair_if_stale(self) -> bool:
        """登記的程式已經不存在（exe 被搬走）時，改登記成目前這個程式。

        只修「指向不存在檔案」的情況：如果登記的是另一個還在的 exe，代表使用者可能刻意這樣設定，不動它。

        Returns:
            bool: 有修正時為 True。
        """
        target = command_target(self.registered_command())
        if target is None or target.exists() or self.registered_elsewhere() is None:
            return False
        try:
            self.enable()
        except OSError as exc:
            logger.warning("無法更新開機自動啟動的路徑：%s", exc)
            return False
        logger.info("開機自動啟動原本指向已不存在的 %s，已改成目前的程式", target)
        return True

    def _delete(self, key_path: str) -> None:
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, self.value_name)
        except FileNotFoundError:
            pass
