"""用印區的排除條件：**標籤才算，說明句不算**。

`detect_fields` 最後會把「用印 / 簽章區」以下的欄位整批排除 —— 那些格子是要蓋
實體章或手寫簽名的，不該自動填。判斷方式是找頁面上第一個提到
`公司章 / 簽章 / 蓋章 / 印鑑 / 用印…` 的文字，然後**丟掉那一行以下的全部欄位**。

問題是很多表單會在**欄位上方的填表說明**裡提到蓋章，例如
「(四).必須蓋上　貴寶號及負責人印鑑章，以示同意。」——
那句話一旦被當成用印區的起點，**整張表的欄位全部被排除**，
畫面上就是「一欄都沒抓到」，而且沒有任何錯誤訊息。

判準是標籤與句子的本質差異：用印區的標籤**短、而且沒有句讀**。
"""
from __future__ import annotations

import io

import fitz
import pytest

from app.core import pdf_form_detect as D
from app.core.font_catalog import best_cjk_path


def _font() -> bytes:
    import fontTools.ttLib as ttlib
    picked = best_cjk_path("sans", "traditional")
    if not picked:
        pytest.skip("這台機器沒有中文字型")
    path, idx = picked
    face = (ttlib.TTCollection(str(path))[idx] if str(path).lower().endswith(".ttc")
            else ttlib.TTFont(str(path)))
    buf = io.BytesIO()
    face.save(buf)
    return buf.getvalue()


def _form(tmp_path, note: str | None, seal_label: str | None):
    """做一份最小表單：（可選）說明句 → 欄位 → （可選）用印格。"""
    doc = fitz.open()
    page = doc.new_page()
    page.insert_font(fontname="cjk", fontbuffer=_font())
    if note:
        page.insert_text((40, 120), note, fontname="cjk", fontsize=10)
    # 欄位列（標籤在左、值的空格在右，用線框出來）
    for i, label in enumerate(["統一編號", "公司全名", "開戶銀行"]):
        y = 200 + i * 40
        page.insert_text((45, y + 14), label, fontname="cjk", fontsize=11)
        page.draw_rect(fitz.Rect(40, y, 520, y + 22))
        page.draw_line(fitz.Point(130, y), fitz.Point(130, y + 22))
    if seal_label:
        page.insert_text((45, 420), seal_label, fontname="cjk", fontsize=11)
        page.draw_rect(fitz.Rect(40, 400, 300, 470))
    out = tmp_path / "form.pdf"
    out.write_bytes(doc.tobytes())
    doc.close()
    return out


def test_instruction_sentence_does_not_kill_the_whole_form(tmp_path):
    """說明句提到印鑑 → 欄位**照樣要偵測得到**。"""
    pdf = _form(tmp_path, "(四).必須蓋上　貴寶號及負責人印鑑章，以示同意。", None)
    fields, _ = D.detect_fields(pdf)
    keys = {f.profile_key for f in fields}
    assert keys, "說明句裡提到印鑑就讓整份表單一欄都偵測不到"
    assert "tax_id" in keys and "company_name" in keys, f"少了欄位：{sorted(keys)}"


def test_form_without_any_note_is_the_same(tmp_path):
    """對照組：沒有說明句時本來就抓得到 —— 兩者要一致。"""
    (tmp_path / "a").mkdir(parents=True, exist_ok=True)
    (tmp_path / "b").mkdir(parents=True, exist_ok=True)
    with_note = _form(tmp_path / "a", "(四).必須蓋上　貴寶號及負責人印鑑章，以示同意。", None)
    without = _form(tmp_path / "b", None, None)
    a = {f.profile_key for f in D.detect_fields(with_note)[0]}
    b = {f.profile_key for f in D.detect_fields(without)[0]}
    assert a == b, f"有沒有那句說明會改變偵測結果：{sorted(a)} vs {sorted(b)}"


def test_real_seal_label_still_excludes_everything_below(tmp_path):
    """用印區的**標籤**還是要照樣擋 —— 這條防線不能被修掉。"""
    doc = fitz.open()
    page = doc.new_page()
    page.insert_font(fontname="cjk", fontbuffer=_font())
    page.insert_text((45, 214), "統一編號", fontname="cjk", fontsize=11)
    page.draw_rect(fitz.Rect(40, 200, 520, 222))
    page.draw_line(fitz.Point(130, 200), fitz.Point(130, 222))
    # 用印區在下面，裡面也有一個看起來像欄位的標籤
    page.insert_text((45, 320), "負責人印鑑", fontname="cjk", fontsize=11)
    page.insert_text((45, 364), "開戶銀行", fontname="cjk", fontsize=11)
    page.draw_rect(fitz.Rect(40, 350, 520, 372))
    page.draw_line(fitz.Point(130, 350), fitz.Point(130, 372))
    pdf = tmp_path / "seal.pdf"
    pdf.write_bytes(doc.tobytes())
    doc.close()

    keys = {f.profile_key for f in D.detect_fields(pdf)[0]}
    assert "tax_id" in keys, "用印區以上的欄位被誤刪了"
    assert "bank_name" not in keys, "用印區以下的欄位沒有被排除"


@pytest.mark.parametrize("text,is_marker", [
    ("負責人印鑑", True),
    ("公司章", True),
    ("簽章：", True),
    ("(四).必須蓋上　貴寶號及負責人印鑑章，以示同意。", False),
    ("本同意書請蓋妥公司大小章後寄回，謝謝。", False),
    ("如需用印，請於下方空白處蓋章、並註明日期。", False),
])
def test_marker_classification(text: str, is_marker: bool):
    assert D._is_seal_marker(text) is is_marker, text
