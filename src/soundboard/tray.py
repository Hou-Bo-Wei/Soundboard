"""系統匣（通知區域）圖示與單一執行個體。

Tkinter 沒有系統匣功能，這裡直接用 Win32 API（ctypes）實作，不必多帶一個套件進 exe。
圖示有自己的執行緒與訊息迴圈；點選、選單動作透過 callback 回報，
callback 在系統匣執行緒裡被呼叫，呼叫端要自己把工作交回 Tk 主執行緒。

單一執行個體：開機已在背景執行時，使用者再雙擊 exe，不應該開出第二個音效版
（兩個程式會同時搶快捷鍵、重複播放），而是叫出原本那個的視窗。
"""

from __future__ import annotations

import hashlib
import logging
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from soundboard.i18n import N_, tr

logger = logging.getLogger(__name__)

# 只標記：模組載入時還沒決定介面語言，加入圖示時才 tr
TOOLTIP = N_("音效版 Soundboard")
WINDOW_TITLE_PREFIX = "SoundboardTray-"
READY_TIMEOUT = 5.0

# Win32 常數
_WM_APP = 0x8000
WM_TRAY = _WM_APP + 1           # 圖示上的滑鼠事件
WM_SHOW_REQUEST = _WM_APP + 2   # 第二個執行個體請原本的顯示視窗
_WM_NULL = 0x0000
_WM_DESTROY = 0x0002
_WM_CLOSE = 0x0010
_WM_CONTEXTMENU = 0x007B
_WM_LBUTTONUP = 0x0202
_WM_LBUTTONDBLCLK = 0x0203
_WM_RBUTTONUP = 0x0205
_NIM_ADD, _NIM_MODIFY, _NIM_DELETE = 0, 1, 2
_NIF_MESSAGE, _NIF_ICON, _NIF_TIP, _NIF_INFO = 0x01, 0x02, 0x04, 0x10
_NIIF_INFO = 0x01
_MF_STRING, _MF_SEPARATOR, _MF_CHECKED = 0x0000, 0x0800, 0x0008
_TPM_RIGHTBUTTON, _TPM_RETURNCMD, _TPM_NONOTIFY = 0x0002, 0x0100, 0x0080
_IMAGE_ICON, _LR_LOADFROMFILE = 1, 0x0010
_SM_CXSMICON, _SM_CYSMICON = 49, 50
_IDI_APPLICATION = 32512
_ERROR_ALREADY_EXISTS = 183
_ERROR_CLASS_ALREADY_EXISTS = 1410


@dataclass(frozen=True)
class TrayMenuItem:
    """系統匣右鍵選單的一個項目。

    Attributes:
        text (str): 顯示文字；空字串表示分隔線。
        action (Callable[[], None] | None): 點選後呼叫（在系統匣執行緒裡）。
        checked (bool): 是否顯示勾勾。
        default (bool): 是否為預設項目（粗體，等同雙擊圖示）。
    """

    text: str = ""
    action: Callable[[], None] | None = None
    checked: bool = False
    default: bool = False

    @property
    def separator(self) -> bool:
        """是否為分隔線。"""
        return not self.text


SEPARATOR = TrayMenuItem()


def instance_id(settings_file: Path) -> str:
    """依設定檔位置產生執行個體代號。

    用設定檔而不是固定名稱：測試或可攜版用另一份設定檔時，可以和平常的音效版同時執行、互不干擾。

    Args:
        settings_file (Path): 設定檔路徑。

    Returns:
        str: 12 個字元的代號。
    """
    key = str(settings_file.expanduser().resolve()).lower()
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]


def window_title(ident: str) -> str:
    """系統匣隱藏視窗的標題（第二個執行個體用它找到原本的程式）。"""
    return WINDOW_TITLE_PREFIX + ident


def _win32():  # noqa: ANN202 - 回傳 ctypes 函式庫的命名空間
    """載入並設定好 Win32 函式的參數型別（64 位元下沒設定 argtypes 會截斷指標）。"""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    lresult = ctypes.c_ssize_t
    wndproc = ctypes.WINFUNCTYPE(lresult, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

    class WNDCLASSEXW(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.UINT), ("style", wintypes.UINT), ("lpfnWndProc", wndproc),
                    ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                    ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON), ("hCursor", wintypes.HANDLE),
                    ("hbrBackground", wintypes.HBRUSH), ("lpszMenuName", wintypes.LPCWSTR),
                    ("lpszClassName", wintypes.LPCWSTR), ("hIconSm", wintypes.HICON)]

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                    ("Data4", ctypes.c_ubyte * 8)]

    class NOTIFYICONDATAW(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND), ("uID", wintypes.UINT),
                    ("uFlags", wintypes.UINT), ("uCallbackMessage", wintypes.UINT), ("hIcon", wintypes.HICON),
                    ("szTip", wintypes.WCHAR * 128), ("dwState", wintypes.DWORD),
                    ("dwStateMask", wintypes.DWORD), ("szInfo", wintypes.WCHAR * 256),
                    ("uVersion", wintypes.UINT), ("szInfoTitle", wintypes.WCHAR * 64),
                    ("dwInfoFlags", wintypes.DWORD), ("guidItem", GUID), ("hBalloonIcon", wintypes.HICON)]

    def sig(fn, res, *args):  # noqa: ANN001, ANN202
        fn.restype, fn.argtypes = res, list(args)
        return fn

    H, U, P = wintypes.HANDLE, wintypes.UINT, ctypes.c_void_p
    sig(user32.DefWindowProcW, lresult, wintypes.HWND, U, wintypes.WPARAM, wintypes.LPARAM)
    sig(user32.RegisterClassExW, wintypes.ATOM, ctypes.POINTER(WNDCLASSEXW))
    sig(user32.UnregisterClassW, wintypes.BOOL, wintypes.LPCWSTR, wintypes.HINSTANCE)
    sig(user32.CreateWindowExW, wintypes.HWND, wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
        wintypes.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND, wintypes.HMENU,
        wintypes.HINSTANCE, P)
    sig(user32.DestroyWindow, wintypes.BOOL, wintypes.HWND)
    sig(user32.GetMessageW, wintypes.BOOL, ctypes.POINTER(wintypes.MSG), wintypes.HWND, U, U)
    sig(user32.TranslateMessage, wintypes.BOOL, ctypes.POINTER(wintypes.MSG))
    sig(user32.DispatchMessageW, lresult, ctypes.POINTER(wintypes.MSG))
    sig(user32.PostMessageW, wintypes.BOOL, wintypes.HWND, U, wintypes.WPARAM, wintypes.LPARAM)
    sig(user32.PostQuitMessage, None, ctypes.c_int)
    sig(user32.RegisterWindowMessageW, U, wintypes.LPCWSTR)
    sig(user32.LoadImageW, H, wintypes.HINSTANCE, wintypes.LPCWSTR, U, ctypes.c_int, ctypes.c_int, U)
    sig(user32.LoadIconW, wintypes.HICON, wintypes.HINSTANCE, P)
    sig(user32.DestroyIcon, wintypes.BOOL, wintypes.HICON)
    sig(user32.GetSystemMetrics, ctypes.c_int, ctypes.c_int)
    sig(user32.CreatePopupMenu, wintypes.HMENU)
    sig(user32.AppendMenuW, wintypes.BOOL, wintypes.HMENU, U, ctypes.c_size_t, wintypes.LPCWSTR)
    sig(user32.SetMenuDefaultItem, wintypes.BOOL, wintypes.HMENU, U, U)
    sig(user32.TrackPopupMenu, wintypes.BOOL, wintypes.HMENU, U, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HWND, P)
    sig(user32.DestroyMenu, wintypes.BOOL, wintypes.HMENU)
    sig(user32.GetCursorPos, wintypes.BOOL, ctypes.POINTER(wintypes.POINT))
    sig(user32.SetForegroundWindow, wintypes.BOOL, wintypes.HWND)
    sig(user32.FindWindowW, wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR)
    sig(user32.GetWindowThreadProcessId, wintypes.DWORD, wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
    sig(user32.AllowSetForegroundWindow, wintypes.BOOL, wintypes.DWORD)
    sig(shell32.Shell_NotifyIconW, wintypes.BOOL, wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW))
    sig(kernel32.GetModuleHandleW, wintypes.HMODULE, wintypes.LPCWSTR)
    sig(kernel32.CreateMutexW, H, P, wintypes.BOOL, wintypes.LPCWSTR)
    sig(kernel32.CloseHandle, wintypes.BOOL, H)

    class Api:
        pass

    api = Api()
    api.ctypes, api.wintypes = ctypes, wintypes
    api.user32, api.shell32, api.kernel32 = user32, shell32, kernel32
    api.WNDPROC, api.WNDCLASSEXW, api.NOTIFYICONDATAW = wndproc, WNDCLASSEXW, NOTIFYICONDATAW
    return api


class TrayIcon:
    """系統匣圖示。

    在自己的執行緒建立一個看不見的視窗接收圖示事件；檔案總管重新啟動時（``TaskbarCreated``）會自動把圖示加回去。
    """

    def __init__(self, ident: str, icon_path: Path | None, on_activate: Callable[[], None],
                 menu: Callable[[], list[TrayMenuItem]]) -> None:
        """
        Args:
            ident (str): 執行個體代號（:func:`instance_id`）。
            icon_path (Path | None): .ico 檔；None 或載入失敗時用 Windows 預設的程式圖示。
            on_activate (Callable[[], None]): 點一下或雙擊圖示時呼叫。
            menu (Callable[[], list[TrayMenuItem]]): 每次按右鍵時呼叫，回傳要顯示的選單（可反映目前狀態）。
        """
        self.ident = ident
        self.icon_path = icon_path
        self.on_activate = on_activate
        self.menu = menu
        self.hwnd: int | None = None
        self._api = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._hicon = None
        self._owns_icon = False
        self._proc = None   # 必須保留 callback 物件的參照，否則被回收後 Windows 呼叫它會當掉
        self._class_name = f"SoundboardTray{id(self):x}"
        self._taskbar_created = 0

    @property
    def running(self) -> bool:
        """圖示是否已顯示在系統匣。"""
        return self.hwnd is not None

    def start(self) -> bool:
        """建立圖示（等到建立完成或失敗才回傳）。

        Returns:
            bool: 成功時為 True；不是 Windows 或建立失敗時為 False。
        """
        if sys.platform != "win32":
            return False
        self._thread = threading.Thread(target=self._run, name="tray", daemon=True)
        self._thread.start()
        self._ready.wait(READY_TIMEOUT)
        return self.running

    def stop(self) -> None:
        """移除圖示並結束執行緒。"""
        hwnd = self.hwnd
        if hwnd is not None and self._api is not None:
            self._api.user32.PostMessageW(hwnd, _WM_CLOSE, 0, 0)
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=2.0)

    def notify(self, title: str, message: str) -> None:
        """在圖示旁跳出通知（Windows 10/11 會顯示成右下角的通知）。

        Args:
            title (str): 標題。
            message (str): 內容。
        """
        if not self.running:
            return
        data = self._data(_NIF_INFO)
        data.szInfoTitle = title[:63]
        data.szInfo = message[:255]
        data.dwInfoFlags = _NIIF_INFO
        self._api.shell32.Shell_NotifyIconW(_NIM_MODIFY, self._api.ctypes.byref(data))

    # ------------------------------------------------------------------ 系統匣執行緒
    def _run(self) -> None:
        try:
            api = self._api = _win32()
            self._create_window(api)
            if not self._add_icon():
                raise OSError(f"Shell_NotifyIcon 失敗（錯誤 {api.ctypes.get_last_error()}）")
        except Exception:  # noqa: BLE001 - 沒有系統匣圖示時程式仍可正常使用，只是關閉視窗會直接結束
            logger.exception("無法建立系統匣圖示")
            self._cleanup()
            self._ready.set()
            return
        self._ready.set()
        msg = api.wintypes.MSG()
        while api.user32.GetMessageW(api.ctypes.byref(msg), None, 0, 0) > 0:
            api.user32.TranslateMessage(api.ctypes.byref(msg))
            api.user32.DispatchMessageW(api.ctypes.byref(msg))
        self._cleanup()

    def _create_window(self, api) -> None:  # noqa: ANN001
        hinst = api.kernel32.GetModuleHandleW(None)
        self._proc = api.WNDPROC(self._wndproc)
        wc = api.WNDCLASSEXW()
        wc.cbSize = api.ctypes.sizeof(api.WNDCLASSEXW)
        wc.lpfnWndProc = self._proc
        wc.hInstance = hinst
        wc.lpszClassName = self._class_name
        if not api.user32.RegisterClassExW(api.ctypes.byref(wc)):
            err = api.ctypes.get_last_error()
            if err != _ERROR_CLASS_ALREADY_EXISTS:
                raise OSError(f"RegisterClassExW 失敗（錯誤 {err}）")
        # 一般的隱藏頂層視窗（不是 message-only 視窗）：只有頂層視窗收得到 TaskbarCreated 廣播
        hwnd = api.user32.CreateWindowExW(0, self._class_name, window_title(self.ident), 0, 0, 0, 0, 0,
                                          None, None, hinst, None)
        if not hwnd:
            raise OSError(f"CreateWindowExW 失敗（錯誤 {api.ctypes.get_last_error()}）")
        self.hwnd = hwnd
        self._taskbar_created = api.user32.RegisterWindowMessageW("TaskbarCreated")
        self._hicon = self._load_icon(api)

    def _load_icon(self, api):  # noqa: ANN001, ANN202
        if self.icon_path is not None and self.icon_path.exists():
            cx = api.user32.GetSystemMetrics(_SM_CXSMICON)
            cy = api.user32.GetSystemMetrics(_SM_CYSMICON)
            hicon = api.user32.LoadImageW(None, str(self.icon_path), _IMAGE_ICON, cx, cy, _LR_LOADFROMFILE)
            if hicon:
                self._owns_icon = True
                return hicon
        return api.user32.LoadIconW(None, api.ctypes.c_void_p(_IDI_APPLICATION))

    def _data(self, flags: int):  # noqa: ANN202
        api = self._api
        data = api.NOTIFYICONDATAW()
        data.cbSize = api.ctypes.sizeof(api.NOTIFYICONDATAW)
        data.hWnd = self.hwnd
        data.uID = 1
        data.uFlags = flags
        return data

    def _add_icon(self) -> bool:
        data = self._data(_NIF_MESSAGE | _NIF_ICON | _NIF_TIP)
        data.uCallbackMessage = WM_TRAY
        data.hIcon = self._hicon
        data.szTip = tr(TOOLTIP)[:127]     # szTip 是 128 個字元的固定陣列（含結尾 NUL）
        return bool(self._api.shell32.Shell_NotifyIconW(_NIM_ADD, self._api.ctypes.byref(data)))

    def _wndproc(self, hwnd: int, msg: int, wparam: int, lparam: int) -> int:
        api = self._api
        try:
            if msg == WM_TRAY:
                event = lparam & 0xFFFF
                if event in (_WM_LBUTTONUP, _WM_LBUTTONDBLCLK):
                    self.on_activate()
                elif event in (_WM_RBUTTONUP, _WM_CONTEXTMENU):
                    self._show_menu()
                return 0
            if msg == WM_SHOW_REQUEST:
                self.on_activate()
                return 0
            if msg == self._taskbar_created and self._taskbar_created:
                self._add_icon()     # 檔案總管當掉重開後，系統匣是全新的，要重新加入
                return 0
            if msg == _WM_CLOSE:
                api.shell32.Shell_NotifyIconW(_NIM_DELETE, api.ctypes.byref(self._data(0)))
                api.user32.DestroyWindow(hwnd)
                return 0
            if msg == _WM_DESTROY:
                api.user32.PostQuitMessage(0)
                return 0
        except Exception:  # noqa: BLE001 - 例外不能穿過 Win32 callback，否則整個程式會當掉
            logger.exception("系統匣事件處理失敗")
            return 0
        return api.user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _show_menu(self) -> None:
        api = self._api
        items = self.menu()
        hmenu = api.user32.CreatePopupMenu()
        try:
            for i, item in enumerate(items, start=1):
                if item.separator:
                    api.user32.AppendMenuW(hmenu, _MF_SEPARATOR, 0, None)
                else:
                    flags = _MF_STRING | (_MF_CHECKED if item.checked else 0)
                    api.user32.AppendMenuW(hmenu, flags, i, item.text)
                    if item.default:
                        api.user32.SetMenuDefaultItem(hmenu, i, 0)
            pt = api.wintypes.POINT()
            api.user32.GetCursorPos(api.ctypes.byref(pt))
            # 不先把自己設成前景，點選單外面時選單不會自動關閉（Win32 已知行為）
            api.user32.SetForegroundWindow(self.hwnd)
            chosen = api.user32.TrackPopupMenu(hmenu, _TPM_RIGHTBUTTON | _TPM_RETURNCMD | _TPM_NONOTIFY,
                                               pt.x, pt.y, 0, self.hwnd, None)
            api.user32.PostMessageW(self.hwnd, _WM_NULL, 0, 0)
        finally:
            api.user32.DestroyMenu(hmenu)
        if 0 < chosen <= len(items) and items[chosen - 1].action is not None:
            items[chosen - 1].action()

    def _cleanup(self) -> None:
        api = self._api
        self.hwnd = None
        if api is None:
            return
        if self._hicon and self._owns_icon:
            api.user32.DestroyIcon(self._hicon)
        self._hicon = None
        api.user32.UnregisterClassW(self._class_name, api.kernel32.GetModuleHandleW(None))


class SingleInstance:
    """用具名 mutex 確保同一份設定檔只有一個音效版在執行。

    程式結束（包含當掉）時 Windows 會自動釋放 mutex，不會留下「以為還在執行」的殘留狀態。
    """

    def __init__(self, ident: str) -> None:
        """
        Args:
            ident (str): 執行個體代號（:func:`instance_id`）。
        """
        self.ident = ident
        self._handle = None

    def acquire(self) -> bool:
        """取得執行權。

        Returns:
            bool: 沒有其他執行個體時為 True；已經有一個在執行時為 False。非 Windows 一律 True。
        """
        if sys.platform != "win32":
            return True
        api = _win32()
        handle = api.kernel32.CreateMutexW(None, False, f"Local\\Soundboard-{self.ident}")
        if not handle:
            return True     # 建不出 mutex 時不擋使用者開程式
        if api.ctypes.get_last_error() == _ERROR_ALREADY_EXISTS:
            api.kernel32.CloseHandle(handle)
            return False
        self._handle = handle
        return True

    def release(self) -> None:
        """釋放執行權（一般不必呼叫，程式結束時會自動釋放）。"""
        if self._handle is not None:
            _win32().kernel32.CloseHandle(self._handle)
            self._handle = None


def show_running_instance(ident: str) -> bool:
    """請已經在執行的音效版把主視窗叫出來。

    Args:
        ident (str): 執行個體代號。

    Returns:
        bool: 找到並通知成功時為 True。
    """
    if sys.platform != "win32":
        return False
    api = _win32()
    hwnd = api.user32.FindWindowW(None, window_title(ident))
    if not hwnd:
        return False
    pid = api.wintypes.DWORD()
    api.user32.GetWindowThreadProcessId(hwnd, api.ctypes.byref(pid))
    # 只有目前的前景程式（使用者剛雙擊的這個）能把前景交給別人，原本那個才能把視窗跳到最前面
    api.user32.AllowSetForegroundWindow(pid.value)
    return bool(api.user32.PostMessageW(hwnd, WM_SHOW_REQUEST, 0, 0))
