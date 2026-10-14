"""事務機取件：型號辨識。

**目前沒有任何型號可以得到「已支援」** —— 規格與使用者規定實機驗證通過之前不可以標支援；
首批四款要「系列＋型號」都對上（ApeosPort C5570 ≠ Apeos C5570）。
頁面內容是**自己編的**（我們沒有實機頁面）；這裡測的是判斷規則，不是宣稱看得懂真機。
"""
from __future__ import annotations

import pytest

from app.core import mfp_profiles as mp


def _page(title="", text="", headers=(), pw=0):
    return {"scheme": "https", "port": 443, "path": "/", "title": title,
            "text_full_for_detection": text, "headers": list(headers), "password_inputs": pw}


@pytest.mark.parametrize("title,family,model", [
    ("Apeos C5571 - Internet Services", "Apeos", "C5571"),
    ("FUJIFILM Apeos C5570", "Apeos", "C5570"),
    ("ApeosPort C4570", "ApeosPort", "C4570"),
    ("ApeosPort-V C6680 CentreWare Internet Services", "ApeosPort-V", "C6680"),
])
def test_first_batch_is_recognised_but_never_supported(title, family, model):
    r = mp.detect([_page(title=title)])
    assert (r["family"], r["model"]) == (family, model)
    assert r["status"] == "first_batch_unverified", "未經實機驗證不可以標已支援"
    assert r["vendor"] == "FUJIFILM" and r["evidence"]
    # 依據用代碼（畫面再翻譯），不夾中文標籤
    assert {e["what"] for e in r["evidence"]} <= {"vendor", "model", "ui", "firmware"}
    assert all(e["field"] in ("title", "text", "header") for e in r["evidence"])


def test_series_matters_not_just_the_number():
    r = mp.detect([_page(title="ApeosPort C5570")])
    assert (r["family"], r["model"]) == ("ApeosPort", "C5570")
    assert r["status"] == "same_vendor_unverified", "ApeosPort C5570 不是首批的 Apeos C5570"


def test_generation_suffix_is_kept():
    r = mp.detect([_page(title="ApeosPort-VI C3371")])
    assert r["family"] == "ApeosPort-VI" and r["status"] == "same_vendor_unverified"


def test_nothing_is_supported_until_a_profile_is_verified():
    assert mp.VERIFIED_PROFILES == frozenset(), \
        "加進 VERIFIED_PROFILES 之前要有實機取件測試報告（docs-share/mfp-fetch/）"


def test_fuji_xerox_is_not_mistaken_for_xerox():
    r = mp.detect([_page(text="Fuji Xerox DocuCentre-VI C3371")])
    assert r["vendor"] == "FUJIFILM" and r["status"] == "same_vendor_unverified"


@pytest.mark.parametrize("text,vendor", [
    ("Canon imageRUNNER ADVANCE Remote UI", "Canon"),
    ("RICOH Web Image Monitor", "Ricoh"),
    ("KONICA MINOLTA bizhub", "Konica Minolta"),
    ("Xerox VersaLink C405", "Xerox"),
])
def test_other_vendors_are_marked_unsupported_with_a_reason(text, vendor):
    r = mp.detect([_page(text=text)])
    assert r["vendor"] == vendor and r["status"] == "unsupported_vendor"
    assert "FUJIFILM" in r["message"]


def test_a_login_page_says_login_is_needed():
    r = mp.detect([_page(title="Login", pw=1)])
    assert r["status"] == "login_required"


def test_nothing_recognisable_is_unknown_not_unsupported():
    r = mp.detect([_page(title="Welcome", text="Hello")])
    assert r["status"] == "unknown"


def test_no_pages_means_unreachable():
    assert mp.detect([])["status"] == "unreachable"


def test_firmware_is_picked_up_when_labelled():
    r = mp.detect([_page(title="Apeos C5571", text="Firmware Version: 72.18.5")])
    assert r["firmware"] == "72.18.5"


def test_every_status_has_a_message():
    for k, v in mp.STATUS_MESSAGES.items():
        assert v and isinstance(v, str), k
