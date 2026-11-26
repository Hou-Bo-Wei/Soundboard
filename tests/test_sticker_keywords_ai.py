"""AI 關鍵字（tools/sticker_keywords/keyword_ai.py）：請求內容、解析回應、批次補關鍵字、金鑰加密。

全部使用假的 Claude 用戶端，不連網、不產生費用。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("PIL")
from PIL import Image  # noqa: E402

TOOL_DIR = Path(__file__).resolve().parents[1] / "tools" / "sticker_keywords"
sys.path.insert(0, str(TOOL_DIR))
import keyword_ai  # noqa: E402
import sticker_keywords as sk  # noqa: E402


def _series(root: Path, name: str, count: int) -> Path:
    folder = root / name
    folder.mkdir(parents=True)
    for i in range(1, count + 1):
        Image.new("RGBA", (60, 60), (255, 0, 0, 255)).save(folder / f"{i:03d}.png")
    return folder


def _reply(items: list[dict], *, stop: str = "end_turn", model: str = "claude-sonnet-5-5"):
    text = json.dumps({"stickers": items}, ensure_ascii=False)
    return SimpleNamespace(stop_reason=stop, model=model,
                           content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=text)],
                           usage=SimpleNamespace(input_tokens=2500, output_tokens=3000))


class FakeClient:
    """記錄請求、依序回傳預先準備好的回應（或例外）。"""

    def __init__(self, *replies) -> None:
        self.requests: list[dict] = []
        self._replies = list(replies)
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **request):
        self.requests.append(request)
        reply = self._replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


# ---------------------------------------------------------------- 請求
def test_build_request_sonnet_uses_fallback_and_schema(tmp_path: Path) -> None:
    series = sk.load_series(_series(tmp_path, "早安組_111", 3))
    req = keyword_ai.build_request(series, "claude-sonnet-5-5")
    assert req["model"] == "claude-sonnet-5-5"
    assert req["fallbacks"] == "default" and req["betas"] == ["server-side-fallback-2026-07-01"]
    assert req["output_config"]["effort"] == "low"
    assert req["output_config"]["format"]["type"] == "json_schema"
    content = req["messages"][0]["content"]
    assert [c["type"] for c in content] == ["image", "text"]
    assert content[0]["source"]["media_type"] == "image/jpeg"
    assert "001, 002, 003" in content[1]["text"] and "早安組" in content[1]["text"]


def test_build_request_haiku_has_no_fallback_and_pages(tmp_path: Path) -> None:
    series = sk.load_series(_series(tmp_path, "大組_222", 45))     # 超過 42 張：兩張總覽圖
    req = keyword_ai.build_request(series, "claude-haiku-5-5")
    assert "fallbacks" not in req and "betas" not in req
    assert sum(1 for c in req["messages"][0]["content"] if c["type"] == "image") == 2


# ---------------------------------------------------------------- 回應
def test_parse_response_pads_ids_and_reports_missing() -> None:
    reply = _reply([{"id": "1", "t": " 早安~ ", "k": ["早安", "早安", " ", "morning"]},
                    {"id": "002", "t": "", "k": []},                 # 沒有關鍵字的不算
                    {"id": "999", "t": "", "k": ["多的"]}])          # 不在清單裡的忽略
    data, missing = keyword_ai.parse_response(reply, ["001", "002", "003"])
    assert data == {"001": {"t": "早安~", "k": ["早安", "morning"]}}
    assert missing == ["002", "003"]


@pytest.mark.parametrize("stop", ["refusal", "max_tokens"])
def test_parse_response_rejects_bad_stop(stop: str) -> None:
    with pytest.raises(keyword_ai.KeywordAIError):
        keyword_ai.parse_response(_reply([], stop=stop), ["001"])


def test_parse_response_rejects_bad_json() -> None:
    reply = SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text="不是 JSON")])
    with pytest.raises(keyword_ai.KeywordAIError):
        keyword_ai.parse_response(reply, ["001"])


def test_generate_reports_cost(tmp_path: Path) -> None:
    series = sk.load_series(_series(tmp_path, "組_1", 2))
    client = FakeClient(_reply([{"id": "001", "t": "嗨", "k": ["你好", "hi"]},
                                {"id": "002", "t": "", "k": ["生氣"]}]))
    result = keyword_ai.generate_for_series(series, client=client)
    assert result.missing == [] and result.model == "claude-sonnet-5-5"
    # Sonnet 5.5：輸入 $2、輸出 $10 / 百萬 token
    assert result.cost == pytest.approx((2500 * 2 + 3000 * 10) / 1_000_000)


# ---------------------------------------------------------------- 批次
def test_fill_keywords_only_missing_and_keeps_existing(tmp_path: Path) -> None:
    done_folder = _series(tmp_path, "作者_9/已完成_1", 1)
    sk.write_keywords(done_folder, {"001": {"t": "", "k": ["舊的"]}})
    partial = _series(tmp_path, "作者_9/一半_2", 2)
    sk.write_keywords(partial, {"001": {"t": "手改", "k": ["手改的"]}})
    client = FakeClient(_reply([{"id": "001", "t": "AI", "k": ["AI 的"]}, {"id": "002", "t": "", "k": ["新的"]}]))
    logs: list[str] = []

    done, cost = keyword_ai.fill_keywords([tmp_path / "作者_9"], keyword_ai.AISettings(api_key="k"),
                                          log=logs.append, client=client)
    assert done == 1 and cost > 0
    assert len(client.requests) == 1                       # 已經完整的系列不送出
    data = json.loads((partial / sk.TAGS_FILENAME).read_text(encoding="utf-8"))
    assert data["001"] == {"t": "手改", "k": ["手改的"]}     # 手改過的保留
    assert data["002"]["k"] == ["新的"]
    assert any("預估" in line for line in logs)


def test_fill_keywords_stops_on_fatal_error(tmp_path: Path) -> None:
    for name in ("a_1", "b_2", "c_3"):
        _series(tmp_path, name, 1)
    client = FakeClient(keyword_ai.KeywordAIError("API 金鑰無效", fatal=True))
    done, _ = keyword_ai.fill_keywords([tmp_path], keyword_ai.AISettings(api_key="k"), log=lambda m: None,
                                       client=client)
    assert done == 0 and len(client.requests) == 1        # 金鑰錯誤時不再試其他組


def test_fill_keywords_without_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    _series(tmp_path, "a_1", 1)
    logs: list[str] = []
    assert keyword_ai.fill_keywords([tmp_path], keyword_ai.AISettings(), log=logs.append) == (0, 0.0)
    assert any("金鑰" in line for line in logs)


def test_plan_estimates_cost(tmp_path: Path) -> None:
    _series(tmp_path, "a_1", 40)
    todo = keyword_ai.plan([tmp_path], "claude-sonnet-5-5")
    assert len(todo.series) == 1 and todo.stickers == 40
    assert 0.02 < todo.estimate < 0.06                     # 一組 40 張約 3～5 美分
    haiku = keyword_ai.plan([tmp_path], "claude-haiku-5-5")
    assert haiku.estimate < todo.estimate / 10


# ---------------------------------------------------------------- 設定
def test_settings_roundtrip_hides_key(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    keyword_ai.AISettings(api_key="sk-ant-test-123", model="claude-haiku-5-5", auto=True).save(path)
    raw = path.read_text(encoding="utf-8")
    assert "sk-ant-test-123" not in raw                    # 不以明文存檔
    loaded = keyword_ai.AISettings.load(path)
    assert (loaded.api_key, loaded.model, loaded.auto) == ("sk-ant-test-123", "claude-haiku-5-5", True)


def test_settings_broken_file_and_env_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"api_key_protected": "dpapi:壞掉", "model": ""}), encoding="utf-8")
    loaded = keyword_ai.AISettings.load(path)
    assert loaded.api_key == "" and loaded.model == keyword_ai.DEFAULT_MODEL
    monkeypatch.setenv("ANTHROPIC_API_KEY", "from-env")
    assert loaded.effective_key == "from-env"


def test_check_key_uses_free_models_endpoint() -> None:
    client = SimpleNamespace(models=SimpleNamespace(retrieve=lambda model: SimpleNamespace(display_name="Claude Sonnet 5.5")))
    assert keyword_ai.check_key("k", client=client) == "Claude Sonnet 5.5"
