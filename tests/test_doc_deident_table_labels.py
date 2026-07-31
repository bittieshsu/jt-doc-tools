"""標籤與值分屬兩個表格儲存格時也要偵測得到（GitHub issue #43）。

## 由來

回報內容：Word 文件裡放 4 個段落日期 + 1 個表格日期（`出生日期 | 1998-12-28`），
結果只抓到 3 個，表格那筆完全沒抓到。回報者自己推測是「表格的文字被切成不相鄰的
片段」或「關鍵字鄰近比對的視窗沒算到儲存格之間的距離」—— 兩個猜測都對。

實際重現（把那份 docx 轉成 PDF 再看 PyMuPDF 的輸出）：

    block0: '出生日期 1998/12/18'
    block1: '出生日期：1998-12-19'
    block2: '生日 1998.12.20'          ← 點分隔，正規表示式不吃
    block3: '出生年月日 民國87 年12 月21 日'
    block4: '出生日期'                  ← 表格：標籤自己一條 line
    block4: '1998-12-28'                ← 值在另一條 line

所以是兩個獨立的缺陷疊在一起：

1. **點分隔的日期（1998.12.20）沒被涵蓋** —— 台灣表單很常這樣寫。
2. **需要標籤的式子在表格裡整組失效** —— 比對是逐條 line 做的，標籤與值不在
   同一條 line 就永遠湊不起來。這不只影響出生日期：銀行帳號、駕照號碼、
   帳戶名稱…**所有**靠標籤定位的欄位都一樣，而表格正是它們最常出現的地方。

## 這份要守住的事

* 四種段落寫法都要抓到，而且**年份要完整**（原本 `12/18/1998` 只吃到
  `12/18/19`，遮蔽後還留著 `98`）。
* 表格那筆要抓到，而且**遮蔽範圍要落在值那一格**，不是標籤那一格。
* 跨格比對不可以製造重複，也不可以讓不需要標籤的式子（身分證、Email）亂配對。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.tools.doc_deident import patterns as P


# --------------------------------------------------------------- 純式子層級

@pytest.mark.parametrize("text,expected", [
    ("出生日期 1998/12/18", "1998/12/18"),
    ("出生日期：1998-12-19", "1998-12-19"),
    ("生日 1998.12.20", "1998.12.20"),            # 點分隔 —— 原本漏掉
    ("出生年月日 民國87年12月21日", "民國87年12月21日"),
    ("出生日期 87/12/18", "87/12/18"),
    ("出生日期 1998年12月18日", "1998年12月18日"),
    ("Date of Birth 1998.7.5", "1998.7.5"),
    ("出生日期 19981218", "19981218"),
    ("出生日期\t1998-12-28", "1998-12-28"),       # 分隔符是 tab
])
def test_dob_formats(text, expected):
    m = P.RE_DOB.search(text)
    assert m, f"沒抓到：{text!r}"
    assert m.group(1) == expected


def test_year_last_format_keeps_the_whole_year():
    """`12/18/1998` 要整個吃掉。

    原本第一個分支是「2~4 位數的年在前」，會先吃到 `12/18/19` —— 看起來有抓到，
    實際上遮蔽後還留著 `98`。這種「抓到一半」比完全沒抓到更危險，因為介面上
    會顯示已處理。
    """
    m = P.RE_DOB.search("DOB: 12/18/1998")
    assert m and m.group(1) == "12/18/1998"


def test_bare_date_without_label_is_not_a_dob():
    """沒有標籤的日期不該被當成出生日期（回報者也驗過這點）。"""
    assert not P.RE_DOB.search("合約起始 1998-12-28 至 2000-01-01"[:10])
    assert not P.RE_DOB.search("1998-12-28")


# --------------------------------------------------------- 端對端（含表格）

@pytest.fixture(scope="module")
def dob_pdf(tmp_path_factory) -> Path:
    """產出回報情境的檔案：4 個段落日期 + 1 個表格日期。"""
    docx = pytest.importorskip("docx", reason="需要 python-docx 才能組出表格")
    from app.core import office_convert

    d = tmp_path_factory.mktemp("issue43")
    doc = docx.Document()
    doc.add_paragraph("出生日期 1998/12/18")
    doc.add_paragraph("出生日期：1998-12-19")
    doc.add_paragraph("生日 1998.12.20")
    doc.add_paragraph("出生年月日 民國87年12月21日")
    t = doc.add_table(rows=1, cols=2)
    t.style = "Table Grid"
    t.rows[0].cells[0].text = "出生日期"
    t.rows[0].cells[1].text = "1998-12-28"
    src = d / "dob.docx"
    doc.save(str(src))

    pdf = d / "dob.pdf"
    try:
        office_convert.convert_to_pdf(src, pdf)
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"這台機器沒有可用的 Office 引擎：{e.__class__.__name__}")
    if not pdf.exists():
        pytest.skip("Office 引擎沒有產出 PDF")
    return pdf


def test_all_five_dates_are_found(dob_pdf):
    """回報者實測 3/4；修好之後段落 4 筆 + 表格 1 筆都要在。"""
    import fitz

    from app.tools.doc_deident.router import _build_findings_for_page
    with fitz.open(str(dob_pdf)) as doc:
        found = _build_findings_for_page(doc[0], {"dob"}, [])
    values = ["".join((f["value"] or "").split()) for f in found]
    for want in ("1998/12/18", "1998-12-19", "1998.12.20", "1998-12-28"):
        assert want in values, f"漏掉 {want}（實際抓到 {values}）"
    assert any("87" in v and "12" in v for v in values), "民國格式那筆漏掉"
    assert len(found) == 5, f"應該剛好 5 筆，實際 {len(found)}：{values}"


def test_table_hit_is_masked_on_the_value_cell(dob_pdf):
    """表格那筆要蓋在**值**那一格，不是標籤那一格。

    蓋錯格的話，遮出來的文件會是「▇▇▇▇ 1998-12-28」—— 標籤被塗掉、
    真正的個資還在。
    """
    import fitz

    from app.tools.doc_deident.router import _build_findings_for_page
    with fitz.open(str(dob_pdf)) as doc:
        page = doc[0]
        found = _build_findings_for_page(page, {"dob"}, [])
        hit = [f for f in found if "1998-12-28" in (f["value"] or "")]
        assert hit, "表格那筆沒抓到"
        bx0, by0, bx1, by1 = hit[0]["bbox"]
        # 這個框裡看到的字要是日期，不是標籤
        inside = page.get_textbox(fitz.Rect(bx0 - 1, by0 - 1, bx1 + 1, by1 + 1))
        assert "1998" in inside, f"遮蔽框裡不是日期，而是 {inside!r}"
        assert "出生日期" not in inside, "遮蔽框蓋到標籤那一格了"


def test_no_duplicate_findings(dob_pdf):
    """同一筆不可以因為跨格比對而被收兩次。"""
    import fitz

    from app.tools.doc_deident.router import _build_findings_for_page
    with fitz.open(str(dob_pdf)) as doc:
        found = _build_findings_for_page(doc[0], {"dob"}, [])
    keys = [(f["value"], round(f["bbox"][0], 1)) for f in found]
    assert len(keys) == len(set(keys)), f"有重複：{keys}"


def test_unlabelled_patterns_do_not_pair_across_cells():
    """不需要標籤的式子不參與跨格配對。

    身分證、Email 這種在單一格裡就抓得到；讓它們跨格只會把兩段不相干的字
    接起來湊出一個假的比對。
    """
    from app.tools.doc_deident.router import _scan_unit

    def unit(text: str, x0: float):
        span = {"text": text, "bbox": (x0, 100.0, x0 + 60.0, 112.0),
                "size": 11, "color": 0}
        return {"text": text, "spans": [span],
                "span_map": [0] * len(text), "span_starts": [0],
                "bbox": [x0, 100.0, x0 + 60.0, 112.0]}

    from app.tools.doc_deident.router import _join_units
    # 左格結尾的字母 + 右格的數字，接起來剛好像一個身分證字號
    pair = _join_units(unit("A", 50.0), unit("123456789", 200.0))
    got = _scan_unit(pair, {"tw_id"}, [], labelled_only=True)
    assert not got, f"不需要標籤的式子跨格配出了東西：{got}"
