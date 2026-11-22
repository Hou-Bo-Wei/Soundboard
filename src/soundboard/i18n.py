"""介面多語言（繁體中文、English、日本語、한국어）。

設計：
    * 原始碼裡的介面文字一律寫繁體中文，並用 :func:`tr` 包起來；繁體中文本身就是翻譯的 key。
      這樣讀程式碼時直接看得到畫面上的文字，也不必另外維護一份 key 對照表。
    * 其他語言放在 ``soundboard/locales/<代碼>.json``：``{"繁體中文原文": "翻譯"}``。
      找不到翻譯時顯示繁體中文原文，不會出現空白或 key。
    * 有變數的文字用具名的 ``{name}`` 佔位符：``tr("已載入 {count} 個音效", count=n)``，
      翻譯可以自由調整字詞順序（不同語言的語序不同，所以不用字串串接）。
    * 語言在啟動時決定一次；切換語言需重新啟動程式（介面文字都在建立元件時決定）。

只有使用者看得到的介面文字要翻譯；紀錄檔（logging）與命令列輸出維持中文，方便除錯。
"""

from __future__ import annotations

import json
import locale
import logging
import sys
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

SOURCE_LANGUAGE = "zh_TW"
AUTO = ""   # 設定檔中的「跟隨系統」


@dataclass(frozen=True)
class Language:
    """一種介面語言。

    Attributes:
        code (str): 語言代碼（也是 locales 裡的檔名）。
        native_name (str): 用該語言本身寫的名稱（語言選單一律這樣顯示，看不懂目前語言的人也找得到）。
    """

    code: str
    native_name: str


LANGUAGES: tuple[Language, ...] = (
    Language("zh_TW", "繁體中文"),
    Language("en", "English"),
    Language("ja", "日本語"),
    Language("ko", "한국어"),
)
LANGUAGE_CODES: tuple[str, ...] = tuple(lang.code for lang in LANGUAGES)

_catalog: dict[str, str] = {}
_current = SOURCE_LANGUAGE


def locales_dir() -> Path:
    """翻譯檔所在的資料夾（exe 版位於 PyInstaller 的解壓縮暫存資料夾）。

    Returns:
        Path: ``soundboard/locales`` 資料夾。
    """
    base = getattr(sys, "_MEIPASS", None)
    if base is not None:
        return Path(base) / "soundboard" / "locales"
    return Path(__file__).resolve().parent / "locales"


def match_language(tag: str | None) -> str:
    """把系統的語言標籤（例如 ``ja_JP``、``ko-KR``、``zh-Hant-TW``）對應到支援的介面語言。

    Args:
        tag (str | None): 語言標籤；None 或空字串視為未知。

    Returns:
        str: 支援的語言代碼；中文（含簡體）用繁體中文，其他不支援的語言用英文。
    """
    if not tag:
        return SOURCE_LANGUAGE
    tag = tag.replace("-", "_").lower()
    if tag.startswith("zh") or tag.startswith("chinese"):
        return "zh_TW"
    if tag.startswith("ja") or tag.startswith("japanese"):
        return "ja"
    if tag.startswith("ko") or tag.startswith("korean"):
        return "ko"
    return "en"


def detect_system_language() -> str:
    """讀取 Windows 的顯示語言，決定「跟隨系統」時使用的介面語言。

    Returns:
        str: 支援的語言代碼。
    """
    if sys.platform == "win32":
        try:
            import ctypes

            # 用「顯示語言」而不是地區格式：使用者可能是英文介面、但日期格式設成台灣
            lang_id = ctypes.windll.kernel32.GetUserDefaultUILanguage()
            return match_language(locale.windows_locale.get(lang_id))
        except (AttributeError, OSError):
            pass
    return match_language(locale.getlocale()[0])


def load_catalog(code: str, directory: Path | None = None) -> dict[str, str]:
    """讀取一種語言的翻譯檔。

    Args:
        code (str): 語言代碼。
        directory (Path | None): 翻譯檔資料夾；None 用 :func:`locales_dir`。

    Returns:
        dict[str, str]: 原文 → 翻譯；繁體中文或讀取失敗時為空字典（顯示原文）。
    """
    if code == SOURCE_LANGUAGE:
        return {}
    path = (directory or locales_dir()) / f"{code}.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("無法讀取翻譯檔 %s：%s", path, exc)
        return {}
    # 空字串的翻譯視為尚未翻譯（顯示原文），避免畫面出現空白按鈕
    return {k: v for k, v in raw.items() if isinstance(k, str) and isinstance(v, str) and v}


def set_language(code: str, directory: Path | None = None) -> str:
    """設定介面語言（必須在建立任何介面元件之前呼叫）。

    Args:
        code (str): 語言代碼；:data:`AUTO` 或不支援的代碼會改用系統語言。
        directory (Path | None): 翻譯檔資料夾（測試用）。

    Returns:
        str: 實際使用的語言代碼。
    """
    global _catalog, _current
    if code not in LANGUAGE_CODES:
        code = detect_system_language()
    _catalog = load_catalog(code, directory)
    _current = code
    return code


def current_language() -> str:
    """目前的介面語言代碼。

    Returns:
        str: 語言代碼。
    """
    return _current


def N_(text: str) -> str:
    """標記「之後才翻譯」的文字（模組層級的常數用）。

    模組載入時還沒決定語言，所以常數本身存原文，顯示時再 ``tr(常數)``；
    這個函式只讓 ``packaging/i18n_sync.py`` 找得到要翻譯的文字。

    Args:
        text (str): 繁體中文原文。

    Returns:
        str: 原文（不翻譯）。
    """
    return text


def tr(text: str, /, **values: object) -> str:
    """翻譯一段介面文字。

    Args:
        text (str): 繁體中文原文（同時是翻譯檔的 key）。
        **values (object): 填入 ``{name}`` 佔位符的值。

    Returns:
        str: 目前語言的文字。
    """
    translated = _catalog.get(text, text)
    if not values:
        return translated
    try:
        return translated.format(**values)
    except (KeyError, IndexError, ValueError):
        # 翻譯檔的佔位符打錯時退回原文，至少不會讓介面當掉
        logger.warning("翻譯的佔位符不正確：%r", translated)
        return text.format(**values)
