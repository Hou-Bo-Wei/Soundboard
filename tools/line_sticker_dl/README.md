# LINE 貼圖下載器 / LINE Sticker Downloader

音效版「🖼 貼圖」頁的**附加工具**：貼上 LINE Store 網址，就把貼圖（含動態貼圖、表情貼）抓成透明背景的圖檔，
不必再螢幕截圖＋摳圖。抓下來的資料夾可以直接在音效版按「📁 選擇資料夾」指向它，一個系列一個資料夾。

An optional companion tool for Soundboard's "🖼 Stickers" page: paste a LINE Store URL and it saves the whole set
(including animated stickers and emoji) as transparent images. Point Soundboard's "📁 Choose folder" at the output
folder — one sub-folder per series.

> ⚠️ 貼圖的著作權屬於原作者與 LINE。請**只做個人本機用途**，不要散布、轉貼或商用。
> Sticker artwork belongs to its creators and LINE. Personal, local use only — do not redistribute or use commercially.

## 使用

需要 Python 3.8+（Windows 內建 tkinter）。轉 GIF / WebP 與縮圖需要 Pillow：

```powershell
pip install pillow
```

**開啟介面**：雙擊 `run.bat`，或

```powershell
python line_sticker_dl.py
```

貼上網址 → 按 Enter 或「預覽」（會先顯示縮圖，作者頁可勾選要下載的系列）→「開始下載」。

### 支援的網址

| 類型 | 範例 |
|---|---|
| 單一貼圖組 | `https://store.line.me/stickershop/product/37348063/zh-Hant` |
| 單一表情貼組 | `https://store.line.me/emojishop/product/<24 位英數 ID>/zh-Hant` |
| 貼圖作者頁（整個作者的全部系列） | `https://store.line.me/stickershop/author/681334/zh-Hant` |
| 表情貼作者頁 | `https://store.line.me/emojishop/author/7819400/zh-Hant` |

也可以只貼數字 ID（視為貼圖組）。作者頁請貼完整網址。

### 輸出格式（動態貼圖）

| 格式 | 說明 |
|---|---|
| APNG 原檔（`.png`） | LINE 的原始檔，用瀏覽器開就會動 |
| GIF | 透明、無限循環；GIF 只有「透明／不透明」兩種，邊緣比 APNG 粗 |
| 動態 WebP | 完整半透明，畫質最好 |

靜態貼圖一律只存 PNG。已存在的檔案會略過，所以中斷後重跑可以接續。

### 文字貼圖與 `line_meta.json`

LINE 的**訊息貼圖**（每張可以自己打字）與**「隨你填」名字貼圖**也能下載：主圖存成 `001.png`（訊息貼圖疊上預設文字；
名字貼圖的名字位置是「****」），沒有字的底圖等輔助檔放在隱藏的 `.line` 資料夾。每個系列資料夾另外寫一份 `line_meta.json`
（貼圖 ID、預設文字、文字貼圖的底圖與文字 API），音效版的「🖼 貼圖」頁用它來**套用自己輸入的文字**與**搜尋關鍵字**。
舊版下載的資料夾，把同一個網址再下載一次即可補上（已存在的貼圖會略過）。

Message stickers and custom-name stickers can be downloaded too. Each series folder gets a `line_meta.json`
(sticker IDs, default text, base images in the hidden `.line` folder) that Soundboard uses to apply your own text and keywords.

> 傳到 Discord 時，音效版會自動把 APNG／WebP 轉成會動的 GIF，所以只存 APNG 原檔也可以。

### AI 關鍵字（選用，需要 Claude API 金鑰）

LINE 貼圖本身沒有可以下載的關鍵字，所以下載器可以請 Claude 看貼圖，寫出每張的「圖上的字」與搜尋關鍵字，
存成系列資料夾的 `sticker_keywords.json`。音效版在搜尋框打「早安」時，就會跳出對應的貼圖。

1. 到 [console.anthropic.com](https://console.anthropic.com) 的「API Keys」頁面建立金鑰（費用依用量從你的 Anthropic 帳戶扣款）。
2. 下載器「AI 關鍵字」列按「設定…」→ 貼上金鑰 →「測試金鑰」（不產生費用）→「儲存」。
3. 勾選「下載完自動產生關鍵字」。之後每下載一組，就會自動產生那一組的關鍵字。

以前下載、還沒有關鍵字的資料夾，按「為資料夾補關鍵字…」選擇資料夾（可以選整個 `LINE貼圖`）。
開始前會列出要處理幾組、幾張與預估費用，確認後才會開始。

| 模型 | 一組 40 張的費用（約） | 說明 |
|---|---|---|
| **Sonnet 5.5（預設）** | US$0.03～0.05 | 藝術字、直排字與台語、火星文的白話翻譯都讀得準 |
| Haiku 5.5 | US$0.002 | 價格約 Sonnet 的 1/20；還沒實測過藝術字，可能較常讀錯字 |
| Opus 5.5 | US$0.06～0.10 | 約 Sonnet 的 2 倍；Sonnet 讀錯很多時再改用 |

- 已經有關鍵字的貼圖不會重做；手動改過的關鍵字會保留，只補上缺少的。
- 金鑰以 Windows DPAPI 加密存在 `%APPDATA%\LineStickerDownloader\settings.json`，只有目前的 Windows 帳號能解開。
  也可以不存金鑰，改設環境變數 `ANTHROPIC_API_KEY`。
- 產生關鍵字時，貼圖的總覽圖（每組拼成一張）會傳送到 Anthropic 處理。
- 不想用 API 也可以照 [`tools/sticker_keywords`](../sticker_keywords) 的說明手動做。

AI keywords are optional and need a Claude API key from console.anthropic.com, billed by usage. Set the key via 「設定…」 ("Settings…"), tick 「下載完自動產生關鍵字」 ("Generate keywords after download"), and use 「為資料夾補關鍵字…」 ("Add keywords to a folder…") for older folders; it shows an estimated cost before starting. The default model is Sonnet 5.5 at about US$0.03-0.05 per 40-sticker set, and Haiku 5.5 or Opus 5.5 can be selected instead. The key is stored encrypted with Windows DPAPI (or read from the `ANTHROPIC_API_KEY` environment variable). Note that the sticker overview image is sent to Anthropic.

### 命令列

```powershell
python line_sticker_dl.py <網址或ID> [-o 輸出資料夾] [-f apng,gif,webp] [--limit N] [--keywords]
```

加上 `--keywords` 會在下載完用 Claude 產生關鍵字（使用介面裡設定的金鑰與模型，或環境變數 `ANTHROPIC_API_KEY`）。
從原始碼執行 AI 關鍵字需要 Python 3.10 以上與 `pip install anthropic`（exe 版已內含）。

輸出位置預設為 `下載\LINE貼圖\`：

```
LINE貼圖\
  └─ <作者名>_<作者ID>\
       ├─ <系列名>_<貼圖組ID>\  001.png 001.gif …
       └─ …
```

## 限制

- 只能抓 LINE Store 公開的貼圖組，區域限定、已下架的可能抓不到。
- 這是非官方工具，LINE 改版網頁後可能需要調整。
