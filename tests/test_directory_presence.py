"""目錄裡已經找不到的帳號要看得出來（離職 / 停用偵測）。

## 由來

客戶反映「AD 帳號管理還有精進空間」。盤點後最嚴重的一項是：目錄同步**只增不減**
—— AD 那邊把人停用或刪掉之後，本站的帳號列、角色指派、群組成員關係全部還在。
那個人登不進來（LDAP bind 會失敗），但「這個系統裡還有誰」這個問題答不出來，
內控盤點與離職交接都對不起來。

## 判定基準：**只有完整掃描才算數**

`directory_seen_at` 記錄「最後一次同步看到這個帳號」的時間。同步時帶了名稱過濾就
只看得到一部分目錄 —— 拿那次去推論「其他人都不見了」會把**整個組織誤標成離職**。
所以判定要相對於 `last_full_scan_at`，而且沒有做過完整掃描時一律不下結論
（寧可什麼都不顯示）。

## 絕不可以誤傷

本機帳號與 SSO 帳號本來就不在 AD 裡，標成「離職」會是徹底的誤報。
"""
from __future__ import annotations

import time

import pytest

from app.core import auth_db, db, user_manager


def _mk(conn, username, source, dn, seen_at, *, enabled=1):
    conn.execute(
        "INSERT INTO users(username, display_name, source, external_dn, enabled, "
        "is_admin_seed, created_at, directory_seen_at) VALUES (?,?,?,?,?,0,?,?)",
        (username, username, source, dn, enabled, time.time(), seen_at))


@pytest.fixture
def seeded(auth_off, monkeypatch):
    """一份含各種來源的使用者表 + 一個可控的『上次完整掃描時間』。"""
    auth_db.init()
    scan_at = time.time()
    conn = auth_db.conn()
    with db.tx(conn):
        conn.execute("DELETE FROM users")
        # 這次掃描有看到 → 還在
        _mk(conn, "alice", "ldap", "cn=alice,ou=rd,dc=x", scan_at)
        # 掃描前就沒更新過 → 目錄裡找不到了
        _mk(conn, "bob", "ldap", "cn=bob,ou=rd,dc=x", scan_at - 86400)
        # 從沒被同步涵蓋過（只登入過）→ 也算找不到
        _mk(conn, "carol", "ad", "cn=carol,ou=rd,dc=x", None)
        # 本機帳號：不在 AD 裡本來就正常，**絕不可以**被標成離職
        _mk(conn, "localadm", "local", None, None)
        # SSO 帳號：同理
        _mk(conn, "ssouser", "oidc", "sub-123", None)
    monkeypatch.setattr(user_manager, "last_full_directory_scan_at",
                        lambda: scan_at)
    return scan_at


def _names(view: str) -> set[str]:
    return {u["username"] for u in user_manager.list_users(view)}


def test_missing_view_lists_only_directory_accounts_not_seen(seeded):
    assert _names("missing") == {"bob", "carol"}


def test_local_and_sso_accounts_are_never_flagged(seeded):
    """本機與 SSO 帳號不在 AD 裡是正常的 —— 標成離職是徹底的誤報。"""
    flagged = {u["username"] for u in user_manager.list_users("all")
               if u["directory_missing"]}
    assert "localadm" not in flagged
    assert "ssouser" not in flagged
    assert flagged == {"bob", "carol"}


def test_no_full_scan_yet_means_no_conclusion(auth_off, monkeypatch):
    """沒有做過完整掃描時，一個都不可以標 —— 寧可什麼都不顯示。

    帶名稱過濾的同步只看得到一部分目錄，若拿它當基準，**整個組織都會被標成
    離職**，那比沒有這個功能還糟。
    """
    auth_db.init()
    conn = auth_db.conn()
    with db.tx(conn):
        conn.execute("DELETE FROM users")
        _mk(conn, "bob", "ldap", "cn=bob,dc=x", None)
    monkeypatch.setattr(user_manager, "last_full_directory_scan_at", lambda: 0.0)
    assert _names("missing") == set()
    assert all(not u["directory_missing"] for u in user_manager.list_users("all"))


def test_filtered_sync_does_not_update_the_baseline():
    """帶了 name_contains 的同步**不可以**更新 last_full_scan_at。

    更新了的話，下一次判定就會以「只看了一部分目錄」那次為基準，把沒被過濾到的
    人全部標成離職。
    """
    import inspect

    from app.core import auth_ldap, directory_sync
    src = inspect.getsource(auth_ldap.sync_all_users)
    assert "full_scan = not nc" in src, "沒有區分完整掃描與過濾掃描"
    stamp = inspect.getsource(directory_sync._stamp)
    assert 'us.get("full_scan")' in stamp, \
        "_stamp 沒有檢查 full_scan 就更新了基準時間"


def test_login_refreshes_presence():
    """人剛用目錄帳號登入成功 = 他顯然還在目錄裡，要更新 directory_seen_at。

    少了這一段，「上次完整掃描之後才回來的人」會一直被誤標成離職，直到下一次
    同步才恢復。
    """
    import inspect

    from app.core import auth_ldap
    src = inspect.getsource(auth_ldap._sync_user)
    assert src.count("directory_seen_at") >= 2, \
        "登入時沒有更新 directory_seen_at（已存在與新建兩條路徑都要）"


def test_sync_records_failure_details():
    """群組成員數同步失敗時要記下是哪個群組、什麼原因。

    原本 exception 被整個吞掉、連 log 都沒寫，管理員只看得到「失敗 37 筆」。
    """
    import inspect

    from app.core import directory_sync
    src = inspect.getsource(directory_sync.run_sync)
    assert "failed_detail" in src, "同步失敗沒有記錄明細"
    assert "logger.warning" in src or "logger.exception" in src, \
        "同步失敗連 log 都沒寫"


def test_sync_keeps_history():
    """只留上一次結果的話，看不出「從什麼時候開始失敗的」。"""
    import inspect

    from app.core import directory_sync
    assert directory_sync._HISTORY_KEEP >= 5
    assert "last_history" in inspect.getsource(directory_sync._stamp)


# ------------------------------------------------------ 故障的可觀測性

def test_ldap_outage_is_audited_and_not_leaked_to_the_user():
    """目錄連不上時：細節要進稽核，畫面只給通用訊息。

    原本 `ldap3.core.exceptions.LDAPSocket…` 這種原始例外會一路顯示給**一般
    使用者**，而稽核記錄裡**一筆都沒有** —— service account 密碼一過期就是全公司
    登不進來，管理員事後查不到任何線索。
    """
    import inspect

    from app.core import auth_ldap
    src = inspect.getsource(auth_ldap.authenticate)
    assert "ldap_unavailable" in src, "目錄連不上時沒有寫稽核"
    assert "無法連線到認證伺服器" in src, "沒有換成通用訊息"
    # 原始例外不可以出現在丟給使用者的訊息裡
    i = src.index("ldap_unavailable")
    tail = src[i:i + 600]
    assert "AuthError(f\"" not in tail, "又把格式化過的原始錯誤丟給使用者了"


def test_bind_failure_records_the_real_reason():
    """AD bind 失敗的真正原因要記進稽核（密碼錯 / 帳號鎖 / 密碼過期分得出來）。"""
    from app.core.auth_ldap import _bind_failure_detail

    e = Exception("80090308: LdapErr: ..., AcceptSecurityContext error, "
                  "data 775, v4563")
    d = _bind_failure_detail(e)
    assert d["ad_sub_status"] == "775"
    assert "鎖定" in d["ad_reason"]

    e2 = Exception("... data 532, v4563")
    assert "過期" in _bind_failure_detail(e2)["ad_reason"]

    # 認不出來的也不可以炸
    assert _bind_failure_detail(Exception("something else"))["error_class"]


def test_sync_failure_notifies():
    """同步壞掉要主動通知 —— 原本只有 logger，要有人去開頁面才發現得了。"""
    import inspect

    from app.core import directory_sync
    src = inspect.getsource(directory_sync)
    assert "_notify_if_degraded" in src
    assert "notify_channels" in src, "同步失敗沒有接上通知管道"
    # 通知失敗不可以把一次成功的同步標記成失敗
    fn = inspect.getsource(directory_sync._notify_if_degraded)
    assert "except Exception" in fn and "logger.warning" in fn
