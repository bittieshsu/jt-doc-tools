"""Tests for the pdf-wordcount tool."""
from __future__ import annotations

import io

import fitz
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.tools.pdf_wordcount.router import (
    _classify_chars,
    _count_text,
    _reading_minutes,
    _top_freq,
)


def _make_pdf_bytes(pages: list[str]) -> bytes:
    doc = fitz.open()
    for txt in pages:
        page = doc.new_page(width=595, height=842)  # A4
        page.insert_text((50, 80), txt, fontsize=12, fontname="helv")
    buf = io.BytesIO()
    doc.save(buf)
    doc.close()
    return buf.getvalue()


# ---------- pure functions ----------

def test_classify_chars_mixed():
    r = _classify_chars("Abc 中文 123 ,。!")
    assert r["cjk"] == 2
    assert r["en_letter"] == 3
    assert r["digit"] == 3
    assert r["punct"] == 3
    assert r["whitespace"] == 3
    assert r["other"] == 0


def test_count_text_word_total_combines_cjk_and_en():
    r = _count_text("hello world 中文測試")
    assert r["cjk_chars"] == 4
    assert r["en_words"] == 2
    assert r["word_total"] == 6


def test_count_text_sentences_split_cjk_and_ascii():
    r = _count_text("一句。兩句!三句?Four. Five.")
    assert r["sentences"] >= 5


def test_reading_minutes_basic():
    # 600 cjk @ 300/min = 2 min ; 400 en @ 200/min = 2 min ; total 4
    assert _reading_minutes(600, 400) == 4.0
    # rounding to one decimal
    assert _reading_minutes(150, 100) == 1.0


def test_top_freq_filters_stopwords_and_short_words():
    r = _top_freq("the the a an quick quick brown brown fox 中中中文文")
    en = {d["term"]: d["count"] for d in r["en_words"]}
    # "the", "a", "an" filtered as stopwords; "quick" "brown" "fox" remain
    assert "the" not in en
    assert "a" not in en
    assert en.get("quick") == 2
    assert en.get("brown") == 2
    cjk = {d["term"]: d["count"] for d in r["cjk_chars"]}
    assert cjk["中"] == 3
    assert cjk["文"] == 2
    bigrams = {d["term"]: d["count"] for d in r["cjk_bigrams"]}
    assert bigrams["中中"] == 2  # 中-中-中 yields two 中中 bigrams
    assert bigrams["中文"] == 1


def test_top_freq_only_consecutive_cjk_bigrams():
    """Bigrams only form between adjacent CJK chars — not across spaces or other chars."""
    r = _top_freq("中文 中文")  # space breaks adjacency
    bigrams = {d["term"]: d["count"] for d in r["cjk_bigrams"]}
    # only "中文" appears twice (both halves), space prevents "文中"
    assert bigrams.get("中文") == 2
    assert "文中" not in bigrams


# ---------- HTTP endpoints ----------

@pytest.fixture
def client():
    return TestClient(app)


def test_index_renders(client):
    r = client.get("/tools/pdf-wordcount/")
    assert r.status_code == 200
    assert "字數統計" in r.text


def test_analyze_returns_summary_and_pages(client):
    pdf = _make_pdf_bytes(["Hello world this is page one.", "Second page has more text."])
    r = client.post(
        "/tools/pdf-wordcount/analyze",
        files={"file": ("doc.pdf", pdf, "application/pdf")},
    )
    assert r.status_code == 200
    j = r.json()
    assert j["page_count"] == 2
    assert j["filename"] == "doc.pdf"
    assert len(j["pages"]) == 2
    assert j["summary"]["word_total"] > 0
    assert j["summary"]["en_words"] >= 10
    assert "char_breakdown" in j
    assert "freq" in j and "cjk_chars" in j["freq"]


def test_analyze_accepts_plain_text(client):
    # v1.7.x：pdf-wordcount 已擴充支援 TXT / MD / CSV 等純文字檔
    r = client.post(
        "/tools/pdf-wordcount/analyze",
        files={"file": ("note.txt", b"hello world", "text/plain")},
    )
    assert r.status_code == 200


def test_analyze_rejects_unsupported_type(client):
    # 非支援副檔名（圖片等）仍應拒絕
    r = client.post(
        "/tools/pdf-wordcount/analyze",
        files={"file": ("photo.png", b"\x89PNG\r\n", "image/png")},
    )
    assert r.status_code == 400


def test_analyze_rejects_empty_pdf_file(client):
    r = client.post(
        "/tools/pdf-wordcount/analyze",
        files={"file": ("empty.pdf", b"", "application/pdf")},
    )
    assert r.status_code == 400


def test_analyze_blank_pdf_has_no_text(client):
    pdf = _make_pdf_bytes([""])  # one empty page
    r = client.post(
        "/tools/pdf-wordcount/analyze",
        files={"file": ("blank.pdf", pdf, "application/pdf")},
    )
    assert r.status_code == 200
    j = r.json()
    assert j["summary"]["has_text"] is False
    assert j["page_count"] == 1


def test_api_endpoint_alias_works(client):
    pdf = _make_pdf_bytes(["api endpoint text"])
    r = client.post(
        "/tools/pdf-wordcount/api/pdf-wordcount",
        files={"file": ("a.pdf", pdf, "application/pdf")},
    )
    assert r.status_code == 200
    assert r.json()["summary"]["en_words"] >= 3


def test_export_csv_returns_per_page_rows(client):
    pdf = _make_pdf_bytes(["page one text", "page two text", "page three"])
    r = client.post(
        "/tools/pdf-wordcount/export-csv",
        files={"file": ("doc.pdf", pdf, "application/pdf")},
    )
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    text = r.content.decode("utf-8-sig")
    rows = [ln for ln in text.splitlines() if ln.strip()]
    # 1 header + 3 data rows
    assert len(rows) == 4
    assert rows[0].startswith("page,")


def test_export_csv_filename_handles_cjk(client):
    pdf = _make_pdf_bytes(["x"])
    r = client.post(
        "/tools/pdf-wordcount/export-csv",
        files={"file": ("中文檔名.pdf", pdf, "application/pdf")},
    )
    assert r.status_code == 200
    cd = r.headers.get("content-disposition", "")
    # RFC 5987 encoding for non-latin filenames
    assert "filename*=" in cd or "filename=" in cd


def test_office_documents_are_counted_via_pdf():
    """字數統計也收辦公文件（使用者要求）。

    做法是**先轉成 PDF 再統計** —— 頁數與每頁字數才是「真的印出來會長那樣」
    的數字，而且直接沿用既有的 PDF 統計路徑。直接讀 XML 雖然快，但拿不到頁數，
    段落與換行也跟排版後不一樣。
    """
    import importlib

    R = importlib.import_module("app.tools.pdf_wordcount.router")
    for ext in (".docx", ".odt", ".xlsx", ".ods", ".pptx", ".odp", ".doc", ".xls", ".ppt"):
        assert R._is_supported("x" + ext), ext
    assert R._is_supported("a.pdf") and R._is_supported("a.txt")
    assert not R._is_supported("a.exe")


def test_office_word_count_matches_the_pdf_path(monkeypatch):
    """辦公文件的統計必須走 `_analyze_pdf` —— 而且轉不出檔案要回 400，不是 500。"""
    import importlib

    from fastapi import HTTPException

    R = importlib.import_module("app.tools.pdf_wordcount.router")

    seen = {}

    def _fake_pdf(data, filename="x"):
        seen["called"] = True
        return {"filename": filename, "page_count": 1, "summary": {}}

    monkeypatch.setattr(R, "_analyze_pdf", _fake_pdf)
    monkeypatch.setattr(R, "_office_to_pdf_bytes", lambda d, n: b"%PDF-1.4")
    R._analyze_doc(b"whatever", "report.docx")
    assert seen.get("called"), "辦公文件應該走 PDF 統計路徑"

    def _boom(src, dst, timeout=0):
        raise RuntimeError("soffice died")

    import app.core.office_convert as oc
    monkeypatch.setattr(oc, "convert_to_pdf", _boom)
    monkeypatch.undo()          # 還原 _office_to_pdf_bytes，改測真的那支
    monkeypatch.setattr(oc, "convert_to_pdf", _boom)
    with pytest.raises(HTTPException) as e:
        R._office_to_pdf_bytes(b"x", "a.docx")
    assert e.value.status_code == 400, "轉檔失敗要回 400（毀損檔案不可以回 500）"


def test_wordcount_is_registered_as_an_office_tool():
    """收辦公文件就會起 soffice —— 漏列會低估記憶體，派送時開太多份把機器打爆。"""
    from app.core.concurrency_settings import OFFICE_TOOL_IDS

    assert "pdf-wordcount" in OFFICE_TOOL_IDS
