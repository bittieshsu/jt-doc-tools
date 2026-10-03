"""錄音的波形：WAV 由伺服器直接讀（`app/core/audio_peaks.py`、`/tools/meeting-transcribe/peaks/`）。

2026-10-03 使用者回報「波形圖怎麼只剩一條線」：那份錄音是 363 MB 的 WAV（63 分鐘），
超過瀏覽器解碼的 60 MB 門檻，畫面只剩一條時間軸、什麼都沒說。

判準落在**算出來的峰值**：用已知音量的合成錄音，前半段大聲、後半段小聲，
算出來的波形前後要分得出來 —— 只驗「有回 1,200 個數字」的話，全回 0 也會過。
"""
from __future__ import annotations

import math
import struct
import wave
from pathlib import Path

import pytest

from app.core import audio_peaks as ap
from tests.test_meeting_transcribe import FakeJtlw, _configure, unconfigured  # noqa: F401


def _tone(n: int, amp: float, rate: int = 8000) -> list[float]:
    return [amp * math.sin(2 * math.pi * 440 * i / rate) for i in range(n)]


def _wav16(path: Path, samples: list[float], *, channels: int = 1, rate: int = 8000) -> Path:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"".join(struct.pack("<h", int(max(-1, min(1, s)) * 32767))
                               for s in samples))
    return path


def _raw_wav(path: Path, tag: int, bits: int, data: bytes, *, channels: int = 1,
             extensible: bool = False, data_len: int | None = None) -> Path:
    """手寫檔頭（`wave` 模組寫不出 24 位元以外的浮點與 EXTENSIBLE）。"""
    align = channels * bits // 8
    if extensible:
        fmt = struct.pack("<HHIIHH", 0xFFFE, channels, 8000, 8000 * align, align, bits)
        fmt += struct.pack("<HHI", 22, bits, 0) + struct.pack("<H", tag) + b"\x00" * 14
    else:
        fmt = struct.pack("<HHIIHH", tag, channels, 8000, 8000 * align, align, bits)
    body = b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt
    body += b"data" + struct.pack("<I", len(data) if data_len is None else data_len) + data
    path.write_bytes(b"RIFF" + struct.pack("<I", len(body)) + body)
    return path


def _halves(peaks: list[float]) -> tuple[float, float]:
    h = len(peaks) // 2
    return max(peaks[:h]), max(peaks[h:])


def test_loud_then_quiet_shows_up_in_the_peaks(tmp_path):
    p = _wav16(tmp_path / "a.wav", _tone(8000, 0.8) + _tone(8000, 0.1))
    peaks = ap.wav_peaks(p)
    assert len(peaks) == ap.BUCKETS
    loud, quiet = _halves(peaks)
    assert 0.75 < loud <= 0.81, loud
    assert 0.08 < quiet < 0.12, quiet


def test_stereo_takes_the_loudest_channel(tmp_path):
    """只看第一聲道的話，右聲道才有人講話的錄音會畫成一條平線。"""
    left = [0.0] * 4000
    right = _tone(4000, 0.6)
    inter = [v for pair in zip(left, right) for v in pair]
    peaks = ap.wav_peaks(_wav16(tmp_path / "s.wav", inter, channels=2))
    assert max(peaks) > 0.55, max(peaks)


@pytest.mark.parametrize("bits,tag,pack,amp", [
    (8, 1, lambda v: struct.pack("<B", int(round(v * 127)) + 128), 0.5),
    (24, 1, lambda v: struct.pack("<i", int(v * 8388607))[:3], 0.5),
    (32, 1, lambda v: struct.pack("<i", int(v * 2147483647)), 0.5),
    (32, 3, lambda v: struct.pack("<f", v), 0.5),
    (64, 3, lambda v: struct.pack("<d", v), 0.5),
])
def test_other_sample_formats(tmp_path, bits, tag, pack, amp):
    data = b"".join(pack(v) for v in _tone(4000, amp))
    peaks = ap.wav_peaks(_raw_wav(tmp_path / "x.wav", tag, bits, data))
    assert peaks and abs(max(peaks) - amp) < 0.02, (bits, tag, max(peaks or [0]))


def test_wave_format_extensible(tmp_path):
    data = b"".join(struct.pack("<h", int(v * 32767)) for v in _tone(4000, 0.4))
    peaks = ap.wav_peaks(_raw_wav(tmp_path / "e.wav", 1, 16, data, extensible=True))
    assert peaks and abs(max(peaks) - 0.4) < 0.02


@pytest.mark.parametrize("bad_len", [0, 0xFFFFFFFF])
def test_an_unfinished_length_field_falls_back_to_the_file_size(tmp_path, bad_len):
    """錄音程式沒收尾時資料長度是 0 或 0xFFFFFFFF —— 照檔案大小算，不可以整份當成沒有聲音。"""
    data = b"".join(struct.pack("<h", int(v * 32767)) for v in _tone(4000, 0.7))
    peaks = ap.wav_peaks(_raw_wav(tmp_path / "u.wav", 1, 16, data, data_len=bad_len))
    assert peaks and max(peaks) > 0.65


@pytest.mark.parametrize("content", [
    b"\x00\x00\x00\x20ftypM4A " + b"x" * 500,           # m4a
    b"ID3\x03\x00" + b"\x00" * 500,                     # mp3
    b"RIFF\x10\x00\x00\x00AVI LIST" + b"\x00" * 100,    # RIFF 但不是 WAVE
    b"",
])
def test_anything_that_is_not_wav_says_none(tmp_path, content):
    p = tmp_path / "n.bin"
    p.write_bytes(content)
    assert ap.wav_peaks(p) is None


def test_reading_is_done_in_slices_not_the_whole_file(tmp_path, monkeypatch):
    """**記憶體跟檔案大小無關** —— 一小時的 WAV 有 350 MB，整份讀進來的話伺服器也會被拖垮。"""
    p = _wav16(tmp_path / "big.wav", _tone(240_000, 0.3))
    total = p.stat().st_size
    biggest = []
    real_open = Path.open

    def spy_open(self, *a, **kw):
        f = real_open(self, *a, **kw)
        if self == p:
            orig = f.read

            def read(n=-1):
                biggest.append(n)
                return orig(n)
            f.read = read
        return f

    monkeypatch.setattr(Path, "open", spy_open)
    assert ap.wav_peaks(p)
    assert -1 not in biggest and max(biggest) < total / 100, (max(biggest), total)


# ------------------------------------------------------------- 端點

def _upload_wav(client, tmp_path) -> str:
    p = _wav16(tmp_path / "m.wav", _tone(8000, 0.9) + _tone(8000, 0.05))
    r = client.post("/tools/meeting-transcribe/upload",
                    files={"file": ("會議.wav", p.read_bytes(), "audio/wav")})
    assert r.status_code == 200, r.text
    return r.json()["upload_id"]


def test_the_endpoint_returns_wav_peaks_and_caches_them(client, unconfigured, tmp_path):
    from app.config import settings
    with FakeJtlw() as fake:
        _configure(fake)
        uid = _upload_wav(client, tmp_path)
    r = client.get(f"/tools/meeting-transcribe/peaks/{uid}")
    assert r.status_code == 200, r.text
    peaks = r.json()["peaks"]
    loud, quiet = _halves(peaks)
    assert loud > 0.8 and quiet < 0.1, (loud, quiet)
    cache = settings.temp_dir / f"mt_{uid}_peaks.json"
    assert cache.is_file(), "算過的波形沒有存起來，每次開頁都要重讀整份錄音"
    # 檔名帶著 upload_id —— 保留期內的暫存清理靠它認人
    assert uid in cache.name


def test_the_endpoint_says_null_for_other_formats(client, unconfigured):
    from tests.test_meeting_transcribe import _upload
    with FakeJtlw() as fake:
        _configure(fake)
        uid = _upload(client)["upload_id"]           # m4a
    r = client.get(f"/tools/meeting-transcribe/peaks/{uid}")
    assert r.status_code == 200 and r.json()["peaks"] is None


def test_the_endpoint_validates_the_id(client):
    assert client.get("/tools/meeting-transcribe/peaks/not-a-uuid").status_code in (400, 404)
