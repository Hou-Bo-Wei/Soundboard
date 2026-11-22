"""全域常數與設定檔讀寫。"""

from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import asdict
from pathlib import Path

from soundboard.models import AppSettings

logger = logging.getLogger(__name__)

# 所有音效都在載入時轉成這個取樣率與聲道數，混音時就不必再轉換。
# 48 kHz 是 Discord 與多數音效卡的原生取樣率，可避免系統再重新取樣。
SAMPLE_RATE: int = 48_000
CHANNELS: int = 2

# 麥克風直通緩衝的最大延遲（秒）。輸入與輸出是不同裝置、時脈會飄移，
# 超過這個量就丟掉舊資料，避免朋友聽到的聲音越來越延遲。
PASSTHROUGH_MAX_LATENCY: float = 0.08

# 音效快取上限（bytes）。float32 立體聲 48 kHz 一分鐘約 23 MB。
CACHE_LIMIT_BYTES: int = 512 * 1024 * 1024

# 虛擬音源線常見的播放端名稱關鍵字，用來自動選擇「虛擬麥克風輸出」。
VIRTUAL_CABLE_KEYWORDS: tuple[str, ...] = (
    "CABLE Input",
    "CABLE In ",
    "VB-Audio",
    "VoiceMeeter Input",
    "Voicemeeter Input",
)

# libsndfile 原生支援的格式（不需要 ffmpeg）。
SOUNDFILE_EXTENSIONS: frozenset[str] = frozenset(
    {".wav", ".ogg", ".oga", ".opus", ".mp3", ".flac", ".aif", ".aiff", ".au", ".caf", ".w64"}
)
# 需要 ffmpeg 才能解碼的格式。
FFMPEG_EXTENSIONS: frozenset[str] = frozenset(
    {".m4a", ".aac", ".wma", ".webm", ".mp4", ".mka", ".amr", ".ac3", ".mp2", ".3gp"}
)

PREFERRED_HOST_API: str = "Windows WASAPI"

# 音量滑桿上限（百分比）。超過 100% 時由引擎的柔性限幅避免爆音。
MAX_VOLUME_PERCENT: int = 200

# 柔性限幅的起始門檻：低於此值的訊號完全不受影響。
LIMITER_THRESHOLD: float = 0.8


def is_frozen() -> bool:
    """是否為 PyInstaller 打包後的執行檔。

    Returns:
        bool: 以 exe 執行時為 True。
    """
    return bool(getattr(sys, "frozen", False))


def resource_path(name: str) -> Path:
    """取得隨程式一起打包的資源檔路徑。

    Args:
        name (str): 資源檔名。

    Returns:
        Path: 資源檔完整路徑（打包後位於 PyInstaller 的解壓縮暫存資料夾）。
    """
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / name


def default_sound_dir() -> Path:
    """第一次執行時的預設音效資料夾。

    exe 版以執行檔所在位置為準（從捷徑啟動時工作目錄可能不同），
    若旁邊有「音效」資料夾就用它，方便打包分享給朋友。

    Returns:
        Path: 預設音效資料夾。
    """
    if not is_frozen():
        return Path.cwd()
    exe_dir = Path(sys.executable).resolve().parent
    bundled = exe_dir / "音效"
    return bundled if bundled.is_dir() else exe_dir


def log_path() -> Path:
    """錯誤紀錄檔路徑（exe 版沒有主控台，訊息寫到這裡）。

    Returns:
        Path: soundboard.log 的完整路徑。
    """
    return settings_path().with_name("soundboard.log")


def settings_path() -> Path:
    """取得設定檔路徑。

    可用環境變數 ``SOUNDBOARD_SETTINGS`` 覆寫，否則放在 ``%APPDATA%/Soundboard``。

    Returns:
        Path: settings.json 的完整路徑。
    """
    override = os.environ.get("SOUNDBOARD_SETTINGS")
    if override:
        return Path(override)
    base = Path(os.environ.get("APPDATA", Path.home()))
    return base / "Soundboard" / "settings.json"


def load_settings(path: Path | None = None) -> AppSettings:
    """讀取設定檔，檔案不存在或損毀時回傳預設值。

    Args:
        path (Path | None): 設定檔路徑，預設為 :func:`settings_path`。

    Returns:
        AppSettings: 讀到的設定。
    """
    path = path or settings_path()
    if not path.exists():
        return AppSettings(sound_dir=str(default_sound_dir()))
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return AppSettings.from_dict(raw)
    except (OSError, ValueError, TypeError) as exc:
        # 設定壞掉不應讓程式無法啟動，退回預設值即可
        logger.warning("設定檔讀取失敗，改用預設值：%s", exc)
        return AppSettings(sound_dir=str(default_sound_dir()))


def save_settings(settings: AppSettings, path: Path | None = None) -> None:
    """將設定寫回設定檔。

    Args:
        settings (AppSettings): 要儲存的設定。
        path (Path | None): 設定檔路徑，預設為 :func:`settings_path`。

    Raises:
        OSError: 無法寫入檔案時拋出。
    """
    path = path or settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # 先寫暫存檔再改名，避免寫到一半當機造成設定檔損毀
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(asdict(settings), ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
