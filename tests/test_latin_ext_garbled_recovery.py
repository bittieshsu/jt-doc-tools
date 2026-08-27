"""擷取結果被映到拉丁擴充區、而且每個 span 都很短 —— 舊的判準抓不到。

客戶實際的檔案（2026-08-27）：標楷體子集，中文全部被 ToUnicode 映成拉丁擴充
區的字（Ǻ 之類）。**一個 span 都沒有被判為不可靠**，所以整份亂碼被當成可靠
結果直接送進編輯器的文字框。

原因是舊的兩條判準都有門檻：
  * `is_bad_cmap_text` 要「拉丁擴充字 ≥ 5 個且佔比 > 10%」
  * 佔位字元判斷要「整段都是佔位字元」
而那份檔案每個 span 只有 5～10 個字，一個 span 裡通常只有 1～2 個拉丁擴充字，
兩條都湊不到門檻。

**解法不是再調門檻**（調鬆會誤傷正常內容），而是**不要再猜** —— 字型檔裡就有
正確答案，直接對照：擷取結果含有「中文文件不該出現的字」而且字形反查得出
不同的結果，就用反查的。兩個條件缺一不可，只看「反查不一致」的話正常樣本有
23% 的 span 會對不起來（連字、空白、一形多碼），照那樣改會把正確的文字弄壞。

**完全不用 OCR。** 字形資訊沒有遺失，反查是精確的。
"""
from __future__ import annotations

import re

import fitz
import pytest

from app.core.bad_cmap import is_bad_cmap_text
from app.core.glyph_text import looks_like_placeholder, repair_span_text

#: 刻意做成多個短句 —— 長句會湊到舊判準的門檻，就測不到這個 bug 了。
SHORT_RUNS = ["申請單位", "承辦人員", "聯絡電話", "核准日期", "備註事項"]


@pytest.fixture(scope="module")
def latin_ext_pdf(tmp_path_factory):
    """中文被映到拉丁擴充區、每個 span 只有 4 個字的 PDF。"""
    from app.core.font_catalog import best_cjk_path, embeddable_font

    fpath, idx = best_cjk_path("serif", "traditional") or best_cjk_path("sans", "traditional")
    if not fpath:
        pytest.skip("這台機器沒有 CJK 字型")
    joined = "".join(SHORT_RUNS)
    fontfile, fontbuffer = embeddable_font(str(fpath), idx, joined)

    doc = fitz.open()
    page = doc.new_page()
    page.insert_font(fontname="F0", fontfile=fontfile, fontbuffer=fontbuffer)
    for i, run in enumerate(SHORT_RUNS):
        page.insert_text((72, 100 + i * 30), run, fontname="F0", fontsize=14)
    raw = doc.tobytes()
    doc.close()

    # ToUnicode 逐筆改指到拉丁擴充區（U+01F0 起），模擬 shift 型的壞掉
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
        counter = {"n": 0}

        def _shift(match):
            counter["n"] += 1
            return f"{match.group(1)} <{0x01F0 + counter['n'] % 48:04X}>"

        cmap = re.sub(r"(<[0-9A-Fa-f]{4}>)\s*<[0-9A-Fa-f]{4,}>", _shift, cmap)
        doc.update_stream(tu, cmap.encode("latin-1", "replace"))
    out = tmp_path_factory.mktemp("latinext") / "garbled.pdf"
    doc.save(str(out))
    doc.close()
    return out


def _spans(page):
    return [s for b in page.get_text("dict")["blocks"]
            for line in b.get("lines", []) for s in line["spans"]
            if (s.get("text") or "").strip()]


def test_repro_really_reproduces_the_defect(latin_ext_pdf):
    """先確認重現檔真的是那個樣子，否則後面都在測空氣。"""
    with fitz.open(str(latin_ext_pdf)) as doc:
        page = doc[0]
        spans = _spans(page)
        assert spans, "沒抽到 span"
        text = "".join(s["text"] for s in spans)
        assert not any("㐀" <= c <= "鿿" for c in text), "還抽得到中文，重現檔沒做成"
        assert any(0x0100 <= ord(c) <= 0x024F for c in text), "沒有拉丁擴充字"
        # 畫面上要真的有字（這是「畫面正常、抽出來錯」的前提）
        pix = page.get_pixmap(dpi=100)
        samples = pix.samples
        dark = sum(1 for i in range(0, len(samples), pix.n) if samples[i] < 200)
        assert dark > 200, "頁面上沒有墨水，不是這個 bug"


def test_old_heuristics_miss_it(latin_ext_pdf):
    """釘住「為什麼需要第三條判準」—— 舊的兩條對短 span 是無感的。

    這不是在測「舊行為是對的」，而是提醒後人：把第三條刪掉當重複邏輯的話，
    客戶那個檔案會原封不動再壞一次。
    """
    with fitz.open(str(latin_ext_pdf)) as doc:
        page = doc[0]
        missed = [s for s in _spans(page)
                  if not is_bad_cmap_text(s["text"])
                  and not looks_like_placeholder(s["text"], s["bbox"],
                                                 float(s.get("size") or 0))]
        assert missed, "舊判準居然全抓到了 —— 這條測試的前提變了，請重新確認"


def test_every_span_is_recovered(latin_ext_pdf):
    """整頁的中文都要回來，而且不可以還有拉丁擴充的殘留。"""
    with fitz.open(str(latin_ext_pdf)) as doc:
        page = doc[0]
        spans = _spans(page)
        fixed = [repair_span_text(page, s, doc=doc) or s["text"] for s in spans]
        joined = "".join(fixed)
        assert not any(0x0100 <= ord(c) <= 0x024F for c in joined), \
            "還有拉丁擴充的亂碼沒修掉"
        for run in SHORT_RUNS:
            assert run in joined, f"沒還原出 {run!r}"


def test_editor_returns_real_text_without_ocr(latin_ext_pdf, monkeypatch, tmp_path):
    """端到端：點下去要拿到中文，而且**不可以叫 OCR**。

    使用者明確要求這個情境不要用 OCR 解 —— 字形資訊沒有遺失，反查是精確的，
    OCR 會認錯字又慢。
    """
    import importlib

    monkeypatch.setenv("JTDT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JTDT_CSRF_DISABLE", "1")
    mod = importlib.import_module("app.tools.pdf_editor.router")
    called = []
    monkeypatch.setattr(mod, "_ocr_bbox", lambda *a, **k: called.append("ocr") or "")

    from fastapi.testclient import TestClient
    import app.main as app_main
    client = TestClient(app_main.app)

    uid = client.post("/tools/pdf-editor/load",
                      files={"file": ("t.pdf", latin_ext_pdf.read_bytes(),
                                      "application/pdf")}).json()["upload_id"]
    with fitz.open(str(latin_ext_pdf)) as doc:
        span = _spans(doc[0])[0]
        bx = (span["bbox"][0] + span["bbox"][2]) / 2
        by = (span["bbox"][1] + span["bbox"][3]) / 2

    data = client.post("/tools/pdf-editor/detect-objects",
                       json={"upload_id": uid, "page": 0, "x": bx, "y": by}).json()
    assert data.get("kind") == "text", f"沒點到文字：{data}"
    assert data.get("recovered_from_font") is True
    assert data.get("ocr_used") is False
    assert called == [], "不該叫 OCR"
    got = data.get("text") or ""
    assert any("㐀" <= c <= "鿿" for c in got), f"回傳的還是亂碼：{got!r}"
    assert not any(0x0100 <= ord(c) <= 0x024F for c in got)


def test_normal_pdfs_are_not_touched():
    """正常樣本一個 span 都不可以被改 —— 這條比上面全部加起來還重要。

    只看「反查結果不一致」的話，正常樣本有 23% 的 span 會對不起來
    （連字、空白、一形多碼）。把正確的文字改成別的字，比原本的 bug 更糟，
    而且使用者不會發現。
    """
    import glob
    files = sorted(glob.glob("temp_pdfs/*.pdf"))[:15]
    if not files:
        pytest.skip("這台機器沒有樣本檔")
    touched = []
    for f in files:
        try:
            doc = fitz.open(f)
        except Exception:
            continue
        for pno in range(min(2, doc.page_count)):
            page = doc[pno]
            for s in _spans(page):
                if repair_span_text(page, s, doc=doc):
                    touched.append(f"{f.rsplit('/', 1)[-1]}:{pno + 1}")
                    break
        doc.close()
    assert not touched, f"正常樣本被改動了：{touched[:5]}"
