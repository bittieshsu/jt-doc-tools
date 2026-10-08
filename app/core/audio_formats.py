"""錄音 / 錄影檔：收哪些格式、怎麼認 —— **全站只有這一份**。

## 為什麼要獨立成一支

「會議錄音轉逐字稿」收哪些副檔名、「我的工作區」收哪些錄音檔，原本只有前者
有一份清單（`meeting_transcribe.ACCEPT_EXTS`）。工作區要收錄音檔（給轉逐字稿
「從工作區載入」用）時，如果在工作區那邊再寫一份，兩份遲早會漂 —— 症狀是
**工作區存得進去、轉逐字稿卻挑不到**（或反過來），而且沒有任何錯誤訊息
（v1.15.88 記過同一家族）。所以兩邊都從這裡拿。

## 認格式：看內容，不看副檔名

規矩跟 `workspace.detect_kind()` 一樣 —— 只看副檔名的話，任何東西改名成
`.mp3` 都進得來。各格式的訊號：

| 格式 | 訊號 | 強度 |
|---|---|---|
| WAV | `RIFF`／`RF64` ＋ `WAVE` ＋ 找得到 `fmt ` 區塊 | 強 |
| FLAC | `fLaC` ＋ 第一個區塊是 STREAMINFO（長度 34） | 強 |
| Ogg / Opus | `OggS` ＋ 版本 0 ＋ 串流開頭旗標 ＋ 認得的編碼識別（OpusHead / vorbis…） | 強 |
| m4a / mp4 / mov | 第一個 box 是 `ftyp`，而且品牌是影音（不是 HEIC / AVIF 這類圖片） | 強 |
| mkv / webm | EBML `1A 45 DF A3` ＋ DocType 是 matroska / webm | 強 |
| MP3（沒有 ID3） | 只有 frame sync（`FF Ex`） | **弱** |
| ADTS AAC | 只有 frame sync（`FF F1` / `FF F9`） | **弱** |

**弱訊號要多驗幾個連續 frame 才收**：任何開頭剛好是 `FF Fx` 的東西都長得像
一個 frame 的開頭，但要**連續四個** frame 的長度都剛好接得上、參數一致，
隨機資料幾乎不可能做到（跟純文字那次一樣，「沒有明確簽章」的型別要另外想
判準，不可以單憑頭兩個位元組）。frame sync 只在**檔頭**（或 ID3 標籤之後）
找，**不往後掃** —— 往後掃的話任何二進位檔都找得到一個 `FF Fx`。

## 只讀檔頭

錄音檔動輒上百 MB。判斷格式只需要檔頭（`HEAD_BYTES`），**不可以為了認格式
把整份讀進記憶體**（`sniff_stream()` 只讀檔頭；有 ID3 標籤時跳過標籤再讀一段）。

## 副檔名只在「同一家族」裡二選一

m4a / mp4 / mov 都是同一種容器（ISO BMFF），Ogg 與 Opus 也是；內容決定是哪一個
家族，**使用者的檔名只在同一家族裡挑**（`會議.m4a` 存進來仍是 `.m4a`，不會被改名
成 `.mp4`）—— 跟純文字「檔名只在 .txt 與 .md 之間挑」是同一條規則：檔名永遠
不能讓內容不對的東西過關。
"""
from __future__ import annotations

import io
from pathlib import PurePath
from typing import BinaryIO, Optional

#: 收得下的副檔名（**順序就是畫面上列的順序**：先音訊、後影片）。
AUDIO_EXTS: tuple[str, ...] = (".m4a", ".mp3", ".wav", ".aac", ".ogg", ".opus", ".flac",
                                ".mp4", ".mov", ".mkv", ".webm")

#: 純音訊 vs 帶影像的 —— 只影響畫面上的圖示。
VIDEO_EXTS: frozenset[str] = frozenset({".mp4", ".mov", ".mkv", ".webm"})

#: 副檔名 → MIME。**每個 MIME 只對應一個副檔名**：工作區的 `ALLOWED` 是
#: 「MIME → 副檔名」的表，兩個副檔名共用一個 MIME 的話其中一個會被蓋掉。
MIME_BY_EXT: dict[str, str] = {
    ".m4a": "audio/mp4",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".aac": "audio/aac",
    ".ogg": "audio/ogg",
    ".opus": "audio/opus",
    ".flac": "audio/flac",
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".mkv": "video/x-matroska",
    ".webm": "video/webm",
}

#: 認格式要讀的檔頭長度。最慢的情況是 ADTS 的四個 frame（單一 frame 最長
#: 8191 bytes）—— 64 KB 綽綽有餘。
HEAD_BYTES = 64 * 1024

#: 弱訊號（MP3 / ADTS）要連續接得上的 frame 數。
MIN_FRAMES = 4

#: ID3 標籤後面常有填充用的 0，跳過的上限。
_ID3_PAD_SKIP = 4096


def ext_of(name: str) -> str:
    return PurePath(name or "").suffix.lower()


# --------------------------------------------------------------------- frame sync

# MPEG audio 的 bitrate 表（kbps），索引 0（free）與 15（bad）不收。
_BR_V1 = {
    3: (0, 32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448),   # Layer I
    2: (0, 32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384),      # Layer II
    1: (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320),       # Layer III
}
_BR_V2 = {
    3: (0, 32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256),
    2: (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),
    1: (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),
}
_SR = {3: (44100, 48000, 32000), 2: (22050, 24000, 16000), 0: (11025, 12000, 8000)}


def _mpeg_frame(b: bytes, i: int) -> Optional[tuple[int, tuple]]:
    """位置 i 是不是一個合法的 MPEG audio frame 標頭。回 (frame 長度, 一致性特徵)。"""
    if i + 4 > len(b):
        return None
    h = int.from_bytes(b[i:i + 4], "big")
    if (h >> 21) & 0x7FF != 0x7FF:
        return None
    ver = (h >> 19) & 3          # 0 = MPEG 2.5、1 = 保留、2 = MPEG 2、3 = MPEG 1
    layer = (h >> 17) & 3        # 0 = 保留、1 = Layer III、2 = Layer II、3 = Layer I
    br_idx = (h >> 12) & 0xF
    sr_idx = (h >> 10) & 3
    pad = (h >> 9) & 1
    if ver == 1 or layer == 0 or br_idx in (0, 15) or sr_idx == 3 or (h & 3) == 2:
        return None
    br = (_BR_V1 if ver == 3 else _BR_V2)[layer][br_idx] * 1000
    sr = _SR[ver][sr_idx]
    if layer == 3:
        length = (12 * br // sr + pad) * 4
    elif layer == 1 and ver != 3:
        length = 72 * br // sr + pad
    else:
        length = 144 * br // sr + pad
    if length < 24:
        return None
    return length, (ver, layer, sr_idx)


def _adts_frame(b: bytes, i: int) -> Optional[tuple[int, tuple]]:
    """位置 i 是不是一個合法的 ADTS（AAC）frame 標頭。"""
    if i + 7 > len(b):
        return None
    # 12 bits 的 sync ＋ layer 必須是 00（MPEG audio 的 layer 00 是保留值，兩者互斥）
    if b[i] != 0xFF or (b[i + 1] & 0xF6) != 0xF0:
        return None
    sf = (b[i + 2] >> 2) & 0xF
    if sf > 12:
        return None
    ch = ((b[i + 2] & 1) << 2) | (b[i + 3] >> 6)
    flen = ((b[i + 3] & 3) << 11) | (b[i + 4] << 3) | (b[i + 5] >> 5)
    hdr = 7 if (b[i + 1] & 1) else 9
    if flen <= hdr:
        return None
    return flen, (b[i + 1] & 0x08, sf, ch)


def _frame_chain(b: bytes, parse, whole: bool) -> bool:
    """從檔頭開始，連續 `MIN_FRAMES` 個 frame 都接得上、參數一致才算。

    整份檔案都在 `b` 裡（`whole`）而且剛好在檔尾結束時，少於四個也收（至少兩個）
    —— 只有幾十毫秒的錄音也是錄音。
    """
    pos, sig, n = 0, None, 0
    while n < MIN_FRAMES:
        fr = parse(b, pos)
        if fr is None:
            return whole and n >= 2 and pos == len(b)
        length, s = fr
        if sig is None:
            sig = s
        elif s != sig:
            return False
        n += 1
        pos += length
    return True


# --------------------------------------------------------------------- 各種容器

_ISO_AUDIO_BRANDS = frozenset({b"M4A ", b"M4B ", b"M4P ", b"F4A ", b"F4B "})
_ISO_AV_BRANDS = _ISO_AUDIO_BRANDS | frozenset({
    b"qt  ", b"isom", b"iso2", b"iso3", b"iso4", b"iso5", b"iso6", b"iso7", b"iso8",
    b"iso9", b"mp41", b"mp42", b"mp71", b"avc1", b"dash", b"MSNV", b"mmp4", b"M4V ",
    b"M4VH", b"M4VP", b"F4V ", b"3gp4", b"3gp5", b"3gp6", b"3g2a", b"3g2b", b"3g2c",
    b"XAVC", b"NDAS",
})
#: 同樣是 `ftyp` 開頭的**圖片**（HEIC / AVIF / 相機 RAW）—— 主品牌是這些就不收。
_ISO_IMAGE_BRANDS = frozenset({
    b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"hevm", b"hevs",
    b"mif1", b"msf1", b"miaf", b"avif", b"avis", b"crx ", b"jpeg", b"jpgs",
})
_ISO_FAMILY = (".m4a", ".mp4", ".mov")


def _iso_bmff(b: bytes, name: str) -> Optional[tuple[str, str]]:
    if len(b) < 16 or b[4:8] != b"ftyp":
        return None
    size = int.from_bytes(b[:4], "big")
    if size < 16 or size > 1024 or (size - 16) % 4 or size > len(b):
        return None
    major = b[8:12]
    if major in _ISO_IMAGE_BRANDS:
        return None
    brands = [major] + [b[i:i + 4] for i in range(16, size, 4)]
    if not any(br in _ISO_AV_BRANDS for br in brands):
        return None
    nm = ext_of(name)
    if nm in _ISO_FAMILY:
        ext = nm
    elif major in _ISO_AUDIO_BRANDS:
        ext = ".m4a"
    elif major == b"qt  ":
        ext = ".mov"
    else:
        ext = ".mp4"
    return MIME_BY_EXT[ext], ext


def _ebml(b: bytes) -> Optional[tuple[str, str]]:
    if b[:4] != b"\x1a\x45\xdf\xa3":
        return None
    i = b.find(b"\x42\x82", 4, 128)          # DocType 元素，就在 EBML 標頭裡
    if i < 0 or i + 3 > len(b):
        return None
    sz = b[i + 2]
    if not sz & 0x80:                         # DocType 只會是一個位元組的長度
        return None
    doc = b[i + 3:i + 3 + (sz & 0x7F)].rstrip(b"\x00")
    if doc == b"webm":
        return MIME_BY_EXT[".webm"], ".webm"
    if doc == b"matroska":
        return MIME_BY_EXT[".mkv"], ".mkv"
    return None


def _riff_wave(b: bytes) -> Optional[tuple[str, str]]:
    if b[:4] not in (b"RIFF", b"RF64") or b[8:12] != b"WAVE":
        return None
    pos = 12
    while pos + 8 <= len(b):
        cid = b[pos:pos + 4]
        if not all(0x20 <= c <= 0x7E for c in cid):
            return None
        size = int.from_bytes(b[pos + 4:pos + 8], "little")
        if cid == b"fmt ":
            if size < 14 or pos + 10 > len(b):
                return None
            if int.from_bytes(b[pos + 8:pos + 10], "little") == 0:   # 格式代碼 0 不存在
                return None
            return MIME_BY_EXT[".wav"], ".wav"
        if cid == b"data":                    # 還沒看到 fmt 就到了資料區 → 壞檔
            return None
        pos += 8 + size + (size & 1)
    return None


def _flac(b: bytes) -> Optional[tuple[str, str]]:
    if b[:4] == b"fLaC" and len(b) >= 8 and (b[4] & 0x7F) == 0 and b[5:8] == b"\x00\x00\x22":
        return MIME_BY_EXT[".flac"], ".flac"
    return None


def _ogg(b: bytes, name: str) -> Optional[tuple[str, str]]:
    if b[:4] != b"OggS" or len(b) < 28 or b[4] != 0 or not (b[5] & 0x02):
        return None
    off = 27 + b[26]
    pkt = b[off:off + 8]
    if pkt.startswith(b"OpusHead"):
        ext = ".ogg" if ext_of(name) == ".ogg" else ".opus"
    elif (pkt[:7] == b"\x01vorbis" or pkt[:5] == b"\x7fFLAC"
          or pkt == b"Speex   " or pkt[:7] == b"\x80theora"):
        ext = ".ogg"
    else:
        return None
    return MIME_BY_EXT[ext], ext


def _id3_len(b: bytes) -> Optional[int]:
    """ID3v2 標籤的總長度（含標頭），格式不對就 None。"""
    if len(b) < 10 or b[:3] != b"ID3" or b[3] not in (2, 3, 4) or b[4] == 0xFF:
        return None
    if any(x >= 0x80 for x in b[6:10]):
        return None
    size = (b[6] << 21) | (b[7] << 14) | (b[8] << 7) | b[9]
    return 10 + size + (10 if b[5] & 0x10 else 0)


def _after_id3(b: bytes, whole: bool) -> Optional[tuple[str, str]]:
    k = 0
    while k < len(b) and k < _ID3_PAD_SKIP and b[k] == 0:
        k += 1
    b = b[k:]
    hit = _flac(b)
    if hit:
        return hit
    if _frame_chain(b, _mpeg_frame, whole):
        return MIME_BY_EXT[".mp3"], ".mp3"
    if _frame_chain(b, _adts_frame, whole):
        return MIME_BY_EXT[".aac"], ".aac"
    return None


def _sniff_head(b: bytes, name: str, whole: bool) -> Optional[tuple[str, str]]:
    for check in (_riff_wave, _flac, _ebml):
        hit = check(b)
        if hit:
            return hit
    hit = _ogg(b, name) or _iso_bmff(b, name)
    if hit:
        return hit
    if _frame_chain(b, _mpeg_frame, whole):
        return MIME_BY_EXT[".mp3"], ".mp3"
    if _frame_chain(b, _adts_frame, whole):
        return MIME_BY_EXT[".aac"], ".aac"
    return None


# --------------------------------------------------------------------- 入口

def sniff_stream(f: BinaryIO, name: str = "") -> Optional[tuple[str, str]]:
    """認一個可 seek 的檔案物件是不是收得下的錄音 / 錄影，回 (mime, ext)。

    **只讀檔頭**（最多兩段 `HEAD_BYTES`：檔頭、以及 ID3 標籤之後那一段），
    不管檔案多大都不會整份讀進記憶體。讀完把位置放回開頭。
    """
    try:
        f.seek(0, io.SEEK_END)
        size = f.tell()
        f.seek(0)
        head = f.read(HEAD_BYTES)
        whole = len(head) >= size
        if head[:3] == b"ID3":
            tag = _id3_len(head)
            if tag is None or tag >= size:
                return None
            f.seek(tag)
            body = f.read(HEAD_BYTES)
            return _after_id3(body, tag + len(body) >= size)
        return _sniff_head(head, name, whole)
    except (OSError, ValueError):
        return None
    finally:
        try:
            f.seek(0)
        except (OSError, ValueError):
            pass


def sniff(data: bytes, name: str = "") -> Optional[tuple[str, str]]:
    """同 `sniff_stream()`，給已經在記憶體裡的位元組用（不會另外複製一份）。"""
    if not data:
        return None
    return sniff_stream(io.BytesIO(data), name)


def is_av_ext(ext: str) -> bool:
    return (ext or "").lower() in MIME_BY_EXT
