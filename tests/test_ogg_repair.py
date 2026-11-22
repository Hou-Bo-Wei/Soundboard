"""Ogg Opus 時間戳記修復的測試。"""

from __future__ import annotations

import struct
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from soundboard import decoder
from soundboard.ogg_repair import is_ogg_opus, ogg_crc, opus_packet_samples, repair_opus_granules


def _pages(data: bytes) -> list[int]:
    """回傳每一頁的起始位置。"""
    starts, pos = [], 0
    while pos < len(data):
        starts.append(pos)
        n = data[pos + 26]
        pos += 27 + n + sum(data[pos + 27:pos + 27 + n])
    return starts


def _set_granule(data: bytearray, page_pos: int, value: int) -> None:
    """改寫某頁的時間戳記並重算 CRC（模擬有捨入誤差的轉檔工具）。"""
    n = data[page_pos + 26]
    end = page_pos + 27 + n + sum(data[page_pos + 27:page_pos + 27 + n])
    struct.pack_into("<q", data, page_pos + 6, value)
    struct.pack_into("<I", data, page_pos + 22, 0)
    struct.pack_into("<I", data, page_pos + 22, ogg_crc(data[page_pos:end]))


@pytest.fixture()
def opus_file(tmp_path: Path) -> Path:
    path = tmp_path / "good.ogg"
    t = np.arange(48000 * 2) / 48000
    tone = (0.3 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    sf.write(str(path), np.stack([tone, tone], axis=1), 48000, format="OGG", subtype="OPUS")
    return path


@pytest.fixture()
def broken_file(opus_file: Path, tmp_path: Path) -> Path:
    data = bytearray(opus_file.read_bytes())
    starts = _pages(bytes(data))
    assert len(starts) > 4, "測試檔應有多個音訊頁"
    middle = starts[len(starts) // 2]
    (granule,) = struct.unpack_from("<q", data, middle + 6)
    _set_granule(data, middle, granule - 1)  # 與實際檔案相同：少 1 個取樣
    path = tmp_path / "broken.ogg"
    path.write_bytes(bytes(data))
    return path


def test_crc_matches_libogg(opus_file: Path) -> None:
    data = opus_file.read_bytes()
    for pos in _pages(data):
        n = data[pos + 26]
        end = pos + 27 + n + sum(data[pos + 27:pos + 27 + n])
        page = bytearray(data[pos:end])
        (stored,) = struct.unpack_from("<I", page, 22)
        page[22:26] = b"\0\0\0\0"
        assert ogg_crc(page) == stored


def test_packet_samples() -> None:
    assert opus_packet_samples(b"") == 0
    assert opus_packet_samples(bytes([0xFC])) == 960        # CELT 20 ms、單一音框
    assert opus_packet_samples(bytes([0xFD])) == 1920       # 兩個 20 ms 音框
    assert opus_packet_samples(bytes([0x1B, 0x03])) == 3 * 2880  # SILK 60 ms（config 3）×3


def test_good_file_untouched(opus_file: Path) -> None:
    data = opus_file.read_bytes()
    assert is_ogg_opus(data)
    repaired, fixed = repair_opus_granules(data)
    assert fixed == 0 and repaired == data


def test_broken_file_rejected_by_libsndfile(broken_file: Path) -> None:
    with pytest.raises((sf.LibsndfileError, RuntimeError)):
        sf.read(str(broken_file))


def test_decode_repairs_without_ffmpeg(broken_file: Path, opus_file: Path,
                                       monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(decoder, "find_ffmpeg", lambda: None)
    expected = decoder.decode_file(opus_file)
    got = decoder.decode_file(broken_file)
    assert got.shape == expected.shape
    assert np.allclose(got, expected, atol=1e-4)
    assert broken_file.read_bytes() != opus_file.read_bytes(), "不可改動使用者的原始檔"


def test_non_opus_still_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(decoder, "find_ffmpeg", lambda: None)
    bad = tmp_path / "bad.ogg"
    bad.write_bytes(b"not an ogg file at all" * 10)
    with pytest.raises(decoder.DecodeError):
        decoder.decode_file(bad)
