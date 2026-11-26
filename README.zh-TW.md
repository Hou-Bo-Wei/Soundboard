# 🔊 音效版 Soundboard

[English](README.md) | **繁體中文**

Windows 桌面音效版，讓**沒有 Discord Nitro 的使用者**也能在語音通話中播放音效、傳送貼圖。
也可用於遊戲語音等任何能選擇麥克風的程式。

![首頁](docs/screenshot.png)

## 功能

| 功能 | 說明 |
|---|---|
| 🔊 **音效版** | 把音效檔放進資料夾就會變成按鈕，按一下讓通話對象聽到，沒有長度限制。可分組、搜尋、試聽（只有自己聽得到）、調整單一音效的音量與修剪 |
| ⌨ **全域快速鍵與浮動視窗** | 不必切出遊戲：用快速鍵播放音效，或在螢幕角落開一個半透明的小音效版 |
| ⏺ **回放錄音** | 持續保留最近幾分鐘的聲音（自己的麥克風、Discord、YouTube 等），按一下擷取，剪成新的音效 |
| 🎚 **混音器** | 把 Spotify、Chrome 等程式的聲音傳給通話對象，所有音量集中在同一頁調整 |
| 🚪 **進場音效** | 加入 Discord 語音頻道時自動播放指定的音效（原本是 Nitro 的功能） |
| 🔇 **麥克風降噪** | 以 RNNoise 濾掉鍵盤聲、風扇聲，只處理麥克風，不影響音效 |
| 🖼 **貼圖** | 按一下貼圖就複製，到 Discord 按 `Ctrl+V` 傳送；動態貼圖自動轉成 GIF。打字跳出對應的貼圖（關鍵字搜尋），LINE 訊息貼圖可套用自己打的字，也能用 Webhook 直接傳到頻道 |
| 🖥 **背景執行** | 開機自動啟動、常駐系統匣，關閉視窗後快速鍵與進場音效照常運作 |
| 🌐 **多語言** | 繁體中文、English、日本語、한국어 |

## 運作方式

音效版把音效與你的麥克風混在一起，送進「虛擬麥克風」（VB-CABLE），Discord 再把它當成麥克風使用：

```
音效、程式聲音 ────┬──> 耳機（自己聽）
                   │
實體麥克風（降噪）─┴──> 虛擬麥克風 ──> Discord 的輸入裝置
```

## 快速開始

1. 安裝免費的虛擬音源線 [VB-CABLE](https://vb-audio.com/Cable/)，裝好後重新開機（只需一次）。
2. 從 [Releases](https://github.com/Hou-Bo-Wei/Soundboard/releases/latest) 下載 `Soundboard-*-win64.zip`，解壓縮後執行 `Soundboard.exe`，把音效檔放進旁邊的「音效」資料夾。
3. Discord →「語音與視訊」：**輸入裝置**選 `CABLE Output`，並**關閉雜訊抑制與「自動判斷輸入靈敏度」**（否則音效會被濾掉，降噪改用音效版內建的）。

用原始碼執行（Windows 10／11、Python 3.10 以上）：

```powershell
pip install -e .
```

```powershell
python -m soundboard
```

## 附加工具

| 工具 | 說明 |
|---|---|
| [LINE 貼圖下載器](tools/line_sticker_dl) | 貼上 LINE Store 網址，把整組貼圖（含動態貼圖）存成透明背景的圖檔，可直接給貼圖頁使用。填入 Claude API 金鑰後，下載完會自動產生搜尋用的關鍵字 |
| [貼圖關鍵字工具](tools/sticker_keywords) | 為已下載的貼圖資料夾產生關鍵字：拼總覽圖、匯入關鍵字、檢查進度 |

> 貼圖的著作權屬於原作者，請只做個人用途。

## 主要架構

Python＋Tkinter，原始碼採 `src/` 結構：

```
src/soundboard/
├─ gui/             介面：首頁、貼圖、回放錄音、混音器、設定等頁面
├─ engine.py        音訊引擎：監聽、虛擬麥克風、實體麥克風三條串流
├─ mixer.py         即時混音、音量與限幅
├─ denoise.py       麥克風降噪（RNNoise）
├─ replay.py        回放錄音（多來源環形緩衝）
├─ app_audio.py     混音器：擷取指定程式的聲音（WASAPI 程式迴送）
├─ stickers.py      貼圖掃描、轉檔（動態轉 GIF）
├─ sticker_meta.py  貼圖關鍵字與比對排序
├─ hotkeys.py       全域快速鍵    overlay.py  浮動視窗    tray.py  系統匣
└─ locales/         翻譯檔
tools/              LINE 貼圖下載器、貼圖關鍵字工具
packaging/          打包 exe、同步翻譯檔
tests/              單元測試
```

開發常用指令：

```powershell
python -m pytest
```

```powershell
python packaging/build_exe.py
```

## 詳細說明

每個功能的操作方式、設定項目與常見問題，請見 **[詳細使用說明](docs/GUIDE.zh-TW.md)**。
