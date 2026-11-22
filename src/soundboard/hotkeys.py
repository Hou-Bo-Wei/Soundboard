"""全域快捷鍵：即使焦點在遊戲中也能觸發。

採用 Win32 ``RegisterHotKey``，而不是低階鍵盤勾子（keyboard hook）：
* 不需要系統管理員權限、不需要額外套件；
* 防毒 / 反作弊軟體較不會把它當成鍵盤側錄；
* 系統負責比對按鍵組合，不會因為我們的程式卡住而拖慢整台電腦的鍵盤輸入。

代價是快捷鍵會被「吃掉」（遊戲收不到該按鍵），所以建議使用 Ctrl/Alt 組合鍵、
數字鍵盤或 F13–F24 這類遊戲不常用的按鍵。
"""

from __future__ import annotations

import ctypes
import logging
import queue
import sys
import threading
from collections.abc import Callable
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout

from soundboard.i18n import N_, tr
from soundboard.models import Hotkey

logger = logging.getLogger(__name__)

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
# 按住不放時不要重複觸發（否則一個音效會被連續播放好幾次）
MOD_NOREPEAT = 0x4000

WM_QUIT = 0x0012
WM_HOTKEY = 0x0312
WM_APP = 0x8000

VK_SHIFT, VK_CONTROL, VK_MENU, VK_LWIN, VK_RWIN = 0x10, 0x11, 0x12, 0x5B, 0x5C
MODIFIER_VKS: frozenset[int] = frozenset({VK_SHIFT, VK_CONTROL, VK_MENU, VK_LWIN, VK_RWIN,
                                          0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5})

_VK_NAMES: dict[int, str] = {
    0x08: "Backspace", 0x09: "Tab", 0x0D: "Enter", 0x13: "Pause", 0x14: "CapsLock", 0x1B: "Esc",
    0x20: "Space", 0x21: "PageUp", 0x22: "PageDown", 0x23: "End", 0x24: "Home",
    0x25: "←", 0x26: "↑", 0x27: "→", 0x28: "↓", 0x2C: "PrintScreen", 0x2D: "Insert", 0x2E: "Delete",
    0x6A: "Num*", 0x6B: "Num+", 0x6D: "Num-", 0x6E: "Num.", 0x6F: "Num/",
    0x90: "NumLock", 0x91: "ScrollLock",
    0xBA: ";", 0xBB: "=", 0xBC: ",", 0xBD: "-", 0xBE: ".", 0xBF: "/", 0xC0: "`",
    0xDB: "[", 0xDC: "\\", 0xDD: "]", 0xDE: "'",
}

# 多媒體鍵沒有通用的英文縮寫，名稱要依介面語言顯示（模組載入時只標記，vk_name 才 tr）。
# 設定檔只存虛擬鍵碼，所以顯示文字隨語言改變不影響已儲存的快速鍵。
_MEDIA_VK_NAMES: dict[int, str] = {
    0xAD: N_("靜音鍵"), 0xAE: N_("音量-"), 0xAF: N_("音量+"), 0xB0: N_("下一首"), 0xB1: N_("上一首"),
    0xB3: N_("播放/暫停"),
}

# 不搭配修飾鍵時會讓使用者「在任何地方都打不出這個字」的按鍵
_TYPING_VKS: frozenset[int] = frozenset(
    {0x08, 0x09, 0x0D, 0x20, *range(0x30, 0x3A), *range(0x41, 0x5B),
     0xBA, 0xBB, 0xBC, 0xBD, 0xBE, 0xBF, 0xC0, 0xDB, 0xDC, 0xDD, 0xDE}
)


def vk_name(vk: int) -> str:
    """虛擬鍵碼轉成易讀名稱。

    Args:
        vk (int): Windows 虛擬鍵碼。

    Returns:
        str: 例如 ``"A"``、``"F13"``、``"Num1"``。
    """
    if 0x30 <= vk <= 0x39 or 0x41 <= vk <= 0x5A:
        return chr(vk)
    if 0x60 <= vk <= 0x69:
        return f"Num{vk - 0x60}"
    if 0x70 <= vk <= 0x87:
        return f"F{vk - 0x6F}"
    if vk in _MEDIA_VK_NAMES:
        return tr(_MEDIA_VK_NAMES[vk])
    return _VK_NAMES.get(vk, f"Key{vk:#04x}")


def format_hotkey(hotkey: Hotkey | None) -> str:
    """快捷鍵轉成顯示文字。

    Args:
        hotkey (Hotkey | None): 快捷鍵。

    Returns:
        str: 例如 ``"Ctrl+Alt+1"``；None 時回傳空字串。
    """
    if hotkey is None:
        return ""
    parts = []
    if hotkey.modifiers & MOD_CONTROL:
        parts.append("Ctrl")
    if hotkey.modifiers & MOD_ALT:
        parts.append("Alt")
    if hotkey.modifiers & MOD_SHIFT:
        parts.append("Shift")
    if hotkey.modifiers & MOD_WIN:
        parts.append("Win")
    parts.append(vk_name(hotkey.vk))
    return "+".join(parts)


def blocks_typing(hotkey: Hotkey) -> bool:
    """此快捷鍵是否會妨礙一般打字（沒有 Ctrl/Alt/Win 的字母、數字、符號鍵）。

    Args:
        hotkey (Hotkey): 快捷鍵。

    Returns:
        bool: 會妨礙打字時為 True。
    """
    strong = hotkey.modifiers & (MOD_CONTROL | MOD_ALT | MOD_WIN)
    return not strong and hotkey.vk in _TYPING_VKS


def current_modifiers() -> int:
    """讀取目前實際按住的修飾鍵（Windows 限定）。

    Tk 的事件狀態沒有 Win 鍵、且 Alt 的位元在各平台不同，所以直接問系統。

    Returns:
        int: MOD_* 位元遮罩；非 Windows 時為 0。
    """
    if sys.platform != "win32":
        return 0
    get = ctypes.windll.user32.GetAsyncKeyState
    mods = 0
    if get(VK_CONTROL) & 0x8000:
        mods |= MOD_CONTROL
    if get(VK_MENU) & 0x8000:
        mods |= MOD_ALT
    if get(VK_SHIFT) & 0x8000:
        mods |= MOD_SHIFT
    if get(VK_LWIN) & 0x8000 or get(VK_RWIN) & 0x8000:
        mods |= MOD_WIN
    return mods


class GlobalHotkeys:
    """在專屬執行緒中註冊全域快捷鍵並接收觸發訊息。

    RegisterHotKey 的訊息只會送到「呼叫它的那條執行緒」的訊息佇列，
    所以註冊、取消註冊、接收都必須在同一條執行緒中完成。
    """

    def __init__(self, on_trigger: Callable[[int], None]) -> None:
        """
        Args:
            on_trigger (Callable[[int], None]): 快捷鍵被按下時呼叫，參數為綁定 id。
                會在快捷鍵執行緒中執行，實作必須是執行緒安全的。
        """
        self._on_trigger = on_trigger
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._ready = threading.Event()
        self._requests: queue.Queue[tuple[dict[int, Hotkey], Future[list[int]]]] = queue.Queue()

    @property
    def supported(self) -> bool:
        """目前平台是否支援全域快捷鍵。"""
        return sys.platform == "win32"

    def start(self) -> None:
        """啟動快捷鍵執行緒（重複呼叫無作用）。"""
        if not self.supported or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="global-hotkeys", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=2.0)

    def apply(self, bindings: dict[int, Hotkey], timeout: float = 2.0) -> list[int]:
        """以新的綁定取代目前所有快捷鍵。

        Args:
            bindings (dict[int, Hotkey]): 綁定 id → 快捷鍵；id 必須介於 1 與 0xBFFF。
            timeout (float): 等待註冊完成的秒數。

        Returns:
            list[int]: 註冊失敗的 id（通常是被其他程式佔用的組合鍵）。
        """
        if not self.supported or self._thread is None:
            return []
        future: Future[list[int]] = Future()
        self._requests.put((dict(bindings), future))
        ctypes.windll.user32.PostThreadMessageW(self._thread_id, WM_APP, 0, 0)
        try:
            return future.result(timeout=timeout)
        except FutureTimeout:
            logger.warning("快捷鍵註冊逾時")
            return list(bindings)

    def stop(self) -> None:
        """取消所有快捷鍵並結束執行緒。"""
        if self._thread is None:
            return
        ctypes.windll.user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
        self._thread.join(timeout=2.0)
        self._thread = None

    def _run(self) -> None:
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
        user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
        user32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                                        wintypes.UINT, wintypes.UINT, wintypes.UINT]

        self._thread_id = ctypes.windll.kernel32.GetCurrentThreadId()
        msg = wintypes.MSG()
        # 先呼叫一次 PeekMessage 讓系統建立這條執行緒的訊息佇列，
        # 否則主執行緒緊接著 PostThreadMessage 會失敗
        user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)
        self._ready.set()

        registered: set[int] = set()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY:
                try:
                    self._on_trigger(int(msg.wParam))
                except Exception:  # noqa: BLE001 - 回呼出錯不能讓快捷鍵執行緒整個停掉
                    logger.exception("快捷鍵處理失敗")
            elif msg.message == WM_APP:
                self._process_requests(user32, registered)

        for hotkey_id in registered:
            user32.UnregisterHotKey(None, hotkey_id)

    def _process_requests(self, user32: ctypes.WinDLL, registered: set[int]) -> None:
        while True:
            try:
                bindings, future = self._requests.get_nowait()
            except queue.Empty:
                return
            for hotkey_id in registered:
                user32.UnregisterHotKey(None, hotkey_id)
            registered.clear()
            failed: list[int] = []
            for hotkey_id, hotkey in bindings.items():
                if user32.RegisterHotKey(None, hotkey_id, hotkey.modifiers | MOD_NOREPEAT, hotkey.vk):
                    registered.add(hotkey_id)
                else:
                    failed.append(hotkey_id)
            future.set_result(failed)
