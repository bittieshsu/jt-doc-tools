"""LDAP / AD 登入路徑資安回歸測試（審查後補）。

涵蓋三個修正：
1. 空密碼繞過（CRITICAL）：`auth_ldap.authenticate` 空 username / password 一律拒，
   絕不進 LDAP simple bind（否則 OpenLDAP / Univention 會當匿名 bind 回成功）。
2. 暴力破解鎖定（HIGH）：LDAP / AD 路徑重用本機 lockout；連續失敗達門檻即鎖。
3. lockout 共用 API：precheck / record_fail / clear 行為正確。
"""
from __future__ import annotations

import pytest

from app.core import auth, auth_ldap, auth_local, auth_settings


@pytest.fixture(autouse=True)
def _restore_auth_state():
    """快照 + 還原 backend / lockout 設定，並清空共用 lockouts 表，避免這些
    測試調門檻 / 留鎖定 state 洩漏到其他測試（共用 settings JSON + auth DB）。"""
    from app.core import auth_db, db as _db
    keys = ["backend", "ldap", "lockout_enabled", "lockout_window_minutes",
            "lockout_threshold", "lockout_ip_threshold", "lockout_minutes"]
    saved = {k: auth_settings.get().get(k) for k in keys}
    yield
    s = auth_settings.get()
    s.update(saved)
    auth_settings.save(s)
    try:
        conn = auth_db.conn()
        with _db.tx(conn):
            conn.execute("DELETE FROM lockouts")
    except Exception:
        pass


def _set_ldap_backend():
    s = auth_settings.get()
    s["backend"] = "ldap"
    s["lockout_enabled"] = True
    s["lockout_threshold"] = 3
    s["lockout_ip_threshold"] = 50
    s["lockout_minutes"] = 15
    s["lockout_window_minutes"] = 10
    auth_settings.save(s)


# ── 1. 空密碼 / 空帳號一律拒（不進 bind）──────────────────────────────

@pytest.mark.parametrize("user,pw", [("alice", ""), ("", "secret"), ("", "")])
def test_empty_credentials_rejected_before_bind(auth_off, user, pw):
    # backend 不必是 ldap：guard 在 backend 解析之前。呼叫不應嘗試任何連線。
    with pytest.raises(auth_ldap.AuthError):
        auth_ldap.authenticate(user, pw, ip="1.2.3.4")


def test_empty_password_does_not_reach_ldap(auth_off, monkeypatch):
    """空密碼時，絕不呼叫 _search_ldap_user（即不進目錄查詢 / bind）。"""
    called = {"n": 0}

    def _boom(*a, **k):
        called["n"] += 1
        raise AssertionError("空密碼不該進到 LDAP 查詢")

    monkeypatch.setattr(auth_ldap, "_search_ldap_user", _boom)
    with pytest.raises(auth_ldap.AuthError):
        auth_ldap.authenticate("alice", "", ip="1.2.3.4")
    assert called["n"] == 0


# ── 2. LDAP / AD 路徑的暴力破解鎖定（經 dispatcher）─────────────────────

def test_ldap_path_locks_out_after_threshold(auth_off, monkeypatch):
    _set_ldap_backend()
    # 讓 auth_ldap.authenticate 一律「密碼錯誤」（模擬 bind 失敗）
    monkeypatch.setattr(auth_ldap, "authenticate",
                        lambda u, p, ip="": (_ for _ in ()).throw(auth_ldap.AuthError("帳號或密碼錯誤")))
    # 前 3 次：一般失敗；第 3 次跨門檻後，第 4 次應變成「鎖定」訊息
    for _ in range(3):
        with pytest.raises(auth.AuthError) as ei:
            auth.authenticate("bob", "wrong", ip="9.9.9.9", realm="ldap")
        assert "帳號或密碼錯誤" in str(ei.value)
    with pytest.raises(auth.AuthError) as ei:
        auth.authenticate("bob", "wrong", ip="9.9.9.9", realm="ldap")
    assert "嘗試次數過多" in str(ei.value)


def test_ldap_success_clears_lockout(auth_off, monkeypatch):
    _set_ldap_backend()
    # 先失敗 2 次（未達門檻 3）
    monkeypatch.setattr(auth_ldap, "authenticate",
                        lambda u, p, ip="": (_ for _ in ()).throw(auth_ldap.AuthError("帳號或密碼錯誤")))
    for _ in range(2):
        with pytest.raises(auth.AuthError):
            auth.authenticate("carol", "wrong", ip="8.8.8.8", realm="ldap")
    # 然後成功登入 → 清掉計數
    monkeypatch.setattr(auth_ldap, "authenticate",
                        lambda u, p, ip="": {"user_id": 1, "username": "carol", "source": "ldap"})
    assert auth.authenticate("carol", "right", ip="8.8.8.8", realm="ldap")["username"] == "carol"
    # 清空後再失敗 3 次才會鎖（而非再 1 次就鎖）
    monkeypatch.setattr(auth_ldap, "authenticate",
                        lambda u, p, ip="": (_ for _ in ()).throw(auth_ldap.AuthError("x")))
    with pytest.raises(auth.AuthError) as ei:
        auth.authenticate("carol", "wrong", ip="8.8.8.8", realm="ldap")
    assert "嘗試次數過多" not in str(ei.value)   # 計數已被清，這是第 1 次


# ── 3. lockout 共用 API 直接測 ───────────────────────────────────────

def test_lockout_api_precheck_record_clear(auth_off):
    s = auth_settings.get()
    s["lockout_enabled"] = True
    s["lockout_threshold"] = 2
    s["lockout_ip_threshold"] = 50
    auth_settings.save(s)
    # 未達門檻 → precheck 不拋
    auth_local.lockout_precheck("dave", "7.7.7.7")
    auth_local.lockout_record_fail("dave", "7.7.7.7")
    auth_local.lockout_precheck("dave", "7.7.7.7")   # 1 次，還沒鎖
    auth_local.lockout_record_fail("dave", "7.7.7.7")  # 第 2 次 → 鎖
    with pytest.raises(auth_local.AuthError):
        auth_local.lockout_precheck("dave", "7.7.7.7")
    # clear 後解鎖
    auth_local.lockout_clear("dave", "7.7.7.7")
    auth_local.lockout_precheck("dave", "7.7.7.7")   # 不拋


def test_lockout_disabled_never_locks(auth_off):
    s = auth_settings.get()
    s["lockout_enabled"] = False
    auth_settings.save(s)
    for _ in range(10):
        auth_local.lockout_record_fail("eve", "6.6.6.6")
    auth_local.lockout_precheck("eve", "6.6.6.6")   # 停用 → 永不鎖


# ── 4. 鏡射帳號不變量：sync_all_users 的 UPDATE 路徑絕不動 enabled ─────────
#    （v1.12.70「鏡射 ≠ 啟用」；回歸：舊版 displayName 變動時會 enabled=1）

class _FakeStd:
    def __init__(self, entries):
        self._entries = entries
    def paged_search(self, *a, **k):
        return self._entries

class _FakeExtend:
    def __init__(self, entries):
        self.standard = _FakeStd(entries)

class _FakeConn:
    def __init__(self, entries):
        self.extend = _FakeExtend(entries)
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


def test_sync_all_users_update_does_not_reenable(auth_off, monkeypatch):
    from app.core import auth_db, auth_settings, auth_ldap
    import ldap3
    # 設 ldap backend + 必要 cfg
    s = auth_settings.get()
    s["backend"] = "ldap"
    s["ldap"] = {"service_dn": "cn=svc,dc=x", "service_password": "pw",
                 "user_search_base": "ou=Users,dc=x",
                 "displayname_attr": "displayName", "username_attr": "sAMAccountName"}
    auth_settings.save(s)
    # 先塞一個「已去啟用的鏡射帳號」（enabled=0、從未登入、名字 Old）
    dn = "uid=frank,ou=Users,dc=x"
    conn = auth_db.conn()
    from app.core import db as _db
    with _db.tx(conn):
        import time as _t
        conn.execute(
            "INSERT INTO users(username, display_name, source, external_dn, enabled, "
            "created_at, last_login_at) VALUES (?,?,?,?,0,?,0)",
            ("frank", "Old Name", "ldap", dn, _t.time()))
    # 目錄端回傳同 DN、但 displayName 改成 New Name → 觸發 UPDATE 路徑
    entries = [{"dn": dn, "type": "searchResEntry",
                "attributes": {"displayName": "New Name", "sAMAccountName": "frank"}}]
    monkeypatch.setattr(auth_ldap, "_build_server", lambda cfg: object())
    monkeypatch.setattr(ldap3, "Connection", lambda *a, **k: _FakeConn(entries))
    res = auth_ldap.sync_all_users()
    assert res["updated"] == 1
    row = auth_db.conn().execute(
        "SELECT display_name, enabled, last_login_at FROM users WHERE external_dn=?",
        (dn,)).fetchone()
    assert row["display_name"] == "New Name"   # 名字有更新
    assert row["enabled"] == 0                 # 但**絕不**被重新啟用
    assert row["last_login_at"] == 0           # 仍「從未登入」
