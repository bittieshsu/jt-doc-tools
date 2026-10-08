"""公文撰擬：函的版面細節（2026-10-08 使用者看產出的預覽圖回報）。

* **發文字號**：選填欄位。填了照寫、沒填留空；草稿不可以自己編一個，
  而使用者自己填的字號也不可以被文號檢查標成「找不到依據」。
* **聯絡資訊**：標題之後、受文者之前的每一行（地址、連絡人、電子郵件…）都排在右邊的
  聯絡資訊區塊 —— 第一版只認得 `_META_CONTACT_KEYS` 裡的字，「連絡人」「電子郵件」
  跑成內文大字（套範本時還排在主旨前面）。
* **署名用印的空間**：正副本之前空一行、署名再往下留一段、往右排（使用者自己的函就是這樣）。
* **最後一頁空白**：範本結尾留著幾行空白，草稿一長就把它們擠到第二頁，多出一張只有頁首頁尾的空白頁。
"""
from __future__ import annotations

import io
import re
import zipfile

import pytest
from lxml import etree

from app.core import official_doc as od
from app.core import official_doc_odt as odt
from tests.test_official_doc_template import LETTER_TPL, SIGN, SIGN_TPL, _odt, _frame, _p

NS = {
    "office": "urn:oasis:names:tc:opendocument:xmlns:office:1.0",
    "style": "urn:oasis:names:tc:opendocument:xmlns:style:1.0",
    "text": "urn:oasis:names:tc:opendocument:xmlns:text:1.0",
    "draw": "urn:oasis:names:tc:opendocument:xmlns:drawing:1.0",
    "fo": "urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0",
}
T = "{%s}" % NS["text"]

CONTACT = "地址：嘉禾市文化路1號\n連絡人：王小明\n電話：(02)1234-5678\n電子郵件：wang@example.org"

LETTER = od.assemble_letter(
    {"subject": "為辦理資訊資產盤點，請　貴所協助填報",
     "explanation": ["依本局資訊資產管理計畫辦理。"],
     "measures": ["填報表格請以電子郵件回傳。"]},
    org="嘉禾市資訊局", receiver="東湖區公所", relation="down", closing="請　照辦",
    cc="本局資訊管理科", signature="局長　林○○", contact=CONTACT)


def _parse(data: bytes, part: str = "content.xml"):
    return etree.fromstring(zipfile.ZipFile(io.BytesIO(data)).read(part))


def _styled(text: str) -> list[tuple[str, str]]:
    return odt._paragraphs(text)


# ------------------------------------------------------------------ 發文字號

def test_doc_no_is_written_when_given_and_left_empty_otherwise():
    content = {"subject": "請派員參加研習", "explanation": ["依計畫辦理。"]}
    lines = od.assemble_letter(content, org="嘉禾市資訊局", receiver="東湖區公所",
                               relation="down", doc_no="嘉資字第 1150000001號").split("\n")
    assert "發文字號：嘉資字第 1150000001號" in lines
    # 換行與連續空白收成一行（欄位是一行字）
    lines = od.assemble_letter(content, relation="down", doc_no="嘉資字\n第1150000001號").split("\n")
    assert "發文字號：嘉資字 第1150000001號" in lines
    # 沒填就留空 —— 草稿不可以自己編一個
    lines = od.assemble_letter(content, relation="down").split("\n")
    assert "發文字號：" in lines
    # 企業的函也一樣有這一欄（自己編號的公司用得到）
    lines = od.assemble_letter(content, relation=od.COMPANY_RELATION, doc_no="節字第0001號").split("\n")
    assert "發文字號：節字第0001號" in lines


def _fake_ask(explanation):
    import json

    def ask(prompt):
        if '"explanation"' in prompt:
            return json.dumps({"subject": "請派員參加研習", "explanation": [explanation],
                               "measures": []}, ensure_ascii=False)
        return json.dumps({"subject": {"value": "請派員參加研習", "quote": "派員參加研習"}},
                          ensure_ascii=False)
    return ask


def test_users_own_doc_no_is_not_flagged_as_unsupported():
    """字號是使用者自己填的事實 —— 內文提到它（「本函發文字號為…」）不可以被文號檢查標成
    「原文找不到」。發文字號那一行本身是程式寫的欄位，本來就不檢查。"""
    d = od.run_letter("請東湖區公所派員參加研習。", _fake_ask("本函發文字號為嘉資字第1150000001號。"),
                      relation="down", receiver="東湖區公所", doc_no="嘉資字第1150000001號")
    assert "發文字號：嘉資字第1150000001號" in d.text.split("\n")
    assert "嘉資字第1150000001號" in od.strip_frame(d.text), "前提：內文真的提到了字號"
    hits = [i for i in d.issues if "1150000001" in (i.snippet or "")]
    assert not hits, [(i.code, i.message) for i in hits]


def test_a_doc_no_that_nobody_gave_is_still_flagged():
    """反向對照：同一個字號，不是使用者填的（出現在內文裡）就要標出來。"""
    text = od.assemble_letter({"subject": "請派員參加研習",
                               "explanation": ["依本局115年10月1日嘉資字第1150000001號函辦理。"]},
                              org="嘉禾市資訊局", receiver="東湖區公所", relation="down")
    got = od.check_draft(text, ["請東湖區公所派員參加研習。"], mode="letter")
    assert any("1150000001" in (i.snippet or "") for i in got), [i.code for i in got]


@pytest.mark.parametrize("draft,src,flag", [
    # 照抄原文的字號，前面接什麼句子都一樣是有依據的
    ("說明：一、依嘉禾市政府115年10月1日府資字第1150000001號函辦理。",
     "市府10月1日府資字第1150000001號函要我們辦理。", False),
    ("說明：一、本函發文字號為嘉資字第1150000001號。", "字號：嘉資字第1150000001號", False),
    # 代字錯了（府授資 vs 府資）、號碼錯了都要標
    ("說明：一、依本府府授資字第1150000001號函辦理。", "府資字第1150000001號函", True),
    ("說明：一、依府資字第1150000002號函辦理。", "府資字第1150000001號函", True),
    # 沒有「字」的寫法（第1150000001號）照舊整段比
    ("說明：一、依第1150000009號函辦理。", "第1150000001號函", True),
])
def test_docno_check_compares_the_number_not_the_sentence_before_it(draft, src, flag):
    got = od.check_draft(draft, [src], mode="letter")
    hits = [i for i in got if i.code == "docno_unsupported"]
    assert bool(hits) is flag, [i.args for i in got]
    for i in hits:
        assert i.snippet in draft, "snippet 要是草稿裡的原文（點一條就在草稿裡選取它）"
        assert not i.snippet.startswith(("說明", "依本府府", "一、")), "畫面上不要連前面的句子一起列"


def test_router_passes_doc_no_through():
    import importlib
    from fastapi import HTTPException
    rt = importlib.import_module("app.tools.official_doc.router")
    p = rt._parse_inputs({"mode": "letter", "relation": "down", "narrative": "請派員參加研習",
                          "doc_no": "嘉資字第1150000001號"})
    assert p["doc_no"] == "嘉資字第1150000001號"
    assert rt._parse_inputs({"mode": "letter", "relation": "down",
                             "narrative": "請派員參加研習"})["doc_no"] == ""
    with pytest.raises(HTTPException) as e:
        rt._parse_inputs({"mode": "letter", "relation": "down", "narrative": "x",
                          "doc_no": "字" * (od.MAX_FIELD_CHARS + 1)})
    assert e.value.status_code == 400


def test_page_has_the_doc_no_field_and_sends_it(client, auth_off):
    html = client.get("/tools/official-doc/").text
    assert 'id="odDocNo"' in html
    assert re.search(r"doc_no:\s*el\('odDocNo'\)\.value", html), "送出時沒帶發文字號"
    assert "put('odDocNo', inp.doc_no)" in html, "重新開啟時沒帶回發文字號"


# ------------------------------------------------------------------ 聯絡資訊

def test_every_contact_line_goes_to_the_contact_block():
    paras = _styled(LETTER)
    styles = {t: s for s, t in paras}
    for line in CONTACT.split("\n"):
        assert styles.get(line) == "OD_Contact", (line, styles.get(line))
    # 受文者以後不算（受文者、發文日期照原本的欄位樣式）
    assert styles["受文者：東湖區公所"] == "OD_Receiver"
    assert styles["發文日期："] != "OD_Contact"


def test_file_number_fields_are_not_contact_lines():
    paras = dict((t, s) for s, t in _styled(LETTER))
    assert paras["檔　　號："] == "OD_MetaFile" and paras["保存年限："] == "OD_MetaFile"


def test_a_sign_has_no_contact_block():
    """簽沒有「○○　函」標題 —— 開頭幾行不可以被當成聯絡資訊。"""
    assert "OD_Contact" not in {s for s, _ in _styled(SIGN)}


def test_template_puts_every_contact_line_in_the_frame():
    out = odt.build_from_template(LETTER, LETTER_TPL, draft_mark=False)
    root = _parse(out)
    in_box = ["".join(p.itertext()) for box in root.iter("{%s}text-box" % NS["draw"])
              for p in box.iter(T + "p")]
    for line in CONTACT.split("\n"):
        assert line in in_box, (line, in_box)
    # 本文（框外）不可以再出現一次
    body = []
    for p in root.iter(T + "p"):
        if any(etree.QName(a).localname == "text-box" for a in p.iterancestors()):
            continue
        if any(c.tag == T + "p" for c in p.iterdescendants()):
            continue
        body.append("".join(p.itertext()).strip())
    for line in ("連絡人：王小明", "電子郵件：wang@example.org"):
        assert line not in body, line


# ------------------------------------------------------------------ 署名與用印的空間

def test_letter_gets_a_gap_before_the_copies_and_a_sign_style():
    paras = _styled(LETTER)
    styles = [s for s, _ in paras]
    texts = [t for _, t in paras]
    i = texts.index("正本：東湖區公所")
    assert styles[i - 1] == "OD_Gap" and texts[i - 1] == ""
    assert styles.count("OD_Gap") == 1, "副本前面不要再空一行"
    assert paras[-1] == ("OD_Sign", "局長　林○○")


def test_sign_document_keeps_its_own_ending():
    """簽不受影響：敬陳縮四個字、陳核對象頂格，沒有空行也沒有署名樣式。"""
    paras = _styled(SIGN)
    styles = {s for s, _ in paras}
    assert "OD_Gap" not in styles and "OD_Sign" not in styles
    assert ("OD_Jingchen", "敬陳") in paras


def test_sign_style_leaves_room_for_the_seal():
    st = _parse(odt.build_odt(LETTER), "styles.xml")
    S, FO = "{%s}" % NS["style"], "{%s}" % NS["fo"]
    sign = next(e for e in st.iter(S + "style") if e.get(S + "name") == "OD_Sign")
    pp = sign.find(S + "paragraph-properties")
    assert float(pp.get(FO + "margin-top").rstrip("cm")) >= 2.0, "署名上面要留用印的空間"
    assert float(pp.get(FO + "margin-left").rstrip("cm")) > 3.0, "署名要往右排"


def _soffice_or_skip():
    from app.core import office_convert
    if not office_convert.find_soffice():
        pytest.skip("這台沒有 Office 引擎")


def test_pdf_layout_matches_the_users_own_letter():
    """算圖確認：聯絡資訊在右半邊、署名比副本低一大段而且偏右。"""
    _soffice_or_skip()
    import fitz
    pdf, _ = odt.export(LETTER, "pdf", draft_mark=False)
    page = fitz.open(stream=pdf, filetype="pdf")[0]
    lines = {}
    for b in page.get_text("dict")["blocks"]:
        for ln in b.get("lines", []):
            t = "".join(s["text"] for s in ln["spans"]).replace(" ", "")
            if t:
                lines.setdefault(t, ln["bbox"])
    width = page.rect.width

    def find(prefix):
        return next(v for k, v in lines.items() if k.startswith(prefix))

    for prefix in ("地址：", "連絡人：", "電話：", "電子郵件："):
        assert find(prefix)[0] > width / 2 - 20, (prefix, find(prefix))
    cc, sign, body = find("副本："), find("局長"), find("主旨：")
    assert sign[1] - cc[3] > 50, f"署名跟副本之間只有 {sign[1] - cc[3]:.0f}pt"
    assert sign[0] > body[0] + 120, "署名要往右排"


# ------------------------------------------------------------------ 最後一頁不可以空白

def _tail_doc(body: str) -> etree._Element:
    return _parse(_odt(body))


def _last_texts(root, n=3):
    out = []
    for p in root.iter(T + "p"):
        out.append((p.get(T + "style-name"), "".join(p.itertext())))
    return out[-n:]


def test_trailing_blank_paragraphs_are_removed():
    root = _tail_doc(_p("主旨：範例。") + _p("") + _p("　　　　　") + _p(" "))
    n = odt._trim_trailing_blank(root)
    assert n == 3
    assert [t for _, t in _last_texts(root, 1)] == ["主旨：範例。"]


def test_blank_paragraphs_before_real_content_are_kept():
    """只動「最尾端」—— 中間的空白行是排版用的，不可以動。"""
    root = _tail_doc(_p("主旨：範例。") + _p("") + _p("敬陳"))
    assert odt._trim_trailing_blank(root) == 0
    assert [t for _, t in _last_texts(root, 3)] == ["主旨：範例。", "", "敬陳"]


def test_blank_paragraph_holding_a_frame_is_kept():
    """框的錨點段落沒有字，但它不是空白 —— 拿掉的話框（機關標誌、會辦欄）就不見了。
    用**沒有字的圖片框**：有字的框會讓外層段落本身就有字，驗不到這一道。"""
    img = ('<draw:frame draw:name="logo" text:anchor-type="paragraph" svg:width="2cm" svg:height="2cm">'
           '<draw:image xmlns:xlink="http://www.w3.org/1999/xlink" xlink:href="Pictures/logo.png"/>'
           '</draw:frame>')
    root = _tail_doc(_p("主旨：範例。") + f'<text:p text:style-name="P1">{img}</text:p>')
    assert odt._trim_trailing_blank(root) == 0
    assert sum(1 for _ in root.iter("{%s}frame" % NS["draw"])) == 1


def test_trailing_blank_after_a_table_is_removed_but_the_table_stays():
    tbl = ('<table:table table:name="決行"><table:table-column/><table:table-row><table:table-cell>'
           + _p("第一層決行") + '</table:table-cell></table:table-row></table:table>')
    root = _tail_doc(_p("主旨：範例。") + tbl + _p("") + _p("　　"))
    assert odt._trim_trailing_blank(root) == 2
    texts = ["".join(p.itertext()) for p in root.iter(T + "p")]
    assert texts[-1] == "第一層決行", "表格裡的段落不可以被當成尾端空白"


def test_last_blank_paragraph_of_a_section_is_shrunk_not_removed():
    """一節只剩它時不可以刪（節不可以是空的）—— 改成 1pt 高的樣式。"""
    sec = '<text:section text:name="S1">' + _p("") + "</text:section>"
    root = _tail_doc(_p("主旨：範例。") + sec)
    assert odt._trim_trailing_blank(root) == 1
    sec_el = next(root.iter(T + "section"))
    kids = [c for c in sec_el if isinstance(c.tag, str)]
    assert len(kids) == 1 and kids[0].get(T + "style-name") == odt._TAIL_STYLE
    st = root.find(".//{%s}style[@{%s}name='%s']" % (NS["style"], NS["style"], odt._TAIL_STYLE))
    assert st is not None


def test_template_output_does_not_end_with_blank_paragraphs():
    tpl = _odt(zipfile.ZipFile(io.BytesIO(SIGN_TPL)).read("content.xml").decode("utf-8")
               .split("<office:text>")[1].split("</office:text>")[0]
               + _p("") + _p("　　　　　"))
    out = odt.build_from_template(SIGN, tpl, draft_mark=False)
    root = _parse(out)
    body = root.find(".//{%s}text" % NS["office"])
    last = [p for p in body.iter(T + "p", T + "h")
            if not any(etree.QName(a).localname in ("table-cell", "text-box")
                       for a in p.iterancestors())
            and p.get(T + "style-name") != odt._TAIL_STYLE][-1]
    assert "".join(last.itertext()).strip(" 　"), "範本結尾的空白行還在"


# ------------------------------------------------------------------ 時刻（2026-10-08 Nemotron 實跑抓到的誤報）

_TIME_SRC = ["研習時間是11月6日晚上8點到10點，在本局三樓會議室，共有3點注意事項。"]


@pytest.mark.parametrize("draft,flag", [
    ("說明：一、研習時間為11月6日20:00至22:00。", False),     # 換成 24 小時制
    ("說明：一、研習時間為11月6日晚上8時至10時。", False),
    ("說明：一、研習時間為11月6日20時至22時。", False),
    ("說明：一、研習時間為11月6日下午8點30分起。", True),      # 原文沒有 8 點半
    ("說明：一、研習時間為11月6日21:00至22:00。", True),       # 21 點原文沒有
    ("說明：一、研習時間為11月6日上午8點至10點。", True),      # 原文是晚上
])
def test_times_are_compared_as_times_of_day(draft, flag):
    got = od.check_draft(draft, _TIME_SRC, mode="letter")
    codes = [i.code for i in got]
    assert ("time_unsupported" in codes) is flag, [(i.code, i.args) for i in got]
    # 「20:00」不可以拆成「20」與「00」兩個數量
    assert "qty_unsupported" not in codes, [(i.code, i.args) for i in got]


def test_count_of_points_is_not_a_time():
    """「3點注意事項」「第8點」不是時刻。"""
    assert od.find_times("共有3點注意事項，依第8點辦理，時間另訂，每次3小時") == []


@pytest.mark.parametrize("text,minutes", [
    ("晚上8點", {20 * 60}), ("8點到", {8 * 60, 20 * 60}), ("下午2時30分", {14 * 60 + 30}),
    ("中午12點", {12 * 60}), ("中午1點", {13 * 60}), ("凌晨12:30", {30}), ("20:00", {20 * 60}),
    ("上午9點半", {9 * 60 + 30}), ("十點至", {10 * 60, 22 * 60}),
])
def test_time_values(text, minutes):
    got = od.find_times(text)
    assert len(got) == 1 and set(got[0].minutes) == minutes, got


def test_an_omitted_time_is_hinted():
    hints = od.omission_issues("說明：一、研習在本局辦理。", _TIME_SRC[0])
    assert any(i.args == ("晚上8點",) for i in hints), [i.args for i in hints]
    hints = od.omission_issues("說明：一、研習時間為20:00至22:00。", _TIME_SRC[0])
    assert not any("8點" in str(i.args) or "10點" in str(i.args) for i in hints), [i.args for i in hints]


# ------------------------------------------------------------------ 套範本：企業的函、範本沒有聯絡欄位、署名

def _leaf(out: bytes) -> list[tuple[str, str]]:
    root = _parse(out)
    res = []
    for p in root.iter(T + "p"):
        if any(c.tag == T + "p" for c in p.iterdescendants()):
            continue
        res.append((p.get(T + "style-name"), "".join(p.itertext())))
    return res


COMPANY = od.assemble_letter(
    {"subject": "申請驗收", "explanation": ["本公司已完成安裝。"], "measures": ["請安排驗收。"]},
    org="範例資訊公司", receiver="嘉禾市資訊局", relation=od.COMPANY_RELATION,
    contact="地址：嘉禾市文化路1號\n聯絡人：王小明", signature="範例資訊公司　負責人　林○○")


def test_company_letter_template_has_no_agency_file_fields():
    tpl = _odt(zipfile.ZipFile(io.BytesIO(LETTER_TPL)).read("content.xml").decode("utf-8")
               .split("<office:text>")[1].split("</office:text>")[0]
               .replace(_p("附件：範例附件"), _p("密等及解密條件或保密期限：普通") + _p("附件：範例附件")))
    texts = [t for _, t in _leaf(odt.build_from_template(COMPANY, tpl, draft_mark=False))]
    for k in ("檔號", "保存年限", "密等"):
        assert not any(k in t for t in texts), (k, texts)
    # 反向對照：機關的函照樣有（欄位名留著、值空著）
    agency = [t for _, t in _leaf(odt.build_from_template(LETTER, tpl, draft_mark=False))]
    assert "檔號：" in agency and "保存年限：" in agency and any(t.startswith("密等") for t in agency)


NO_CONTACT_TPL = _odt(_p("示範市政府　函", "PT") + _p("受文者：範例") + _p("發文日期：")
                      + _p("主旨：範例主旨。") + _p("正本：範例") + _p("副本：") + _p("市長　○○○", "PS"))


def test_template_without_contact_frame_keeps_the_contact_lines():
    got = _leaf(odt.build_from_template(LETTER, NO_CONTACT_TPL, draft_mark=False))
    texts = [t for _, t in got]
    i = texts.index("嘉禾市資訊局　函")
    assert texts[i + 1:i + 5] == CONTACT.split("\n"), texts
    assert all(s == "JT_ContactR" for s, _ in got[i + 1:i + 5])
    root = _parse(odt.build_from_template(LETTER, NO_CONTACT_TPL, draft_mark=False))
    st = root.find(".//{%s}style[@{%s}name='JT_ContactR']" % (NS["style"], NS["style"]))
    pp = st.find("{%s}paragraph-properties" % NS["style"])
    assert float(pp.get("{%s}margin-left" % NS["fo"]).rstrip("cm")) >= 5, "聯絡資訊要排在右半邊"


def test_template_signature_gets_room_for_the_seal():
    out = odt.build_from_template(LETTER, NO_CONTACT_TPL, draft_mark=False)
    got = _leaf(out)
    assert got[-1] == ("JT_SignGap", "局長　林○○"), got[-3:]
    root = _parse(out)
    st = root.find(".//{%s}style[@{%s}name='JT_SignGap']" % (NS["style"], NS["style"]))
    pp = st.find("{%s}paragraph-properties" % NS["style"])
    assert float(pp.get("{%s}margin-top" % NS["fo"]).rstrip("cm")) >= 1.5
    # 範本自己的段落樣式照留（左右位置照範本）：新樣式以 PS 為父樣式
    assert st.get("{%s}parent-style-name" % NS["style"]) == "PS"


def test_signature_with_an_automatic_style_keeps_its_own_properties():
    """官方範本的署名多半是**自動樣式**（content.xml 裡的 P5 這種）—— 自動樣式不能當父樣式，
    要複製一份再改上方距離；原本的對齊、縮排照留。合成範本沒有自動樣式時這條路走不到。"""
    auto = ('<style:style style:name="P9" style:family="paragraph" style:parent-style-name="PS">'
            '<style:paragraph-properties fo:margin-left="7cm" fo:text-align="start"/></style:style>')
    tpl = _odt(_p("示範市政府　函", "PT") + _p("受文者：範例") + _p("主旨：範例主旨。")
               + _p("正本：範例") + _p("副本：") + _p("市長　○○○", "P9"))
    z = zipfile.ZipFile(io.BytesIO(tpl))
    content = z.read("content.xml").decode("utf-8").replace(
        "<office:body>", f"<office:automatic-styles>{auto}</office:automatic-styles><office:body>")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as out:
        for info in z.infolist():
            out.writestr(info, content if info.filename == "content.xml" else z.read(info.filename))
    root = _parse(odt.build_from_template(LETTER, buf.getvalue(), draft_mark=False))
    st = root.find(".//{%s}style[@{%s}name='JT_SignGap']" % (NS["style"], NS["style"]))
    pp = st.find("{%s}paragraph-properties" % NS["style"])
    assert pp.get("{%s}margin-left" % NS["fo"]) == "7cm", "範本自己的縮排要留著"
    assert float(pp.get("{%s}margin-top" % NS["fo"]).rstrip("cm")) >= 1.5
    assert st.get("{%s}parent-style-name" % NS["style"]) == "PS"
