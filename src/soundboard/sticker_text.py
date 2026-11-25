"""文字貼圖：把使用者輸入的文字套用到 LINE 的訊息貼圖或「隨你填」名字貼圖上。

做法：
    * **訊息貼圖**：優先請 LINE 伺服器畫文字圖層（``store.line.me/overlay/...?text=``，和 LINE App 裡的字型、
      排版一模一樣），疊到底圖上。沒有網路或 LINE 拒絕時，改在本機畫字。
    * **名字貼圖**：LINE 畫名字的 API 需要登入，所以在本機畫：比對「****」那張圖與沒有名字的底圖，
      差異的範圍就是名字的位置，顏色取自「****」。

本機畫字使用 Windows 的微軟正黑體（粗體），字太多時自動縮小並換行。這個模組不依賴 Tk。
"""

from __future__ import annotations

import hashlib
import io
import logging
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from soundboard.i18n import tr
from soundboard.sticker_meta import TextSpec

logger = logging.getLogger(__name__)

API_TIMEOUT_SECONDS = 8
MAX_TEXT_LENGTH = 100            # LINE 訊息貼圖的上限
_USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
_DIFF_THRESHOLD = 48             # 「****」與底圖每個像素差多少才算是名字的一部分
_ALPHA_VISIBLE = 64
_FONT_CANDIDATES: tuple[str, ...] = (
    r"C:\Windows\Fonts\msjhbd.ttc",   # 微軟正黑體 粗體
    r"C:\Windows\Fonts\msjh.ttc",
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\meiryob.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
)
_MIN_FONT_PX = 8
_MESSAGE_WIDEN = 1.3
_NAME_HEIGHT_SCALE = 1.6
_MAX_LINES = 4

# 取得文字圖層的函式（測試時換成假的，避免連網）：(網址, Referer) → PNG bytes
Fetcher = Callable[[str, str], bytes]


class TextStickerError(Exception):
    """文字貼圖無法產生（訊息已翻譯）。"""


@dataclass(frozen=True)
class TextLayout:
    """本機畫字時的位置與顏色。

    Attributes:
        box (tuple[int, int, int, int]): 文字範圍（左, 上, 右, 下），以底圖像素為單位。
        color (tuple[int, int, int]): 文字顏色。
        max_width (int): 文字比原本長時，最多可以往左右延伸到多寬（像素）。
    """

    box: tuple[int, int, int, int]
    color: tuple[int, int, int]
    max_width: int


def _pillow():
    try:
        from PIL import Image
    except ImportError as exc:      # pragma: no cover - 依賴安裝問題
        raise TextStickerError(tr("貼圖功能需要 Pillow 套件，請執行：pip install pillow")) from exc
    return Image


def _open_rgba(path: Path):
    Image = _pillow()
    try:
        with Image.open(path) as im:
            im.seek(0)
            return im.convert("RGBA")
    except (OSError, ValueError, SyntaxError) as exc:
        raise TextStickerError(tr("無法讀取文字貼圖的底圖「{name}」：{error}", name=path.name, error=exc)) from exc


def clean_text(text: str) -> str:
    """整理要套用的文字：去掉頭尾空白，限制長度。

    Args:
        text (str): 使用者輸入的文字。

    Returns:
        str: 可以套用的文字。
    """
    return text.strip()[:MAX_TEXT_LENGTH]


# ====================================================================== 版面
def _fallback_layout(width: int, height: int) -> TextLayout:
    """找不到文字位置時：放在上方四分之一。"""
    margin = width // 10
    return TextLayout((margin, height // 20, width - margin, height // 4), (40, 40, 40), width - 2 * margin)


def _pixels(image) -> list:
    """圖片的像素清單（Pillow 12 起 getdata 改名為 get_flattened_data）。"""
    getter = getattr(image, "get_flattened_data", None) or image.getdata
    return list(getter())


def _dominant_color(pixels: list[tuple[int, int, int, int]]) -> tuple[int, int, int]:
    """文字的顏色：取不透明像素中最暗的那一半的平均（避開反鋸齒邊緣與白色描邊）。"""
    solid = sorted((p for p in pixels if p[3] >= 200), key=lambda p: p[0] + p[1] + p[2])
    if not solid:
        return 40, 40, 40
    half = solid[: max(1, len(solid) // 2)]
    return tuple(sum(p[i] for p in half) // len(half) for i in range(3))  # type: ignore[return-value]


@lru_cache(maxsize=512)
def find_layout(spec: TextSpec) -> TextLayout:
    """找出本機畫字的位置與顏色。

    訊息貼圖看預設文字圖層的不透明範圍；名字貼圖比對「****」與底圖的差異。

    Args:
        spec (TextSpec): 文字貼圖。

    Returns:
        TextLayout: 位置與顏色；判斷不出來時放在上方。

    Raises:
        TextStickerError: 底圖無法讀取時拋出。
    """
    Image = _pillow()
    from PIL import ImageChops, ImageFilter

    base = _open_rgba(spec.base)
    width, height = base.size
    if spec.kind == "message" and spec.overlay is not None and spec.overlay.is_file():
        overlay = _open_rgba(spec.overlay)
        if overlay.size != base.size:
            overlay = overlay.resize(base.size, Image.Resampling.NEAREST)
        mask = overlay.getchannel("A").point(lambda a: 255 if a >= _ALPHA_VISIBLE else 0)
        box = mask.getbbox()
        if box is None:
            return _fallback_layout(width, height)
        pixels = [p for p, m in zip(_pixels(overlay), _pixels(mask)) if m]
        # LINE 會在文字框裡自動換行；預設文字通常比框小，所以容許稍微加寬，但不超出貼圖
        margin = width // 20
        max_width = min(width - 2 * margin, int((box[2] - box[0]) * _MESSAGE_WIDEN))
        return TextLayout(box, _dominant_color(pixels), max(max_width, box[2] - box[0]))
    if spec.kind == "name" and spec.sample.is_file():
        sample = _open_rgba(spec.sample)
        if sample.size != base.size:
            sample = sample.resize(base.size, Image.Resampling.NEAREST)
        # 四個通道取最大差異：名字常畫在透明背景上，只看亮度（convert("L")）會忽略透明度的變化
        red, green, blue, alpha = ImageChops.difference(sample, base).split()
        diff = ImageChops.lighter(ImageChops.lighter(red, green), ImageChops.lighter(blue, alpha))
        mask = diff.point(lambda d: 255 if d >= _DIFF_THRESHOLD else 0)
        # 兩張圖各自壓縮過，邊緣會有零星的差異點；先侵蝕掉孤立的點，只留下「****」這種成塊的範圍
        core = mask.filter(ImageFilter.MinFilter(3)).getbbox()
        box = mask.getbbox() if core is None else (
            max(core[0] - 2, 0), max(core[1] - 2, 0), min(core[2] + 2, width), min(core[3] + 2, height))
        if box is None:
            return _fallback_layout(width, height)
        pixels = [p for p, m in zip(_pixels(sample.crop(box)), _pixels(mask.crop(box))) if m]
        # 星號比中文字矮，以星號的高度當字高會太小：上下放大到和一般文字差不多高（中心不變）
        cy, half = (box[1] + box[3]) / 2, (box[3] - box[1]) * _NAME_HEIGHT_SCALE / 2
        box = (box[0], max(int(cy - half), 0), box[2], min(int(cy + half), height))
        # 名字後面常接著底圖本身的字（例如「****是真的」），所以名字只能放在原本的寬度內，太長就縮小
        return TextLayout(box, _dominant_color(pixels), box[2] - box[0])
    return _fallback_layout(width, height)


# ====================================================================== 本機畫字
@lru_cache(maxsize=64)
def _font(size: int):
    from PIL import ImageFont

    for candidate in _FONT_CANDIDATES:
        if Path(candidate).is_file():
            try:
                return ImageFont.truetype(candidate, size)
            except OSError:
                continue
    return ImageFont.load_default(size)


def _wrap(text: str, lines: int) -> list[str]:
    """把文字平均切成指定行數（中文沒有空白可以斷，用字數平均切；使用者自己的換行優先）。"""
    if "\n" in text:
        return [line for line in text.split("\n")][:_MAX_LINES]
    if lines <= 1 or len(text) <= 1:
        return [text]
    per_line = -(-len(text) // lines)
    return [text[i:i + per_line] for i in range(0, len(text), per_line)]


def _fit(draw, text: str, max_w: int, max_h: int) -> tuple[list[str], object, int]:
    """找出最大的字級與行數，讓文字放得進 max_w × max_h。"""
    best: tuple[list[str], object, int] | None = None
    for lines_count in range(1, _MAX_LINES + 1):
        lines = _wrap(text, lines_count)
        size = max(_MIN_FONT_PX, int(max_h / len(lines) * 0.95))
        while size > _MIN_FONT_PX:
            font = _font(size)
            widths = [draw.textlength(line, font=font) for line in lines]
            line_h = size * 1.15
            if max(widths) <= max_w and line_h * len(lines) <= max_h * 1.05:
                break
            size -= 1
        if best is None or size > best[2]:
            best = (lines, _font(size), size)
        if "\n" in text:
            break
    assert best is not None
    return best


def render_local(spec: TextSpec, text: str):
    """在本機把文字畫到底圖上。

    文字比原本的範圍長時，以原本的中心點向左右延伸（不超過 ``max_width``），再不夠就縮小字級或換行。

    Args:
        spec (TextSpec): 文字貼圖。
        text (str): 要畫的文字。

    Returns:
        PIL.Image.Image: RGBA 貼圖。

    Raises:
        TextStickerError: 底圖無法讀取時拋出。
    """
    from PIL import ImageDraw

    image = _open_rgba(spec.base)
    layout = find_layout(spec)
    left, top, right, bottom = layout.box
    cx = (left + right) / 2
    box_h = max(bottom - top, 12)
    draw = ImageDraw.Draw(image)
    lines, font, size = _fit(draw, text, layout.max_width, box_h)
    line_h = size * 1.15
    y = (top + bottom) / 2 - line_h * len(lines) / 2
    # 白色描邊：貼圖背景常是插畫，加一點描邊在任何顏色上都讀得到
    stroke = max(1, size // 14)
    for line in lines:
        width = draw.textlength(line, font=font)
        x = min(max(cx - width / 2, 0), image.width - width)
        draw.text((x, y), line, font=font, fill=(*layout.color, 255), stroke_width=stroke,
                  stroke_fill=(255, 255, 255, 255))
        y += line_h
    return image


# ====================================================================== LINE 伺服器畫字
def fetch_overlay(url: str, referer: str) -> bytes:
    """向 LINE 取得文字圖層（PNG）。

    Args:
        url (str): 完整網址（含文字）。
        referer (str): 商品頁網址（LINE 只接受從商品頁發出的要求）。

    Returns:
        bytes: PNG 內容。

    Raises:
        OSError: 網路錯誤或 LINE 回應錯誤時拋出。
    """
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT, "Referer": referer,
                                                   "Accept": "image/png,image/*;q=0.8"})
    with urllib.request.urlopen(request, timeout=API_TIMEOUT_SECONDS) as response:
        data = response.read()
    if not data.startswith(b"\x89PNG"):
        raise OSError("LINE 回傳的不是 PNG")
    return data


def _overlay_cache_dir() -> Path:
    return Path(tempfile.gettempdir()) / "Soundboard_stickers" / "overlay"


def render_with_line(spec: TextSpec, text: str, fetch: Fetcher = fetch_overlay):
    """請 LINE 畫文字圖層，疊到底圖上（圖層會快取在暫存資料夾，同樣的字不再重抓）。

    Args:
        spec (TextSpec): 訊息貼圖。
        text (str): 文字。
        fetch (Fetcher): 取得圖層的函式。

    Returns:
        PIL.Image.Image: RGBA 貼圖。

    Raises:
        OSError: 取得圖層失敗時拋出。
        TextStickerError: 底圖無法讀取時拋出。
    """
    Image = _pillow()
    url = spec.api + urllib.parse.quote(text, safe="")
    cache = _overlay_cache_dir() / (hashlib.sha1(url.encode("utf-8")).hexdigest() + ".png")
    if cache.is_file():
        data = cache.read_bytes()
    else:
        data = fetch(url, spec.referer)
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_bytes(data)
        except OSError:
            pass        # 快取失敗不影響結果
    base = _open_rgba(spec.base)
    try:
        overlay = Image.open(io.BytesIO(data)).convert("RGBA")
    except (OSError, ValueError, SyntaxError) as exc:
        raise OSError(f"LINE 回傳的文字圖層無法讀取：{exc}") from exc
    if overlay.size != base.size:
        overlay = overlay.resize(base.size, Image.Resampling.LANCZOS)
    base.alpha_composite(overlay)
    return base


def render(spec: TextSpec, text: str, *, fetch: Fetcher | None = fetch_overlay):
    """產生套用文字的貼圖。

    Args:
        spec (TextSpec): 文字貼圖。
        text (str): 文字（空白時用預設文字）。
        fetch (Fetcher | None): 取得 LINE 文字圖層的函式；None 表示只在本機畫字。

    Returns:
        tuple[PIL.Image.Image, bool]: （RGBA 貼圖, 是否由 LINE 伺服器畫字）。

    Raises:
        TextStickerError: 底圖無法讀取時拋出。
    """
    text = clean_text(text) or spec.default_text or " "
    if spec.kind == "message" and spec.api and fetch is not None:
        try:
            return render_with_line(spec, text, fetch), True
        except (OSError, urllib.error.URLError, ValueError) as exc:
            # 沒有網路或 LINE 改版：本機畫字仍然可用，只是字型與排版不完全相同
            logger.info("LINE 文字圖層取得失敗，改在本機畫字（%s）：%s", spec.base.name, exc)
    return render_local(spec, text), False


def save_png(image, folder: Path, stem: str) -> Path:
    """把產生的貼圖存成 PNG（複製到剪貼簿時，貼上的程式要讀得到檔案）。

    Args:
        image (PIL.Image.Image): 貼圖。
        folder (Path): 存放的資料夾。
        stem (str): 檔名主檔名（相同時覆蓋，避免暫存檔越堆越多）。

    Returns:
        Path: 存好的檔案。
    """
    folder.mkdir(parents=True, exist_ok=True)
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in stem)[:60] or "sticker"
    target = folder / f"{safe}.png"
    image.save(target, format="PNG", optimize=True)
    return target
