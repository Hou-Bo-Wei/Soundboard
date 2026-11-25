"""貼圖：掃描貼圖資料夾、判斷動態貼圖、為 Discord 準備檔案（必要時轉 GIF、縮小）。

這個模組不依賴 Tk，方便單獨測試。圖片處理使用 Pillow（延後匯入，沒安裝時只有貼圖功能不能用）。

Discord 的限制與做法：
    * 一般上傳檔案上限約 10 MiB（未加成的伺服器）。超過時先縮小尺寸、再抽掉部分影格，仍太大才回報失敗。
    * 動態 **GIF** 在 Discord 會動；動態 **APNG／WebP** 以附件上傳時多半只顯示第一格，
      所以動態貼圖一律轉成 GIF 再傳。
    * GIF 只有「全透明／不透明」兩種，半透明的陰影邊緣會被截掉，這是 GIF 格式本身的限制。
"""

from __future__ import annotations

import io
import logging
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from soundboard.i18n import tr

logger = logging.getLogger(__name__)

STICKER_EXTENSIONS: frozenset[str] = frozenset({".png", ".apng", ".gif", ".webp", ".jpg", ".jpeg"})

# 傳給 Discord 的檔案大小上限（bytes）。預設伺服器的上限是 10 MiB，扣掉一點給表單格式的額外開銷
DISCORD_UPLOAD_LIMIT: int = 10 * 1024 * 1024 - 64 * 1024
# 「傳送大小」的選項（長邊像素）；0 = 原始大小
SEND_SIZE_CHOICES: tuple[int, ...] = (0, 320, 240, 160, 120)

# 動態貼圖超過大小上限時依序嘗試的縮放比例
_SHRINK_STEPS: tuple[float, ...] = (1.0, 0.85, 0.7, 0.55, 0.4, 0.3)
_GIF_MIN_DURATION_MS = 20       # 瀏覽器與 Discord 會把更短的影格間隔當成 100 毫秒
_ALPHA_CUTOFF = 128             # GIF 只有一個透明色：透明度低於這個值視為透明


class StickerError(Exception):
    """貼圖無法讀取、轉換或傳送（訊息已翻譯，可直接顯示給使用者）。"""


@dataclass(frozen=True)
class StickerItem:
    """貼圖資料夾裡的一張貼圖。

    Attributes:
        key (str): 唯一識別碼（相對於貼圖資料夾的路徑，以 ``/`` 分隔）。
        path (Path): 檔案完整路徑。
        name (str): 顯示名稱（不含副檔名）。
        series (str): 所屬系列（貼圖在哪個子資料夾；直接放在根目錄時為空字串）。
    """

    key: str
    path: Path
    name: str
    series: str


@dataclass(frozen=True)
class StickerInfo:
    """貼圖檔的內容資訊。

    Attributes:
        width (int): 寬（像素）。
        height (int): 高（像素）。
        frames (int): 影格數（靜態圖為 1）。
        size_bytes (int): 檔案大小。
    """

    width: int
    height: int
    frames: int
    size_bytes: int

    @property
    def animated(self) -> bool:
        """是否為動態貼圖。"""
        return self.frames > 1


@dataclass(frozen=True)
class PreparedFile:
    """準備好要上傳的檔案。

    Attributes:
        filename (str): 上傳時的檔名。
        data (bytes): 檔案內容。
        content_type (str): MIME 類型。
        converted (bool): 是否經過轉換或縮小（false 表示原檔照傳）。
    """

    filename: str
    data: bytes
    content_type: str
    converted: bool


def _require_pillow():
    try:
        from PIL import Image
    except ImportError as exc:      # pragma: no cover - 依賴安裝問題
        raise StickerError(tr("貼圖功能需要 Pillow 套件，請執行：pip install pillow")) from exc
    return Image


def _read_error(path: Path, exc: Exception) -> StickerError:
    """把 Pillow 的讀取例外轉成使用者看得懂的錯誤。"""
    from PIL import UnidentifiedImageError

    reason = tr("不是有效的圖片檔") if isinstance(exc, UnidentifiedImageError) else str(exc)
    return StickerError(tr("無法讀取貼圖「{name}」：{error}", name=path.name, error=reason))


# ====================================================================== 掃描
def scan_stickers(folder: Path) -> list[StickerItem]:
    """遞迴掃描貼圖資料夾（子資料夾視為貼圖系列，LINE 貼圖下載器一個系列一個資料夾）。

    Args:
        folder (Path): 貼圖資料夾。

    Returns:
        list[StickerItem]: 依系列與名稱（自然排序，``2`` 排在 ``10`` 前面）排序；資料夾不存在時為空清單。
    """
    if not folder.is_dir():
        return []
    items: list[StickerItem] = []
    # os.walk 讀目錄時順便拿到檔案類型，比 rglob 再對每個檔案 stat 一次快很多（幾千張時差好幾倍）
    for current, _dirs, names in os.walk(folder):
        series = Path(current).relative_to(folder).as_posix()
        series = "" if series == "." else series
        for filename in names:
            if os.path.splitext(filename)[1].lower() not in STICKER_EXTENSIONS:
                continue
            key = f"{series}/{filename}" if series else filename
            items.append(StickerItem(key=key, path=Path(current) / filename, name=os.path.splitext(filename)[0],
                                     series=series))
    items.sort(key=lambda it: (natural_key(it.series), natural_key(it.name)))
    return items


def natural_key(text: str) -> list[object]:
    """自然排序的排序鍵：數字依大小而不是依字元比較（``2`` < ``10``）。

    Args:
        text (str): 要排序的文字。

    Returns:
        list[object]: 排序鍵。
    """
    return [int(part) if part.isdigit() else part.casefold() for part in re.split(r"(\d+)", text)]


def series_title(series: str) -> str:
    """系列資料夾的顯示名稱：去掉 LINE 貼圖下載器加在資料夾名稱後面的編號（``名稱_11155948``），子資料夾用 ``›`` 連接。

    Args:
        series (str): 相對路徑（以 ``/`` 分隔）。

    Returns:
        str: 顯示用的名稱。
    """
    parts = [re.sub(r"_[0-9a-fA-F]{5,}$", "", part) or part for part in series.split("/") if part]
    return " › ".join(parts)


def default_sticker_dir(base: Path) -> Path:
    """預設的貼圖資料夾（音效資料夾旁邊的「貼圖」資料夾）。

    Args:
        base (Path): 基準資料夾（通常是程式的工作目錄或執行檔所在位置）。

    Returns:
        Path: ``base / "貼圖"``。
    """
    return base / "貼圖"


def unique_destination(folder: Path, filename: str) -> Path:
    """在資料夾中找一個不會覆蓋既有檔案的檔名（重複時加 `` (2)``、`` (3)``…）。

    Args:
        folder (Path): 目的資料夾。
        filename (str): 想要的檔名。

    Returns:
        Path: 可用的完整路徑。
    """
    target = folder / filename
    stem, suffix = target.stem, target.suffix
    n = 2
    while target.exists():
        target = folder / f"{stem} ({n}){suffix}"
        n += 1
    return target


# ====================================================================== 讀取
def inspect_sticker(path: Path) -> StickerInfo:
    """讀取貼圖的尺寸、影格數與檔案大小。

    Args:
        path (Path): 貼圖檔。

    Returns:
        StickerInfo: 貼圖資訊。

    Raises:
        StickerError: 檔案無法讀取或不是有效的圖片時拋出。
    """
    Image = _require_pillow()
    try:
        with Image.open(path) as im:
            frames = int(getattr(im, "n_frames", 1))
            return StickerInfo(im.width, im.height, frames, path.stat().st_size)
    except (OSError, ValueError, SyntaxError) as exc:
        raise _read_error(path, exc) from exc


def load_thumbnail(path: Path, box: int):
    """產生縮圖（動態貼圖取第一格）。

    Args:
        path (Path): 貼圖檔。
        box (int): 縮圖的長邊像素。

    Returns:
        PIL.Image.Image: RGBA 縮圖（保留透明背景）。

    Raises:
        StickerError: 檔案無法讀取時拋出。
    """
    Image = _require_pillow()
    try:
        with Image.open(path) as im:
            im.seek(0)
            frame = im.convert("RGBA")
    except (OSError, ValueError, SyntaxError) as exc:
        raise _read_error(path, exc) from exc
    frame.thumbnail((box, box), Image.Resampling.LANCZOS)
    return frame


def load_animation(path: Path, box: int, max_frames: int = 120):
    """讀出動態貼圖的所有影格（縮成縮圖大小），給滑鼠移上去時播放動畫。

    Args:
        path (Path): 貼圖檔。
        box (int): 影格的長邊像素。
        max_frames (int): 最多讀幾格（避免超長動畫吃光記憶體）。

    Returns:
        list[tuple[PIL.Image.Image, int]]: （RGBA 影格, 顯示毫秒數）；靜態貼圖為空清單。

    Raises:
        StickerError: 檔案無法讀取時拋出。
    """
    Image = _require_pillow()
    from PIL import ImageSequence

    out = []
    try:
        with Image.open(path) as im:
            if int(getattr(im, "n_frames", 1)) <= 1:
                return []
            for i, frame in enumerate(ImageSequence.Iterator(im)):
                if i >= max_frames:
                    break
                duration = max(_GIF_MIN_DURATION_MS, int(frame.info.get("duration") or im.info.get("duration") or 100))
                rgba = frame.convert("RGBA")
                rgba.thumbnail((box, box), Image.Resampling.LANCZOS)
                out.append((rgba, duration))
    except (OSError, ValueError, SyntaxError, EOFError) as exc:
        raise _read_error(path, exc) from exc
    return out


# ====================================================================== 為 Discord 準備檔案
def _ascii_filename(stem: str, suffix: str) -> str:
    """上傳用的檔名：保留英數字，其餘（中文、符號）換成底線；全部被換掉時用 ``sticker``。"""
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", stem).strip("._")
    if not cleaned or set(cleaned) <= {"_"}:
        cleaned = "sticker"
    return f"{cleaned[:60]}{suffix}"


def _resized(im, max_edge: int):
    """把圖縮到長邊不超過 max_edge（0 = 不縮；不會放大）。"""
    Image = _require_pillow()
    if max_edge <= 0 or max(im.size) <= max_edge:
        return im
    ratio = max_edge / max(im.size)
    size = (max(1, round(im.width * ratio)), max(1, round(im.height * ratio)))
    return im.resize(size, Image.Resampling.LANCZOS)


def _to_gif_frame(rgba):
    """RGBA 影格轉成帶透明色的調色盤影格（索引 255 保留給透明）。"""
    Image = _require_pillow()
    alpha = rgba.getchannel("A")
    paletted = rgba.convert("RGB").convert("P", palette=Image.Palette.ADAPTIVE, colors=255)
    paletted.paste(255, alpha.point(lambda a: 255 if a < _ALPHA_CUTOFF else 0))
    paletted.info["transparency"] = 255
    return paletted


def _encode_gif(frames, durations: list[int], loop: int) -> bytes:
    buffer = io.BytesIO()
    frames[0].save(buffer, format="GIF", save_all=True, append_images=frames[1:], duration=durations, loop=loop,
                   disposal=2, transparency=255, optimize=False)
    return buffer.getvalue()


def _load_frames(im) -> tuple[list, list[int]]:
    """讀出動態圖的完整影格（RGBA）與每格的毫秒數。"""
    from PIL import ImageSequence

    frames, durations = [], []
    for frame in ImageSequence.Iterator(im):
        durations.append(max(_GIF_MIN_DURATION_MS, int(frame.info.get("duration") or 100)))
        frames.append(frame.convert("RGBA"))
    return frames, durations


def prepare_for_discord(path: Path, *, max_edge: int = 0, limit: int = DISCORD_UPLOAD_LIMIT,
                        progress: Callable[[str], None] | None = None) -> PreparedFile:
    """為上傳到 Discord 準備貼圖：能原檔照傳就原檔照傳，否則轉換或縮小到符合限制。

    規則：
        * 靜態圖：沒要縮小且在大小限制內 → 原檔；要縮小或太大 → 縮小後存成 PNG（保留透明）。
        * 動態 GIF：沒要縮小且在大小限制內 → 原檔。
        * 動態 APNG／WebP（以及需要縮小的 GIF）→ 轉成 GIF；超過大小限制時依序縮小、再抽掉一半影格。

    Args:
        path (Path): 貼圖檔。
        max_edge (int): 長邊上限像素（0 = 原始大小；不會放大）。
        limit (int): 檔案大小上限（bytes）。
        progress (Callable[[str], None] | None): 轉換比較久時回報進度文字。

    Returns:
        PreparedFile: 準備好的檔案。

    Raises:
        StickerError: 檔案無法讀取、或縮到最小仍超過大小限制時拋出。
    """
    Image = _require_pillow()
    try:
        raw = path.read_bytes()
        im = Image.open(io.BytesIO(raw))
        frames_count = int(getattr(im, "n_frames", 1))
        fmt = (im.format or "").upper()
        if frames_count <= 1:
            im.load()       # 提早發現損毀的檔案，而不是等到轉換時才出錯
        too_big = max(im.size) > max_edge > 0
    except (OSError, ValueError, SyntaxError) as exc:
        raise _read_error(path, exc) from exc

    animated = frames_count > 1
    # ---- 原檔照傳
    if not too_big and len(raw) <= limit:
        if not animated and fmt in {"PNG", "GIF", "JPEG", "WEBP"}:
            ext = ".jpg" if fmt == "JPEG" else f".{fmt.lower()}"
            ctype = {"PNG": "image/png", "GIF": "image/gif", "JPEG": "image/jpeg", "WEBP": "image/webp"}[fmt]
            return PreparedFile(_ascii_filename(path.stem, ext), raw, ctype, converted=False)
        if animated and fmt == "GIF":
            return PreparedFile(_ascii_filename(path.stem, ".gif"), raw, "image/gif", converted=False)

    # ---- 靜態圖：縮小後存 PNG
    if not animated:
        out = _resized(im.convert("RGBA"), max_edge)
        for ratio in (1.0, 0.7, 0.5, 0.35):
            candidate = out if ratio == 1.0 else _resized(out, round(max(out.size) * ratio))
            buffer = io.BytesIO()
            candidate.save(buffer, format="PNG", optimize=True)
            if buffer.tell() <= limit:
                return PreparedFile(_ascii_filename(path.stem, ".png"), buffer.getvalue(), "image/png", True)
        raise StickerError(tr("貼圖「{name}」縮到最小仍超過 Discord 的檔案大小上限", name=path.name))

    # ---- 動態圖：轉 GIF
    if progress is not None:
        progress(tr("正在把動態貼圖轉成 GIF…"))
    try:
        frames, durations = _load_frames(im)
    except (OSError, ValueError, SyntaxError, EOFError) as exc:
        raise _read_error(path, exc) from exc
    loop = int(im.info.get("loop", 0))
    base_edge = min(max(im.size), max_edge) if max_edge > 0 else max(im.size)
    for step in (1, 2):                        # 第二輪抽掉一半影格（每格時間加倍，動畫速度不變）
        use_frames = frames[::step]
        use_durations = [sum(durations[i:i + step]) for i in range(0, len(durations), step)]
        for ratio in _SHRINK_STEPS:
            edge = max(16, round(base_edge * ratio))
            gif_frames = [_to_gif_frame(_resized(f, edge)) for f in use_frames]
            data = _encode_gif(gif_frames, use_durations, loop)
            logger.debug("貼圖 %s 轉 GIF：邊長 %d、影格 %d、%d bytes", path.name, edge, len(gif_frames), len(data))
            if len(data) <= limit:
                return PreparedFile(_ascii_filename(path.stem, ".gif"), data, "image/gif", True)
            if progress is not None:
                progress(tr("檔案太大，正在縮小再試一次…"))
    raise StickerError(tr("貼圖「{name}」縮到最小仍超過 Discord 的檔案大小上限", name=path.name))

