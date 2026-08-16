"""目錄查詢要能在 **AD / OpenLDAP / UCS** 三種結構上都跑得起來。

## 由來（2026-08-11 ~ 08-15 正式機故障）

登入時畫面顯示「目前無法連線到認證伺服器」，看起來像網路不通，實際上伺服器
好好的 —— 是我們的查詢自己被拒絕了：

    LDAPAttributeError: invalid attribute type in attribute list: objectSid

v1.14.16 加 AD 支援時，在屬性清單裡寫死了 `objectSid` / `primaryGroupID` /
`userAccountControl`，並在註解裡假設「OpenLDAP 沒有這些屬性，要不到就是空的，
不影響」。**那個假設是錯的**：OpenLDAP / UCS 收到不認得的屬性名會拒絕**整個
查詢**，不是該欄位回空。

## 為什麼既有的 22 支測試都沒抓到

它們全部直接呼叫內部函式（`_sync_user` 之類），**沒有一支走到真正的搜尋
路徑**，也**沒有一支用非 AD 的目錄結構跑過**。只要沒有東西模擬「伺服器會
拒絕未知屬性」，這一類 bug 就是隱形的。

## 這份測試怎麼做

用一個假的 `Connection`，它拿著某一種目錄的屬性白名單；查詢時只要出現白名單
以外的屬性，就跟真的 OpenLDAP 一樣丟 `LDAPAttributeError`。然後把**每一個對外
函式**在三種結構上各跑一次。

新增目錄相關功能時，把函式加進 `ENTRY_POINTS` 就會自動被三種結構驗過。
"""
from __future__ import annotations

import pytest

# --------------------------------------------------------------------------
# 三種目錄的屬性白名單
# --------------------------------------------------------------------------

#: 所有目錄都有的基本屬性
_COMMON = {
    "cn", "sn", "givenName", "mail", "displayName", "description",
    "objectClass", "distinguishedName", "member", "memberOf", "ou", "o",
    "uid", "uidNumber", "gidNumber", "userPassword", "entryDN", "entryUUID",
}

#: Active Directory 專屬（OpenLDAP / UCS 收到會拒絕整個查詢）
_AD_ONLY = {
    "sAMAccountName", "userPrincipalName", "objectSid", "primaryGroupID",
    "userAccountControl", "msDS-UserPasswordExpiryTimeComputed",
    "whenCreated", "whenChanged", "objectGUID", "memberUid",
}

#: Univention Corporate Server 自己的擴充（跟 AD 的完全不同）
_UCS_ONLY = {
    "univentionObjectType", "univentionSambaSID", "krb5PrincipalName",
    "shadowLastChange", "sambaAcctFlags",
}

SCHEMAS = {
    "Active Directory": _COMMON | _AD_ONLY,
    "OpenLDAP": _COMMON,
    "Univention (UCS)": _COMMON | _UCS_ONLY,
}

#: 每種結構對應的 `auth_settings.backend` 值
BACKEND_OF = {
    "Active Directory": "ad",
    "OpenLDAP": "ldap",
    "Univention (UCS)": "ldap",
}


# --------------------------------------------------------------------------
# 假目錄
# --------------------------------------------------------------------------

class _Entry(dict):
    """假的搜尋結果 —— 同時支援 `e["attr"].value` 與 `e.get("attributes")`。"""

    class _Val:
        def __init__(self, v):
            self.value = v
            self.values = [v] if v is not None else []

        def __str__(self):
            return str(self.value)

    def __init__(self, dn, attrs):
        super().__init__(dn=dn, attributes=dict(attrs), type="searchResEntry")
        self.entry_dn = dn
        self._attrs = attrs

    def __contains__(self, k):
        return k in self._attrs

    def __getitem__(self, k):
        if k in ("dn", "attributes", "type"):
            return super().__getitem__(k)
        return self._Val(self._attrs.get(k))


class FakeConnection:
    """行為像真的目錄伺服器 —— **不認得的屬性就拒絕整個查詢**。

    這是這份測試的核心：真的 OpenLDAP 不會「把不認得的欄位回空」，
    它會讓整個 search 失敗。假得太寬鬆就抓不到我們要抓的東西。
    """

    schema: set[str] = set()
    calls: list[tuple] = []
    #: 被伺服器拒絕的屬性。**這才是判準** —— 正式碼會把
    #: `LDAPAttributeError` 吞掉、轉成「無法連線到認證伺服器」，
    #: 例外根本傳不到測試（那正是正式機上看到的症狀）。
    rejections: list[str] = []

    def __init__(self, *a, **kw):
        self.entries = []
        self.response = []
        self.result = {"description": "success"}
        self.bound = True

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def unbind(self):
        return True

    # -- 屬性檢查 ------------------------------------------------------
    def _check(self, attributes):
        if not attributes:
            return
        if isinstance(attributes, str):
            attributes = [attributes]
        # **要在檢查之前就記錄** —— 記在後面的話，被拒絕的那一次不會留下
        # 任何痕跡，「有沒有要求 AD 屬性」的檢查就永遠是綠的（實測踩過）。
        type(self).calls.append(("attrs", list(attributes)))
        for a in attributes:
            if a in ("*", "+", None):
                continue
            if a not in type(self).schema:
                from ldap3.core.exceptions import LDAPAttributeError

                type(self).rejections.append(a)
                raise LDAPAttributeError(
                    f"invalid attribute type in attribute list: {a}")

    def _make_entries(self):
        s = type(self).schema
        attrs = {k: v for k, v in (
            ("cn", "Jason Cheng"), ("uid", "jason"), ("mail", "j@example.com"),
            ("displayName", "Jason Cheng"), ("sAMAccountName", "jason"),
            ("memberOf", ["cn=Admins,dc=example,dc=com"]),
            ("objectClass", ["person"]), ("ou", "IT"),
        ) if k in s}
        return [_Entry("uid=jason,dc=example,dc=com", attrs)]

    def search(self, *a, **kw):
        self._check(kw.get("attributes"))
        type(self).calls.append(("search", kw.get("attributes")))
        self.entries = self._make_entries()
        self.response = list(self.entries)
        return True

    # -- paged_search 走 conn.extend.standard --------------------------
    @property
    def extend(self):
        outer = self

        class _Std:
            def paged_search(self, *a, **kw):
                outer._check(kw.get("attributes"))
                type(outer).calls.append(("paged_search", kw.get("attributes")))
                return outer._make_entries()

        class _Ext:
            standard = _Std()

        return _Ext()


@pytest.fixture
def directory(request, monkeypatch, tmp_path):
    """把 auth_ldap 的 LDAP 連線換成指定結構的假目錄。"""
    label = request.param
    from app.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    from app.core import auth_ldap, auth_settings

    FakeConnection.schema = SCHEMAS[label]
    FakeConnection.calls = []
    FakeConnection.rejections = []
    # **要攔 `ldap3` 本身，不是攔 `auth_ldap` 的屬性** —— `Connection` 是在
    # 每個函式**內部**才 `from ldap3 import Connection`，所以模組層根本沒有
    # 那個名字，攔了也不會生效（第一版就是這樣，整組測試直接 AttributeError）。
    import ldap3

    monkeypatch.setattr(ldap3, "Connection", FakeConnection)
    monkeypatch.setattr(auth_ldap, "_build_server", lambda *a, **kw: object())

    cfg = {
        "backend": BACKEND_OF[label],
        "ldap": {
            "server_url": "ldap://directory.example.com:389",
            "service_dn": "cn=svc,dc=example,dc=com",
            "service_password": "x",
            "user_search_base": "dc=example,dc=com",
            "user_search_filter": "(uid={username})",
            "username_attr": "sAMAccountName" if label == "Active Directory" else "uid",
            "displayname_attr": "displayName",
            "email_attr": "mail",
            "group_attr": "memberOf",
            "use_tls": False,
            "verify_cert": False,
        },
    }
    monkeypatch.setattr(auth_settings, "get", lambda: cfg)
    return label, auth_ldap, cfg


#: 每一個會查目錄的對外函式 —— 新增功能時加進來就自動被三種結構驗過。
ENTRY_POINTS = [
    # **真正的登入路徑一定要在裡面**。第一版只放了管理頁的「連線測試」
    # 輔助函式（`test_user_login`），漏掉 `authenticate()` —— 而正式機掛掉的
    # 正是登入。變異驗證當場發現：把 objectSid 寫回去，整組測試照樣全綠。
    ("使用者登入（authenticate）", lambda m, c: m.authenticate("jason", "pw")),
    ("登入用的使用者查詢", lambda m, c: m._search_ldap_user(c["ldap"], "jason")),
    ("依帳號同步使用者", lambda m, c: m.sync_user_by_username("jason")),
    ("管理頁的登入測試", lambda m, c: m.test_user_login(c["ldap"], "jason", "pw")),
    ("連線測試", lambda m, c: m.test_connection(c["ldap"])),
    ("同步所有群組", lambda m, c: m.sync_all_groups()),
    ("同步所有使用者", lambda m, c: m.sync_all_users()),
    ("列出 OU 子節點", lambda m, c: m.list_ou_children("")),
    ("列出 OU 使用者", lambda m, c: m.list_ou_users("ou=IT,dc=example,dc=com")),
    ("群組成員清單", lambda m, c: m.get_group_members("cn=Admins,dc=example,dc=com")),
    ("群組成員數", lambda m, c: m.count_group_members("cn=Admins,dc=example,dc=com")),
    ("使用者細節", lambda m, c: m.get_user_detail("uid=jason,dc=example,dc=com")),
]


@pytest.mark.parametrize("directory", list(SCHEMAS), indirect=True)
@pytest.mark.parametrize("name,call", ENTRY_POINTS, ids=[n for n, _ in ENTRY_POINTS])
def test_directory_call_works_on_every_schema(directory, name, call):
    """每一個對外函式，在三種目錄結構上都不可以因為屬性被拒而失敗。

    失敗的樣子就是正式機上看到的：使用者以為網路不通，其實是我們自己
    要了對方沒有的屬性，整個查詢被拒。
    """
    label, mod, cfg = directory
    FakeConnection.rejections = []
    try:
        call(mod, cfg)
    except Exception:
        # 例外本身不是判準 —— 正式碼多半會把它吞掉轉成友善訊息。
        # 其他例外（沒有資料庫、綁定失敗…）也不是這份測試要管的。
        pass

    assert not FakeConnection.rejections, (
        f"【{label}】{name}：要求了這個目錄沒有的屬性 "
        f"{sorted(set(FakeConnection.rejections))}\n"
        "OpenLDAP / UCS 收到不認得的屬性名會拒絕**整個查詢**，不是該欄位回空"
        " —— 使用者會看到「無法連線到認證伺服器」，以為是網路問題。")


@pytest.mark.parametrize("directory", ["OpenLDAP", "Univention (UCS)"],
                         indirect=True)
def test_no_ad_attribute_ever_requested_on_non_ad(directory):
    """非 AD 的目錄上，**任何一次查詢**都不可以出現 AD 專屬屬性。

    上面那個測試是「不要爆掉」，這個是「連要都不要要」—— 就算某天假伺服器
    變寬鬆了，這條仍然守得住。
    """
    label, mod, cfg = directory
    for _name, call in ENTRY_POINTS:
        try:
            call(mod, cfg)
        except Exception:
            pass

    asked = set()
    for kind, attrs in FakeConnection.calls:
        if kind != "attrs":
            continue
        for a in (attrs or []):
            if isinstance(a, str):
                asked.add(a)
    leaked = sorted(asked & _AD_ONLY - {"sAMAccountName"})
    assert not leaked, (
        f"【{label}】查詢裡出現了 AD 專屬屬性：{leaked} —— "
        "這些在非 AD 的目錄上會讓整個查詢被拒絕")


def test_fake_directory_actually_rejects_unknown_attributes():
    """**先確認這個假伺服器真的會拒絕** —— 否則上面兩個測試都是空的。

    假造得太寬鬆是這類測試最常見的失敗方式：它會一直是綠的，而且看起來
    很有保障。
    """
    from ldap3.core.exceptions import LDAPAttributeError

    FakeConnection.schema = _COMMON
    conn = FakeConnection()
    conn.search(attributes=["cn", "mail"])          # 都在白名單裡 → 應該過
    with pytest.raises(LDAPAttributeError):
        conn.search(attributes=["cn", "objectSid"])  # 未知屬性 → 應該炸
    with pytest.raises(LDAPAttributeError):
        conn.extend.standard.paged_search(attributes=["userAccountControl"])
