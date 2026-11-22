"""回放片段：多音軌存檔、讀取、混音與後製（音量、降噪、修剪、淡入淡出、匯出）。

一個片段是一個資料夾::

    回放片段/20261121-224107_Discord+我的麥克風/
        clip.json        # {"version": 1, "created": "...", "tracks": [{"source": "Discord.exe", "file": "1.flac"}, ...]}
        1.flac           # 每個來源一軌（48 kHz 立體聲、16 位元無損）
        2.flac

分軌存放，編輯時才能只留想要的來源、各自調整音量與降噪。
舊版（單一 .flac 檔）的片段仍可讀取，視為只有一軌。
"""

from __future__ import annotations

import json
import logging
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np
import soundfile as sf

from soundboard.config import LIMITER_THRESHOLD
from soundboard.i18n import N_, tr
from soundboard.replay import CHANNELS, SAMPLE_RATE, source_label

logger = logging.getLogger(__name__)

META_FILE = "clip.json"
LEGACY_SUFFIX = ".flac"
# 舊版檔名無法解析出來源時使用的來源代號；它同時是顯示文字的原文，顯示時由 track_label 翻譯
LEGACY_SOURCE = N_("錄音")
EXPORT_FORMATS: dict[str, tuple[str, str | None]] = {
    ".ogg": ("OGG", "VORBIS"),
    ".mp3": ("MP3", None),
    ".wav": ("WAV", "PCM_16"),
    ".flac": ("FLAC", "PCM_16"),
}
EXPORT_CHUNK_FRAMES = SAMPLE_RATE      # 匯出時每次寫入 1 秒（見 export_audio）
_CLIP_NAME = re.compile(r"^(\d{8}-\d{6})_(.*)$")

Track = tuple[str, np.ndarray]   # (來源, (frames, 2) float32)


# ---------------------------------------------------------------- 存取
@dataclass
class ClipInfo:
    """一個已擷取的片段。

    Attributes:
        path (Path): 片段資料夾（舊版為單一檔案）。
        created (datetime): 擷取時間。
        sources (list[str]): 各音軌的來源。
        seconds (float): 長度（秒）。
    """

    path: Path
    created: datetime
    sources: list[str] = field(default_factory=list)
    seconds: float = 0.0

    @property
    def label(self) -> str:
        """來源的顯示文字，例如「Discord + 我的麥克風」。"""
        return " + ".join(track_label(s) for s in self.sources)


def track_label(source: str) -> str:
    """音軌來源的顯示名稱（舊版片段的預設來源會依介面語言顯示）。

    Args:
        source (str): 來源代號。

    Returns:
        str: 顯示名稱。
    """
    return tr(LEGACY_SOURCE) if source == LEGACY_SOURCE else source_label(source)


def clips_dir(settings_file: Path) -> Path:
    """片段存放資料夾（與設定檔放在一起）。

    Args:
        settings_file (Path): 設定檔路徑。

    Returns:
        Path: 資料夾路徑（不一定已存在）。
    """
    return settings_file.parent / "回放片段"


def _safe_name(text: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', "_", text)[:40]


def save_clip(tracks: list[Track], folder: Path, when: datetime | None = None) -> Path:
    """把擷取到的各來源音軌存成一個片段資料夾（無損 FLAC）。

    Args:
        tracks (list[Track]): (來源, 音訊) 清單，音訊等長。
        folder (Path): 片段資料夾的上層。
        when (datetime | None): 擷取時間。

    Returns:
        Path: 片段資料夾。

    Raises:
        ValueError: 沒有任何音軌時拋出。
    """
    if not tracks:
        raise ValueError(tr("沒有音軌"))
    when = when or datetime.now()
    label = _safe_name("+".join(source_label(s) for s, _a in tracks)) or "錄音"
    stem = f"{when:%Y%m%d-%H%M%S}_{label}"
    path = folder / stem
    counter = 2
    while path.exists():
        path = folder / f"{stem}-{counter}"
        counter += 1
    # 先寫到暫存資料夾再改名：寫到一半當機時，清單不會出現壞掉的片段
    temp = path.with_name(path.name + ".tmp")
    temp.mkdir(parents=True)
    meta = {"version": 1, "created": when.isoformat(timespec="seconds"), "tracks": []}
    for i, (source, audio) in enumerate(tracks, start=1):
        name = f"{i}.flac"
        sf.write(str(temp / name), np.clip(audio, -1.0, 1.0), SAMPLE_RATE, format="FLAC", subtype="PCM_16")
        meta["tracks"].append({"source": source, "file": name})
    (temp / META_FILE).write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    temp.rename(path)
    return path


def _read_meta(path: Path) -> dict | None:
    try:
        meta = json.loads((path / META_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(meta, dict) or not isinstance(meta.get("tracks"), list):
        return None
    return meta


def list_clips(folder: Path) -> list[ClipInfo]:
    """列出片段（新的在前）。

    Args:
        folder (Path): 片段資料夾的上層。

    Returns:
        list[ClipInfo]: 片段清單；讀不到的片段會略過。
    """
    if not folder.is_dir():
        return []
    clips = []
    for path in folder.iterdir():
        try:
            if path.is_dir():
                meta = _read_meta(path)
                if meta is None:
                    continue
                tracks = [t for t in meta["tracks"] if isinstance(t, dict)]
                created = datetime.fromisoformat(str(meta.get("created")))
                seconds = sf.info(str(path / tracks[0]["file"])).duration if tracks else 0.0
                clips.append(ClipInfo(path, created, [str(t.get("source", "?")) for t in tracks], seconds))
            elif path.suffix.lower() == LEGACY_SUFFIX:
                match = _CLIP_NAME.match(path.stem)
                created = datetime.strptime(match.group(1), "%Y%m%d-%H%M%S") if match else \
                    datetime.fromtimestamp(path.stat().st_mtime)
                clips.append(ClipInfo(path, created, [match.group(2) if match else LEGACY_SOURCE],
                                      sf.info(str(path)).duration))
        except (ValueError, OSError, RuntimeError, KeyError, IndexError):
            logger.debug("略過無法讀取的片段：%s", path)
    clips.sort(key=lambda c: c.created, reverse=True)
    return clips


def _read_stereo(path: Path) -> np.ndarray:
    data, _rate = sf.read(str(path), dtype="float32", always_2d=True)
    if data.shape[1] == 1:
        data = np.repeat(data, CHANNELS, axis=1)
    return np.ascontiguousarray(data[:, :CHANNELS])


def load_clip(path: Path) -> list[Track]:
    """讀取片段的所有音軌（補齊成等長）。

    Args:
        path (Path): 片段資料夾或舊版單一檔案。

    Returns:
        list[Track]: (來源, 音訊) 清單。

    Raises:
        OSError: 檔案讀不到或格式錯誤時拋出。
    """
    if path.is_file():
        match = _CLIP_NAME.match(path.stem)
        return [(match.group(2) if match else LEGACY_SOURCE, _read_stereo(path))]
    meta = _read_meta(path)
    if meta is None:
        raise OSError(tr("片段資料已損毀：{path}", path=path))
    try:
        tracks = [(str(t["source"]), _read_stereo(path / str(t["file"]))) for t in meta["tracks"]]
    except (KeyError, TypeError, RuntimeError) as exc:
        raise OSError(tr("片段資料已損毀：{error}", error=exc)) from exc
    length = max((len(a) for _s, a in tracks), default=0)
    return [(s, a if len(a) == length else np.pad(a, ((0, length - len(a)), (0, 0)))) for s, a in tracks]


def delete_clip(path: Path) -> None:
    """刪除片段（資料夾或舊版檔案）。

    Args:
        path (Path): 片段路徑。
    """
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()


# ---------------------------------------------------------------- 修剪與匯出
def trim(audio: np.ndarray, start: float, end: float) -> np.ndarray:
    """取出 ``start``–``end`` 秒之間的音訊。

    Args:
        audio (np.ndarray): (frames, 2) 音訊。
        start (float): 起點（秒）。
        end (float): 終點（秒）。

    Returns:
        np.ndarray: 修剪後的複本。

    Raises:
        ValueError: 範圍無效（起點不小於終點）時拋出。
    """
    a = max(0, int(round(start * SAMPLE_RATE)))
    b = min(len(audio), int(round(end * SAMPLE_RATE)))
    if b <= a:
        raise ValueError(tr("選取範圍為空"))
    return audio[a:b].copy()


def fade_edges(audio: np.ndarray, fade_in: float = 0.005, fade_out: float = 0.005) -> np.ndarray:
    """頭尾淡入淡出。至少保留極短的淡化，避免剪在波形中間時發出「啪」一聲。

    Args:
        audio (np.ndarray): (frames, 2) float32。
        fade_in (float): 淡入秒數。
        fade_out (float): 淡出秒數。

    Returns:
        np.ndarray: 處理後的複本。
    """
    out = audio.astype(np.float32, copy=True)
    for seconds, head in ((fade_in, True), (fade_out, False)):
        n = min(len(out) // 2, int(SAMPLE_RATE * max(seconds, 0.005)))
        if n <= 0:
            continue
        # 等功率曲線（正弦）比直線自然，淡入時不會有「突然變大」的感覺
        ramp = np.sin(np.linspace(0.0, np.pi / 2, n, dtype=np.float32))[:, None]
        if head:
            out[:n] *= ramp
        else:
            out[-n:] *= ramp[::-1]
    return out


def export_audio(audio: np.ndarray, path: Path) -> Path:
    """依副檔名存成 .ogg / .mp3 / .wav / .flac。

    Args:
        audio (np.ndarray): (frames, 2) float32。
        path (Path): 目標檔案。

    Returns:
        Path: 實際存的檔案。

    Raises:
        ValueError: 不支援的副檔名。
    """
    suffix = path.suffix.lower()
    if suffix not in EXPORT_FORMATS:
        raise ValueError(tr("不支援的格式：{suffix}", suffix=suffix))
    fmt, subtype = EXPORT_FORMATS[suffix]
    path.parent.mkdir(parents=True, exist_ok=True)
    audio = np.clip(audio, -1.0, 1.0)
    # 先寫到暫存檔再改名：寫到一半失敗時，音效資料夾不會多出壞掉的檔案
    tmp = path.with_name(f".{path.stem}.tmp{suffix}")
    try:
        with sf.SoundFile(str(tmp), "w", SAMPLE_RATE, audio.shape[1] if audio.ndim == 2 else 1,
                          format=fmt, subtype=subtype) as out:
            # 分段寫入：libsndfile 1.2 的 Vorbis／MP3 編碼器一次寫入超過約 30 秒的資料會直接讓程式崩潰
            # （原生程式錯誤，Python 攔不到），3 分鐘的回放片段整段存檔就會閃退
            for start in range(0, len(audio), EXPORT_CHUNK_FRAMES):
                out.write(audio[start:start + EXPORT_CHUNK_FRAMES])
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)
    return path


def unique_sound_path(folder: Path, stem: str, suffix: str = ".ogg") -> Path:
    """在音效資料夾中找一個不會覆蓋既有檔案的檔名。

    Args:
        folder (Path): 音效資料夾。
        stem (str): 想要的檔名（不含副檔名）。
        suffix (str): 副檔名。

    Returns:
        Path: 可用的檔案路徑。
    """
    path = folder / f"{stem}{suffix}"
    counter = 2
    while path.exists():
        path = folder / f"{stem} ({counter}){suffix}"
        counter += 1
    return path


def waveform_peaks(audio: np.ndarray, columns: int) -> np.ndarray:
    """把音訊縮成每欄一個峰值，用來畫波形。

    Args:
        audio (np.ndarray): (frames, 2) 音訊。
        columns (int): 欄數（通常是畫布寬度的像素數）。

    Returns:
        np.ndarray: 長度 ``columns`` 的峰值（≥ 0）。
    """
    if columns <= 0 or len(audio) == 0:
        return np.zeros(max(columns, 0), dtype=np.float32)
    mono = np.abs(audio).max(axis=1)
    edges = np.linspace(0, len(mono), columns + 1).astype(int)
    peaks = np.maximum.reduceat(mono, np.minimum(edges[:-1], len(mono) - 1))
    peaks[edges[1:] <= edges[:-1]] = 0.0
    return peaks.astype(np.float32)


# ---------------------------------------------------------------- 音量
MAX_BOOST_DB = 30.0
MIN_BOOST_DB = -24.0
# 音效版上每個音效的音量：下載來的音效大小差很多，範圍給大一點，效果由使用者自己判斷
SOUND_MAX_DB = 80.0
SOUND_MIN_DB = -80.0
# 自動放大的目標：說話時的音量（100 ms 均方根的第 90 百分位）拉到這個大小，接近一般音效的響度
AUTO_TARGET_DB = -16.0
# 自動放大時，峰值最多超過滿刻度這麼多（超過的部分交給峰值限制器），避免為了一聲爆音整段都不放大
AUTO_PEAK_OVER_DB = 6.0
# 峰值限制器：混音後的峰值壓在這個大小以下
LIMITER_CEILING = 0.95
# 增益在峰值前後各用這麼久平滑地降下、回升；比即時播放用的 tanh 柔性限幅乾淨得多，
# 峰值超出好幾 dB 也聽不太出來（只是那一瞬間小聲一點，不會破音）
LIMITER_SMOOTH_SECONDS = 0.02
_LIMITER_BLOCK = 32          # 以 32 個取樣為一塊計算增益，長片段也算得快
# 超過「即時播放開始壓縮的門檻」15 倍才提醒「壓得很重」：效果好不好交給使用者自己聽，
# 只有誇張到幾乎一定會失真時才標紅
COMPRESS_WARN_LEVEL = 15 * LIMITER_THRESHOLD
_LOUDNESS_WINDOW = SAMPLE_RATE // 10
_SILENCE_DB = -70.0


def _db(value: float) -> float:
    return 20.0 * float(np.log10(max(value, 1e-12)))


def peak_db(audio: np.ndarray) -> float:
    """峰值（dBFS）；沒有聲音時約為 -240。"""
    return _db(float(np.abs(audio).max())) if len(audio) else _db(0.0)


def loudness_db(audio: np.ndarray) -> float | None:
    """有聲音部分的響度（dBFS）：每 100 ms 的均方根取第 90 百分位，忽略靜音。

    比峰值更接近人耳感受到的大小，也不會被一聲短促的爆音左右。

    Args:
        audio (np.ndarray): (frames, 2) 音訊。

    Returns:
        float | None: 響度；整段都是靜音時為 None。
    """
    mono = audio.mean(axis=1) if audio.ndim == 2 else audio
    n = len(mono) // _LOUDNESS_WINDOW
    if n == 0:
        rms = np.array([np.sqrt(np.mean(mono ** 2))]) if len(mono) else np.zeros(0)
    else:
        rms = np.sqrt((mono[: n * _LOUDNESS_WINDOW].reshape(n, -1) ** 2).mean(axis=1))
    loud = rms[rms > 10 ** (_SILENCE_DB / 20)]
    if len(loud) == 0:
        return None
    return _db(float(np.percentile(loud, 90)))


def auto_gain_db(audio: np.ndarray) -> float:
    """自動放大需要的增益（dB），讓說話聲接近一般音效的大小。

    回放錄到的是程式「調整音量前」的聲音（不受 Windows 音量、Discord 使用者音量影響），
    常常比平常聽到的小 20 dB 以上，所以預設就套用這個增益。

    Args:
        audio (np.ndarray): 音訊（通常是目前選取的範圍）。

    Returns:
        float: 介於 ``MIN_BOOST_DB`` 與 ``MAX_BOOST_DB`` 的增益，以 0.5 dB 為單位；靜音時為 0。
    """
    loud = loudness_db(audio)
    if loud is None:
        return 0.0
    gain = min(AUTO_TARGET_DB - loud, AUTO_PEAK_OVER_DB - peak_db(audio))
    return round(min(max(gain, MIN_BOOST_DB), MAX_BOOST_DB) * 2) / 2


def gain_factor(gain_db: float) -> float:
    """dB 轉成倍率。"""
    return float(10 ** (gain_db / 20))


def volume_to_db(factor: float) -> float:
    """音量倍率轉成 dB（以 0.5 dB 為單位，限制在 ±80 dB；0 倍視為 -80 dB）。

    Args:
        factor (float): 倍率（1.0 = 原音量）。

    Returns:
        float: dB。
    """
    if factor <= 0:
        return SOUND_MIN_DB
    value = 20 * float(np.log10(factor))
    return round(min(max(value, SOUND_MIN_DB), SOUND_MAX_DB) * 2) / 2


def render_gain(audio: np.ndarray, factor: float) -> np.ndarray:
    """套用音量倍率並以峰值限制器處理，結果和編輯器試聽、存檔的聲音一致。

    Args:
        audio (np.ndarray): 原始音訊（不會被修改）。
        factor (float): 音量倍率。

    Returns:
        np.ndarray: 新的 float32 陣列；倍率為 1 時直接回傳原陣列。
    """
    if factor == 1.0:
        return audio
    return peak_limit(audio * np.float32(factor))


def limited_ratio(audio: np.ndarray, gain_db: float) -> float:
    """套用增益後有多少比例的取樣會被限幅壓縮（用來提醒「太大聲」）。

    Args:
        audio (np.ndarray): 音訊。
        gain_db (float): 增益（dB）。

    Returns:
        float: 0–1。
    """
    if len(audio) == 0:
        return 0.0
    return float(np.count_nonzero(np.abs(audio) > COMPRESS_WARN_LEVEL / gain_factor(gain_db))) / audio.size


def _centered_min(values: np.ndarray, half: int) -> np.ndarray:
    """每個位置前後各 ``half`` 個值裡的最小值（倍增法，O(n log half)）。"""
    width = 2 * half + 1
    out = np.pad(values, half, constant_values=1.0)
    covered = 1                 # out[i] 目前是 out[i : i + covered] 的最小值
    while covered < width:
        step = min(covered, width - covered)
        out[:-step] = np.minimum(out[:-step], out[step:])
        covered += step
    return out[: len(values)]


def _centered_mean(values: np.ndarray, half: int) -> np.ndarray:
    """每個位置前後各 ``half`` 個值的平均（兩端以端點值延伸）。"""
    padded = np.pad(values.astype(np.float64), half, mode="edge")
    total = np.concatenate(([0.0], np.cumsum(padded)))
    width = 2 * half + 1
    return (total[width:] - total[:-width]) / width


def peak_limit(audio: np.ndarray, ceiling: float = LIMITER_CEILING) -> np.ndarray:
    """離線的預看式峰值限制器：只在峰值附近平滑地調低音量，讓輸出不超過 ``ceiling``。

    做法：每塊需要的增益 → 前後取最小值（預看與保持）→ 前後平均（平滑升降）。
    取最小值的範圍比平均的範圍多一塊，保證平均後的增益在每個位置都不超過該處需要的增益，
    因此峰值一定壓得住，而沒超過的段落完全不變。

    Args:
        audio (np.ndarray): (frames, channels) float32。
        ceiling (float): 輸出峰值上限。

    Returns:
        np.ndarray: 處理後的 float32 音訊（不需要處理時回傳原陣列）。
    """
    frames = len(audio)
    if frames == 0:
        return audio
    peaks = np.abs(audio).max(axis=1) if audio.ndim == 2 else np.abs(audio)
    if float(peaks.max()) <= ceiling:
        return audio
    blocks = -(-frames // _LIMITER_BLOCK)
    padded = np.zeros(blocks * _LIMITER_BLOCK, dtype=np.float32)
    padded[:frames] = peaks
    block_peak = padded.reshape(blocks, _LIMITER_BLOCK).max(axis=1).astype(np.float64)
    need = np.minimum(1.0, ceiling / np.maximum(block_peak, 1e-12))
    half = max(1, round(LIMITER_SMOOTH_SECONDS * SAMPLE_RATE / _LIMITER_BLOCK))
    gain_blocks = _centered_mean(_centered_min(need, half + 1), half)
    centers = (np.arange(blocks) + 0.5) * _LIMITER_BLOCK
    gain = np.interp(np.arange(frames), centers, gain_blocks).astype(np.float32)
    out = audio * (gain[:, None] if audio.ndim == 2 else gain)
    np.clip(out, -ceiling, ceiling, out=out)     # 浮點誤差的保險
    return out.astype(np.float32, copy=False)


def mix(tracks: list[np.ndarray], gains_db: list[float]) -> np.ndarray:
    """依各軌增益混音，最後以峰值限制器避免爆音。

    Args:
        tracks (list[np.ndarray]): 等長的 (frames, 2) float32 音軌。
        gains_db (list[float]): 各軌增益（dB）。

    Returns:
        np.ndarray: (frames, 2) float32；沒有音軌時長度為 0。
    """
    if not tracks:
        return np.zeros((0, CHANNELS), dtype=np.float32)
    out = np.zeros_like(tracks[0], dtype=np.float32)
    for audio, gain in zip(tracks, gains_db, strict=True):
        out += audio * np.float32(gain_factor(gain))
    return peak_limit(out)


def auto_mix(tracks: list[Track]) -> np.ndarray:
    """每軌各自自動調整音量後混音（片段清單的試聽與整段下載使用）。

    Args:
        tracks (list[Track]): (來源, 音訊) 清單。

    Returns:
        np.ndarray: (frames, 2) float32。
    """
    audios = [a for _s, a in tracks]
    return mix(audios, [auto_gain_db(a) for a in audios])


def denoise_stereo(audio: np.ndarray, strong: bool = False) -> np.ndarray:
    """以 RNNoise 對一段音訊降噪（轉單聲道處理後再複製成立體聲）。

    RNNoise 是針對人聲訓練的：適合朋友的說話聲（去掉鍵盤、風扇聲），不適合音樂。

    Args:
        audio (np.ndarray): (frames, 2) float32。
        strong (bool): 使用「強」降噪（沒說話時完全靜音）。

    Returns:
        np.ndarray: (frames, 2) float32。

    Raises:
        RuntimeError: RNNoise 無法載入時拋出。
    """
    from soundboard.denoise import DenoiseLevel, denoise_clip  # 延後匯入：只有用到降噪才載入 DLL

    level = DenoiseLevel.STRONG if strong else DenoiseLevel.STANDARD
    mono = denoise_clip(audio.mean(axis=1).astype(np.float32), level)
    return np.repeat(mono[:, None], CHANNELS, axis=1)
