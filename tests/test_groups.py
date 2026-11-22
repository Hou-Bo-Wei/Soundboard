"""群組與重新命名（實際改檔名）的測試。"""

from pathlib import Path

import pytest

from soundboard.grouping import build_sections
from soundboard.library import RenameError, rename_sound_file, validate_file_stem
from soundboard.models import AppSettings, SoundItem


def _items(*keys: str) -> list[SoundItem]:
    return [SoundItem(key=k, path=Path(k), display_name=Path(k).stem) for k in keys]


def test_no_groups_gives_single_ungrouped_section() -> None:
    sections = build_sections(_items("a.ogg", "b.ogg"), AppSettings())
    assert len(sections) == 1
    assert sections[0].name is None
    assert [it.key for it in sections[0].items] == ["a.ogg", "b.ogg"]


def test_sections_follow_group_order_and_member_order() -> None:
    settings = AppSettings()
    settings.move_to_group(["c.ogg", "a.ogg"], "梗")
    settings.add_group("空的")
    sections = build_sections(_items("a.ogg", "b.ogg", "c.ogg"), settings)
    assert [s.name for s in sections] == ["梗", "空的", None]
    assert [it.key for it in sections[0].items] == ["c.ogg", "a.ogg"]
    assert [it.key for it in sections[2].items] == ["b.ogg"]


def test_search_hides_empty_sections_and_ignores_collapse() -> None:
    settings = AppSettings()
    settings.move_to_group(["apple.ogg"], "水果")
    settings.move_to_group(["boom.ogg"], "爆炸")
    settings.collapsed_groups = ["水果"]
    sections = build_sections(_items("apple.ogg", "boom.ogg", "cat.ogg"), settings, "APP")
    assert [s.name for s in sections] == ["水果"]
    assert not sections[0].collapsed
    assert sections[0].total == 1


def test_missing_files_stay_in_settings_but_not_shown() -> None:
    settings = AppSettings(sound_groups={"g": ["gone.ogg", "a.ogg"]})
    sections = build_sections(_items("a.ogg"), settings)
    assert [it.key for it in sections[0].items] == ["a.ogg"]
    assert settings.sound_groups["g"] == ["gone.ogg", "a.ogg"]


def test_move_rename_delete_and_reorder_groups() -> None:
    s = AppSettings()
    s.move_to_group(["a", "b"], "一")
    s.move_to_group(["b"], "二")
    assert s.group_of("a") == "一" and s.group_of("b") == "二"
    s.collapsed_groups = ["一"]
    s.rename_group("一", "壹")
    assert list(s.sound_groups) == ["壹", "二"] and s.collapsed_groups == ["壹"]
    with pytest.raises(ValueError):
        s.rename_group("壹", "二")
    s.move_group("二", -1)
    assert list(s.sound_groups) == ["二", "壹"]
    s.delete_group("壹")
    assert s.group_of("a") is None and s.collapsed_groups == []
    s.move_to_group(["b"], None)
    assert s.sound_groups == {"二": []}


def test_rename_sound_key_moves_every_setting() -> None:
    s = AppSettings(
        display_names={"old.ogg": "舊名"},
        sound_volumes={"old.ogg": 1.5},
        sound_hotkeys={"old.ogg": [3, 0x31]},
        overlay_keys=["x.ogg", "old.ogg", "y.ogg"],
        sound_groups={"g": ["old.ogg", "z.ogg"]},
    )
    s.rename_sound_key("old.ogg", "new.ogg")
    assert s.display_names == {}
    assert s.sound_volumes == {"new.ogg": 1.5}
    assert s.sound_hotkeys == {"new.ogg": [3, 0x31]}
    assert s.overlay_keys == ["x.ogg", "new.ogg", "y.ogg"]
    assert s.sound_groups == {"g": ["new.ogg", "z.ogg"]}


@pytest.mark.parametrize("name", ["a/b", "a\\b", "what?", "CON", "com1", "trailing.", "trailing ", "", "  "])
def test_invalid_file_names_rejected(name: str) -> None:
    assert validate_file_stem(name) is not None


def test_valid_file_names_accepted() -> None:
    assert validate_file_stem("哇 好耶！(remix) v2.0") is None


def test_rename_sound_file_renames_on_disk(tmp_path: Path) -> None:
    src = tmp_path / "old.ogg"
    src.write_bytes(b"x")
    new = rename_sound_file(src, "新名字")
    assert new == tmp_path / "新名字.ogg"
    assert new.read_bytes() == b"x" and not src.exists()


def test_rename_sound_file_case_only(tmp_path: Path) -> None:
    src = tmp_path / "boom.mp3"
    src.write_bytes(b"x")
    new = rename_sound_file(src, "BOOM")
    assert [p.name for p in tmp_path.iterdir()] == ["BOOM.mp3"]
    assert new.name == "BOOM.mp3"


def test_rename_sound_file_refuses_overwrite(tmp_path: Path) -> None:
    (tmp_path / "a.ogg").write_bytes(b"a")
    (tmp_path / "b.ogg").write_bytes(b"b")
    with pytest.raises(RenameError):
        rename_sound_file(tmp_path / "a.ogg", "b")
    assert (tmp_path / "b.ogg").read_bytes() == b"b"


def test_move_to_group_inserts_before_target() -> None:
    s = AppSettings(sound_groups={"g": ["a", "b", "c"], "h": ["x"]})
    s.move_to_group(["x"], "g", before="b")
    assert s.sound_groups == {"g": ["a", "x", "b", "c"], "h": []}
    s.move_to_group(["c"], "g", before="a")  # 同群組內重新排序
    assert s.sound_groups["g"] == ["c", "a", "x", "b"]
    s.move_to_group(["a"], "g", before="missing")
    assert s.sound_groups["g"] == ["c", "x", "b", "a"]


def test_cycle_overlay_group_wraps_through_pinned() -> None:
    s = AppSettings(sound_groups={"一": [], "二": []})
    assert [s.cycle_overlay_group(1) for _ in range(4)] == ["一", "二", None, "一"]
    assert s.cycle_overlay_group(-1) is None
    s.overlay_group = "不存在"
    assert s.cycle_overlay_group(1) == "一"


def test_group_rename_and_delete_update_overlay_group() -> None:
    s = AppSettings(sound_groups={"一": []}, overlay_group="一")
    s.rename_group("一", "壹")
    assert s.overlay_group == "壹"
    s.delete_group("壹")
    assert s.overlay_group is None


def test_rename_sound_key_moves_join_sound() -> None:
    s = AppSettings(join_sound_key="old.ogg")
    s.rename_sound_key("old.ogg", "new.ogg")
    assert s.join_sound_key == "new.ogg"


def test_move_group_before_reorders_groups() -> None:
    from soundboard.models import AppSettings

    s = AppSettings(sound_groups={"A": [], "B": ["x"], "C": []})
    assert s.move_group_before("C", "A")
    assert list(s.sound_groups) == ["C", "A", "B"]
    assert s.move_group_before("C", None)
    assert list(s.sound_groups) == ["A", "B", "C"]
    assert s.sound_groups["B"] == ["x"]
    # 放回原位、自己前面、不存在的群組：不改變
    assert not s.move_group_before("A", "B")
    assert not s.move_group_before("A", "A")
    assert not s.move_group_before("A", "nope")
    assert not s.move_group_before("nope", None)
    assert list(s.sound_groups) == ["A", "B", "C"]
