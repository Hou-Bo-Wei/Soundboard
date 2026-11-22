"""混音器與解碼器的單元測試。"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from soundboard.decoder import decode_file, resample, to_stereo
from soundboard.library import scan_folder
from soundboard.mixer import MixBus, RingBuffer, Voice


def _clip(frames: int, value: float) -> np.ndarray:
    return np.full((frames, 2), value, dtype=np.float32)


def test_bus_mixes_and_removes_finished_voices() -> None:
    bus = MixBus()
    bus.add(Voice("a", _clip(100, 0.25)))
    bus.add(Voice("b", _clip(50, 0.25)))
    out = bus.mix(80)
    assert out[0, 0] == pytest.approx(0.5)   # 兩個聲部疊加
    assert out[60, 0] == pytest.approx(0.25)  # b 已播完
    assert bus.active_keys() == {"a"}
    bus.mix(80)
    assert bus.active_keys() == set()


def test_bus_gain_and_stop() -> None:
    bus = MixBus(gain=0.5)
    bus.add(Voice("a", _clip(10, 1.0), gain=0.5))
    assert bus.mix(5)[0, 0] == pytest.approx(0.25)
    bus.stop("a")
    assert not bus.mix(5).any()


def test_ring_buffer_drops_oldest_and_pads_silence() -> None:
    ring = RingBuffer(4)
    ring.write(np.arange(12, dtype=np.float32).reshape(6, 2))  # 超出容量，只留最後 4 個 frame
    out = ring.read(6)
    assert out[:4, 0].tolist() == [4, 6, 8, 10]
    assert not out[4:].any()
    assert ring.size == 0


def test_ring_buffer_wraps_around() -> None:
    ring = RingBuffer(4)
    ring.write(_clip(3, 1.0))
    ring.read(2)
    ring.write(_clip(3, 2.0))  # 這次寫入會跨越陣列尾端
    assert ring.read(4)[:, 0].tolist() == [1, 2, 2, 2]


def test_to_stereo_and_resample() -> None:
    mono = np.ones((10, 1), dtype=np.float32)
    assert to_stereo(mono).shape == (10, 2)
    up = resample(to_stereo(mono), 24_000, 48_000)
    assert up.shape == (20, 2)


def test_decode_and_scan(tmp_path: Path) -> None:
    tone = np.sin(np.linspace(0, 200, 22_050)).astype(np.float32)
    sf.write(tmp_path / "beep.wav", tone, 22_050)
    sf.write(tmp_path / "beep2.ogg", tone, 22_050)
    (tmp_path / "notes.txt").write_text("not audio")

    items = scan_folder(tmp_path, {"beep.wav": "嗶"}, {})
    assert [it.display_name for it in items] == ["beep2", "嗶"]
    data = decode_file(tmp_path / "beep.wav")
    assert data.shape == (48_000, 2)  # 22.05 kHz 一秒 → 重新取樣成 48 kHz
    assert data.dtype == np.float32


def test_soft_limit_keeps_quiet_and_bounds_loud() -> None:
    from soundboard.mixer import soft_limit

    buf = np.array([[0.5, -0.5], [1.5, -3.0]], dtype=np.float32)
    soft_limit(buf)
    assert buf[0].tolist() == [0.5, -0.5]          # 門檻以下完全不變
    assert 0.8 < buf[1, 0] <= 1.0 and -1.0 <= buf[1, 1] < -0.8


def test_monitor_never_picks_virtual_cable() -> None:
    from soundboard.devices import is_virtual_cable, pick_monitor_device
    from soundboard.models import AudioDevice

    outs = [
        AudioDevice(1, "CABLE Input (VB-Audio Virtual Cable)", "WASAPI", 0, 2),
        AudioDevice(2, "CABLE In 16ch (VB-Audio Virtual Cable)", "WASAPI", 0, 16),
        AudioDevice(3, "喇叭 (2- EDIFIER S880 MKII)", "WASAPI", 0, 2),
        AudioDevice(4, "喇叭 (Logitech G733 Gaming Headset)", "WASAPI", 0, 8),
    ]
    assert is_virtual_cable(outs[1].name)
    assert pick_monitor_device(outs, "麥克風 (Logitech G733 Gaming Headset)").index == 4  # 同一組耳麥
    assert pick_monitor_device(outs, None).index == 3
