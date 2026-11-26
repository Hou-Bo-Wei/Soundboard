"""貼圖關鍵字工具：拼總覽圖（看圖寫關鍵字用）、把關鍵字檔裝進貼圖系列資料夾、檢查進度。

LINE 每張貼圖的隱藏關鍵字沒有公開，所以做法是：
    1. ``sheets``：把每組貼圖拼成一張總覽圖，每格左上角標編號。
    2. 看總覽圖（人或 AI）寫 ``<組id>.json``：``{"001": {"t": "圖上的字", "k": ["關鍵字", ...]}}``。
    3. ``install``：把 ``<組id>.json`` 複製到對應的系列資料夾，存成 ``sticker_keywords.json``，
       音效版的貼圖頁掃描時會讀進來。
    4. ``status``：列出哪些系列還沒有關鍵字檔、檔案裡缺了哪幾張。

組 id 是系列資料夾名稱最後一個底線後面的部分（``胖鯊魚鯊西米-嘿嘿嘿_32292384`` → ``32292384``），
與貼圖鍵盤 App 的 ``tools/keywords/<組id>.json`` 相同，兩邊的檔案可以直接共用。

用法::

    python tools/sticker_keywords/sticker_keywords.py sheets  "%USERPROFILE%\\Downloads\\LINE貼圖" out\\sheets
    python tools/sticker_keywords/sticker_keywords.py install "%USERPROFILE%\\Downloads\\LINE貼圖" out\\keywords
    python tools/sticker_keywords/sticker_keywords.py status  "%USERPROFILE%\\Downloads\\LINE貼圖"
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

TAGS_FILENAME = "sticker_keywords.json"
STICKER_EXTENSIONS = (".png", ".gif", ".webp", ".apng", ".jpg", ".jpeg")
# 動態貼圖下載器會同時存 NNN.png（第一格）與 NNN.gif；總覽圖只要一張，優先用靜態圖
_PREFERRED = {ext: rank for rank, ext in enumerate((".png", ".jpg", ".jpeg", ".webp", ".apng", ".gif"))}
_SKIP_SUFFIXES = ("_popup", "_key", "_sound")
_STEM = re.compile(r"^\d+$")


@dataclass(frozen=True)
class Series:
    """一組貼圖（一個系列資料夾）。

    Attributes:
        folder (Path): 系列資料夾。
        group_id (str): 組 id（資料夾名稱最後一個底線後面的部分）。
        stickers (dict[str, Path]): 貼圖編號（主檔名）→ 總覽圖要用的圖檔。
    """

    folder: Path
    group_id: str
    stickers: dict[str, Path]

    @property
    def title(self) -> str:
        """去掉組 id 的系列名稱。"""
        return self.folder.name.rsplit("_", 1)[0] if "_" in self.folder.name else self.folder.name


def group_id_of(folder: Path) -> str:
    """系列資料夾的組 id。

    Args:
        folder (Path): 系列資料夾。

    Returns:
        str: 最後一個底線後面的部分；沒有底線時為整個資料夾名稱。
    """
    return folder.name.rsplit("_", 1)[-1]


def find_series(root: Path) -> list[Series]:
    """找出貼圖資料夾裡所有直接放著貼圖的子資料夾（放在根目錄或作者資料夾底下都可以）。

    Args:
        root (Path): 貼圖資料夾（例如 ``Downloads\\LINE貼圖``）。

    Returns:
        list[Series]: 依資料夾路徑排序。
    """
    result: list[Series] = []
    for current, dirs, names in os.walk(root):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))     # .line 放的是文字貼圖的底圖
        series = _series_from(Path(current), names)
        # 根目錄本身有貼圖時，只有在它沒有子資料夾（也就是直接選了一個系列資料夾）才算一組
        if series is not None and (Path(current) != root or not dirs):
            result.append(series)
    return sorted(result, key=lambda s: str(s.folder))


def _series_from(folder: Path, names: list[str]) -> Series | None:
    """由資料夾裡的檔名建立一組貼圖；沒有貼圖時為 None。"""
    picks: dict[str, Path] = {}
    for name in names:
        stem, ext = os.path.splitext(name)
        ext = ext.lower()
        if ext not in STICKER_EXTENSIONS or stem.endswith(_SKIP_SUFFIXES):
            continue
        old = picks.get(stem)
        if old is None or _PREFERRED[ext] < _PREFERRED[old.suffix.lower()]:
            picks[stem] = folder / name
    if not picks:
        return None
    ordered = dict(sorted(picks.items(), key=lambda kv: (len(kv[0]), kv[0])))
    return Series(folder, group_id_of(folder), ordered)


def load_series(folder: Path) -> Series | None:
    """讀取單一系列資料夾（例如剛下載好的那一組）。

    Args:
        folder (Path): 系列資料夾。

    Returns:
        Series | None: 一組貼圖；資料夾不存在或沒有貼圖時為 None。
    """
    try:
        names = [entry.name for entry in os.scandir(folder) if entry.is_file()]
    except OSError:
        return None
    return _series_from(Path(folder), names)


# ====================================================================== 總覽圖
def contact_sheet(series: Series, out_dir: Path, **options) -> list[Path]:
    """拼總覽圖並存檔（版面參數同 :func:`render_sheets`）。

    Args:
        series (Series): 一組貼圖。
        out_dir (Path): 輸出資料夾。
        **options: 傳給 :func:`render_sheets` 的版面參數。

    Returns:
        list[Path]: 產生的總覽圖（``<組id>.png``，分頁時為 ``<組id>_p2.png``…）。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for page, sheet in enumerate(render_sheets(series, **options), 1):
        name = f"{series.group_id}.png" if page == 1 else f"{series.group_id}_p{page}.png"
        sheet.save(out_dir / name)
        paths.append(out_dir / name)
    return paths


def render_sheets(series: Series, *, cell: int = 180, columns: int = 6, per_sheet: int = 42) -> list:
    """把一組貼圖拼成總覽圖（每格左上角標編號，灰色底讓白字也看得清楚）。

    Args:
        series (Series): 一組貼圖。
        cell (int): 每格邊長（像素）。
        columns (int): 每列幾格。
        per_sheet (int): 每張總覽圖最多幾格（太多張擠在一起會看不清楚，超過就分頁）。

    Returns:
        list[PIL.Image.Image]: 每頁一張 RGB 總覽圖。
    """
    from PIL import Image, ImageDraw, ImageFont

    try:
        font = ImageFont.truetype("msjhbd.ttc", 22)
    except OSError:
        font = ImageFont.load_default()
    stems = list(series.stickers)
    sheets = []
    for start in range(0, len(stems), per_sheet):
        chunk = stems[start:start + per_sheet]
        rows = (len(chunk) + columns - 1) // columns
        sheet = Image.new("RGB", (columns * cell, rows * cell), (200, 200, 200))
        draw = ImageDraw.Draw(sheet)
        for i, stem in enumerate(chunk):
            x, y = (i % columns) * cell, (i // columns) * cell
            draw.rectangle((x, y, x + cell - 1, y + cell - 1), outline=(150, 150, 150))
            try:
                with Image.open(series.stickers[stem]) as im:
                    im.seek(0)
                    frame = im.convert("RGBA")
            except (OSError, ValueError, SyntaxError):
                continue
            frame.thumbnail((cell - 8, cell - 8))
            sheet.paste(frame, (x + (cell - frame.width) // 2, y + (cell - frame.height) // 2), frame)
            draw.text((x + 4, y + 2), stem, fill=(220, 0, 0), font=font, stroke_width=3, stroke_fill=(255, 255, 255))
        sheets.append(sheet)
    return sheets


# ====================================================================== 關鍵字檔
def load_keyword_file(path: Path) -> dict[str, dict]:
    """讀取並檢查一個關鍵字檔（``{"001": {"t": str, "k": [str]}}``）。

    Args:
        path (Path): 關鍵字檔。

    Returns:
        dict[str, dict]: 整理過的內容（``t`` 去掉前後空白、``k`` 去掉空字串與重複）。

    Raises:
        ValueError: 格式不對時拋出（訊息說明哪一張有問題）。
    """
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError(f"{path.name}：最外層必須是物件")
    clean: dict[str, dict] = {}
    for stem, entry in data.items():
        if isinstance(entry, list):
            entry = {"t": "", "k": entry}
        if not isinstance(entry, dict) or not isinstance(entry.get("k", []), list):
            raise ValueError(f"{path.name}：{stem} 的格式不對")
        words = list(dict.fromkeys(str(w).strip() for w in entry.get("k", []) if str(w).strip()))
        clean[str(stem)] = {"t": str(entry.get("t") or "").strip(), "k": words}
    return clean


def install(root: Path, keyword_dir: Path, *, overwrite: bool = False) -> tuple[int, list[str]]:
    """把 ``<組id>.json`` 裝進對應的系列資料夾（存成 ``sticker_keywords.json``）。

    已經有 ``sticker_keywords.json`` 時會合併：新檔案有的貼圖覆蓋，沒有的保留；
    ``overwrite`` 為 True 時整個取代。

    Args:
        root (Path): 貼圖資料夾。
        keyword_dir (Path): 放 ``<組id>.json`` 的資料夾。
        overwrite (bool): 是否整個取代既有的檔案。

    Returns:
        tuple[int, list[str]]: 裝好的系列數、找不到對應系列的關鍵字檔名。
    """
    by_id: dict[str, list[Series]] = {}
    for series in find_series(root):
        by_id.setdefault(series.group_id, []).append(series)
    installed, orphans = 0, []
    for path in sorted(keyword_dir.glob("*.json")):
        targets = by_id.get(path.stem)
        if not targets:
            orphans.append(path.name)
            continue
        data = load_keyword_file(path)
        for series in targets:
            write_keywords(series.folder, data, overwrite=overwrite)
            installed += 1
    return installed, orphans


def write_keywords(folder: Path, data: dict[str, dict], *, overwrite: bool = False,
                   keep_existing: bool = False) -> Path:
    """把關鍵字寫進系列資料夾的 ``sticker_keywords.json``（先寫暫存檔再換名，中途失敗不會留下壞檔）。

    Args:
        folder (Path): 系列資料夾。
        data (dict[str, dict]): 貼圖編號 →（``t``, ``k``）。
        overwrite (bool): 整個取代既有的檔案。
        keep_existing (bool): 合併時既有的項目優先（AI 補關鍵字時不蓋掉使用者手改過的）；
            預設是新的優先。

    Returns:
        Path: 寫好的檔案。
    """
    target = folder / TAGS_FILENAME
    merged: dict[str, dict] = {}
    if target.exists() and not overwrite:
        try:
            merged = load_keyword_file(target)
        except (OSError, ValueError) as exc:
            print(f"⚠ 既有的 {target} 無法讀取，改為取代：{exc}", file=sys.stderr)
    merged = {**data, **merged} if keep_existing else {**merged, **data}
    ordered = dict(sorted(merged.items(), key=lambda kv: (len(kv[0]), kv[0])))
    tmp = target.with_suffix(".json.part")
    tmp.write_text(json.dumps(ordered, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, target)
    return target


def missing_stems(series: Series) -> list[str]:
    """這一組裡還沒有關鍵字的貼圖編號。

    Args:
        series (Series): 一組貼圖。

    Returns:
        list[str]: 缺少關鍵字的編號；關鍵字檔壞掉時視為全部缺少。
    """
    target = series.folder / TAGS_FILENAME
    try:
        data = load_keyword_file(target) if target.exists() else {}
    except (OSError, ValueError):
        data = {}
    return [s for s in series.stickers if not data.get(s, {}).get("k")]


def status(root: Path) -> list[tuple[Series, int, list[str]]]:
    """每一組的關鍵字完成度。

    Args:
        root (Path): 貼圖資料夾。

    Returns:
        list[tuple[Series, int, list[str]]]: （系列, 有關鍵字的張數, 缺少關鍵字的編號）。
    """
    report = []
    for series in find_series(root):
        missing = missing_stems(series)
        report.append((series, len(series.stickers) - len(missing), missing))
    return report


# ====================================================================== 指令列
def _generate(root: Path, model: str | None, yes: bool) -> int:
    """``generate`` 指令：列出要處理的系列與預估費用，確認後呼叫 Claude。"""
    import keyword_ai     # 延後匯入：只有這個指令需要 anthropic 套件

    settings = keyword_ai.AISettings.load()
    if model:
        settings.model = model
    if not settings.effective_key:
        print("尚未設定 API 金鑰：請在 LINE 貼圖下載器的「AI 關鍵字設定」輸入，或設定環境變數 ANTHROPIC_API_KEY")
        return 1
    todo = keyword_ai.plan([root], settings.model)
    if not todo.series:
        print("所有系列都已經有關鍵字")
        return 0
    label = keyword_ai.model_choice(settings.model).label
    print(f"將用 {label} 處理 {len(todo.series)} 組、{todo.stickers} 張，預估約 US${todo.estimate:.2f}")
    if not yes and input("要開始嗎？(y/N) ").strip().lower() != "y":
        return 0
    done, _cost = keyword_ai.fill_keywords([root], settings)
    return 0 if done == len(todo.series) else 1


def main(argv: list[str] | None = None) -> int:
    """指令列進入點。

    Args:
        argv (list[str] | None): 參數（None 表示使用 ``sys.argv``）。

    Returns:
        int: 結束碼。
    """
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="貼圖關鍵字工具")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("sheets", help="把每組貼圖拼成總覽圖")
    p.add_argument("root", type=Path)
    p.add_argument("out", type=Path)
    p.add_argument("--only-missing", action="store_true", help="只做還沒有關鍵字檔的系列")
    p = sub.add_parser("install", help="把 <組id>.json 裝進系列資料夾")
    p.add_argument("root", type=Path)
    p.add_argument("keywords", type=Path)
    p.add_argument("--overwrite", action="store_true", help="整個取代既有的 sticker_keywords.json（預設合併）")
    p = sub.add_parser("status", help="列出哪些系列還缺關鍵字")
    p.add_argument("root", type=Path)
    p = sub.add_parser("generate", help="用 Claude API 為還缺關鍵字的系列自動產生關鍵字（會產生費用）")
    p.add_argument("root", type=Path)
    p.add_argument("--model", help="模型 ID（預設用 LINE 貼圖下載器設定的模型，沒設定時為 Sonnet 5.5）")
    p.add_argument("--yes", action="store_true", help="不詢問，直接開始")
    args = parser.parse_args(argv)

    if args.command == "sheets":
        index = []
        for series in find_series(args.root):
            if args.only_missing and (series.folder / TAGS_FILENAME).exists():
                continue
            sheets = contact_sheet(series, args.out)
            index.append({"id": series.group_id, "title": series.title, "stickers": list(series.stickers),
                          "sheets": [p.name for p in sheets]})
            print(f"{series.group_id}\t{len(series.stickers)} 張\t{series.title}")
        (args.out / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"共 {len(index)} 組，總覽圖在 {args.out}")
    elif args.command == "generate":
        return _generate(args.root, args.model, args.yes)
    elif args.command == "install":
        count, orphans = install(args.root, args.keywords, overwrite=args.overwrite)
        print(f"已裝好 {count} 組的關鍵字")
        for name in orphans:
            print(f"⚠ 找不到對應的系列資料夾：{name}")
    else:
        total = done_total = 0
        for series, done, missing in status(args.root):
            total += len(series.stickers)
            done_total += done
            if missing:
                shown = ", ".join(missing[:10]) + ("…" if len(missing) > 10 else "")
                print(f"{series.group_id}\t缺 {len(missing)} 張（{shown}）\t{series.title}")
        print(f"共 {total} 張，{done_total} 張有關鍵字")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
