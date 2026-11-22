"""修復時間戳記（granule position）有誤差的 Ogg Opus 檔。

有些錄音／轉檔工具（例如瀏覽器錄音、線上剪輯網站）寫出的 Ogg 頁面時間戳記會因為
浮點數捨入少 1 個取樣（應為 12480 卻寫成 12479）。ffmpeg 會容忍，但 exe 內建的
libsndfile 會把整個檔案判定為「malformed」而拒絕解碼，結果在沒有安裝 ffmpeg 的電腦上
按了音效完全沒反應。

這裡依每個 Opus 封包實際的長度重新計算時間戳記並重算 CRC，只在記憶體中修正，
不會改動使用者的檔案。
"""

from __future__ import annotations

import struct

_CAPTURE = b"OggS"
_HEADER_SIZE = 27          # 固定長度的頁首（不含分段表）
_GRANULE_OFFSET = 6
_CRC_OFFSET = 22
_NO_GRANULE = -1           # 這一頁沒有封包結束時，規格規定填 -1
_HEADER_PACKETS = 2        # OpusHead、OpusTags 兩個標頭封包沒有音訊長度


def _make_crc_table() -> tuple[int, ...]:
    # Ogg 用的是多項式 0x04C11DB7、不反轉位元的 CRC-32，與 zlib.crc32 不同，只能自己算
    table = []
    for i in range(256):
        r = i << 24
        for _ in range(8):
            r = ((r << 1) ^ 0x04C11DB7) if r & 0x80000000 else (r << 1)
        table.append(r & 0xFFFFFFFF)
    return tuple(table)


_CRC_TABLE = _make_crc_table()


def ogg_crc(data: bytes | bytearray) -> int:
    """計算 Ogg 頁面的 CRC（計算前 CRC 欄位須先填 0）。

    Args:
        data (bytes | bytearray): 整個頁面的位元組。

    Returns:
        int: 32 位元 CRC 值。
    """
    crc = 0
    table = _CRC_TABLE
    for byte in data:
        crc = ((crc << 8) & 0xFFFFFFFF) ^ table[((crc >> 24) & 0xFF) ^ byte]
    return crc


def _frame_samples(toc: int) -> int:
    """依 Opus TOC 位元組取得單一音框的取樣數（48 kHz）。"""
    config = toc >> 3
    if config < 12:       # SILK：10/20/40/60 ms
        return (480, 960, 1920, 2880)[config % 4]
    if config < 16:       # Hybrid：10/20 ms
        return (480, 960)[config % 2]
    return (120, 240, 480, 960)[config % 4]  # CELT：2.5/5/10/20 ms


def opus_packet_samples(packet: bytes) -> int:
    """計算一個 Opus 封包包含的取樣數（48 kHz），依 RFC 6716 第 3.1 節。

    Args:
        packet (bytes): 一個完整的 Opus 封包。

    Returns:
        int: 取樣數；空封包或格式不完整時回傳 0。
    """
    if not packet:
        return 0
    code = packet[0] & 0x3
    if code == 0:
        frames = 1
    elif code in (1, 2):
        frames = 2
    elif len(packet) > 1:
        frames = packet[1] & 0x3F
    else:
        return 0
    return frames * _frame_samples(packet[0])


def is_ogg_opus(data: bytes) -> bool:
    """資料是否為 Ogg 封裝的 Opus。

    Args:
        data (bytes): 檔案內容（至少前 64 位元組）。

    Returns:
        bool: 是 Ogg Opus 時為 True。
    """
    return data[:4] == _CAPTURE and b"OpusHead" in data[:64]


def repair_opus_granules(data: bytes) -> tuple[bytes, int]:
    """依封包長度重新計算 Ogg Opus 每一頁的時間戳記。

    只處理單一串流（一般音效檔都是），遇到無法解析的結構就停止修改、原樣回傳剩下的部分，
    讓後續解碼器自行判斷。

    Args:
        data (bytes): 原始檔案內容。

    Returns:
        tuple[bytes, int]: (修正後的內容, 被修正的頁數)。
    """
    out = bytearray(data)
    pos = 0
    packet_index = 0
    total = 0
    pending = 0          # 跨頁封包目前累積的長度（只需要第一個位元組與長度）
    first_byte: int | None = None
    second_byte: int | None = None
    fixed = 0
    while pos + _HEADER_SIZE <= len(out) and out[pos:pos + 4] == _CAPTURE:
        segment_count = out[pos + 26]
        lacing = out[pos + _HEADER_SIZE:pos + _HEADER_SIZE + segment_count]
        body = pos + _HEADER_SIZE + segment_count
        page_end = body + sum(lacing)
        if page_end > len(out):
            break
        offset = body
        packet_ended = False
        for seg in lacing:
            if pending == 0 and seg > 0:
                first_byte = out[offset]
                second_byte = out[offset + 1] if seg > 1 else None
            elif pending == 1 and second_byte is None and seg > 0:
                second_byte = out[offset]
            pending += seg
            offset += seg
            if seg < 255:  # 長度小於 255 的分段代表封包在這裡結束
                if packet_index >= _HEADER_PACKETS and first_byte is not None:
                    head = bytes([first_byte] + ([second_byte] if second_byte is not None else []))
                    total += opus_packet_samples(head)
                packet_index += 1
                pending, first_byte, second_byte = 0, None, None
                packet_ended = True
        if packet_index > _HEADER_PACKETS and packet_ended:
            (granule,) = struct.unpack_from("<q", out, pos + _GRANULE_OFFSET)
            # 最後一頁的時間戳記可以比封包總長短（用來裁掉結尾補的靜音），不能去改它
            is_last = page_end >= len(out) or out[pos + 5] & 0x4
            if granule != total and granule != _NO_GRANULE and not (is_last and granule < total):
                struct.pack_into("<q", out, pos + _GRANULE_OFFSET, total)
                struct.pack_into("<I", out, pos + _CRC_OFFSET, 0)
                struct.pack_into("<I", out, pos + _CRC_OFFSET, ogg_crc(out[pos:page_end]))
                fixed += 1
        pos = page_end
    return bytes(out), fixed
