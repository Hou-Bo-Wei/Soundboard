"""偵測「進入 Discord 語音頻道」：Discord 開始從錄音裝置收音的瞬間。

Discord 只有在語音頻道中（或在設定裡按麥克風測試）時才會開啟錄音串流。
Windows 的音訊工作階段 API（WASAPI ``IAudioSessionManager2``）可以即時列出
「哪個程式正在從哪個錄音裝置收音」——工作列的麥克風使用中圖示就是用這套資訊，
所以不需要 Discord 帳號授權，也不用連 Discord 的 API。

舊版改讀登錄檔的「麥克風使用紀錄」，但新版 Windows（測試版 26300 起）不再即時寫入，
導致一直判斷為「未在語音中」，因此改用工作階段 API。

COM 呼叫刻意用 ctypes 直接呼叫 vtable、每個介面都在同一條執行緒裡明確 Release：
先前用 comtypes 時，背景執行緒建立的 COM 物件會被主執行緒的垃圾回收釋放，
在 Windows 上會直接讓程式閃退（存取違規），開啟麥克風測試時特別容易觸發。
"""

from __future__ import annotations

import ctypes
import logging
import sys
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from ctypes import wintypes
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# 正式版、PTB、Canary 的執行檔名稱都以 discord 開頭
_DISCORD_EXE_PREFIX = "discord"
_E_RENDER = 0               # EDataFlow.eRender
_E_CAPTURE = 1              # EDataFlow.eCapture
_DEVICE_STATE_ACTIVE = 1
_SESSION_STATE_ACTIVE = 1   # AudioSessionState.AudioSessionStateActive
_CLSCTX_ALL = 0x17
_COINIT_MULTITHREADED = 0x0
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

_CLSID_MMDEVICE_ENUMERATOR = "{BCDE0395-E52F-467C-8E3D-C4579291692E}"
_IID_IMMDEVICE_ENUMERATOR = "{A95664D2-9614-4F35-A746-DE8DB63617E6}"
_IID_IAUDIO_SESSION_MANAGER2 = "{77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F}"
_IID_IAUDIO_SESSION_CONTROL2 = "{BFB7FF88-7239-4FC9-8FA2-07C950BE9C6D}"

# vtable 索引（0–2 是 IUnknown 的 QueryInterface / AddRef / Release）
_QUERY_INTERFACE, _RELEASE = 0, 2
_ENUM_AUDIO_ENDPOINTS = 3   # IMMDeviceEnumerator
_COLLECTION_GET_COUNT, _COLLECTION_ITEM = 3, 4   # IMMDeviceCollection
_DEVICE_ACTIVATE = 3        # IMMDevice
_GET_SESSION_ENUMERATOR = 5  # IAudioSessionManager2（繼承 IAudioSessionManager 的 3、4）
_ENUM_GET_COUNT, _ENUM_GET_SESSION = 3, 4        # IAudioSessionEnumerator
_SESSION_GET_STATE = 3      # IAudioSessionControl
_SESSION_GET_PROCESS_ID = 14  # IAudioSessionControl2


class _GUID(ctypes.Structure):
    _fields_ = [("data1", wintypes.DWORD), ("data2", wintypes.WORD), ("data3", wintypes.WORD),
                ("data4", ctypes.c_ubyte * 8)]

    @classmethod
    def parse(cls, text: str) -> _GUID:
        guid = cls()
        ctypes.oledll.ole32.CLSIDFromString(ctypes.c_wchar_p(text), ctypes.byref(guid))
        return guid


def _call(obj: ctypes.c_void_p, index: int, *args: object, argtypes: tuple = ()) -> None:
    """呼叫 COM 介面 vtable 中第 ``index`` 個方法；HRESULT 失敗時拋出 OSError。"""
    vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    prototype = ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, *argtypes)
    prototype(vtable[index])(obj, *args)


def _release(obj: ctypes.c_void_p) -> None:
    if obj:
        vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        ctypes.WINFUNCTYPE(wintypes.ULONG, ctypes.c_void_p)(vtable[_RELEASE])(obj)


@contextmanager
def _com_ptr() -> Iterator[ctypes.c_void_p]:
    """取得一個 COM 介面指標的容器，離開時一定 Release（同一條執行緒）。"""
    ptr = ctypes.c_void_p()
    try:
        yield ptr
    finally:
        _release(ptr)


def capturing_process_ids() -> set[int]:
    """列出目前正在從任一錄音裝置收音的程式 PID。

    必須在已呼叫 CoInitializeEx 的執行緒中呼叫（見 :class:`VoiceWatcher`）。

    Returns:
        set[int]: PID 集合。

    Raises:
        OSError: COM 呼叫失敗時拋出（例如音訊服務沒有啟動）。
    """
    return session_process_ids(render=False, active_only=True)


def session_process_ids(*, render: bool, active_only: bool) -> set[int]:
    """列出在任一音訊裝置上有工作階段的程式 PID。

    必須在已呼叫 CoInitializeEx 的執行緒中呼叫。

    Args:
        render (bool): True 看播放裝置（程式發出聲音），False 看錄音裝置（程式在收音）。
        active_only (bool): 只列出目前正在使用中的工作階段。

    Returns:
        set[int]: PID 集合。

    Raises:
        OSError: COM 呼叫失敗時拋出（例如音訊服務沒有啟動）。
    """
    vp, u32, i32 = ctypes.c_void_p, ctypes.c_uint, ctypes.c_int
    pp = ctypes.POINTER(ctypes.c_void_p)
    pids: set[int] = set()
    with _com_ptr() as enumerator:
        ctypes.oledll.ole32.CoCreateInstance(
            ctypes.byref(_GUID.parse(_CLSID_MMDEVICE_ENUMERATOR)), None, _CLSCTX_ALL,
            ctypes.byref(_GUID.parse(_IID_IMMDEVICE_ENUMERATOR)), ctypes.byref(enumerator))
        with _com_ptr() as devices:
            _call(enumerator, _ENUM_AUDIO_ENDPOINTS, _E_RENDER if render else _E_CAPTURE, _DEVICE_STATE_ACTIVE, ctypes.byref(devices),
                  argtypes=(i32, wintypes.DWORD, pp))
            device_count = u32()
            _call(devices, _COLLECTION_GET_COUNT, ctypes.byref(device_count), argtypes=(ctypes.POINTER(u32),))
            for i in range(device_count.value):
                with _com_ptr() as device, _com_ptr() as manager, _com_ptr() as sessions:
                    _call(devices, _COLLECTION_ITEM, i, ctypes.byref(device), argtypes=(u32, pp))
                    _call(device, _DEVICE_ACTIVATE, ctypes.byref(_GUID.parse(_IID_IAUDIO_SESSION_MANAGER2)),
                          _CLSCTX_ALL, None, ctypes.byref(manager), argtypes=(vp, wintypes.DWORD, vp, pp))
                    _call(manager, _GET_SESSION_ENUMERATOR, ctypes.byref(sessions), argtypes=(pp,))
                    session_count = i32()
                    _call(sessions, _ENUM_GET_COUNT, ctypes.byref(session_count), argtypes=(ctypes.POINTER(i32),))
                    for j in range(session_count.value):
                        pid = _session_pid(sessions, j, active_only)
                        if pid:
                            pids.add(pid)
    return pids


def _session_pid(sessions: ctypes.c_void_p, index: int, active_only: bool) -> int:
    """回傳第 ``index`` 個工作階段的 PID；``active_only`` 且未在使用中時回傳 0。"""
    pp = ctypes.POINTER(ctypes.c_void_p)
    with _com_ptr() as control, _com_ptr() as control2:
        _call(sessions, _ENUM_GET_SESSION, index, ctypes.byref(control), argtypes=(ctypes.c_int, pp))
        state = ctypes.c_int()
        _call(control, _SESSION_GET_STATE, ctypes.byref(state), argtypes=(ctypes.POINTER(ctypes.c_int),))
        # 程式關閉錄音後工作階段仍會留著（狀態變成非使用中），所以要看狀態
        if active_only and state.value != _SESSION_STATE_ACTIVE:
            return 0
        _call(control, _QUERY_INTERFACE, ctypes.byref(_GUID.parse(_IID_IAUDIO_SESSION_CONTROL2)),
              ctypes.byref(control2), argtypes=(ctypes.c_void_p, pp))
        pid = wintypes.DWORD()
        _call(control2, _SESSION_GET_PROCESS_ID, ctypes.byref(pid), argtypes=(ctypes.POINTER(wintypes.DWORD),))
        return int(pid.value)


def _process_name(pid: int) -> str:
    """以 PID 取得執行檔名稱（例如 ``Discord.exe``）；查不到時回傳空字串。"""
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return ""
        return buf.value.rsplit("\\", 1)[-1]
    finally:
        kernel32.CloseHandle(handle)


def discord_capturing() -> bool | None:
    """Discord 目前是否正在收音（= 在語音頻道中）。

    Returns:
        bool | None: 收音中為 True；不是 Windows 或無法查詢時為 None。
    """
    if sys.platform != "win32":
        return None
    try:
        pids = capturing_process_ids()
    except OSError as exc:
        logger.debug("無法查詢錄音工作階段：%s", exc)
        return None
    return any(_process_name(pid).lower().startswith(_DISCORD_EXE_PREFIX) for pid in pids)


class VoiceWatcher:
    """背景執行緒：每隔一段時間查一次 Discord 是否在語音中，結果交給回呼。

    COM 查詢偶爾可能卡住（例如音訊服務忙碌），放在背景才不會讓介面停頓。
    回呼在背景執行緒中執行，不可直接操作 Tk。
    """

    def __init__(self, on_result: Callable[[bool | None], None], interval: float = 1.0) -> None:
        """
        Args:
            on_result (Callable[[bool | None], None]): 每次查詢後呼叫，參數同 :func:`discord_capturing`。
            interval (float): 查詢間隔秒數。
        """
        self._on_result = on_result
        self._interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """開始監看（重複呼叫無作用）。"""
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="discord-voice", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """停止監看。"""
        self._stop.set()
        self._thread = None

    def _run(self) -> None:
        initialized = False
        if sys.platform == "win32":
            try:
                ctypes.oledll.ole32.CoInitializeEx(None, _COINIT_MULTITHREADED)
                initialized = True
            except OSError as exc:
                logger.warning("無法初始化 COM，停用 Discord 語音偵測：%s", exc)
        try:
            while not self._stop.is_set():
                self._on_result(discord_capturing() if initialized else None)
                self._stop.wait(self._interval)
        finally:
            if initialized:
                ctypes.windll.ole32.CoUninitialize()


@dataclass
class VoiceJoinDetector:
    """把「是否在語音中」的輪詢結果轉成「剛進入語音」事件。

    Attributes:
        cooldown (float): 觸發後至少隔幾秒才會再觸發，避免 Discord 重新連線時連播。
        in_voice (bool | None): 最近一次的狀態；None 表示還沒讀到或無法偵測。
    """

    # 只擋「同一瞬間的閃斷重連」。原本 10 秒太長：直接切換語音頻道時，Discord 會先離開約 1 秒再進入，
    # 使用者進一個房間後幾秒內又切到朋友的房間，第二次進場就被擋掉了
    cooldown: float = 3.0
    in_voice: bool | None = None
    _last_trigger: float = field(default=float("-inf"), repr=False)

    def update(self, in_use: bool | None, now: float) -> bool:
        """餵入一次輪詢結果。

        Args:
            in_use (bool | None): :func:`discord_capturing` 的結果。
            now (float): 目前時間（秒，例如 ``time.monotonic()``）。

        Returns:
            bool: 這次是否剛進入語音（應該播放進場音效）。
        """
        if in_use is None:
            return False
        previous = self.in_voice
        self.in_voice = in_use
        # 第一次讀取只記錄狀態：開音效版時本來就在語音中，不應該突然播放
        if previous is None or previous or not in_use:
            return False
        if now - self._last_trigger < self.cooldown:
            return False
        self._last_trigger = now
        return True
