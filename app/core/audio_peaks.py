"""錄音檔的波形（每一小段的最大音量），**在伺服器端從 WAV 直接讀**。

為什麼要在伺服器端算（2026-10-03 使用者回報「波形圖怎麼只剩一條線」）：
瀏覽器畫波形靠 `decodeAudioData`，它要先把**整份檔案下載下來、再解成整份 PCM 放進記憶體** ——
63 分鐘的會議解開來就超過 1 GB，分頁會直接掛掉，所以畫面設了「超過 60 MB 不畫」的門檻。
而會議錄音最常超過門檻的正是 WAV（沒壓縮，一小時約 350 MB）。

WAV 不需要解碼器：檔頭說明格式之後，後面就是一筆一筆的樣本。這裡**逐段讀**，
每一段只取最大的絕對值，記憶體用量跟檔案大小無關；結果只有 1,200 個數字。

只處理 WAV（含 RF64 與 WAVE_FORMAT_EXTENSIBLE）；其他格式回 `None`，
由瀏覽器照原本的方式解碼（小檔）或不畫（大檔）。**這台伺服器沒有 ffmpeg**，
不為了波形多裝一個解碼器。

回傳的是**原始**的峰值（0~1，滿刻度＝1）。畫面怎麼把它放大（正規化、開根號）
只在前端做一份 —— 兩邊各做一份的話，伺服器算的與瀏覽器算的會長得不一樣。
"""
from __future__ import annotations

import struct
from pathlib import Path
from typing import Optional

import numpy as np

#: 畫面上的波形分成幾格（跟前端解碼那條路一樣多）
BUCKETS = 1200

_PCM = 1
_FLOAT = 3
_EXTENSIBLE = 0xFFFE


def _read_format(f) -> Optional[tuple[int, int, int, int, int, int]]:
    """讀 RIFF / RF64 的檔頭 → (格式, 聲道數, 每樣本位元, 每格位元組, 資料起點, 資料長度)。"""
    head = f.read(12)
    if len(head) < 12 or head[:4] not in (b"RIFF", b"RF64") or head[8:12] != b"WAVE":
        return None
    fmt = None
    while True:
        ch = f.read(8)
        if len(ch) < 8:
            return None
        cid, clen = ch[:4], struct.unpack("<I", ch[4:])[0]
        if cid == b"fmt ":
            body = f.read(clen)
            if len(body) < 16:
                return None
            tag, nch, _rate, _bps, align, bits = struct.unpack("<HHIIHH", body[:16])
            if tag == _EXTENSIBLE and len(body) >= 26:
                tag = struct.unpack("<H", body[24:26])[0]   # 子格式 GUID 的前兩個位元組
            fmt = (tag, nch, bits, align)
            if clen & 1:
                f.read(1)
        elif cid == b"data":
            if fmt is None:
                return None
            return fmt + (f.tell(), clen)
        else:
            f.seek(clen + (clen & 1), 1)


def _samples(raw: bytes, tag: int, bits: int) -> Optional[np.ndarray]:
    """一段原始位元組 → 浮點樣本（-1~1）。不支援的格式回 None。"""
    if tag == _PCM:
        if bits == 16:
            return np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
        if bits == 8:                       # 8 位元 WAV 是無號數，中點 128
            return (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
        if bits == 24:
            b = np.frombuffer(raw[:len(raw) - len(raw) % 3], dtype=np.uint8).reshape(-1, 3)
            v = (b[:, 0].astype(np.int32) | (b[:, 1].astype(np.int32) << 8)
                 | (b[:, 2].astype(np.int32) << 16))
            v = np.where(v >= 1 << 23, v - (1 << 24), v)
            return v.astype(np.float32) / float(1 << 23)
        if bits == 32:
            return np.frombuffer(raw, dtype="<i4").astype(np.float64) / float(1 << 31)
    if tag == _FLOAT:
        if bits == 32:
            return np.frombuffer(raw, dtype="<f4")
        if bits == 64:
            return np.frombuffer(raw, dtype="<f8")
    return None


def wav_peaks(path: Path, buckets: int = BUCKETS) -> Optional[list[float]]:
    """WAV 的波形峰值（`buckets` 格，每格 0~1）。不是 WAV 或格式不支援時回 None。"""
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            got = _read_format(f)
            if got is None:
                return None
            tag, nch, bits, align, start, length = got
            if nch < 1 or align < 1 or bits not in (8, 16, 24, 32, 64):
                return None
            if _samples(b"\x00" * align, tag, bits) is None:
                return None
            # 錄音程式沒收尾（或 RF64）時長度欄位是 0 / 0xFFFFFFFF / 超過檔案 —— 以檔案為準
            if length == 0 or length > size - start:
                length = size - start
            frames = length // align
            if frames <= 0:
                return None
            n = min(buckets, frames)
            out: list[float] = []
            for i in range(n):
                a = i * frames // n
                b = (i + 1) * frames // n
                f.seek(start + a * align)
                raw = f.read((b - a) * align)
                s = _samples(raw, tag, bits)
                m = float(np.max(np.abs(s))) if s is not None and s.size else 0.0
                out.append(round(min(1.0, m), 4))
            return out
    except (OSError, ValueError, struct.error):
        return None
