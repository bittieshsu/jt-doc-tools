"""兩種讓欄位「有偵測到卻填不進去」的版型。

都是同一類無聲失敗：畫面上那一欄就是空的，看不出是版型沒支援還是資料沒填。

1. **逐格分寫的值區**（統一編號 / 帳號畫成一格一字的小方框）。
   「找標籤右邊的格子」原本會跳過窄欄去找第一個夠寬的格子 —— 一整排 30pt
   的小格全被跳過，落到隔壁欄的**標籤**上，再被「值不可以蓋到別的標籤」
   那條防線丟掉，結果那一欄整個沒有位置。

2. **值格裡印著子標籤**（`郵遞區號(        )`）。原本被當成「已經填好的
   資料」而整欄跳過；但也不能直接寫上去 —— 那樣會壓在印好的字上面
   （實測重疊 95pt）。**疊字比沒填更糟**，正解是把值的起點推到子標籤後面。
"""
from __future__ import annotations

import io

import fitz
import pytest

from app.core import pdf_form_detect as D
from app.core import pdf_layout as L
from app.core.font_catalog import best_cjk_path


def _fontbuf() -> bytes:
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


# ---------- 1. 逐格分寫 ----------

def test_run_of_equal_width_boxes_is_one_value_area():
    """一整排等寬相鄰的小格 = 值區，不是要跳過的窄欄。"""
    label_cell = (30.0, 100.0, 110.0, 125.0)
    boxes = [(110.0 + i * 30, 100.0, 140.0 + i * 30, 125.0) for i in range(8)]
    neighbour_label = (350.0, 100.0, 410.0, 125.0)   # 隔壁欄的標籤格
    cells = [label_cell, *boxes, neighbour_label]
    got = L.find_cell_right_of(label_cell, cells)
    assert got is not None
    assert got[0] == pytest.approx(110.0), "沒有從第一個小格開始"
    assert got[2] == pytest.approx(350.0), f"沒有涵蓋整排小格，拿到 {got}"


def test_a_single_narrow_marker_column_is_still_skipped():
    """只有一兩個窄欄時維持原本行為（那是標記欄，不是值區）。"""
    label_cell = (30.0, 100.0, 110.0, 125.0)
    marker = (110.0, 100.0, 122.0, 125.0)
    value = (122.0, 100.0, 400.0, 125.0)
    got = L.find_cell_right_of(label_cell, [label_cell, marker, value])
    assert got == value, f"窄的標記欄不該被當成值區：{got}"


def test_boxed_digit_form_gets_a_slot_for_the_tax_id(tmp_path):
    doc = fitz.open()
    page = doc.new_page()
    page.insert_font(fontname="cjk", fontbuffer=_fontbuf())
    page.insert_text((45, 118), "統一編號", fontname="cjk", fontsize=11)
    page.draw_rect(fitz.Rect(40, 100, 115, 125))
    for i in range(8):
        page.draw_rect(fitz.Rect(115 + i * 30, 100, 145 + i * 30, 125))
    page.insert_text((360, 118), "負責人", fontname="cjk", fontsize=11)
    page.draw_rect(fitz.Rect(355, 100, 415, 125))
    page.draw_rect(fitz.Rect(415, 100, 560, 125))
    pdf = tmp_path / "boxed.pdf"
    pdf.write_bytes(doc.tobytes())
    doc.close()

    fields = {f.profile_key: f for f in D.detect_fields(pdf)[0]}
    assert "tax_id" in fields, "統一編號沒被偵測到"
    slot = fields["tax_id"].value_slot
    assert slot is not None, "統一編號沒有值的位置（小格被當成窄欄跳過了）"
    assert slot[2] <= 356, f"值的位置延伸到隔壁欄的標籤上：{slot}"


# ---------- 2. 值格裡的子標籤 ----------

def _addr_form(tmp_path):
    doc = fitz.open()
    page = doc.new_page()
    page.insert_font(fontname="cjk", fontbuffer=_fontbuf())
    page.insert_text((45, 118), "通訊地址", fontname="cjk", fontsize=11)
    page.draw_rect(fitz.Rect(40, 100, 540, 125))
    page.draw_line(fitz.Point(115, 100), fitz.Point(115, 125))
    # 值格裡印著子標籤
    page.insert_text((122, 118), "郵遞區號(        )", fontname="cjk", fontsize=10)
    pdf = tmp_path / "addr.pdf"
    pdf.write_bytes(doc.tobytes())
    doc.close()
    return pdf


def test_sub_label_is_not_treated_as_pre_filled_data(tmp_path):
    fields = {f.profile_key: f for f in D.detect_fields(_addr_form(tmp_path))[0]}
    assert "address" in fields
    assert not fields["address"].slot_occupied, \
        "`郵遞區號(  )` 被當成已經填好的資料，整欄會被跳過"


def test_value_starts_after_the_sub_label(tmp_path):
    """不可以從格子左緣開始寫 —— 那會壓在印好的子標籤上（疊字比沒填更糟）。"""
    pdf = _addr_form(tmp_path)
    fields = {f.profile_key: f for f in D.detect_fields(pdf)[0]}
    slot = fields["address"].value_slot
    assert slot is not None
    with fitz.open(pdf) as doc:
        page = doc[0]
        subs = [s["bbox"] for b in page.get_text("dict")["blocks"] if b.get("type") == 0
                for l in b["lines"] for s in l["spans"] if "郵遞區號" in s["text"]]
    assert subs, "測試素材本身壞了：找不到子標籤"
    for bx0, by0, bx1, by1 in subs:
        overlap_x = min(slot[2], bx1) - max(slot[0], bx0)
        overlap_y = min(slot[3], by1) - max(slot[1], by0)
        assert not (overlap_x > 2 and overlap_y > 2), \
            f"值的位置壓在子標籤上：slot={slot} 子標籤={(bx0, by0, bx1, by1)}"


# ---------- 3. 括號本身就是填寫的位置 ----------

def test_parens_after_a_label_are_that_label_value_slot(tmp_path):
    """`郵遞區號(      )` —— 括號中間就是郵遞區號要寫的地方。

    原本這種 span 只會被當成「值格裡的子標籤」跳過，於是那個欄位**永遠不會
    被填**（使用者回報：郵遞區號沒抓到）。
    """
    doc = fitz.open()
    page = doc.new_page()
    page.insert_font(fontname="cjk", fontbuffer=_fontbuf())
    page.insert_text((45, 118), "通訊地址", fontname="cjk", fontsize=11)
    page.draw_rect(fitz.Rect(40, 100, 540, 125))
    page.draw_line(fitz.Point(115, 100), fitz.Point(115, 125))
    page.insert_text((122, 118), "郵遞區號(        )", fontname="cjk", fontsize=10)
    pdf = tmp_path / "zip.pdf"
    pdf.write_bytes(doc.tobytes())
    doc.close()

    fields = [f for f in D.detect_fields(pdf)[0] if f.profile_key == "zip_code"]
    assert fields, "括號沒有被當成郵遞區號的填寫位置"
    slot = fields[0].value_slot
    assert slot is not None and slot[2] - slot[0] >= 6

    # 位置要**真的落在括號中間**，不可以壓在「郵遞區號(」上面。
    # 等寬估算在中文與半形括號混排時會偏左一大截，所以這裡要逐字座標。
    with fitz.open(pdf) as d2:
        chars = [c for b in d2[0].get_text("rawdict")["blocks"] if b.get("type") == 0
                 for l in b["lines"] for s in l["spans"] for c in s["chars"]]
    open_paren = next(c for c in chars if c["c"] in "(（")
    close_paren = next(c for c in chars if c["c"] in ")）")
    assert slot[0] >= open_paren["bbox"][2] - 1, \
        f"值的位置壓在「郵遞區號(」上面：slot={slot}"
    assert slot[2] <= close_paren["bbox"][0] + 1, \
        f"值的位置蓋到右括號：slot={slot}"


@pytest.mark.parametrize("label,key", [
    ("開戶全名", "bank_account_name"),
    ("開戶名稱", "bank_account_name"),
    # 「解放行代號」是實際表單上的誤植（款→放）；表單印好了不會改。
    ("解放行代號", "bank_branch_code"),
    ("解款行代號", "bank_branch_code"),
])
def test_labels_map_to_the_right_field(label: str, key: str):
    idx = D._build_synonym_index(D._active_label_map())
    got = idx.get(D._normalize(label))
    assert got and key in got, f"{label} 沒有對應到 {key}（拿到 {got}）"
