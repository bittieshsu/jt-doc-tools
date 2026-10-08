"""公文撰擬：匯出的「版面加註」與 PNG / SVG 圖片（使用者 2026-10-08：「匯出那邊，版面下方加選項 是否加入
裝訂線 是否加入正本 是否加入發文方式」「下載增加 PNG 圖片, SVG 圖片」）。

對照實際的公文整理出的幾項：左側裝訂線（虛線＋「裝」「訂」「線」）、頁尾「第○頁　共○頁」、
左上角「正本／副本／抄本」與「發文方式：…」、署名下方「本案依分層負責規定授權○○決行」、
受文者的郵遞區號與地址（開窗信封）。

* **都不寫進草稿文字**（「依分層負責規定」寫進草稿會被事實檢查當成沒有依據的法規）。
* 選項只收清單上的；文字限長、去控制字元。
* 頁首框裡的字要用自動樣式 —— 放共用樣式的話 LibreOffice 一律畫成 16pt（實測，有一條測試算圖釘住）。
* 圖片匯出走跟 PDF 同一條路；多頁時一頁一張打包成 zip。
"""
from __future__ import annotations

import io
import json
import re
import zipfile

import pytest

from app.core import official_doc as od
from app.core import official_doc_odt as odx

BASE = "/tools/official-doc"

LETTER = od.assemble_letter(
    {"subject": "檢送115年度資訊安全稽核報告1份", "explanation": ["依　貴局115年3月2日函辦理。"],
     "measures": ["請　貴局惠予備查。"]},
    org="嘉禾市資訊局", receiver="嘉禾市政府", relation="up", signature="局長　王○○")

ALL = {"page_numbers": True, "binding_line": True, "copy_mark": "副本", "send_method": "電子交換",
       "delegate": "業務主管", "receiver_address": "100000 嘉禾市中正路2號"}


def _xml(data: bytes, part: str) -> str:
    return zipfile.ZipFile(io.BytesIO(data)).read(part).decode("utf-8")


# ------------------------------------------------------------------ 輸入

def test_page_extras_validates_the_input():
    assert odx.page_extras(None) == odx.NO_EXTRAS
    ex = odx.page_extras(ALL)
    assert ex.copy_mark == "副本" and ex.send_method == "電子交換" and ex.delegate == "業務主管"
    assert ex.page_numbers and ex.binding_line
    # 整句貼進來也收，只留中間那幾個字
    assert odx.page_extras({"delegate": "本案依分層負責規定授權單位主管決行"}).delegate == "單位主管"
    # 控制字元與換行收掉
    assert odx.page_extras({"receiver_address": "100000\n嘉禾市\x00中正路"}).receiver_address == "100000 嘉禾市 中正路"
    for bad in ({"copy_mark": "影本"}, {"send_method": "飛鴿傳書"}, {"delegate": "長" * 21},
                {"receiver_address": "路" * 81}, {"copy_mark": 1}, "正本"):
        with pytest.raises(ValueError):
            odx.page_extras(bad)


def test_address_lines_split_the_postcode():
    assert odx.address_lines("100000 嘉禾市中正路2號") == ["100000", "嘉禾市中正路2號"]
    assert odx.address_lines("嘉禾市中正路2號") == ["嘉禾市中正路2號"]
    assert odx.address_lines("") == []


# ------------------------------------------------------------------ 內建版面

def test_nothing_is_added_without_extras():
    data = odx.build_odt(LETTER, draft_mark=False)
    styles = _xml(data, "styles.xml")
    assert "<style:header>" not in styles and "<style:footer>" not in styles
    content = _xml(data, "content.xml")
    assert "分層負責" not in content and "OD_RecvAddr" not in content


def test_built_in_layout_gets_every_extra():
    data = odx.build_odt(LETTER, draft_mark=True, extras=odx.page_extras(ALL))
    styles, content = _xml(data, "styles.xml"), _xml(data, "content.xml")
    assert "<draw:line" in styles and all(ch in styles for ch in ("裝", "訂", "線"))
    assert "副　本" in styles and "發文方式：電子交換" in styles
    assert "<text:page-number" in styles and "<text:page-count" in styles and "<style:footer>" in styles
    assert "本案依分層負責規定授權業務主管決行" in content
    # 受文者地址在「受文者」那一段的前面
    assert content.index("嘉禾市中正路2號") < content.index("受文者：嘉禾市政府")
    # 頁首框裡的字用的樣式在 automatic-styles（共用樣式在框裡不生效）
    auto = styles[styles.index("<office:automatic-styles>"):styles.index("</office:automatic-styles>")]
    for name in ("OD_BindChar", "OD_CopyMark", "OD_SendMethod", "OD_t10", "OD_t14"):
        assert f'style:name="{name}"' in auto, name


def test_header_and_footer_stay_inside_the_margins():
    """頁首、頁尾放在上下邊界裡：本文的上下緣不動（邊界 2.0cm ＝ 1.0 ＋ 頁首／頁尾 1.0）。"""
    lay = odx._page_layout(False, odx.page_extras({"page_numbers": True, "binding_line": True}))
    assert 'fo:margin-top="1.0cm"' in lay and 'fo:margin-bottom="1.0cm"' in lay
    assert 'fo:min-height="1.0cm"' in lay
    lay0 = odx._page_layout(False)
    assert 'fo:margin-top="2.0cm"' in lay0 and 'fo:margin-bottom="2.0cm"' in lay0


def _soffice_or_skip():
    from app.core import office_convert
    if not office_convert.find_soffice():
        pytest.skip("這台沒有 Office 引擎")


def test_pdf_has_the_extras_at_the_right_size_and_place():
    """算圖確認：字級對（框裡的字不是 16pt）、裝訂線在左邊界裡、本文沒有被推下去。"""
    _soffice_or_skip()
    import fitz
    with_x, _ = odx.export(LETTER, "pdf", draft_mark=True, extras=odx.page_extras(ALL))
    without, _ = odx.export(LETTER, "pdf", draft_mark=True)
    d = fitz.open(stream=with_x, filetype="pdf")
    spans = [s for b in d[0].get_text("dict")["blocks"] for l in b.get("lines", []) for s in l["spans"]]

    def size_of(t):
        return next(round(s["size"]) for s in spans if s["text"].strip() == t)

    assert size_of("裝") == 10 and size_of("訂") == 10 and size_of("線") == 10
    assert size_of("副　本") == 14 and size_of("發文方式：電子交換") == 10
    words = " ".join(s["text"] for s in spans)
    assert "第1頁" in words.replace(" ", "") and "本案依分層負責規定授權業務主管決行" in words
    # 裝訂線的字在左邊界裡（版心從 2.5cm ≈ 71pt 開始）
    x_bind = next(s["bbox"][0] for s in spans if s["text"].strip() == "裝")
    assert x_bind < 71
    # 有一條直的虛線
    lines = [dr for dr in d[0].get_drawings() if dr["rect"].width < 2 and dr["rect"].height > 400]
    assert lines, "沒有畫出裝訂線"
    # 本文起點不動（標題那一行的位置跟沒加註時一樣）
    def title_y(pdf):
        dd = fitz.open(stream=pdf, filetype="pdf")
        return next(w[1] for w in dd[0].get_text("words") if "函" in w[4] and "嘉禾市資訊局" in w[4])
    assert abs(title_y(with_x) - title_y(without)) < 1.0


# ------------------------------------------------------------------ 套範本

def test_template_gets_header_footer_and_the_delegate_line():
    from tests.test_official_doc_template import LETTER_TPL
    data = odx.build_from_template(LETTER, LETTER_TPL, draft_mark=False, extras=odx.page_extras(ALL))
    styles, content = _xml(data, "styles.xml"), _xml(data, "content.xml")
    assert "<draw:line" in styles and "副　本" in styles and "<text:page-number" in styles
    assert "header-style" in styles and "footer-style" in styles, "沒有頁面設定的頁首頁尾，LibreOffice 不會畫"
    # 範本的範例句換成使用者選的那一句；沒選就清掉
    assert content.count("本案依分層負責規定授權業務主管決行") == 1
    plain = odx.build_from_template(LETTER, LETTER_TPL, draft_mark=False)
    assert "本案依分層負責" not in re.sub(r"<[^>]+>", "", _xml(plain, "content.xml"))
    # 寫在範本原本那一段（保留範本的格式），不是另外接一段在最後
    only = odx.build_from_template(LETTER, LETTER_TPL, draft_mark=False,
                                   extras=odx.page_extras({"delegate": "業務主管"}))
    # 沒選時那一段被清空、又在文件尾端 → 當成尾端空白行拿掉（避免多一張空白頁），所以多一段；
    # 另外接一段的話，清空的那一段會卡在中間留下來（＝多兩段）
    assert _xml(only, "content.xml").count("<text:p") == _xml(plain, "content.xml").count("<text:p") + 1
    assert "<style:footer" not in _xml(plain, "styles.xml")


# ------------------------------------------------------------------ 端點

@pytest.fixture
def letter_case(client, auth_off, monkeypatch):
    from tests.test_official_doc_tool import FakeLLM, _run
    from app.core import llm_settings as ls
    fake = FakeLLM()
    monkeypatch.setattr(ls.llm_settings, "is_enabled", lambda: True)
    monkeypatch.setattr(ls.llm_settings, "make_client", lambda *a, **k: fake)
    monkeypatch.setattr(ls.llm_settings, "get_model_for", lambda _t: "fake-model")
    return _run(client, mode="letter")[1]


@pytest.fixture
def sign_case(client, auth_off, monkeypatch):
    from tests.test_official_doc_tool import FakeLLM, _run
    from app.core import llm_settings as ls
    fake = FakeLLM()
    monkeypatch.setattr(ls.llm_settings, "is_enabled", lambda: True)
    monkeypatch.setattr(ls.llm_settings, "make_client", lambda *a, **k: fake)
    monkeypatch.setattr(ls.llm_settings, "get_model_for", lambda _t: "fake-model")
    return _run(client)[1]


def _export(client, cid, fmt, **kw):
    body = {"text": LETTER, "fmt": fmt, "title": "稽核報告", "case_id": cid}
    body.update(kw)
    return client.post(f"{BASE}/export", json=body)


def test_bad_extras_are_a_400(client, letter_case):
    r = _export(client, letter_case, "odt", extras={"copy_mark": "影本"})
    assert r.status_code == 400 and "正本／副本標示" in r.text


def test_letter_export_carries_the_extras(client, letter_case):
    r = _export(client, letter_case, "odt", extras=ALL)
    assert r.status_code == 200, r.text
    assert "副　本" in _xml(r.content, "styles.xml")
    assert "本案依分層負責規定授權業務主管決行" in _xml(r.content, "content.xml")


def test_sign_drops_the_letter_only_extras(client, sign_case):
    """正本標示、發文方式、分層負責、受文者地址只有函才有 —— 簽送來也不用。"""
    r = _export(client, sign_case, "odt", text="簽　　於資訊室\n主旨：測試，簽請　核示。", extras=ALL)
    assert r.status_code == 200, r.text
    styles, content = _xml(r.content, "styles.xml"), _xml(r.content, "content.xml")
    # （「公文發文方式」是樣式的名稱，看的是印出來的那一行「發文方式：…」）
    assert "副　本" not in styles and "發文方式：" not in styles and "分層負責" not in content
    assert "<draw:line" in styles and "<text:page-number" in styles     # 頁碼、裝訂線照樣有


def test_company_letter_drops_the_delegate_line(client, letter_case):
    import importlib
    path = importlib.import_module("app.tools.official_doc.router")._result_path(letter_case)
    out = json.loads(path.read_text(encoding="utf-8"))
    out["inputs"]["relation"] = od.COMPANY_RELATION
    path.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    r = _export(client, letter_case, "odt", extras=ALL)
    assert r.status_code == 200
    assert "分層負責" not in _xml(r.content, "content.xml"), "企業沒有分層負責"
    assert "副　本" in _xml(r.content, "styles.xml")


def test_png_and_svg_export(client, letter_case):
    _soffice_or_skip()
    r = _export(client, letter_case, "png", extras={"page_numbers": True})
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "image/png" and r.content[:8] == b"\x89PNG\r\n\x1a\n"
    assert ".png" in r.headers["content-disposition"]
    r = _export(client, letter_case, "svg")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("image/svg+xml") and b"<svg" in r.content[:400]
    # 多頁：一頁一張，打包成 zip
    long = LETTER.replace("說明：依", "說明：" + "依　貴局來函辦理，" * 120 + "依")
    r = _export(client, letter_case, "png", text=long)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/zip"
    names = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
    assert len(names) >= 2 and all(n.endswith(".png") for n in names) and "第1頁" in names[0]


def test_preview_cache_key_includes_the_extras(client, letter_case, monkeypatch):
    """預覽圖的快取要跟著加註變：勾了頁碼，預覽不可以還是沒勾那一張。"""
    import importlib
    rt = importlib.import_module("app.tools.official_doc.router")
    calls = []

    def fake_office(case_id, text, fmt, title, draft_mark, tpl_key, extras=None):
        calls.append(extras)
        import fitz
        doc = fitz.open()
        doc.new_page()
        return doc.tobytes(), "application/pdf", "none"

    monkeypatch.setattr(rt, "_office_bytes", fake_office)
    h = []
    for ex in ({}, {"page_numbers": True}, {"page_numbers": True}):
        r = client.post(f"{BASE}/preview", json={"case_id": letter_case, "text": LETTER, "extras": ex})
        assert r.status_code == 200, r.text
        h.append(r.json()["hash"])
    assert h[0] != h[1] and h[1] == h[2]
    assert len(calls) == 2 and calls[1].page_numbers
