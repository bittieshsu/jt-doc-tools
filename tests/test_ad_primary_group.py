"""AD 的「主要群組」（primaryGroupID）也要算成使用者的群組。

## 由來

AD 的主要群組**不會出現在 `memberOf`**。多數帳號的主要群組是 Domain Users
（RID 513），但企業確實會把它改成別的群組 —— 一旦有人把權限掛在那個群組上，
成員一個都拿不到，而且**完全看不出原因**（memberOf 裡就是沒有那一筆，管理員在
AD 使用者的「成員隸屬」分頁也只看得到 memberOf）。這是 AD 整合最經典的坑之一。

## 怎麼算

主要群組的 SID = 使用者 `objectSid` 的網域部分 + `primaryGroupID` 當 RID。
使用者 SID 的最後一段是他自己的 RID，換掉就是群組的 SID；拿 SID 反查 DN。

## 三個不可以

* **不可以讓登入失敗**：查不到就跳過。這只是補強。
* **不可以對 OpenLDAP 出錯**：那邊沒有 objectSid / primaryGroupID，要不到就是空的。
* **filter 不可以塞字串 SID**：AD 的 objectSid 是二進位屬性，filter 要寫成
  `\\XX\\XX…` 的跳脫位元組，直接塞 `S-1-5-…` 在多數環境查不到東西。
"""
from __future__ import annotations

import pytest

from app.core.auth_ldap import (_lookup_primary_group_dn, _sid_to_ldap_filter,
                                primary_group_sid, sid_to_string)


def _raw_sid(*subs: int, authority: int = 5, revision: int = 1) -> bytes:
    """組出一個真實形狀的二進位 SID。"""
    out = bytes([revision, len(subs)]) + authority.to_bytes(6, "big")
    for sub in subs:
        out += sub.to_bytes(4, "little")
    return out


# ------------------------------------------------------------ SID 解析

def test_binary_sid_is_decoded():
    """sub-authority 是**小端序**、identifier authority 是**大端序** —— 弄反了
    會解出完全不同的網域 SID，然後主要群組永遠查不到。"""
    raw = _raw_sid(21, 1004336348, 1177238915, 682003330, 512)
    assert sid_to_string(raw) == \
        "S-1-5-21-1004336348-1177238915-682003330-512"


def test_string_sid_passes_through():
    """ldap3 依 schema 有時已經轉好了 —— 不要再解一次。"""
    assert sid_to_string("S-1-5-21-1-2-3-500") == "S-1-5-21-1-2-3-500"


@pytest.mark.parametrize("bad", [None, b"", b"\x01", "not-a-sid", 123, []])
def test_bad_sid_returns_empty_not_raise(bad):
    assert sid_to_string(bad) == ""


# ------------------------------------------------------------ 主要群組 SID

def test_primary_group_sid_swaps_the_rid():
    user = "S-1-5-21-1004336348-1177238915-682003330-1103"
    assert primary_group_sid(user, 513) == \
        "S-1-5-21-1004336348-1177238915-682003330-513"


def test_primary_group_sid_accepts_string_gid():
    """ldap3 可能把 primaryGroupID 回成字串。"""
    assert primary_group_sid("S-1-5-21-1-2-3-1103", "513").endswith("-513")


@pytest.mark.parametrize("sid,gid", [
    ("", 513),                 # 沒有 objectSid（OpenLDAP）
    ("S-1-5-21-1-2-3-1103", None),   # 沒有 primaryGroupID
    ("S-1-5-21-1-2-3-1103", ""),
    ("not-a-sid", 513),
    ("S-1-5-21-1-2-3-1103", "abc"),
])
def test_primary_group_sid_gives_up_quietly(sid, gid):
    assert primary_group_sid(sid, gid) == ""


# ------------------------------------------------------------ filter 跳脫

def test_filter_is_escaped_binary_not_the_string_sid():
    flt = _sid_to_ldap_filter("S-1-5-21-1004336348-1177238915-682003330-513")
    assert flt.startswith("\\01\\05"), flt[:20]
    assert "S-1-5" not in flt, "把字串 SID 塞進 filter，AD 查不到"
    # 每個 byte 都是 \XX
    assert all(len(p) == 2 for p in flt.split("\\") if p)


def test_filter_roundtrips_back_to_the_same_sid():
    """跳脫出來的位元組要能解回原本的 SID —— 這是最直接的正確性檢查。"""
    sid = "S-1-5-21-1004336348-1177238915-682003330-513"
    flt = _sid_to_ldap_filter(sid)
    raw = bytes(int(p, 16) for p in flt.split("\\") if p)
    assert sid_to_string(raw) == sid


def test_bad_input_to_filter_returns_empty():
    assert _sid_to_ldap_filter("nonsense") == ""


# ------------------------------------------------------------ 查詢整合

class _FakeAttr:
    def __init__(self, value): self.value = value


class _FakeEntry:
    def __init__(self, attrs, dn="cn=x,dc=e"):
        self._a = attrs
        self.entry_dn = dn
    def __contains__(self, k): return k in self._a
    def __getitem__(self, k): return _FakeAttr(self._a[k])


class _FakeConn:
    """記下最後一次搜尋，並回傳預設好的結果。"""
    def __init__(self, result=None): self.result = result or []; self.last = None
    def search(self, **kw): self.last = kw
    @property
    def entries(self): return self.result


def test_lookup_finds_the_primary_group_dn():
    user = _FakeEntry({"objectSid": _raw_sid(21, 1, 2, 3, 1103),
                       "primaryGroupID": 513})
    grp = _FakeEntry({}, dn="CN=Domain Users,CN=Users,DC=example,DC=com")
    conn = _FakeConn([grp])
    dn = _lookup_primary_group_dn(conn, user, {"user_search_base": "dc=example,dc=com"})
    assert dn == "CN=Domain Users,CN=Users,DC=example,DC=com"
    # 查的是 objectSid，而且是跳脫過的二進位
    assert conn.last["search_filter"].startswith("(objectSid=\\01\\05")


def test_lookup_is_a_noop_for_openldap():
    """OpenLDAP 沒有這兩個屬性 —— 不可以出錯，也不該發出查詢。"""
    user = _FakeEntry({"uid": "alice"})
    conn = _FakeConn()
    assert _lookup_primary_group_dn(conn, user, {"user_search_base": "dc=e"}) == ""
    assert conn.last is None, "沒有必要的資訊還是發了一次查詢"


def test_lookup_never_raises():
    """查詢炸掉時要安靜跳過 —— 主要群組只是補強，不該讓登入失敗。"""
    class Boom(_FakeConn):
        def search(self, **kw): raise RuntimeError("DC 掛了")
    user = _FakeEntry({"objectSid": _raw_sid(21, 1, 2, 3, 1103),
                       "primaryGroupID": 513})
    assert _lookup_primary_group_dn(Boom(), user, {"user_search_base": "dc=e"}) == ""


def test_lookup_needs_a_base():
    user = _FakeEntry({"objectSid": _raw_sid(21, 1, 2, 3, 1103),
                       "primaryGroupID": 513})
    assert _lookup_primary_group_dn(_FakeConn(), user, {}) == ""


def test_primary_group_is_appended_to_group_dns():
    """登入流程要把主要群組併進 group_dns（去重）。"""
    import inspect

    from app.core import auth_ldap
    src = inspect.getsource(auth_ldap._search_ldap_user)
    assert "primary_dn" in src
    assert "group_dns.append(primary_dn)" in src
    assert "primary_dn not in group_dns" in src, "沒有去重，會重複掛同一個群組"
    assert '"objectSid", "primaryGroupID"' in src, "搜尋沒有要這兩個屬性"
