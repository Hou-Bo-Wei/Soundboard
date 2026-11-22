"""Tkinter 圖形介面（Discord 風格深色主題）。

模組分工：

- ``app``：主視窗外框（側邊欄、內容區、狀態列）與播放、小視窗、背景訊息等共用流程
- ``home``：首頁（音效按鈕、群組、搜尋、拖曳）
- ``settings_page``：設定頁（裝置、音量、降噪、進場音效）
- ``menus``／``editing``：右鍵選單與音效、群組的編輯動作
- ``hotkey_controller``：全域快捷鍵
- ``widgets``／``styles``／``presenters``：自訂元件、樣式與顯示文字
"""

from soundboard.gui.app import SoundboardApp, run

__all__ = ["SoundboardApp", "run"]
