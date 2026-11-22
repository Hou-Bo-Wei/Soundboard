"""音效庫：掃描資料夾並快取解碼後的音訊。"""

from __future__ import annotations

import logging
import re
import sys
import threading
from collections import OrderedDict
from collections.abc import Callable, Iterable
from pathlib import Path

import numpy as np

from soundboard.config import CACHE_LIMIT_BYTES
from soundboard.decoder import DecodeError, decode_file, supported_extensions
from soundboard.i18n import tr
from soundboard.models import SoundItem

logger = logging.getLogger(__name__)

# Windows 檔名不允許的字元與保留名稱
_INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


class RenameError(Exception):
    """音效檔無法改名（名稱不合法、檔名重複、檔案被佔用等）。"""


def validate_file_stem(name: str) -> str | None:
    """檢查名稱能不能當作 Windows 檔名（不含副檔名）。

    Args:
        name (str): 使用者輸入的新名稱。

    Returns:
        str | None: 錯誤說明；合法時為 None。
    """
    if not name.strip():
        return tr("名稱不可空白")
    bad = sorted(set(_INVALID_CHARS.findall(name)))
    if bad:
        shown = " ".join(c if c.isprintable() else tr("控制字元") for c in bad)
        return tr("檔名不可包含下列字元：{chars}", chars=shown)
    if name != name.rstrip(" ."):
        return tr("檔名結尾不可為空白或句點")
    if name.split(".")[0].upper() in _RESERVED_NAMES:
        return tr("「{name}」為 Windows 保留名稱，無法作為檔名", name=name)
    if len(name) > 200:
        return tr("名稱過長（上限 {limit} 個字元）", limit=200)
    return None


def rename_sound_file(path: Path, new_stem: str) -> Path:
    """把音效檔改名（保留副檔名與所在資料夾）。

    Args:
        path (Path): 目前的音效檔路徑。
        new_stem (str): 新檔名（不含副檔名）。

    Returns:
        Path: 改名後的路徑；名稱沒變時回傳原路徑。

    Raises:
        RenameError: 名稱不合法、同名檔案已存在、或檔案無法改名時拋出。
    """
    error = validate_file_stem(new_stem)
    if error:
        raise RenameError(error)
    target = path.with_name(new_stem + path.suffix)
    if target.name == path.name:
        return path
    # Windows 檔名不分大小寫：只改大小寫時 target 會「存在」但其實就是同一個檔案
    if target.exists() and not (target.name.casefold() == path.name.casefold()):
        raise RenameError(tr("資料夾中已有名為「{name}」的檔案", name=target.name))
    try:
        path.rename(target)
    except PermissionError as exc:
        raise RenameError(tr("檔案正由其他程式使用中，或沒有修改此資料夾的權限")) from exc
    except OSError as exc:
        raise RenameError(tr("無法重新命名：{error}", error=exc.strerror or exc)) from exc
    return target


def scan_folder(
    folder: Path,
    display_names: dict[str, str],
    volumes: dict[str, float],
) -> list[SoundItem]:
    """遞迴掃描資料夾中的音效檔。

    Args:
        folder (Path): 音效資料夾。
        display_names (dict[str, str]): 自訂名稱（key → 名稱）。
        volumes (dict[str, float]): 個別音量（key → 倍率）。

    Returns:
        list[SoundItem]: 依顯示名稱排序的音效清單；資料夾不存在時為空清單。
    """
    if not folder.is_dir():
        return []
    exts = supported_extensions()
    items: list[SoundItem] = []
    for path in folder.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in exts:
            continue
        # 用相對路徑當 key：搬動整個資料夾後自訂名稱仍然有效
        key = path.relative_to(folder).as_posix()
        items.append(
            SoundItem(
                key=key,
                path=path,
                display_name=display_names.get(key, path.stem),
                volume=float(volumes.get(key, 1.0)),
            )
        )
    items.sort(key=lambda it: it.display_name.casefold())
    return items


class ClipCache:
    """執行緒安全的 LRU 音訊快取，總大小超過上限時淘汰最久沒用的音效。"""

    def __init__(self, limit_bytes: int = CACHE_LIMIT_BYTES) -> None:
        """
        Args:
            limit_bytes (int): 快取上限（bytes）。
        """
        self._limit = limit_bytes
        self._lock = threading.Lock()
        self._clips: OrderedDict[Path, np.ndarray] = OrderedDict()
        self._bytes = 0

    def peek(self, path: Path) -> np.ndarray | None:
        """取得已快取的音訊，不會觸發解碼。

        Args:
            path (Path): 音效檔路徑。

        Returns:
            np.ndarray | None: 音訊資料，尚未快取則為 None。
        """
        with self._lock:
            clip = self._clips.get(path)
            if clip is not None:
                self._clips.move_to_end(path)
            return clip

    def get(self, path: Path) -> np.ndarray:
        """取得音訊，必要時解碼並放入快取。

        Args:
            path (Path): 音效檔路徑。

        Returns:
            np.ndarray: (frames, 2) float32 音訊。

        Raises:
            DecodeError: 解碼失敗時拋出。
        """
        clip = self.peek(path)
        if clip is not None:
            return clip
        # 解碼可能耗時，故意在 lock 外執行，避免阻塞其他執行緒
        clip = decode_file(path)
        with self._lock:
            if path not in self._clips:
                self._clips[path] = clip
                self._bytes += clip.nbytes
                while self._bytes > self._limit and len(self._clips) > 1:
                    _, old = self._clips.popitem(last=False)
                    self._bytes -= old.nbytes
        return clip

    def rename(self, old: Path, new: Path) -> None:
        """檔案改名後沿用已解碼的音訊，不必重新解碼。

        Args:
            old (Path): 原本的路徑。
            new (Path): 新路徑。
        """
        with self._lock:
            clip = self._clips.pop(old, None)
            if clip is not None:
                self._clips[new] = clip

    def forget(self, path: Path) -> None:
        """移除某個音效的快取（檔案被刪除後）。

        Args:
            path (Path): 音效檔路徑。
        """
        with self._lock:
            clip = self._clips.pop(path, None)
            if clip is not None:
                self._bytes -= clip.nbytes

    def clear(self) -> None:
        """清空快取（例如重新掃描資料夾後檔案可能已變更）。"""
        with self._lock:
            self._clips.clear()
            self._bytes = 0

    def preload(
        self,
        items: Iterable[SoundItem],
        should_stop: Callable[[], bool],
        on_error: Callable[[SoundItem, str], None],
    ) -> None:
        """在背景預先解碼音效，讓第一次按下也能立即播放。

        Args:
            items (Iterable[SoundItem]): 要預載的音效。
            should_stop (Callable[[], bool]): 回傳 True 時中止（例如使用者切換了資料夾）。
            on_error (Callable[[SoundItem, str], None]): 解碼失敗時的回報函式。
        """
        for item in items:
            if should_stop():
                return
            try:
                self.get(item.path)
            except DecodeError as exc:
                on_error(item, str(exc))


def move_to_recycle_bin(path: Path) -> None:
    """把檔案移到資源回收筒（刪錯了還能從資源回收筒還原）。

    用 Windows 的 ``SHFileOperationW``（檔案總管按 Delete 的同一套機制）；不顯示任何對話框，
    確認由呼叫端自己負責。

    Args:
        path (Path): 要刪除的檔案。

    Raises:
        FileNotFoundError: 檔案不存在時拋出。
        OSError: 不是 Windows 或系統無法移到資源回收筒時拋出（例如網路磁碟沒有資源回收筒）。
    """
    path = Path(path).resolve()
    if not path.exists():
        raise FileNotFoundError(str(path))
    if sys.platform != "win32":
        raise OSError(tr("僅 Windows 支援移至資源回收筒"))
    import ctypes
    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT), ("pFrom", wintypes.LPCWSTR),
                    ("pTo", wintypes.LPCWSTR), ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", wintypes.BOOL),
                    ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR)]

    fo_delete = 0x0003
    # 允許復原（＝資源回收筒）、不再詢問、不顯示進度與錯誤視窗
    flags = 0x0040 | 0x0010 | 0x0004 | 0x0400
    op = SHFILEOPSTRUCTW(None, fo_delete, str(path) + "\0", None, flags, False, None, None)
    shell32 = ctypes.WinDLL("shell32")
    shell32.SHFileOperationW.argtypes = [ctypes.POINTER(SHFILEOPSTRUCTW)]
    shell32.SHFileOperationW.restype = ctypes.c_int
    result = shell32.SHFileOperationW(ctypes.byref(op))
    if result != 0 or op.fAnyOperationsAborted or path.exists():
        raise OSError(tr("無法移至資源回收筒（錯誤碼 {code}）", code=result))
