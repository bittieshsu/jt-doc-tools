"""工作區收錄音檔 ＋ 轉逐字稿「從工作區載入」（使用者 2026-09-23 交代）。

原話：「我的工作區可以上傳錄音檔嗎？要給會議錄音轉逐字稿用的，所以會議錄音轉逐字稿
工具也要可以有從工作區載入的按鈕」。

動手前記下來的四件事，每一件這裡都有對應的檢查：

1. **格式看內容不看副檔名**；MP3（沒有 ID3）與 ADTS AAC 只有 frame sync 這種弱訊號，
   要連續好幾個 frame 接得上才收。收的清單跟 `meeting_transcribe.ACCEPT_EXTS` **同一份**。
2. **單檔上限**：錄音檔另有上限（一般型別的 50 MB 會把三小時的會議擋掉），
   被擋下時訊息要講「是上限、不是格式」。
3. **認格式只讀檔頭**，錄音檔不整份讀進記憶體。
4. **錄音是敏感資料**：照工作區的保留期清、管理員看得到每個人存了多少錄音、清得掉。

另外：錄音檔沒有預覽圖，畫面上要顯示圖示（不是破圖、也不是「檔案不存在」）；
轉逐字稿從工作區取檔**不再上傳一次**，歸屬由伺服器判斷。

測試素材全部在這裡用程式組出來（不依賴 ffmpeg —— CI 上沒有）。
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import struct
import subprocess
import time
import wave
from pathlib import Path

import pytest

from app.core import audio_formats as af
from app.core import workspace as ws
# 假的 JTLW 與它的設定工具照用轉逐字稿那支測試的（不另寫一份）
from tests.test_meeting_transcribe import FakeJtlw, _configure, _run, unconfigured  # noqa: F401


# --------------------------------------------------------------------- 素材

def _wav(seconds: float = 0.25, rate: int = 8000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        n = int(seconds * rate)
        w.writeframes(b"".join(struct.pack("<h", (i * 37) % 2000 - 1000) for i in range(n)))
    return buf.getvalue()


#: MPEG-1 Layer III、128 kbps、44.1 kHz、沒有 padding → 每個 frame 417 bytes
_MP3_HDR = b"\xff\xfb\x90\x64"
_MP3_LEN = 144 * 128000 // 44100


def _mp3_frames(n: int = 6) -> bytes:
    return (_MP3_HDR + b"\x00" * (_MP3_LEN - 4)) * n


def _adts(n: int = 6, length: int = 200) -> bytes:
    """ADTS（AAC LC、44.1 kHz、雙聲道、沒有 CRC），每個 frame `length` bytes。"""
    hdr = bytes([0xFF, 0xF1, 0x50, 0x80 | ((length >> 11) & 3), (length >> 3) & 0xFF,
                 ((length & 7) << 5) | 0x1F, 0xFC])
    return (hdr + b"\x00" * (length - 7)) * n


def _id3(body: bytes, tag_size: int = 300) -> bytes:
    ss = bytes([(tag_size >> 21) & 0x7F, (tag_size >> 14) & 0x7F,
                (tag_size >> 7) & 0x7F, tag_size & 0x7F])
    return b"ID3\x04\x00\x00" + ss + b"\x00" * tag_size + body


def _flac() -> bytes:
    return b"fLaC" + b"\x80\x00\x00\x22" + b"\x10" * 34 + b"\xff\xf8" + b"\x00" * 200


def _ogg(first_packet: bytes) -> bytes:
    page = (b"OggS" + b"\x00" + b"\x02" + b"\x00" * 8 + b"\x01\x00\x00\x00"
            + b"\x00" * 4 + b"\x00" * 4 + b"\x01" + bytes([len(first_packet)]))
    return page + first_packet + b"\x00" * 200


def _ftyp(major: bytes, compat: tuple = (b"isom",)) -> bytes:
    size = 16 + 4 * len(compat)
    return (size.to_bytes(4, "big") + b"ftyp" + major + b"\x00\x00\x02\x00"
            + b"".join(compat) + b"\x00\x00\x00\x08free" + b"\x00" * 200)


def _ebml(doctype: bytes) -> bytes:
    body = b"\x42\x86\x81\x01" + b"\x42\x82" + bytes([0x80 | len(doctype)]) + doctype
    return b"\x1a\x45\xdf\xa3" + bytes([0x80 | len(body)]) + body + b"\x00" * 200


PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
PDF_BYTES = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"


# --------------------------------------------------------------------- 同一份清單

def test_the_list_has_one_source():
    """**工作區與轉逐字稿的清單是同一個物件**，不是兩份剛好一樣的。

    兩份一樣的話這一刻會過，但下一次有人只改一邊就漂了 —— 而症狀是
    「工作區存得進去、轉逐字稿卻挑不到」，沒有錯誤訊息。
    """
    import importlib
    mtr = importlib.import_module("app.tools.meeting_transcribe.router")
    assert mtr.ACCEPT_EXTS is af.AUDIO_EXTS, "轉逐字稿自己寫了一份清單"
    assert ws.AUDIO_EXTS == frozenset(af.AUDIO_EXTS)
    allowed = set(ws.ALLOWED.values())
    missing = [e for e in af.AUDIO_EXTS if e not in allowed]
    assert not missing, f"工作區收不下轉逐字稿收的這些格式：{missing}"
    # 前端的「存至工作區 / 從工作區載入」照 `data-ws-exts` 決定出不出現
    from app.main import _tpl_workspace_extensions
    exts = set(_tpl_workspace_extensions().split())
    assert {e.lstrip(".") for e in af.AUDIO_EXTS} <= exts


def test_every_extension_has_its_own_mime():
    """`ALLOWED` 是「MIME → 副檔名」—— 兩個副檔名共用一個 MIME 的話其中一個會被蓋掉。"""
    mimes = [af.MIME_BY_EXT[e] for e in af.AUDIO_EXTS]
    assert len(mimes) == len(set(mimes)), mimes
    assert set(af.MIME_BY_EXT) == set(af.AUDIO_EXTS)


# --------------------------------------------------------------------- 認格式

@pytest.mark.parametrize("label,data,name,want", [
    ("WAV", None, "a.wav", ".wav"),
    ("RF64", None, "a.wav", ".wav"),
    ("MP3（沒有 ID3）", None, "a.mp3", ".mp3"),
    ("MP3（有 ID3）", None, "a.mp3", ".mp3"),
    ("ADTS AAC", None, "a.aac", ".aac"),
    ("FLAC", None, "a.flac", ".flac"),
    ("Ogg Vorbis", None, "a.ogg", ".ogg"),
    ("Ogg Opus", None, "a.opus", ".opus"),
    ("m4a", None, "a.m4a", ".m4a"),
    ("mov", None, "a.mov", ".mov"),
    ("mp4", None, "a.mp4", ".mp4"),
    ("mkv", None, "a.mkv", ".mkv"),
    ("webm", None, "a.webm", ".webm"),
])
def test_each_format_is_recognised_by_content(label, data, name, want):
    data = {
        "WAV": _wav(), "RF64": b"RF64" + _wav()[4:],
        "MP3（沒有 ID3）": _mp3_frames(), "MP3（有 ID3）": _id3(_mp3_frames()),
        "ADTS AAC": _adts(), "FLAC": _flac(),
        "Ogg Vorbis": _ogg(b"\x01vorbis" + b"\x00" * 22),
        "Ogg Opus": _ogg(b"OpusHead\x01\x02" + b"\x00" * 9),
        "m4a": _ftyp(b"M4A ", (b"M4A ", b"isom")), "mov": _ftyp(b"qt  ", (b"qt  ",)),
        "mp4": _ftyp(b"isom", (b"isom", b"mp41")),
        "mkv": _ebml(b"matroska"), "webm": _ebml(b"webm"),
    }[label]
    got = ws.detect_kind(data, name)
    assert got == (af.MIME_BY_EXT[want], want), (label, got)
    # **副檔名換掉也一樣認得出來** —— 判準是內容
    assert ws.detect_kind(data, "renamed.bin") is not None, label


@pytest.mark.parametrize("label,data", [
    # 弱訊號：開頭剛好是 frame sync，後面接不上
    ("只有一個 MP3 frame 標頭", _MP3_HDR + os.urandom(5000)),
    ("三個 MP3 frame 之後接垃圾", _mp3_frames(3) + b"\x12\x34" + os.urandom(5000)),
    ("只有一個 ADTS 標頭", _adts(1)[:7] + os.urandom(5000)),
    ("FF F1 開頭的隨機資料", b"\xff\xf1" + os.urandom(5000)),
    # 參數前後不一致（44.1 kHz 接 48 kHz）
    ("MP3 frame 取樣率不一致", _mp3_frames(2) + (b"\xff\xfb\x94\x64" + b"\x00" * 413) * 3),
    # 同樣是容器開頭、但不是錄音
    ("HEIC 圖片（ftyp）", _ftyp(b"heic", (b"mif1", b"heic"))),
    # 圖片的相容品牌裡也可能列著影音容器常見的品牌 —— **主品牌是圖片就不收**
    ("HEIC（相容品牌含 iso8）", _ftyp(b"heic", (b"mif1", b"heic", b"iso8"))),
    ("相機 RAW（CR3，相容品牌含 isom）", _ftyp(b"crx ", (b"crx ", b"isom"))),
    ("AVIF 圖片（ftyp）", _ftyp(b"avif", (b"avif", b"mif1", b"miaf"))),
    ("AVI（RIFF 但不是 WAVE）", b"RIFF\x00\x10\x00\x00AVI LIST" + b"\x00" * 200),
    ("沒有 fmt 區塊的 WAVE", b"RIFF\x00\x10\x00\x00WAVEdata\x00\x00\x00\x00" + b"\x00" * 64),
    ("EBML 但 DocType 不認得", _ebml(b"other")),
    ("Ogg 但編碼認不得", _ogg(b"\x99unknown" + b"\x00" * 20)),
    # 以文字開頭的檔案
    ("以 fLaC 開頭的文字", b"fLaC is a codec\n" * 20),
    ("以 OggS 開頭的文字", b"OggS and more\n" * 20),
    ("只有 ID3 標籤、後面不是錄音", _id3(b"hello world " * 40)),
])
def test_weak_or_lookalike_signals_are_rejected(label, data):
    """只驗「抓得到」的話，把判準放寬到收一切也會過 —— 這一組驗**不該收的不收**。"""
    assert af.sniff(data, "x.mp3") is None, label


def test_frame_sync_is_only_looked_for_at_the_start():
    """**不往後掃 frame sync**：往後掃的話任何二進位檔裡都找得到一段 `FF Fx`。"""
    assert af.sniff(b"\x00" * 16 + _mp3_frames(), "x.mp3") is None
    assert af.sniff(b"junk" + _adts(), "x.aac") is None


def test_a_tiny_whole_file_with_two_frames_is_still_a_recording():
    """只有兩個 frame（幾十毫秒）的整份檔案也是錄音 —— 但要剛好在檔尾結束。"""
    assert af.sniff(_mp3_frames(2), "x.mp3") == ("audio/mpeg", ".mp3")
    assert af.sniff(_mp3_frames(2) + b"\x00\x01", "x.mp3") is None
    assert af.sniff(_mp3_frames(1), "x.mp3") is None


def test_the_file_name_only_picks_within_the_same_family():
    """m4a / mp4 / mov 是同一種容器、Ogg 與 Opus 也是：內容決定家族，
    檔名只在家族裡挑 —— `會議.m4a` 不會被改名成 `.mp4`。"""
    generic = _ftyp(b"isom", (b"isom", b"mp42"))
    assert ws.detect_kind(generic, "會議.m4a")[1] == ".m4a"
    assert ws.detect_kind(generic, "會議.mov")[1] == ".mov"
    assert ws.detect_kind(generic, "會議.mp3")[1] == ".mp4"     # 家族外的檔名不算數
    opus = _ogg(b"OpusHead\x01\x02" + b"\x00" * 9)
    assert ws.detect_kind(opus, "a.ogg")[1] == ".ogg"
    assert ws.detect_kind(opus, "a.mp3")[1] == ".opus"
    # 檔名永遠不能讓內容不對的東西過關
    assert ws.detect_kind(PNG_BYTES, "recording.mp3")[1] == ".png"
    assert ws.detect_kind("會議記錄\n".encode(), "recording.mp3")[1] == ".txt"
    assert ws.detect_kind(os.urandom(4000).replace(b"\xff", b"\x00"), "recording.wav") is None


class _NoBigReads(io.FileIO):
    """包一個真的檔案：**一次讀超過 1 MB（或不給長度整份讀）就算失敗**。"""

    max_read = 0

    def read(self, size=-1):
        assert size is not None and 0 <= size <= (1 << 20), f"一次讀了 {size}（整份讀進記憶體）"
        type(self).max_read = max(type(self).max_read, size)
        return super().read(size)

    def readall(self):  # pragma: no cover — 被呼叫就是錯
        raise AssertionError("readall()：整份讀進記憶體")


def test_sniffing_reads_only_the_head(tmp_path):
    p = tmp_path / "big.wav"
    wavb = _wav()
    with p.open("wb") as f:
        f.write(wavb)
        f.truncate(300 * 1024 * 1024)        # 300 MB（稀疏檔，不佔磁碟）
    _NoBigReads.max_read = 0
    with _NoBigReads(str(p), "rb") as f:
        assert af.sniff_stream(f, "big.wav") == ("audio/wav", ".wav")
        assert f.tell() == 0, "認完格式要把位置放回開頭"
    assert _NoBigReads.max_read <= af.HEAD_BYTES


# --------------------------------------------------------------------- 存進工作區

class _State:
    def __init__(self, user):
        self.user = user


class _Req:
    def __init__(self, uid=1):
        self.state = _State({"user_id": uid, "username": f"u{uid}", "source": "local"})


@pytest.fixture
def wsenv(tmp_path, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "data_dir", tmp_path, raising=False)
    monkeypatch.setattr(ws, "_CACHE", None, raising=False)
    monkeypatch.setattr("app.core.auth_settings.is_enabled", lambda: True)
    ws.save_settings({"enabled": True, "per_user_quota_mb": 0, "max_file_mb": 1,
                      "max_audio_mb": 2, "retention_hours": -1})
    return tmp_path


def _sized_wav(total: int) -> bytes:
    data = _wav()
    return data + b"\x00" * (total - len(data))


def test_a_large_recording_streams_into_the_workspace_without_a_full_read(wsenv):
    """**錄音檔不整份讀進記憶體**：格式看檔頭、大小用 seek 量、內容串流寫進去。

    而且它比一般型別的單檔上限（這裡 1 MB）大也收得下 —— 錄音檔用自己的上限。
    """
    p = wsenv / "upload.bin"
    p.write_bytes(_sized_wav(int(1.5 * 1024 * 1024)))
    _NoBigReads.max_read = 0
    with _NoBigReads(str(p), "rb") as f:
        meta = ws.save_stream(_Req(), f, "週會錄音.wav", "手動上傳")
    assert (meta["ext"], meta["mime"]) == (".wav", "audio/wav")
    assert meta["name"] == "週會錄音.wav"
    assert meta["size"] == p.stat().st_size
    stored, _ = ws.get_file(_Req(), meta["file_id"])
    assert hashlib.sha256(stored.read_bytes()).hexdigest() == \
        hashlib.sha256(p.read_bytes()).hexdigest()
    assert not list(stored.parent.glob("*.part")), "暫存的半成品沒有收掉"


def test_a_recording_over_its_limit_says_it_is_a_size_limit_not_a_format(wsenv):
    p = wsenv / "too_big.bin"
    p.write_bytes(_sized_wav(int(2.5 * 1024 * 1024)))
    with p.open("rb") as f, pytest.raises(ws.QuotaExceeded) as e:
        ws.save_stream(_Req(), f, "長會議.wav")
    msg = str(e.value)
    assert "錄音檔" in msg and "2 MB" in msg and "不是格式問題" in msg, msg
    # bytes 那條路（作業完成自動存入）用同一個上限、同一句話
    with pytest.raises(ws.QuotaExceeded) as e2:
        ws.save_bytes(_Req(), p.read_bytes(), "長會議.wav")
    assert "不是格式問題" in str(e2.value)
    assert ws.list_files(_Req()) == [], "被擋下的檔案留下了一筆"


def test_other_types_keep_their_own_limit_and_say_so(wsenv):
    big_pdf = PDF_BYTES + b"%" + b"x" * (int(1.5 * 1024 * 1024))
    with pytest.raises(ws.QuotaExceeded) as e:
        ws.save_stream(_Req(), io.BytesIO(big_pdf), "a.pdf")
    assert "1 MB" in str(e.value) and "不是格式問題" in str(e.value)
    # 認不得的大檔：講「格式不支援」，不要說是大小問題（那會讓人去壓縮一個根本收不下的檔）
    junk = os.urandom(int(1.5 * 1024 * 1024)).replace(b"\xff", b"\x00")
    with pytest.raises(ws.UnsupportedType):
        ws.save_stream(_Req(), io.BytesIO(junk), "a.bin")


def test_the_quota_still_applies_to_recordings(wsenv):
    ws.save_settings({"per_user_quota_mb": 2, "max_audio_mb": 0})
    ws.save_stream(_Req(), io.BytesIO(_sized_wav(int(1.5 * 1024 * 1024))), "a.wav")
    with pytest.raises(ws.QuotaExceeded) as e:
        ws.save_stream(_Req(), io.BytesIO(_sized_wav(int(1.0 * 1024 * 1024))), "b.wav")
    assert "容量已滿" in str(e.value)


def test_recordings_are_listed_without_a_preview(wsenv):
    """錄音檔沒有預覽圖 —— **由伺服器端說**（`preview: false`），前端照著畫圖示。"""
    ws.save_stream(_Req(), io.BytesIO(_wav()), "a.wav")
    ws.save_stream(_Req(), io.BytesIO(_ftyp(b"isom")), "b.mp4")
    ws.save_bytes(_Req(), PDF_BYTES, "c.pdf")
    rows = {f["ext"]: f for f in ws.list_files(_Req())}
    assert (rows[".wav"]["preview"], rows[".wav"]["kind"]) == (False, "audio")
    assert (rows[".mp4"]["preview"], rows[".mp4"]["kind"]) == (False, "video")
    assert (rows[".pdf"]["preview"], rows[".pdf"]["kind"]) == (True, "pdf")
    # 算出來的欄位不寫回 meta.json
    stored = json.loads(next((wsenv / "workspace").rglob("meta.json")).read_text("utf-8"))
    assert "preview" not in stored
    # 有人還是去要縮圖的話：要明講「沒有預覽圖」，不可以掉到「當成 PDF」那條路說「檔案不存在」
    with pytest.raises(ws.WorkspaceError) as e:
        ws.get_thumbnail(_Req(), rows[".wav"]["file_id"])
    assert not isinstance(e.value, ws.NotFound)
    assert "預覽" in str(e.value)


def test_recordings_follow_the_workspace_retention_and_admin_sees_them(wsenv):
    """**錄音是敏感資料**：照工作區的保留期清，管理員看得到誰存了多少、清得掉。"""
    a = ws.save_stream(_Req(1), io.BytesIO(_wav()), "a.wav")
    ws.save_bytes(_Req(1), PDF_BYTES, "a.pdf")
    ws.save_stream(_Req(2), io.BytesIO(_wav()), "b.wav")
    st = ws.collect_stats()
    assert st["audio_count"] == 2
    row = {u["key"]: u for u in st["users"]}["u1"]
    assert (row["count"], row["audio_count"]) == (2, 1)
    assert row["audio_bytes"] == a["size"]
    # 清空某個人 → 他的錄音也不見
    assert ws.admin_clear_user("u2") == 1
    assert ws.collect_stats()["audio_count"] == 1
    # 保留期到了照樣清
    d = next(p for p in (wsenv / "workspace" / "u1").iterdir() if p.name == a["file_id"])
    meta = json.loads((d / "meta.json").read_text("utf-8"))
    meta["saved_at"] = time.time() - 10 * 3600
    (d / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    assert ws.sweep_older_than(3600) == 1
    assert [f["ext"] for f in ws.list_files(_Req(1))] == [".pdf"]


def test_accepted_type_groups_cover_exactly_what_the_workspace_takes():
    """設定頁的格式說明由這裡算 —— 每個副檔名剛好出現在一組裡，不多不少。"""
    groups = ws.accepted_type_groups()
    seen = [e for _label, exts in groups for e in exts]
    assert len(seen) == len(set(seen)), "同一個副檔名出現在兩組"
    assert set(seen) == set(ws.ALLOWED.values())
    labels = [label for label, _ in groups]
    assert "錄音 / 錄影" in labels


@pytest.mark.parametrize("locale", ["en", "ja"])
def test_the_format_group_labels_are_translated(locale):
    """分組名稱在樣板裡是 `tr(label)`（變數）—— 掃字面 `tr('…')` 的檢查看不到，
    漏翻的話英日介面會安靜地顯示中文。"""
    cat = json.loads((Path(__file__).resolve().parents[1] / "app" / "i18n"
                      / f"{locale}.json").read_text(encoding="utf-8"))
    missing = [label for label, _ in ws.accepted_type_groups() if label not in cat]
    assert not missing, f"{locale} 沒有這些格式分組的譯文：{missing}"


# --------------------------------------------------------------------- HTTP

def _ws_on(**over):
    cfg = {"enabled": True, "per_user_quota_mb": 500, "max_file_mb": 50,
           "max_audio_mb": 500, "retention_hours": -1}
    cfg.update(over)
    ws.save_settings(cfg)


def test_upload_through_the_page_endpoint(client, auth_off):
    _ws_on()
    r = client.post("/workspace/save", files={"file": ("週會.wav", _wav(), "audio/wav")},
                    data={"source_tool": "手動上傳"})
    assert r.status_code == 200, r.text
    fid = r.json()["file"]["file_id"]
    try:
        files = {f["file_id"]: f for f in client.get("/workspace/api/list").json()["files"]}
        assert files[fid]["preview"] is False and files[fid]["kind"] == "audio"
        # 「從工作區載入」的挑選清單用副檔名過濾
        lst = client.get("/workspace/api/list?accept=wav,m4a").json()["files"]
        assert fid in [f["file_id"] for f in lst]
        # 縮圖：回佔位圖（不破圖），而且不讓瀏覽器快取
        t = client.get(f"/workspace/thumb/{fid}")
        assert t.status_code == 200 and t.headers["content-type"] == "image/png"
        assert "no-store" in t.headers.get("cache-control", "")
        # 原檔照樣拿得到（下載鈕）
        f = client.get(f"/workspace/file/{fid}?dl=1")
        assert f.status_code == 200 and f.content == _wav()
    finally:
        client.post("/workspace/delete", data={"file_id": fid})


def test_an_oversized_recording_is_a_413_that_says_it_is_a_limit(client, auth_off):
    _ws_on(max_audio_mb=1)
    try:
        r = client.post("/workspace/save",
                        files={"file": ("長.wav", _sized_wav(int(1.5 * 1024 * 1024)),
                                        "audio/wav")})
        assert r.status_code == 413, r.text
        assert "不是格式問題" in r.json()["detail"]
    finally:
        _ws_on()


def test_the_admin_page_lists_the_real_formats(client, auth_off):
    _ws_on()
    r = client.get("/admin/workspace")
    assert r.status_code == 200, r.status_code
    assert "只接受 PDF 與 PNG 檔" not in r.text, "說明還是寫死的舊清單"
    for e in sorted(set(ws.ALLOWED.values())):
        assert e in r.text, f"設定頁的格式說明少了 {e}"
    assert 'id="ws-maxaudio"' in r.text, "錄音檔的單檔上限沒有欄位可以調"


def test_the_audio_limit_is_listed_with_the_other_upload_limits():
    from app.core.upload_limits import app_side_limits
    keys = {r["key"] for r in app_side_limits()}
    assert "ws_audio_file" in keys


# --------------------------------------------------------------------- 轉逐字稿那一側

def test_the_load_button_appears_on_the_transcribe_page(client, unconfigured):
    """按鈕是依「工作區收得下的」∩「這個上傳框收的」決定的 —— **真的把前端那支
    函式跑一次**，不是看樣板裡有沒有那顆按鈕（有按鈕但交集是空的，它會被藏起來）。"""
    import re
    _ws_on()
    with FakeJtlw() as fake:
        _configure(fake)
        page = client.get("/tools/meeting-transcribe/").text
    m = re.search(r'class="ws-load-btn" data-accept="([^"]*)"\s+data-ws-exts="([^"]*)"', page)
    assert m, "轉逐字稿頁沒有「從工作區載入」"
    accept, ws_exts = m.group(1), m.group(2)
    js = Path(__file__).resolve().parents[1] / "static" / "js" / "workspace_picker.js"
    script = (
        "global.window = {}; global.document = {querySelector: () => null};"
        "global.tr = (s) => s;"
        f"eval(require('fs').readFileSync({json.dumps(str(js))}, 'utf8'));"
        f"console.log(JSON.stringify(window.workspaceAcceptExts({json.dumps(accept)}, "
        f"{json.dumps(ws_exts)})));")
    try:
        out = subprocess.run(["node", "-e", script], capture_output=True, text=True,
                             timeout=30, check=True).stdout
    except FileNotFoundError:
        pytest.skip("沒有 node")
    got = set(json.loads(out))
    assert got == {e.lstrip(".") for e in af.AUDIO_EXTS}, got


def test_a_workspace_recording_goes_to_jtlw_without_a_second_upload(client, auth_off, unconfigured):
    """端到端：錄音檔先存進工作區 → 轉逐字稿從工作區接過去 → 送到（假的）JTLW 跑完。

    驗的是**對方拿到的就是工作區那一份**：送件時的 sha256 / 大小、以及照簽章網址
    拉回來的內容，都要跟工作區裡的檔案一模一樣。
    """
    _ws_on()
    wav = _wav(0.5)
    r = client.post("/workspace/save", files={"file": ("週會.wav", wav, "audio/wav")})
    assert r.status_code == 200, r.text
    fid = r.json()["file"]["file_id"]
    try:
        with FakeJtlw() as fake:
            _configure(fake, audio_base="http://testserver")
            r = client.post("/tools/meeting-transcribe/from-workspace", json={"file_id": fid})
            assert r.status_code == 200, r.text
            up = r.json()
            assert up["filename"] == "週會.wav"
            assert up["sha256"] == hashlib.sha256(wav).hexdigest()
            assert up["size_bytes"] == len(wav)
            job = _run(client, up["upload_id"])
            assert job["status"] == "done", job.get("error")
            src = fake.seen["body"]["source"]
            assert src["sha256"] == hashlib.sha256(wav).hexdigest()
            assert src["content_type"] == "audio/wav"
            pulled = client.get(src["url"].replace("http://testserver", ""))
            assert pulled.status_code == 200 and pulled.content == wav
        # 工作區那一份刪掉，送件用的那一份不受影響（兩個名字各自獨立）
        client.post("/workspace/delete", data={"file_id": fid})
        from app.web import speech_routes
        assert speech_routes.audio_path(up["upload_id"]).read_bytes() == wav
    finally:
        client.post("/workspace/delete", data={"file_id": fid})


def test_from_workspace_refuses_what_it_should(client, auth_off, unconfigured):
    _ws_on()
    r = client.post("/workspace/save", files={"file": ("a.pdf", PDF_BYTES, "application/pdf")})
    pdf_id = r.json()["file"]["file_id"]
    try:
        # 沒設定語音服務 → 503（部署問題，不是使用者送錯東西）
        r = client.post("/tools/meeting-transcribe/from-workspace", json={"file_id": pdf_id})
        assert r.status_code == 503, r.status_code
        with FakeJtlw() as fake:
            _configure(fake)
            # 不是錄音檔
            r = client.post("/tools/meeting-transcribe/from-workspace", json={"file_id": pdf_id})
            assert r.status_code == 400, r.text
            # 編號格式不對 / 不存在 → 404，不是 500
            for bad in ("../../etc/passwd", "x" * 32, "0" * 32, ""):
                r = client.post("/tools/meeting-transcribe/from-workspace",
                                json={"file_id": bad})
                assert r.status_code == 404, (bad, r.status_code)
            # 工作區被停用
            _ws_on(enabled=False)
            r = client.post("/tools/meeting-transcribe/from-workspace", json={"file_id": pdf_id})
            assert r.status_code == 404
    finally:
        _ws_on()
        client.post("/workspace/delete", data={"file_id": pdf_id})


def test_another_user_cannot_take_my_recording(admin_session, unconfigured):
    """**歸屬由伺服器判斷**：B 拿 A 的 file_id 一樣是「找不到」（不說「不是你的」）。"""
    from tests.test_authz_boundaries import _user_client
    _ws_on()
    _, ca = _user_client("alice_rec")
    _, cb = _user_client("bob_rec")
    r = ca.post("/workspace/save", files={"file": ("a.wav", _wav(), "audio/wav")})
    assert r.status_code == 200, r.text
    fid = r.json()["file"]["file_id"]
    with FakeJtlw() as fake:
        _configure(fake)
        rb = cb.post("/tools/meeting-transcribe/from-workspace", json={"file_id": fid})
        assert rb.status_code == 404, f"B 用 A 的錄音送件了 → {rb.status_code}"
        ra = ca.post("/tools/meeting-transcribe/from-workspace", json={"file_id": fid})
        assert ra.status_code == 200, ra.text
        # 接過來的檔案記在 A 名下：B 也不能用那個 upload_id 送件
        uid = ra.json()["upload_id"]
        rs = cb.post("/tools/meeting-transcribe/start", json={"upload_id": uid})
        assert rs.status_code in (403, 404), rs.status_code
