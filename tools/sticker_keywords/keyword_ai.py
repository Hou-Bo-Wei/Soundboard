"""用 Claude API 看貼圖總覽圖，自動寫出每張貼圖的「圖上的字」與關鍵字。

流程與手動做法相同（見 README）：把一組貼圖拼成總覽圖 → 請 Claude 看圖寫 ``t``、``k`` →
存成系列資料夾的 ``sticker_keywords.json``。LINE 貼圖下載器下載完會呼叫這裡，也可以從指令列執行。

預設模型是 Claude Sonnet 5.5：這套貼圖（3256 張）的關鍵字就是用 Sonnet 看總覽圖寫的，
藝術字、直排字與台語、火星文的白話翻譯都讀得準，一組 40 張約 0.03～0.05 美元。
Haiku 5.5 便宜約 20 倍但還沒實測過藝術字的辨識，Opus 5.5 約貴一倍，都可以在設定裡改選。

API 金鑰以 Windows DPAPI 加密後存在 ``%APPDATA%\\LineStickerDownloader\\settings.json``
（只有同一個 Windows 帳號能解開），也可以改用環境變數 ``ANTHROPIC_API_KEY``。
"""

from __future__ import annotations

import base64
import io
import json
import os
import sys
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

import sticker_keywords as sk

DEFAULT_MODEL = "claude-sonnet-5-5"
# 拒絕回應（refusal）時由伺服器自動改用其他模型重跑；Haiku 5.5 沒有這個功能
_FALLBACK_BETA = "server-side-fallback-2026-07-01"
_FALLBACK_MODELS = frozenset({"claude-sonnet-5-5", "claude-opus-5-5"})
# 看圖讀字屬於擷取類工作，不需要深度推理；low 可省下大部分思考 token
_EFFORT = "low"
_MAX_TOKENS = 16000
_TIMEOUT_S = 300.0
# 估算費用用的 token 數：總覽圖約 1080×1260 像素 ≈ 1800 token，說明文字約 900 token；
# 每張貼圖輸出約 70 token，加上思考約 1500 token
_TOKENS_PER_SHEET = 1800
_PROMPT_TOKENS = 900
_OUTPUT_PER_STICKER = 70
_THINKING_TOKENS = 1500


@dataclass(frozen=True)
class ModelChoice:
    """設定畫面裡可選的模型。

    Attributes:
        model_id (str): API 的模型 ID。
        label (str): 顯示名稱。
        note (str): 一句話說明。
        input_price (float): 每百萬輸入 token 的美元價格。
        output_price (float): 每百萬輸出 token 的美元價格。
    """

    model_id: str
    label: str
    note: str
    input_price: float
    output_price: float

    def cost(self, input_tokens: int, output_tokens: int) -> float:
        """依 token 數計算費用（美元）。"""
        return (input_tokens * self.input_price + output_tokens * self.output_price) / 1_000_000


MODELS: tuple[ModelChoice, ...] = (
    ModelChoice("claude-sonnet-5-5", "Sonnet 5.5（建議）",
                "藝術字、直排字與台語、火星文翻譯都讀得準；一組 40 張約 US$0.03～0.05", 2.0, 10.0),
    ModelChoice("claude-haiku-5-5", "Haiku 5.5（最便宜）",
                "價格約 Sonnet 的 1/20；還沒實測過藝術字，可能較常讀錯字", 0.10, 0.50),
    ModelChoice("claude-opus-5-5", "Opus 5.5（最準確）",
                "價格約 Sonnet 的 2 倍；Sonnet 讀錯很多時再改用", 4.0, 20.0),
)


def model_choice(model_id: str) -> ModelChoice:
    """取得模型資訊；不認得的 ID 視為預設模型的價格。"""
    for choice in MODELS:
        if choice.model_id == model_id:
            return choice
    default = MODELS[0]
    return ModelChoice(model_id, model_id, "", default.input_price, default.output_price)


class KeywordAIError(Exception):
    """產生關鍵字失敗（訊息可直接顯示給使用者）。

    Attributes:
        fatal (bool): 每一組都會遇到同樣的問題（金鑰無效、餘額不足、連不上），批次處理應該直接停下。
    """

    def __init__(self, message: str, *, fatal: bool = False) -> None:
        super().__init__(message)
        self.fatal = fatal


# ====================================================================== 設定
def settings_path() -> Path:
    """設定檔位置：``%APPDATA%\\LineStickerDownloader\\settings.json``。"""
    base = os.environ.get("APPDATA") or str(Path.home() / ".config")
    return Path(base) / "LineStickerDownloader" / "settings.json"


@dataclass
class AISettings:
    """AI 關鍵字的設定。

    Attributes:
        api_key (str): Anthropic API 金鑰（存檔時加密，不以明文寫入）。
        model (str): 使用的模型 ID。
        auto (bool): 下載完是否自動產生關鍵字。
    """

    api_key: str = ""
    model: str = DEFAULT_MODEL
    auto: bool = False

    @property
    def effective_key(self) -> str:
        """實際使用的金鑰：設定裡的，沒有時用環境變數 ``ANTHROPIC_API_KEY``。"""
        return self.api_key or os.environ.get("ANTHROPIC_API_KEY", "")

    @classmethod
    def load(cls, path: Path | None = None) -> AISettings:
        """讀取設定；檔案不存在、格式不對或金鑰解不開時回傳預設值（金鑰留空）。

        Args:
            path (Path | None): 設定檔（None 表示預設位置）。

        Returns:
            AISettings: 設定。
        """
        path = path or settings_path()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        if not isinstance(data, dict):
            return cls()
        key = ""
        if data.get("api_key_protected"):
            try:
                key = unprotect(data["api_key_protected"])
            except (OSError, ValueError):
                key = ""        # 換了 Windows 帳號或電腦：解不開就當作沒設定
        return cls(api_key=key, model=str(data.get("model") or DEFAULT_MODEL), auto=bool(data.get("auto")))

    def save(self, path: Path | None = None) -> None:
        """存檔（金鑰加密）。

        Args:
            path (Path | None): 設定檔（None 表示預設位置）。

        Raises:
            OSError: 無法寫入時拋出。
        """
        path = path or settings_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {"model": self.model, "auto": self.auto,
                "api_key_protected": protect(self.api_key) if self.api_key else ""}
        tmp = path.with_suffix(".json.part")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, path)


# ---- 金鑰加密（Windows DPAPI：綁定目前的 Windows 帳號，其他帳號或電腦解不開）
def protect(text: str) -> str:
    """加密字串；非 Windows 時只做 base64（沒有加密，僅避免一眼看到）。

    Args:
        text (str): 明文。

    Returns:
        str: 加密後的 base64 字串（Windows 前綴 ``dpapi:``）。
    """
    raw = text.encode("utf-8")
    if sys.platform == "win32":
        return "dpapi:" + base64.b64encode(_dpapi(raw, encrypt=True)).decode("ascii")
    return "b64:" + base64.b64encode(raw).decode("ascii")


def unprotect(token: str) -> str:
    """解開 :func:`protect` 的結果。

    Args:
        token (str): 加密字串。

    Returns:
        str: 明文。

    Raises:
        ValueError: 格式不對時拋出。
        OSError: DPAPI 解密失敗（例如換了 Windows 帳號）時拋出。
    """
    kind, _, payload = token.partition(":")
    raw = base64.b64decode(payload)
    if kind == "dpapi":
        return _dpapi(raw, encrypt=False).decode("utf-8")
    if kind == "b64":
        return raw.decode("utf-8")
    raise ValueError("不認得的金鑰格式")


def _dpapi(data: bytes, *, encrypt: bool) -> bytes:
    """呼叫 CryptProtectData / CryptUnprotectData。"""
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    buffer = ctypes.create_string_buffer(data, len(data))
    blob_in = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    blob_out = Blob()
    func = crypt32.CryptProtectData if encrypt else crypt32.CryptUnprotectData
    # 參數：輸入、描述、額外熵、保留、提示、旗標（1 = 不顯示任何 UI）、輸出
    if not func(ctypes.byref(blob_in), None, None, None, None, 1, ctypes.byref(blob_out)):
        raise OSError("無法解開已儲存的 API 金鑰（可能換了 Windows 帳號），請重新輸入")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


# ====================================================================== 呼叫 Claude
SYSTEM_PROMPT = """你負責為 LINE 貼圖寫搜尋關鍵字，讓台灣使用者打中文字就能找到想要的貼圖。

使用者會給你一組貼圖的總覽圖：每一格是一張貼圖，格子左上角的紅色數字是貼圖編號（是我們加上的，不是貼圖內容）。
請為清單裡的每一個編號寫：

- t：貼圖上實際寫的字，照原樣、照閱讀順序（直排由上而下、由右而左）。台語、火星文、諧音都照原字寫。
  沒有字就寫空字串；名字貼圖的「****」佔位符也寫空字串。不要把紅色編號當成貼圖上的字。
- k：3～7 個台灣使用者想用這張貼圖時「可能會打的字」，繁體中文、彼此不重複、大多 2～4 個字。可以從這幾個方向想：
  * 台語、火星文、諧音的白話翻譯（跨啥小→看什麼、歹勢→不好意思、斯巴拉西→太棒了）
  * 同義詞（謝謝→感恩、謝啦、多謝）
  * 自然的話加一兩個英文（ok、hi、thanks、good night、sorry）
  * 情緒或場合，圖上沒有字的貼圖特別需要（生氣、哭哭、晚安、打招呼、吃飯、加油、無言）
  * 圖上文字的核心詞本身（t 是「哩賀~」就放「哩賀」）
  不要放系列名稱或角色名稱，除非貼圖上真的寫著。

每個編號都要有一筆，不要漏掉。字看不清楚時寫出最可能的讀法。"""

_SCHEMA = {
    "type": "object",
    "properties": {
        "stickers": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "t": {"type": "string"},
                    "k": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["id", "t", "k"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["stickers"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class GenerationResult:
    """一組貼圖的產生結果。

    Attributes:
        data (dict[str, dict]): 貼圖編號 →（``t``, ``k``）。
        missing (list[str]): Claude 漏掉的編號。
        input_tokens (int): 輸入 token 數。
        output_tokens (int): 輸出 token 數（含思考）。
        model (str): 實際回應的模型（被拒絕而改用備援模型時會不同）。
        cost (float): 估計費用（美元）。
    """

    data: dict[str, dict]
    missing: list[str]
    input_tokens: int
    output_tokens: int
    model: str
    cost: float


def _image_block(sheet) -> dict:
    """總覽圖轉成 API 的圖片區塊（JPEG：比 PNG 小很多，讀字一樣清楚）。"""
    buffer = io.BytesIO()
    sheet.convert("RGB").save(buffer, format="JPEG", quality=88)
    return {"type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg",
                       "data": base64.standard_b64encode(buffer.getvalue()).decode("ascii")}}


def build_request(series: sk.Series, model: str) -> dict:
    """組出 ``beta.messages.create`` 的參數。

    Args:
        series (sk.Series): 一組貼圖。
        model (str): 模型 ID。

    Returns:
        dict: 請求參數。
    """
    sheets = sk.render_sheets(series)
    stems = list(series.stickers)
    text = (f"這組貼圖「{series.title}」共 {len(stems)} 張，總覽圖 {len(sheets)} 張。"
            f"請為這些編號各寫一筆：{', '.join(stems)}")
    request = {
        "model": model,
        "max_tokens": _MAX_TOKENS,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": [*(_image_block(s) for s in sheets),
                                                   {"type": "text", "text": text}]}],
        "output_config": {"effort": _EFFORT, "format": {"type": "json_schema", "schema": _SCHEMA}},
    }
    if model in _FALLBACK_MODELS:
        request["betas"] = [_FALLBACK_BETA]
        request["fallbacks"] = "default"
    return request


def make_client(api_key: str):
    """建立 Anthropic 用戶端。

    Args:
        api_key (str): API 金鑰。

    Returns:
        anthropic.Anthropic: 用戶端。

    Raises:
        KeywordAIError: 沒有金鑰或沒安裝 anthropic 套件時拋出。
    """
    if not api_key:
        raise KeywordAIError("尚未設定 Claude API 金鑰", fatal=True)
    try:
        import anthropic
    except ImportError as exc:      # pragma: no cover - 依賴安裝問題
        raise KeywordAIError("需要 anthropic 套件：pip install anthropic", fatal=True) from exc
    return anthropic.Anthropic(api_key=api_key, timeout=_TIMEOUT_S)


def _friendly_error(exc: Exception) -> KeywordAIError:
    """把 SDK 的例外轉成看得懂的中文訊息（由具體到一般判斷）。"""
    import anthropic

    detail = getattr(exc, "message", "") or str(exc)
    if isinstance(exc, anthropic.AuthenticationError):
        return KeywordAIError("API 金鑰無效，請到設定重新輸入", fatal=True)
    if isinstance(exc, anthropic.PermissionDeniedError):
        return KeywordAIError(f"這把金鑰沒有權限使用此模型：{detail}", fatal=True)
    if isinstance(exc, anthropic.NotFoundError):
        return KeywordAIError(f"找不到模型，請在設定改選其他模型：{detail}", fatal=True)
    if isinstance(exc, anthropic.RateLimitError):
        return KeywordAIError("請求太頻繁或額度已用完，請稍後再試")
    if isinstance(exc, anthropic.BadRequestError):
        return KeywordAIError(f"請求被拒絕（常見原因是帳戶餘額不足）：{detail}", fatal=True)
    if isinstance(exc, anthropic.APIStatusError):
        return KeywordAIError(f"Claude 伺服器錯誤（{exc.status_code}），請稍後再試")
    if isinstance(exc, anthropic.APIConnectionError):
        return KeywordAIError("無法連線到 Claude API，請檢查網路", fatal=True)
    return KeywordAIError(str(exc))


def check_key(api_key: str, model: str = DEFAULT_MODEL, *, client=None) -> str:
    """確認金鑰可以使用指定的模型（查詢模型資訊，不產生任何費用）。

    Args:
        api_key (str): API 金鑰。
        model (str): 模型 ID。
        client: 測試時注入的假用戶端。

    Returns:
        str: 模型的顯示名稱。

    Raises:
        KeywordAIError: 金鑰無效、沒有權限或連不上時拋出。
    """
    client = client or make_client(api_key)
    try:
        info = client.models.retrieve(model)
    except Exception as exc:
        raise _friendly_error(exc) from exc
    return getattr(info, "display_name", "") or model


def parse_response(response, stems: list[str]) -> tuple[dict[str, dict], list[str]]:
    """讀出回應裡的關鍵字。

    Args:
        response: ``beta.messages.create`` 的回應。
        stems (list[str]): 這組的貼圖編號。

    Returns:
        tuple[dict[str, dict], list[str]]: 整理過的關鍵字、漏掉的編號。

    Raises:
        KeywordAIError: 被拒絕、輸出被截斷或格式不對時拋出。
    """
    reason = getattr(response, "stop_reason", "")
    if reason == "refusal":
        raise KeywordAIError("Claude 拒絕處理這組貼圖")
    if reason == "max_tokens":
        raise KeywordAIError("回應太長被截斷，請改用分頁較小的總覽圖")
    text = next((b.text for b in response.content if getattr(b, "type", "") == "text"), "")
    try:
        items = json.loads(text)["stickers"]
    except (ValueError, KeyError, TypeError) as exc:
        raise KeywordAIError("Claude 回傳的格式不對") from exc
    wanted = set(stems)
    data: dict[str, dict] = {}
    for item in items:
        stem = str(item.get("id", "")).strip()
        # 偶爾會寫成「1」而不是「001」：補零後再對
        if stem not in wanted and stem.isdigit():
            stem = next((s for s in stems if s.isdigit() and int(s) == int(stem)), stem)
        if stem not in wanted:
            continue
        words = list(dict.fromkeys(str(w).strip() for w in item.get("k", []) if str(w).strip()))
        if words:
            data[stem] = {"t": str(item.get("t") or "").strip(), "k": words}
    return data, [s for s in stems if s not in data]


def generate_for_series(series: sk.Series, *, api_key: str = "", model: str = DEFAULT_MODEL,
                        client=None) -> GenerationResult:
    """請 Claude 看一組貼圖的總覽圖，寫出每張的圖上文字與關鍵字。

    Args:
        series (sk.Series): 一組貼圖。
        api_key (str): API 金鑰（有傳入 client 時不需要）。
        model (str): 模型 ID。
        client: 測試時注入的假用戶端。

    Returns:
        GenerationResult: 關鍵字與用量。

    Raises:
        KeywordAIError: 呼叫失敗或回應不能用時拋出。
    """
    client = client or make_client(api_key)
    request = build_request(series, model)
    try:
        response = client.beta.messages.create(**request)
    except KeywordAIError:
        raise
    except Exception as exc:
        raise _friendly_error(exc) from exc
    data, missing = parse_response(response, list(series.stickers))
    usage = response.usage
    used_model = getattr(response, "model", "") or model
    input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
    output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
    cost = model_choice(used_model).cost(input_tokens, output_tokens)
    return GenerationResult(data, missing, input_tokens, output_tokens, used_model, cost)


# ====================================================================== 批次
@dataclass
class Plan:
    """要補關鍵字的系列與預估費用。

    Attributes:
        series (list[sk.Series]): 還缺關鍵字的系列。
        stickers (int): 總張數。
        estimate (float): 預估費用（美元）。
    """

    series: list[sk.Series] = field(default_factory=list)
    stickers: int = 0
    estimate: float = 0.0


def plan(folders: Iterable[Path], model: str = DEFAULT_MODEL) -> Plan:
    """找出這些資料夾底下還缺關鍵字的系列，並預估費用。

    Args:
        folders (Iterable[Path]): 系列資料夾或上層資料夾（例如作者資料夾、整個 LINE貼圖 資料夾）。
        model (str): 模型 ID（預估費用用）。

    Returns:
        Plan: 要處理的系列。
    """
    result = Plan()
    choice = model_choice(model)
    seen: set[Path] = set()
    for folder in folders:
        for series in sk.find_series(Path(folder)):
            if series.folder in seen or not sk.missing_stems(series):
                continue
            seen.add(series.folder)
            n = len(series.stickers)
            pages = -(-n // 42)
            result.series.append(series)
            result.stickers += n
            result.estimate += choice.cost(pages * _TOKENS_PER_SHEET + _PROMPT_TOKENS,
                                           n * _OUTPUT_PER_STICKER + _THINKING_TOKENS)
    return result


def fill_keywords(folders: Iterable[Path], settings: AISettings, *, log: Callable[[str], None] = print,
                  progress: Callable[[int, int], None] | None = None, stop: threading.Event | None = None,
                  client=None) -> tuple[int, float]:
    """為還缺關鍵字的系列產生關鍵字，存成各自的 ``sticker_keywords.json``。

    既有的項目（例如手動改過的）會保留，只補上缺少的。

    Args:
        folders (Iterable[Path]): 系列資料夾或上層資料夾。
        settings (AISettings): 金鑰與模型。
        log (Callable[[str], None]): 顯示進度訊息。
        progress (Callable[[int, int], None] | None): 回報（完成組數, 總組數）。
        stop (threading.Event | None): 設定後在下一組之前停止。
        client: 測試時注入的假用戶端。

    Returns:
        tuple[int, float]: 完成的組數、總費用（美元）。
    """
    todo = plan(folders, settings.model)
    if not todo.series:
        log("關鍵字：所有系列都已經有關鍵字")
        return 0, 0.0
    try:
        client = client or make_client(settings.effective_key)
    except KeywordAIError as exc:
        log(f"⚠ 關鍵字：{exc}")
        return 0, 0.0
    label = model_choice(settings.model).label
    log(f"關鍵字：用 {label} 處理 {len(todo.series)} 組、{todo.stickers} 張，預估約 US${todo.estimate:.2f}")
    done, total_cost = 0, 0.0
    for i, series in enumerate(todo.series, 1):
        if stop is not None and stop.is_set():
            log("關鍵字：已中止")
            break
        try:
            result = generate_for_series(series, model=settings.model, client=client)
        except KeywordAIError as exc:
            log(f"  ✗ {series.title}：{exc}")
            if exc.fatal:
                break       # 每一組都會遇到同樣的問題，不用繼續花時間
            continue
        sk.write_keywords(series.folder, result.data, keep_existing=True)
        done += 1
        total_cost += result.cost
        miss = f"，漏了 {len(result.missing)} 張" if result.missing else ""
        log(f"  ✓ {series.title}：{len(result.data)} 張（US${result.cost:.3f}{miss}）")
        if progress is not None:
            progress(i, len(todo.series))
    log(f"關鍵字：完成 {done} 組，共約 US${total_cost:.2f}")
    return done, total_cost


__all__ = [
    "AISettings", "DEFAULT_MODEL", "GenerationResult", "KeywordAIError", "MODELS", "ModelChoice", "Plan",
    "check_key", "fill_keywords", "generate_for_series", "model_choice", "plan", "settings_path",
]
