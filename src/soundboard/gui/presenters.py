"""把程式狀態轉成畫面文字的純函式（不碰 Tk 元件，方便單元測試）。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from soundboard.grouping import Section
from soundboard.i18n import N_, tr
from soundboard.theme import FG_MUTED, OK, WARN

# 下拉選單中的特殊選項。這兩個值同時是程式內部與設定檔使用的「儲存值」：
# LABEL_NONE 會原樣存進 settings.json 的裝置欄位，為了相容既有設定檔，字串不可更改、也不可翻譯後再存。
# 下拉選單上顯示的文字一律經過 display_label()／label_from_display() 轉換。
LABEL_DEFAULT = N_("（系統預設）")
LABEL_NONE = N_("（不使用）")

# 功能快速鍵：設定檔中的 id → 顯示名稱（顯示時用 tr() 翻譯）
ACTIONS: dict[str, str] = {
    "stop": N_("⏹ 全部停止"),
    "overlay": N_("🪟 顯示／隱藏浮動視窗"),
    "preview": N_("🎧 切換試聽模式"),
    "overlay_group": N_("🗂 浮動視窗切換至下一個群組"),
    "replay_capture": N_("⏺ 擷取回放錄音"),
}

JOIN_DELAY_CHOICES: tuple[str, ...] = ("0", "0.5", "1", "1.5", "2", "3", "4", "5")

AUDIO_OK_TEXT = N_("✔ 虛擬麥克風運作中。請在 Discord『語音與視訊 → 輸入裝置』中選擇對應的錄音端"
                   "（VB-CABLE 為「CABLE Output」）。")

ELLIPSIS = "…"


@dataclass(frozen=True)
class AudioWarning:
    """一則音訊設定警告。

    Attributes:
        short (str): 首頁橫幅用的一行摘要。
        detail (str): 設定頁顯示的完整說明與處理方式。
    """

    short: str
    detail: str


def audio_warnings(virtual_mic_active: bool, monitor_redirected: str | None) -> list[AudioWarning]:
    """依目前裝置狀態產生要提醒使用者的警告。

    Args:
        virtual_mic_active (bool): 虛擬麥克風是否已開啟。
        monitor_redirected (str | None): None 表示正常；字串表示 Windows 預設播放裝置是虛擬音源線，
            監聽已改導向的裝置名稱（空字串表示找不到其他裝置）。

    Returns:
        list[AudioWarning]: 警告清單；沒有問題時為空。
    """
    warnings: list[AudioWarning] = []
    if not virtual_mic_active:
        warnings.append(AudioWarning(
            tr("尚未設定虛擬麥克風，通話對象無法聽到音效"),
            tr("⚠ 尚未設定虛擬麥克風，通話對象無法聽到音效。請安裝 VB-Audio Virtual Cable，"
               "按一下「重新偵測裝置」後選擇「CABLE Input」（詳見 README）。"),
        ))
    if monitor_redirected is not None:
        # 有沒有找到替代裝置是兩種不同的句子，各自完整翻譯（不串接片段）
        if monitor_redirected:
            detail = tr("⚠ Windows 預設播放裝置為虛擬音源線，監聽已改用「{device}」。"
                        "建議在 Windows 音效設定中將預設播放裝置改回耳機，否則遊戲音訊也會傳送給通話對象。",
                        device=monitor_redirected)
        else:
            detail = tr("⚠ Windows 預設播放裝置為虛擬音源線，且找不到其他播放裝置。"
                        "建議在 Windows 音效設定中將預設播放裝置改回耳機，否則遊戲音訊也會傳送給通話對象。")
        warnings.append(AudioWarning(tr("Windows 預設播放裝置為虛擬音源線"), detail))
    return warnings


def display_label(value: str) -> str:
    """下拉選單的儲存值 → 畫面上顯示的文字。

    只有兩個特殊選項需要翻譯；裝置名稱是系統提供的使用者資料，原樣顯示。

    Args:
        value (str): :data:`LABEL_DEFAULT`、:data:`LABEL_NONE` 或裝置名稱。

    Returns:
        str: 目前語言的顯示文字。
    """
    if value in (LABEL_DEFAULT, LABEL_NONE):
        return tr(value)
    return value


def label_from_display(text: str) -> str:
    """畫面上顯示的文字 → 下拉選單的儲存值（:func:`display_label` 的反向）。

    Args:
        text (str): 下拉選單目前顯示的文字。

    Returns:
        str: :data:`LABEL_DEFAULT`、:data:`LABEL_NONE` 或裝置名稱。
    """
    # 也接受未翻譯的原文：切換語言前留在變數裡的值、或程式直接設定的儲存值都能辨識
    if text in (LABEL_DEFAULT, tr(LABEL_DEFAULT)):
        return LABEL_DEFAULT
    if text in (LABEL_NONE, tr(LABEL_NONE)):
        return LABEL_NONE
    return text


def label_for(name: str | None, names: list[str], allow_default: bool) -> str:
    """設定檔中的裝置名稱 → 下拉選單要選取的選項（儲存值；顯示前再經過 :func:`display_label`）。

    Args:
        name (str | None): 設定檔中的值（None = 系統預設、LABEL_NONE = 不使用、其他 = 裝置名稱）。
        names (list[str]): 目前清單中有的裝置名稱。
        allow_default (bool): 這個選單是否有「系統預設」選項。

    Returns:
        str: 要選取的選項（LABEL_DEFAULT、LABEL_NONE 或裝置名稱）；裝置不存在時退回預設或不使用。
    """
    if name == LABEL_NONE:
        return LABEL_NONE
    if name and name in names:
        return name
    return LABEL_DEFAULT if allow_default else LABEL_NONE


def stored_device(label: str) -> str | None:
    """下拉選單的選項 → 存進設定檔的值。

    Args:
        label (str): 選項的儲存值（顯示文字請先用 :func:`label_from_display` 轉換）。

    Returns:
        str | None: None 代表系統預設；其他照原文字存（含 LABEL_NONE）。
    """
    return None if label == LABEL_DEFAULT else label


@dataclass(frozen=True)
class TileText:
    """音效按鈕上的文字。

    Attributes:
        title (str): 主文字（名稱，加上複選、進場音效、小視窗的前綴）。
        subtitle (str): 第二行（快捷鍵）；沒有時為空字串。
    """

    title: str
    subtitle: str

    @property
    def full(self) -> str:
        """合併成一段文字（除錯、測試用）。"""
        return f"{self.title}\n{self.subtitle}" if self.subtitle else self.title


def tile_text(name: str, *, selected: bool, join: bool, pinned: bool, hotkey: str) -> TileText:
    """組出音效按鈕的文字。

    Args:
        name (str): 顯示名稱。
        selected (bool): 是否被 Ctrl＋左鍵複選（☑）。
        join (bool): 是否為進場音效（🚪）。
        pinned (bool): 是否釘選在小視窗（📌）。
        hotkey (str): 已格式化的快捷鍵文字，沒有時為空字串。

    Returns:
        TileText: 按鈕文字。
    """
    prefix = ("☑ " if selected else "") + ("🚪 " if join else "") + ("📌 " if pinned else "")
    return TileText(prefix + name, f"⌨ {hotkey}" if hotkey else "")


def section_header_text(section: Section, searching: bool) -> tuple[str, str]:
    """群組標題列的文字。

    Args:
        section (Section): 區塊。
        searching (bool): 是否正在搜尋（搜尋時顯示「符合數/總數」）。

    Returns:
        tuple[str, str]: （左側標題, 右側數量）。
    """
    # 用 ► 而不是 ▶：▶ 在 Windows 上會被畫成彩色表情符號方塊
    arrow = "►" if section.collapsed else "▼"
    icon = "🗂" if section.name is not None else "📄"
    count = f"{len(section.items)} / {section.total}" if searching else str(section.total)
    return f"{arrow}   {icon}  {section.title}", count


def grid_columns(width: int, tile_width: int) -> int:
    """依可用寬度計算一列放幾個音效按鈕。

    Args:
        width (int): 按鈕區寬度（像素）。
        tile_width (int): 每個按鈕佔用的寬度（含間距）。

    Returns:
        int: 欄數，至少 1。
    """
    return max(1, max(width, tile_width) // max(1, tile_width))


def voice_status(in_voice: bool | None) -> tuple[str, str]:
    """Discord 語音偵測狀態的顯示文字與顏色。

    Args:
        in_voice (bool | None): True = 通話中、False = 未在語音中、None = 無法偵測。

    Returns:
        tuple[str, str]: （文字, 顏色）。
    """
    if in_voice is None:
        return tr("Discord 語音：無法偵測"), FG_MUTED
    if in_voice:
        return tr("Discord 語音：🟢 通話中"), OK
    return tr("Discord 語音：未加入語音頻道"), FG_MUTED


def background_note(*, supported: bool, registered: bool, disabled_by_windows: bool,
                    elsewhere: Path | None, tray_ok: bool) -> tuple[str, str]:
    """「開機與背景執行」卡片下方的狀態說明。

    Args:
        supported (bool): 系統是否支援開機自動啟動。
        registered (bool): 登錄檔裡是否有登記。
        disabled_by_windows (bool): 是否在工作管理員被停用。
        elsewhere (Path | None): 登記的是另一個位置的程式時，那個程式的路徑。
        tray_ok (bool): 系統匣圖示是否建立成功。

    Returns:
        tuple[str, str]: (文字, 顏色)。
    """
    if not tray_ok:
        return tr("⚠ 無法建立系統匣圖示，按一下 ✕ 將直接結束程式。"), WARN
    if not supported:
        return tr("此系統不支援開機自動啟動。"), FG_MUTED
    if registered and disabled_by_windows:
        return tr("⚠ 已在 Windows「工作管理員 → 啟動應用程式」中停用，開機時不會啟動。"
                  "開啟上方的開關即可重新啟用。"), WARN
    if registered and elsewhere is not None:
        return tr("⚠ 開機時將啟動另一個位置的音效版：{path}\n關閉後重新開啟上方的開關，即可改為啟動目前的版本。",
                  path=elsewhere), WARN
    if registered:
        return tr("✔ 登入 Windows 後將自動在背景啟動（系統匣中會顯示音效版圖示）。"), OK
    return tr("目前未設定開機自動啟動。"), FG_MUTED


def wrap_text(text: str, measure: Callable[[str], int], max_width: int, max_lines: int) -> str:
    """逐字斷行並限制行數，超出時最後一行以「…」結尾。

    音效名稱多半是沒有空白的中文，Tk 的自動換行只在空白處斷行，
    長名稱會整行溢出按鈕，所以自己逐字斷行。

    Args:
        text (str): 原始文字（可含換行）。
        measure (Callable[[str], int]): 量測文字寬度（像素）的函式。
        max_width (int): 每行最大寬度。
        max_lines (int): 最多幾行。

    Returns:
        str: 斷好行的文字。
    """
    if max_lines <= 0:
        return ""
    lines: list[str] = []
    for paragraph in text.split("\n"):
        line = ""
        for ch in paragraph:
            if line and measure(line + ch) > max_width:
                lines.append(line)
                line = ch
            else:
                line += ch
        lines.append(line)
    if len(lines) <= max_lines:
        return "\n".join(lines)
    kept = lines[:max_lines]
    last = kept[-1]
    while last and measure(last + ELLIPSIS) > max_width:
        last = last[:-1]
    kept[-1] = last + ELLIPSIS
    return "\n".join(kept)
