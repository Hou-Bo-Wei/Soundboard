"""音訊引擎：管理播放 / 錄音串流，把音效送往「自己聽」與「虛擬麥克風」。

訊號流程::

    音效 ──┬──> monitor_bus ──> 自己的耳機 / 喇叭
           └──> mic_bus ──┐
    真實麥克風 ──> 降噪(RNNoise) ──> RingBuffer ┴─(混音)─> 虛擬音源線 (CABLE Input) ──> Discord 讀 CABLE Output

降噪只作用在真實麥克風：音效不經過降噪，才不會像 Discord 的雜訊抑制那樣把音效濾掉。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

import numpy as np
import sounddevice as sd

from soundboard.config import CHANNELS, PASSTHROUGH_MAX_LATENCY, SAMPLE_RATE
from soundboard.denoise import Denoiser, DenoiseLevel, denoise_available
from soundboard.i18n import tr
from soundboard.mixer import LiveSource, MixBus, RingBuffer, SampleTap, Voice, soft_limit

logger = logging.getLogger(__name__)

# 麥克風測試最多保留幾秒（也是錄音長度上限）
MIC_TAP_SECONDS = 15


class EngineError(Exception):
    """音訊串流開啟失敗（訊息已翻譯，會直接顯示在狀態列）。"""


class AudioEngine:
    """音效版的音訊核心。

    三條串流彼此獨立，任一個裝置出錯都不影響其他輸出。

    Attributes:
        monitor_bus (MixBus): 自己聽的混音匯流排。
        mic_bus (MixBus): 送進虛擬麥克風的混音匯流排。
        passthrough_enabled (bool): 是否把真實麥克風混入虛擬麥克風。
        passthrough_gain (float): 真實麥克風音量。
        denoise_level (DenoiseLevel): 真實麥克風的降噪強度。
        mic_monitor_enabled (bool): 是否把（降噪後的）麥克風也送到自己的耳機，用來試聽效果。
        mic_test_active (bool): 麥克風測試視窗開著時為 True：保留原音與降噪後的取樣（波形、錄音用）。
        tap_raw (SampleTap): 麥克風原音（單聲道）。
        tap_processed (SampleTap): 降噪後、送給朋友的麥克風聲音（單聲道）。
        live_sources (dict[str, LiveSource]): 混音器送給朋友的其他程式聲音；整個換掉（不原地修改），
            音訊執行緒才不會讀到改到一半的 dict。
        master_gain (float): 朋友聽到的總音量（音效、麥克風、程式聲音全部）。
        levels (dict[str, float]): 最近一次輸出的峰值（``sounds``／``mic``／``master``／``monitor``），給混音器的音量表用。
    """

    def __init__(self, host_api: str) -> None:
        """
        Args:
            host_api (str): 要使用的音訊 API 名稱。
        """
        self.host_api = host_api
        self.monitor_bus = MixBus()
        self.mic_bus = MixBus()
        self.passthrough_enabled = True
        self.passthrough_gain = 1.0
        self._ring = RingBuffer(max(1, int(SAMPLE_RATE * PASSTHROUGH_MAX_LATENCY)))
        self._lock = threading.Lock()
        self._monitor_stream: sd.OutputStream | None = None
        self._mic_out_stream: sd.OutputStream | None = None
        self._mic_in_stream: sd.InputStream | None = None
        self._mic_in_channels = 1
        self.denoise_level = DenoiseLevel.OFF
        self.mic_monitor_enabled = False
        self._monitor_ring = RingBuffer(max(1, int(SAMPLE_RATE * PASSTHROUGH_MAX_LATENCY)))
        # 降噪器只在麥克風的音訊執行緒中建立與使用（見 _mic_in_callback），避免跨執行緒釋放
        self._denoiser: Denoiser | None = None
        self.mic_test_active = False
        self.tap_raw = SampleTap(SAMPLE_RATE * MIC_TAP_SECONDS)
        self.tap_processed = SampleTap(SAMPLE_RATE * MIC_TAP_SECONDS)
        # 回放錄音的「我的麥克風」來源：收到降噪後（朋友聽到的）聲音時呼叫，參數為 (frames, 2) float32。
        # 在音訊執行緒中呼叫，必須很快完成；None 表示沒有在錄
        self.mic_record_sink: Callable[[np.ndarray], None] | None = None
        self.live_sources: dict[str, LiveSource] = {}
        self.master_gain = 1.0
        self.levels: dict[str, float] = {"sounds": 0.0, "mic": 0.0, "master": 0.0, "monitor": 0.0}

    # ------------------------------------------------------------------ 串流管理
    def _extra_settings(self) -> Any:
        # WASAPI 共用模式要求取樣率與系統混音器一致；auto_convert 讓 Windows 幫忙轉換，
        # 否則遇到 44.1 kHz 的裝置會直接開啟失敗。
        if "WASAPI" in self.host_api:
            return sd.WasapiSettings(auto_convert=True)
        return None

    @staticmethod
    def _close(stream: sd.StreamBase | None) -> None:
        if stream is None:
            return
        try:
            stream.stop()
            stream.close()
        except sd.PortAudioError as exc:
            logger.warning("關閉串流時發生錯誤：%s", exc)

    def set_monitor_device(self, device_index: int | None) -> None:
        """設定「自己聽」的播放裝置。

        Args:
            device_index (int | None): 裝置編號；None 表示不播放給自己。

        Raises:
            EngineError: 裝置無法開啟時拋出。
        """
        with self._lock:
            self._close(self._monitor_stream)
            self._monitor_stream = None
            if device_index is not None:
                self._monitor_stream = self._open_output(device_index, self._monitor_callback)

    def set_virtual_mic_device(self, device_index: int | None) -> None:
        """設定虛擬麥克風的播放端（例如 CABLE Input）。

        Args:
            device_index (int | None): 裝置編號；None 表示不輸出到麥克風。

        Raises:
            EngineError: 裝置無法開啟時拋出。
        """
        with self._lock:
            self._close(self._mic_out_stream)
            self._mic_out_stream = None
            if device_index is not None:
                self._mic_out_stream = self._open_output(device_index, self._mic_out_callback)

    def set_mic_input_device(self, device_index: int | None) -> None:
        """設定真實麥克風（混入虛擬麥克風用）。

        Args:
            device_index (int | None): 裝置編號；None 表示不擷取麥克風。

        Raises:
            EngineError: 裝置無法開啟時拋出。
        """
        with self._lock:
            self._close(self._mic_in_stream)
            self._mic_in_stream = None
            self._ring.clear()
            self._monitor_ring.clear()
            # 串流已關閉，回呼不會再執行，這裡釋放降噪器是安全的
            if self._denoiser is not None:
                self._denoiser.close()
                self._denoiser = None
            if device_index is None:
                return
            info = sd.query_devices(device_index)
            self._mic_in_channels = max(1, min(CHANNELS, int(info["max_input_channels"])))
            try:
                stream = sd.InputStream(
                    device=device_index,
                    samplerate=SAMPLE_RATE,
                    channels=self._mic_in_channels,
                    dtype="float32",
                    latency="low",
                    callback=self._mic_in_callback,
                    extra_settings=self._extra_settings(),
                )
                stream.start()
            except (sd.PortAudioError, ValueError) as exc:
                raise EngineError(tr("無法開啟麥克風「{name}」：{error}", name=info["name"], error=exc)) from exc
            self._mic_in_stream = stream

    def _open_output(self, device_index: int, callback: Any) -> sd.OutputStream:
        info = sd.query_devices(device_index)
        try:
            stream = sd.OutputStream(
                device=device_index,
                samplerate=SAMPLE_RATE,
                channels=CHANNELS,
                dtype="float32",
                latency="low",
                callback=callback,
                extra_settings=self._extra_settings(),
            )
            stream.start()
        except (sd.PortAudioError, ValueError) as exc:
            raise EngineError(tr("無法開啟播放裝置「{name}」：{error}", name=info["name"], error=exc)) from exc
        return stream

    def close(self) -> None:
        """關閉所有串流。"""
        with self._lock:
            for stream in (self._monitor_stream, self._mic_out_stream, self._mic_in_stream):
                self._close(stream)
            self._monitor_stream = self._mic_out_stream = self._mic_in_stream = None

    @property
    def monitor_active(self) -> bool:
        """「自己聽」輸出是否正在運作（預覽需要它）。"""
        return self._monitor_stream is not None

    @property
    def mic_input_active(self) -> bool:
        """真實麥克風是否正在收音。"""
        return self._mic_in_stream is not None

    @property
    def virtual_mic_active(self) -> bool:
        """虛擬麥克風輸出是否正在運作。"""
        return self._mic_out_stream is not None

    # ------------------------------------------------------------------ 音訊回呼
    # 以下函式在 PortAudio 的即時執行緒中執行：不可阻塞、不可拋出例外。
    def _monitor_callback(self, outdata: np.ndarray, frames: int, _time: Any, _status: Any) -> None:
        mixed = self.monitor_bus.mix(frames)
        if self.mic_monitor_enabled and self._mic_in_stream is not None:
            mixed += self._monitor_ring.read(frames) * self.passthrough_gain
        soft_limit(mixed)
        self.levels["monitor"] = float(np.abs(mixed).max()) if frames else 0.0
        outdata[:] = mixed

    def _mic_out_callback(self, outdata: np.ndarray, frames: int, _time: Any, _status: Any) -> None:
        outdata[:] = self.render_friends(frames)

    def render_friends(self, frames: int) -> np.ndarray:
        """混出朋友聽到的聲音：音效＋（降噪後的）麥克風＋混音器裡的程式聲音，再套總音量。

        在虛擬麥克風的音訊執行緒中呼叫；獨立出來是為了不開任何裝置也能測試。

        Args:
            frames (int): 音框數。

        Returns:
            np.ndarray: (frames, 2) float32。
        """
        mixed = self.mic_bus.mix(frames)
        levels = self.levels
        levels["sounds"] = float(np.abs(mixed).max()) if frames else 0.0
        if self.passthrough_enabled and self._mic_in_stream is not None:
            mic = self._ring.read(frames) * self.passthrough_gain
            levels["mic"] = float(np.abs(mic).max()) if frames else 0.0
            mixed += mic
        else:
            levels["mic"] = 0.0
        for source in self.live_sources.values():
            source.mix_into(mixed)
        if self.master_gain != 1.0:
            mixed *= np.float32(self.master_gain)
        soft_limit(mixed)
        levels["master"] = float(np.abs(mixed).max()) if frames else 0.0
        return mixed

    def _mic_in_callback(self, indata: np.ndarray, _frames: int, _time: Any, _status: Any) -> None:
        data = self._denoise(indata)
        if data is None:
            data = indata if indata.shape[1] == CHANNELS else np.repeat(indata[:, :1], CHANNELS, axis=1)
        self._ring.write(data)
        if self.mic_test_active:
            self.tap_raw.write(indata.mean(axis=1, dtype=np.float32))
            self.tap_processed.write(data[:, 0].copy())
        if self.mic_monitor_enabled:
            self._monitor_ring.write(data)
        sink = self.mic_record_sink
        if sink is not None:
            try:
                sink(data)
            except Exception:  # noqa: BLE001 - 音訊回呼中不可拋出例外，否則麥克風會中斷
                logger.exception("回放錄音寫入麥克風失敗，停止錄麥克風")
                self.mic_record_sink = None

    def _denoise(self, indata: np.ndarray) -> np.ndarray | None:
        """依目前的降噪設定處理麥克風；關閉或無法使用時回傳 None（使用原音）。"""
        level = self.denoise_level
        if level is DenoiseLevel.OFF:
            if self._denoiser is not None:
                self._denoiser.close()
                self._denoiser = None
            return None
        if self._denoiser is None:
            try:
                self._denoiser = Denoiser(level)
                logger.info("麥克風降噪啟動：%s", level.label)
            except RuntimeError:
                # 載入失敗就退回原音，不讓麥克風中斷
                self.denoise_level = DenoiseLevel.OFF
                return None
        self._denoiser.level = level
        try:
            return self._denoiser.process(indata)
        except Exception:  # noqa: BLE001 - 音訊回呼中不可拋出例外，否則串流會停止
            logger.exception("降噪失敗，改用原音")
            self.denoise_level = DenoiseLevel.OFF
            return None

    @property
    def voice_gate_open(self) -> bool | None:
        """「強」降噪的人聲閘門是否打開（有聲音送給朋友）。

        Returns:
            bool | None: 沒有使用人聲閘門（未開麥克風或不是「強」）時為 None。
        """
        denoiser = self._denoiser
        if denoiser is None or self._mic_in_stream is None or self.denoise_level is not DenoiseLevel.STRONG:
            return None
        return denoiser.gate_open

    @staticmethod
    def denoise_supported() -> bool:
        """這台電腦能不能使用麥克風降噪。

        Returns:
            bool: 可以使用時為 True。
        """
        return denoise_available()

    # ------------------------------------------------------------------ 播放控制
    def play(self, sound_key: str, clip: np.ndarray, gain: float = 1.0, *, to_mic: bool = True) -> None:
        """播放一個音效。

        Args:
            sound_key (str): 音效 key。
            clip (np.ndarray): (frames, 2) float32 音訊。
            gain (float): 此音效的音量倍率。
            to_mic (bool): 是否也送進虛擬麥克風；False 只有自己聽得到（試聽）。
        """
        # 兩條匯流排各自有播放位置，所以要建立兩個 Voice（音訊資料本身共用，不複製）
        self.monitor_bus.add(Voice(sound_key, clip, gain))
        if to_mic:
            self.mic_bus.add(Voice(sound_key, clip, gain))

    def stop(self, sound_key: str | None = None) -> None:
        """停止播放。

        Args:
            sound_key (str | None): 只停止此音效；None 表示全部停止。
        """
        self.monitor_bus.stop(sound_key)
        self.mic_bus.stop(sound_key)

    def active_keys(self) -> set[str]:
        """正在播放的音效 key。

        Returns:
            set[str]: key 集合。
        """
        return self.monitor_bus.active_keys() | self.mic_bus.active_keys()
