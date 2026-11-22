"""進入 Discord 語音偵測的測試。"""

from soundboard.discord_voice import VoiceJoinDetector, discord_capturing


def test_first_reading_never_triggers() -> None:
    # 開音效版時本來就在語音中，不應該突然播放
    d = VoiceJoinDetector()
    assert d.update(True, 0.0) is False
    assert d.in_voice is True


def test_triggers_only_on_join_edge() -> None:
    d = VoiceJoinDetector(cooldown=0)
    assert d.update(False, 0.0) is False
    assert d.update(True, 1.0) is True      # 進入語音
    assert d.update(True, 2.0) is False     # 持續通話中
    assert d.update(False, 3.0) is False    # 離開
    assert d.update(True, 4.0) is True      # 再次進入


def test_cooldown_blocks_quick_reconnect() -> None:
    d = VoiceJoinDetector(cooldown=10)
    d.update(False, 0.0)
    assert d.update(True, 1.0) is True
    d.update(False, 2.0)
    assert d.update(True, 3.0) is False     # Discord 斷線重連，不要連播
    d.update(False, 20.0)
    assert d.update(True, 21.0) is True


def test_unknown_reading_is_ignored() -> None:
    d = VoiceJoinDetector(cooldown=0)
    d.update(False, 0.0)
    assert d.update(None, 1.0) is False
    assert d.in_voice is False
    assert d.update(True, 2.0) is True


def test_session_query_returns_bool_or_none() -> None:
    assert discord_capturing() in (True, False, None)


def test_switching_channels_quickly_plays_again() -> None:
    """實際紀錄：進房間 7 秒後直接切到朋友的房間（中間離開約 1 秒），第二次也要播放。"""
    d = VoiceJoinDetector()
    d.update(False, 0.0)
    assert d.update(True, 58.0) is True
    d.update(False, 65.0)
    assert d.update(True, 66.0) is True
    d.update(False, 67.0)                      # 1 秒內又閃斷：視為同一次連線
    assert d.update(True, 68.0) is False
