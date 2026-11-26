"""貼圖的附加資訊：LINE 貼圖下載器寫的 ``line_meta.json``（文字貼圖、預設文字）與使用者自訂的關鍵字。

LINE App 的「貼圖建議」用的是每張貼圖的隱藏標籤，但這份資料只在登入的 LINE App 裡，
LINE Store 與 CDN 都沒有公開。所以這裡的關鍵字來源是：
    * 訊息貼圖的預設文字（LINE 公開的 productInfo.meta 裡有）
    * 系列資料夾裡的 ``sticker_keywords.json``：每張貼圖「圖上的字」（``t``）與關鍵字（``k``），
      由 ``tools/sticker_keywords`` 看總覽圖產生，格式與貼圖鍵盤 App 的 ``tools/keywords/<組id>.json`` 相同
    * 使用者在貼圖上按右鍵「🏷 編輯關鍵字…」自己加的字詞（存在貼圖資料夾的 ``keywords.json``）

搜尋時依「比對的緊密程度」打分數排序（完全相同 > 結尾相同 > 開頭相同 > 包含），
符合圖上的字再加分，讓真的寫著這句話的貼圖排在前面。

這個模組不依賴 Tk，方便單獨測試。
"""

from __future__ import annotations

import json
import logging
import os
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

META_FILENAME = "line_meta.json"
KEYWORDS_FILENAME = "keywords.json"
TAGS_FILENAME = "sticker_keywords.json"
AUX_DIR_NAME = ".line"
# 關鍵字之間的分隔：中英文逗號、頓號、分號、換行與空白
_KEYWORD_SPLIT = re.compile(r"[,，、;；\s]+")
# 「關鍵字被包含在輸入的字裡」也算符合，但太短的關鍵字（一個字）會到處符合，只接受完全相同
_MIN_CONTAINED_KEYWORD = 2


@dataclass(frozen=True)
class TextSpec:
    """可自訂文字的貼圖（LINE 的訊息貼圖或「隨你填」名字貼圖）。

    Attributes:
        kind (str): ``message``（訊息貼圖，LINE 伺服器可代為畫字）或 ``name``（名字貼圖，在本機畫字）。
        base (Path): 沒有文字的底圖。
        sample (Path): 顯示用的貼圖（訊息貼圖疊上預設文字；名字貼圖的名字位置是「****」）。
        overlay (Path | None): 訊息貼圖的預設文字圖層（用來判斷文字的位置與顏色）。
        api (str): 訊息貼圖的文字 API（網址後面接上文字即可取得文字圖層）；沒有時為空字串。
        referer (str): 呼叫 API 時要帶的商品頁網址。
        default_text (str): 預設文字。
    """

    kind: str
    base: Path
    sample: Path
    overlay: Path | None = None
    api: str = ""
    referer: str = ""
    default_text: str = ""


@dataclass(frozen=True)
class StickerMeta:
    """一張貼圖在 ``line_meta.json`` 裡的資訊。

    Attributes:
        sticker_id (str): LINE 的貼圖 ID。
        default_text (str): 預設文字（只有訊息貼圖有）。
        text (TextSpec | None): 可自訂文字時的資訊。
    """

    sticker_id: str = ""
    default_text: str = ""
    text: TextSpec | None = None


def load_series_meta(folder: Path) -> dict[str, StickerMeta]:
    """讀取系列資料夾裡的 ``line_meta.json``。

    Args:
        folder (Path): 系列資料夾。

    Returns:
        dict[str, StickerMeta]: 檔名主檔名（例如 ``001``）→ 資訊；沒有檔案或格式不對時為空 dict。
    """
    path = folder / META_FILENAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        logger.warning("無法讀取 %s：%s", path, exc)
        return {}
    if not isinstance(data, dict) or not isinstance(data.get("stickers"), list):
        return {}
    referer = str(data.get("store_url") or "")
    result: dict[str, StickerMeta] = {}
    for entry in data["stickers"]:
        if not isinstance(entry, dict) or not entry.get("stem"):
            continue
        stem = str(entry["stem"])
        default_text = str(entry.get("default_text") or "")
        text = None
        raw = entry.get("text")
        if isinstance(raw, dict) and raw.get("kind") in ("message", "name") and raw.get("base"):
            text = TextSpec(
                kind=raw["kind"],
                base=folder / str(raw["base"]),
                sample=folder / f"{stem}.png",
                overlay=folder / str(raw["overlay"]) if raw.get("overlay") else None,
                api=str(raw.get("api") or ""),
                referer=referer,
                default_text=default_text,
            )
        result[stem] = StickerMeta(sticker_id=str(entry.get("id") or ""), default_text=default_text, text=text)
    return result


# ====================================================================== 關鍵字
def parse_keywords(text: str) -> list[str]:
    """把使用者輸入的關鍵字字串拆成清單（去掉空白與重複，保留順序）。

    Args:
        text (str): 例如 ``"早安, 你好、謝謝"``。

    Returns:
        list[str]: 關鍵字。
    """
    seen: set[str] = set()
    words: list[str] = []
    for word in _KEYWORD_SPLIT.split(text or ""):
        word = word.strip()
        if word and word.casefold() not in seen:
            seen.add(word.casefold())
            words.append(word)
    return words


@dataclass(frozen=True)
class StickerTags:
    """一張貼圖在 ``sticker_keywords.json`` 裡的文字。

    Attributes:
        text (str): 圖上寫的字（沒有字時為空字串）。
        keywords (tuple[str, ...]): 關鍵字：同義詞、白話翻譯、英文、情緒或場合。
    """

    text: str = ""
    keywords: tuple[str, ...] = ()


def load_series_tags(folder: Path) -> dict[str, StickerTags]:
    """讀取系列資料夾裡的 ``sticker_keywords.json``。

    格式：``{"001": {"t": "圖上的字", "k": ["關鍵字", ...]}, ...}``；值直接是清單時視為只有關鍵字。

    Args:
        folder (Path): 系列資料夾。

    Returns:
        dict[str, StickerTags]: 檔名主檔名（例如 ``001``）→ 文字；沒有檔案或格式不對時為空 dict。
    """
    path = folder / TAGS_FILENAME
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        logger.warning("無法讀取 %s：%s", path, exc)
        return {}
    if not isinstance(data, dict):
        return {}
    result: dict[str, StickerTags] = {}
    for stem, entry in data.items():
        if isinstance(entry, list):
            entry = {"k": entry}
        if not isinstance(entry, dict):
            continue
        words = entry.get("k") or []
        keywords = tuple(str(w).strip() for w in words if str(w).strip()) if isinstance(words, list) else ()
        result[str(stem)] = StickerTags(text=str(entry.get("t") or "").strip(), keywords=keywords)
    return result


def normalize_term(text: str) -> str:
    """比對前的整理：去掉標點、符號與空白，英文轉小寫（``OK``、``ok``、``Ok!`` 都一樣）。

    Args:
        text (str): 搜尋文字或關鍵字。

    Returns:
        str: 整理後的文字。
    """
    # 貼圖上的字常帶「~」「！」「★」，使用者打字時通常不會打，比對時一律忽略
    return "".join(ch for ch in text.casefold() if unicodedata.category(ch)[0] not in "PSZC")


@dataclass(frozen=True)
class SearchTerms:
    """一張貼圖整理好、可以直接比對的文字（每次打字都要比對幾千張，先整理好比較快）。

    Attributes:
        text (str): 圖上的字（已整理）。
        keywords (tuple[str, ...]): 關鍵字（已整理）。
    """

    text: str = ""
    keywords: tuple[str, ...] = ()

    @classmethod
    def build(cls, text: str, keywords: tuple[str, ...] | list[str]) -> SearchTerms:
        """從原始文字建立。

        Args:
            text (str): 圖上的字。
            keywords (tuple[str, ...] | list[str]): 關鍵字。

        Returns:
            SearchTerms: 整理好的文字（空字串與重複會去掉）。
        """
        normalized = dict.fromkeys(w for w in (normalize_term(k) for k in keywords) if w)
        return cls(normalize_term(text), tuple(normalized))


# 圖上的字比中時的加分：讓「圖上真的寫這句話」的貼圖排在只有同義詞的前面
_TEXT_BONUS = 10


def term_score(query: str, term: str) -> int:
    """一個關鍵字的分數（兩者都要先用 :func:`normalize_term` 整理過）。

    ============================================  ===================  ==========================
    情況                                          分數                 例子
    ============================================  ===================  ==========================
    完全相同                                      100                  打「晚安」，關鍵字「晚安」
    打的字以關鍵字結尾（關鍵字至少 2 字）         60 + 關鍵字字數×2    打「好啦再見」，關鍵字「再見」
    關鍵字以打的字開頭（打的字至少 2 字）         50                   打「再見」，關鍵字「再見啦」
    關鍵字包含打的字（打的字至少 2 字）           40                   打「放假」，關鍵字「等待放假」
    打的字包含關鍵字（關鍵字至少 2 字）           30 + 關鍵字字數×2    打「今天好累喔」，關鍵字「好累」
    ============================================  ===================  ==========================

    Args:
        query (str): 整理過的搜尋文字。
        term (str): 整理過的關鍵字。

    Returns:
        int: 分數；不符合時為 0。
    """
    if not query or not term:
        return 0
    if query == term:
        return 100
    # 只打一個字時必須完全相同，否則幾千張貼圖裡會跳出一大堆不相關的
    long_term = len(term) >= _MIN_CONTAINED_KEYWORD
    long_query = len(query) >= _MIN_CONTAINED_KEYWORD
    if long_term and query.endswith(term):
        return 60 + 2 * len(term)
    if long_query and term.startswith(query):
        return 50
    if long_query and query in term:
        return 40
    if long_term and term in query:
        return 30 + 2 * len(term)
    return 0


def match_score(query: str, terms: SearchTerms) -> int:
    """一張貼圖對搜尋文字的分數：取圖上的字與每個關鍵字的最高分，比中圖上的字再加分。

    Args:
        query (str): 整理過的搜尋文字（:func:`normalize_term`）。
        terms (SearchTerms): 貼圖的文字。

    Returns:
        int: 分數；0 表示不符合。
    """
    best = max((term_score(query, k) for k in terms.keywords), default=0)
    on_image = term_score(query, terms.text)
    if on_image:
        best = max(best, on_image + _TEXT_BONUS)
    return best


def keyword_matches(query: str, keywords: tuple[str, ...] | list[str]) -> bool:
    """搜尋文字是否觸發關鍵字（仿 LINE 的貼圖建議：打「早安」會出現標了「早安」的貼圖）。

    規則見 :func:`term_score`；比對前會去掉標點與空白、英文不分大小寫。

    Args:
        query (str): 搜尋框的文字。
        keywords (tuple[str, ...] | list[str]): 貼圖的關鍵字。

    Returns:
        bool: 是否符合。
    """
    return match_score(normalize_term(query), SearchTerms.build("", keywords)) > 0


@dataclass
class KeywordStore:
    """使用者自訂的貼圖關鍵字，存在貼圖資料夾的 ``keywords.json``（跟著貼圖走，換電腦也能用）。

    Attributes:
        root (Path): 貼圖資料夾。
        words (dict[str, list[str]]): 貼圖 key（相對路徑）→ 關鍵字。
    """

    root: Path
    words: dict[str, list[str]] = field(default_factory=dict)

    @property
    def path(self) -> Path:
        """``keywords.json`` 的完整路徑。"""
        return self.root / KEYWORDS_FILENAME

    @classmethod
    def load(cls, root: Path) -> KeywordStore:
        """讀取關鍵字檔；不存在或格式不對時回傳空的。

        Args:
            root (Path): 貼圖資料夾。

        Returns:
            KeywordStore: 關鍵字。
        """
        store = cls(root)
        try:
            data = json.loads(store.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return store
        except (OSError, ValueError) as exc:
            logger.warning("無法讀取 %s：%s", store.path, exc)
            return store
        if isinstance(data, dict):
            store.words = {str(k): [str(w) for w in v if str(w).strip()]
                           for k, v in data.items() if isinstance(v, list)}
        return store

    def get(self, key: str) -> list[str]:
        """取得一張貼圖的自訂關鍵字。"""
        return list(self.words.get(key, []))

    def set(self, key: str, words: list[str]) -> None:
        """設定一張貼圖的自訂關鍵字並存檔（空清單表示刪除）。

        Args:
            key (str): 貼圖 key。
            words (list[str]): 關鍵字。

        Raises:
            OSError: 無法寫入檔案時拋出。
        """
        if words:
            self.words[key] = list(words)
        else:
            self.words.pop(key, None)
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.part")
        tmp.write_text(json.dumps(self.words, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)
