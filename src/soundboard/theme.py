"""介面共用配色與字型（Discord 風格深色主題）。"""

from __future__ import annotations

from soundboard.i18n import current_language

BG = "#2b2d31"
BG_DARK = "#1e1f22"
PANEL = "#313338"
BTN = "#404249"
BTN_HOVER = "#4e5058"
BTN_PLAYING = "#5865f2"
FG = "#f2f3f5"
FG_MUTED = "#b5bac1"
FG_SUBTLE = "#949ba4"
WARN = "#f0b232"
OK = "#23a55a"
# 預覽（只有自己聽得到）用綠色，和送給朋友的藍色明顯區分
PREVIEW = "#248046"
PREVIEW_HOVER = "#2d9d55"
ERR = "#f23f43"
ERR_HOVER = "#c93034"

# ---- 主視窗版面（側邊欄＋內容區＋卡片）
ACCENT = BTN_PLAYING
ACCENT_HOVER = "#4752c4"
SIDEBAR = BG
SURFACE = PANEL
CARD = BG
INPUT = BG_DARK
NAV_ACTIVE = BTN
NAV_HOVER = "#393c41"
HEADER_HOVER = "#393c41"
STATUS_BAR = "#232428"
# 首頁的警告橫幅用實心黃底深色字：比黃字更醒目，讓人一眼注意到「朋友聽不到」
BANNER_BG = WARN
BANNER_FG = "#1e1f22"

# 各語言用 Windows 內建、該文字最完整的介面字型（微軟正黑體沒有韓文字）。
# 字型在模組載入時決定，所以 __main__ 必須先呼叫 i18n.set_language 再匯入介面
FONTS_BY_LANGUAGE = {
    "zh_TW": "Microsoft JhengHei UI",
    "en": "Segoe UI",
    "ja": "Yu Gothic UI",
    "ko": "Malgun Gothic",
}
FONT_FAMILY = FONTS_BY_LANGUAGE.get(current_language(), "Microsoft JhengHei UI")
