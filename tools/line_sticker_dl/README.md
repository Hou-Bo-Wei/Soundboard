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

### 命令列

```powershell
python line_sticker_dl.py <網址或ID> [-o 輸出資料夾] [-f apng,gif,webp] [--limit N]
```

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
