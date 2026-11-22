"""即時混音元件：音效聲部、混音匯流排與麥克風直通用的環形緩衝。

這些類別會在音訊回呼（另一條執行緒）中被呼叫，因此：
* 所有共用狀態都以 lock 保護；
* lock 內只做最少的工作，避免音訊斷音。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

import numpy as np

from soundboard.config import CHANNELS, LIMITER_THRESHOLD, SAMPLE_RATE


def soft_limit(buf: np.ndarray, threshold: float = LIMITER_THRESHOLD) -> None:
    """柔性限幅（原地修改），讓音量調到 100% 以上時不會刺耳爆音。

    低於門檻的訊號完全不變；超過門檻的部分以 tanh 平滑壓縮，
    使輸出永遠落在 (-1, 1) 內。相較直接截斷（hard clip），失真聽起來柔和得多。

    Args:
        buf (np.ndarray): float32 音訊，會被直接修改。
        threshold (float): 開始壓縮的振幅，需介於 0 與 1 之間。

    Raises:
        ValueError: threshold 不在 (0, 1) 範圍內時拋出。
    """
    if not 0.0 < threshold < 1.0:
        raise ValueError("threshold must be between 0 and 1")
    mag = np.abs(buf)
    over = mag > threshold
    if not over.any():
        return
    headroom = 1.0 - threshold
    squeezed = threshold + headroom * np.tanh((mag[over] - threshold) / headroom)
    buf[over] = np.sign(buf[over]) * squeezed


@dataclass
class Voice:
    """正在播放的一個音效實例。

    Attributes:
        sound_key (str): 對應 SoundItem.key，用來查詢 / 停止。
        clip (np.ndarray): (frames, 2) float32 音訊資料（多個 Voice 可共用同一份）。
        gain (float): 此實例的音量倍率。
        position (int): 目前播放到的 frame。
    """

    sound_key: str
    clip: np.ndarray
    gain: float = 1.0
    position: int = 0

    @property
    def finished(self) -> bool:
        """是否已播放完畢。"""
        return self.position >= len(self.clip)


class MixBus:
    """一條輸出匯流排，把所有正在播放的音效混成一段輸出。

    「自己聽」與「虛擬麥克風」各用一條，因為兩個裝置的時脈不同步，
    各自依自己的速度讀取，才不會互相拖累。
    """

    def __init__(self, gain: float = 1.0) -> None:
        """
        Args:
            gain (float): 匯流排總音量。
        """
        self._lock = threading.Lock()
        self._voices: list[Voice] = []
        self.gain = gain

    def add(self, voice: Voice) -> None:
        """加入一個要播放的聲部。

        Args:
            voice (Voice): 聲部。
        """
        with self._lock:
            self._voices.append(voice)

    def stop(self, sound_key: str | None = None) -> None:
        """停止播放。

        Args:
            sound_key (str | None): 只停止此音效；None 表示全部停止。
        """
        with self._lock:
            if sound_key is None:
                self._voices.clear()
            else:
                self._voices = [v for v in self._voices if v.sound_key != sound_key]

    def active_keys(self) -> set[str]:
        """目前正在播放的音效 key。

        Returns:
            set[str]: key 集合。
        """
        with self._lock:
            return {v.sound_key for v in self._voices}

    def mix(self, frames: int) -> np.ndarray:
        """產生下一段混音結果，並推進所有聲部的播放位置。

        Args:
            frames (int): 要產生的 frame 數。

        Returns:
            np.ndarray: (frames, 2) float32，尚未限幅。
        """
        out = np.zeros((frames, CHANNELS), dtype=np.float32)
        with self._lock:
            alive: list[Voice] = []
            for voice in self._voices:
                chunk = voice.clip[voice.position : voice.position + frames]
                n = len(chunk)
                if n:
                    out[:n] += chunk * voice.gain
                voice.position += n
                if not voice.finished:
                    alive.append(voice)
            self._voices = alive
            gain = self.gain
        if gain != 1.0:
            out *= gain
        return out


class RingBuffer:
    """固定容量的音訊環形緩衝，用於「真實麥克風 → 虛擬麥克風」直通。

    輸入與輸出是兩個獨立裝置，時脈會有微小差異，
    因此寫入過多時丟掉最舊資料、讀取不足時補靜音，維持低延遲。
    """

    def __init__(self, max_frames: int) -> None:
        """
        Args:
            max_frames (int): 最多保留的 frame 數（決定最大延遲）。

        Raises:
            ValueError: max_frames 不為正數時拋出。
        """
        if max_frames <= 0:
            raise ValueError("max_frames must be positive")
        self._lock = threading.Lock()
        self._buf = np.zeros((max_frames, CHANNELS), dtype=np.float32)
        self._max = max_frames
        self._start = 0
        self._size = 0

    @property
    def size(self) -> int:
        """目前緩衝中的 frame 數。"""
        with self._lock:
            return self._size

    def clear(self) -> None:
        """清空緩衝。"""
        with self._lock:
            self._start = 0
            self._size = 0

    def write(self, data: np.ndarray) -> None:
        """寫入音訊，超出容量時捨棄最舊資料。

        Args:
            data (np.ndarray): (frames, 2) float32 音訊。
        """
        if len(data) >= self._max:
            data = data[-self._max :]
        n = len(data)
        with self._lock:
            overflow = self._size + n - self._max
            if overflow > 0:
                self._start = (self._start + overflow) % self._max
                self._size -= overflow
            end = (self._start + self._size) % self._max
            first = min(n, self._max - end)
            self._buf[end : end + first] = data[:first]
            if first < n:
                self._buf[: n - first] = data[first:]
            self._size += n

    def read(self, frames: int) -> np.ndarray:
        """讀出音訊，資料不足的部分補 0（靜音）。

        Args:
            frames (int): 要讀出的 frame 數。

        Returns:
            np.ndarray: (frames, 2) float32 音訊。
        """
        out = np.zeros((frames, CHANNELS), dtype=np.float32)
        with self._lock:
            n = min(frames, self._size)
            first = min(n, self._max - self._start)
            out[:first] = self._buf[self._start : self._start + first]
            if first < n:
                out[first:n] = self._buf[: n - first]
            self._start = (self._start + n) % self._max
            self._size -= n
        return out


class LiveSource:
    """即時音訊來源（例如其他程式的聲音），送進虛擬麥克風混音。

    擷取執行緒寫入、音訊輸出執行緒讀出，兩邊時脈不同步：
    先累積 ``prebuffer`` 個音框才開始輸出，讀到不夠時回到累積狀態，
    比每次不足就補靜音少很多「喀喀」聲；寫太多時 :class:`RingBuffer` 會丟掉最舊的資料，延遲不會越來越大。

    Attributes:
        gain (float): 音量倍率。
        muted (bool): 是否靜音（仍會讀出資料，避免解除靜音時播出累積的舊聲音）。
        peak (float): 最近一段收到的峰值（調整音量前，0–1），給音量表用。
        last_write (float): 最後一次收到資料的 ``time.monotonic()``。
    """

    def __init__(self, max_latency: float = 0.15, prebuffer: float = 0.03) -> None:
        """
        Args:
            max_latency (float): 最大延遲（秒）。
            prebuffer (float): 開始輸出前要累積的秒數。
        """
        self._ring = RingBuffer(max(1, int(SAMPLE_RATE * max_latency)))
        self._prebuffer = max(1, int(SAMPLE_RATE * prebuffer))
        self._buffering = True
        self.gain = 1.0
        self.muted = False
        self.peak = 0.0
        self.last_write = 0.0

    def write_pcm(self, pcm: np.ndarray) -> None:
        """寫入 int16 立體聲（擷取執行緒呼叫）。

        Args:
            pcm (np.ndarray): (frames, 2) int16。
        """
        data = pcm.astype(np.float32) * np.float32(1 / 32768)
        self.peak = float(np.abs(data).max()) if len(data) else 0.0
        self.last_write = time.monotonic()
        self._ring.write(data)

    def mix_into(self, out: np.ndarray) -> None:
        """把這個來源加進輸出緩衝（音訊輸出執行緒呼叫）。

        Args:
            out (np.ndarray): (frames, 2) float32，原地加總。
        """
        frames = len(out)
        if self._buffering:
            if self._ring.size < self._prebuffer + frames:
                return
            self._buffering = False
        if self._ring.size < frames:
            self._buffering = True
        data = self._ring.read(frames)
        if not self.muted and self.gain > 0:
            out += data * np.float32(self.gain)

    def level(self, now: float | None = None) -> float:
        """目前的音量表數值（套用音量後；太久沒收到資料視為 0）。

        Args:
            now (float | None): 目前時間，預設 ``time.monotonic()``。

        Returns:
            float: 0 以上的峰值。
        """
        now = time.monotonic() if now is None else now
        if self.muted or now - self.last_write > 0.3:
            return 0.0
        return self.peak * self.gain


class SampleTap:
    """保留最近一段時間的單聲道音訊，供波形圖與錄音讀取。

    音訊執行緒持續寫入，介面執行緒隨時讀取「最新 N 個樣本」或「從某個位置之後的全部樣本」。
    以總寫入數量當位置，即使環形緩衝繞回去也能正確算出錄音範圍。
    """

    def __init__(self, capacity: int) -> None:
        """
        Args:
            capacity (int): 最多保留的樣本數。
        """
        self._buf = np.zeros(capacity, dtype=np.float32)
        self._written = 0
        self._lock = threading.Lock()

    @property
    def capacity(self) -> int:
        """最多保留的樣本數。"""
        return len(self._buf)

    @property
    def position(self) -> int:
        """目前為止寫入的總樣本數（當作錄音的起點標記）。"""
        with self._lock:
            return self._written

    def write(self, samples: np.ndarray) -> None:
        """寫入樣本；超過容量時覆蓋最舊的資料。

        Args:
            samples (np.ndarray): 一維 float32 樣本。
        """
        total = len(samples)
        cap = len(self._buf)
        if total > cap:
            samples = samples[-cap:]
        n = len(samples)
        with self._lock:
            # 被丟掉的前段也要算進位置，第 k 個樣本固定放在 k % cap
            start = (self._written + total - n) % cap
            first = min(n, cap - start)
            self._buf[start:start + first] = samples[:first]
            self._buf[: n - first] = samples[first:]
            # 位置以實際寫入的總數計算（即使超過容量的部分被丟掉）
            self._written += total

    def latest(self, count: int) -> np.ndarray:
        """取最新的 ``count`` 個樣本；資料不足時前面補 0。

        Args:
            count (int): 樣本數。

        Returns:
            np.ndarray: 長度為 ``count`` 的陣列。
        """
        with self._lock:
            available = min(count, len(self._buf), self._written)
            return self._slice(self._written - available, self._written, pad_to=count)

    def since(self, position: int) -> np.ndarray:
        """取從 ``position`` 之後寫入的全部樣本（最多容量那麼多）。

        Args:
            position (int): 之前從 :attr:`position` 取得的位置。

        Returns:
            np.ndarray: 樣本陣列。
        """
        with self._lock:
            start = max(position, self._written - len(self._buf))
            return self._slice(start, self._written)

    def _slice(self, start: int, end: int, pad_to: int = 0) -> np.ndarray:
        cap = len(self._buf)
        n = max(0, end - start)
        idx = (np.arange(start, end) % cap) if n else np.zeros(0, dtype=np.int64)
        out = self._buf[idx].copy()
        if pad_to > n:
            out = np.concatenate((np.zeros(pad_to - n, np.float32), out))
        return out

    def clear(self) -> None:
        """清空內容（位置歸零）。"""
        with self._lock:
            self._buf[:] = 0
            self._written = 0
