"""麥克風降噪（RNNoise）的測試。"""

import numpy as np
import pytest

from soundboard.config import CHANNELS
from soundboard.denoise import Denoiser, DenoiseLevel, denoise_available

pytestmark = pytest.mark.skipif(not denoise_available(), reason="RNNoise 無法載入")


def _rms(x: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.square(x))))


@pytest.mark.parametrize("block", [64, 256, 480, 1000])
def test_output_has_same_length_for_any_block_size(block: int) -> None:
    d = Denoiser()
    for _ in range(10):
        out = d.process(np.zeros((block, 1), np.float32))
        assert out.shape == (block, CHANNELS)
        assert out.dtype == np.float32


def test_removes_steady_noise() -> None:
    rng = np.random.default_rng(0)
    noise = rng.normal(0, 0.02, 48000).astype(np.float32)
    d = Denoiser()
    out = np.concatenate([d.process(noise[i:i + 256, None]) for i in range(0, len(noise), 256)])
    # 前 0.5 秒讓模型適應，之後雜訊應該被壓低 20 dB 以上
    assert _rms(out[24000:, 0]) < _rms(noise) / 10


def test_strong_level_is_quieter_on_keyboard_like_clicks() -> None:
    rng = np.random.default_rng(1)
    clicks = np.zeros(48000 * 2, np.float32)
    for i in range(0, len(clicks), 4800):
        clicks[i:i + 200] = rng.normal(0, 0.3, 200)
    results = {}
    for level in (DenoiseLevel.STANDARD, DenoiseLevel.STRONG):
        d = Denoiser(level)
        out = np.concatenate([d.process(clicks[i:i + 480, None]) for i in range(0, len(clicks), 480)])
        results[level] = _rms(out[48000:, 0])
    assert results[DenoiseLevel.STRONG] <= results[DenoiseLevel.STANDARD]
    assert results[DenoiseLevel.STANDARD] < _rms(clicks) / 10


def test_level_labels_are_unique() -> None:
    labels = [lvl.label for lvl in DenoiseLevel]
    assert len(set(labels)) == len(labels)


def test_voice_gate_silences_non_speech_and_keeps_speech() -> None:
    from soundboard.denoise import VoiceGate

    gate = VoiceGate(open_threshold=0.6, hold_frames=3, release_frames=2)
    frame = np.ones(480, np.float32)
    outs = [gate.process(frame, p) for p in (0.1, 0.1, 0.9, 0.9, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1)]
    levels = [float(o.max()) for o in outs]
    assert levels[0] == 0.0 and levels[1] == 0.0       # 鍵盤聲（低人聲機率）完全靜音
    assert levels[2] == 1.0                             # 預讀：字頭前一個 frame 就打開
    assert levels[3] == 1.0 and levels[4] == 1.0        # 說話中
    assert all(v == 1.0 for v in levels[5:8])           # 保持：字尾不被切掉
    assert 0.0 < float(outs[8].min()) or levels[8] < 1.0  # 開始淡出
    assert levels[-1] == 0.0                            # 最後回到靜音
    assert not gate.is_open
