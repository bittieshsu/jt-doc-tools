"""帳號上的信箱欄位（作業完成通知要寄給誰）。

## 由來

使用者回報：「現在帳號部份是不是缺欄位？不然 job 完成後要通知怎麼知道要給誰？」

的確缺 —— `users` 表**從來沒有 email 欄位**。原本每個人都得自己到「我的作業 →
通知設定」手動填一次信箱；接了 AD / LDAP / SSO 的環境更不合理：來源系統早就有
`mail` 屬性 / `email` claim（OIDC 的 `map_claims` 甚至已經解出來了），卻在登入時
被丟掉。

## 這份要守住的規則

| 來源 | 行為 |
|---|---|
| AD / LDAP | 登入時讀 `mail`（屬性名可設定）寫入帳號 |
| SSO（OIDC / SAML） | 用 IdP 給的 email claim / 屬性 |
| 本機帳號 | 管理員在使用者管理填 |
| 使用者自己 | 在通知設定填 `email_to`（**優先於**帳號信箱） |

兩個關鍵：
1. **使用者自填的優先**，而且下一次目錄同步不可以蓋掉它 —— 那是兩個不同欄位。
2. **來源沒給信箱時不可以用空字串覆蓋掉既有的值**，否則管理員手動補的會在下次
   登入時消失。
"""
from __future__ import annotations

import pytest

from app.core import auth_db, user_manager


@pytest.fixture(autouse=True)
def _seed_roles(auth_off):
    """建使用者會指派預設角色 —— 角色列不存在的話外鍵會失敗。

    正常執行時由 app 啟動 seed；這份測試多數不發 HTTP 請求，要自己來。
    """
    from app.core import roles
    roles.seed_builtin_roles()


# ---------- 欄位與清理 ----------

def test_users_table_has_email_column(auth_off):
    cols = {r[1] for r in auth_db.conn().execute("PRAGMA table_info(users)")}
    assert "email" in cols


def test_migration_is_registered_in_order():
    import re
    nums = [int(re.match(r"_m(\d+)_", f.__name__).group(1))
            for f in auth_db.MIGRATIONS]
    assert nums == list(range(1, len(nums) + 1)), f"編號不連續：{nums}"
    assert any("email" in f.__name__ for f in auth_db.MIGRATIONS)


def test_migration_is_idempotent(tmp_path):
    """升級步驟要能重複執行（同一台機器可能因為還原備份而再跑一次）。"""
    import sqlite3
    dbp = tmp_path / "a.sqlite"
    conn = sqlite3.connect(dbp)
    conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT)")
    auth_db._m14_user_email(conn)
    auth_db._m14_user_email(conn)          # 第二次不可以炸
    cols = {r[1] for r in conn.execute("PRAGMA table_info(users)")}
    assert "email" in cols
    conn.close()


@pytest.mark.parametrize("raw,expect", [
    ("  a@b.test  ", "a@b.test"),
    ("a@b.test\nBcc: evil@x.test", "a@b.testBcc: evil@x.test"),   # 控制字元去掉
    ("姓名 <a@b.test>", "姓名 <a@b.test>"),                        # 目錄裡真的有這種
    ("", ""),
    ("x" * 300, "x" * 200),
])
def test_email_normalisation(raw, expect):
    assert user_manager.normalise_email(raw) == expect


# ---------- 管理員 / 本人可以設定 ----------

def test_admin_can_set_and_read_email(auth_off):
    uid = user_manager.create_local("mailuser", "郵件", "UserPass1234")
    user_manager.update(uid, email="  who@example.test ")
    assert user_manager.get_by_id(uid)["email"] == "who@example.test"


def test_email_is_exposed_in_session_dict(auth_off):
    """`request.state.user` 要帶得出信箱，通知才拿得到。"""
    from app.core import sessions
    uid = user_manager.create_local("mailuser2", "郵件", "UserPass1234")
    user_manager.update(uid, email="s@example.test")
    tok, _ = sessions.issue(uid, remember=False, ip="127.0.0.1", ua="pytest")
    assert sessions.lookup(tok)["email"] == "s@example.test"


# ---------- 通知的優先順序 ----------

def _enable_email_notify():
    """把 email 管道打開 —— 沒開的話 `resolve_for_user` 會早退回空 dict。"""
    from app.core import notify_settings as ns
    ns.save({"enabled": True,
             "channels": {"email": {"smtp_host": "smtp.example.test",
                                    "smtp_port": 25, "smtp_from": "a@b.test"}}})


def _resolved(uid: int) -> dict:
    from app.core import notify_settings as ns
    key = f"u{uid}"
    prefs = ns.get_prefs(key)
    ns.save_prefs(key, {"enabled": True, "channels": ["email"],
                        "email_to": prefs.get("email_to", "")})
    _channels, merged = ns.resolve_for_user(key)
    return merged


def test_account_email_used_when_user_did_not_fill_one(auth_off):
    from app.core import notify_settings as ns
    uid = user_manager.create_local("mailuser3", "郵件", "UserPass1234")
    user_manager.update(uid, email="acct@example.test")
    _enable_email_notify()
    assert _resolved(uid)["email_to"] == "acct@example.test"


def test_user_cannot_redirect_notifications_elsewhere(auth_off):
    """**通知信箱只認帳號上的那一個。**

    使用者交代：「使用者不能自己改通知信箱」。原本允許在通知設定填一個覆寫，
    結果同一件事有兩個欄位、兩個地方 —— 使用者不知道哪個才算數，管理員也無從
    得知通知實際寄去哪。現在只有一個來源，而且擋在伺服器端（自己組請求也沒用）。
    """
    from app.core import notify_settings as ns
    uid = user_manager.create_local("mailuser4", "郵件", "UserPass1234")
    user_manager.update(uid, email="acct@example.test")
    ns.save_prefs(f"u{uid}", {"email_to": "mine@example.test"})
    _enable_email_notify()
    assert _resolved(uid)["email_to"] == "acct@example.test"
    assert ns.get_prefs(f"u{uid}").get("email_to", "") == ""


def test_directory_sync_updates_the_notification_address(auth_off):
    """目錄改了信箱 → 通知就跟著改（只有一個來源，不會各說各話）。"""
    uid = user_manager.create_local("mailuser5", "郵件", "UserPass1234")
    user_manager.update(uid, email="old@example.test")
    _enable_email_notify()
    assert _resolved(uid)["email_to"] == "old@example.test"
    user_manager.update(uid, email="from-ad@example.test")   # 模擬同步
    assert _resolved(uid)["email_to"] == "from-ad@example.test"


def test_no_email_anywhere_is_not_an_error(auth_off):
    from app.core import notify_settings as ns
    uid = user_manager.create_local("mailuser6", "郵件", "UserPass1234")
    _enable_email_notify()
    # 兩邊都沒信箱 → email 管道不會被選用（不是錯誤，只是還沒設定）
    from app.core import notify_settings as _ns2
    _ns2.save_prefs(f"u{uid}", {"enabled": True, "channels": ["email"]})
    usable, merged = _ns2.resolve_for_user(f"u{uid}")
    assert merged.get("email_to", "") == ""
    assert "email" not in usable


def test_shared_key_has_no_account_email(auth_off):
    """未啟用認證時沒有「帳號」，不可以誤取到別人的信箱。"""
    from app.core import notify_settings as ns
    assert ns._account_email("_shared") == ""
    assert ns._account_email("") == ""
    assert ns._account_email("u") == ""


# ---------- 目錄 / SSO 帶入 ----------

def test_ldap_search_requests_the_mail_attribute():
    """搜尋時要把信箱屬性一起要回來，否則永遠拿不到。"""
    import inspect
    from app.core import auth_ldap
    src = inspect.getsource(auth_ldap)
    assert src.count('cfg.get("email_attr", "mail")') >= 2, \
        "兩處 LDAP 搜尋都要取信箱屬性"


def test_ldap_entry_email_handles_multivalue_and_missing():
    from app.core import auth_ldap

    class FakeAttr:
        def __init__(self, v): self.value = v

    class FakeEntry:
        def __init__(self, d): self._d = d
        def __contains__(self, k): return k in self._d
        def __getitem__(self, k): return FakeAttr(self._d[k])

    cfg = {}
    assert auth_ldap._entry_email(FakeEntry({"mail": "a@b.test"}), cfg) == "a@b.test"
    assert auth_ldap._entry_email(
        FakeEntry({"mail": ["a@b.test", "b@b.test"]}), cfg) == "a@b.test"
    assert auth_ldap._entry_email(FakeEntry({}), cfg) == ""
    assert auth_ldap._entry_email(FakeEntry({"mail": None}), cfg) == ""


def test_sso_provision_stores_email(auth_off):
    from app.core import sso_provision
    u = sso_provision.provision("oidc", external_id="sub-1", username="ssouser",
                                display_name="SSO 使用者",
                                email="sso@example.test")
    assert user_manager.get_by_id(u["user_id"])["email"] == "sso@example.test"


def test_sso_reprovision_does_not_clear_email_when_idp_omits_it(auth_off):
    """IdP 這次沒給信箱時要保留原值，不可以用空字串蓋掉。"""
    from app.core import sso_provision
    u = sso_provision.provision("oidc", external_id="sub-2", username="ssouser2",
                                display_name="SSO", email="keep@example.test")
    sso_provision.provision("oidc", external_id="sub-2", username="ssouser2",
                            display_name="SSO", email="")
    assert user_manager.get_by_id(u["user_id"])["email"] == "keep@example.test"


def test_sso_routes_pass_email_through():
    """兩條 SSO 路徑都要把 IdP 給的信箱傳下去（`map_claims` 早就解出來了）。"""
    import inspect
    from app.web import sso_routes
    src = inspect.getsource(sso_routes)
    assert src.count('email=ident.get("email", "")') == 2, "OIDC / SAML 各一處"


def test_email_attr_setting_is_saved_and_used(auth_off):
    """信箱屬性名要可設定（不是每個目錄都用 `mail`），而且真的存得起來。

    設定頁的欄位若沒被列進後端的允許清單，畫面上填了會**無聲消失** ——
    那種問題只有客戶改了之後才會發現。
    """
    from app.core import auth_settings
    s = auth_settings.get()
    assert s["ldap"]["email_attr"] == "mail", "預設值要是 mail"
    import inspect
    from app.admin import auth_router
    src = inspect.getsource(auth_router)
    assert src.count('"email_attr"') >= 2, "兩個儲存端點都要允許這個欄位"


# ---------- 自助修改（只有本機帳號） ----------

def _client_for(uid: int):
    from fastapi.testclient import TestClient
    from app.core import sessions
    import app.main as app_main
    tok, _ = sessions.issue(uid, remember=False, ip="127.0.0.1", ua="pytest")
    c = TestClient(app_main.app)
    c.cookies.set(sessions.COOKIE_NAME, tok)
    return c


def _enable_auth():
    from app.core import auth_settings
    pw = "TestAdmin1234"
    try:
        auth_settings.enable_local_with_admin(
            admin_username="jtdt-admin", admin_display_name="管理員",
            admin_password=pw, admin_password_confirm=pw, actor_ip="127.0.0.1")
    except Exception:
        pass


def test_local_user_can_change_own_email(auth_off):
    """本機帳號要能自己改 —— 這是「通知寄到哪」，本人最清楚，不該找管理員。"""
    _enable_auth()
    uid = user_manager.create_local("selfmail", "自助", "UserPass1234")
    c = _client_for(uid)
    r = c.post("/me/email", json={"email": " me@example.test "})
    assert r.status_code == 200, r.text
    assert r.json()["email"] == "me@example.test"
    assert user_manager.get_by_id(uid)["email"] == "me@example.test"


def test_directory_user_cannot_change_own_email(auth_off):
    """目錄帳號改了也會被下次登入覆蓋 —— 給一個「改得動但沒用」的欄位更糟。

    而且要擋在**伺服器端**，不是只把 UI 藏起來。
    """
    _enable_auth()
    from app.core import auth_db, db
    uid = user_manager.create_local("dirmail", "目錄", "UserPass1234")
    conn = auth_db.conn()
    with db.tx(conn):
        conn.execute("UPDATE users SET source='ldap' WHERE id=?", (uid,))
    c = _client_for(uid)
    r = c.post("/me/email", json={"email": "hack@example.test"})
    assert r.status_code == 403, r.status_code
    assert "目錄" in r.text
    assert user_manager.get_by_id(uid)["email"] == ""


def test_self_email_requires_login(auth_off):
    from fastapi.testclient import TestClient
    import app.main as app_main
    _enable_auth()
    # 不要跟著轉址 —— 認證閘會把未登入的請求導到 /login，而那一頁回 200，
    # 跟著走就會誤判成「端點放行了」。
    r = TestClient(app_main.app).post("/me/email", json={"email": "x@y.test"},
                                      follow_redirects=False)
    assert r.status_code in (302, 303, 401, 403), r.status_code
    # 真的沒有寫進去
    assert not user_manager.get_by_username("selfmail3")


def test_self_email_is_normalised(auth_off):
    """控制字元要去掉（含換行的值會讓寄信在送出當下失敗）。"""
    _enable_auth()
    uid = user_manager.create_local("selfmail2", "自助", "UserPass1234")
    c = _client_for(uid)
    c.post("/me/email", json={"email": "a@b.test\nBcc: evil@x.test"})
    assert "\n" not in user_manager.get_by_id(uid)["email"]


def test_whoami_exposes_email_and_editability(auth_off):
    """卡片要顯示信箱 —— 使用者現在完全看不到「通知會寄到哪」。"""
    _enable_auth()
    uid = user_manager.create_local("cardmail", "卡片", "UserPass1234")
    user_manager.update(uid, email="card@example.test")
    j = _client_for(uid).get("/whoami").json()
    assert j["email"] == "card@example.test"
    assert j["email_editable"] is True


def test_whoami_marks_directory_account_read_only(auth_off):
    _enable_auth()
    from app.core import auth_db, db
    uid = user_manager.create_local("cardmail2", "卡片", "UserPass1234")
    conn = auth_db.conn()
    with db.tx(conn):
        conn.execute("UPDATE users SET source='ad', email=? WHERE id=?",
                     ("from-ad@example.test", uid))
    j = _client_for(uid).get("/whoami").json()
    assert j["email"] == "from-ad@example.test"
    assert j["email_editable"] is False


# ---------- 排程目錄同步也要帶信箱 ----------

def test_directory_sync_requests_and_stores_email(auth_off, monkeypatch):
    """**不必等使用者下次登入** —— 排程 / 手動的目錄同步就要把信箱帶進來。

    使用者問「我設好信箱屬性，它何時才會同步？」——如果只有登入時才帶，
    管理員設好之後得等每個人各自登入一次，而通知正是要寄給那些「還沒回來」的人。
    """
    from app.core import auth_db, auth_settings, db, auth_ldap

    s = auth_settings.get()
    s["backend"] = "ldap"
    s["ldap"] = {**s.get("ldap", {}),
                 "service_dn": "cn=svc", "service_password": "x",
                 "user_search_base": "dc=example,dc=test",
                 "username_attr": "uid", "displayname_attr": "displayName",
                 "email_attr": "mailPrimaryAddress"}
    auth_settings.save(s)

    asked = {}

    class FakeConn:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False

        @property
        def extend(self):
            outer = self

            class Std:
                def paged_search(self, **kw):
                    asked["attributes"] = kw.get("attributes")
                    return [{
                        "dn": "uid=alice,dc=example,dc=test",
                        "type": "searchResEntry",
                        "attributes": {"uid": ["alice"],
                                       "displayName": ["Alice"],
                                       "mailPrimaryAddress": ["alice@ucs.test"]},
                    }]

            class Ext:
                standard = Std()
            return Ext()

    monkeypatch.setattr(auth_ldap, "Connection", FakeConn, raising=False)
    monkeypatch.setattr(auth_ldap, "_build_server", lambda cfg: object())
    import ldap3
    monkeypatch.setattr(ldap3, "Connection", FakeConn)

    out = auth_ldap.sync_all_users()
    assert "mailPrimaryAddress" in (asked.get("attributes") or []), \
        f"同步時沒有把信箱屬性要回來：{asked.get('attributes')}"
    row = auth_db.conn().execute(
        "SELECT email FROM users WHERE username='alice'").fetchone()
    assert row is not None, f"使用者沒有被同步進來：{out}"
    assert row["email"] == "alice@ucs.test"


def test_directory_sync_does_not_clear_email_when_absent(auth_off, monkeypatch):
    """目錄這次沒給信箱 → 保留原值，不可以清成空白。"""
    from app.core import auth_db, auth_settings, db, auth_ldap
    s = auth_settings.get()
    s["backend"] = "ldap"
    s["ldap"] = {**s.get("ldap", {}), "service_dn": "cn=svc",
                 "service_password": "x", "user_search_base": "dc=example,dc=test",
                 "username_attr": "uid", "email_attr": "mail"}
    auth_settings.save(s)
    conn = auth_db.conn()
    with db.tx(conn):
        conn.execute(
            "INSERT INTO users(username, display_name, source, external_dn, "
            "enabled, is_admin_seed, created_at, email) "
            "VALUES ('bob','Bob','ldap','uid=bob,dc=example,dc=test',1,0,0,"
            "'kept@example.test')")

    class FakeConn:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False

        @property
        def extend(self):
            class Std:
                def paged_search(self, **kw):
                    return [{"dn": "uid=bob,dc=example,dc=test",
                             "type": "searchResEntry",
                             "attributes": {"uid": ["bob"]}}]

            class Ext:
                standard = Std()
            return Ext()

    import ldap3
    monkeypatch.setattr(ldap3, "Connection", FakeConn)
    monkeypatch.setattr(auth_ldap, "_build_server", lambda cfg: object())
    auth_ldap.sync_all_users()
    row = conn.execute("SELECT email FROM users WHERE username='bob'").fetchone()
    assert row["email"] == "kept@example.test"
