"""回放片段（多音軌存取、修剪、匯出、音量、混音、降噪）的測試。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from soundboard import clips
from soundboard.denoise import denoise_available
from soundboard.replay import MIC_SOURCE

SR = 48000


def _tone(seconds: float, amp: float, freq: float = 440.0) -> np.ndarray:
    t = np.arange(int(SR * seconds)) / SR
    return np.stack([amp * np.sin(2 * np.pi * freq * t)] * 2, axis=1).astype(np.float32)


# ---------------------------------------------------------------- 存取
def test_save_list_load_multitrack(tmp_path: Path) -> None:
    tracks = [("Discord.exe", _tone(3, 0.05)), (MIC_SOURCE, _tone(3, 0.3, 220))]
    path = clips.save_clip(tracks, tmp_path, datetime(2026, 11, 21, 22, 30, 5))
    assert path.is_dir() and path.name == "20261121-223005_Discord+我的麥克風"
    again = clips.save_clip(tracks, tmp_path, datetime(2026, 11, 21, 22, 30, 5))
    assert again.name.endswith("-2")
    assert not list(tmp_path.glob("*.tmp"))

    found = clips.list_clips(tmp_path)
    assert len(found) == 2
    info = found[0]
    assert info.sources == ["Discord.exe", MIC_SOURCE] and info.label == "Discord + 我的麥克風"
    assert abs(info.seconds - 3.0) < 0.01

    loaded = clips.load_clip(path)
    assert [s for s, _a in loaded] == ["Discord.exe", MIC_SOURCE]
    assert np.allclose(loaded[1][1], tracks[1][1], atol=1e-4)

    clips.delete_clip(path)
    assert not path.exists() and len(clips.list_clips(tmp_path)) == 1


def test_legacy_single_file_clip(tmp_path: Path) -> None:
    """上一版存的是單一 .flac：仍要能列出、讀取、刪除。"""
    legacy = tmp_path / "20261121-234218_Discord.flac"
    sf.write(str(legacy), _tone(2, 0.1), SR, format="FLAC", subtype="PCM_16")
    (tmp_path / "broken").mkdir()              # 沒有 clip.json 的資料夾要略過
    (info,) = clips.list_clips(tmp_path)
    assert info.sources == ["Discord"] and info.created == datetime(2026, 11, 21, 23, 42, 18)
    (source, audio), = clips.load_clip(legacy)
    assert source == "Discord" and audio.shape == (2 * SR, 2)
    clips.delete_clip(legacy)
    assert not legacy.exists()


def test_save_clip_requires_tracks(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        clips.save_clip([], tmp_path)


# ---------------------------------------------------------------- 修剪、淡入淡出、匯出
def test_trim_and_fade() -> None:
    audio = _tone(3, 0.5)
    part = clips.trim(audio, 1.0, 1.5)
    assert len(part) == SR // 2
    with pytest.raises(ValueError):
        clips.trim(audio, 2.0, 1.0)
    flat = np.full((SR, 2), 0.5, np.float32)
    faded = clips.fade_edges(flat, fade_in=0.1, fade_out=0.2)
    assert faded[0, 0] == 0 and faded[-1, 0] < 1e-3
    assert faded[SR // 2, 0] == pytest.approx(0.5)
    assert faded[int(SR * 0.05), 0] < 0.5      # 淡入 0.1 秒的中間還沒到全音量
    assert faded[int(SR * 0.15), 0] == pytest.approx(0.5)


@pytest.mark.parametrize("suffix", [".ogg", ".mp3", ".wav", ".flac"])
def test_export_formats(tmp_path: Path, suffix: str) -> None:
    audio = (0.3 * np.random.default_rng(0).standard_normal((SR, 2))).astype(np.float32)
    out = clips.export_audio(audio, tmp_path / f"clip{suffix}")
    data, rate = sf.read(str(out))
    assert rate == SR and abs(len(data) - SR) < 2000


def test_export_rejects_unknown(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        clips.export_audio(np.zeros((10, 2), np.float32), tmp_path / "x.aac")


def test_unique_sound_path(tmp_path: Path) -> None:
    (tmp_path / "笑聲.ogg").write_bytes(b"x")
    assert clips.unique_sound_path(tmp_path, "笑聲").name == "笑聲 (2).ogg"
    assert clips.unique_sound_path(tmp_path, "新的").name == "新的.ogg"


def test_waveform_peaks() -> None:
    audio = np.zeros((1000, 2), np.float32)
    audio[500] = [0.8, -0.9]
    peaks = clips.waveform_peaks(audio, 10)
    assert len(peaks) == 10 and abs(peaks[5] - 0.9) < 1e-6 and peaks[0] == 0
    assert len(clips.waveform_peaks(audio[:3], 10)) == 10


# ---------------------------------------------------------------- 音量與混音
def test_auto_gain_boosts_quiet_speech() -> None:
    """實際錄到的 Discord 片段：說話時均方根約 -45 dBFS，自動放大要把它拉到一般音量。"""
    quiet = _tone(2, 10 ** (-42 / 20))         # 正弦波均方根比峰值低 3 dB → 約 -45 dBFS
    gain = clips.auto_gain_db(quiet)
    assert 20 < gain <= clips.MAX_BOOST_DB and gain * 2 == int(gain * 2)
    boosted = clips.mix([quiet], [gain])
    assert -20 < clips.loudness_db(boosted) < -12
    assert np.abs(boosted).max() < 1.0


def test_auto_gain_limits_by_peak_and_silence() -> None:
    assert clips.auto_gain_db(np.zeros((SR, 2), np.float32)) == 0.0
    spiky = _tone(2, 0.001)
    spiky[1000] = 0.9                          # 一聲很大的爆音：不能為了它完全不放大，也不能放大到爆
    # 以 0.5 dB 為單位四捨五入，最多超過 0.25 dB
    assert clips.auto_gain_db(spiky) <= clips.AUTO_PEAK_OVER_DB - clips.peak_db(spiky) + 0.25
    loud = _tone(1, 0.99)
    assert clips.auto_gain_db(loud) < 0          # 本來就很大聲時會調小


def test_mix_respects_gains_and_never_clips() -> None:
    a, b = _tone(1, 0.4), _tone(1, 0.4, 660)
    only_a = clips.mix([a, b], [0.0, -240.0])
    assert np.allclose(only_a, a, atol=1e-6)
    loud = clips.mix([a, b], [12.0, 12.0])
    assert np.abs(loud).max() <= clips.LIMITER_CEILING + 1e-6
    # 只有超過即時限幅門檻的 15 倍（12.0）才算「壓得很重」：0.4 放大 10 倍（+20 dB）還不算，放大 31.6 倍（+30 dB）才算
    assert clips.limited_ratio(a, 20.0) == 0 and clips.limited_ratio(a, 30.0) > 0
    assert clips.limited_ratio(a, 0.0) == 0
    assert clips.mix([], []).shape == (0, 2)


def test_peak_limit_only_touches_peaks() -> None:
    quiet = _tone(2, 0.3)
    spiky = quiet.copy()
    spiky[SR:SR + 50] *= 8                     # 第 1 秒有一小段爆音（峰值 2.4）
    out = clips.peak_limit(spiky)
    assert np.abs(out).max() <= clips.LIMITER_CEILING + 1e-6
    # 離爆音 0.1 秒以外完全不變；沒超過的音訊直接回傳
    far = int(0.1 * SR)
    assert np.array_equal(out[: SR - far], spiky[: SR - far])
    assert np.array_equal(out[SR + far:], spiky[SR + far:])
    assert clips.peak_limit(quiet) is quiet
    # 增益是平滑變化的：相鄰取樣的增益差很小（不像硬截斷會破音）
    gain = np.abs(out[:, 0]) / np.maximum(np.abs(spiky[:, 0]), 1e-6)
    mask = np.abs(spiky[:, 0]) > 0.05
    assert np.abs(np.diff(gain[mask])).max() < 0.05


def test_auto_mix_balances_tracks() -> None:
    quiet, normal = _tone(1, 0.01), _tone(1, 0.2, 330)
    out = clips.auto_mix([("Discord.exe", quiet), (MIC_SOURCE, normal)])
    assert out.shape == quiet.shape and np.abs(out).max() < 1.0


@pytest.mark.skipif(not denoise_available(), reason="RNNoise 無法載入")
def test_denoise_stereo_shape() -> None:
    noisy = (np.random.default_rng(1).standard_normal((SR, 2)) * 0.05).astype(np.float32)
    out = clips.denoise_stereo(noisy)
    assert out.shape == noisy.shape
    assert np.sqrt(np.mean(out ** 2)) < np.sqrt(np.mean(noisy ** 2))   # 純噪音要被壓低


def test_export_long_clip(tmp_path: Path) -> None:
    """libsndfile 一次寫入超過約 30 秒的 Vorbis／MP3 會讓程式崩潰；分段寫入後 3 分鐘的片段也能存。"""
    audio = clips.peak_limit((np.random.default_rng(0).standard_normal((SR * 180, 2)) * 0.3).astype(np.float32))
    for suffix in (".ogg", ".mp3"):
        path = clips.export_audio(audio, tmp_path / f"long{suffix}")
        assert abs(sf.info(str(path)).duration - 180) < 0.2
    assert not list(tmp_path.glob(".*.tmp*"))     # 暫存檔有清掉


def test_volume_to_db_roundtrip() -> None:
    assert clips.volume_to_db(1.0) == 0.0
    assert clips.volume_to_db(2.0) == 6.0                  # 舊版的 200% 約等於 +6 dB
    assert clips.volume_to_db(0.0) == clips.SOUND_MIN_DB   # 舊版的 0%
    assert clips.volume_to_db(1e9) == clips.SOUND_MAX_DB
    for db in (-80.0, -12.5, 0.5, 37.0, 80.0):
        assert clips.volume_to_db(clips.gain_factor(db)) == db


def test_render_gain_boosts_without_clipping() -> None:
    quiet = _tone(1, 0.01)
    assert clips.render_gain(quiet, 1.0) is quiet
    louder = clips.render_gain(quiet, clips.gain_factor(20))
    assert np.allclose(louder, quiet * 10, atol=1e-6)      # 沒超過滿刻度：完全照倍率放大
    hot = clips.render_gain(quiet, clips.gain_factor(80))  # +80 dB：峰值壓在上限內，不會爆音
    assert np.abs(hot).max() <= clips.LIMITER_CEILING + 1e-6
    assert np.abs(quiet).max() == pytest.approx(0.01)      # 原陣列不會被改到
