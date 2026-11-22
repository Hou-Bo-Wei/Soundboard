"""即時回放錄音（環形緩衝、多來源錄音器、時間對齊）的測試。"""

from __future__ import annotations

import numpy as np
import pytest

from soundboard.process_capture import CaptureError, root_pids
from soundboard.replay import MIC_SOURCE, REPLAY_SECONDS_CHOICES, ReplayRecorder, RingBuffer, source_label

SR = 48000


def _pcm(n: int, value: int) -> np.ndarray:
    return np.full((n, 2), value, dtype=np.int16)


# ---------------------------------------------------------------- RingBuffer
def test_ring_keeps_latest() -> None:
    ring = RingBuffer(10)
    t = 0.0
    for v in range(1, 6):          # 5 段 × 3 音框 = 15 > 容量 10
        t += 3 / SR
        ring.write(_pcm(3, v), now=t)
    out = ring.latest(100)
    assert len(out) == 10 and ring.filled == 10
    assert out[:, 0].tolist() == [2, 3, 3, 3, 4, 4, 4, 5, 5, 5]


def test_ring_wraps_exactly() -> None:
    ring = RingBuffer(8)
    data = np.arange(20, dtype=np.int16).repeat(2).reshape(-1, 2)
    t = 0.0
    for i in range(0, 20, 3):
        chunk = data[i:i + 3]
        t += len(chunk) / SR
        ring.write(chunk, now=t)
    assert ring.latest(8)[:, 0].tolist() == list(range(12, 20))
    assert ring.latest(3)[:, 0].tolist() == [17, 18, 19]


def test_ring_big_write() -> None:
    ring = RingBuffer(4)
    ring.write(np.arange(10, dtype=np.int16).repeat(2).reshape(-1, 2), now=1.0)
    assert ring.latest(4)[:, 0].tolist() == [6, 7, 8, 9]


def test_ring_fills_gap_with_silence() -> None:
    ring = RingBuffer(SR * 5)
    ring.write(_pcm(480, 100), now=1.0)
    ring.write(_pcm(480, 100), now=2.0)        # 中間斷了約 1 秒
    out = ring.latest(SR * 5)
    assert abs(len(out) - SR * 1.0) < 1000
    assert (out[480:-480] == 0).all()


def test_ring_burst_is_not_a_gap() -> None:
    """執行緒延遲後封包一次湧入，不應被當成斷線而多補靜音。"""
    ring = RingBuffer(SR * 5)
    t = 1.0
    for _ in range(50):
        t += 0.01
        ring.write(_pcm(480, 1), now=t)
    t += 0.2                                   # 延遲 200 ms
    for _ in range(20):                        # 補上那 200 ms 的封包，幾乎同時到達
        ring.write(_pcm(480, 1), now=t)
    assert (ring.latest(SR * 5) == 1).all()


def test_ring_write_float_mono_to_stereo() -> None:
    ring = RingBuffer(100)
    ring.write_float(np.full((10, 1), 0.5, dtype=np.float32))
    out = ring.latest(10)
    assert out.shape == (10, 2) and (out == 16383).all()


# ---------------------------------------------------------------- ReplayRecorder
class FakeCapture:
    """假的擷取物件：測試直接寫入對應的緩衝區。"""

    instances: list[FakeCapture] = []

    def __init__(self, pid: int, on_audio, on_stopped=None) -> None:  # noqa: ANN001
        self.pid = pid
        self.ring = on_audio.__self__            # on_audio 就是緩衝區的 write
        self.running = False
        FakeCapture.instances.append(self)

    def start(self) -> None:
        if self.pid == 666:
            raise CaptureError("拒絕存取")
        self.running = True

    def stop(self) -> None:
        self.running = False


PROCS = {
    10: ("explorer.exe", 1),
    100: ("Discord.exe", 10), 101: ("Discord.exe", 100), 102: ("Discord.exe", 100),
    200: ("chrome.exe", 10), 201: ("chrome.exe", 200),
    666: ("locked.exe", 10),
}


class FakeMic:
    """模擬音訊引擎的麥克風：記住錄音器裝上的接收函式。"""

    def __init__(self) -> None:
        self.sink = None

    def hook(self, sink) -> None:  # noqa: ANN001
        self.sink = sink


@pytest.fixture(autouse=True)
def _reset_fakes() -> None:
    FakeCapture.instances.clear()


def test_root_pids_picks_top_process() -> None:
    assert root_pids("discord.exe", PROCS) == [100]
    assert root_pids("chrome.exe", PROCS) == [200]
    assert root_pids("missing.exe", PROCS) == []


def test_source_label() -> None:
    assert source_label("Discord.exe") == "Discord"
    assert source_label(MIC_SOURCE) == "我的麥克風"
    assert 180 in REPLAY_SECONDS_CHOICES


def test_source_label_translated_but_source_code_unchanged(tmp_path) -> None:  # noqa: ANN001
    import json

    from soundboard import i18n
    from soundboard.gui.replay_page import seconds_label

    (tmp_path / "en.json").write_text(json.dumps({"我的麥克風": "My microphone", "{minutes} 分鐘": "{minutes} min"}),
                                      encoding="utf-8")
    try:
        i18n.set_language("en", tmp_path)
        assert source_label(MIC_SOURCE) == "My microphone"
        assert seconds_label(180) == "3 min" and seconds_label(90) == "1.5 min"
        assert MIC_SOURCE == "@mic"   # 設定檔裡存的代號不受語言影響
    finally:
        i18n.set_language(i18n.SOURCE_LANGUAGE)


def test_recorder_attaches_and_reports_status() -> None:
    rec = ReplayRecorder(10, ["Discord.exe", "obs64.exe", "locked.exe"], FakeCapture, lambda: PROCS)
    rec.refresh()
    assert [c.pid for c in FakeCapture.instances] == [100, 666]
    states = {s.source: s.state for s in rec.status()}
    assert states == {"Discord.exe": "recording", "obs64.exe": "waiting", "locked.exe": "error"}
    rec.refresh()                              # 已連上的不重複連、失敗的不一直重試
    assert len(FakeCapture.instances) == 2
    rec.stop()
    assert not FakeCapture.instances[0].running


def test_snapshot_tracks_are_separate_and_aligned() -> None:
    mic = FakeMic()
    rec = ReplayRecorder(2, [MIC_SOURCE, "Discord.exe", "chrome.exe"], FakeCapture, lambda: PROCS, mic.hook)
    rec._sync_mic()                            # 不啟動背景監看執行緒，手動連上各來源
    rec.refresh()
    discord, chrome = (c.ring for c in FakeCapture.instances)
    mic_ring = mic.sink.__self__
    t = 100.0
    for i in range(100):                       # 1 秒：Discord 有聲音、chrome 靜音；麥克風只有後半秒
        t += 0.01
        discord.write(_pcm(480, 1000), now=t)
        chrome.write(_pcm(480, 0), now=t)
        if i >= 50:
            mic_ring.write(_pcm(480, 2000), now=t)
    tracks = rec.snapshot_tracks(now=t)
    assert [s for s, _a in tracks] == [MIC_SOURCE, "Discord.exe", "chrome.exe"]
    assert all(len(a) == SR for _s, a in tracks)
    mic_audio = tracks[0][1]
    assert np.allclose(mic_audio[: SR // 2, 0], 0) and np.allclose(mic_audio[SR // 2:, 0], 2000 / 32768)
    assert np.allclose(tracks[1][1][:, 0], 1000 / 32768)
    mixed = rec.snapshot(now=t)
    assert np.allclose(mixed[-1, 0], 3000 / 32768)
    rec.stop()
    assert mic.sink is None                    # 停止後要把接收函式拔掉


def test_mic_status_follows_data() -> None:
    mic = FakeMic()
    rec = ReplayRecorder(5, [MIC_SOURCE], FakeCapture, lambda: {}, mic.hook)
    rec._sync_mic()
    assert rec.status()[0].state == "waiting"  # 設定頁沒選麥克風時不會有資料
    mic.sink(np.zeros((480, 1), np.float32))
    assert rec.status()[0].state == "recording"
    rec.configure(5, ["Discord.exe"])
    assert mic.sink is None and [s.source for s in rec.status()] == ["Discord.exe"]


def test_mic_without_hook_is_error() -> None:
    rec = ReplayRecorder(5, [MIC_SOURCE], FakeCapture, lambda: {})
    assert rec.status()[0].state == "error"


def test_snapshot_keeps_closed_app_at_correct_time() -> None:
    rec = ReplayRecorder(3, ["Discord.exe"], FakeCapture, lambda: PROCS)
    rec.refresh()
    ring = FakeCapture.instances[0].ring
    t = 50.0
    for _ in range(100):
        t += 0.01
        ring.write(_pcm(480, 500), now=t)
    (source, audio), = rec.snapshot_tracks(now=t + 1.0)   # 程式 1 秒前就沒有再送聲音
    assert source == "Discord.exe" and len(audio) == 2 * SR
    assert np.allclose(audio[:SR, 0], 500 / 32768)
    assert np.allclose(audio[SR:, 0], 0)


def test_snapshot_empty() -> None:
    rec = ReplayRecorder(5, ["Discord.exe"], FakeCapture, lambda: {})
    rec.refresh()
    assert rec.snapshot_tracks() == [] and rec.snapshot().shape == (0, 2)


def test_configure_sources_keeps_other_buffers() -> None:
    rec = ReplayRecorder(5, ["Discord.exe", "chrome.exe"], FakeCapture, lambda: PROCS)
    rec.refresh()
    rec.configure(5, ["Discord.exe"])
    assert [s.source for s in rec.status()] == ["Discord.exe"]
    assert FakeCapture.instances[0].running and not FakeCapture.instances[1].running


def test_time_format_helpers() -> None:
    from soundboard.gui.clip_editor import format_gain, format_seconds, parse_seconds
    from soundboard.gui.replay_page import seconds_label

    assert format_seconds(65.25) == "1:05.25"
    assert format_seconds(-3) == "0:00.00"
    assert parse_seconds("1:05.25") == pytest.approx(65.25)
    assert parse_seconds(" 12.5 ") == pytest.approx(12.5)
    with pytest.raises(ValueError):
        parse_seconds("abc")
    assert [seconds_label(s) for s in REPLAY_SECONDS_CHOICES] == [
        "30 秒", "1 分鐘", "2 分鐘", "3 分鐘", "5 分鐘", "10 分鐘"]
    assert format_gain(0) == "+0.0 dB（×1.0）" and format_gain(20) == "+20.0 dB（×10）"
