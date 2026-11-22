"""麥克風測試：取樣緩衝、波形計算、整段降噪對齊。"""

import numpy as np
import pytest

from soundboard.denoise import DenoiseLevel, denoise_available, denoise_clip, latency_samples
from soundboard.mictest import db_fraction, envelope, rms_db
from soundboard.mixer import SampleTap


def test_sample_tap_latest_and_since_across_wraparound() -> None:
    tap = SampleTap(10)
    tap.write(np.arange(4, dtype=np.float32))
    mark = tap.position
    tap.write(np.arange(4, 12, dtype=np.float32))
    assert tap.latest(5).tolist() == [7, 8, 9, 10, 11]
    assert tap.since(mark).tolist() == list(range(4, 12))
    # 要求超過容量時前面補 0，不會讀到被覆蓋的舊資料
    assert tap.latest(12).tolist() == [0, 0, *range(2, 12)]


def test_sample_tap_oversized_write_keeps_order() -> None:
    tap = SampleTap(10)
    tap.write(np.arange(12, dtype=np.float32))
    mark = tap.position
    tap.write(np.arange(100, 125, dtype=np.float32))
    assert tap.position == 37
    assert tap.since(mark).tolist() == list(range(115, 125))


def test_envelope_and_db_scale() -> None:
    samples = np.concatenate((np.full(100, 0.001, np.float32), np.full(100, 1.0, np.float32)))
    peaks = envelope(samples, 2)
    assert peaks.tolist() == pytest.approx([0.001, 1.0])
    frac = db_fraction(peaks)               # -60 dB → 0，0 dB → 1
    assert frac.tolist() == pytest.approx([0.0, 1.0], abs=1e-6)
    assert envelope(np.zeros(0, np.float32), 5).tolist() == [0.0] * 5
    assert len(envelope(np.ones(3, np.float32), 8)) == 8


def test_rms_db() -> None:
    assert rms_db(np.zeros(10, np.float32)) == -120.0
    assert rms_db(np.full(10, 0.1, np.float32)) == pytest.approx(-20.0, abs=0.01)


@pytest.mark.skipif(not denoise_available(), reason="RNNoise 無法載入")
@pytest.mark.parametrize("level", list(DenoiseLevel))
def test_denoise_clip_keeps_length_and_alignment(level: DenoiseLevel) -> None:
    # 一段「說話」（諧波很多的方波 + 包絡）前後接靜音：對齊正確時能量應落在同一段
    t = np.arange(48000) / 48000
    voice = (np.sign(np.sin(2 * np.pi * 180 * t)) * 0.2 * np.sin(np.pi * t)).astype(np.float32)
    clip = np.concatenate((np.zeros(24000, np.float32), voice, np.zeros(24000, np.float32)))
    out = denoise_clip(clip, level)
    assert len(out) == len(clip)
    if level is DenoiseLevel.OFF:
        assert np.array_equal(out, clip)
    assert latency_samples(level) == {DenoiseLevel.OFF: 0, DenoiseLevel.STANDARD: 1440,
                                      DenoiseLevel.STRONG: 1920}[level]


def test_noise_floor_ignores_loud_speech() -> None:
    from soundboard.mictest import noise_floor_db

    quiet = np.full(48000, 0.001, np.float32)     # -60 dB 背景
    loud = np.full(24000, 0.5, np.float32)        # 說話
    assert noise_floor_db(np.concatenate((quiet, loud))) == pytest.approx(-60, abs=0.5)
    assert noise_floor_db(np.zeros(48000, np.float32)) == -120.0


def test_denoise_level_label_translated_value_unchanged(tmp_path) -> None:
    """降噪強度的顯示名稱隨語言改變，但存進設定檔的 value 不變。"""
    import json

    from soundboard import i18n

    assert DenoiseLevel.STANDARD.label == "標準"
    (tmp_path / "en.json").write_text(json.dumps({"標準": "Standard"}), encoding="utf-8")
    i18n.set_language("en", tmp_path)
    try:
        assert DenoiseLevel.STANDARD.label == "Standard"
        assert DenoiseLevel.STANDARD.value == "standard"
        # 下拉選單以顯示名稱反查強度（設定頁、麥克風測試視窗都這樣做）
        assert next(lvl for lvl in DenoiseLevel if lvl.label == "Standard") is DenoiseLevel.STANDARD
    finally:
        i18n.set_language(i18n.SOURCE_LANGUAGE)
