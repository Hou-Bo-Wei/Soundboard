"""只錄「某個程式」發出的聲音（Windows 程式音訊迴送，Process Loopback）。

Windows 10 2004（組建 19041）起，WASAPI 可以用 ``VAD\\Process_Loopback`` 虛擬裝置，
只擷取指定程式（含其子程式）送到喇叭的聲音，例如只錄 Discord 裡朋友講話、不錄 YouTube。
擷取的是送往喇叭「之前」的數位訊號，不受喇叭音量影響，也不會發出任何聲音。

與 :mod:`soundboard.discord_voice` 相同，COM 呼叫刻意用 ctypes 直接呼叫 vtable，
所有介面都在同一條背景執行緒內明確 Release，避免垃圾回收在其他執行緒釋放 COM 物件而閃退。
"""

from __future__ import annotations

import ctypes
import logging
import os
import sys
import threading
from collections.abc import Callable
from ctypes import wintypes
from dataclasses import dataclass

import numpy as np

from soundboard.discord_voice import _GUID, _call, _com_ptr, _process_name, _release
from soundboard.i18n import tr

logger = logging.getLogger(__name__)

SAMPLE_RATE = 48000
CHANNELS = 2

_VIRTUAL_DEVICE = "VAD\\Process_Loopback"
_IID_IAUDIO_CLIENT = "{1CB9AD4C-DBFA-4C32-B178-C2F568A703B2}"
_IID_IAUDIO_CAPTURE_CLIENT = "{C8ADBD64-E71E-48A0-A4DE-185C395CD317}"
_IID_IUNKNOWN = "{00000000-0000-0000-C000-000000000046}"
_IID_COMPLETION_HANDLER = "{41D949AB-9862-444A-80F6-C261334DA5EB}"
# 回呼物件必須是「agile」，否則系統在其他執行緒呼叫它時會失敗
_IID_IAGILE_OBJECT = "{94EA2B94-E9CC-49E0-C0FF-EE64CA8F5B90}"

_VT_BLOB = 65
_ACTIVATION_TYPE_PROCESS_LOOPBACK = 1
_LOOPBACK_INCLUDE_TREE = 0          # 連同子程式一起錄（Discord、瀏覽器的聲音都在子程式）
_SHAREMODE_SHARED = 0
_STREAMFLAGS_LOOPBACK = 0x00020000
_STREAMFLAGS_EVENTCALLBACK = 0x00040000
_STREAMFLAGS_AUTOCONVERTPCM = 0x80000000
_STREAMFLAGS_SRC_DEFAULT_QUALITY = 0x08000000
_BUFFER_FLAG_SILENT = 0x2
_WAVE_FORMAT_PCM = 1
_BUFFER_HNS = 200 * 10_000          # 200 ms 系統緩衝（100 ns 為單位）
_WAIT_MS = 200
_COINIT_MULTITHREADED = 0x0
_E_NOINTERFACE = 0x80004002 - (1 << 32)
_S_OK = 0

# vtable 索引
_ASYNC_GET_RESULT = 3               # IActivateAudioInterfaceAsyncOperation
_CLIENT_INITIALIZE, _CLIENT_START, _CLIENT_STOP = 3, 10, 11
_CLIENT_SET_EVENT, _CLIENT_GET_SERVICE = 13, 14
_CAPTURE_GET_BUFFER, _CAPTURE_RELEASE_BUFFER, _CAPTURE_NEXT_PACKET = 3, 4, 5

MIN_WINDOWS_BUILD = 19041


class CaptureError(Exception):
    """無法開始擷取程式聲音。

    訊息會顯示在混音器與回放錄音頁的狀態文字，所以建立時就用 :func:`tr` 翻譯。
    """


class _LoopbackParams(ctypes.Structure):
    # AUDIOCLIENT_ACTIVATION_PARAMS（union 只會用到 ProcessLoopbackParams）
    _fields_ = [("activation_type", ctypes.c_int), ("target_pid", wintypes.DWORD), ("mode", ctypes.c_int)]


class _PropVariantBlob(ctypes.Structure):
    # PROPVARIANT（VT_BLOB）；x64 上指標自動對齊到 8，總長 24 位元組與 Windows 相同
    _fields_ = [("vt", ctypes.c_ushort), ("r1", ctypes.c_ushort), ("r2", ctypes.c_ushort),
                ("r3", ctypes.c_ushort), ("size", wintypes.ULONG), ("data", ctypes.c_void_p)]


class _WaveFormatEx(ctypes.Structure):
    _fields_ = [("format_tag", wintypes.WORD), ("channels", wintypes.WORD),
                ("samples_per_sec", wintypes.DWORD), ("avg_bytes_per_sec", wintypes.DWORD),
                ("block_align", wintypes.WORD), ("bits_per_sample", wintypes.WORD), ("cb_size", wintypes.WORD)]


class _CompletionHandler:
    """以 ctypes 實作的 IActivateAudioInterfaceCompletionHandler，完成時設定 Win32 事件。

    生命週期由 Python 物件掌握（AddRef / Release 固定回傳 1），
    呼叫端要確保啟用完成前不讓它被回收。
    """

    _QI = ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))
    _REF = ctypes.WINFUNCTYPE(wintypes.ULONG, ctypes.c_void_p)
    _DONE = ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, ctypes.c_void_p)

    def __init__(self, event: int) -> None:
        self._event = event
        self._accepted = [bytes(_GUID.parse(i)) for i in (_IID_IUNKNOWN, _IID_COMPLETION_HANDLER, _IID_IAGILE_OBJECT)]
        self._funcs = (self._QI(self._query), self._REF(lambda _this: 1), self._REF(lambda _this: 1),
                       self._DONE(self._completed))
        self._vtable = (ctypes.c_void_p * 4)(*[ctypes.cast(f, ctypes.c_void_p) for f in self._funcs])
        self._obj = ctypes.c_void_p(ctypes.addressof(self._vtable))  # 物件 = 指向 vtable 的指標

    @property
    def pointer(self) -> ctypes.c_void_p:
        """傳給 COM 的介面指標。"""
        return ctypes.c_void_p(ctypes.addressof(self._obj))

    def _query(self, this: int, riid: int, out: ctypes.POINTER(ctypes.c_void_p)) -> int:
        if ctypes.string_at(riid, 16) in self._accepted:
            out[0] = this
            return _S_OK
        out[0] = None
        return _E_NOINTERFACE

    def _completed(self, _this: int, _operation: int) -> int:
        ctypes.windll.kernel32.SetEvent(self._event)
        return _S_OK


def supported() -> bool:
    """這台電腦是否支援只錄特定程式的聲音。

    Returns:
        bool: Windows 10 2004（組建 19041）以上為 True。
    """
    if sys.platform != "win32":
        return False
    return sys.getwindowsversion().build >= MIN_WINDOWS_BUILD


def _activate_loopback(pid: int) -> ctypes.c_void_p:
    """取得只擷取 ``pid`` 程式樹聲音的 IAudioClient（呼叫端負責 Release）。"""
    kernel32 = ctypes.windll.kernel32
    event = kernel32.CreateEventW(None, True, False, None)
    params = _LoopbackParams(_ACTIVATION_TYPE_PROCESS_LOOPBACK, pid, _LOOPBACK_INCLUDE_TREE)
    prop = _PropVariantBlob(vt=_VT_BLOB, size=ctypes.sizeof(params),
                            data=ctypes.cast(ctypes.pointer(params), ctypes.c_void_p))
    handler = _CompletionHandler(event)
    client = ctypes.c_void_p()
    try:
        with _com_ptr() as operation:
            ctypes.oledll.mmdevapi.ActivateAudioInterfaceAsync(
                ctypes.c_wchar_p(_VIRTUAL_DEVICE), ctypes.byref(_GUID.parse(_IID_IAUDIO_CLIENT)),
                ctypes.byref(prop), handler.pointer, ctypes.byref(operation))
            if kernel32.WaitForSingleObject(event, 5000) != 0:
                raise CaptureError(tr("等待音訊介面啟用逾時"))
            result = ctypes.c_long()
            unknown = ctypes.c_void_p()
            # HRESULT 回傳型別會在失敗時自動拋 OSError；這裡改用 c_long 以取得 activateResult
            vtable = ctypes.cast(operation, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
            ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(ctypes.c_long),
                               ctypes.POINTER(ctypes.c_void_p))(vtable[_ASYNC_GET_RESULT])(
                operation, ctypes.byref(result), ctypes.byref(unknown))
            if result.value != 0 or not unknown:
                _release(unknown)
                raise CaptureError(tr("無法啟用程式音訊擷取（{code}）", code=f"0x{result.value & 0xFFFFFFFF:08X}"))
            client = unknown  # 要求的就是 IAudioClient，不必再 QueryInterface
    finally:
        kernel32.CloseHandle(event)
        del handler
    return client


@dataclass
class CaptureStats:
    """擷取統計（測試與除錯用）。

    Attributes:
        frames (int): 收到的音框總數。
        packets (int): 收到的封包數。
        silent_packets (int): 標記為靜音的封包數。
    """

    frames: int = 0
    packets: int = 0
    silent_packets: int = 0


class ProcessCapture:
    """在背景執行緒持續擷取一個程式（含子程式）的聲音。

    每收到一段音訊就呼叫 ``on_audio(pcm)``，``pcm`` 為 (frames, 2) 的 int16 陣列。
    回呼在擷取執行緒中執行，必須很快完成（例如只寫進環形緩衝區）。

    Args:
        pid (int): 目標程式 PID（會連同子程式一起擷取）。
        on_audio (Callable[[np.ndarray], None]): 收到音訊時的回呼。
        on_stopped (Callable[[str | None], None] | None): 擷取結束時呼叫，參數為錯誤訊息（正常停止為 None）。
    """

    def __init__(self, pid: int, on_audio: Callable[[np.ndarray], None],
                 on_stopped: Callable[[str | None], None] | None = None) -> None:
        self.pid = pid
        self.stats = CaptureStats()
        self._on_audio = on_audio
        self._on_stopped = on_stopped
        self._stop = threading.Event()
        self._started = threading.Event()
        self._error: str | None = None
        self._thread = threading.Thread(target=self._run, name=f"ProcessCapture-{pid}", daemon=True)

    def start(self, timeout: float = 5.0) -> None:
        """開始擷取，等到串流真正啟動或失敗才回傳。

        Raises:
            CaptureError: 系統不支援或啟用失敗時拋出。
        """
        if not supported():
            raise CaptureError(tr("僅錄製特定程式的聲音需要 Windows 10 2004 或以上版本"))
        self._thread.start()
        if not self._started.wait(timeout):
            raise CaptureError(tr("啟動程式音訊擷取逾時"))
        if self._error:
            raise CaptureError(self._error)

    def stop(self) -> None:
        """停止擷取並等待背景執行緒結束。"""
        self._stop.set()
        if self._thread.is_alive() and threading.current_thread() is not self._thread:
            self._thread.join(timeout=2)

    @property
    def running(self) -> bool:
        """擷取執行緒是否仍在執行。"""
        return self._thread.is_alive() and not self._stop.is_set()

    def _run(self) -> None:
        ole32 = ctypes.oledll.ole32
        try:
            ole32.CoInitializeEx(None, _COINIT_MULTITHREADED)
        except OSError as exc:
            self._error = tr("無法初始化 COM：{error}", error=exc)
            self._started.set()
            return
        error: str | None = None
        try:
            self._capture_loop()
        except (OSError, CaptureError) as exc:
            error = str(exc)
            logger.warning("程式聲音擷取（PID %d）結束：%s", self.pid, exc)
        except Exception as exc:  # noqa: BLE001 - 背景執行緒的例外不能直接丟掉，要回報給介面
            error = f"{type(exc).__name__}: {exc}"
            logger.exception("程式聲音擷取（PID %d）發生未預期錯誤", self.pid)
        finally:
            if not self._started.is_set():
                self._error = error or tr("無法啟動擷取")
                self._started.set()
            ctypes.windll.ole32.CoUninitialize()
            if self._on_stopped is not None:
                self._on_stopped(error)

    def _capture_loop(self) -> None:
        kernel32 = ctypes.windll.kernel32
        pp = ctypes.POINTER(ctypes.c_void_p)
        u32 = ctypes.c_uint32
        client = _activate_loopback(self.pid)
        event = kernel32.CreateEventW(None, False, False, None)
        try:
            with _com_ptr() as capture:
                block = CHANNELS * 2
                fmt = _WaveFormatEx(_WAVE_FORMAT_PCM, CHANNELS, SAMPLE_RATE, SAMPLE_RATE * block, block, 16, 0)
                flags = (_STREAMFLAGS_LOOPBACK | _STREAMFLAGS_EVENTCALLBACK
                         | _STREAMFLAGS_AUTOCONVERTPCM | _STREAMFLAGS_SRC_DEFAULT_QUALITY)
                _call(client, _CLIENT_INITIALIZE, _SHAREMODE_SHARED, flags, ctypes.c_longlong(_BUFFER_HNS),
                      ctypes.c_longlong(0), ctypes.byref(fmt), None,
                      argtypes=(ctypes.c_int, wintypes.DWORD, ctypes.c_longlong, ctypes.c_longlong,
                                ctypes.c_void_p, ctypes.c_void_p))
                _call(client, _CLIENT_SET_EVENT, event, argtypes=(wintypes.HANDLE,))
                _call(client, _CLIENT_GET_SERVICE, ctypes.byref(_GUID.parse(_IID_IAUDIO_CAPTURE_CLIENT)),
                      ctypes.byref(capture), argtypes=(ctypes.c_void_p, pp))
                _call(client, _CLIENT_START)
                self._started.set()
                try:
                    data = ctypes.c_void_p()
                    frames, flags_out, packet = u32(), wintypes.DWORD(), u32()
                    while not self._stop.is_set():
                        kernel32.WaitForSingleObject(event, _WAIT_MS)
                        while True:
                            _call(capture, _CAPTURE_NEXT_PACKET, ctypes.byref(packet), argtypes=(ctypes.POINTER(u32),))
                            if packet.value == 0:
                                break
                            _call(capture, _CAPTURE_GET_BUFFER, ctypes.byref(data), ctypes.byref(frames),
                                  ctypes.byref(flags_out), None, None,
                                  argtypes=(pp, ctypes.POINTER(u32), ctypes.POINTER(wintypes.DWORD),
                                            ctypes.c_void_p, ctypes.c_void_p))
                            n = frames.value
                            if flags_out.value & _BUFFER_FLAG_SILENT or not data:
                                pcm = np.zeros((n, CHANNELS), dtype=np.int16)
                                self.stats.silent_packets += 1
                            else:
                                pcm = np.frombuffer(ctypes.string_at(data, n * block), dtype=np.int16).reshape(n, CHANNELS)
                            _call(capture, _CAPTURE_RELEASE_BUFFER, n, argtypes=(u32,))
                            self.stats.frames += n
                            self.stats.packets += 1
                            self._on_audio(pcm)
                finally:
                    try:
                        _call(client, _CLIENT_STOP)
                    except OSError:
                        pass
        finally:
            kernel32.CloseHandle(event)
            _release(client)


# ---------------------------------------------------------------- 找出要錄的程式
_TH32CS_SNAPPROCESS = 0x2


class _ProcessEntry(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("usage", wintypes.DWORD), ("pid", wintypes.DWORD),
                ("heap", ctypes.c_void_p), ("module", wintypes.DWORD), ("threads", wintypes.DWORD),
                ("parent", wintypes.DWORD), ("priority", ctypes.c_long), ("flags", wintypes.DWORD),
                ("exe", ctypes.c_wchar * 260)]


def list_processes() -> dict[int, tuple[str, int]]:
    """列出所有程式。

    Returns:
        dict[int, tuple[str, int]]: PID → (執行檔名稱, 父程式 PID)。
    """
    if sys.platform != "win32":
        return {}
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    snap = kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    result: dict[int, tuple[str, int]] = {}
    if not snap or snap == wintypes.HANDLE(-1).value:
        return result
    try:
        entry = _ProcessEntry()
        entry.size = ctypes.sizeof(entry)
        ok = kernel32.Process32FirstW(wintypes.HANDLE(snap), ctypes.byref(entry))
        while ok:
            result[int(entry.pid)] = (entry.exe, int(entry.parent))
            ok = kernel32.Process32NextW(wintypes.HANDLE(snap), ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(wintypes.HANDLE(snap))
    return result


def root_pids(exe_name: str, processes: dict[int, tuple[str, int]] | None = None) -> list[int]:
    """找出某個執行檔的「最上層」程式 PID（父程式不是同名程式）。

    Discord、Chrome 等程式會開很多同名子程式，聲音常在子程式裡；
    只要從最上層開始擷取並包含子程式，就能錄到全部。

    Args:
        exe_name (str): 執行檔名稱，例如 ``Discord.exe``（不分大小寫）。
        processes (dict | None): :func:`list_processes` 的結果；None 時自動取得。

    Returns:
        list[int]: PID 清單（通常只有一個）。
    """
    procs = list_processes() if processes is None else processes
    target = exe_name.lower()
    roots = []
    for pid, (name, parent) in procs.items():
        if name.lower() != target:
            continue
        parent_name = procs.get(parent, ("", 0))[0].lower()
        if parent_name != target:
            roots.append(pid)
    return sorted(roots)


def list_audio_apps() -> list[str]:
    """列出在播放裝置上有音訊工作階段的程式（可以被錄的程式）。

    在獨立的背景執行緒初始化 COM 後查詢，呼叫端不必處理 COM；最多等 5 秒。

    Returns:
        list[str]: 執行檔名稱（不分大小寫去重、依字母排序，不含音效版自己）。
    """
    if sys.platform != "win32":
        return []
    from soundboard.discord_voice import session_process_ids  # 避免循環匯入

    found: list[int] = []

    def worker() -> None:
        try:
            ctypes.oledll.ole32.CoInitializeEx(None, _COINIT_MULTITHREADED)
        except OSError:
            return
        try:
            found.extend(session_process_ids(render=True, active_only=False))
        except OSError as exc:
            logger.debug("無法列出播放中的程式：%s", exc)
        finally:
            ctypes.windll.ole32.CoUninitialize()

    thread = threading.Thread(target=worker, name="ListAudioApps", daemon=True)
    thread.start()
    thread.join(timeout=5)
    own = _process_name(os.getpid()).lower()
    names: dict[str, str] = {}
    for pid in found:
        if pid == 0:   # PID 0 是系統音效
            continue
        name = _process_name(pid)
        if name and name.lower() != own:
            names.setdefault(name.lower(), name)
    return sorted(names.values(), key=str.casefold)


__all__ = [
    "CaptureError", "CaptureStats", "ProcessCapture", "list_processes", "root_pids", "supported",
    "SAMPLE_RATE", "CHANNELS", "list_audio_apps",
]
