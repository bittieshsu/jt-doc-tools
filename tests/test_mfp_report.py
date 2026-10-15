"""事務機診斷報告的多種格式（txt / md / json / html / pdf）。

* json 就是原始報告，一個位元都不改。
* 給人看的四種內容一致（摘要、辨識依據、逐步診斷都在），固定字串跟著介面語言、標點也跟著。
* 報告裡的值來自設備網頁：HTML 不可以帶出任何標籤或指令碼、Markdown 不可以變成連結或拆掉表格。
* PDF：中文嵌字型子集、複製出來的文字是正常字（數字不可以變私用區字元）、有頁碼。
"""
from __future__ import annotations

import json
import re

import pytest

from app.core import i18n, mfp_report as R
from app.core.mfp_client import MESSAGES as M

EVIL = '<script>alert(1)</script>"><img src=x onerror=alert(2)>[點我](javascript:alert(3))|破表|'


def _report(title="Apeos C5571 - Internet Services") -> dict:
    """形狀照 `mfp_client.probe()` 的產出（遮蔽標記已經在裡面）。"""
    return {
        "tool": "mfp-fetch", "kind": "connection-test", "app_version": "1.16.91",
        "started_at": "2026-10-15T01:02:03+00:00",
        "target": {"host": "192.0.2.50", "tries": [{"scheme": "https", "port": 443}, {"scheme": "http", "port": 80}]},
        "steps": [{"step": "dns", "ok": True, "code": "ADDR_OK", "message": "位址通過檢查（內部網路）", "ms": 0,
                   "detail": {"host": "192.0.2.50", "addresses": [
                       {"ip": "192.0.2.50", "code": "ADDR_OK", "message": "位址通過檢查（內部網路）"}]}}],
        "attempts": [
            {"scheme": "https", "port": 443, "reachable": True, "tls": {"verified": False}, "steps": [
                {"step": "tcp", "ok": True, "code": "TCP_OK", "message": "TCP 連線成功", "ms": 3,
                 "detail": {"ip": "192.0.2.50", "port": 443}},
                {"step": "tls", "ok": False, "code": "TLS_VERIFY_FAILED", "ms": 40,
                 "message": "TLS 憑證沒有通過驗證（下面列出原因與憑證內容）；連線測試改用未驗證的連線讀首頁，不會傳送任何帳密",
                 "detail": {"verified": False, "version": "TLSv1.3",
                            "reason": {"verify_code": 18, "verify_message": "self-signed certificate"},
                            "certificate": {"subject": "CN=Apeos-C5571-Test", "self_signed": True,
                                            "public_key": "［已遮蔽，長度 6］"}}},
                {"step": "http", "ok": True, "code": "HTTP_OK", "message": "網頁回應正常", "ms": 12,
                 "detail": {"path": "/home/index.html?session=［已遮蔽，長度 6］", "status": 200,
                            "http_version": "HTTP/1.1", "title": title,
                            "headers": [["Server", "Apache"], ["Set-Cookie", "JSID=［已遮蔽，長度 23］; Path=/"]],
                            "text": "FUJIFILM Business Innovation\nFirmware Version: 72.18.5 ```x```",
                            "forms": [{"method": "post", "action": "/login.cgi?token=［已遮蔽，長度 5］",
                                       "inputs": [{"name": "userId", "type": "text"},
                                                  {"name": "passwd", "type": "password"}, {"type": "hidden"}]}],
                            "frames": ["/frame1.html", "http://［已遮蔽］@evil.example/steal"]}},
            ]},
            {"scheme": "http", "port": 80, "reachable": False, "steps": [
                {"step": "tcp", "ok": False, "code": "TCP_REFUSED", "message": M["TCP_REFUSED"], "ms": 1,
                 "detail": {"ip": "192.0.2.50", "port": 80, "error": "Connection refused"}}]},
        ],
        "escl": {"scheme": "https", "port": 443, "version": "2.63", "model": "Apeos C5571",
                 "sources": {"platen": {"colors": ["color", "gray"], "formats": ["pdf", "jpeg"],
                                        "resolutions": [200, 300, 600]}}},
        "detection": {"vendor": "FUJIFILM", "family": "Apeos", "model": "C5571", "firmware": "72.18.5",
                      "status": "first_batch_unverified", "message": "",
                      "evidence": [{"what": "model", "page": "https:443/", "field": "title",
                                    "excerpt": "Apeos C5571 - Internet Services"}]},
        "summary": {"reachable": True, "reachable_on": ["https:443"], "tls": {"verified": False},
                    "status": "first_batch_unverified", "scan": True, "elapsed_ms": 321},
        "finished_at": "2026-10-15T01:02:04+00:00",
    }


def _text(fmt, locale="zh-Hant", rep=None) -> str:
    data, _media, _ext = R.render(rep or _report(), fmt, locale)
    if fmt == "pdf":
        import fitz
        with fitz.open("pdf", data) as d:
            return "".join(p.get_text() for p in d)
    return data.decode("utf-8")


def test_json_is_the_original_report_unchanged():
    rep = _report()
    data, media, ext = R.render(rep, "json", "en")
    assert json.loads(data) == rep and media == "application/json" and ext == "json"


@pytest.mark.parametrize("fmt", ["txt", "md", "html", "pdf"])
def test_every_readable_format_has_the_whole_report(fmt):
    t = _text(fmt)
    for must in ("事務機連線測試診斷報告", "192.0.2.50", "FUJIFILM", "Apeos C5571", "72.18.5",
                 "尚未驗證", "辨識依據", "逐步診斷", "TLS 憑證沒有通過驗證", "self-signed certificate",
                 "CN=Apeos-C5571-Test", "Set-Cookie", "userId", "passwd", "/frame1.html",
                 "設備拒絕連線", "平台玻璃", "HTTP/1.1", "本報告已遮蔽帳號、密碼與工作階段資訊"):
        assert must in t, f"{fmt} 少了「{must}」"


@pytest.mark.parametrize("fmt", ["txt", "md", "html", "pdf"])
def test_fixed_text_follows_the_interface_language(fmt):
    en = _text(fmt, "en")
    assert "MFP connection test diagnostic report" in en and "Step-by-step" in en
    assert "masked, length 6" in en and "已遮蔽" not in en, "遮蔽標記沒有照介面語言"
    # 英文版只剩報告裡原本的值可以有中文（這份報告的值沒有中文）
    assert not re.search(r"[㐀-鿿　-〿＀-￯]", en), \
        re.findall(r".{0,20}[㐀-鿿　-〿＀-￯].{0,20}", en)[:5]
    ja = _text(fmt, "ja")
    assert "複合機接続テスト診断レポート" in ja and "マスク済み" in ja


def test_values_from_the_device_cannot_become_html():
    h = _text("html", rep=_report(EVIL))
    assert "<script" not in h.lower() and "<img" not in h.lower(), "設備的值被當成 HTML 標籤"
    assert "&lt;script&gt;" in h and "onerror" in h, "值要原樣看得到（跳脫後）"
    assert "Content-Security-Policy" in h and "default-src 'none'" in h
    assert "javascript:" not in re.sub(r"<[^>]*>", "", h.split("<body>")[0]), "head 裡不可以有連結"
    assert not re.search(r"<a\b", h), "報告裡不可以有連結"


def test_values_from_the_device_cannot_break_markdown():
    md = _text("md", rep=_report(EVIL))
    # Markdown 裡反斜線跳脫過的 `\<` 顯示成字面的 <，不是標籤；沒跳脫的 < 一個都不可以有
    assert not re.search(r"(?<!\\)<", md), "設備的值可以變成 HTML 標籤"
    assert not re.search(r"(?<!\\)\]\(", md), "設備的值變成連結了"
    assert "\\<script\\>" in md, "值要原樣看得到（跳脫後）"
    row = next(line for line in md.splitlines() if "Internet" not in line and "破表" in line)
    # 表格一列只能有三根未跳脫的直線（開頭、中間、結尾）
    assert len(re.findall(r"(?<!\\)\|", row)) == 3, row
    # 頁面文字裡有 ```，圍欄要比它長，不然後面全變成程式碼
    assert "````text" in md


def test_pdf_embeds_a_font_subset_and_copies_as_normal_text():
    import fitz
    data, media, ext = R.render(_report(), "pdf", "zh-Hant")
    assert data.startswith(b"%PDF") and media == "application/pdf" and ext == "pdf"
    assert len(data) < 2_000_000, f"PDF 太大（{len(data)} 位元組）：中文字型沒有子集化？"
    with fitz.open("pdf", data) as d:
        text = "".join(p.get_text() for p in d)
        fonts = {f[3] for p in d for f in p.get_fonts()}
        assert d.metadata["title"] == "事務機連線測試診斷報告"
        assert f"1 / {d.page_count}" in d[0].get_text()
    assert not any(0xE000 <= ord(c) <= 0xF8FF for c in text), "複製出來有私用區字元（數字被換成別的字形）"
    assert "HTTP/1.1" in text and "200" in text
    assert not any("Droid" in f for f in fonts), f"中文落到 MuPDF 的後備字型（整支 3.5 MB）：{fonts}"


@pytest.mark.parametrize("locale", ["zh-Hant", "en", "ja"])
def test_check_marks_use_dingbats_not_a_fallback_font(locale):
    """✓ ✗ 中日文字型多半沒有：不指定的話 MuPDF 嵌整支後備字型（Windows 實測 Droid 3.5 MB、Linux Symbols2 375 KB）。"""
    import fitz
    data = R.render(_report(), "pdf", locale)[0]
    with fitz.open("pdf", data) as d:
        spans = [(sp["font"], "".join(c["c"] for c in sp["chars"])) for p in d
                 for b in p.get_text("rawdict")["blocks"] for line in b.get("lines", []) for sp in line["spans"]]
    marks = {f for f, t in spans if "✓" in t or "✗" in t}
    assert marks and all("Dingbats" in f for f in marks), marks
    assert not any(("Droid" in f or "Symbols" in f) for f, _t in spans), {f for f, _t in spans}


def test_unknown_format_is_refused():
    with pytest.raises(ValueError):
        R.render(_report(), "docx", "zh-Hant")


def test_a_broken_report_still_renders():
    """報告是存在資料庫裡的舊資料，欄位可能缺、型別可能不對 —— 不可以 500。"""
    for rep in ({}, {"steps": [None, {"step": "tcp", "detail": "x"}], "attempts": [{"steps": [{}]}],
                     "summary": {"tls": {}}, "detection": {"evidence": [None, {}]}, "escl": {"sources": {"x": 1}}}):
        for fmt in ("txt", "md", "html", "pdf"):
            assert R.render(rep, fmt, "en")[0]


@pytest.mark.parametrize("host,want", [("192.0.2.5", "192.0.2.5"), ("fe80::1", "fe80__1"),
                                       ("a b/../x", "a_b_.._x"), ("", "mfp")])
def test_file_name_is_plain_ascii(host, want):
    assert R.filename(host, "20261015-090000", "pdf") == f"mfp-diagnostic-{want}-20261015-090000.pdf"


def test_every_fixed_string_has_english_and_japanese():
    keys = set(R.PHRASES) | set(R.KEY_LABELS.values()) | set(R.STEP_NAMES.values()) | \
        set(R.EVIDENCE_WHAT.values()) | set(R.EVIDENCE_FIELD.values()) | set(R.SOURCE_NAMES.values()) | \
        set(R.COLOR_NAMES.values())
    keys = {k for k in keys if re.search(r"[㐀-鿿]", k)}
    for loc in ("en", "ja"):
        missing = sorted(k for k in keys if k not in i18n.catalog(loc))
        assert missing == [], f"{loc} 少了：{missing}"


# ---- PDF 字型：涵蓋全部字、日文用日文字形（v1.16.92）

def _pan_cjk_ttc():
    from app.core import font_catalog
    best = font_catalog.best_cjk_path("sans", "traditional")
    if not best or best[0].suffix.lower() != ".ttc":
        pytest.skip("這台沒有泛中日韓的 .ttc（Noto CJK）")
    return best[0]


def test_japanese_report_uses_the_japanese_subfont_and_stays_small():
    """Noto CJK 的日文在第 0 套：以前第 0 套子集化失敗、整支 14 MB 嵌進 PDF。"""
    import fitz
    _pan_cjk_ttc()
    data = R.render(_report(), "pdf", "ja")[0]
    assert len(data) < 2_000_000, f"日文 PDF {len(data)} 位元組：字型沒有子集化"
    with fitz.open("pdf", data) as d:
        fonts = {f[3] for p in d for f in p.get_fonts()}
    assert any("JP" in f for f in fonts), f"日文報告沒有用日文字形：{fonts}"
    assert not any("Droid" in f for f in fonts), fonts


def test_a_font_missing_characters_is_skipped(monkeypatch):
    """預設字型缺字（Windows 的 Noto Sans TC 沒有「済」）→ 換一個涵蓋得到的，不要落到 MuPDF 的後備字型。"""
    from pathlib import Path
    from app.core import font_catalog
    ttc = _pan_cjk_ttc()
    latin = next((f for f in font_catalog.list_fonts() if f.get("category") == "latin" and f.get("path")), None)
    if not latin:
        pytest.skip("這台沒有拉丁字型可以當作缺字的字型")
    monkeypatch.setattr(font_catalog, "best_cjk_path", lambda *a, **k: (Path(latin["path"]), 0))
    monkeypatch.setattr(font_catalog, "list_fonts", lambda *a, **k: [
        {"path": latin["path"], "category": "latin", "style": "sans", "idx": 0},
        {"path": str(ttc), "category": "free-cjk", "style": "sans", "idx": 3}])
    assert R._pick_font("マスク済み", "zh-Hant") == (ttc, 3)
    assert R._pick_font("abc", "en") == (Path(latin["path"]), 0), "沒有中日文字就用預設的那一個"


def test_ttc_subfont_zero_is_extracted_not_the_whole_collection():
    import io
    from fontTools.ttLib import TTFont
    from app.core import font_catalog
    ttc = _pan_cjk_ttc()
    _ff, buf = font_catalog.embeddable_font(ttc, 0, text="済み")
    assert buf and len(buf) < 5_000_000, "第 0 套沒有子集化（整份集合嵌進去）"
    TTFont(io.BytesIO(buf))     # 單一字型，不用指定第幾套就能開
