"""登入失敗的稽核要夠全、夠細，而且停用的目錄帳號不可以自己復活（v1.16.94）。

## 由來（使用者 2026-10-15：「帳號登入 ad ldap sso 失敗要在哪裡查 log」「要看一下有沒有夠全 夠詳細」）

逐條盤點登入路徑時找到：

* **資安漏洞**：管理員在本站停用的 AD / LDAP 帳號，只要 AD 密碼還對，**登入就成功、帳號被改回啟用**
  （同步程式把「名冊帳號第一次登入要啟用」的 `enabled=1` 也套在停用的人身上；代理 SSO 走同一支）。
  修法是另外記 `admin_disabled`，目錄登入一律看它擋。OIDC / SAML 本來就有擋。
* 稽核缺口：登入領域不對（只開本機帳號時硬試目錄領域）不留紀錄；本機分不出「沒這個帳號」還是「密碼錯」；
  目錄連不上的原因被截掉；找不到使用者沒記搜尋條件；沒記耗時；SSO 沒記是哪一種；兩步驟驗證失敗記成數字 ID。

判準：每一種失敗都寫 `login_fail`（或代理 SSO 的 `proxy_sso_login_fail`），`reason` 分得出原因，**稽核裡沒有密碼**。
"""
from __future__ import annotations

import json
import time

import pytest

from app.core import audit_db, auth, auth_db, auth_ldap, auth_settings, db, user_manager

PW = "Corr3ct-Horse-Battery"


@pytest.fixture
def events(monkeypatch):
    """同步攔下稽核事件（實際寫入是非同步的）。"""
    got: list[dict] = []
    real = audit_db.log_event

    def capture(event_type, **kw):
        got.append({"event": event_type, **kw})
        return real(event_type, **kw)
    monkeypatch.setattr(audit_db, "log_event", capture)
    return got


@pytest.fixture
def ldap_backend(auth_off, monkeypatch):
    from app.core import roles
    roles.seed_builtin_roles()          # 登入成功要指派預設角色
    s = auth_settings.get()
    saved = {k: s.get(k) for k in ("backend", "ldap")}
    s["backend"] = "ldap"
    s["ldap"] = {"server_url": "ldap://dc.example.test", "service_dn": "cn=svc,dc=example,dc=test",
                 "service_password": "svc-secret", "user_search_base": "ou=Users,dc=example,dc=test",
                 "user_search_filter": "(uid={username})", "use_tls": False}
    auth_settings.save(s)
    yield
    s = auth_settings.get()
    s.update(saved)
    auth_settings.save(s)
    with db.tx(auth_db.conn()):
        auth_db.conn().execute("DELETE FROM lockouts")


def _ldap_user(name: str, *, enabled: int, logged_in: bool) -> tuple[int, str]:
    dn = f"uid={name},ou=Users,dc=example,dc=test"
    conn = auth_db.conn()
    with db.tx(conn):
        conn.execute("DELETE FROM users WHERE username=?", (name,))
        conn.execute("INSERT INTO users(username, display_name, source, external_dn, enabled, created_at, last_login_at) "
                     "VALUES (?,?, 'ldap', ?, ?, ?, ?)", (name, name, dn, enabled, time.time(),
                                                         time.time() if logged_in else 0))
    return conn.execute("SELECT id FROM users WHERE username=?", (name,)).fetchone()["id"], dn


class _FakeConn:
    """像 ldap3 的 Connection：連線（open）、驗證（bind）、實際連上的 DC（server / socket）。"""
    fail_with: Exception | None = None

    def __init__(self, *a, **k):
        self.bound = False
        self.server = type("S", (), {"host": "dc-02.example.test", "port": 636, "ssl": True})()
        self.socket = type("Sock", (), {"getpeername": lambda self: ("192.0.2.22", 636)})()

    def open(self, read_server_info=True):
        return True

    def bind(self, read_server_info=True):
        if type(self).fail_with is not None:
            raise type(self).fail_with
        self.bound = True
        return True

    def unbind(self):
        return True


def _directory_says_ok(monkeypatch, dn: str, *, fail_with: Exception | None = None):
    """目錄找得到這個人；`fail_with` 沒給就是密碼也對（不連真的 LDAP）。"""
    import ldap3
    conn_cls = type("_Conn", (_FakeConn,), {"fail_with": fail_with})
    monkeypatch.setattr(auth_ldap, "_search_ldap_user",
                        lambda u, c: {"user_dn": dn, "display_name": u, "email": "", "group_dns": []})
    monkeypatch.setattr(ldap3, "Connection", conn_cls)


def _row(uid):
    return auth_db.conn().execute("SELECT enabled, admin_disabled FROM users WHERE id=?", (uid,)).fetchone()


# --------------------------------------------------------------------- 停用的目錄帳號不能自己復活

def test_admin_disabled_ldap_user_cannot_sign_in_with_a_correct_password(ldap_backend, monkeypatch, events):
    uid, dn = _ldap_user("frank", enabled=1, logged_in=True)
    user_manager.update(uid, enabled=False)
    assert tuple(_row(uid)) == (0, 1)
    _directory_says_ok(monkeypatch, dn)
    with pytest.raises(auth.AuthError) as ei:
        auth.authenticate("frank", PW, ip="192.0.2.9", realm="ldap")
    assert "停用" in str(ei.value)
    assert tuple(_row(uid)) == (0, 1), "停用的帳號被登入改回啟用了"
    fail = [e for e in events if e["event"] == "login_fail"][-1]
    assert fail["details"]["reason"] == "disabled_in_jtdt" and fail["details"]["source"] == "ldap"
    # 密碼是對的：不算猜密碼（不累計鎖定）
    assert not auth_db.conn().execute("SELECT 1 FROM lockouts WHERE key LIKE 'user:frank'").fetchone()


def test_admin_disabled_ldap_user_cannot_sign_in_by_proxy_sso(ldap_backend, monkeypatch):
    uid, dn = _ldap_user("grace", enabled=1, logged_in=True)
    user_manager.update(uid, enabled=False)
    _directory_says_ok(monkeypatch, dn)
    with pytest.raises(auth_ldap.AccountDisabledError):
        auth_ldap.sync_user_by_username("grace")
    assert tuple(_row(uid)) == (0, 1)


def test_directory_roster_user_is_still_activated_on_first_sign_in(ldap_backend, monkeypatch):
    """名冊帳號（目錄同步進來、還沒登入過、管理員沒停用）第一次登入照樣啟用 —— 原本的設計不可以被修壞。"""
    uid, dn = _ldap_user("henry", enabled=0, logged_in=False)
    _directory_says_ok(monkeypatch, dn)
    user = auth.authenticate("henry", PW, ip="192.0.2.9", realm="ldap")
    assert user["username"] == "henry" and tuple(_row(uid)) == (1, 0)


def test_re_enabling_clears_the_flag(ldap_backend, monkeypatch):
    uid, dn = _ldap_user("ivy", enabled=1, logged_in=True)
    user_manager.update(uid, enabled=False)
    user_manager.update(uid, enabled=True)
    assert tuple(_row(uid)) == (1, 0)
    _directory_says_ok(monkeypatch, dn)
    assert auth.authenticate("ivy", PW, ip="192.0.2.9", realm="ldap")["username"] == "ivy"


def test_upgrade_marks_existing_disabled_accounts(tmp_path):
    """升級前就停用的帳號：目錄帳號曾經登入過 → 當成管理員停用；名冊（沒登入過）不動；本機停用的照樣停用。"""
    p = tmp_path / "auth.sqlite"
    db.migrate(p, auth_db.MIGRATIONS[:-1])
    c = db.get_conn(p)
    now = time.time()
    with db.tx(c):
        for name, src, enabled, last in (("was-on", "ldap", 0, now), ("roster", "ad", 0, 0),
                                         ("local-off", "local", 0, now), ("active", "ldap", 1, now)):
            c.execute("INSERT INTO users(username, display_name, source, external_dn, enabled, created_at, last_login_at) "
                      "VALUES (?,?,?,?,?,?,?)", (name, name, src, f"uid={name}" if src != "local" else None,
                                                  enabled, now, last))
    db.migrate(p, auth_db.MIGRATIONS)
    got = {r["username"]: r["admin_disabled"] for r in c.execute("SELECT username, admin_disabled FROM users")}
    assert got == {"was-on": 1, "roster": 0, "local-off": 1, "active": 0}


# --------------------------------------------------------------------- 稽核夠全、夠細

def test_local_failure_says_whether_the_account_exists(admin_session, events):
    from app.core import auth_local
    for name in ("jtdt-admin", "nobody-here"):
        with pytest.raises(auth_local.AuthError):
            auth_local.authenticate(name, "wrong-password-1", ip="192.0.2.7")
    fails = [e for e in events if e["event"] == "login_fail"][-2:]
    assert [f["details"]["user_found"] for f in fails] == [True, False]
    assert all(f["details"]["source"] == "local" for f in fails)


def test_wrong_realm_is_recorded(admin_session, events):
    with pytest.raises(auth.AuthError):
        auth.authenticate("someone", "x", ip="192.0.2.8", realm="ldap")        # 只開本機帳號
    with pytest.raises(auth.AuthError):
        auth.authenticate("someone", "x", ip="192.0.2.8", realm="kerberos")
    reasons = [e["details"]["reason"] for e in events if e["event"] == "login_fail"][-2:]
    assert reasons == ["realm_disabled", "realm_invalid"]


def test_user_not_found_records_the_search_settings(ldap_backend, monkeypatch, events):
    def not_found(u, c):
        raise auth_ldap.UserNotFoundError("找不到使用者")
    monkeypatch.setattr(auth_ldap, "_search_ldap_user", not_found)
    with pytest.raises(auth.AuthError):
        auth.authenticate("ghost", PW, ip="192.0.2.9", realm="ldap")
    d = [e for e in events if e["event"] == "login_fail"][-1]["details"]
    assert d["reason"] == "ldap_user_not_found" and d["search_base"] == "ou=Users,dc=example,dc=test"
    assert d["search_filter"] == "(uid={username})" and isinstance(d["elapsed_ms"], int)


def test_unreachable_directory_reason_is_not_cut_off(ldap_backend, monkeypatch, events):
    """LDAPS 憑證名稱不符時，原因在原始訊息的最後一句 —— 以前截 300 字剛好截掉。"""
    cert = ("socket ssl wrapping error: certificate {'subject': (), 'issuer': ((('commonName', 'example-CA'),),), "
            "'version': 3, 'serialNumber': '" + "AB" * 60 + "', 'subjectAltName': (('DNS', 'dc-01.example.local'),)} "
            "doesn't match any name in ['192.0.2.5'] ")

    def boom(u, c):
        raise auth_ldap.AuthError(f"無法連線/查詢 LDAP：{auth_ldap.explain_ldap_error(Exception(cert))}")
    monkeypatch.setattr(auth_ldap, "_search_ldap_user", boom)
    with pytest.raises(auth.AuthError):
        auth.authenticate("frank", PW, ip="192.0.2.9", realm="ldap")
    d = [e for e in events if e["event"] == "login_fail"][-1]["details"]
    assert d["reason"] == "ldap_unavailable" and "dc-01.example.local" in d["detail"] and "192.0.2.5" in d["detail"]


def test_bind_failure_keeps_the_ad_reason_and_which_dc_answered(ldap_backend, monkeypatch, events):
    """客戶 2026-10-15：存檔後馬上登入 AD 回 52e、兩分鐘後就好 —— 要看得出是哪一台 DC、哪個 IP 回的。"""
    _uid, dn = _ldap_user("judy", enabled=1, logged_in=True)
    _directory_says_ok(monkeypatch, dn, fail_with=Exception(
        "80090308: LdapErr: DSID-0C09044E, comment: AcceptSecurityContext error, data 52e, v4563"))
    with pytest.raises(auth.AuthError):
        auth.authenticate("judy", PW, ip="192.0.2.9", realm="ldap")
    d = [e for e in events if e["event"] == "login_fail"][-1]["details"]
    assert d["reason"] == "ldap_bind_failed" and d["ad_reason"] == "密碼錯誤" and "elapsed_ms" in d
    assert d["dc"] == "ldaps://dc-02.example.test:636" and d["dc_ip"] == "192.0.2.22"


def test_success_also_records_which_dc_answered(ldap_backend, monkeypatch, events):
    """成功也記：同一個人一下失敗一下成功時，拿兩筆的 DC 一比就知道是不是問到不同台。"""
    _uid, dn = _ldap_user("liam", enabled=1, logged_in=True)
    _directory_says_ok(monkeypatch, dn)
    auth.authenticate("liam", PW, ip="192.0.2.9", realm="ldap")
    d = [e for e in events if e["event"] == "login_success"][-1]["details"]
    assert d["dc"] == "ldaps://dc-02.example.test:636" and d["dc_ip"] == "192.0.2.22"


def test_user_not_found_records_which_dc_searched(ldap_backend, monkeypatch, events):
    def not_found(u, c):
        e = auth_ldap.UserNotFoundError("找不到使用者")
        e.dc_info = {"dc": "ldap://dc-01.example.test:389", "dc_ip": "192.0.2.21"}
        raise e
    monkeypatch.setattr(auth_ldap, "_search_ldap_user", not_found)
    with pytest.raises(auth.AuthError):
        auth.authenticate("ghost", PW, ip="192.0.2.9", realm="ldap")
    d = [e for e in events if e["event"] == "login_fail"][-1]["details"]
    assert d["dc"] == "ldap://dc-01.example.test:389" and d["dc_ip"] == "192.0.2.21"


def test_proxy_sso_failure_reasons(monkeypatch, events):
    from app.core import proxy_sso
    for exc, want in ((auth_ldap.UserNotFoundError("x"), "ldap_user_not_found"),
                      (auth_ldap.AccountDisabledError("x"), "disabled_in_jtdt"),
                      (auth_ldap.AuthError("無法連線/查詢 LDAP：所有 LDAP 伺服器都連不上"), "ldap_unavailable")):
        def boom(u, ip="", _e=exc):
            raise _e
        monkeypatch.setattr(auth_ldap, "sync_user_by_username", boom)
        monkeypatch.setattr(proxy_sso, "_cfg", lambda: {"enabled": True, "header": "X-Remote-User",
                                                        "trusted_proxies": ["127.0.0.1"]})

        class _Req:
            headers = {"X-Remote-User": "alice@EXAMPLE.TEST"}
            client = type("C", (), {"host": "127.0.0.1"})()
            scope = {"client": ("127.0.0.1", 1)}
        try:
            proxy_sso.resolve_user(_Req())
        except Exception:
            pass
    reasons = [e["details"]["reason"] for e in events if e["event"] == "proxy_sso_login_fail"]
    assert reasons[-3:] == ["ldap_user_not_found", "disabled_in_jtdt", "ldap_unavailable"], reasons


def test_no_password_ever_reaches_the_audit(ldap_backend, monkeypatch, events):
    _uid, dn = _ldap_user("kate", enabled=1, logged_in=True)
    _directory_says_ok(monkeypatch, dn, fail_with=Exception("invalidCredentials"))
    with pytest.raises(auth.AuthError):
        auth.authenticate("kate", PW, ip="192.0.2.9", realm="ldap")
    dump = json.dumps(events, ensure_ascii=False, default=str)
    assert PW not in dump and "svc-secret" not in dump


def test_dc_is_not_recorded_when_no_dc_answered():
    """連線池連上之前 `conn.server` 已經有值；全部連不上時不可以記成「這台回的」。"""
    import socket
    from ldap3 import Server, ServerPool, Connection, FIRST
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()   # 沒人聽的埠
    pool = ServerPool([Server("127.0.0.1", port=port, connect_timeout=1)], FIRST, active=1, exhaust=1)
    conn = Connection(pool, user="cn=x", password="y", auto_bind=False, raise_exceptions=True)
    with pytest.raises(Exception):
        conn.open(read_server_info=False)
    assert auth_ldap._dc_label(conn) == {}


def test_dc_label_reads_the_server_the_pool_actually_used():
    """真的 ldap3 物件：第一台連不上、第二台連上 → 記第二台，而且有實際 IP。"""
    import socket, threading
    from ldap3 import Server, ServerPool, Connection, FIRST
    ls = socket.socket(); ls.bind(("127.0.0.1", 0)); ls.listen(2); live = ls.getsockname()[1]
    threading.Thread(target=ls.accept, daemon=True).start()
    d = socket.socket(); d.bind(("127.0.0.1", 0)); dead = d.getsockname()[1]; d.close()
    pool = ServerPool([Server("127.0.0.1", port=dead, connect_timeout=1),
                       Server("localhost", port=live, connect_timeout=1)], FIRST, active=1, exhaust=10)
    conn = Connection(pool, user="cn=x", password="y", auto_bind=False, raise_exceptions=True)
    try:
        conn.open(read_server_info=False)
        assert auth_ldap._dc_label(conn) == {"dc": f"ldap://localhost:{live}", "dc_ip": "127.0.0.1"}
    finally:
        conn.unbind(); ls.close()
