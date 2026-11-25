#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LINE 貼圖下載器（個人自用）
============================
貼上 LINE Store 網址就能把貼圖抓成透明背景的圖檔，不必再螢幕截圖＋摳圖。

支援的網址
----------
  單一貼圖組  https://store.line.me/stickershop/product/37348063/zh-Hant
  單一表情貼  https://store.line.me/emojishop/product/<24位英數ID>/zh-Hant
  作者頁      https://store.line.me/stickershop/author/681334/zh-Hant
  表情貼作者頁 https://store.line.me/emojishop/author/7819400/zh-Hant
              → 抓該作者全部作品，每個系列（貼圖組 / 表情貼組）一個資料夾
  也可以只貼數字 ID（視為貼圖組）。

用法
----
  GUI:  python line_sticker_dl.py
  CLI:  python line_sticker_dl.py <網址或ID> [-o 輸出資料夾] [-f apng,gif,webp] [--limit N]

輸出格式（動態貼圖）
--------------------
  apng : LINE 原始檔（副檔名 .png，動態貼圖本身就是 APNG，用瀏覽器可看動畫）
  gif  : 轉成 GIF（透明、無限循環；GIF 只有 1 bit 透明，邊緣會比 APNG 粗）
  webp : 轉成動態 WebP（完整半透明，畫質最好）
  靜態貼圖一律只存 PNG。已存在的檔案會略過，所以中斷後重跑可以接續。

文字貼圖與 line_meta.json
--------------------------
  訊息貼圖（每張可自己打字）與「隨你填」名字貼圖也能下載：主圖存成 001.png（訊息貼圖疊上預設文字，
  名字貼圖的名字位置是「****」），沒有字的底圖等輔助檔放在隱藏的 .line 資料夾。
  每個系列資料夾另外寫一份 line_meta.json（貼圖 ID、預設文字、文字貼圖的底圖與文字 API），
  音效版的「🖼 貼圖」頁用它來套用自己輸入的文字與搜尋關鍵字。舊版下載的資料夾重新下載一次即可補上。

依賴：Python 3.8+、tkinter（內建）；轉 GIF / WebP、縮圖縮放需要 Pillow（pip install pillow）。
請僅供個人本機使用，不要散布或商用——貼圖版權屬於原創作者與 LINE。
"""
import argparse
import html
import io
import json
import os
import queue
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    from PIL import Image, ImageSequence
    HAVE_PIL = True
except Exception:  # Pillow 沒裝就只能存原始檔
    HAVE_PIL = False

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
KINDS = {  # kind -> (中文名, 商品頁網址)
    "sticker": ("貼圖", "https://store.line.me/stickershop/product/{id}/zh-Hant"),
    "emoji": ("表情貼", "https://store.line.me/emojishop/product/{id}/zh-Hant"),
}
AUTHOR_URL = "https://store.line.me/{shop}/author/{id}/zh-Hant?page={page}"
AUTHOR_SHOPS = {"author": "stickershop", "emoji_author": "emojishop"}  # 目標種類 -> 商店路徑
WORKERS = 4  # 同時下載幾張
CDN = "https://stickershop.line-scdn.net/stickershop/v1"
# 可以自訂文字的貼圖：訊息貼圖（每張自己打字）與「隨你填」名字貼圖
TEXT_KINDS = {"PER_STICKER_TEXT": "message", "NAME_TEXT": "name"}
# 訊息貼圖的文字由 LINE 伺服器畫成透明圖層（在網址後面接上文字）；必須帶商品頁當 Referer
OVERLAY_API = "https://store.line.me/overlay/sticker/{pid}/{sid}/iPhone/sticker.png?text="
META_FILE = "line_meta.json"   # 每個系列資料夾一份，音效版讀它來支援文字貼圖與關鍵字
AUX_DIR = ".line"              # 文字貼圖的底圖等輔助檔（音效版不會把它們當成貼圖）
META_FORMAT = 1


# ---------------------------------------------------------------- 網路 / 解析
def http_get(url, retries=3, referer=None):
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA,
                "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
                **({"Referer": referer} if referer else {}),
            })
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (403, 404):  # 重試也沒用
                break
        except Exception as e:
            last = e
        time.sleep(0.8 * (i + 1))
    raise RuntimeError(f"下載失敗：{url}（{last}）")


def parse_target(text):
    """回傳 (kind, id)；kind 為 'author' / 'emoji_author' / 'sticker' / 'emoji'，看不懂回傳 None。"""
    text = (text or "").strip()
    m = re.search(r"stickershop/author/(\d+)", text)
    if m:
        return "author", m.group(1)
    m = re.search(r"emojishop/author/(\d+)", text)
    if m:
        return "emoji_author", m.group(1)
    m = re.search(r"emojishop/product/([0-9a-fA-F]{24})", text)
    if m:
        return "emoji", m.group(1).lower()
    for pat in (r"stickershop/product/(\d+)", r"line\.me/S/sticker/(\d+)",
                r"shop/detail/(\d+)", r"^(\d{3,})$"):
        m = re.search(pat, text)
        if m:
            return "sticker", m.group(1)
    if re.fullmatch(r"[0-9a-fA-F]{24}", text):
        return "emoji", text.lower()
    m = re.search(r"(\d{5,})", text)
    return ("sticker", m.group(1)) if m else None


def safe_name(s):
    s = re.sub(r'[\\/:*?"<>|\r\n\t]', " ", s).strip(" .")
    return re.sub(r"\s+", " ", s)[:60] or "LINE貼圖"


def fetch_product_meta(pack_id):
    """讀取貼圖組的 productInfo.meta（貼圖 ID、種類、訊息貼圖的預設文字）；讀不到時回傳空 dict。"""
    try:
        return json.loads(http_get(f"{CDN}/product/{pack_id}/android/productInfo.meta", retries=2))
    except Exception:
        return {}


def text_items_from_meta(pack_id, meta):
    """可自訂文字的貼圖組：依 productInfo.meta 組出下載項目（商品頁上沒有逐張的圖檔網址可用）。

    訊息貼圖：底圖（沒有字）＋預設文字圖層，另外記下 LINE 畫文字的 API。
    名字貼圖：顯示用的圖（名字位置是「****」）＋沒有名字的底圖，音效版比對兩張圖找出名字的位置。
    """
    text_kind = TEXT_KINDS.get(meta.get("stickerResourceType", ""))
    if not text_kind:
        return []
    items = []
    for s in meta.get("stickers", []):
        sid = str(s.get("id", ""))
        if not sid:
            continue
        if text_kind == "message":
            items.append({
                "type": "per_sticker_text", "id": sid, "textKind": "message",
                "staticUrl": f"{CDN}/sticker/{sid}/iPhone/base/plus/sticker@2x.png",
                "customOverlayUrl": f"{CDN}/product/{pack_id}/sticker/{sid}/iPhone/overlay/plus/default/sticker@2x.png",
                "customApi": OVERLAY_API.format(pid=pack_id, sid=sid),
                "defaultText": ((s.get("customPlus") or {}).get("defaultText") or ""),
            })
        else:
            items.append({
                "type": "name", "id": sid, "textKind": "name",
                "staticUrl": f"{CDN}/sticker/{sid}/iPhone/sticker@2x.png",
                "baseUrl": f"{CDN}/sticker/{sid}/iPhone/base/sticker@2x.png",
            })
    return items


def fetch_pack(kind, pack_id):
    """回傳 (標題, [項目])。每個項目是 data-preview 的 dict（文字貼圖另外帶 textKind 等欄位）。"""
    raw = http_get(KINDS[kind][1].format(id=pack_id))
    text = raw.decode("utf-8", "replace")
    m = re.search(r'<meta property="og:title" content="([^"]*)"', text)
    title = html.unescape(m.group(1)) if m else f"line_{pack_id}"
    title = re.split(r"\s+[–\-|]\s+LINE", title)[0]

    if kind == "sticker":
        # 文字貼圖的商品頁只給沒有字的底圖（訊息貼圖）或根本沒有逐張圖片（名字貼圖），改用 meta
        items = text_items_from_meta(pack_id, fetch_product_meta(pack_id))
        if items:
            return safe_name(title), items

    items, seen = [], set()
    for _, blob in re.findall(r"data-preview=(['\"])(.*?)\1", text, flags=re.S):
        try:
            d = json.loads(html.unescape(blob))
        except Exception:
            continue
        sid = str(d.get("id", ""))
        if not sid or sid in seen:
            continue
        seen.add(sid)
        # 第一筆是整組的封面（main.png），不是貼圖本身
        if "/product/" in (d.get("staticUrl") or "") and sid == pack_id:
            continue
        items.append(d)
    if not items:
        raise RuntimeError("這組沒有可下載的貼圖（可能是區域限定，或頁面改版了）")
    return safe_name(title), items


def fetch_author(author_id, shop="stickershop"):
    """回傳 (作者名, [{kind,id,title,cover,anim}])，自動翻頁抓完所有作品。
    shop 為 'stickershop'（貼圖作者頁）或 'emojishop'（表情貼作者頁）。"""
    packs, seen, name = [], set(), None
    for page in range(1, 60):
        text = http_get(AUTHOR_URL.format(shop=shop, id=author_id, page=page)).decode("utf-8", "replace")
        if name is None:
            m = re.search(r'data-test="author-name">(.*?)</em>', text, re.S)
            name = safe_name(html.unescape(re.sub(r"<[^>]+>", "", m.group(1)))) if m else f"author_{author_id}"
        new = 0
        for b in text.split('data-test="author-item"')[1:]:
            m = re.search(r"/(stickershop|emojishop)/product/([0-9A-Za-z]+)", b)
            if not m:
                continue
            kind = "sticker" if m.group(1) == "stickershop" else "emoji"
            pid = m.group(2).lower() if kind == "emoji" else m.group(2)
            if (kind, pid) in seen:
                continue
            seen.add((kind, pid))
            new += 1
            t = re.search(r'data-test="item-name">(.*?)</p>', b, re.S)
            c = re.search(r'<img[^>]*?src="([^"]+)"', b)
            cover = html.unescape(c.group(1)) if c else ""
            if not cover or ".svg" in cover:  # 有些系列給的是通用佔位圖，改用封面規則網址
                cover = (f"https://stickershop.line-scdn.net/stickershop/v1/product/{pid}/LINEStorePC/main.png"
                         if kind == "sticker" else
                         f"https://stickershop.line-scdn.net/sticonshop/v1/product/{pid}/iPhone/main.png")
            packs.append({
                "kind": kind, "id": pid,
                "title": safe_name(html.unescape(re.sub(r"<[^>]+>", "", t.group(1)))) if t else pid,
                "cover": cover,
                "anim": ("animation-sticker-icon" in b or "animation-sticon-icon" in b),
            })
        if not new or 'data-test="next-btn"' not in text:
            break
    if not packs:
        raise RuntimeError("作者頁沒有作品（網址錯誤，或此區域看不到）")
    return name, packs


# ---------------------------------------------------------------- 轉檔
def _frames(data):
    im = Image.open(io.BytesIO(data))
    frames, durs = [], []
    for fr in ImageSequence.Iterator(im):
        frames.append(fr.convert("RGBA"))
        durs.append(int(fr.info.get("duration", 100)) or 100)
    return frames, durs


def apng_to_gif(data, dst):
    frames, durs = _frames(data)
    out = []
    for rgba in frames:
        pal = rgba.convert("RGB").quantize(
            colors=255, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
        mask = rgba.getchannel("A").point(lambda a: 255 if a < 128 else 0)
        pal.paste(255, mask)  # 索引 255 保留給透明
        out.append(pal)
    out[0].save(dst, format="GIF", save_all=True, append_images=out[1:], duration=durs,
                loop=0, disposal=2, transparency=255, optimize=False)


def apng_to_webp(data, dst):
    frames, durs = _frames(data)
    frames[0].save(dst, format="WEBP", save_all=True, append_images=frames[1:], duration=durs,
                   loop=0, lossless=True, quality=100, method=4)


def _save_bytes(path, data):
    tmp = path + ".part"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def _save_via(path, func, data):
    tmp = path + ".part"
    func(data, tmp)
    os.replace(tmp, path)


def compose_message(base, overlay, dst):
    """訊息貼圖：把預設文字圖層疊到底圖上，存成一般看得到文字的貼圖。"""
    b = Image.open(io.BytesIO(base)).convert("RGBA")
    o = Image.open(io.BytesIO(overlay)).convert("RGBA")
    if o.size != b.size:
        o = o.resize(b.size)
    b.alpha_composite(o)
    b.save(dst, format="PNG")


def _make_aux_dir(folder):
    aux = os.path.join(folder, AUX_DIR)
    if not os.path.isdir(aux):
        os.makedirs(aux, exist_ok=True)
        if sys.platform == "win32":  # 設成隱藏，在檔案總管裡不礙眼
            try:
                import ctypes
                ctypes.windll.kernel32.SetFileAttributesW(aux, 0x02)
            except Exception:
                pass
    return aux


def build_meta(kind, pack_id, title, items, store_url):
    """組出寫進 line_meta.json 的內容（音效版用：文字貼圖的底圖與 API、預設文字）。"""
    resource = ""
    stickers = []
    for idx, d in enumerate(items, 1):
        base = f"{idx:03d}"
        entry = {"stem": base, "id": str(d.get("id", "")), "default_text": d.get("defaultText", "")}
        text_kind = d.get("textKind")
        if text_kind == "message":
            resource = "PER_STICKER_TEXT"
            entry["text"] = {"kind": "message", "base": f"{AUX_DIR}/{base}_base.png",
                             "overlay": f"{AUX_DIR}/{base}_overlay.png", "api": d.get("customApi", "")}
        elif text_kind == "name":
            resource = "NAME_TEXT"
            entry["text"] = {"kind": "name", "base": f"{AUX_DIR}/{base}_base.png"}
        stickers.append(entry)
    return {"format": META_FORMAT, "kind": kind, "package_id": str(pack_id), "title": title,
            "resource_type": resource, "store_url": store_url, "stickers": stickers}


def write_meta(folder, meta):
    _save_bytes(os.path.join(folder, META_FILE),
                json.dumps(meta, ensure_ascii=False, indent=1).encode("utf-8"))


# ---------------------------------------------------------------- 下載
def download_pack(target, out_root, formats, log=print, progress=None, stop=None,
                  prefetched=None, verbose=True):
    """下載一組貼圖到 out_root/<標題>_<ID>/。回傳 (資料夾, 成功數, 失敗數)。
    target 可以是網址字串或 (kind, id)；progress(done, total)。"""
    if isinstance(target, str):
        t = parse_target(target)
        if not t or t[0] in AUTHOR_SHOPS:
            raise RuntimeError("看不出貼圖組 ID，請貼 LINE Store 商品頁網址（含 /stickershop/product/數字）")
        target = t
    kind, pack_id = target
    vlog = log if verbose else (lambda m: None)

    if prefetched:
        title, items = prefetched
    else:
        vlog(f"貼圖組 ID：{pack_id}，讀取商品頁…")
        title, items = fetch_pack(kind, pack_id)
    folder = os.path.join(out_root, f"{title}_{pack_id}")
    os.makedirs(folder, exist_ok=True)
    n_anim = sum(1 for d in items if d.get("animationUrl"))
    vlog(f"「{title}」共 {len(items)} 張（動態 {n_anim}）→ {folder}")

    want_gif = "gif" in formats and HAVE_PIL
    want_webp = "webp" in formats and HAVE_PIL
    if ({"gif", "webp"} & set(formats)) and not HAVE_PIL:
        log("⚠ 沒安裝 Pillow，無法轉 GIF / WebP（pip install pillow），只存原始檔")
    need_apng = "apng" in formats or not (want_gif or want_webp)
    ref = KINDS[kind][1].format(id=pack_id)
    n_text = sum(1 for d in items if d.get("textKind"))
    if n_text:
        _make_aux_dir(folder)
        vlog(f"  可自訂文字的貼圖 {n_text} 張（音效版可直接套用文字）")

    def work_text(base, d):
        """文字貼圖：主圖（看得到預設文字或「****」）＋ .line 裡的底圖等輔助檔。"""
        main = os.path.join(folder, base + ".png")
        aux_base = os.path.join(folder, AUX_DIR, base + "_base.png")
        if d["textKind"] == "message":
            aux_overlay = os.path.join(folder, AUX_DIR, base + "_overlay.png")
            paths = [main, aux_base, aux_overlay]
            if all(os.path.exists(p) and os.path.getsize(p) > 0 for p in paths):
                return f"{base} 已存在，略過"
            base_data = http_get(d["staticUrl"], referer=ref)
            overlay_data = http_get(d["customOverlayUrl"], referer=ref)
            _save_bytes(aux_base, base_data)
            _save_bytes(aux_overlay, overlay_data)
            if HAVE_PIL:
                _save_via(main, lambda data, dst: compose_message(data, overlay_data, dst), base_data)
            else:
                _save_bytes(main, base_data)
            return f"{base} 訊息貼圖 ✓"
        paths = [main, aux_base]
        if all(os.path.exists(p) and os.path.getsize(p) > 0 for p in paths):
            return f"{base} 已存在，略過"
        _save_bytes(main, http_get(d["staticUrl"], referer=ref))
        _save_bytes(aux_base, http_get(d["baseUrl"], referer=ref))
        return f"{base} 名字貼圖 ✓"

    def work(idx, d):
        if stop and stop.is_set():
            return idx, None, ""
        base = f"{idx:03d}"
        try:
            if d.get("textKind"):
                return idx, True, work_text(base, d)
            anim = d.get("animationUrl") or ""
            popup = d.get("popupUrl") or ""
            static = d.get("staticUrl") or ""
            snd = d.get("soundUrl") or ""
            snd_ext = (os.path.splitext(snd.split("?")[0])[1] or ".m4a") if snd else ""
            outs = []
            if anim:
                if need_apng:
                    outs.append(base + ".png")
                if want_gif:
                    outs.append(base + ".gif")
                if want_webp:
                    outs.append(base + ".webp")
            elif static:
                outs.append(base + ".png")
            if popup:
                outs.append(base + "_popup.png")
            if snd:
                outs.append(base + "_sound" + snd_ext)
            if not outs:
                return idx, False, f"{base} ✗ 沒有圖檔網址"
            paths = [os.path.join(folder, o) for o in outs]
            if all(os.path.exists(p) and os.path.getsize(p) > 0 for p in paths):
                return idx, True, f"{base} 已存在，略過"

            if anim:
                data = http_get(anim, referer=ref)
                if need_apng:
                    _save_bytes(os.path.join(folder, base + ".png"), data)
                if want_gif:
                    _save_via(os.path.join(folder, base + ".gif"), apng_to_gif, data)
                if want_webp:
                    _save_via(os.path.join(folder, base + ".webp"), apng_to_webp, data)
                msg = f"{base} 動態 ✓"
            else:
                _save_bytes(os.path.join(folder, base + ".png"), http_get(static, referer=ref))
                msg = f"{base} 靜態 ✓"
            if popup:  # 「全螢幕特效貼圖」的特效層
                _save_bytes(os.path.join(folder, base + "_popup.png"), http_get(popup, referer=ref))
            if snd:
                _save_bytes(os.path.join(folder, base + "_sound" + snd_ext), http_get(snd, referer=ref))
            return idx, True, msg
        except Exception as e:
            return idx, False, f"{base} ✗ {e}"

    ok = fail = done = 0
    total = len(items)
    with ThreadPoolExecutor(WORKERS) as ex:
        futs = [ex.submit(work, i, d) for i, d in enumerate(items, 1)]
        for f in as_completed(futs):
            _, good, msg = f.result()
            done += 1
            if good is True:
                ok += 1
                vlog("  " + msg)
            elif good is False:
                fail += 1
                log(f"  [{title}] {msg}")
            if progress:
                progress(done, total)
    # 每次都重寫：舊版下載的資料夾重新下載一次（檔案都會略過）就能補上這份資訊
    write_meta(folder, build_meta(kind, pack_id, title, items, ref))
    if stop and stop.is_set():
        log("已中止")
    log(("完成：" if verbose else "  → ") + f"成功 {ok}、失敗 {fail}")
    return folder, ok, fail


def download_author(author_id, out_root, formats, log=print, progress=None, stop=None,
                    prefetched=None, selected=None, limit=None, shop="stickershop"):
    """下載作者全部作品：out_root/<作者>_<ID>/<系列>_<ID>/（表情貼作者多一層「表情貼」標記）。
    selected 為 {(kind,id)} 時只抓這些；limit 只抓前 N 組。回傳作者資料夾。"""
    if prefetched:
        name, packs = prefetched
    else:
        log(f"作者 ID：{author_id}，讀取作品清單…")
        name, packs = fetch_author(author_id, shop)
    if selected is not None:
        packs = [p for p in packs if (p["kind"], p["id"]) in selected]
    if limit:
        packs = packs[:limit]
    if not packs:
        raise RuntimeError("沒有要下載的系列")
    tag = "_表情貼" if shop == "emojishop" else ""  # 避免和同編號的貼圖作者資料夾撞名
    folder = os.path.join(out_root, f"{name}{tag}_{author_id}")
    os.makedirs(folder, exist_ok=True)
    n = len(packs)
    log(f"作者「{name}」共 {n} 個系列 → {folder}")

    tot_ok = tot_fail = bad_packs = 0
    for i, p in enumerate(packs):
        if stop and stop.is_set():
            log("已中止")
            break
        log(f"[{i + 1}/{n}] {p['title']}（{KINDS[p['kind']][0]}）")
        sub = (lambda a, b, i=i: progress(i + a / max(b, 1), n)) if progress else None
        try:
            _, ok, fail = download_pack((p["kind"], p["id"]), folder, formats, log=log,
                                        progress=sub, stop=stop, verbose=False)
            tot_ok += ok
            tot_fail += fail
        except Exception as e:
            bad_packs += 1
            log(f"  ✗ 略過此系列：{e}")
        if progress:
            progress(i + 1, n)
    log(f"全部結束：{n} 個系列、成功 {tot_ok} 張、失敗 {tot_fail} 張、略過系列 {bad_packs}")
    return folder


# ---------------------------------------------------------------- GUI
def run_gui(hook=None):
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox
    try:
        from PIL import ImageTk
    except Exception:
        ImageTk = None

    try:  # Windows 高 DPI 不模糊
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

    BG = "#ececec"
    THUMB = 96
    root = tk.Tk()
    root.title("LINE 貼圖下載器（個人自用）")
    root.geometry("760x780")
    root.minsize(600, 560)

    pad = {"padx": 10, "pady": 4}
    frm = ttk.Frame(root)
    frm.pack(fill="both", expand=True, padx=8, pady=8)
    frm.columnconfigure(1, weight=1)
    frm.rowconfigure(5, weight=3)
    frm.rowconfigure(6, weight=1)

    # --- 輸入列 ---
    ttk.Label(frm, text="網址 / ID").grid(row=0, column=0, sticky="w", **pad)
    url_var = tk.StringVar()
    ent = ttk.Entry(frm, textvariable=url_var)
    ent.grid(row=0, column=1, sticky="ew", **pad)
    in_btns = ttk.Frame(frm)
    in_btns.grid(row=0, column=2, **pad)
    btn_paste = ttk.Button(in_btns, text="貼上", width=6)
    btn_paste.pack(side="left")
    btn_prev = ttk.Button(in_btns, text="預覽", width=6)
    btn_prev.pack(side="left", padx=(4, 0))

    ttk.Label(frm, text="存到資料夾").grid(row=1, column=0, sticky="w", **pad)
    out_var = tk.StringVar(value=os.path.join(os.path.expanduser("~"), "Downloads", "LINE貼圖"))
    ttk.Entry(frm, textvariable=out_var).grid(row=1, column=1, sticky="ew", **pad)

    def browse():
        d = filedialog.askdirectory(initialdir=out_var.get() if os.path.isdir(out_var.get()) else None)
        if d:
            out_var.set(d)
    ttk.Button(frm, text="瀏覽…", command=browse, width=6).grid(row=1, column=2, **pad)

    ttk.Label(frm, text="動態貼圖格式").grid(row=2, column=0, sticky="w", **pad)
    fmt_box = ttk.Frame(frm)
    fmt_box.grid(row=2, column=1, columnspan=2, sticky="w", **pad)
    v_apng = tk.BooleanVar(value=True)
    v_gif = tk.BooleanVar(value=True)
    v_webp = tk.BooleanVar(value=False)
    ttk.Checkbutton(fmt_box, text="APNG 原檔(.png)", variable=v_apng).pack(side="left", padx=(0, 12))
    ttk.Checkbutton(fmt_box, text="GIF", variable=v_gif).pack(side="left", padx=(0, 12))
    ttk.Checkbutton(fmt_box, text="動態 WebP", variable=v_webp).pack(side="left")
    if not HAVE_PIL:
        v_gif.set(False)
        ttk.Label(frm, text="（未安裝 Pillow：只能存原始檔，請 pip install pillow）",
                  foreground="#b45309").grid(row=3, column=1, columnspan=2, sticky="w", padx=10)

    btn_row = ttk.Frame(frm)
    btn_row.grid(row=4, column=0, columnspan=3, sticky="ew", **pad)
    btn_go = ttk.Button(btn_row, text="開始下載")
    btn_go.pack(side="left")
    btn_stop = ttk.Button(btn_row, text="中止", state="disabled")
    btn_stop.pack(side="left", padx=8)
    btn_open = ttk.Button(btn_row, text="開啟資料夾", state="disabled")
    btn_open.pack(side="left")
    bar = ttk.Progressbar(btn_row, mode="determinate")
    bar.pack(side="left", fill="x", expand=True, padx=(12, 0))

    # --- 預覽區 ---
    pv_box = ttk.LabelFrame(frm, text="預覽")
    pv_box.grid(row=5, column=0, columnspan=3, sticky="nsew", padx=10, pady=(6, 4))
    pv_box.columnconfigure(0, weight=1)
    pv_box.rowconfigure(1, weight=1)
    pv_head = ttk.Frame(pv_box)
    pv_head.grid(row=0, column=0, columnspan=2, sticky="ew", padx=6, pady=(4, 2))
    pv_info = ttk.Label(pv_head, text="貼上網址後按「預覽」，會先列出縮圖（作者頁可勾選要下載的系列）")
    pv_info.pack(side="left")
    btn_none = ttk.Button(pv_head, text="全不選", width=7)
    btn_all = ttk.Button(pv_head, text="全選", width=6)
    canvas = tk.Canvas(pv_box, highlightthickness=0, bg=BG)
    vsb = ttk.Scrollbar(pv_box, orient="vertical", command=canvas.yview)
    canvas.configure(yscrollcommand=vsb.set)
    canvas.grid(row=1, column=0, sticky="nsew", padx=(6, 0), pady=(0, 6))
    vsb.grid(row=1, column=1, sticky="ns", pady=(0, 6), padx=(0, 4))
    inner = tk.Frame(canvas, bg=BG)
    win = canvas.create_window((0, 0), window=inner, anchor="nw")
    inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))

    def on_wheel(e):
        canvas.yview_scroll(int(-e.delta / 120), "units")
    canvas.bind("<Enter>", lambda e: canvas.bind_all("<MouseWheel>", on_wheel))
    canvas.bind("<Leave>", lambda e: canvas.unbind_all("<MouseWheel>"))

    # --- 記錄 ---
    log_box = tk.Text(frm, height=6, wrap="word", state="disabled",
                      font=("Consolas", 10), relief="solid", borderwidth=1)
    log_box.grid(row=6, column=0, columnspan=3, sticky="nsew", padx=10, pady=(2, 8))

    q = queue.Queue()
    jobq = queue.Queue()
    state = {"folder": None, "stop": None, "gen": 0, "preview": None,
             "cells": [], "cols": 0, "cell_w": 112, "labels": {}, "photos": {}, "vars": []}

    def log(msg):
        q.put(("log", msg))

    # 縮圖背景下載（daemon 執行緒，不會卡住關閉視窗）
    def thumb_worker():
        while True:
            gen, idx, url = jobq.get()
            if gen != state["gen"]:
                continue
            try:
                data = http_get(url, retries=2)
            except Exception:
                data = None
            q.put(("thumb", (gen, idx, data)))
    for _ in range(6):
        threading.Thread(target=thumb_worker, daemon=True).start()

    def make_photo(data):
        if HAVE_PIL and ImageTk:
            im = Image.open(io.BytesIO(data))
            im.seek(0)
            im = im.convert("RGBA")
            im.thumbnail((THUMB, THUMB))
            return ImageTk.PhotoImage(im)
        ph = tk.PhotoImage(data=data)
        k = max(1, -(-max(ph.width(), ph.height()) // THUMB))
        return ph.subsample(k)

    def reflow(force=False):
        w = canvas.winfo_width()
        cols = max(1, w // state["cell_w"])
        if cols == state["cols"] and not force:
            return
        state["cols"] = cols
        for i, c in enumerate(state["cells"]):
            c.grid(row=i // cols, column=i % cols, padx=4, pady=4, sticky="n")

    def on_canvas_cfg(e):
        canvas.itemconfigure(win, width=e.width)
        reflow()
    canvas.bind("<Configure>", on_canvas_cfg)

    def clear_preview():
        state["gen"] += 1
        with jobq.mutex:
            jobq.queue.clear()
        for c in state["cells"]:
            c.destroy()
        state.update(cells=[], labels={}, photos={}, vars=[], cols=0)
        btn_all.pack_forget()
        btn_none.pack_forget()
        canvas.yview_moveto(0)

    def add_cell(idx, url, caption, var=None, badge=""):
        cell = tk.Frame(inner, bg=BG, width=state["cell_w"] - 8)
        img = tk.Label(cell, text="…", bg=BG, fg="#888", width=THUMB // 8, height=THUMB // 16)
        img.pack()
        state["labels"][idx] = img
        txt = (badge + " " if badge else "") + caption
        if txt.strip():
            tk.Label(cell, text=txt, bg=BG, wraplength=state["cell_w"] - 12,
                     justify="center", font=("Microsoft JhengHei UI", 8),
                     height=3 if var is not None else 1, anchor="n").pack()  # 固定高度讓勾選框對齊
        if var is not None:
            tk.Checkbutton(cell, text="下載", variable=var, bg=BG, activebackground=BG).pack()
            img.bind("<Button-1>", lambda e, v=var: v.set(not v.get()))
            img.configure(cursor="hand2")
        state["cells"].append(cell)
        if url:
            jobq.put((state["gen"], idx, url))

    def show_preview(p):
        clear_preview()
        state["preview"] = p
        if p["kind"] in AUTHOR_SHOPS:
            state["cell_w"] = 124
            packs = p["packs"]
            n_anim = sum(1 for k in packs if k["anim"])
            pv_info.configure(text=f"作者「{p['name']}」：{len(packs)} 個系列（{n_anim} 個含動態）　勾選要下載的系列，每個系列一個資料夾")
            btn_none.pack(side="right")
            btn_all.pack(side="right", padx=(0, 4))
            for i, k in enumerate(packs):
                var = tk.BooleanVar(value=True)
                state["vars"].append(var)
                badge = "▶" if k["anim"] else ""
                if k["kind"] == "emoji":
                    badge += "[表情貼]"
                add_cell(i, k["cover"], k["title"], var, badge)
            p["vars"] = state["vars"]
        else:
            state["cell_w"] = 108
            items = p["items"]
            n_anim = sum(1 for d in items if d.get("animationUrl"))
            n_text = sum(1 for d in items if d.get("textKind"))
            extra = f"，可自訂文字 {n_text}" if n_text else ""
            pv_info.configure(text=f"「{p['title']}」：{len(items)} 張（動態 {n_anim}{extra}）")
            for i, d in enumerate(items):
                badge = "▶" if d.get("animationUrl") else ("✏" if d.get("textKind") else "")
                add_cell(i, d.get("staticUrl") or d.get("fallbackStaticUrl") or "",
                         f"{i + 1:03d}", None, badge)
        reflow(force=True)

    def set_all(v):
        for var in state["vars"]:
            var.set(v)
    btn_all.configure(command=lambda: set_all(True))
    btn_none.configure(command=lambda: set_all(False))

    def pump():
        try:
            while True:
                kind, val = q.get_nowait()
                if kind == "log":
                    log_box.configure(state="normal")
                    log_box.insert("end", val + "\n")
                    log_box.see("end")
                    log_box.configure(state="disabled")
                elif kind == "prog":
                    bar["maximum"], bar["value"] = val[1], val[0]
                elif kind == "done":
                    btn_go.configure(state="normal")
                    btn_stop.configure(state="disabled")
                    if state["folder"]:
                        btn_open.configure(state="normal")
                elif kind == "preview":
                    show_preview(val)
                elif kind == "pvdone":
                    btn_prev.configure(state="normal")
                elif kind == "thumb":
                    gen, idx, data = val
                    lbl = state["labels"].get(idx)
                    if gen != state["gen"] or lbl is None:
                        continue
                    try:
                        ph = make_photo(data)
                        state["photos"][idx] = ph  # 留參考，避免被回收
                        lbl.configure(image=ph, text="", width=0, height=0)
                    except Exception:
                        lbl.configure(text="?")
        except queue.Empty:
            pass
        root.after(100, pump)

    def do_preview():
        text = url_var.get().strip()
        t = parse_target(text)
        if not t:
            messagebox.showinfo("提示", "請先貼上 LINE Store 的貼圖商品頁或作者頁網址")
            return
        btn_prev.configure(state="disabled")
        log("讀取中…")

        def w():
            try:
                if t[0] in AUTHOR_SHOPS:
                    name, packs = fetch_author(t[1], AUTHOR_SHOPS[t[0]])
                    q.put(("preview", {"text": text, "kind": t[0], "id": t[1],
                                       "name": name, "packs": packs}))
                else:
                    title, items = fetch_pack(*t)
                    q.put(("preview", {"text": text, "kind": t[0], "id": t[1],
                                       "title": title, "items": items}))
            except Exception as e:
                log(f"✗ {e}")
            q.put(("pvdone", None))
        threading.Thread(target=w, daemon=True).start()

    def paste():
        try:
            url_var.set(root.clipboard_get().strip())
        except Exception:
            return
        do_preview()
    btn_paste.configure(command=paste)
    btn_prev.configure(command=do_preview)

    def worker(t, out, fmts, selected, prefetched):
        try:
            prog = lambda a, b: q.put(("prog", (a, b)))
            if t[0] in AUTHOR_SHOPS:
                state["folder"] = download_author(t[1], out, fmts, log=log, progress=prog,
                                                  stop=state["stop"], prefetched=prefetched,
                                                  selected=selected, shop=AUTHOR_SHOPS[t[0]])
            else:
                state["folder"] = download_pack(t, out, fmts, log=log, progress=prog,
                                                stop=state["stop"], prefetched=prefetched)[0]
        except Exception as e:
            log(f"✗ {e}")
        q.put(("done", None))

    def start():
        text = url_var.get().strip()
        t = parse_target(text)
        if not t:
            messagebox.showinfo("提示", "請先貼上 LINE Store 的貼圖商品頁或作者頁網址")
            return
        fmts = [n for n, v in (("apng", v_apng), ("gif", v_gif), ("webp", v_webp)) if v.get()]
        if not fmts:
            messagebox.showinfo("提示", "至少勾一種格式")
            return
        pv = state["preview"]
        if pv and (pv["kind"], pv["id"]) != t:
            pv = None  # 預覽的不是目前這個網址
        selected = prefetched = None
        if pv and pv["kind"] in AUTHOR_SHOPS:
            selected = {(k["kind"], k["id"]) for k, v in zip(pv["packs"], pv["vars"]) if v.get()}
            if not selected:
                messagebox.showinfo("提示", "沒有勾選任何系列")
                return
            prefetched = (pv["name"], pv["packs"])
        elif pv:
            prefetched = (pv["title"], pv["items"])
        state["folder"] = None
        state["stop"] = threading.Event()
        btn_go.configure(state="disabled")
        btn_stop.configure(state="normal")
        btn_open.configure(state="disabled")
        bar["value"] = 0
        threading.Thread(target=worker, args=(t, out_var.get(), fmts, selected, prefetched),
                         daemon=True).start()

    def stop():
        if state["stop"]:
            state["stop"].set()

    def open_folder():
        if state["folder"] and os.path.isdir(state["folder"]):
            os.startfile(state["folder"])

    btn_go.configure(command=start)
    btn_stop.configure(command=stop)
    btn_open.configure(command=open_folder)
    ent.bind("<Return>", lambda e: do_preview())
    ent.focus_set()
    log("貼上貼圖商品頁或作者頁網址，按 Enter 或「預覽」看縮圖，再按「開始下載」。\n"
        "貼圖作者頁：https://store.line.me/stickershop/author/681334/zh-Hant\n"
        "表情貼作者頁：https://store.line.me/emojishop/author/7819400/zh-Hant\n"
        "只供個人本機使用，請勿散布或商用。")
    pump()
    if hook:
        hook(root, {"url_var": url_var, "out_var": out_var, "do_preview": do_preview,
                    "start": start, "state": state, "q": q, "canvas": canvas})
    root.mainloop()


# ---------------------------------------------------------------- 入口
def main():
    for s in (sys.stdout, sys.stderr):  # cp950 主控台印不出 ✓ ✗
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    ap = argparse.ArgumentParser(description="LINE 貼圖下載器（個人自用）")
    ap.add_argument("url", nargs="?", help="商品頁 / 作者頁網址或貼圖組 ID；省略則開啟 GUI")
    ap.add_argument("-o", "--out", default=os.path.join(os.path.expanduser("~"), "Downloads", "LINE貼圖"))
    ap.add_argument("-f", "--formats", default="apng,gif", help="apng,gif,webp（逗號分隔）")
    ap.add_argument("--limit", type=int, default=None, help="作者頁只抓前 N 個系列（測試用）")
    a = ap.parse_args()
    if not a.url:
        run_gui()
        return
    fmts = [x.strip() for x in a.formats.split(",") if x.strip()]
    try:
        t = parse_target(a.url)
        if not t:
            raise RuntimeError("看不出網址或 ID，請貼 LINE Store 商品頁 / 作者頁網址")
        if t[0] in AUTHOR_SHOPS:
            download_author(t[1], a.out, fmts, limit=a.limit, shop=AUTHOR_SHOPS[t[0]])
        else:
            download_pack(t, a.out, fmts)
    except Exception as e:
        print(f"✗ {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
