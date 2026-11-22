"""即時回放錄音：持續錄下多個來源最近 N 秒的聲音，按「擷取」就把每個來源分軌存下來。

來源可以是：

* 程式（例如 ``Discord.exe``、``chrome.exe``）：用 :class:`ProcessCapture` 只錄該程式的聲音；
* 我的麥克風（:data:`MIC_SOURCE`）：接在音訊引擎的麥克風串流上，錄到的是降噪後、朋友聽到的聲音。

架構：

* 每個來源有自己的 :class:`RingBuffer`（程式以 PID 為單位，同一個程式重開會有新的緩衝區）。
  擷取執行緒只把音訊寫進環形緩衝區，不做其他處理，所以幾乎不耗 CPU。
* 緩衝區存 int16 立體聲（每秒 192 KB；3 分鐘約 35 MB），比 float32 省一半記憶體。
* 背景監看執行緒每 2 秒檢查目標程式是否啟動／關閉（例如 Discord 重開），自動重新連上。
* 擷取時把各緩衝區依「實際時間」對齊，所以就算某個程式中途關閉，它之前的聲音仍在正確的時間點；
  每個來源分成獨立音軌，編輯時才能只留想要的來源、各自調整音量。
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from soundboard.i18n import N_, tr
from soundboard.process_capture import CHANNELS, SAMPLE_RATE, CaptureError, ProcessCapture, list_processes, root_pids

logger = logging.getLogger(__name__)

REPLAY_SECONDS_CHOICES: tuple[int, ...] = (30, 60, 120, 180, 300, 600)
WATCH_INTERVAL = 2.0
# 「我的麥克風」來源在設定檔中的代號（不會和執行檔名稱衝突）
MIC_SOURCE = "@mic"
# 顯示名稱（顯示時才 tr；設定檔與片段的中繼資料只存 MIC_SOURCE，不受語言影響）
MIC_LABEL = N_("我的麥克風")
# 收到的音框比實際經過的時間少這麼多就補靜音，讓各來源的緩衝區維持與實際時間對齊
_GAP_FILL_SECONDS = 0.25
# 麥克風這麼久沒有送資料，就視為沒有在錄（例如設定頁把麥克風關掉了）
_MIC_STALE_SECONDS = 1.0
_INT16_SCALE = 32768.0


def source_label(source: str) -> str:
    """來源的顯示名稱（``Discord.exe`` → ``Discord``、:data:`MIC_SOURCE` → ``我的麥克風``）。

    Args:
        source (str): 來源代號。

    Returns:
        str: 顯示名稱。
    """
    return tr(MIC_LABEL) if source == MIC_SOURCE else Path(source).stem


class RingBuffer:
    """固定長度的 int16 立體聲環形緩衝區，只保留最近 ``capacity`` 個音框。

    寫入在擷取執行緒、讀取在介面執行緒，所以用鎖保護。

    Args:
        capacity (int): 最多保留的音框數。
    """

    def __init__(self, capacity: int) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        self.capacity = capacity
        # np.zeros 在 Windows 上會向系統要「延遲配置」的記憶體，沒寫過的部分不佔實體記憶體
        self._data = np.zeros((capacity, CHANNELS), dtype=np.int16)
        self._pos = 0
        self._filled = 0
        self.last_write: float | None = None
        self._clock_start: float | None = None
        self._total = 0
        self._lock = threading.Lock()

    @property
    def filled(self) -> int:
        """目前保留的音框數。"""
        return self._filled

    def write(self, pcm: np.ndarray, now: float | None = None) -> None:
        """寫入一段音訊；收到的比實際經過的時間少很多時，先補上相應長度的靜音。

        Args:
            pcm (np.ndarray): (frames, 2) int16。
            now (float | None): 目前的 ``time.monotonic()``（測試時可指定）。
        """
        now = time.monotonic() if now is None else now
        with self._lock:
            if self._clock_start is None:
                self._clock_start = now - len(pcm) / SAMPLE_RATE
            # 比較「收到的音框總數」與「實際經過的時間」，而不是只看相鄰兩次寫入的間隔：
            # 執行緒偶爾被延遲時封包會一次湧入，只看間隔會誤判成斷線而多補靜音
            deficit = int((now - self._clock_start) * SAMPLE_RATE) - (self._total + len(pcm))
            limit = int(_GAP_FILL_SECONDS * SAMPLE_RATE)
            if deficit > limit:
                self._write_locked(np.zeros((min(deficit, self.capacity), CHANNELS), dtype=np.int16))
                self._total += deficit
            elif deficit < -limit:
                # 音效卡時脈比系統時鐘快一點點時會慢慢累積，重新對齊基準即可
                self._clock_start -= -deficit / SAMPLE_RATE
            self._write_locked(pcm)
            self._total += len(pcm)
            self.last_write = now

    def write_float(self, data: np.ndarray) -> None:
        """寫入 float32 音訊（-1–1，單聲道或立體聲），轉成 int16 後存入。

        Args:
            data (np.ndarray): (frames, channels) float32。
        """
        stereo = data if data.shape[1] == CHANNELS else np.repeat(data[:, :1], CHANNELS, axis=1)
        self.write((np.clip(stereo, -1.0, 1.0) * 32767.0).astype(np.int16))

    def _write_locked(self, pcm: np.ndarray) -> None:
        n = len(pcm)
        if n >= self.capacity:
            self._data[:] = pcm[-self.capacity:]
            self._pos = 0
            self._filled = self.capacity
            return
        end = self._pos + n
        if end <= self.capacity:
            self._data[self._pos:end] = pcm
        else:
            first = self.capacity - self._pos
            self._data[self._pos:] = pcm[:first]
            self._data[:n - first] = pcm[first:]
        self._pos = end % self.capacity
        self._filled = min(self.capacity, self._filled + n)

    def latest(self, frames: int) -> np.ndarray:
        """取出最近 ``frames`` 個音框（不足時回傳全部），依時間先後排列。

        Args:
            frames (int): 要取的音框數。

        Returns:
            np.ndarray: (n, 2) int16 複本。
        """
        with self._lock:
            n = min(frames, self._filled)
            start = (self._pos - n) % self.capacity
            if start + n <= self.capacity:
                return self._data[start:start + n].copy()
            return np.concatenate([self._data[start:], self._data[:self._pos]])


@dataclass
class _Track:
    """一個正在（或曾經）被錄的來源實例。"""

    source: str
    pid: int
    ring: RingBuffer
    capture: ProcessCapture | None = None
    error: str | None = None


@dataclass
class SourceStatus:
    """介面顯示用的來源錄音狀態。

    Attributes:
        source (str): 來源代號（執行檔名稱或 :data:`MIC_SOURCE`）。
        state (str): ``recording`` / ``waiting``（程式沒開、麥克風沒開）/ ``error``。
        detail (str): 補充說明（錯誤訊息等）。
    """

    source: str
    state: str
    detail: str = ""


class ReplayRecorder:
    """持續錄下多個來源最近 ``seconds`` 秒的聲音。

    Args:
        seconds (int): 保留的秒數。
        sources (list[str]): 要錄的來源（執行檔名稱，或 :data:`MIC_SOURCE`）。
        capture_factory (Callable | None): 建立程式擷取物件的函式（測試時可替換）。
        process_lister (Callable | None): 列出程式的函式（測試時可替換）。
        mic_hook (Callable | None): 安裝／移除麥克風接收函式（傳入 None 表示移除）；
            None 表示不支援錄麥克風。
    """

    def __init__(self, seconds: int, sources: list[str],
                 capture_factory: Callable[..., ProcessCapture] | None = None,
                 process_lister: Callable[[], dict[int, tuple[str, int]]] | None = None,
                 mic_hook: Callable[[Callable[[np.ndarray], None] | None], None] | None = None) -> None:
        self.seconds = int(seconds)
        self.sources = list(sources)
        self._factory = capture_factory or ProcessCapture
        self._lister = process_lister or list_processes
        self._mic_hook = mic_hook
        self._mic: _Track | None = None
        self._tracks: list[_Track] = []
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def apps(self) -> list[str]:
        """要錄的程式（不含麥克風）。"""
        return [s for s in self.sources if s != MIC_SOURCE]

    # ------------------------------------------------------------------ 生命週期
    @property
    def running(self) -> bool:
        """是否正在錄音。"""
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        """開始錄音（背景監看執行緒會自動連上目標程式）。"""
        if self.running:
            return
        self._stop.clear()
        self._sync_mic()
        self._thread = threading.Thread(target=self._watch_loop, name="ReplayWatcher", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """停止錄音並丟棄緩衝區內容。"""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None
        self._detach_mic()
        with self._lock:
            tracks, self._tracks = self._tracks, []
        for track in tracks:
            if track.capture is not None:
                track.capture.stop()

    def configure(self, seconds: int, sources: list[str]) -> None:
        """變更保留秒數或要錄的來源。

        秒數改變時必須重新配置緩衝區，舊的錄音會清空；只改來源則保留其他來源的錄音。

        Args:
            seconds (int): 保留的秒數。
            sources (list[str]): 要錄的來源。
        """
        was_running = self.running
        if int(seconds) != self.seconds:
            self.stop()
            self.seconds = int(seconds)
            self.sources = list(sources)
            if was_running:
                self.start()
            return
        self.sources = list(sources)
        wanted = {a.lower() for a in self.apps}
        with self._lock:
            removed = [t for t in self._tracks if t.source.lower() not in wanted]
            self._tracks = [t for t in self._tracks if t.source.lower() in wanted]
        for track in removed:
            if track.capture is not None:
                track.capture.stop()
        if was_running:
            self._sync_mic()
            self.refresh()
        elif MIC_SOURCE not in self.sources:
            self._detach_mic()

    # ------------------------------------------------------------------ 麥克風
    def _sync_mic(self) -> None:
        if MIC_SOURCE in self.sources and self._mic_hook is not None:
            if self._mic is None:
                self._mic = _Track(MIC_SOURCE, 0, RingBuffer(self.seconds * SAMPLE_RATE))
                self._mic_hook(self._mic.ring.write_float)
        else:
            self._detach_mic()

    def _detach_mic(self) -> None:
        if self._mic is not None:
            if self._mic_hook is not None:
                self._mic_hook(None)
            self._mic = None

    # ------------------------------------------------------------------ 監看
    def _watch_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.refresh()
            except Exception:  # noqa: BLE001 - 監看失敗不能讓錄音整個停掉，下次再試
                logger.exception("回放錄音：檢查程式時發生錯誤")
            self._stop.wait(WATCH_INTERVAL)

    def refresh(self) -> None:
        """連上新啟動的目標程式、清掉已經過期的舊錄音。"""
        procs = self._lister()
        now = time.monotonic()
        with self._lock:
            # 程式關掉後，它的錄音保留到超出回放長度為止
            self._tracks = [t for t in self._tracks if self._track_alive(t, now, procs)]
            known = {t.pid for t in self._tracks if t.capture is not None and t.capture.running}
            failed = {t.pid for t in self._tracks if t.error}
        for exe in self.apps:
            for pid in root_pids(exe, procs):
                if pid in known or pid in failed or self._stop.is_set():
                    continue
                self._attach(exe, pid)

    def _track_alive(self, track: _Track, now: float, procs: dict[int, tuple[str, int]]) -> bool:
        if track.capture is not None and track.capture.running:
            return True
        if track.error:
            # 失敗的程式在它還開著時不再重試（避免每 2 秒寫一次錯誤紀錄），重開後才再試
            return track.pid in procs
        last = track.ring.last_write
        return last is not None and now - last < self.seconds

    def _attach(self, exe: str, pid: int) -> None:
        track = _Track(exe, pid, RingBuffer(self.seconds * SAMPLE_RATE))
        try:
            capture = self._factory(pid, track.ring.write)
            capture.start()
        except CaptureError as exc:
            track.error = str(exc)
            logger.warning("回放錄音：無法錄 %s（PID %d）：%s", exe, pid, exc)
        else:
            track.capture = capture
            logger.info("回放錄音：開始錄 %s（PID %d）", exe, pid)
        with self._lock:
            self._tracks.append(track)

    def _all_tracks(self) -> list[_Track]:
        with self._lock:
            tracks = list(self._tracks)
        if self._mic is not None:
            tracks.append(self._mic)
        return tracks

    def status(self) -> list[SourceStatus]:
        """每個來源目前的錄音狀態。

        Returns:
            list[SourceStatus]: 依設定順序排列。
        """
        tracks = self._all_tracks()
        now = time.monotonic()
        result = []
        for source in self.sources:
            if source == MIC_SOURCE:
                last = self._mic.ring.last_write if self._mic is not None else None
                if self._mic_hook is None:
                    result.append(SourceStatus(source, "error", tr("無法錄製麥克風")))
                elif last is not None and now - last < _MIC_STALE_SECONDS:
                    result.append(SourceStatus(source, "recording"))
                else:
                    result.append(SourceStatus(source, "waiting", tr("未收到聲音，請確認設定頁的實體麥克風")))
                continue
            mine = [t for t in tracks if t.source.lower() == source.lower()]
            if any(t.capture is not None and t.capture.running for t in mine):
                result.append(SourceStatus(source, "recording"))
            elif any(t.error for t in mine):
                result.append(SourceStatus(source, "error", next(t.error for t in mine if t.error) or ""))
            else:
                result.append(SourceStatus(source, "waiting", tr("程式未執行")))
        return result

    def buffered_seconds(self) -> float:
        """目前最多可以擷取幾秒。"""
        return max((t.ring.filled for t in self._all_tracks()), default=0) / SAMPLE_RATE

    # ------------------------------------------------------------------ 擷取
    def snapshot_tracks(self, now: float | None = None) -> list[tuple[str, np.ndarray]]:
        """取出每個來源最近 ``seconds`` 秒的聲音，依實際時間對齊成等長的音軌。

        同一個程式的多個實例（例如中途重開）會合併成一軌。

        Args:
            now (float | None): 目前的 ``time.monotonic()``（測試時可指定）。

        Returns:
            list[tuple[str, np.ndarray]]: (來源, (frames, 2) float32) 清單，依設定順序；
            沒有任何錄音時為空清單。
        """
        now = time.monotonic() if now is None else now
        total = self.seconds * SAMPLE_RATE
        parts: dict[str, list[tuple[int, np.ndarray]]] = {}
        for track in self._all_tracks():
            if track.ring.last_write is None:
                continue
            # lag：這個來源最後一筆音訊距離現在多久（程式已關閉時會很大）
            lag = int(max(0.0, now - track.ring.last_write) * SAMPLE_RATE)
            if lag >= total:
                continue
            pcm = track.ring.latest(total - lag)
            if len(pcm):
                parts.setdefault(track.source.lower(), []).append((lag, pcm))
        if not parts:
            return []
        length = max(lag + len(pcm) for items in parts.values() for lag, pcm in items)
        result = []
        order = {s.lower(): i for i, s in enumerate(self.sources)}
        names = {t.source.lower(): t.source for t in self._all_tracks()}
        for key in sorted(parts, key=lambda k: order.get(k, len(order))):
            audio = np.zeros((length, CHANNELS), dtype=np.float32)
            for lag, pcm in parts[key]:
                end = length - lag
                audio[end - len(pcm):end] += pcm.astype(np.float32) / _INT16_SCALE
            result.append((names.get(key, key), np.clip(audio, -1.0, 1.0)))
        return result

    def snapshot(self, now: float | None = None) -> np.ndarray:
        """把所有來源混成一段（不調整音量）。

        Args:
            now (float | None): 目前的 ``time.monotonic()``（測試時可指定）。

        Returns:
            np.ndarray: (frames, 2) float32；沒有任何錄音時長度為 0。
        """
        tracks = self.snapshot_tracks(now)
        if not tracks:
            return np.zeros((0, CHANNELS), dtype=np.float32)
        return np.clip(np.sum([audio for _src, audio in tracks], axis=0), -1.0, 1.0).astype(np.float32)
