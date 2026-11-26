"""貼圖關鍵字工具（tools/sticker_keywords）：找系列、拼總覽圖、裝關鍵字檔、檢查進度。"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("PIL")
from PIL import Image  # noqa: E402

from soundboard import sticker_meta, stickers  # noqa: E402

TOOL = Path(__file__).resolve().parents[1] / "tools" / "sticker_keywords" / "sticker_keywords.py"


@pytest.fixture(scope="module")
def kw():
    spec = importlib.util.spec_from_file_location("sticker_keywords", TOOL)
    module = importlib.util.module_from_spec(spec)
    sys.modules["sticker_keywords"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def library(tmp_path: Path) -> Path:
    """仿 Downloads\\LINE貼圖：一組放在根目錄、一組放在作者資料夾底下，動態貼圖同時有 png 與 gif。"""
    root = tmp_path / "LINE貼圖"
    a = root / "早安組_111"
    b = root / "作者_999" / "動態組_222"
    for folder in (a, b):
        folder.mkdir(parents=True)
        (folder / ".line").mkdir()
    for stem in ("001", "002", "010"):
        Image.new("RGBA", (40, 40), (255, 0, 0, 255)).save(a / f"{stem}.png")
    Image.new("RGBA", (40, 40), (0, 255, 0, 255)).save(b / "001.png")
    Image.new("RGBA", (40, 40), (0, 255, 0, 255)).save(b / "001.gif")
    Image.new("RGBA", (40, 40)).save(b / "001_popup.png")       # 特效層不算貼圖
    Image.new("RGBA", (40, 40)).save(a / ".line" / "001_base.png")
    return root


def test_find_series(kw, library: Path) -> None:
    found = {s.group_id: s for s in kw.find_series(library)}
    assert set(found) == {"111", "222"}         # 作者資料夾本身沒有貼圖，不算一組
    assert list(found["111"].stickers) == ["001", "002", "010"]
    assert found["222"].stickers["001"].suffix == ".png"        # 同一張有 png 與 gif 時用靜態圖
    assert found["111"].title == "早安組"


def test_contact_sheet_pages(kw, library: Path, tmp_path: Path) -> None:
    series = next(s for s in kw.find_series(library) if s.group_id == "111")
    sheets = kw.contact_sheet(series, tmp_path / "out", cell=50, columns=2, per_sheet=2)
    assert [p.name for p in sheets] == ["111.png", "111_p2.png"]
    with Image.open(sheets[0]) as im:
        assert im.size == (100, 50)


def test_install_merges_and_app_reads_it(kw, library: Path, tmp_path: Path) -> None:
    src = tmp_path / "keywords"
    src.mkdir()
    (src / "111.json").write_text(json.dumps({
        "001": {"t": "早安~", "k": ["早安", "morning", "早安"]},
        "002": {"t": "", "k": ["睡覺"]},
    }, ensure_ascii=False), encoding="utf-8")
    (src / "333.json").write_text("{}", encoding="utf-8")
    target = library / "早安組_111" / sticker_meta.TAGS_FILENAME
    target.write_text(json.dumps({"010": {"t": "", "k": ["掰掰"]}, "002": {"t": "", "k": ["舊的"]}},
                                 ensure_ascii=False), encoding="utf-8")

    count, orphans = kw.install(library, src)
    assert (count, orphans) == (1, ["333.json"])
    data = json.loads(target.read_text(encoding="utf-8"))
    assert list(data) == ["001", "002", "010"]
    assert data["001"] == {"t": "早安~", "k": ["早安", "morning"]}       # 重複的關鍵字去掉
    assert data["002"]["k"] == ["睡覺"]                                   # 新檔案覆蓋同一張
    assert data["010"]["k"] == ["掰掰"]                                   # 新檔案沒有的保留

    items = {it.key: it for it in stickers.scan_stickers(library)}
    assert items["早安組_111/001.png"].tags == sticker_meta.StickerTags("早安~", ("早安", "morning"))

    kw.install(library, src, overwrite=True)
    assert "010" not in json.loads(target.read_text(encoding="utf-8"))


def test_status_lists_missing(kw, library: Path) -> None:
    (library / "早安組_111" / sticker_meta.TAGS_FILENAME).write_text(
        json.dumps({"001": {"t": "", "k": ["早安"]}, "002": {"t": "", "k": []}}), encoding="utf-8")
    report = {s.group_id: (done, missing) for s, done, missing in kw.status(library)}
    assert report["111"] == (1, ["002", "010"])
    assert report["222"] == (0, ["001"])


def test_load_keyword_file_rejects_bad_format(kw, tmp_path: Path) -> None:
    bad = tmp_path / "1.json"
    bad.write_text(json.dumps({"001": {"k": "不是清單"}}), encoding="utf-8")
    with pytest.raises(ValueError):
        kw.load_keyword_file(bad)


def test_import_copies_series_data_files(library: Path, tmp_path: Path) -> None:
    from soundboard.gui.sticker_actions import StickerActions

    series = library / "早安組_111"
    (series / sticker_meta.TAGS_FILENAME).write_text('{"001": {"t": "", "k": ["新的"]}}', encoding="utf-8")
    dest = tmp_path / "貼圖" / series.name
    dest.mkdir(parents=True)
    (dest / sticker_meta.TAGS_FILENAME).write_text('{"001": {"t": "", "k": ["舊的"]}}', encoding="utf-8")
    files = [(series / "001.png", dest), (series / sticker_meta.TAGS_FILENAME, dest)]

    assert StickerActions.import_files(files) == 1              # 資料檔不算一張貼圖
    assert "新的" in (dest / sticker_meta.TAGS_FILENAME).read_text(encoding="utf-8")   # 直接覆蓋，不是「(2)」
    assert not list(dest.glob("sticker_keywords (*"))


def test_scan_lists_each_sticker_once(library: Path) -> None:
    keys = [it.key for it in stickers.scan_stickers(library)]
    # 動態貼圖同時有 APNG（001.png）與 GIF：只列 GIF 一張；特效層與 .line 底圖不列
    assert keys == ["作者_999/動態組_222/001.gif", "早安組_111/001.png", "早安組_111/002.png", "早安組_111/010.png"]
