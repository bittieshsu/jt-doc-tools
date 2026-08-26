"""從字形反查還原文字 —— 對付壞掉的 ToUnicode 對照表。

客戶回報（2026-08-26）：PDF 編輯器點文件上原本的中文，文字框裡整排變成
`••••••`。那類 PDF 的 ToUnicode 把每個字碼都對應到同一個符號，**畫面正常、
抽出來的字不對**。

重點是**不該用 OCR 解**：PDF 記著每個字用了哪個字形編號，內嵌字型檔裡就有
字形編號 ↔ Unicode 的對照表，反查回去是精確的。OCR 會認錯字、要幾秒鐘，
只留給「字型沒內嵌 / 沒有 cmap / 真的是掃描件」。
"""
from __future__ import annotations

import re

import fitz
import pytest

from app.core.glyph_text import recover_text_in_bbox

SAMPLE = "測試單位：範例文字"


def _make_broken_cmap_pdf(path, text=SAMPLE, size=20):
    from app.core.font_catalog import best_cjk_path, embeddable_font

    fpath, idx = best_cjk_path("sans", "traditional")
    if not fpath:
        pytest.skip("這台機器沒有 CJK 字型")
    fontfile, fontbuffer = embeddable_font(str(fpath), idx, text)
    doc = fitz.open()
    page = doc.new_page()
    page.insert_font(fontname="F0", fontfile=fontfile, fontbuffer=fontbuffer)
    page.insert_text((72, 120), text, fontname="F0", fontsize=size)
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
    doc.save(str(path))
    doc.close()
    return path


@pytest.fixture(scope="module")
def broken_pdf(tmp_path_factory):
    return _make_broken_cmap_pdf(tmp_path_factory.mktemp("glyph") / "broken.pdf")


def test_recovers_the_exact_original_text(broken_pdf):
    """精確還原 —— 一個字都不能錯（這是它勝過 OCR 的整個理由）。

    注意用**整行**的框：全形冒號那個字碼落在別的對照表寫法裡，會把 span
    切成兩段，逐 span 只會拿到半句（那半句本身是對的）。
    """
    doc = fitz.open(str(broken_pdf))
    page = doc[0]
    line = next(line for b in page.get_text("dict")["blocks"]
                for line in b.get("lines", []))
    assert "•" in "".join(s["text"] for s in line["spans"]), \
        "重現檔沒做成，擷取結果不是圓點"
    assert recover_text_in_bbox(page, line["bbox"]) == SAMPLE
    doc.close()


def test_healthy_pdf_recovers_the_same_text(tmp_path):
    """對照組：對正常 PDF 反查，結果要跟正常擷取一致（不會反而弄壞）。"""
    from app.core.font_catalog import best_cjk_path, embeddable_font

    fpath, idx = best_cjk_path("sans", "traditional")
    if not fpath:
        pytest.skip("這台機器沒有 CJK 字型")
    fontfile, fontbuffer = embeddable_font(str(fpath), idx, SAMPLE)
    doc = fitz.open()
    page = doc.new_page()
    page.insert_font(fontname="F0", fontfile=fontfile, fontbuffer=fontbuffer)
    page.insert_text((72, 120), SAMPLE, fontname="F0", fontsize=20)
    out = tmp_path / "ok.pdf"
    doc.save(str(out))
    doc.close()

    doc = fitz.open(str(out))
    page = doc[0]
    line = next(line for b in page.get_text("dict")["blocks"]
                for line in b.get("lines", []))
    assert "".join(s["text"] for s in line["spans"]) == SAMPLE   # 本來就是好的
    assert recover_text_in_bbox(page, line["bbox"]) == SAMPLE
    doc.close()


def test_no_embedded_font_returns_empty(tmp_path):
    """字型沒內嵌就查不到 —— 要安靜回空字串讓呼叫端退回 OCR，不可以丟例外。"""
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 120), "hello", fontsize=20)   # 標準 14 字型，不內嵌
    out = tmp_path / "nofont.pdf"
    doc.save(str(out))
    doc.close()

    doc = fitz.open(str(out))
    assert recover_text_in_bbox(doc[0], (60, 100, 200, 130)) == ""
    doc.close()


def test_bad_bbox_is_safe(broken_pdf):
    doc = fitz.open(str(broken_pdf))
    assert recover_text_in_bbox(doc[0], ("x", None, 1, 2)) == ""
    assert recover_text_in_bbox(doc[0], (0, 0, 1, 1)) == ""     # 空白處
    doc.close()


def test_partial_lookup_is_all_or_nothing(broken_pdf, monkeypatch):
    """有字查不到就整段放棄 —— 半段正確半段問號比整段失敗更糟，
    使用者會以為那就是原文（缺字方框那次的教訓）。"""
    import app.core.glyph_text as gt

    doc = fitz.open(str(broken_pdf))
    page = doc[0]
    line = next(line for b in page.get_text("dict")["blocks"]
                for line in b.get("lines", []))
    # 拿掉的必須是**這段文字真的用到**的字形 —— 隨便刪一半刪不到重點
    # （子集字型只留 117 個字形，用到的都排在後面）。
    used = [c[1] for c in page.get_texttrace()[0]["chars"]]
    assert used, "沒讀到字形編號，測試前提不成立"
    victim = used[len(used) // 2]

    real = gt._gid_to_unicode

    def holey(buf):
        table = dict(real(buf))
        table.pop(victim, None)
        return table

    gt._gid_map_cache.clear()
    monkeypatch.setattr(gt, "_gid_to_unicode", holey)
    got = gt.recover_text_in_bbox(page, line["bbox"])
    assert got == "", f"少一個字就要整段放棄，不可以吐 {got!r}"
    doc.close()
    gt._gid_map_cache.clear()


def test_editor_prefers_glyph_recovery_over_ocr(broken_pdf, monkeypatch, tmp_path):
    """接線：編輯器要先反查字形，**不要**去叫 OCR。"""
    import importlib

    monkeypatch.setenv("JTDT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JTDT_CSRF_DISABLE", "1")

    mod = importlib.import_module("app.tools.pdf_editor.router")
    called = []
    monkeypatch.setattr(mod, "_ocr_bbox",
                        lambda *a, **k: called.append("ocr") or "OCR 猜的結果")

    from fastapi.testclient import TestClient
    import app.main as app_main
    client = TestClient(app_main.app)

    uid = client.post("/tools/pdf-editor/load",
                      files={"file": ("t.pdf", broken_pdf.read_bytes(),
                                      "application/pdf")}).json()["upload_id"]
    data = client.post("/tools/pdf-editor/detect-objects",
                       json={"upload_id": uid, "page": 0, "x": 110, "y": 112}).json()

    assert data.get("kind") == "text", f"沒點到文字：{data}"
    got = data.get("text") or ""
    # 點到的是那一行的其中一段 → 應該是原文的一部分，而且不含圓點
    assert got and got in SAMPLE, f"沒還原出原文：{got!r}"
    assert "\u2022" not in got
    assert data.get("recovered_from_font") is True
    assert called == [], "字形反查得到答案時不應該再叫 OCR"
    # 這個旗標會讓前端整個放棄、只顯示「請自己重打」—— 已經還原出文字時
    # 一定要清掉（真實瀏覽器測試抓到過）
    assert data.get("extracted_text_unreliable") is False
    assert data.get("is_filler") is False


def test_frontend_tells_the_user_it_is_exact():
    """畫面上要說清楚是「從字型還原」，不是 OCR —— 否則使用者會逐字校對。"""
    from pathlib import Path
    html = Path("app/tools/pdf_editor/templates/pdf_editor.html").read_text(encoding="utf-8")
    assert "data.recovered_from_font" in html
    assert "已從字型還原出原文" in html


def test_does_not_bleed_into_the_neighbouring_glyph(tmp_path):
    """反查不可以抓到隔壁的字。

    一個字的原點就是它的左緣，而**下一個字的原點正好落在這個框的右緣** ——
    水平範圍用閉區間就會多吃一個字。實測拿真實表單掃過：`□` 變成 `□主`、
    `□刪除` 尾巴多一個字元，等於把本來正確的文字改壞（比原本的 bug 更糟）。
    """
    from app.core.font_catalog import best_cjk_path, embeddable_font

    fpath, idx = best_cjk_path("sans", "traditional")
    if not fpath:
        pytest.skip("這台機器沒有 CJK 字型")
    text = "甲乙丙丁"
    fontfile, fontbuffer = embeddable_font(str(fpath), idx, text)
    doc = fitz.open()
    page = doc.new_page()
    page.insert_font(fontname="F0", fontfile=fontfile, fontbuffer=fontbuffer)
    page.insert_text((72, 120), text, fontname="F0", fontsize=20)
    out = tmp_path / "neighbour.pdf"
    doc.save(str(out))
    doc.close()

    doc = fitz.open(str(out))
    page = doc[0]
    # 只框住前兩個字（每字 20pt 寬）
    got = recover_text_in_bbox(page, (72, 104, 72 + 40, 126))
    assert got == "甲乙", f"多吃到隔壁的字：{got!r}"
    doc.close()


def test_control_characters_are_treated_as_lookup_failure(broken_pdf, monkeypatch):
    """反查到控制字元表示那張表本身不對勁 —— 實測在真實表單上踩到 NUL。
    塞一個看不見的字進使用者的文字框比整段放棄糟。"""
    import app.core.glyph_text as gt

    doc = fitz.open(str(broken_pdf))
    page = doc[0]
    line = next(line for b in page.get_text("dict")["blocks"]
                for line in b.get("lines", []))
    used = [c[1] for c in page.get_texttrace()[0]["chars"]]
    victim = used[-1]

    real = gt._gid_to_unicode

    def with_nul(buf):
        table = dict(real(buf))
        table[victim] = 0x00
        return table

    gt._gid_map_cache.clear()
    monkeypatch.setattr(gt, "_gid_to_unicode", with_nul)
    assert gt.recover_text_in_bbox(page, line["bbox"]) == ""
    doc.close()
    gt._gid_map_cache.clear()
