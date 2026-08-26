"""擷取出來全是佔位字元（圓點 / 星號…）但畫面上其實是真的字。

客戶回報（2026-08-26）：在 PDF 編輯器用「選既有物件」點文件上原本的中文，
文字框裡卻整排變成 `••••••`。根因是那份 PDF 的 ToUnicode 對照表壞掉，
**把每個字碼都映到同一個圓點**；畫面用字形畫所以完全正常，抽出來卻是圓點。

`_looks_garbled()` 只認「數學符號 / 方框 / 罕用漢字」那幾類，圓點不在名單內
→ 被當成可靠結果原樣送進文字框。

判準**不能只看字元** —— 真正的點引導符（`目錄………12`）自 v1.6.10 起是刻意
讓使用者選得到的。兩者差在寬度：壞掉的中文擷取每個「點」佔滿一個字寬，
真點引導符每點只有 0.2–0.35 字寬。
"""
from __future__ import annotations

import re

import fitz
import pytest

from app.tools.pdf_editor.router import _looks_garbled, _placeholder_extraction


# --- 單元：判斷式本身 -------------------------------------------------

def test_broken_cjk_extraction_flagged():
    """4 個圓點佔 56pt、字級 14 → 每字 14pt（實測重現檔的數字）。"""
    assert _placeholder_extraction("••••", (0, 0, 56, 14), 14) is True


@pytest.mark.parametrize("ch", ["•", "·", "●", "*", "?", "？", "_", "□", "．"])
def test_all_common_placeholder_chars_flagged(ch):
    """壞掉的 ToUnicode 可能映到任何一個佔位符號，不只圓點。"""
    assert _placeholder_extraction(ch * 5, (0, 0, 5 * 14, 14), 14) is True


def test_real_leader_dots_not_flagged():
    """真的點引導符每點約 3.5pt（0.25 字寬）→ 不可以被誤判。"""
    assert _placeholder_extraction("········", (0, 0, 28, 14), 14) is False


def test_mixed_with_real_text_not_flagged():
    """只要混有真正的字，就不是「整段擷取失敗」。"""
    assert _placeholder_extraction("測試單位••", (0, 0, 84, 14), 14) is False


def test_empty_and_bad_font_size_are_safe():
    assert _placeholder_extraction("", (0, 0, 56, 14), 14) is False
    assert _placeholder_extraction("••••", (0, 0, 56, 14), 0) is False


def test_garbled_check_alone_misses_dots():
    """守住這條 bug 的形狀：舊的判斷式對圓點是無感的。

    這不是在測「舊行為是對的」，而是釘住「為什麼需要新的判斷式」——
    哪天有人把佔位判斷刪掉當成重複邏輯，這條會提醒他不是。
    """
    assert _looks_garbled("••••••") is False


# --- 端到端：真的做一份壞 ToUnicode 的 PDF ---------------------------

@pytest.fixture(scope="module")
def broken_cmap_pdf(tmp_path_factory):
    """畫面畫得出中文、但抽出來全是圓點的 PDF。"""
    from app.core.font_catalog import best_cjk_path, embeddable_font

    path, idx = best_cjk_path("sans", "traditional")
    if not path:
        pytest.skip("這台機器沒有 CJK 字型，做不出重現檔")
    text = "測試單位：範例文字"
    fontfile, fontbuffer = embeddable_font(str(path), idx, text)

    doc = fitz.open()
    page = doc.new_page()
    page.insert_font(fontname="F0", fontfile=fontfile, fontbuffer=fontbuffer)
    page.insert_text((72, 130), text, fontname="F0", fontsize=14)
    raw = doc.tobytes()
    doc.close()

    # 把 ToUnicode 內每個字碼都改指到 U+2022（圓點）
    doc = fitz.open(stream=raw, filetype="pdf")
    patched = 0
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
        patched += 1
    assert patched, "沒找到 ToUnicode，重現檔沒做成"

    out = tmp_path_factory.mktemp("brokencmap") / "broken.pdf"
    doc.save(str(out))
    doc.close()
    return out


def test_repro_pdf_really_has_the_defect(broken_cmap_pdf):
    """先確認重現檔真的是「畫面正常、擷取變圓點」，否則後面都在測空氣。"""
    doc = fitz.open(str(broken_cmap_pdf))
    page = doc[0]

    extracted = page.get_text().strip()
    assert extracted and set(extracted) <= {"•", " ", "\n", ""}, \
        f"擷取結果應該全是圓點，實際是 {extracted!r}"

    pix = page.get_pixmap(dpi=100)
    samples = pix.samples          # property，每次存取都重建整份緩衝
    dark = sum(1 for i in range(0, len(samples), pix.n) if samples[i] < 200)
    assert dark > 200, "頁面上要真的有墨水（字畫得出來），否則不是這個 bug"
    doc.close()


def test_detect_objects_flags_the_span(broken_cmap_pdf):
    """真正要守的：這種 span 不可以被當成可靠文字送到畫面上。"""
    doc = fitz.open(str(broken_cmap_pdf))
    span = next(
        s for b in doc[0].get_text("dict")["blocks"]
        for line in b.get("lines", []) for s in line["spans"]
    )
    doc.close()

    bbox = span["bbox"]
    assert _placeholder_extraction(span["text"], bbox, span["size"]) is True, (
        f"span {span['text']!r} 寬 {bbox[2] - bbox[0]:.1f}pt、"
        f"字級 {span['size']:.1f} 應被判為擷取失敗"
    )


def test_endpoint_does_not_hand_the_dots_to_the_editor(broken_cmap_pdf, monkeypatch, tmp_path):
    """真正要守的接線：判斷式有沒有**接進** detect_objects。

    只測判斷式本身是不夠的 —— 實測過：把 `_placeholder_extraction` 從
    detect_objects 拿掉，純單元測試整包照樣全綠。
    """
    import importlib

    monkeypatch.setenv("JTDT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JTDT_CSRF_DISABLE", "1")

    mod = importlib.import_module("app.tools.pdf_editor.router")
    # OCR 一律當作認不出東西 —— 這條測試要驗的是「圓點不會被當成正常文字」，
    # 不該讓結果取決於這台機器有沒有裝 OCR 引擎。
    monkeypatch.setattr(mod, "_ocr_bbox", lambda *a, **k: "")

    from fastapi.testclient import TestClient
    import app.main as app_main
    client = TestClient(app_main.app)

    raw = broken_cmap_pdf.read_bytes()
    uid = client.post("/tools/pdf-editor/load",
                      files={"file": ("t.pdf", raw, "application/pdf")}).json()["upload_id"]
    data = client.post("/tools/pdf-editor/detect-objects",
                       json={"upload_id": uid, "page": 0, "x": 90, "y": 126}).json()

    assert data.get("kind") == "text", f"沒點到文字：{data}"
    got = (data.get("text") or "").strip()
    assert not (got and all(ch in "\u2022\u00b7*?_" for ch in got)), \
        f"整排圓點被送進編輯器：{got!r}"
    # 兩種可接受的結果：①從字型精確還原出原文 ②還原不了，明確請使用者重打
    if data.get("recovered_from_font"):
        assert got and "\u2022" not in got
        assert data.get("extracted_text_unreliable") is False
    else:
        assert data.get("extracted_text_unreliable") is True
        assert not got


def test_single_char_is_too_weak_a_signal():
    """一個全形問號 / 句號可能是真的內容 —— 寧可漏判不要誤判。"""
    assert _placeholder_extraction("？", (0, 0, 14, 14), 14) is False
    assert _placeholder_extraction("。", (0, 0, 14, 14), 14) is False


def test_ascii_underscore_fill_lines_survive():
    """表單常見的底線填空（ASCII `_` 約半形寬）不可以被當成擷取失敗。"""
    assert _placeholder_extraction("______", (0, 0, 6 * 7, 14), 14) is False
