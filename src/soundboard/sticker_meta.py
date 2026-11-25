"""貼圖的附加資訊：LINE 貼圖下載器寫的 ``line_meta.json``（文字貼圖、預設文字）與使用者自訂的關鍵字。

LINE App 的「貼圖建議」用的是每張貼圖的隱藏標籤，但這份資料只在登入的 LINE App 裡，
LINE Store 與 CDN 都沒有公開。所以這裡的關鍵字來源是：
    * 訊息貼圖的預設文字（LINE 公開的 productInfo.meta 裡有）
    * 使用者在貼圖上按右鍵「🏷 編輯關鍵字…」自己加的字詞（存在貼圖資料夾的 ``keywords.json``）

這個模組不依賴 Tk，方便單獨測試。
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

META_FILENAME = "line_meta.json"
KEYWORDS_FILENAME = "keywords.json"
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


def keyword_matches(query: str, keywords: tuple[str, ...] | list[str]) -> bool:
    """搜尋文字是否觸發關鍵字（仿 LINE 的貼圖建議：打「早安」會出現標了「早安」的貼圖）。

    符合的條件（不分大小寫）：關鍵字包含搜尋文字（打「早」也會出現「早安」），
    或搜尋文字包含兩個字以上的關鍵字（打「大家早安」也會出現「早安」）。

    Args:
        query (str): 搜尋框的文字。
        keywords (tuple[str, ...] | list[str]): 貼圖的關鍵字。

    Returns:
        bool: 是否符合。
    """
    q = query.strip().casefold()
    if not q:
        return False
    for word in keywords:
        w = word.casefold()
        if q in w or (len(w) >= _MIN_CONTAINED_KEYWORD and w in q):
            return True
    return False


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
