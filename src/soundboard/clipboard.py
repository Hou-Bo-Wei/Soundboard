"""把檔案放進 Windows 剪貼簿（等同在檔案總管選取檔案後按 Ctrl+C），之後在 Discord 按 Ctrl+V 就能以自己的身分上傳。"""

from __future__ import annotations

import ctypes
import struct
import sys
import time
from ctypes import wintypes
from pathlib import Path

from soundboard.i18n import tr

CF_HDROP = 15
GMEM_MOVEABLE = 0x0002
GMEM_ZEROINIT = 0x0040
DROPEFFECT_COPY = 1
_OPEN_RETRIES = 10      # 剪貼簿可能被別的程式暫時佔用


class ClipboardError(Exception):
    """無法寫入剪貼簿（訊息已翻譯）。"""


def build_dropfiles(paths: list[Path]) -> bytes:
    """組出 CF_HDROP 剪貼簿格式的內容：``DROPFILES`` 標頭加上以 NUL 分隔、雙 NUL 結尾的寬字元路徑。

    Args:
        paths (list[Path]): 檔案路徑（會轉成絕對路徑）。

    Returns:
        bytes: 剪貼簿資料。
    """
    listing = "".join(f"{Path(p).resolve()}\0" for p in paths) + "\0"
    # DROPFILES：pFiles(DWORD 偏移) pt(POINT 兩個 LONG) fNC(BOOL) fWide(BOOL)
    header = struct.pack("<IiiII", 20, 0, 0, 0, 1)
    return header + listing.encode("utf-16-le")


def _alloc(data: bytes) -> int:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    handle = kernel32.GlobalAlloc(GMEM_MOVEABLE | GMEM_ZEROINIT, len(data))
    if not handle:
        raise ClipboardError(tr("記憶體不足，無法複製到剪貼簿"))
    pointer = kernel32.GlobalLock(handle)
    ctypes.memmove(pointer, data, len(data))
    kernel32.GlobalUnlock(handle)
    return handle


def copy_files(paths: list[Path]) -> None:
    """把檔案複製到剪貼簿。

    Args:
        paths (list[Path]): 要複製的檔案（必須存在）。

    Raises:
        ClipboardError: 不是 Windows、檔案不存在或剪貼簿無法使用時拋出。
    """
    if sys.platform != "win32":
        raise ClipboardError(tr("僅 Windows 支援複製檔案到剪貼簿"))
    missing = [p for p in paths if not Path(p).is_file()]
    if missing:
        raise ClipboardError(tr("找不到檔案：{name}", name=Path(missing[0]).name))
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user32.SetClipboardData.restype = wintypes.HANDLE
    user32.RegisterClipboardFormatW.argtypes = [wintypes.LPCWSTR]
    for _ in range(_OPEN_RETRIES):
        if user32.OpenClipboard(None):
            break
        time.sleep(0.05)
    else:
        raise ClipboardError(tr("剪貼簿正被其他程式使用，請稍後再試"))
    try:
        user32.EmptyClipboard()
        drop = _alloc(build_dropfiles(paths))
        if not user32.SetClipboardData(CF_HDROP, drop):
            raise ClipboardError(tr("無法寫入剪貼簿"))
        # 告訴貼上的程式這是「複製」而不是「剪下」，否則有些程式會把來源檔案搬走
        effect = user32.RegisterClipboardFormatW("Preferred DropEffect")
        user32.SetClipboardData(effect, _alloc(struct.pack("<I", DROPEFFECT_COPY)))
    finally:
        user32.CloseClipboard()
