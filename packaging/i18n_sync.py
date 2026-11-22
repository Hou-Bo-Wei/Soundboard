"""整理翻譯檔：找出原始碼中所有 ``tr("…")`` / ``N_("…")`` 的文字，同步到 ``soundboard/locales/*.json``。

用法（在專案根目錄）::

    python packaging/i18n_sync.py              # 新增缺少的 key（值為空字串）、移除已不使用的 key，並列出統計
    python packaging/i18n_sync.py --missing ja # 以 JSON 輸出日文還沒翻譯的原文（給翻譯用）
    python packaging/i18n_sync.py --check      # 有缺漏或佔位符不一致時回傳非 0（CI／測試用）

翻譯檔是 ``{"繁體中文原文": "翻譯"}``；空字串代表尚未翻譯，程式會顯示中文原文。
"""

from __future__ import annotations

import argparse
import ast
import json
import string
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "soundboard"
LOCALES = SRC / "locales"
TARGET_LANGUAGES = ("en", "ja", "ko")
MARKERS = frozenset({"tr", "N_"})


@dataclass
class Message:
    """原始碼中的一段待翻譯文字。

    Attributes:
        text (str): 繁體中文原文。
        places (list[str]): 出現位置（``檔案:行``）。
    """

    text: str
    places: list[str] = field(default_factory=list)


def extract(src: Path = SRC) -> dict[str, Message]:
    """掃描原始碼，取出所有以字串常數呼叫 ``tr`` / ``N_`` 的文字。

    Args:
        src (Path): 套件資料夾。

    Returns:
        dict[str, Message]: 原文 → 訊息（依第一次出現的順序）。
    """
    found: dict[str, Message] = {}
    for path in sorted(src.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and node.args):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
            arg = node.args[0]
            if name in MARKERS and isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                msg = found.setdefault(arg.value, Message(arg.value))
                msg.places.append(f"{path.relative_to(src).as_posix()}:{node.lineno}")
    return found


def placeholders(text: str) -> set[str]:
    """取出 ``str.format`` 的具名佔位符。

    Args:
        text (str): 文字。

    Returns:
        set[str]: 佔位符名稱；格式錯誤時回傳 ``{"<invalid>"}``。
    """
    try:
        return {name for _, name, _, _ in string.Formatter().parse(text) if name is not None}
    except ValueError:
        return {"<invalid>"}


def load(code: str) -> dict[str, str]:
    """讀取翻譯檔（不存在時為空）。"""
    path = LOCALES / f"{code}.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def save(code: str, catalog: dict[str, str]) -> None:
    """寫入翻譯檔（UTF-8、保留中日韓文字不跳脫，方便直接閱讀與修改）。"""
    LOCALES.mkdir(parents=True, exist_ok=True)
    text = json.dumps(catalog, ensure_ascii=False, indent=1)
    (LOCALES / f"{code}.json").write_text(text + "\n", encoding="utf-8")


def problems(messages: dict[str, Message], code: str) -> list[str]:
    """列出一種語言的問題：缺少翻譯、佔位符與原文不一致。

    Args:
        messages (dict[str, Message]): 原始碼中的文字。
        code (str): 語言代碼。

    Returns:
        list[str]: 問題描述；沒有問題時為空清單。
    """
    catalog = load(code)
    found = []
    for text in messages:
        value = catalog.get(text, "")
        if not value:
            found.append(f"[{code}] 缺少翻譯：{text!r}")
        elif placeholders(value) != placeholders(text):
            found.append(f"[{code}] 佔位符不一致：{text!r} → {value!r}")
    return found


def sync(messages: dict[str, Message]) -> None:
    """把每個翻譯檔整理成「原始碼中的 key、依出現順序」，保留既有翻譯。"""
    for code in TARGET_LANGUAGES:
        old = load(code)
        save(code, {text: old.get(text, "") for text in messages})
        missing = sum(1 for text in messages if not old.get(text))
        removed = len(set(old) - set(messages))
        print(f"{code}: {len(messages)} 個 key，未翻譯 {missing}，移除已不使用 {removed}")


def main(argv: list[str] | None = None) -> int:
    """命令列進入點。

    Args:
        argv (list[str] | None): 參數。

    Returns:
        int: 結束代碼。
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--missing", metavar="LANG", help="輸出該語言還沒翻譯的原文（JSON）")
    parser.add_argument("--check", action="store_true", help="只檢查，不寫檔")
    args = parser.parse_args(argv)
    messages = extract()
    if args.missing:
        catalog = load(args.missing)
        todo = {text: "" for text in messages if not catalog.get(text)}
        sys.stdout.reconfigure(encoding="utf-8")
        print(json.dumps(todo, ensure_ascii=False, indent=1))
        return 0
    if args.check:
        found = [p for code in TARGET_LANGUAGES for p in problems(messages, code)]
        for line in found:
            print(line)
        return 1 if found else 0
    sync(messages)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
