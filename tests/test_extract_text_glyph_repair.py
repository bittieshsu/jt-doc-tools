"""壞掉的文字對應表：擷取文字 / 字數統計 / 逐句翻譯也要能還原。

v1.14.54 先修了 PDF 編輯器（客戶回報點文字變成一排圓點）。但同一份檔案拿去
「擷取文字」照樣吐 `••••••` —— 走的是同一條抽取路徑，同一個盲點。

判準有兩條，而且**第二條比第一條重要**：
  ① 壞掉的檔要能還原出原文
  ② **正常的檔一個位元都不可以變** —— 為了救 1% 的壞檔把 99% 的好檔弄出
     細微差異，那是更糟的結果
"""
from __future__ import annotations

import re

import fitz
import pytest

from app.core.glyph_text import page_text_repaired, repair_span_text

SAMPLE = "測試單位：範例文字"


@pytest.fixture(scope="module")
def broken_pdf(tmp_path_factory):
    """畫面畫得出中文、但抽出來全是圓點的 PDF。"""
    from app.core.font_catalog import best_cjk_path, embeddable_font

    fpath, idx = best_cjk_path("sans", "traditional")
    if not fpath:
        pytest.skip("這台機器沒有 CJK 字型")
    fontfile, fontbuffer = embeddable_font(str(fpath), idx, SAMPLE)
    doc = fitz.open()
    page = doc.new_page()
    page.insert_font(fontname="F0", fontfile=fontfile, fontbuffer=fontbuffer)
    page.insert_text((72, 120), SAMPLE, fontname="F0", fontsize=20)
    raw = doc.tobytes()
    doc.close()

    doc = fitz.open(stream=raw, filetype="pdf")
    for xref in range(1, doc.xref_length()):
        try:
            obj = doc.xref_object(xref, compressed=False) or ""
        except Exception:
            continue
        m = re.search(r"/ToUnicode (\d+) 0 R", obj)
        if not m:
            continue
        tu = int(m.group(1))
        cmap = doc.xref_stream(tu).decode("latin-1", "replace")
        cmap = re.sub(r"(<[0-9A-Fa-f]{4}>)\s*<[0-9A-Fa-f]{4,}>", r"\1 <2022>", cmap)
        doc.update_stream(tu, cmap.encode("latin-1", "replace"))
    out = tmp_path_factory.mktemp("repair") / "broken.pdf"
    doc.save(str(out))
    doc.close()
    return out


@pytest.fixture(scope="module")
def healthy_pdf(tmp_path_factory):
    doc = fitz.open()
    page = doc.new_page()
    for i, line in enumerate(["Hello world", "Second line here", "12345"]):
        page.insert_text((60, 100 + i * 24), line, fontsize=12)
    out = tmp_path_factory.mktemp("repair") / "ok.pdf"
    doc.save(str(out))
    doc.close()
    return out


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("JTDT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JTDT_CSRF_DISABLE", "1")
    from fastapi.testclient import TestClient
    import app.main as app_main
    return TestClient(app_main.app)


def test_healthy_page_takes_the_original_path(healthy_pdf):
    """回 None 才代表「照原樣走」—— 這條是整個修法的安全閥。"""
    with fitz.open(str(healthy_pdf)) as doc:
        assert page_text_repaired(doc[0], doc=doc) is None


def test_broken_page_is_repaired(broken_pdf):
    with fitz.open(str(broken_pdf)) as doc:
        assert page_text_repaired(doc[0], doc=doc) == SAMPLE


def test_span_repair_reports_three_distinct_outcomes(broken_pdf, healthy_pdf):
    """`None`（本來就好）/ 還原後的字 / `""`（壞了但救不回）要分得清楚。"""
    with fitz.open(str(healthy_pdf)) as doc:
        span = next(s for b in doc[0].get_text("dict")["blocks"]
                    for line in b.get("lines", []) for s in line["spans"])
        assert repair_span_text(doc[0], span, doc=doc) is None

    with fitz.open(str(broken_pdf)) as doc:
        span = next(s for b in doc[0].get_text("dict")["blocks"]
                    for line in b.get("lines", []) for s in line["spans"])
        got = repair_span_text(doc[0], span, doc=doc)
        assert got and "•" not in got

    # 沒有 bbox → **判不出來**（寬度判斷需要 bbox），照原樣用才安全。
    # 空字串代表「確定壞掉但救不回」，兩者語意不同不可混。
    assert repair_span_text(None, {"text": "••••••", "bbox": ()}) is None


def test_extract_text_api_returns_the_real_text(client, broken_pdf):
    r = client.post("/tools/pdf-extract-text/api/pdf-extract-text",
                    files={"file": ("t.pdf", broken_pdf.read_bytes(), "application/pdf")})
    assert r.status_code == 200, r.text
    text = " ".join(p.get("text", "") for p in r.json().get("pages", []))
    assert SAMPLE in text, f"沒還原出原文：{text!r}"
    assert "•" not in text, "還是把圓點吐給使用者了"


def test_extract_text_api_unchanged_for_healthy_pdf(client, healthy_pdf):
    r = client.post("/tools/pdf-extract-text/api/pdf-extract-text",
                    files={"file": ("t.pdf", healthy_pdf.read_bytes(), "application/pdf")})
    assert r.status_code == 200
    text = " ".join(p.get("text", "") for p in r.json().get("pages", []))
    for expected in ("Hello world", "Second line here", "12345"):
        assert expected in text


def test_wordcount_does_not_count_a_broken_page_as_empty(client, broken_pdf):
    """畫面上明明有字，字數卻是 0 —— 使用者會以為工具壞了。"""
    r = client.post("/tools/pdf-wordcount/api/pdf-wordcount",
                    files={"file": ("t.pdf", broken_pdf.read_bytes(), "application/pdf")})
    assert r.status_code == 200, r.text
    summary = r.json().get("summary") or {}
    assert summary.get("cjk_chars", 0) > 0, f"壞掉的頁面被算成 0 個中文字：{summary}"
    assert summary.get("has_text") is True
    # 還原出來的要是真的字，不是被當成標點的圓點
    breakdown = r.json().get("char_breakdown") or {}
    assert breakdown.get("cjk", 0) >= 8 and breakdown.get("punct", 0) <= 2


def test_real_samples_are_untouched():
    """真實樣本一頁都不可以被判成「壞掉」。

    這條是安全閥：判斷式太鬆的話，正常文件會突然改走另一條路徑，抽出來的
    文字產生細微差異（斷行、空白），而且沒有人會發現。
    """
    import glob
    files = sorted(glob.glob("temp_pdfs/*.pdf"))[:20]
    if not files:
        pytest.skip("這台機器沒有樣本檔")
    changed = []
    for f in files:
        try:
            doc = fitz.open(f)
        except Exception:
            continue
        for pno in range(min(3, doc.page_count)):
            if page_text_repaired(doc[pno], doc=doc) is not None:
                changed.append(f"{f}:{pno + 1}")
        doc.close()
    assert not changed, f"這些正常頁面被判成壞掉了：{changed[:5]}"
