"""公文撰擬：套用範本（`official_doc_odt.build_from_template`）。

範本是管理員下載的官方「筆硯」範本（函.odt、簽.odt）或機關自己的範本。
官方範本的形狀是**檔號、聯絡資訊、簽頭放在文字框（draw:frame）裡，外面再包一個
表格儲存格的段落** —— 第一版直接改那個外層段落，把框整個清掉了
（算圖才看到：簽頭、承辦人、電話都不見）。

這支用**合成範本**（照官方範本的結構縮小）驗，CI 上一定跑得到；
開發樹有真的官方範本時（`temp/official_doc_refs/`，不進公開樹）另外跑一次。
"""
from __future__ import annotations

import io
import re
import zipfile
from pathlib import Path

import pytest
from lxml import etree

from app.core import official_doc as od
from app.core import official_doc_odt as odt

NS = {
    "office": "urn:oasis:names:tc:opendocument:xmlns:office:1.0",
    "style": "urn:oasis:names:tc:opendocument:xmlns:style:1.0",
    "text": "urn:oasis:names:tc:opendocument:xmlns:text:1.0",
    "table": "urn:oasis:names:tc:opendocument:xmlns:table:1.0",
    "draw": "urn:oasis:names:tc:opendocument:xmlns:drawing:1.0",
    "fo": "urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0",
    "svg": "urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0",
}
_DECL = " ".join(f'xmlns:{k}="{v}"' for k, v in NS.items())


def _p(t: str, style: str = "P1") -> str:
    return f'<text:p text:style-name="{style}"><text:span text:style-name="T1">{t}</text:span></text:p>'


def _frame(*lines: str) -> str:
    inner = "".join(_p(x, "PF") for x in lines)
    return ('<draw:frame draw:name="f" text:anchor-type="paragraph" svg:width="5cm">'
            f'<draw:text-box>{inner}</draw:text-box></draw:frame>')


def _cell_with_frames(*frames: str) -> str:
    """官方範本的形狀：表格 → 儲存格 → 段落 → 好幾個文字框。"""
    return ('<table:table table:name="t"><table:table-column/><table:table-row><table:table-cell>'
            f'<text:p text:style-name="P1">{"".join(frames)}</text:p>'
            '</table:table-cell></table:table-row></table:table>')


def _odt(body: str) -> bytes:
    content = (f'<?xml version="1.0" encoding="UTF-8"?><office:document-content {_DECL} office:version="1.3">'
               '<office:font-face-decls><style:font-face style:name="標楷體" svg:font-family="標楷體"/>'
               '</office:font-face-decls>'
               f'<office:body><office:text>{body}</office:text></office:body></office:document-content>')
    styles = (f'<?xml version="1.0" encoding="UTF-8"?><office:document-styles {_DECL} office:version="1.3">'
              '<office:font-face-decls><style:font-face style:name="標楷體" svg:font-family="標楷體"/>'
              '</office:font-face-decls><office:styles/>'
              '<office:automatic-styles><style:page-layout style:name="Mpm1">'
              '<style:page-layout-properties fo:page-width="21.00cm" fo:page-height="29.70cm" '
              'fo:margin-top="2.00cm" fo:margin-bottom="2.00cm" fo:margin-left="2.50cm" '
              'fo:margin-right="2.21cm"/></style:page-layout></office:automatic-styles>'
              '<office:master-styles><style:master-page style:name="Standard" '
              'style:page-layout-name="Mpm1"/></office:master-styles></office:document-styles>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(zipfile.ZipInfo("mimetype"), "application/vnd.oasis.opendocument.text",
                   compress_type=zipfile.ZIP_STORED)
        z.writestr("content.xml", content)
        z.writestr("styles.xml", styles)
        z.writestr("META-INF/manifest.xml",
                   '<?xml version="1.0" encoding="UTF-8"?><manifest:manifest '
                   'xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0"/>')
    return buf.getvalue()


LETTER_TPL = _odt(
    _cell_with_frames(_frame("檔號："), _frame("保存年限："))
    + _p("示範市政府　函", "PT")
    + _cell_with_frames(_frame("地址：示範市範例路1號", "承辦人：王○華", "電話：(02)0000-0000"))
    + _p("受文者：範例機關等")
    + _p("發文日期：中華民國○年○月○日")
    + _p("發文字號：府範字第0000000000號")
    + _p("速別：最速件")
    + _p("附件：範例附件")
    + _p("主旨：範例主旨，請　查照。")
    + _p("說明：", "PL")
    + _p("一、範例說明第一點。", "PI1")
    + _p("（一）範例子項。", "PI2")
    + _p("")
    + _p("正本：範例機關")
    + _p("副本：")
    + _p("市長　○○○", "PS")
    + _p("本案依分層負責規定授權業務主管決行")
)

SIGN_TPL = _odt(
    _cell_with_frames(_frame("檔號："), _frame("保存年限："))
    # 官方簽.odt 框的文件順序：日期、單位、「於」、「簽」
    + _cell_with_frames(_frame("日期：○年○月○日"), _frame("文書科a"), _frame("於"), _frame("簽"))
    + _p("主旨：範例主旨，簽請　核示。")
    + _p("說明：", "PL")
    + _p("一、範例說明。", "PI1")
    + _p("擬辦：", "PL")
    + _p("一、範例擬辦。", "PI1")
    + _p("")
    + _p("敬陳")
    + _p("○○○、○○○○")
    + _p("○○○")
    + _p("會辦單位：")
    + ('<table:table table:name="決行"><table:table-column table:number-columns-repeated="2"/>'
       '<table:table-row>'
       '<table:table-cell>' + _p("第一層決行") + '</table:table-cell>'
       '<table:table-cell>' + _p("承辦單位") + '</table:table-cell>'
       '</table:table-row></table:table>')
)

LETTER = od.assemble_letter(
    {"subject": "為辦理資訊資產盤點，請　貴所協助填報",
     "explanation": ["依本局資訊資產管理計畫辦理。", "請　貴所於115年10月20日前填復。"],
     "measures": ["填報表格請以電子郵件回傳。"]},
    org="嘉禾市資訊局", receiver="東湖區公所", relation="down", closing="請　照辦",
    cc="本局資訊管理科", signature="局長　林○○",
    contact="地址：嘉禾市文化路1號\n承辦人：王小明\n電話：(02)1234-5678\n電子信箱：wang@example.org",
    attachments="資訊資產清冊1份")

SIGN = od.assemble_sign(
    {"subject": "為辦理本室備份設備汰換採購案，擬採公開招標方式辦理，預估金額新臺幣180萬元",
     "explanation": ["本室現有備份設備已使用10年，近半年發生故障3次。"],
     "proposal": ["擬採公開招標方式辦理。", "奉核後依程序辦理。"]},
    unit="資訊室", addressee="主任秘書、局長", date_line="中華民國115年10月7日")


def _parse(data: bytes, part: str = "content.xml"):
    return etree.fromstring(zipfile.ZipFile(io.BytesIO(data)).read(part))


def _leaf_texts(root) -> list[str]:
    tp, th = "{%s}p" % NS["text"], "{%s}h" % NS["text"]
    out = []
    for p in root.iter(tp, th):
        if any(d is not p and d.tag in (tp, th) for d in p.iter()):
            continue
        out.append("".join(p.itertext()).strip())
    return out


def _frames(root) -> int:
    return sum(1 for _ in root.iter("{%s}frame" % NS["draw"]))


# ---------------------------------------------------------------- 函


@pytest.fixture(scope="module")
def letter_out():
    return odt.build_from_template(LETTER, LETTER_TPL, draft_mark=False)


def test_frames_inside_table_cells_survive(letter_out):
    """**第一版的 bug**：外層段落的文字是所有框的字接起來，改它就把框整個清掉。"""
    assert _frames(_parse(letter_out)) == _frames(_parse(LETTER_TPL)) == 3


def test_letter_fields_are_filled_and_samples_are_gone(letter_out):
    texts = _leaf_texts(_parse(letter_out))
    joined = "\n".join(texts)
    assert "嘉禾市資訊局　函" in texts
    assert any(t.startswith("受文者：東湖區公所") for t in texts)
    assert any(t.startswith("附件：資訊資產清冊1份") for t in texts)
    assert any(t.startswith("主旨：為辦理資訊資產盤點") for t in texts)
    # 範本的範例值一個都不可以跟出去
    for sample in ("示範市政府", "範例機關等", "府範字第0000000000號", "最速件",
                   "範例主旨", "範例說明", "範例子項", "範例附件", "王○華",
                   "本案依分層負責規定"):
        assert sample not in joined, sample


def test_contact_lines_go_into_the_frame_in_order(letter_out):
    """範本的聯絡框只有三行、我們有四行 —— 多的接在框裡，順序照草稿。"""
    texts = _leaf_texts(_parse(letter_out))
    contact = [t for t in texts if re.match(r"^(地址|承辦人|電話|電子信箱)：", t)]
    assert contact == ["地址：嘉禾市文化路1號", "承辦人：王小明",
                       "電話：(02)1234-5678", "電子信箱：wang@example.org"]
    root = _parse(letter_out)
    box_texts = ["".join(p.itertext()) for box in root.iter("{%s}text-box" % NS["draw"])
                 for p in box.iter("{%s}p" % NS["text"])]
    assert "電子信箱：wang@example.org" in box_texts, "多出來的那一行要在同一個框裡"


def test_users_own_placeholder_is_not_wiped(letter_out):
    """「林○○」是使用者自己寫的 —— 清範本的 ○○○ 佔位不可以碰我們填過的段落。"""
    assert "局長　林○○" in _leaf_texts(_parse(letter_out))


def test_letter_body_keeps_items_and_copies(letter_out):
    texts = _leaf_texts(_parse(letter_out))
    i = texts.index(next(t for t in texts if t.startswith("主旨：")))
    tail = [t for t in texts[i:] if t]
    assert tail[1] == "說明："
    assert tail[2].startswith("一、依本局資訊資產管理計畫辦理")
    assert any(t.startswith("正本：東湖區公所") for t in tail)
    assert any(t.startswith("副本：本局資訊管理科") for t in tail)


def test_body_reuses_the_templates_paragraph_styles(letter_out):
    """本文用範本自己的段落樣式（字級、縮排在範本裡），不是我們內建的。"""
    root = _parse(letter_out)
    styles = {"".join(p.itertext()).strip(): p.get("{%s}style-name" % NS["text"])
              for p in root.iter("{%s}p" % NS["text"])}
    assert styles["說明："] == "PL"
    assert styles["一、依本局資訊資產管理計畫辦理。"] == "PI1"


# ---------------------------------------------------------------- 簽


@pytest.fixture(scope="module")
def sign_out():
    return odt.build_from_template(SIGN, SIGN_TPL, draft_mark=False)


def test_sign_head_unit_and_date_go_into_their_frames(sign_out):
    root = _parse(sign_out)
    assert _frames(root) == _frames(_parse(SIGN_TPL))
    box_texts = ["".join(p.itertext()).strip() for box in root.iter("{%s}text-box" % NS["draw"])
                 for p in box.iter("{%s}p" % NS["text"])]
    assert "資訊室" in box_texts, box_texts
    assert "日期：115年10月7日" in box_texts, box_texts
    assert "簽" in box_texts and "於" in box_texts, "簽頭的固定字要留著"
    assert "文書科a" not in box_texts


def test_sign_addressees_replace_the_placeholders(sign_out):
    texts = _leaf_texts(_parse(sign_out))
    j = texts.index("敬陳")
    after = [t for t in texts[j + 1:] if t]
    assert after[:2] == ["主任秘書", "局長"], after
    assert "○○○" not in "\n".join(texts)


def test_sign_keeps_the_decision_table(sign_out):
    root = _parse(sign_out)
    names = [t.get("{%s}name" % NS["table"]) for t in root.iter("{%s}table" % NS["table"])]
    assert "決行" in names
    assert "第一層決行" in _leaf_texts(root)


def test_sign_body_has_both_sections(sign_out):
    texts = [t for t in _leaf_texts(_parse(sign_out)) if t]
    i = next(n for n, t in enumerate(texts) if t.startswith("主旨："))
    assert texts[i].startswith("主旨：為辦理本室備份設備汰換採購案")
    # 說明只有一點時寫在同一行（「說明：…」），不另起「一、」
    assert any(t.startswith("說明：本室現有備份設備") for t in texts)
    assert "擬辦：" in texts
    assert "一、擬採公開招標方式辦理。" in texts
    assert "範例擬辦" not in "\n".join(texts)


# ---------------------------------------------------------------- 草稿頁首


def test_draft_header_is_actually_drawable():
    """只在 master-page 塞 `<style:header>` 不會畫出來 —— page-layout 要有 header-style。

    本文起點不往下移：上緣邊界扣掉頁首的高度（同內建版面）。
    """
    data = odt.build_from_template(LETTER, LETTER_TPL, draft_mark=True)
    s = _parse(data, "styles.xml")
    pl = s.find(".//style:page-layout[@style:name='Mpm1']", NS)
    assert pl.find("style:header-style", NS) is not None
    top = pl.find("style:page-layout-properties", NS).get("{%s}margin-top" % NS["fo"])
    assert abs(float(top.rstrip("cm")) + odt._HEADER_HEIGHT_CM - 2.0) < 0.01, top
    hdr = s.find(".//style:master-page/style:header", NS)
    assert hdr is not None and "".join(hdr.itertext()) == "草稿"


def test_no_draft_header_when_not_asked(letter_out):
    s = _parse(letter_out, "styles.xml")
    assert s.find(".//style:master-page/style:header", NS) is None
    top = s.find(".//style:page-layout-properties", NS).get("{%s}margin-top" % NS["fo"])
    assert top == "2.00cm"


# ---------------------------------------------------------------- 不是範本 / 有惡意


def test_not_an_odt_is_template_error():
    with pytest.raises(odt.TemplateError):
        odt.build_from_template(LETTER, b"not a zip")


def test_odt_without_a_subject_paragraph_is_template_error():
    with pytest.raises(odt.TemplateError):
        odt.build_from_template(LETTER, _odt(_p("這不是公文範本")))


def test_external_entities_are_not_resolved():
    """範本是管理員下載的外部檔案 —— XML 實體不展開、不連網。"""
    evil = LETTER_TPL
    z = zipfile.ZipFile(io.BytesIO(evil))
    content = z.read("content.xml").decode()
    content = content.replace(
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<?xml version="1.0" encoding="UTF-8"?><!DOCTYPE x [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>')
    content = content.replace("範例說明第一點", "&xxe;")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as out:
        for info in z.infolist():
            out.writestr(info, content if info.filename == "content.xml" else z.read(info.filename))
    try:
        data = odt.build_from_template(LETTER, buf.getvalue(), draft_mark=False)
    except odt.TemplateError:
        return
    assert b"root:" not in zipfile.ZipFile(io.BytesIO(data)).read("content.xml")


def test_templates_own_metadata_and_thumbnail_do_not_go_out():
    """機關範本的 meta.xml 帶著範本作者與原機關；縮圖是範本範例那一頁的畫面。"""
    z = zipfile.ZipFile(io.BytesIO(LETTER_TPL))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as out:
        for info in z.infolist():
            out.writestr(info, z.read(info.filename))
        out.writestr("meta.xml",
                     '<?xml version="1.0" encoding="UTF-8"?><office:document-meta '
                     'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
                     'xmlns:meta="urn:oasis:names:tc:opendocument:xmlns:meta:1.0" '
                     'xmlns:dc="http://purl.org/dc/elements/1.1/"><office:meta>'
                     '<meta:initial-creator>範本作者甲</meta:initial-creator>'
                     '<dc:title>示範市政府函範本</dc:title></office:meta></office:document-meta>')
        out.writestr("Thumbnails/thumbnail.png", b"\x89PNG fake")
    data = odt.build_from_template(LETTER, buf.getvalue(), title="資產盤點", draft_mark=False)
    zz = zipfile.ZipFile(io.BytesIO(data))
    names = zz.namelist()
    assert not any(n.startswith("Thumbnails/") for n in names)
    meta = zz.read("meta.xml").decode()
    assert "範本作者甲" not in meta and "示範市政府函範本" not in meta
    assert "資產盤點" in meta
    man = zz.read("META-INF/manifest.xml").decode()
    assert 'full-path="meta.xml"' in man and "Thumbnails/" not in man


def test_export_with_a_template_uses_it_and_needs_no_office_for_odt(monkeypatch):
    from app.core import office_convert
    monkeypatch.setattr(office_convert, "find_soffice", lambda: None)
    data, mt = odt.export(LETTER, "odt", template=LETTER_TPL, draft_mark=False)
    assert mt == odt.MEDIA_TYPES["odt"]
    assert _frames(_parse(data)) == 3, "要是套了範本的那一份，不是內建版面"


def test_export_with_a_broken_template_raises_instead_of_silently_falling_back():
    """安靜地退回內建版面的話，使用者以為套了機關範本 —— 要讓呼叫端講出來。"""
    with pytest.raises(odt.TemplateError):
        odt.export(LETTER, "odt", template=b"broken")


def test_output_is_a_valid_odt(letter_out):
    z = zipfile.ZipFile(io.BytesIO(letter_out))
    assert z.infolist()[0].filename == "mimetype"
    assert z.infolist()[0].compress_type == zipfile.ZIP_STORED
    assert z.read("mimetype") == b"application/vnd.oasis.opendocument.text"
    _parse(letter_out)
    _parse(letter_out, "styles.xml")


# ---------------------------------------------------------------- 真的官方範本（只在開發樹）

_REFS = Path(__file__).resolve().parent.parent / "temp" / "official_doc_refs"


@pytest.mark.parametrize("name,text", [("函.odt", LETTER), ("簽.odt", SIGN)], ids=["letter", "sign"])
def test_real_official_templates_keep_their_frames(name, text):
    """官方「筆硯」範本（政府資料開放平臺下載，**不隨程式散布**）—— 有就驗，沒有就 skip。"""
    path = _REFS / name
    if not path.is_file():
        pytest.skip("開發樹才有官方範本")
    tpl = path.read_bytes()
    out = odt.build_from_template(text, tpl, draft_mark=False)
    root = _parse(out)
    assert _frames(root) == _frames(_parse(tpl))
    texts = _leaf_texts(root)
    joined = "\n".join(texts)
    assert "本案依分層負責規定" not in joined
    if name == "函.odt":
        assert "嘉禾市資訊局　函" in texts
        assert "局長　林○○" in texts
        assert "電子信箱：wang@example.org" in texts
    else:
        assert "資訊室" in texts
        assert "日期：115年10月7日" in texts


def test_pdf_from_a_template_has_the_filled_text_and_the_draft_mark():
    """轉成 PDF 之後打開來看：框裡的字、草稿頁首都要在（「轉檔成功」不算驗收）。"""
    from app.core import office_convert
    if office_convert.find_soffice() is None:
        pytest.skip("沒有 Office 引擎")
    import fitz
    data, _ = odt.export(LETTER, "pdf", template=LETTER_TPL, draft_mark=True)
    doc = fitz.open(stream=data, filetype="pdf")
    # 合成範本的框只有 5cm 寬，長的那行會折開 —— 比對前把空白與換行都拿掉
    text = re.sub(r"\s", "", "".join(pg.get_text() for pg in doc))
    for s in ("草稿", "嘉禾市資訊局", "承辦人：王小明", "電子信箱：wang@example.org",
              "主旨：為辦理資訊資產盤點"):
        assert s in text, s
    assert "示範市政府" not in text
    # 署名的「○」走替代字型，抽出來的順序會亂（XML 那一層已經驗過整句）—— 這裡只驗字都在
    assert "局長" in text and "林" in text and "○○" in text


# ---------------------------------------------------------------- 文字框不可以把字蓋掉（2026-10-08）

def _overpainted(pdf: bytes) -> list:
    """PDF 裡「字畫完之後，有一塊後畫的填色矩形壓在它上面」的地方 —— 字被切掉的那一種。
    用繪圖順序（`seqno`）判斷，不用看圖：蓋住的字照樣抽得出文字，只有畫面上少半截。"""
    import fitz
    out = []
    for page in fitz.open(stream=pdf, filetype="pdf"):
        fills = [g for g in page.get_drawings() if g.get("fill") is not None]
        for sp in page.get_texttrace():
            txt = "".join(chr(c[0]) for c in sp["chars"]).strip()
            if not txt:
                continue
            box = fitz.Rect(sp["bbox"])
            for g in fills:
                inter = box & g["rect"]
                if g["seqno"] > sp["seqno"] and not inter.is_empty and inter.height > 0.5 \
                        and inter.width > 1:
                    out.append(txt)
    return out


def test_template_frames_get_a_transparent_background_and_a_minimum_height():
    frame_style = ('<office:automatic-styles><style:style style:name="fr1" style:family="graphic">'
                   '<style:graphic-properties fo:border="none"/></style:style></office:automatic-styles>')
    body = ('<text:p text:style-name="P1"><draw:frame draw:style-name="fr1" draw:name="a" '
            'text:anchor-type="paragraph" svg:width="3cm" svg:height="0.59cm"><draw:text-box>'
            + _p("檔號：", "PF") + '</draw:text-box></draw:frame>'
            '<draw:frame draw:name="pic" text:anchor-type="paragraph" svg:width="2cm" svg:height="1cm">'
            '<draw:image xlink:href="Pictures/x.png" xmlns:xlink="http://www.w3.org/1999/xlink"/>'
            '</draw:frame></text:p>' + _p("主旨：範例"))
    tpl = _odt(body)
    z = zipfile.ZipFile(io.BytesIO(tpl))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zo:
        for info in z.infolist():
            data = z.read(info.filename)
            if info.filename == "content.xml":
                data = data.replace(b"<office:body>", frame_style.encode() + b"<office:body>")
            zo.writestr(info, data)
    out = _parse(odt.build_from_template(SIGN, buf.getvalue(), draft_mark=False))
    D, SVG, FO = NS["draw"], NS["svg"], NS["fo"]
    frames = {f.get("{%s}name" % D): f for f in out.iter("{%s}frame" % D)}
    box = frames["a"].find("draw:text-box", NS)
    assert frames["a"].get("{%s}height" % SVG) is None, "文字框的高度要改成最小高度（字變高時跟著長）"
    assert box.get("{%s}min-height" % FO) == "0.59cm"
    assert frames["pic"].get("{%s}height" % SVG) == "1cm", "圖片框不動"
    gp = out.find(".//style:style[@style:name='fr1']/style:graphic-properties", NS)
    assert gp.get("{%s}fill" % D) == "none"
    assert gp.get("{%s}background-transparency" % NS["style"]) == "100%"


@pytest.mark.parametrize("name,text", [("簽.odt", SIGN), ("函.odt", LETTER)], ids=["sign", "letter"])
def test_real_templates_do_not_paint_over_their_own_labels(name, text, monkeypatch):
    """官方「簽」範本的檔號、會辦單位、決行表的欄名是**固定高度、上下疊一點點**的文字框。
    換成這台的楷體之後字比較高，下一個框的白底後畫、把字的下半截蓋掉（使用者看到「字被截斷」）。"""
    path = _REFS / name
    if not path.is_file():
        pytest.skip("開發樹才有官方範本")
    from app.core import office_convert
    if office_convert.find_soffice() is None:
        pytest.skip("沒有 Office 引擎")
    tpl = path.read_bytes()
    pdf, _ = odt.export(text, "pdf", template=tpl, draft_mark=True)
    assert _overpainted(pdf) == []
    if name == "簽.odt":
        # 前提：不處理的話這份範本真的會蓋字（不然上面那條什麼都沒驗到）
        monkeypatch.setattr(odt, "_loosen_frames", lambda root: None)
        before, _ = odt.export(text, "pdf", template=tpl, draft_mark=True)
        assert "檔號：" in _overpainted(before)
