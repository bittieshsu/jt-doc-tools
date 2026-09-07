"""試算表翻譯的兩件事：預覽要看得到東西、產出要開在內容的開頭。

兩個都是 v1.15.14 使用者回報的（並排比對右邊整片空白 / 打開翻譯後的檔案
乍看是空的），而且**兩個都不會有任何錯誤訊息** —— soffice 讀不進去的
工作表照樣回傳碼 0、照樣產得出 PDF，只是裡面只剩頁首頁尾。
"""
from __future__ import annotations

import importlib
import io
import re
import sys
import zipfile
from xml.etree import ElementTree as ET

import pytest

from app.core import office_text_map as M

R = importlib.import_module("app.tools.doc_translate.router")

_XL = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"

#: 真實檔案的形狀：`fitToPage` / `fitToWidth` / `fitToHeight` **原本就寫了值**。
#: 只把新值接在後面的話會變成同名屬性出現兩次 —— 那是不合法的 XML。
_SHEET = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    f'<worksheet xmlns="{_XL}">'
    '<sheetPr filterMode="false"><pageSetUpPr fitToPage="false"/></sheetPr>'
    '<dimension ref="A1:D9"/>'
    '<sheetViews><sheetView topLeftCell="A1" workbookViewId="0">'
    '<pane xSplit="0" ySplit="1" topLeftCell="A338" activePane="bottomLeft"'
    ' state="frozen"/>'
    '<selection pane="topLeft" activeCell="A1" sqref="A1"/>'
    '<selection pane="bottomLeft" activeCell="A88" sqref="A88"/>'
    "</sheetView></sheetViews>"
    '<sheetData><row r="1"><c r="A1" t="s"><v>0</v></c></row></sheetData>'
    '<pageSetup paperSize="9" scale="100" fitToWidth="1" fitToHeight="1"'
    ' orientation="portrait"/>'
    "</worksheet>"
)
_SHARED = (f'<sst xmlns="{_XL}"><si><t>Requirements v4.0</t></si></sst>')


def _xlsx(sheet: str = _SHEET) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("xl/sharedStrings.xml", _SHARED)
        z.writestr("xl/worksheets/sheet1.xml", sheet)
    return buf.getvalue()


def _sheet_of(data: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        return z.read("xl/worksheets/sheet1.xml").decode("utf-8")


# ---- 預覽：縮成一頁寬不可以把工作表改壞 --------------------------------

def test_fit_to_width_keeps_the_sheet_readable():
    """改完的工作表**一定要還讀得進去**。

    這條是整件事的核心：原本的寫法產生
    `<pageSetUpPr fitToPage="false" fitToPage="1"/>`，XML 剖析器直接拒絕，
    而 LibreOffice 拒絕之後**不會報錯，會安靜地當成一張空白表**。
    """
    sheet = _sheet_of(R._fit_to_width(_xlsx()))
    ET.fromstring(sheet)          # 壞掉的話這行就會丟 ParseError


def test_fit_to_width_replaces_the_print_settings_instead_of_adding_them():
    sheet = _sheet_of(R._fit_to_width(_xlsx()))
    assert sheet.count("fitToPage=") == 1
    assert sheet.count("fitToWidth=") == 1
    assert sheet.count("fitToHeight=") == 1
    assert 'fitToPage="1"' in sheet
    assert 'fitToWidth="1"' in sheet and 'fitToHeight="0"' in sheet
    # 原本就有的其他列印設定不可以被吃掉
    assert 'paperSize="9"' in sheet


@pytest.mark.parametrize("sheet", [
    # 沒有 pageSetUpPr，但 sheetPr 底下已經有別的元素（順序有規定）
    f'<worksheet xmlns="{_XL}"><sheetPr><tabColor rgb="FFFF0000"/></sheetPr>'
    '<dimension ref="A1"/><sheetData/></worksheet>',
    # sheetPr 是自閉合的
    f'<worksheet xmlns="{_XL}"><sheetPr filterMode="false"/>'
    '<dimension ref="A1"/><sheetData/></worksheet>',
    # 連 sheetPr 都沒有
    f'<worksheet xmlns="{_XL}"><dimension ref="A1"/><sheetData/></worksheet>',
    # 單引號的屬性值
    f'<worksheet xmlns="{_XL}"><dimension ref="A1"/><sheetData/>'
    "<pageSetup fitToWidth='3'/></worksheet>",
])
def test_fit_to_width_handles_every_shape_without_breaking_the_xml(sheet: str):
    out = _sheet_of(R._fit_to_width(_xlsx(sheet)))
    ET.fromstring(out)
    assert 'fitToPage="1"' in out
    assert out.count("fitToWidth=") == 1 and 'fitToWidth="1"' in out


def test_a_patch_that_would_break_the_sheet_is_thrown_away(monkeypatch):
    """改不動就退回原本的列印設定 —— **不可以送一份壞檔案給 soffice**。

    欄位被切到後面幾頁只是預覽難看；送壞檔案是整片空白，看起來像轉檔失敗。
    """
    monkeypatch.setattr(R, "_fit_sheet_xml", lambda x: "<not-xml")
    data = _xlsx()
    assert _sheet_of(R._fit_to_width(data)) == _sheet_of(data)


# ---- 預覽：原文與譯文兩邊要套一樣的設定 --------------------------------

def test_both_sides_get_the_same_print_settings(monkeypatch):
    """原稿是存成沒有副檔名的 `dt_<id>_src`。

    拿 `suffix` 判斷是不是試算表的話，**只有譯文那邊會縮成一頁寬**，原文
    照樣被切到後面幾頁 —— 兩邊條件不同，並排比對就不能證明任何事。
    """
    seen: list[str] = []
    monkeypatch.setattr(R.office_convert, "convert_to_pdf",
                        lambda src, dst: (seen.append(src.name),
                                          dst.write_bytes(b"%PDF-1.4\n")))

    class _Doc:
        page_count = 0

        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setitem(sys.modules, "fitz",
                        type("m", (), {"open": staticmethod(lambda p: _Doc())}))

    uid = "f" * 32
    src = R._src_path(uid)          # ← 沒有副檔名，跟正式路徑一樣
    src.write_bytes(_xlsx())
    try:
        R._render_side(uid, src, "src", ".xlsx")
        assert seen == [f"dt_{uid}_src_fit.xlsx"], "原文那邊沒有套用縮成一頁寬"
    finally:
        for f in R.settings.temp_dir.glob(f"dt_{uid}_*"):
            f.unlink(missing_ok=True)
        src.unlink(missing_ok=True)


# ---- 產出：打開時要看得到內容 ------------------------------------------

def test_translated_spreadsheet_opens_where_the_content_is():
    units, state = M.extract_units(_xlsx(), ".xlsx")
    sheet = _sheet_of(M.rebuild(state, {}, units))
    root = ET.fromstring(sheet)
    pane = root.find(f".//{{{_XL}}}pane")
    assert pane.get("topLeftCell") == "A2", "捲動位置沒有歸零"
    # 凍結窗格本身不可以被動到
    assert pane.get("ySplit") == "1" and pane.get("state") == "frozen"
    sels = {s.get("pane"): s for s in root.iter(f"{{{_XL}}}selection")}
    assert sels["topLeft"].get("activeCell") == "A1"
    assert sels["bottomLeft"].get("activeCell") == "A2"
    assert sels["bottomLeft"].get("sqref") == "A2"


def test_resetting_the_view_does_not_touch_any_cell():
    units, state = M.extract_units(_xlsx(), ".xlsx")
    out = _sheet_of(M.rebuild(state, {}, units))
    cells = lambda s: [re.sub(r"\s+/>", "/>", c)
                       for c in re.findall(r"<c [^>]*>.*?</c>", s)]
    assert cells(out) == cells(_SHEET)


def test_a_sheet_without_a_frozen_pane_still_goes_back_to_the_top():
    sheet = (f'<worksheet xmlns="{_XL}"><dimension ref="A1"/>'
             '<sheetViews><sheetView topLeftCell="A200" workbookViewId="0">'
             '<selection activeCell="B210" sqref="B210"/>'
             "</sheetView></sheetViews><sheetData/></worksheet>")
    units, state = M.extract_units(_xlsx(sheet), ".xlsx")
    root = ET.fromstring(_sheet_of(M.rebuild(state, {}, units)))
    view = root.find(f".//{{{_XL}}}sheetView")
    assert view.get("topLeftCell") == "A1"
    assert view.find(f"{{{_XL}}}selection").get("activeCell") == "A1"


def test_ods_scroll_state_is_reset_too():
    """ODF 把同一件事存在 `settings.xml`（我們原封不動複製的那一份）。"""
    body = (
        '<?xml version="1.0" encoding="UTF-8"?><office:document-settings '
        'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
        'xmlns:config="urn:oasis:names:tc:opendocument:xmlns:config:1.0">'
        '<config:config-item config:name="CursorPositionY" '
        'config:type="int">87</config:config-item>'
        '<config:config-item config:name="PositionBottom" '
        'config:type="int">337</config:config-item>'
        '<config:config-item config:name="VerticalSplitPosition" '
        'config:type="int">1</config:config-item>'
        "</office:document-settings>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("mimetype", "application/vnd.oasis.opendocument.spreadsheet")
        z.writestr("settings.xml", body)
        z.writestr("content.xml",
                   '<office:document-content xmlns:office="urn:oasis:names:tc:'
                   'opendocument:xmlns:office:1.0"/>')
    units, state = M.extract_units(buf.getvalue(), ".ods")
    with zipfile.ZipFile(io.BytesIO(M.rebuild(state, {}, units))) as z:
        got = z.read("settings.xml").decode("utf-8")
    assert 'config:name="CursorPositionY" config:type="int">0<' in got
    assert 'config:name="PositionBottom" config:type="int">0<' in got
    # 分割 / 凍結的位置不是捲動位置，不可以一起歸零
    assert 'config:name="VerticalSplitPosition" config:type="int">1<' in got
