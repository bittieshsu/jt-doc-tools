"""LDAP 查詢的屬性清單不可以夾帶 AD 專屬屬性。

## 由來（2026-08-11 ~ 08-15 正式機故障）

使用者登入時畫面顯示「目前無法連線到認證伺服器」——看起來像網路不通，
實際上 LDAP 伺服器好好的（ping 通、port 開），是**我們的查詢自己被拒絕了**：

    LDAPAttributeError: invalid attribute type in attribute list: objectSid

v1.14.16 加入 Active Directory 支援時，在使用者查詢的屬性清單裡加了
`objectSid` / `primaryGroupID`（主要群組）與 `userAccountControl` /
`msDS-UserPasswordExpiryTimeComputed`（帳號狀態）。當時程式裡的註解寫著：

> OpenLDAP 沒有這兩個屬性，要不到就是空的，不影響。

**那個假設是錯的。** OpenLDAP / UCS 收到不認得的屬性名，會直接拒絕**整個
查詢**，不是「該欄位回空」。於是連登入用的使用者查詢都失敗，目錄同步的
使用者列舉也一起失敗。

## 為什麼躲了四天沒被發現

* 錯誤訊息長得像網路問題，第一直覺會去查防火牆與 DNS
* 已登入的人有 30 天的 session，不會馬上受影響
* **沒有任何測試在守這件事** —— 這份檔案就是補上那道防線

## 判準

AD 專屬屬性只能在 `backend == "ad"` 時出現在屬性清單裡。
"""
from __future__ import annotations

import ast
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent

#: 這些屬性只有 Active Directory 有；OpenLDAP / UCS / FreeIPA 收到會拒絕整個查詢。
AD_ONLY = {
    "objectSid",
    "primaryGroupID",
    "userAccountControl",
    "msDS-UserPasswordExpiryTimeComputed",
}


def test_ad_only_attributes_are_gated_on_backend():
    """`_ad_only_attributes()` 只在 backend 是 AD 時回傳東西。"""
    from app.core import auth_ldap, auth_settings

    real = auth_settings.get

    for backend, expect_any in (("ad", True), ("ldap", False),
                                ("local", False), ("off", False)):
        auth_settings.get = lambda b=backend: {"backend": b}
        try:
            got = auth_ldap._ad_only_attributes()
        finally:
            auth_settings.get = real
        assert bool(got) is expect_any, (
            f"backend={backend} 時回了 {got} —— "
            "非 AD 的目錄會因為認不得這些屬性而拒絕整個查詢")
        if expect_any:
            assert set(got) == AD_ONLY, got


def test_reading_settings_failure_does_not_request_ad_attributes():
    """讀不到設定時要保守 —— 當作不是 AD。

    這條很重要：如果讀設定失敗時回傳 AD 屬性，那麼「設定檔有問題」會連帶
    讓所有 OpenLDAP 使用者登不進去，兩個問題疊在一起更難查。
    """
    from app.core import auth_ldap, auth_settings

    real = auth_settings.get

    def boom():
        raise RuntimeError("設定讀不到")

    auth_settings.get = boom
    try:
        assert auth_ldap._ad_only_attributes() == []
    finally:
        auth_settings.get = real


def test_no_hardcoded_ad_attributes_in_search_calls():
    """靜態把關：`attributes=[...]` 裡不可以寫死 AD 專屬屬性。

    只測 `_ad_only_attributes()` 不夠 —— 下一個人可能又直接在某個新的
    `search(...)` 呼叫裡塞一個 `objectSid`，那支查詢就會在 OpenLDAP 上
    整個掛掉，而單元測試完全沒感覺。
    """
    src = (ROOT / "app" / "core" / "auth_ldap.py").read_text(encoding="utf-8")
    tree = ast.parse(src)

    bad = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for kw in node.keywords:
            if kw.arg != "attributes":
                continue
            for el in ast.walk(kw.value):
                if (isinstance(el, ast.Constant) and isinstance(el.value, str)
                        and el.value in AD_ONLY):
                    bad.append(f"第 {el.lineno} 行寫死了 {el.value!r}")

    assert not bad, (
        "查詢的屬性清單裡寫死了 AD 專屬屬性 —— OpenLDAP / UCS 會拒絕**整個**"
        "查詢（不是該欄位回空），使用者會看到「無法連線到認證伺服器」：\n  "
        + "\n  ".join(bad)
        + "\n請改用 `*_ad_only_attributes()`。")


def test_the_wrong_assumption_is_documented():
    """把「為什麼不能這樣寫」留在程式裡。

    這個雷的成因是一句寫錯的註解（「要不到就是空的，不影響」）。
    註解被相信了將近兩個月，所以正確的說明必須留在原地，不能只寫在測試裡。
    """
    src = (ROOT / "app" / "core" / "auth_ldap.py").read_text(encoding="utf-8")
    assert "拒絕" in src and "整個" in src, (
        "`auth_ldap.py` 裡要留下「OpenLDAP 會拒絕整個查詢」的說明，"
        "否則下一個人會重蹈覆轍")
