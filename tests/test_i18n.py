"""多語言（i18n）與版本資訊測試。"""

from __future__ import annotations

import json
import string
import sys
from pathlib import Path

import pytest

from soundboard import i18n
from soundboard.version import display_version

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "packaging"))
import i18n_sync  # noqa: E402


@pytest.fixture(autouse=True)
def _restore_language():
    yield
    i18n.set_language("zh_TW")


def test_match_language() -> None:
    assert i18n.match_language("zh_TW") == "zh_TW"
    assert i18n.match_language("zh-Hans-CN") == "zh_TW"
    assert i18n.match_language("ja_JP") == "ja"
    assert i18n.match_language("ko-KR") == "ko"
    assert i18n.match_language("de_DE") == "en"
    assert i18n.match_language(None) == "zh_TW"


def test_tr_uses_catalog_and_falls_back(tmp_path: Path) -> None:
    (tmp_path / "en.json").write_text(json.dumps({
        "已載入 {count} 個音效": "Loaded {count} sounds",
        "壞掉": "Broken {oops}",
        "空": "",
    }), encoding="utf-8")
    assert i18n.set_language("en", tmp_path) == "en"
    assert i18n.tr("已載入 {count} 個音效", count=3) == "Loaded 3 sounds"
    assert i18n.tr("沒有翻譯的文字") == "沒有翻譯的文字"
    # 佔位符打錯：退回原文；空字串：視為未翻譯
    assert i18n.tr("壞掉", count=1) == "壞掉"
    assert i18n.tr("空") == "空"
    i18n.set_language("zh_TW", tmp_path)
    assert i18n.tr("已載入 {count} 個音效", count=3) == "已載入 3 個音效"


def test_unknown_language_falls_back_to_system() -> None:
    assert i18n.set_language("xx") in i18n.LANGUAGE_CODES


def test_display_version() -> None:
    assert display_version("0.1.0") == "0.1"
    assert display_version("1.2.3") == "1.2.3"
    assert display_version("2.0.0") == "2.0"


@pytest.mark.parametrize("code", i18n_sync.TARGET_LANGUAGES)
def test_locale_files_are_complete(code: str) -> None:
    """每段介面文字都要有翻譯，佔位符也要與原文一致（否則該語言會顯示中文或格式錯誤）。"""
    problems = i18n_sync.problems(i18n_sync.extract(), code)
    assert not problems, "\n".join(problems[:20])


def test_no_tr_at_import_time() -> None:
    """tr() 不可在模組載入時執行（那時還沒設定語言）：檢查模組層級與類別層級的呼叫。"""
    import ast

    offenders = []
    for path in sorted((ROOT / "src" / "soundboard").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        scopes = [tree] + [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]
        for scope in scopes:
            for stmt in scope.body:
                if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    # 函式預設參數也是載入時執行
                    if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        nodes = [d for d in stmt.args.defaults + stmt.args.kw_defaults if d is not None]
                    else:
                        continue
                else:
                    nodes = [stmt]
                for node in nodes:
                    for sub in ast.walk(node):
                        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name) and sub.func.id == "tr":
                            offenders.append(f"{path.name}:{sub.lineno}")
    assert not offenders, offenders


def test_placeholders_helper() -> None:
    assert i18n_sync.placeholders("{a} 與 {b:.1f}") == {"a", "b"}
    assert i18n_sync.placeholders("{{literal}}") == set()
    assert i18n_sync.placeholders("{oops") == {"<invalid>"}
    assert string  # 確認匯入可用
