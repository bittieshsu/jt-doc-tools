"""表單自動填寫的定位規則。

## 為什麼這支工具的測試要特別嚴

填錯位置的後果是**使用者不會發現，收件方才會發現** —— 畫面上兩串字疊在一起
看起來只是有點糊，寄出去之後才知道欄位是錯的。所以這裡的判準不是「有沒有
填」，是「填在對的地方」。

## 這一輪（v1.14.31 對抗式驗證）抓到的四件事

1. **後綴標籤會讓整個偵測崩潰**：`slot` 被設成 `None` 之後下一行就 `slot[0]`，
   一列格子裡「分行」被框成 60~70pt 的小格就會踩到 —— 那是很常見的版型，
   使用者拿到的是 500，整份表一欄都填不出來。
2. **「分行：」被當成後綴標籤**：`is_suffix_label` 把冒號一起 strip 掉，於是
   標準的前置標籤被判成後置，值被瞄到左邊隔壁欄的空白，再因為位置衝突整欄
   被靜靜丟掉。使用者看到「分行沒填」。
3. **值帶不同單位詞時整欄無聲丟掉**：表上只有一個「分行」欄、使用者的值是
   「中山郵局」→ 完全不填而且沒有任何提示。
4. **退路把偵測端否決的位置又拿回來用**：偵測端判定「這個位置會蓋掉別的
   標籤」而把 `value_slot` 設成 `None`（註解寫著「留白至少看得出這裡沒填，
   比疊字好」），但填寫端用 `value_anchor` 重建了同一個位置 —— 而那個 anchor
   正是用剛被否決的 slot 算出來的。實測真實表單上 29 處墨水級疊字，改掉之後
   剩 9 處，代價是少填 20 欄，而**每一份少填的表單都同時減少了疊字**（逐份
   A/B，零例外）。
"""
from __future__ import annotations

import pathlib
import tempfile

import pytest


def _font(page):
    """拿一支畫得出中文的字型，取不到就跳過整組測試。"""
    from app.core import font_catalog

    best = font_catalog.best_cjk_path("sans", "traditional")
    if not best:
        pytest.skip("這台機器沒有安裝 CJK 字型")
    import fitz

    ff, buf = font_catalog.embeddable_font(
        best[0], best[1], text="公司全名統一編號負責人受款銀行分行支局戶名帳號聯絡人")
    page.insert_font(fontname="cjk", fontfile=ff, fontbuffer=buf)
    return "cjk"


def _box(pg, x0, y0, x1, y1):
    import fitz

    pg.draw_rect(fitz.Rect(x0, y0, x1, y1), color=(0, 0, 0), width=0.8)


def _vline(pg, x, y0, y1):
    pg.draw_line((x, y0), (x, y1), color=(0, 0, 0), width=0.8)


PROFILE = {
    "company_name": "測試工具箱有限公司", "tax_id": "12345678",
    "owner": "王小明", "bank_name": "國泰世華商業銀行",
    "bank_branch": "崇德分行", "bank_account_name": "測試工具箱有限公司",
    "bank_account_no": "012345678901", "contact": "王小明",
}


def _run(build, profile=None):
    """造一份表 → 跑偵測與填寫 → 回 (偵測欄, 有填到的鍵, 實際墨水疊字數)。"""
    import fitz

    from app.core import pdf_form_detect
    from app.tools.pdf_fill import service

    with tempfile.TemporaryDirectory() as td:
        src = pathlib.Path(td) / "in.pdf"
        d = fitz.open()
        pg = d.new_page(width=595, height=842)
        build(pg, _font(pg))
        d.save(str(src))
        d.close()

        det, _ = pdf_form_detect.detect_fields(src)
        out = pathlib.Path(td) / "out.pdf"
        rep = service.fill_pdf(src, out, dict(profile or PROFILE))
        filled = {pl.source_key for pl in (rep.placements or [])}

        with fitz.open(str(src)) as a:
            orig = {i: [(w[0], w[1], w[2], w[3], w[4]) for w in p.get_text("words")]
                    for i, p in enumerate(a)}
        with fitz.open(str(out)) as b:
            new = {i: [(w[0], w[1], w[2], w[3], w[4]) for w in p.get_text("words")]
                   for i, p in enumerate(b)}
        overlap = 0
        for pno, ws in new.items():
            olds = orig.get(pno, [])
            seen = {(round(w[0], 1), round(w[1], 1), w[4]) for w in olds}
            for w in ws:
                if (round(w[0], 1), round(w[1], 1), w[4]) in seen:
                    continue
                for (bx0, by0, bx1, by1, _t) in olds:
                    ox = min(w[2], bx1) - max(w[0], bx0)
                    oy = min(w[3], by1) - max(w[1], by0)
                    if ox > 1 and oy > 1 and ox * oy > (bx1 - bx0) * (by1 - by0) * 0.5:
                        overlap += 1
                        break
        return det, filled, overlap


# ---------------------------------------------------------------------------
# 1. 後綴標籤 + 窄格 → 不可以崩潰
# ---------------------------------------------------------------------------

def _narrow_suffix_form(pg, f):
    """「分行」被框成一個 60~70pt 的小格 —— 真實表單很常見的版型。"""
    pg.insert_text((60, 60), "廠商匯款資料表", fontname=f, fontsize=18)
    _box(pg, 60, 100, 535, 130)
    _vline(pg, 160, 100, 130)
    _vline(pg, 400, 100, 130)
    pg.insert_text((70, 120), "受款銀行", fontname=f, fontsize=11)
    pg.insert_text((462, 120), "分行", fontname=f, fontsize=11)
    _box(pg, 60, 130, 535, 160)
    _vline(pg, 160, 130, 160)
    pg.insert_text((70, 150), "銀行帳號", fontname=f, fontsize=11)


def test_narrow_suffix_label_does_not_crash():
    """`slot` 被設成 None 之後不可以馬上拿它下標。

    第一版在同一段程式裡先 `slot, slot_kind = None, None`，下一行就
    `anchor = (slot[0], baseline)` → `TypeError: 'NoneType' object is not
    subscriptable`，而且是在 `detect_fields` 裡，**整份表單一欄都偵測不出來**。
    """
    det, filled, _ = _run(_narrow_suffix_form)
    assert det, "偵測不應該回空的（崩潰時整份表都沒了）"


# ---------------------------------------------------------------------------
# 2. 「分行：」是前置標籤
# ---------------------------------------------------------------------------

def test_colon_suffix_is_a_prefix_label():
    """結尾有冒號的一律不算後綴標籤 —— 值在冒號**右邊**。"""
    from app.core import pdf_layout

    assert pdf_layout.is_suffix_label("分行") is True
    assert pdf_layout.is_suffix_label("（分行）") is True, "括號是排版修飾，仍是後綴"
    assert pdf_layout.is_suffix_label("分行：") is False, (
        "「分行：」是標準的前置標籤，值在冒號右邊")
    assert pdf_layout.is_suffix_label("分行:") is False
    assert pdf_layout.is_suffix_label("支局：") is False


def _colon_midrow_form(pg, f):
    """一列裡「銀行名稱：___ 分行：___」—— 兩個都是前置標籤。"""
    pg.insert_text((60, 60), "匯款資料", fontname=f, fontsize=18)
    _box(pg, 60, 100, 535, 130)
    pg.insert_text((70, 120), "銀行名稱：", fontname=f, fontsize=11)
    pg.draw_line((130, 126), (300, 126), color=(0, 0, 0), width=0.8)
    pg.insert_text((320, 120), "分行：", fontname=f, fontsize=11)
    pg.draw_line((355, 126), (525, 126), color=(0, 0, 0), width=0.8)
    _box(pg, 60, 130, 535, 160)
    _vline(pg, 160, 130, 160)
    pg.insert_text((70, 150), "銀行帳號", fontname=f, fontsize=11)


def test_colon_labelled_branch_is_still_filled():
    """「分行：」那一欄要填得到 —— 被判成後綴時它會整欄消失。"""
    _det, filled, _ov = _run(_colon_midrow_form)
    assert "bank_branch" in filled, "「分行：」欄沒填到（被誤判成後綴標籤）"


# ---------------------------------------------------------------------------
# 3. 值帶不同單位詞不可以無聲丟掉整欄
# ---------------------------------------------------------------------------

def _single_branch_form(pg, f):
    """表上只有一個「分行」欄。"""
    pg.insert_text((60, 60), "匯款資料", fontname=f, fontsize=18)
    _box(pg, 60, 100, 535, 130)
    _vline(pg, 160, 100, 130)
    pg.insert_text((70, 120), "受款銀行", fontname=f, fontsize=11)
    _box(pg, 60, 130, 535, 160)
    _vline(pg, 160, 130, 160)
    pg.insert_text((480, 150), "分行", fontname=f, fontsize=11)
    _box(pg, 60, 160, 535, 190)
    _vline(pg, 160, 160, 190)
    pg.insert_text((70, 180), "銀行帳號", fontname=f, fontsize=11)


def test_post_office_value_on_a_branch_only_form_is_still_filled():
    """表上只有「分行」欄、使用者的值叫「中山郵局」→ 仍然要填。

    「值帶著單位詞就只認同名的標籤」這條規則本身是對的（免得「崇德分行」被
    填進郵局那一列），但**只有在表上真的有那個單位的欄位時**才叫「放錯地方」。
    只有一個候選欄位時，那裡就是唯一的位置 —— 第一版無條件跳過，郵局帳戶的
    使用者去填一般匯款表就會看到那一欄空白而且沒有任何提示。
    """
    prof = dict(PROFILE, bank_branch="中山郵局")
    _det, filled, _ov = _run(_single_branch_form, prof)
    assert "bank_branch" in filled, "只有一個分行欄時不可以因為值叫「郵局」就不填"


# ---------------------------------------------------------------------------
# 4. 退路不可以把否決過的位置拿回來用
# ---------------------------------------------------------------------------

def test_fallback_slot_must_not_cover_printed_labels():
    """沒有精確位置時的退路，要先確認自己不會壓到印好的字。

    偵測端把 `value_slot` 設成 None 是**刻意的**（那個位置會蓋掉別的標籤），
    但填寫端用 `value_anchor` 重建了同一個位置。這裡直接測那個判斷函式。
    """
    from app.core.pdf_form_detect import DetectedField
    from app.tools.pdf_fill.service import _overlaps_printed_text

    def mk(key, rect):
        d = object.__new__(DetectedField)
        d.page, d.profile_key, d.label_rect = 0, key, rect
        return d

    me = mk("bank_name", (70.0, 110.0, 130.0, 125.0))
    other = mk("bank_branch", (300.0, 110.0, 360.0, 125.0))

    # 退路整個蓋過右邊那個標籤 → 不可以用
    assert _overlaps_printed_text((140.0, 110.0, 380.0, 125.0), me, [me, other])
    # 停在它前面 → 可以用
    assert not _overlaps_printed_text((140.0, 110.0, 290.0, 125.0), me, [me, other])
    # 蓋到**自己的**標籤也不行（實測真實樣本上有值完全蓋掉自己標籤的情況）
    assert _overlaps_printed_text((70.0, 110.0, 300.0, 125.0), me, [me, other])


def test_fallback_is_clamped_before_the_next_label():
    """退路的右緣要夾到同一列下一個標籤之前，而不是整個放棄。

    整個放棄會少填 25 欄；夾一下能救回 5 欄而疊字不變（實測）。
    """
    from app.core.pdf_form_detect import DetectedField
    from app.tools.pdf_fill.service import _clamp_before_next_label

    def mk(key, rect):
        d = object.__new__(DetectedField)
        d.page, d.profile_key, d.label_rect = 0, key, rect
        return d

    me = mk("bank_name", (70.0, 110.0, 130.0, 125.0))
    other = mk("bank_branch", (300.0, 110.0, 360.0, 125.0))

    got = _clamp_before_next_label((140.0, 110.0, 380.0, 125.0), me, [me, other])
    assert got is not None and got[2] == 298.0, got

    # 夾完太窄就別填了
    tight = mk("x", (150.0, 110.0, 200.0, 125.0))
    assert _clamp_before_next_label(
        (140.0, 110.0, 380.0, 125.0), me, [me, tight]) is None


def _adjacent_labels_form(pg, f):
    """兩個標籤靠很近、中間沒有格線。

    這種版型算不出精確的值框，於是走退路 —— 而退路是「從 anchor 往右畫
    240pt」，會一路蓋過右邊那個標籤。
    """
    pg.insert_text((60, 60), "匯款資料", fontname=f, fontsize=18)
    _box(pg, 60, 100, 535, 130)
    pg.insert_text((70, 120), "受款銀行", fontname=f, fontsize=11)
    pg.insert_text((150, 120), "分行", fontname=f, fontsize=11)
    _box(pg, 60, 130, 535, 160)
    _vline(pg, 160, 130, 160)
    pg.insert_text((70, 150), "銀行帳號", fontname=f, fontsize=11)


def test_fallback_never_writes_on_top_of_printed_text():
    """整合層的判準：**畫出來的字不可以壓到原本就印在紙上的字**。

    只測 `_overlaps_printed_text` 這個函式不夠 —— 把它從主流程拔掉之後
    那些單元測試照樣全綠（實測變異驗證沒抓到）。這裡跑完整的填寫流程，
    量的是**實際畫出來的字**。

    這個版型在沒有保護時會多填一欄，而那一欄正好壓在「分行」上面。
    """
    _det, _filled, overlap = _run(_adjacent_labels_form)
    assert overlap == 0, (
        f"有 {overlap} 個填入的值壓在印好的字上面 —— "
        "使用者看到的是兩串字糊在一起，收件方才會發現欄位是錯的")
