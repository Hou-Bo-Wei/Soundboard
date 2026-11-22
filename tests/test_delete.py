"""刪除音效：設定清理、快取移除的單元測試（不碰資源回收筒）。"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from soundboard.library import ClipCache, move_to_recycle_bin
from soundboard.models import AppSettings


def test_forget_sound_key_removes_everything():
    s = AppSettings(display_names={"a.ogg": "A", "b.ogg": "B"}, sound_volumes={"a.ogg": 2.0},
                    sound_hotkeys={"a.ogg": [1, 2]}, overlay_keys=["b.ogg", "a.ogg"],
                    sound_groups={"搞笑": ["a.ogg", "b.ogg"], "空": []}, join_sound_key="a.ogg")
    s.forget_sound_key("a.ogg")
    assert s.display_names == {"b.ogg": "B"} and s.sound_volumes == {} and s.sound_hotkeys == {}
    assert s.overlay_keys == ["b.ogg"] and s.sound_groups == {"搞笑": ["b.ogg"], "空": []}
    assert s.join_sound_key is None


def test_forget_keeps_other_join_sound():
    s = AppSettings(join_sound_key="b.ogg")
    s.forget_sound_key("a.ogg")
    assert s.join_sound_key == "b.ogg"


def test_cache_forget(monkeypatch):
    cache = ClipCache()
    clip = np.zeros((10, 2), np.float32)
    monkeypatch.setattr("soundboard.library.decode_file", lambda _p: clip)
    path = Path("x.ogg")
    cache.get(path)
    cache.forget(path)
    assert cache.peek(path) is None and cache._bytes == 0
    cache.forget(path)      # 不存在也不出錯


def test_recycle_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        move_to_recycle_bin(tmp_path / "沒有這個檔案.ogg")
