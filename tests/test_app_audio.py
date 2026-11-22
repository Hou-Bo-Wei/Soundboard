"""混音器：程式聲音來源、路由、引擎混音、通道顯示邏輯的單元測試（不開任何音訊裝置、不擷取真的程式）。"""

from __future__ import annotations

import time

import numpy as np

from soundboard.app_audio import AppAudioRouter, is_blocked
from soundboard.engine import AudioEngine
from soundboard.gui.channel_strip import fraction_to_value, meter_color, meter_fraction, value_to_fraction
from soundboard.gui.mixer_page import app_icon, app_title, route_status_text
from soundboard.mixer import LiveSource, Voice
from soundboard.models import AppSettings
from soundboard.process_capture import CaptureError
from soundboard.theme import ERR, OK


def pcm(value: int, frames: int = 480) -> np.ndarray:
    return np.full((frames, 2), value, dtype=np.int16)


def test_live_source_prebuffers_then_plays():
    src = LiveSource(prebuffer=0.02)          # 960 音框
    out = np.zeros((480, 2), np.float32)
    src.write_pcm(pcm(16384))
    src.mix_into(out)
    assert not out.any(), "還在累積，不輸出"
    src.write_pcm(pcm(16384, 1440))
    src.mix_into(out)
    assert np.allclose(out, 0.5)
    src.gain = 2.0
    out[:] = 0
    src.mix_into(out)
    assert np.allclose(out, 1.0)
    src.muted = True
    out[:] = 0
    src.mix_into(out)
    assert not out.any()
    assert src.level() == 0.0


def test_live_source_underrun_rebuffers():
    src = LiveSource(prebuffer=0.01)
    src.write_pcm(pcm(1000, 960))
    out = np.zeros((480, 2), np.float32)
    src.mix_into(out)
    src.mix_into(out)                          # 剛好讀完
    src.write_pcm(pcm(1000, 100))
    out[:] = 0
    src.mix_into(out)                          # 不足：先把剩下的 100 個播完，再回到累積狀態
    assert out[:100].any() and not out[100:].any()
    src.write_pcm(pcm(1000, 100))
    out[:] = 0
    src.mix_into(out)
    assert not out.any(), "累積中不輸出"


def test_live_source_latency_bounded():
    src = LiveSource(max_latency=0.05, prebuffer=0.0)
    for _ in range(50):
        src.write_pcm(pcm(1000))
    assert src._ring.size <= int(48000 * 0.05)


def test_level_and_stale():
    src = LiveSource()
    src.write_pcm(pcm(16384))
    src.gain = 0.5
    assert abs(src.level() - 0.25) < 1e-6
    assert src.level(now=time.monotonic() + 1) == 0.0


class FakeCapture:
    instances: list[FakeCapture] = []

    def __init__(self, pid, on_audio):  # noqa: ANN001
        self.pid, self.on_audio, self.running = pid, on_audio, False
        FakeCapture.instances.append(self)

    def start(self):  # noqa: ANN201
        if self.pid == 666:
            raise CaptureError("拒絕存取")
        self.running = True

    def stop(self):  # noqa: ANN201
        self.running = False


def make_router(procs: dict[int, tuple[str, int]]):  # noqa: ANN201
    published: list[dict] = []
    router = AppAudioRouter(published.append, capture_factory=FakeCapture, process_lister=lambda: procs,
                            is_supported=lambda: True)
    return router, published


def test_router_attaches_and_follows_restart():
    FakeCapture.instances.clear()
    procs = {10: ("chrome.exe", 1), 11: ("chrome.exe", 10), 20: ("spotify.exe", 1)}
    router, published = make_router(procs)
    router.apps = ["Chrome.exe"]
    router.refresh()
    assert [c.pid for c in FakeCapture.instances] == [10], "只接最上層的 chrome"
    assert list(published[-1]) == ["chrome.exe:10"]
    assert router.status("chrome.exe").state == "sending"
    router.set_gain("CHROME.EXE", 0.5)
    router.set_muted("chrome.exe", True)
    source = published[-1]["chrome.exe:10"]
    assert source.gain == 0.5 and source.muted
    # Chrome 關掉再開：舊的擷取結束、接上新的 PID，音量設定沿用
    FakeCapture.instances[0].running = False
    del procs[10], procs[11]
    procs[30] = ("chrome.exe", 1)
    router.refresh()
    assert list(published[-1]) == ["chrome.exe:30"]
    assert published[-1]["chrome.exe:30"].gain == 0.5
    router.stop()
    assert published[-1] == {}


def test_router_drops_exited_process_even_if_capture_still_running():
    FakeCapture.instances.clear()
    procs = {10: ("vlc.exe", 1)}
    router, published = make_router(procs)
    router.apps = ["vlc.exe"]
    router.refresh()
    capture = FakeCapture.instances[0]
    procs.clear()
    procs[10] = ("notepad.exe", 1)            # 同一個 PID 被別的程式拿去用
    router.refresh()
    assert not capture.running and published[-1] == {}
    assert router.status("vlc.exe").state == "waiting"


def test_router_error_not_retried_while_alive():
    FakeCapture.instances.clear()
    procs = {666: ("game.exe", 1)}
    router, published = make_router(procs)
    router.apps = ["game.exe"]
    router.refresh()
    router.refresh()
    assert len(FakeCapture.instances) == 1
    status = router.status("game.exe")
    assert status.state == "error" and "拒絕" in status.detail
    assert published == [] or published[-1] == {}
    assert router.status("other.exe").state == "waiting"


def test_router_rejects_discord():
    router, _ = make_router({})
    router.configure(["Discord.exe", "chrome.exe"], {}, set())
    assert router.apps == ["chrome.exe"]
    assert is_blocked("discordcanary.exe")
    router.stop()


def test_engine_mixes_apps_with_master_gain():
    engine = AudioEngine("Windows WASAPI")     # 沒有開任何裝置，只呼叫混音函式
    src = LiveSource(prebuffer=0.0)
    src.write_pcm(pcm(3277, 960))              # 約 0.1
    engine.live_sources = {"chrome.exe:1": src}
    engine.mic_bus.add(Voice("s", np.full((960, 2), 0.2, np.float32), 1.0))
    out = engine.render_friends(480)
    assert np.allclose(out, 0.3, atol=1e-3)
    assert abs(engine.levels["sounds"] - 0.2) < 1e-6 and abs(engine.levels["master"] - 0.3) < 1e-3
    engine.master_gain = 0.0
    assert not engine.render_friends(480).any()


def test_settings_defaults_backward_compatible():
    s = AppSettings.from_dict({"sound_dir": "x"})
    assert s.mixer_apps == [] and s.mixer_volumes == {} and s.mixer_muted == []


def test_fader_and_meter_math():
    assert value_to_fraction(100) == 0.5 and value_to_fraction(500) == 1.0 and value_to_fraction(-5) == 0.0
    assert fraction_to_value(0.5) == 100 and fraction_to_value(0.333) == 67
    assert meter_fraction(0) == 0 and meter_fraction(1.0) == 1.0 and 0.45 < meter_fraction(10 ** (-30 / 20)) < 0.55
    assert meter_color(1.0) == ERR and meter_color(0.01) == OK


def test_channel_texts():
    assert app_title("chrome.exe") == "Chrome" and app_title("PotPlayer64.exe") == "PotPlayer64"
    assert app_icon("msedge.exe") == "🌐" and app_icon("Spotify.exe") == "🎵" and app_icon("x.exe") == "🖥"
    assert route_status_text("sending")[1] == OK
    assert "拒絕" in route_status_text("error", "拒絕存取")[0]
    assert route_status_text("error")[0] == "⚠ 無法擷取"


def test_mixer_fixed_channel_titles() -> None:
    from soundboard.gui.mixer_page import FIXED_CHANNELS, MASTER, MIC, MONITOR, SOUNDS

    # 通道 key 會存進設定檔（mixer_muted 等），不可因介面文字改變
    assert [(key, f"{icon} {title}") for key, title, icon, _c in FIXED_CHANNELS] == [
        (MASTER, "🎚 總音量"), (SOUNDS, "🔊 音效"), (MIC, "🎙 麥克風"), (MONITOR, "🎧 監聽")]
    assert (MASTER, SOUNDS, MIC, MONITOR) == ("@master", "@sounds", "@mic", "@monitor")
