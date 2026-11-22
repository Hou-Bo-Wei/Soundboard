"""音效檔解碼：統一轉成 48 kHz、立體聲、float32 的 numpy 陣列。"""

from __future__ import annotations

import io
import logging
import shutil
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

import numpy as np
import soundfile as sf

from soundboard.config import CHANNELS, FFMPEG_EXTENSIONS, SAMPLE_RATE, SOUNDFILE_EXTENSIONS
from soundboard.i18n import tr
from soundboard.ogg_repair import is_ogg_opus, repair_opus_granules

logger = logging.getLogger(__name__)


class DecodeError(Exception):
    """音效檔無法解碼。"""


@lru_cache(maxsize=1)
def find_ffmpeg() -> str | None:
    """尋找可用的 ffmpeg 執行檔。

    依序嘗試系統 PATH 與 ``imageio-ffmpeg`` 套件附帶的版本。

    Returns:
        str | None: ffmpeg 路徑，找不到時回傳 None。
    """
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg  # type: ignore[import-not-found]

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001 - 套件不存在或內建檔案損毀都視為沒有 ffmpeg
        return None


def supported_extensions() -> frozenset[str]:
    """目前環境可解碼的副檔名。

    Returns:
        frozenset[str]: 小寫、含點號的副檔名集合。
    """
    if find_ffmpeg():
        return SOUNDFILE_EXTENSIONS | FFMPEG_EXTENSIONS
    return SOUNDFILE_EXTENSIONS


def to_stereo(data: np.ndarray) -> np.ndarray:
    """把任意聲道數的音訊轉成立體聲。

    Args:
        data (np.ndarray): 形狀為 (frames, channels) 的音訊。

    Returns:
        np.ndarray: 形狀為 (frames, 2) 的音訊。
    """
    channels = data.shape[1]
    if channels == CHANNELS:
        return data
    if channels == 1:
        return np.repeat(data, CHANNELS, axis=1)
    # 多聲道（5.1 等）只取前兩聲道作為左右，簡單且不會改變音量
    return data[:, :CHANNELS]


def resample(data: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    """以線性內插重新取樣。

    音效多半很短，線性內插的音質已足夠，且不必額外依賴 scipy。

    Args:
        data (np.ndarray): 形狀為 (frames, channels) 的音訊。
        src_rate (int): 原始取樣率。
        dst_rate (int): 目標取樣率。

    Returns:
        np.ndarray: 重新取樣後的 float32 音訊。

    Raises:
        ValueError: 取樣率不為正數時拋出。
    """
    if src_rate <= 0 or dst_rate <= 0:
        raise ValueError("sample rate must be positive")
    if src_rate == dst_rate or len(data) == 0:
        return data.astype(np.float32, copy=False)
    n_out = int(round(len(data) * dst_rate / src_rate))
    src_t = np.arange(len(data), dtype=np.float64)
    dst_t = np.linspace(0, len(data) - 1, n_out, dtype=np.float64)
    out = np.empty((n_out, data.shape[1]), dtype=np.float32)
    for ch in range(data.shape[1]):
        out[:, ch] = np.interp(dst_t, src_t, data[:, ch])
    return out


def _decode_with_soundfile(path: Path) -> tuple[np.ndarray, int]:
    try:
        data, rate = sf.read(str(path), dtype="float32", always_2d=True)
    except (sf.LibsndfileError, RuntimeError):
        # 時間戳記有誤差的 Ogg Opus 會被 libsndfile 整個拒絕；修正後在記憶體中重試，
        # 這樣沒有安裝 ffmpeg 的電腦（例如朋友用 exe）也能播放
        raw = path.read_bytes()
        if not is_ogg_opus(raw):
            raise
        repaired, fixed = repair_opus_granules(raw)
        if not fixed:
            raise
        data, rate = sf.read(io.BytesIO(repaired), dtype="float32", always_2d=True)
        logger.info("已自動修正 %s 的 %d 個 Ogg 時間戳記", path.name, fixed)
    return data, int(rate)


def _decode_with_ffmpeg(path: Path) -> tuple[np.ndarray, int]:
    ffmpeg = find_ffmpeg()
    if ffmpeg is None:
        raise DecodeError(tr("播放 {ext} 檔案需要 ffmpeg，請安裝 ffmpeg 或 imageio-ffmpeg", ext=path.suffix))
    cmd = [
        ffmpeg, "-v", "error", "-nostdin", "-i", str(path),
        "-f", "f32le", "-ac", str(CHANNELS), "-ar", str(SAMPLE_RATE), "-",
    ]
    # Windows 上避免每次解碼都閃出一個黑色主控台視窗
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    try:
        proc = subprocess.run(cmd, capture_output=True, check=True, creationflags=flags, timeout=120)
    except subprocess.CalledProcessError as exc:
        msg = exc.stderr.decode("utf-8", "replace").strip()
        raise DecodeError(tr("ffmpeg 解碼失敗：{error}", error=msg)) from exc
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DecodeError(tr("無法執行 ffmpeg：{error}", error=exc)) from exc
    data = np.frombuffer(proc.stdout, dtype=np.float32).reshape(-1, CHANNELS)
    return data.copy(), SAMPLE_RATE


def decode_file(path: Path) -> np.ndarray:
    """解碼音效檔。

    先用 libsndfile（快、無外部程序），失敗再改用 ffmpeg，
    這樣常見格式不依賴 ffmpeg，少見格式也能播放。

    Args:
        path (Path): 音效檔路徑。

    Returns:
        np.ndarray: 形狀為 (frames, 2)、取樣率 48 kHz 的 float32 陣列。

    Raises:
        DecodeError: 所有解碼方式都失敗時拋出。
    """
    ext = path.suffix.lower()
    errors: list[str] = []
    data: np.ndarray | None = None
    rate = SAMPLE_RATE

    if ext not in FFMPEG_EXTENSIONS:
        try:
            data, rate = _decode_with_soundfile(path)
        except (sf.LibsndfileError, RuntimeError, OSError) as exc:
            errors.append(f"soundfile: {exc}")

    if data is None:
        try:
            data, rate = _decode_with_ffmpeg(path)
        except DecodeError as exc:
            errors.append(str(exc))

    if data is None:
        raise DecodeError(tr("無法解碼「{name}」：{error}", name=path.name, error="；".join(errors)))

    data = to_stereo(data)
    data = resample(data, rate, SAMPLE_RATE)
    return np.ascontiguousarray(data, dtype=np.float32)
