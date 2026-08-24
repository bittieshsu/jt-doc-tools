"""OU 授權的 DN 大小寫 / 空白正規化（v1.14.48）。

背景：OU 授權的比對是**精確字串相等**（SQL `subject_key=?`），但 LDAP 的 DN
屬性型別不分大小寫、逗號後可有空白。管理介面正常操作寫進來的 key 與目錄樹
同源沒問題；手動輸入 / 匯入改過的設定 / 換目錄來源就可能寫成 `OU=Sales,DC=x`，
於是登入時算出的 `ou=Sales,dc=x` 對不上 —— 指派看起來成功，底下的人卻**無聲
地**拿不到權限（2026-08-24 用真的 OpenLDAP 做端到端登入時撞到）。

測試清單：
  1. 四種等價寫法都正規化成同一個 key
  2. user / group 的 key（是 id）**不可**被動到
  3. 值的大小寫**不動**（只動屬性型別與結構空白）
  4. 寫入端正規化：用大寫 DN 指派，用目錄樣式 DN 讀得回來
  5. 登入比對：大寫 DN 指派的角色，登入後真的生效（effective_tools）
  6. migration 把既有大寫 key 就地校準，並合併衝突
  7. auth_db 那份 `_canon_ou_key` 與 permissions 的 `canon_subject_key` 一致
     （兩處實作，必須同答案 —— 否則就是下一個會漂的東西）
"""
from __future__ import annotations

import sqlite3

import pytest

from app.core import permissions as P


CANON = "ou=Sales,dc=corp,dc=example,dc=com"
VARIANTS = [
    "ou=Sales,dc=corp,dc=example,dc=com",          # 目錄回傳原樣
    "OU=Sales,DC=corp,DC=example,DC=com",          # AD 慣用大寫
    "OU=Sales,dc=corp,dc=example,dc=com",          # 只有屬性型別大寫
    "ou=Sales, dc=corp, dc=example, dc=com",       # 逗號後有空白
    " ou=Sales,dc=corp,dc=example,dc=com ",        # 前後空白
]


# ---------------------------------------------------------------- 1
@pytest.mark.parametrize("v", VARIANTS)
def test_all_variants_canonicalise_to_same_key(v):
    assert P.canon_subject_key("ou", v) == CANON


# ---------------------------------------------------------------- 2
def test_user_and_group_keys_untouched():
    assert P.canon_subject_key("user", "42") == "42"
    assert P.canon_subject_key("group", "Grp-ID-XYZ") == "Grp-ID-XYZ"
    # 就算 group key 長得像 DN 也不動（它不是 DN）
    assert P.canon_subject_key("group", "OU=x,DC=y") == "OU=x,DC=y"


# ---------------------------------------------------------------- 3
def test_value_case_is_preserved():
    """屬性型別轉小寫，但值（Sales）的大小寫保留 —— 只壓平結構，不壓平身分。"""
    out = P.canon_subject_key("ou", "OU=SaLeS,DC=Corp")
    assert out == "ou=SaLeS,dc=Corp"


# ---------------------------------------------------------------- 4
def test_write_with_uppercase_reads_back_with_directory_style(auth_off):
    from app.core import roles
    roles.seed_builtin_roles()
    # 用 AD 大寫指派
    P.set_subject_roles("ou", "OU=Sales,DC=corp,DC=example,DC=com", ["clerk"])
    # 用目錄樣式讀 —— 應該讀得回來（兩者正規化後同一把 key）
    got = P.list_roles_for_subject("ou", "ou=Sales,dc=corp,dc=example,dc=com")
    assert got == ["clerk"]
    # 反過來也一樣
    got2 = P.list_roles_for_subject("ou", "OU=Sales, DC=corp, DC=example, DC=com")
    assert got2 == ["clerk"]


# ---------------------------------------------------------------- 5
def test_login_grants_role_assigned_with_uppercase_dn(auth_off, monkeypatch):
    """端到端：OU 用大寫指派 finance，該 OU 下的使用者登入後真的拿到 pdf-fill。"""
    from app.core import roles, auth_ldap
    roles.seed_builtin_roles()
    # 走真實的目錄同步路徑建立使用者（與登入時同一條），DN 落在 Sales OU 底下
    u = auth_ldap._sync_user(
        username="ou-alice", display_name="OU Alice",
        dn="uid=alice,ou=Sales,dc=corp,dc=example,dc=com", backend="ldap")
    uid = u["user_id"]

    # pdf-fill 不在預設角色裡 → 適合當判準
    P.set_subject_roles("ou", "OU=Sales,DC=corp,DC=example,DC=com", ["finance"])
    P.invalidate_cache()
    tools = P.effective_tools(uid)
    assert tools != "ALL"
    assert "pdf-fill" in tools, "大寫 DN 指派的 OU 角色沒有在登入時生效"


# ---------------------------------------------------------------- 5b
def test_login_matches_when_directory_returns_uppercase_dn(auth_off):
    """**目錄回傳大寫 DN**（真 AD 的樣子）也要對得上指派時的目錄樣式 key。

    這條守的是「登入比對端」的正規化。少了它，上一條測試仍會過 —— 因為那條
    的使用者 DN 本來就是小寫，寫入端正規化後剛好對上（變異驗證當場發現這個
    盲點：拿掉登入端正規化，整組測試照樣全綠）。
    """
    from app.core import roles, auth_ldap
    roles.seed_builtin_roles()
    # AD 回傳的 DN 慣例：屬性型別大寫
    u = auth_ldap._sync_user(
        username="ad-bob", display_name="AD Bob",
        dn="CN=Bob,OU=Taipei,DC=corp,DC=example,DC=com", backend="ad")
    uid = u["user_id"]

    # 管理介面指派時寫的是目錄樹回來的那一份（這裡故意用小寫，模擬另一個來源）
    P.set_subject_roles("ou", "ou=Taipei,dc=corp,dc=example,dc=com", ["finance"])
    P.invalidate_cache()
    tools = P.effective_tools(uid)
    assert tools != "ALL"
    assert "pdf-fill" in tools, (
        "目錄回傳大寫 DN 時，OU 角色沒有生效 —— 登入比對端沒有正規化")


# ---------------------------------------------------------------- 6
def test_migration_canonicalises_existing_rows(tmp_path):
    from app.core import auth_db
    db = tmp_path / "auth.sqlite"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE subject_roles(subject_type TEXT, subject_key TEXT, role_id TEXT);
        CREATE TABLE subject_perms(subject_type TEXT, subject_key TEXT, tool_id TEXT);
    """)
    # 兩筆等價（大小寫不同）→ 應合併成一筆；一筆 user 不可動
    conn.execute("INSERT INTO subject_roles VALUES('ou','OU=Sales,DC=x','clerk')")
    conn.execute("INSERT INTO subject_roles VALUES('ou','ou=Sales,dc=x','clerk')")
    conn.execute("INSERT INTO subject_roles VALUES('ou','OU=Other,DC=x','finance')")
    conn.execute("INSERT INTO subject_roles VALUES('user','7','clerk')")
    conn.execute("INSERT INTO subject_perms VALUES('ou','OU=Sales,DC=x','pdf-fill')")
    conn.commit()

    auth_db._m23_canon_ou_subject_keys(conn)
    conn.commit()

    roles = conn.execute(
        "SELECT subject_type, subject_key, role_id FROM subject_roles "
        "ORDER BY subject_key, role_id").fetchall()
    # 兩筆 Sales 合併成一，Other 正規化，user 不動
    assert ("ou", "ou=Other,dc=x", "finance") in roles
    assert ("ou", "ou=Sales,dc=x", "clerk") in roles
    assert ("user", "7", "clerk") in roles
    assert sum(1 for r in roles if r[1] == "ou=Sales,dc=x") == 1, "重複列沒有被合併"
    assert not any(r[1] == "OU=Sales,DC=x" for r in roles), "還有大寫殘留"

    perms = conn.execute("SELECT subject_key, tool_id FROM subject_perms").fetchall()
    assert perms == [("ou=Sales,dc=x", "pdf-fill")]
    conn.close()


# ---------------------------------------------------------------- 7
def test_two_canon_implementations_agree():
    """auth_db 的 `_canon_ou_key` 與 permissions 的 `canon_subject_key`
    必須對同一組輸入給相同答案 —— 兩處各存一份，漂掉這條就紅。"""
    from app.core import auth_db
    samples = VARIANTS + [
        "OU=A,OU=B,DC=c", "ou=x, ou=y ,dc=z", "cn=weird,ou=T,dc=t",
        "", "OU=只有一段", "DC=corp,DC=example",
    ]
    for s in samples:
        assert auth_db._canon_ou_key(s) == P.canon_subject_key("ou", s), s
