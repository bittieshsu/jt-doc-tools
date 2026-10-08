"""公文撰擬的匯出（`app/core/official_doc_odt.py`）。

判準分兩層：

* **不需要 Office 引擎的**（ZIP 結構、XML、樣式數值、跳脫、空白、頁首）——
  直接拆 ODT 驗。CI 上一定跑得到。
* **要真的轉檔的**（DOCX / PDF、版面位置、字型替代）—— 沒有 soffice 就 skip，
  有的話**打開產出量**：頁面尺寸、抽得到的字、續行的 x 座標、內嵌的字型。
  「轉檔成功」不算驗收（CLAUDE.md §0.5）。
"""
from __future__ import annotations

import io
import os
import re
import zipfile
import xml.etree.ElementTree as ET

import pytest

from app.core import office_convert
from app.core import official_doc_odt as odt

NS = {
    "office": "urn:oasis:names:tc:opendocument:xmlns:office:1.0",
    "style": "urn:oasis:names:tc:opendocument:xmlns:style:1.0",
    "text": "urn:oasis:names:tc:opendocument:xmlns:text:1.0",
    "fo": "urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0",
    "svg": "urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0",
    "manifest": "urn:oasis:names:tc:opendocument:xmlns:manifest:1.0",
    "dc": "http://purl.org/dc/elements/1.1/",
    "meta": "urn:oasis:names:tc:opendocument:xmlns:meta:1.0",
}


def _q(prefix: str, name: str) -> str:
    return "{%s}%s" % (NS[prefix], name)


SIGN = """簽　　於資訊室
中華民國115年10月7日
主旨：為辦理備份設備採購案，請同意採公開招標方式辦理並由本室擔任承辦單位，以利後續作業進行，簽請　核示。
說明：
一、本室備份設備已使用十年，硬碟陸續故障，備份作業時有中斷，影響本局資料保全甚鉅，亟待汰換。
二、預估金額新臺幣180萬元，經費來源如下：
（一）本年度資訊設備費項下支應。
（二）不足部分擬由相關計畫經費勻支。
擬辦：擬採公開招標方式辦理。
敬陳
主任秘書
局長"""

#: SIGN 裡每一段（依序）應該落到的樣式與文字
SIGN_EXPECTED = [
    ("OD_Head", "簽　　於資訊室"),
    ("OD_Date", "中華民國115年10月7日"),
    ("OD_Label", "主旨：為辦理備份設備採購案，請同意採公開招標方式辦理並由本室擔任承辦單位，"
                 "以利後續作業進行，簽請　核示。"),
    ("OD_LabelTitle", "說明："),
    ("OD_Item1", "一、本室備份設備已使用十年，硬碟陸續故障，備份作業時有中斷，影響本局資料保全甚鉅，亟待汰換。"),
    ("OD_Item1", "二、預估金額新臺幣180萬元，經費來源如下："),
    # 括號改半形（官方範本寫「(一)」）：剛好兩個字寬，等於懸掛寬度
    ("OD_Item2", "(一)本年度資訊設備費項下支應。"),
    ("OD_Item2", "(二)不足部分擬由相關計畫經費勻支。"),
    ("OD_Label", "擬辦：擬採公開招標方式辦理。"),
    ("OD_Jingchen", "敬陳"),
    ("OD_Ending", "主任秘書"),
    ("OD_Ending", "局長"),
]


def _zip(data: bytes) -> zipfile.ZipFile:
    return zipfile.ZipFile(io.BytesIO(data))


def _xml(data: bytes, name: str) -> ET.Element:
    return ET.fromstring(_zip(data).read(name))


def _ptext(el: ET.Element) -> str:
    """把一個段落讀回純文字（text:s → 空白、text:tab → \\t、text:line-break → \\n）。"""
    out = [el.text or ""]
    for ch in el:
        if ch.tag == _q("text", "s"):
            out.append(" " * int(ch.get(_q("text", "c"), "1")))
        elif ch.tag == _q("text", "tab"):
            out.append("\t")
        elif ch.tag == _q("text", "line-break"):
            out.append("\n")
        else:
            out.append(_ptext(ch))
        out.append(ch.tail or "")
    return "".join(out)


def _paras(data: bytes) -> list[tuple[str, str]]:
    root = _xml(data, "content.xml")
    return [(p.get(_q("text", "style-name")), _ptext(p))
            for p in root.iter(_q("text", "p"))]


def _styles(data: bytes) -> dict[str, ET.Element]:
    root = _xml(data, "styles.xml")
    return {s.get(_q("style", "name")): s for s in root.iter(_q("style", "style"))}


def _cm(v: str) -> float:
    assert v.endswith("cm"), v
    return float(v[:-2])


def _para_prop(style: ET.Element, attr: str):
    pp = style.find(_q("style", "paragraph-properties"))
    return None if pp is None else pp.get(_q("fo", attr))


EM = 16 * 2.54 / 72          # 1em（16pt）＝ 0.5644cm


# ------------------------------------------------------------------ ZIP 結構

def test_mimetype_is_first_and_stored():
    data = odt.build_odt(SIGN)
    z = _zip(data)
    first = z.infolist()[0]
    assert first.filename == "mimetype"
    assert first.compress_type == zipfile.ZIP_STORED, "mimetype 必須不壓縮（ODF 規定）"
    assert z.read("mimetype") == b"application/vnd.oasis.opendocument.text"
    # 固定位移：讀取端靠第 38 個位元組開始的字串認格式
    assert data[30:38] == b"mimetype"
    assert data[38:38 + 39] == b"application/vnd.oasis.opendocument.text"


def test_manifest_lists_every_file_and_versions_agree():
    data = odt.build_odt(SIGN)
    z = _zip(data)
    man = ET.fromstring(z.read("META-INF/manifest.xml"))
    assert man.get(_q("manifest", "version")) == "1.3"
    listed = {e.get(_q("manifest", "full-path")): e.get(_q("manifest", "media-type"))
              for e in man.iter(_q("manifest", "file-entry"))}
    assert listed["/"] == "application/vnd.oasis.opendocument.text"
    for name in z.namelist():
        if name in ("mimetype", "META-INF/manifest.xml"):
            continue
        assert name in listed, f"{name} 不在 manifest 裡（讀取端會拒載）"
    # 反過來：manifest 列的檔案都要真的在
    for path in listed:
        if path != "/":
            assert path in z.namelist(), path
    # 三份 XML 都 parse 得過，office:version 一致（版本不一致 soffice 會出事）
    for name in ("content.xml", "styles.xml", "meta.xml"):
        root = ET.fromstring(z.read(name))
        assert root.get(_q("office", "version")) == "1.3", name


def test_meta_has_title_and_generator():
    root = _xml(odt.build_odt(SIGN, title="備份設備採購 <簽>"), "meta.xml")
    assert root.find(".//dc:title", NS).text == "備份設備採購 <簽>"
    assert root.find(".//meta:generator", NS).text.startswith("jt-doc-tools")
    root2 = _xml(odt.build_odt(SIGN), "meta.xml")
    assert root2.find(".//dc:title", NS) is None


# ------------------------------------------------------------------ 內容與樣式

def test_every_block_lands_in_order_with_its_style():
    assert _paras(odt.build_odt(SIGN)) == SIGN_EXPECTED


def test_label_and_items_use_the_hanging_indents():
    st = _styles(odt.build_odt(SIGN))
    # 照官方範本（筆硯 簽.odt / 函.odt）：「一、」縮 1 字、文字對齊第 3 字；每深一層多縮 1 字
    expected = {
        "OD_Label": (3, 3),
        "OD_Item1": (3, 2),
        "OD_Item2": (4, 2),
        "OD_Item3": (5, 2),
        "OD_Item4": (6, 2),
    }
    for name, (left_em, hang_em) in expected.items():
        left = _cm(_para_prop(st[name], "margin-left"))
        indent = _cm(_para_prop(st[name], "text-indent"))
        assert left == pytest.approx(left_em * EM, abs=0.002), name
        assert indent == pytest.approx(-hang_em * EM, abs=0.002), name
    # 段名單獨一行（「說明：」）不懸掛
    assert _para_prop(st["OD_LabelTitle"], "text-indent") is None
    assert _para_prop(st["OD_LabelTitle"], "margin-left") is None


def test_font_sizes_and_line_height():
    data = odt.build_odt(SIGN)
    st = _styles(data)

    def size(name):
        tp = st[name].find(_q("style", "text-properties"))
        # 西文與中文要一起設 —— 只設 fo:font-size 的話中文字還是預設大小
        assert tp.get(_q("style", "font-size-asian")) == tp.get(_q("fo", "font-size"))
        return tp.get(_q("fo", "font-size"))

    assert size("OD_Body") == "16pt"
    # 簽頭沿用內文 16pt（不另設字級）；「簽」字另外 20pt（字元樣式，見下面）
    assert st["OD_Head"].find(_q("style", "text-properties")) is None
    assert size("OD_Date") == "12pt"
    assert _para_prop(st["OD_Body"], "line-height") == f"{odt.LINE_HEIGHT_BODY_CM}cm"
    # 簽頭的「簽」字 20pt（官方範本）
    content = _zip(data).read("content.xml").decode("utf-8")
    assert '<text:span text:style-name="OD_HeadBig">簽</text:span>' in content
    assert 'style:name="OD_HeadBig"' in content and 'fo:font-size="20pt"' in content
    # 字型：西文與中文都指向同一個 font-face
    tp = st["OD_Body"].find(_q("style", "text-properties"))
    assert tp.get(_q("style", "font-name")) == "OD_Font"
    assert tp.get(_q("style", "font-name-asian")) == "OD_Font"
    ff = _xml(data, "styles.xml").find(".//style:font-face", NS)
    assert ff.get(_q("svg", "font-family")) == "標楷體"
    assert ff.get(_q("style", "font-family-generic")) == "script"


def test_page_is_a4_with_the_official_margins():
    """邊界照筆硯官方範本：上下 2.0、左 2.5、右 2.21cm。"""
    want = {"top": 2.0, "bottom": 2.0, "left": 2.5, "right": 2.21}
    assert odt.PAGE_MARGINS_CM == want
    for draft in (True, False):
        root = _xml(odt.build_odt(SIGN, draft_mark=draft), "styles.xml")
        pl = root.find(".//style:page-layout/style:page-layout-properties", NS)
        assert pl.get(_q("fo", "page-width")) == "21cm"
        assert pl.get(_q("fo", "page-height")) == "29.7cm"
        for side in ("bottom", "left", "right"):
            assert _cm(pl.get(_q("fo", f"margin-{side}"))) == pytest.approx(want[side])
        top = _cm(pl.get(_q("fo", "margin-top")))
        hdr = root.find(".//style:page-layout/style:header-style/style:header-footer-properties", NS)
        if draft:
            # 頁首放在上緣的邊界裡：上緣到頁首 ＋ 頁首總高 ＝ 本文起點 2.0cm
            assert top + _cm(hdr.get(_q("fo", "min-height"))) == pytest.approx(want["top"])
        else:
            assert hdr is None
            assert top == pytest.approx(want["top"])


def test_draft_mark_puts_a_grey_header():
    data = odt.build_odt(SIGN, draft_mark=True)
    root = _xml(data, "styles.xml")
    header = root.find(".//style:master-page/style:header", NS)
    assert header is not None
    assert [_ptext(p) for p in header.iter(_q("text", "p"))] == ["草稿"]
    st = _styles(data)["OD_Header"]
    tp = st.find(_q("style", "text-properties"))
    assert tp.get(_q("fo", "font-size")) == "10pt"
    assert tp.get(_q("fo", "color")) == "#888888"
    assert _para_prop(st, "text-align") == "end"

    root2 = _xml(odt.build_odt(SIGN, draft_mark=False), "styles.xml")
    assert root2.find(".//style:master-page/style:header", NS) is None
    assert "草稿" not in _zip(odt.build_odt(SIGN, draft_mark=False)).read("styles.xml").decode()


def test_single_digit_markers_become_fullwidth():
    """標記一律兩個字寬：括號改半形、單一位數改全形 —— 不然第一行跟續行差半個字。"""
    text = "說明：\n一、甲。\n（一）乙。\n1、丙。\n（1）丁。\n10、戊。\n（12）己。"
    got = _paras(odt.build_odt(text))
    assert got[1:] == [
        ("OD_Item1", "一、甲。"),
        ("OD_Item2", "(一)乙。"),
        ("OD_Item3", "１、丙。"),
        ("OD_Item4", "(１)丁。"),
        ("OD_Item3", "10、戊。"),       # 兩位數本來就剛好 2em
        ("OD_Item4", "(12)己。"),
    ]


def test_endorse_paragraph_and_list():
    one = "本案係本局資訊室簽辦備份設備採購，經核尚無不合，陳核。"
    assert _paras(odt.build_odt(one)) == [("OD_Para", one)]
    lst = "一、來文摘述。\n二、擬同意辦理，陳核。"
    assert _paras(odt.build_odt(lst)) == [("OD_Item1", "一、來文摘述。"),
                                         ("OD_Item1", "二、擬同意辦理，陳核。")]


def test_halfwidth_colon_label_is_normalised():
    got = _paras(odt.build_odt("主旨:請核示。"))
    assert got == [("OD_Label", "主旨：請核示。")]


def test_empty_text_is_still_a_valid_document():
    data = odt.build_odt("")
    paras = _paras(data)
    assert len(paras) == 1 and paras[0][1] == ""


# ------------------------------------------------------------------ 跳脫與空白

def test_xml_special_characters_round_trip():
    nasty = 'A<script>alert("x")&\'y\'</script>B & C > D'
    text = f"主旨：{nasty}\n{nasty}"
    data = odt.build_odt(text, title=nasty)
    paras = _paras(data)                       # parse 得過就代表跳脫正確
    assert paras[0] == ("OD_Label", f"主旨：{nasty}")
    assert paras[1] == ("OD_Para", nasty)
    assert _xml(data, "meta.xml").find(".//dc:title", NS).text == nasty
    raw = _zip(data).read("content.xml").decode()
    assert "<script>" not in raw


def test_characters_xml_cannot_carry_are_dropped_not_fatal():
    text = "主旨：甲\x00乙\x0b丙\x1f丁"
    assert _paras(odt.build_odt(text)) == [("OD_Label", "主旨：甲乙丙丁")]


def test_consecutive_spaces_use_text_s():
    text = "說明：\n一、A   B C\t D\n甲　　乙"
    data = odt.build_odt(text)
    raw = _zip(data).read("content.xml").decode()
    assert '<text:s text:c="2"/>' in raw
    assert "<text:tab/>" in raw
    paras = _paras(data)
    assert paras[1] == ("OD_Item1", "一、A   B C\t D")
    # 全形空白不是 XML 的空白，照原樣留著
    assert paras[2] == ("OD_Para", "甲　　乙")
    assert "甲　　乙" in raw


def test_inline_leading_and_trailing_spaces_are_kept():
    # parse_text 會 strip 每一行，所以直接驗 _inline：ODF 會吃掉段首段尾的空白
    xml = odt._inline("  A  ")
    p = ET.fromstring(f'<text:p xmlns:text="{NS["text"]}">{xml}</text:p>')
    assert _ptext(p) == "  A  "
    assert not xml.startswith(" ") and not xml.endswith(" ")


# ------------------------------------------------------------------ 字型挑選

def test_word_font_is_written_as_a_single_name():
    data = odt.build_odt(SIGN)
    for name in ("content.xml", "styles.xml"):
        ff = _xml(data, name).find(".//style:font-face", NS)
        assert ff.get(_q("svg", "font-family")) == odt.WORD_FONT


def test_pick_font_takes_the_first_installed(monkeypatch):
    monkeypatch.setattr(odt, "available_font_families",
                        lambda soffice=None: frozenset({"noto serif cjk tc", "tw-kai"}))
    assert odt.pick_font("標楷體;TW-Kai;Noto Serif CJK TC") == "TW-Kai"
    assert odt.pick_font("標楷體;Noto Serif CJK TC") == "Noto Serif CJK TC"
    # 一個都沒有 → 清單的第一個（交給 soffice 依 generic 退）
    assert odt.pick_font("甲;乙") == "甲"
    # 單一名字原樣，不必去查字型
    monkeypatch.setattr(odt, "available_font_families",
                        lambda soffice=None: (_ for _ in ()).throw(AssertionError("不該查")))
    assert odt.pick_font("標楷體") == "標楷體"


def test_pdf_font_list_is_never_written_into_the_file(monkeypatch):
    """Linux 的 soffice 只看清單的第一個名字 —— 清單一定要先換成裝了的那一個。"""
    monkeypatch.setattr(odt, "available_font_families",
                        lambda soffice=None: frozenset({"noto serif cjk tc"}))
    data = odt.build_odt(SIGN, font_family=odt.PDF_FONT)
    for name in ("content.xml", "styles.xml"):
        ff = _xml(data, name).find(".//style:font-face", NS)
        assert ff.get(_q("svg", "font-family")) == "'Noto Serif CJK TC'"
        assert ff.get(_q("style", "font-family-generic")) == "roman"


def test_pdf_font_prefers_kai_then_serif():
    names = odt.PDF_FONT.split(";")
    assert names[0] == odt.WORD_FONT
    kai = [i for i, n in enumerate(names) if "楷" in n or "kai" in n.lower()]
    serif = [i for i, n in enumerate(names) if "serif" in n.lower() or "明" in n]
    assert kai and serif and max(kai) < min(serif)
    # 黑體不可以出現在退路裡（LibreOffice 把標楷體退成黑體正是要擋的事）
    assert not any("sans" in n.lower() or "黑" in n for n in names)


# ------------------------------------------------------------------ export（不需要 soffice 的部分）

def test_export_odt_needs_no_office_engine(monkeypatch):
    monkeypatch.setattr(office_convert, "find_soffice", lambda: None)

    def _boom(*a, **k):
        raise AssertionError("匯出 ODT 不可以叫 Office 引擎")

    monkeypatch.setattr(office_convert, "convert_to_pdf", _boom)
    monkeypatch.setattr(office_convert, "convert_to_docx", _boom)
    data, media = odt.export(SIGN, "odt", title="測試")
    assert media == "application/vnd.oasis.opendocument.text"
    assert _paras(data) == SIGN_EXPECTED


@pytest.mark.parametrize("fmt", ["docx", "pdf"])
def test_export_without_office_engine_raises_unavailable(monkeypatch, fmt):
    monkeypatch.setattr(office_convert, "find_soffice", lambda: None)
    with pytest.raises(office_convert.OfficeUnavailableError):
        odt.export(SIGN, fmt)


@pytest.mark.parametrize("fmt", ["", "doc", "html", "PDFX", None])
def test_unknown_format_is_value_error(fmt):
    with pytest.raises(ValueError):
        odt.export(SIGN, fmt)


def test_format_name_is_case_insensitive(monkeypatch):
    monkeypatch.setattr(office_convert, "find_soffice", lambda: None)
    data, media = odt.export(SIGN, " ODT ")
    assert media.endswith("opendocument.text")


# ------------------------------------------------------------------ 真的轉檔（沒有 soffice 就 skip）

needs_soffice = pytest.mark.skipif(
    office_convert.find_soffice() is None,
    reason="這台沒有 LibreOffice / OxOffice，DOCX / PDF 匯出驗不到")


def _engines() -> list[str]:
    """這台機器上所有的 Office 引擎（依實際路徑去重）。字型替代的結果每個引擎不同
    （OxOffice 自己帶楷體、LibreOffice 沒有），所以每一個都要驗。"""
    try:
        from app.core.conv_settings import conv_settings
        paths = [office_convert.find_soffice()] + list(conv_settings.get_executable_paths())
    except Exception:
        paths = [office_convert.find_soffice()]
    seen, out = set(), []
    for p in paths:
        if not p or not os.path.exists(p) or not os.access(p, os.X_OK):
            continue
        real = os.path.realpath(p)
        if real not in seen:
            seen.add(real)
            out.append(p)
    return out


def _lines(pdf_bytes: bytes):
    import fitz
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    out = []
    for page in doc:
        for b in page.get_text("dict")["blocks"]:
            for ln in b.get("lines", []):
                t = "".join(s["text"] for s in ln["spans"])
                out.append((ln["bbox"][0] / 72 * 2.54, ln["bbox"][1] / 72 * 2.54, t))
    return doc, out


@needs_soffice
def test_docx_export_is_a_word_file_with_the_text():
    data, media = odt.export(SIGN, "docx", title="備份設備採購")
    assert media == ("application/vnd.openxmlformats-officedocument."
                     "wordprocessingml.document")
    z = _zip(data)
    assert z.testzip() is None
    doc = z.read("word/document.xml").decode("utf-8")
    assert "為辦理備份設備採購案" in doc
    assert "本室備份設備已使用十年" in doc
    # Word 用的是標楷體（不是 PDF 那串替代清單）
    styles = z.read("word/styles.xml").decode("utf-8") + doc
    assert 'w:eastAsia="標楷體"' in styles
    assert "Noto" not in styles and "TW-Kai" not in styles
    headers = [z.read(n).decode() for n in z.namelist()
               if re.match(r"word/header\d*\.xml$", n)]
    assert any("草稿" in h for h in headers)
    # A4、本文上邊界 2.0cm（1134 twips，官方範本的上邊界）
    assert 'w:w="11906"' in doc and 'w:h="16838"' in doc
    assert re.search(r'w:top="113[34]"', doc)


@needs_soffice
def test_pdf_export_is_a4_and_has_the_text():
    data, media = odt.export(SIGN, "pdf", title="備份設備採購")
    assert media == "application/pdf"
    doc, lines = _lines(data)
    page = doc[0]
    assert page.rect.width == pytest.approx(595, abs=2)
    assert page.rect.height == pytest.approx(842, abs=2)
    text = "".join(page.get_text() for page in doc).replace("\n", "")
    assert "為辦理備份設備採購案" in text
    assert "本室備份設備已使用十年" in text
    # 中英之間不可以被自動加空白（抽出來要跟原文一樣，才搜尋得到）
    assert "新臺幣180萬元" in text
    assert "草稿" in text
    assert doc.metadata.get("title") == "備份設備採購"


@needs_soffice
def test_pdf_hanging_indents_land_where_the_styles_say():
    """量 PDF 上續行的 x 座標 —— 懸掛縮排拿掉的話續行會回到左邊界。"""
    data, _ = odt.export(SIGN, "pdf")
    _, lines = _lines(data)
    left = 2.5

    def line_after(prefix):
        for i, (_, _, t) in enumerate(lines):
            if t.startswith(prefix):
                return lines[i], lines[i + 1]
        raise AssertionError(f"PDF 裡找不到開頭是 {prefix!r} 的行：{[t for *_, t in lines]}")

    first, cont = line_after("主旨：")
    assert first[0] == pytest.approx(left, abs=0.05)
    assert cont[0] == pytest.approx(left + 3 * EM, abs=0.05), "主旨的續行要對齊在「主旨：」之後"
    # 「一、」縮一個字，續行對齊第 3 字；「(一)」縮兩個字（官方範本）
    first, cont = line_after("一、")
    assert first[0] == pytest.approx(left + 1 * EM, abs=0.05)
    assert cont[0] == pytest.approx(left + 3 * EM, abs=0.05)
    item2 = next(x for x, _, t in lines if t.startswith("(一)"))
    assert item2 == pytest.approx(left + 2 * EM, abs=0.05)


@needs_soffice
def test_draft_header_does_not_push_the_body_down():
    tops = {}
    for draft in (True, False):
        data, _ = odt.export(SIGN, "pdf", draft_mark=draft)
        _, lines = _lines(data)
        tops[draft] = next(y for _, y, t in lines if t.startswith("簽"))
        assert any(t == "草稿" for *_, t in lines) is draft
    assert tops[True] == pytest.approx(tops[False], abs=0.05)


@needs_soffice
@pytest.mark.parametrize("engine", _engines() or [None])
def test_pdf_uses_an_installed_kai_or_serif_font(monkeypatch, engine):
    """字型替代：PDF 用的是 `pick_font` 挑出來、**這台真的裝了**的那一個，不是黑體。

    LibreOffice 24.2 對找不到的「標楷體」直接畫成 Noto Sans CJK TC（黑體），
    分號清單在 Linux 上也沒有用（只看第一個名字）—— 所以每個引擎都要量內嵌字型。
    """
    if engine is None:
        pytest.skip("沒有 Office 引擎")
    monkeypatch.setattr(office_convert, "find_soffice", lambda: engine)
    odt.refresh_font_cache()
    try:
        picked = odt.pick_font(odt.PDF_FONT)
        data, _ = odt.export(SIGN, "pdf")
    finally:
        odt.refresh_font_cache()
    import fitz
    doc = fitz.open(stream=data, filetype="pdf")
    fonts = {f[3] for page in doc for f in page.get_fonts()}

    def norm(s):
        return re.sub(r"[^0-9a-z]", "", s.split("+")[-1].casefold())

    # 中文名的字型內嵌時用的是英文 PostScript 名
    token = {"標楷體": "dfkai", "DFKai-SB": "dfkai",
             "新細明體": "mingliu", "PMingLiU": "mingliu"}.get(picked, norm(picked))
    assert any(token and token in norm(f) for f in fonts), (
        f"{engine}：挑的是 {picked!r}，PDF 內嵌的卻是 {sorted(fonts)}")
    assert not any("sans" in norm(f) for f in fonts), (
        f"{engine}：PDF 用了黑體 {sorted(fonts)}（挑的是 {picked!r}）")
    # 有楷體或明體可以挑時，一定要挑到其中一個（不可以落到清單第一個交給 soffice 猜）
    avail = odt.available_font_families(engine)
    if any(n.casefold() in avail for n in odt.PDF_FONT.split(";")):
        assert picked.casefold() in avail


def test_letter_title_and_fields_get_their_own_styles():
    from app.core import official_doc as od
    t = od.assemble_letter({"subject": "甲", "explanation": ["乙"]}, org="嘉禾市資訊局",
                           receiver="東湖區公所", relation="down", cc="本局資訊管理科")
    data = odt.build_odt(t, title="x")
    import io, zipfile
    content = zipfile.ZipFile(io.BytesIO(data)).read("content.xml").decode("utf-8")
    assert 'text:style-name="OD_Title">嘉禾市資訊局　函<' in content
    assert 'text:style-name="OD_Receiver">受文者：東湖區公所<' in content
    # 發文欄位 12pt、懸掛「欄位名＋冒號」的字數（發文字號：＝ 5 字）
    assert 'text:style-name="OD_Meta5">發文字號：<' in content
