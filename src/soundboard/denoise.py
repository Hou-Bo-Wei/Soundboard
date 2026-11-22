"""麥克風降噪：RNNoise（開源 AI 降噪，OBS 內建的「RNNoise」濾鏡用的就是它）。

為什麼不用 Discord 的雜訊抑制：Discord 的 Krisp／標準降噪會把音效版的音效當成雜音濾掉，
所以使用音效版時必須關掉它。改在這裡只對「真實麥克風」降噪，音效完全不經過降噪，
兩邊就能兼顧：鍵盤聲被壓掉，音效原音送出。

RNNoise 的 DLL 來自 PyPI 套件 pyrnnoise 0.4.6（Apache-2.0，RNNoise 本體為 BSD-3），
放在 ``_vendor/``，用 ctypes 直接呼叫，避免安裝 pyrnnoise 時連帶裝進 matplotlib 等大型套件。
"""

from __future__ import annotations

import ctypes
import logging
import sys
from enum import Enum
from functools import lru_cache
from pathlib import Path

import numpy as np

from soundboard.config import CHANNELS
from soundboard.i18n import N_, tr

logger = logging.getLogger(__name__)

# RNNoise 以 int16 的數值範圍運算（但型別是 float）
_INT16_SCALE = 32768.0


class DenoiseLevel(str, Enum):
    """降噪強度，對應設定檔中的字串。"""

    OFF = "off"
    STANDARD = "standard"   # 只用 RNNoise
    STRONG = "strong"       # RNNoise ＋ 人聲閘門：不是人聲的片段完全靜音（鍵盤聲、滑鼠聲）

    @property
    def label(self) -> str:
        """介面上顯示的名稱（目前語言；設定檔存的是 value，不受語言影響）。"""
        return tr(_LEVEL_LABELS[self.value])


# 降噪強度的顯示名稱（value → 原文）。放在模組層級用 N_() 標記，顯示時才 tr()：模組載入時還沒決定語言
_LEVEL_LABELS: dict[str, str] = {
    "off": N_("關閉"),
    "standard": N_("標準"),
    "strong": N_("強（抑制鍵盤聲）"),
}


def _dll_path() -> Path:
    # exe 版時檔案被解壓到 sys._MEIPASS/soundboard/_vendor
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    return base / "soundboard" / "_vendor" / "rnnoise.dll"


@lru_cache(maxsize=1)
def _load_library() -> ctypes.CDLL | None:
    """載入 RNNoise；找不到或載入失敗時回傳 None（降噪功能停用，其他功能照常）。"""
    if sys.platform != "win32":
        return None
    path = _dll_path()
    try:
        lib = ctypes.CDLL(str(path))
    except OSError as exc:
        logger.warning("無法載入 RNNoise（%s）：%s", path, exc)
        return None
    lib.rnnoise_create.argtypes = [ctypes.c_void_p]
    lib.rnnoise_create.restype = ctypes.c_void_p
    lib.rnnoise_destroy.argtypes = [ctypes.c_void_p]
    lib.rnnoise_destroy.restype = None
    lib.rnnoise_get_frame_size.restype = ctypes.c_int
    lib.rnnoise_process_frame.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_float),
                                          ctypes.POINTER(ctypes.c_float)]
    lib.rnnoise_process_frame.restype = ctypes.c_float
    logger.info("已載入 RNNoise 降噪：%s", path)
    return lib


def denoise_available() -> bool:
    """這台電腦能不能使用降噪。

    Returns:
        bool: RNNoise 載入成功時為 True。
    """
    return _load_library() is not None


class Denoiser:
    """串流用的降噪器：輸入任意長度的區塊，輸出同樣長度的降噪結果（固定延遲約 30 ms）。

    RNNoise 一次只能處理 480 個樣本（48 kHz 的 10 ms），而音效卡回呼的區塊長度不固定，
    所以內部用 FIFO 湊滿一個 frame 才處理。先墊 480 個 0 讓輸出永遠有東西可拿，延遲固定不抖動
    （緩衝 10 ms ＋ RNNoise 內部 20 ms）。

    這個物件只會在麥克風的音訊執行緒中使用，因此不需要鎖。
    """

    def __init__(self, level: DenoiseLevel = DenoiseLevel.STANDARD) -> None:
        """
        Args:
            level (DenoiseLevel): 降噪強度（不可為 OFF）。

        Raises:
            RuntimeError: RNNoise 無法載入時拋出。
        """
        lib = _load_library()
        if lib is None:
            raise RuntimeError("RNNoise 無法載入")
        self._lib = lib
        self.level = level
        self._frame = int(lib.rnnoise_get_frame_size())
        self._state = lib.rnnoise_create(None)
        self._pending = np.zeros(0, np.float32)
        self._ready = np.zeros(self._frame, np.float32)
        self._buf = np.zeros(self._frame, np.float32)
        self._ptr = self._buf.ctypes.data_as(ctypes.POINTER(ctypes.c_float))
        self._gate: VoiceGate | None = None

    @property
    def gate_open(self) -> bool:
        """人聲閘門是否打開；不是「強」模式時永遠為 True。"""
        return self._gate.is_open if self._gate is not None else True

    def close(self) -> None:
        """釋放 RNNoise 狀態。"""
        if self._state:
            self._lib.rnnoise_destroy(self._state)
            self._state = None

    def __del__(self) -> None:
        self.close()

    def process(self, block: np.ndarray) -> np.ndarray:
        """處理一個麥克風區塊。

        Args:
            block (np.ndarray): ``(frames, channels)`` 的 float32 音訊（-1.0 ~ 1.0）。

        Returns:
            np.ndarray: ``(frames, CHANNELS)`` 的降噪結果。
        """
        mono = block.mean(axis=1, dtype=np.float32) if block.ndim == 2 else block.astype(np.float32)
        self._pending = np.concatenate((self._pending, mono))
        outputs = [self._ready]
        while len(self._pending) >= self._frame:
            self._buf[:] = self._pending[: self._frame] * _INT16_SCALE
            self._pending = self._pending[self._frame:]
            # 輸入與輸出用同一塊記憶體（RNNoise 支援就地處理）
            speech_prob = self._lib.rnnoise_process_frame(self._state, self._ptr, self._ptr)
            out = (self._buf / _INT16_SCALE).astype(np.float32)
            if self.level is DenoiseLevel.STRONG:
                if self._gate is None:
                    self._gate = VoiceGate()
                out = self._gate.process(out, float(speech_prob))
            else:
                self._gate = None
            outputs.append(out)
        ready = np.concatenate(outputs)
        n = len(mono)
        self._ready = ready[n:]
        result = ready[:n]
        if len(result) < n:  # 理論上不會發生（有預先墊 1 個 frame），保險起見補 0
            result = np.pad(result, (0, n - len(result)))
        return np.repeat(result[:, None], CHANNELS, axis=1)


class VoiceGate:
    """人聲閘門：依 RNNoise 的人聲機率，把不是在說話的片段完全靜音。

    RNNoise 擅長去除持續性雜音，但鍵盤、滑鼠這種短促聲響常常只被壓低一部分；
    它同時會算出每個 frame 是人聲的機率，所以用這個機率當開關，沒在說話時直接靜音。

    - 開啟門檻 ``open_threshold``：鍵盤聲的人聲機率通常很低，人聲通常 0.8 以上
    - 保持 ``hold_frames``：說完話後保持開啟一段時間，避免切掉字尾或句子中的短停頓
    - 預讀 1 個 frame（10 ms）：先看下一個 frame 是不是人聲，閘門在字頭之前就打開
    - 開啟時在 1 個 frame 內淡入、關閉時用 ``release_frames`` 個 frame 淡出，避免「喀」聲
    """

    def __init__(self, open_threshold: float = 0.6, hold_frames: int = 25, release_frames: int = 8) -> None:
        """
        Args:
            open_threshold (float): 人聲機率達到多少就打開閘門。
            hold_frames (int): 最後一次偵測到人聲後保持開啟的 frame 數（每個 10 ms）。
            release_frames (int): 關閉時淡出的 frame 數。
        """
        self.open_threshold = open_threshold
        self.hold_frames = hold_frames
        self.release_frames = max(1, release_frames)
        self._prev: np.ndarray | None = None
        self._prev_prob = 0.0
        self._hold = 0
        self._gain = 0.0

    @property
    def is_open(self) -> bool:
        """閘門目前是否打開（有聲音送出）。"""
        return self._gain > 0.0

    def process(self, frame: np.ndarray, speech_prob: float) -> np.ndarray:
        """送入一個 frame，取回「上一個」frame 套用閘門後的結果（預讀 1 個 frame）。

        Args:
            frame (np.ndarray): 降噪後的 frame。
            speech_prob (float): RNNoise 回傳的人聲機率（0–1）。

        Returns:
            np.ndarray: 與輸入等長的輸出；第一次呼叫時為靜音。
        """
        prev, prev_prob = self._prev, self._prev_prob
        self._prev, self._prev_prob = frame.copy(), speech_prob
        if prev is None:
            return np.zeros_like(frame)
        # 這個 frame 或下一個 frame 是人聲就打開：下一個 frame 是字頭時也能完整送出
        speech = max(prev_prob, speech_prob) >= self.open_threshold
        if speech:
            self._hold = self.hold_frames
        elif self._hold > 0:
            self._hold -= 1
        start = self._gain
        if speech or self._hold > 0:
            end = 1.0
        else:
            end = max(0.0, start - 1.0 / self.release_frames)
        self._gain = end
        if start == end == 0.0:
            return np.zeros_like(prev)
        return prev * np.linspace(start, end, len(prev), dtype=np.float32)


# Denoiser 的固定延遲：緩衝 1 個 frame ＋ RNNoise 內部 2 個 frame；「強」再加人聲閘門預讀 1 個 frame。
# 以實際音訊做互相關測得（標準 1440、強 1920 個樣本）。
_FRAME_SIZE = 480


def latency_samples(level: DenoiseLevel) -> int:
    """降噪造成的延遲（樣本數，48 kHz）。

    Args:
        level (DenoiseLevel): 降噪強度。

    Returns:
        int: 延遲樣本數；關閉時為 0。
    """
    if level is DenoiseLevel.OFF:
        return 0
    return _FRAME_SIZE * (4 if level is DenoiseLevel.STRONG else 3)


def denoise_clip(samples: np.ndarray, level: DenoiseLevel) -> np.ndarray:
    """把一整段單聲道錄音降噪，並扣掉延遲讓結果和原音對齊（方便前後比較）。

    Args:
        samples (np.ndarray): 一維 float32 原音。
        level (DenoiseLevel): 降噪強度；OFF 時回傳原音的複本。

    Returns:
        np.ndarray: 與輸入等長的降噪結果。

    Raises:
        RuntimeError: RNNoise 無法載入時拋出。
    """
    if level is DenoiseLevel.OFF or len(samples) == 0:
        return samples.astype(np.float32).copy()
    lag = latency_samples(level)
    # 尾端補靜音，把延遲中的最後一段也推出來
    padded = np.concatenate((samples.astype(np.float32), np.zeros(lag, np.float32)))
    denoiser = Denoiser(level)
    try:
        out = np.concatenate([denoiser.process(padded[i:i + _FRAME_SIZE, None])[:, 0]
                              for i in range(0, len(padded), _FRAME_SIZE)])
    finally:
        denoiser.close()
    return out[lag:lag + len(samples)]
