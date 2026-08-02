"""把「同上」「同登記地址」展開成實際內容。

## 由來

廠商資料表很常出現「發票地址：同上」。使用者把公司資料存進本站時也會照抄那兩個字。
結果填出來的表單上就印著「同上」—— 而**新表單的版面跟原本那張不一樣**，上面那一格
根本不是同一個欄位，收件方看到的是一個指不到任何東西的「同上」。

## 這一份主要在守「不可以展開」的那一半

會展開才是簡單的部分。真正的風險是**把 A 欄位的值悄悄填進 B 欄位** ——
那種錯誤在畫面上看起來完全正常（欄位有值、格式也對），只有收件方會發現地址是錯的。
所以：

* 裸的「同上」只在**少數約定俗成的配對**上展開，其他一律保留字面。
* 中文地址不可以被填進英文地址欄。
* 展不開時保留使用者原本打的字，**絕不填空**。
* 展開過的一定要回報，畫面上要看得到。
"""
from __future__ import annotations

import pytest

from app.core import same_as_ref as sar


BASE = {
    "company_name": "節省股份有限公司",
    "english_name": "Save Co., Ltd.",
    "address": "臺北市中正區忠孝東路一段 1 號",
    "english_address": "1 Zhongxiao E. Rd., Taipei",
}


def _resolve(key, value, extra=None):
    prof = dict(BASE)
    prof.update(extra or {})
    prof[key] = value
    return sar.resolve_one(key, value, prof)


# ------------------------------------------------------------ 會展開的

@pytest.mark.parametrize("word", ["同上", "同前", "如上", "〃", "同上述", "ditto"])
def test_bare_reference_expands_on_known_pairs(word):
    """裸指涉 + 約定俗成的配對 → 展開。"""
    got, src = _resolve("invoice_address", word)
    assert got == BASE["address"]
    assert src == "address"


def test_named_reference_resolves_by_label():
    """「同公司地址」指名得很清楚 —— 用標籤同義詞查出正式 key。"""
    got, src = _resolve("factory_address", "同公司地址")
    assert got == BASE["address"] and src == "address"


@pytest.mark.parametrize("value", ["同登記地址", "同 營業地址", "與公司地址相同",
                                   "（同公司地址）", "如公司地址"])
def test_named_reference_variants(value):
    got, src = _resolve("factory_address", value)
    assert src == "address", f"{value} 沒有展開"
    assert got == BASE["address"]


def test_account_name_ditto_means_company_name():
    got, src = _resolve("bank_account_name", "同上")
    assert got == BASE["company_name"] and src == "company_name"


# ------------------------------------------------------------ 不可以展開的

def test_bare_reference_is_kept_when_there_is_no_known_pair():
    """「電話：同上」指的是版面上一格，不是固定的某個欄位 —— 保留字面。

    亂猜一個來源填進去，畫面上看起來完全正常，只有收件方會發現號碼是錯的。
    """
    got, src = _resolve("phone", "同上")
    assert got == "同上" and src is None


def test_chinese_address_never_lands_in_the_english_field():
    """中文地址填進英文地址欄是明顯的錯 —— 這一組刻意不放進配對表。"""
    got, src = _resolve("english_address", "同上")
    assert got == "同上" and src is None


def test_named_reference_to_an_unknown_label_is_kept():
    got, src = _resolve("factory_address", "同倉庫地址")
    assert got == "同倉庫地址" and src is None


def test_never_returns_empty(monkeypatch):
    """展不開時保留原字面 —— 換成空白是把使用者打的字刪掉。"""
    got, src = _resolve("factory_address", "同某個不存在的欄位")
    assert got.strip() != ""


def test_target_empty_keeps_the_literal():
    """指到的欄位本身是空的 → 不展開（不要用空字串蓋掉「同上」）。"""
    prof = dict(BASE, address="", invoice_address="同上")
    got, src = sar.resolve_one("invoice_address", "同上", prof)
    assert got == "同上" and src is None


def test_chain_does_not_loop():
    """A 指向 B、B 又是指涉 → 不再往下追，避免繞圈。"""
    prof = dict(BASE, address="同上", invoice_address="同上")
    got, src = sar.resolve_one("invoice_address", "同上", prof)
    assert got == "同上" and src is None


def test_self_reference_is_ignored():
    prof = dict(BASE, factory_address="同工廠地址")
    got, src = sar.resolve_one("factory_address", "同工廠地址", prof)
    assert src != "factory_address"


@pytest.mark.parametrize("value", [
    "臺北市中正區忠孝東路一段 1 號",     # 真的地址
    "同心圓實業有限公司",                 # 「同」開頭但是公司名
    "同泰路 12 號",                       # 「同」開頭的路名
    "",
    "   ",
])
def test_real_values_are_not_treated_as_references(value):
    """**誤判成指涉會直接把真的內容換掉** —— 這比不展開嚴重得多。"""
    got, src = _resolve("address", value)
    assert got == value and src is None


def test_long_text_is_never_a_reference():
    long_addr = "同安街 100 號 5 樓之 3（近捷運古亭站 3 號出口，請由側門進入）"
    assert not sar.is_reference(long_addr)


# ------------------------------------------------------------ 整份展開 + 回報

def test_resolve_profile_reports_what_it_changed():
    prof = dict(BASE, invoice_address="同上", phone="同上")
    out, expanded = sar.resolve_profile(prof)
    assert out["invoice_address"] == BASE["address"]
    assert out["phone"] == "同上", "不該展開的被展開了"
    assert len(expanded) == 1
    e = expanded[0]
    assert e == {"key": "invoice_address", "from": "address",
                 "original": "同上", "value": BASE["address"]}


def test_resolve_profile_does_not_mutate_the_input():
    prof = dict(BASE, invoice_address="同上")
    snapshot = dict(prof)
    sar.resolve_profile(prof)
    assert prof == snapshot, "改到了傳進來的 dict"


def test_nothing_to_do_reports_nothing():
    out, expanded = sar.resolve_profile(dict(BASE))
    assert expanded == []
    assert out == BASE


# ------------------------------------------------------------ 接線

def test_fill_pdf_expands_before_the_template_branch():
    """套用範本的那條路徑是 early-return —— 展開一定要在它**之前**做，
    否則有範本的表單（也就是回頭客最常用的那些）完全吃不到這個功能。"""
    import inspect

    from app.tools.pdf_fill import service
    src = inspect.getsource(service.fill_pdf)
    assert src.index("resolve_profile") < src.index("_fill_from_template")


def test_both_return_paths_carry_the_report():
    import inspect

    from app.tools.pdf_fill import service
    src = inspect.getsource(service)
    assert src.count("expanded_refs") >= 4, "有一條回傳路徑沒帶上展開明細"


def test_ui_shows_the_expansion():
    """悄悄換掉使用者打的字是這個功能最可能造成的傷害 —— 畫面一定要講。"""
    from pathlib import Path

    from app.tools.pdf_fill import service
    tpl = (Path(service.__file__).resolve().parent / "templates" /
           "pdf_fill.html").read_text(encoding="utf-8")
    assert "expanded_refs" in tpl
    assert "已展開成實際內容" in tpl
    i = tpl.index("expanded_refs")
    assert "escapeHTML" in tpl[i:i + 600], "展開內容沒有跳脫就塞進 HTML"


def test_company_name_starting_with_tong_still_expands():
    """公司名叫「同心圓實業…」的客戶，`戶名：同上` 一樣要展得開。

    第一版的 `is_reference` 只看「同」開頭就回 True，於是迴圈防護把這個公司名
    誤判成「來源本身也是指涉」→ 拒絕展開。安全方向的錯，但使用者會覺得功能壞了。
    """
    prof = {"company_name": "同心圓實業有限公司", "bank_account_name": "同上"}
    got, src = sar.resolve_one("bank_account_name", "同上", prof)
    assert got == "同心圓實業有限公司" and src == "company_name"


@pytest.mark.parametrize("value,expect", [
    ("同心圓實業有限公司", False),   # 公司名
    ("同泰路 12 號", False),          # 路名
    ("同上", True),
    ("同公司地址", True),             # 對得上某個欄位標籤
    ("同倉庫地址", False),            # 對不上任何標籤 → 不算指涉
])
def test_is_reference_requires_a_resolvable_target(value, expect):
    assert sar.is_reference(value) is expect


# ------------------------------------------------------------ 端到端

def _make_form(path):
    """造一張含常見標籤的空白表單。

    **不用真實廠商表單**：那些檔案含公司名 / 統編 / 地址，不可以進版本庫。
    """
    import fitz
    FONT = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
    d = fitz.open()
    pg = d.new_page(width=595, height=842)
    try:
        pg.insert_font(fontname="F0", fontfile=FONT)
    except Exception:                      # pragma: no cover — 環境沒這支字型
        pytest.skip("測試機沒有 Noto CJK 字型")
    y = 100
    for lab in ("公司全名：", "營業地址：", "發票地址：", "工廠地址：", "銀行戶名："):
        pg.insert_text((60, y), lab, fontname="F0", fontsize=12)
        pg.draw_line(fitz.Point(160, y + 3), fitz.Point(520, y + 3))
        y += 60
    d.save(str(path))
    d.close()


def test_end_to_end_no_ditto_words_reach_the_pdf(tmp_path):
    """**這是這個功能真正要達成的事**：產出的 PDF 裡不可以再出現「同上」。

    單元測試只驗得到解析函式；這一條真的跑 `fill_pdf`、真的把文字抽回來看。
    關掉展開時，同一張表會印出「同上」與「同營業地址」—— 實測確認過。
    """
    import fitz
    src = tmp_path / "form.pdf"
    dst = tmp_path / "out.pdf"
    _make_form(src)
    from app.tools.pdf_fill import service
    rep = service.fill_pdf(src, dst, {
        "company_name": "節省股份有限公司",
        "address": "臺北市中正區忠孝東路一段 1 號",
        "invoice_address": "同上",
        "factory_address": "同營業地址",
        "bank_account_name": "同上",
    })
    # 抽回來的文字要正規化兩件事才比得了：
    #   ① 疊字層寫的是不斷行空白（\xa0）
    #   ② 內嵌 CJK 字型會把部分字對到**相容表意字**（北 U+F963 而不是 U+5317），
    #      NFC 正規化會還原。這是字型 ToUnicode 的性質，不是填錯字。
    import unicodedata
    raw = fitz.open(str(dst))[0].get_text()
    text = unicodedata.normalize("NFC", raw).replace("\xa0", " ")
    assert "同上" not in text, "產出的 PDF 上還印著「同上」"
    assert "同營業地址" not in text
    assert text.count("臺北市中正區忠孝東路一段 1 號") == 3, \
        f"營業 / 發票 / 工廠三個地址欄位應該都有實際地址，實際抽到：{text!r}"
    assert len(rep.expanded_refs) == 3
