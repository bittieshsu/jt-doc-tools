"""AD 端的帳號狀態：已停用偵測 + 密碼到期預警。

## 由來

客戶反映「AD 帳號管理還有精進空間」。盤點出兩個管理員看不見的狀態：

* **AD 端已停用**：那個人登不進來（bind 會失敗），但本站的帳號、角色指派、群組
  成員關係全部還在，清單上跟正常人長得一模一樣。內控盤點對不起來。
* **密碼即將到期**：到期當下對使用者的症狀是「突然登不進來」，然後來問管理員 ——
  而管理員手上沒有任何資訊可以事先提醒。

## 兩個必須守住的判斷

* **`dir_disabled` 是三態**：True 停用 / False 正常 / **None 不知道**。
  OpenLDAP、本機、SSO 帳號沒有這個概念，一律 None。把 None 當成「正常」等於
  替目錄做了它沒說過的保證。
* **不可以用網域的 `maxPwdAge` 自己算**：它忽略細緻密碼原則（PSO），也忽略
  DONT_EXPIRE_PASSWD 旗標 —— 對套了 PSO 的人算出來的日期是錯的。要用 AD 自己
  算好的構造屬性 `msDS-UserPasswordExpiryTimeComputed`。
"""
from __future__ import annotations

import inspect
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.core import auth_db, auth_ldap, db, user_manager


def _filetime(dt: datetime) -> int:
    """Unix 時間 → AD FILETIME（1601 起算的 100 奈秒）。"""
    return int((dt.timestamp() + 11644473600.0) * 10_000_000)


# ------------------------------------------------------ userAccountControl

@pytest.mark.parametrize("uac,expect", [
    (514, True),        # 512 NORMAL_ACCOUNT + 2 ACCOUNTDISABLE
    (512, False),       # 一般啟用帳號
    (66050, True),      # 停用 + DONT_EXPIRE_PASSWD
    (66048, False),     # 啟用 + DONT_EXPIRE_PASSWD
    ("514", True),      # ldap3 可能回字串
    ([514], True),      # 也可能回 list
])
def test_uac_disabled_bit(uac, expect):
    assert auth_ldap.uac_disabled(uac) is expect


@pytest.mark.parametrize("bad", [None, [], "", "abc", True, {}])
def test_uac_unknown_is_none_not_false(bad):
    """**這是最關鍵的一條**：取不到就是「不知道」。

    回 False 會被讀成「已確認為啟用」—— OpenLDAP 沒有這個屬性，那樣全體帳號都會
    被標成「已確認正常」，是無中生有的結論。
    """
    assert auth_ldap.uac_disabled(bad) is None


# ------------------------------------------------------ FILETIME

def test_filetime_roundtrip():
    want = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    got = auth_ldap.filetime_to_unix(_filetime(want))
    assert abs(got - want.timestamp()) < 2


@pytest.mark.parametrize("never", [0, 0x7FFFFFFFFFFFFFFF, "0",
                                   "9223372036854775807"])
def test_never_expires_is_none(never):
    """0 與 0x7FFF… 都代表「不會到期」—— 不可以畫成 1601 年或 30828 年的日期。"""
    assert auth_ldap.filetime_to_unix(never) is None


def test_datetime_passthrough():
    """ldap3 有時已經依 schema 轉成 datetime 了。"""
    want = datetime(2026, 9, 1, tzinfo=timezone.utc)
    assert abs(auth_ldap.filetime_to_unix(want) - want.timestamp()) < 2


@pytest.mark.parametrize("bad", [None, [], "", "abc", -5, {}])
def test_bad_filetime_is_none(bad):
    assert auth_ldap.filetime_to_unix(bad) is None


# ------------------------------------------------------ 同步取值

def test_sync_requests_the_constructed_attribute():
    """`msDS-UserPasswordExpiryTimeComputed` **一定要顯式列在 attributes**。

    它是構造屬性，`*` 不會回傳 —— 漏了就永遠是空的，而且不會有任何錯誤，
    症狀是「功能做好了但一直沒有資料」。
    """
    src = inspect.getsource(auth_ldap.sync_all_users)
    assert '"userAccountControl"' in src
    assert '"msDS-UserPasswordExpiryTimeComputed"' in src


def test_sync_writes_both_columns():
    src = inspect.getsource(auth_ldap.sync_all_users)
    assert "dir_disabled=?, pwd_expires_at=?" in src, "既有帳號沒有更新目錄狀態"
    assert "dir_disabled, pwd_expires_at" in src, "新建帳號沒有帶入目錄狀態"


def test_sync_clears_state_when_it_goes_away():
    """停用之後又在 AD 端啟用回來的人，狀態要跟著回正常。

    只在「有值時才寫」的話，`dir_disabled=1` 會永遠黏著。
    """
    src = inspect.getsource(auth_ldap.sync_all_users)
    i = src.index("dir_disabled=?, pwd_expires_at=?")
    ctx = src[max(0, i - 400):i + 300]
    assert "None if disabled is None else int(disabled)" in ctx, \
        "沒有把「不知道」寫回 NULL"


def test_maxpwdage_is_not_used():
    """網域的 `maxPwdAge` 對套了 PSO 的人會算錯 —— 不可以拿它自己算。"""
    src = Path(auth_ldap.__file__).read_text(encoding="utf-8")
    assert "maxPwdAge" not in src


# ------------------------------------------------------ 呈現

def _mk(conn, username, source="ad", *, disabled=None, exp=None):
    conn.execute(
        "INSERT INTO users(username, display_name, source, external_dn, enabled, "
        "is_admin_seed, created_at, dir_disabled, pwd_expires_at) "
        "VALUES (?,?,?,?,1,0,?,?,?)",
        (username, username, source, f"cn={username},dc=x", time.time(),
         disabled, exp))


@pytest.fixture
def seeded(auth_off):
    auth_db.init()
    now = time.time()
    conn = auth_db.conn()
    with db.tx(conn):
        conn.execute("DELETE FROM users")
        _mk(conn, "normal", disabled=0, exp=now + 90 * 86400)
        _mk(conn, "gone", disabled=1, exp=now + 90 * 86400)
        _mk(conn, "soon", disabled=0, exp=now + 3 * 86400)
        _mk(conn, "expired", disabled=0, exp=now - 86400)
        _mk(conn, "nopolicy", disabled=0, exp=None)     # 密碼永久有效
        _mk(conn, "localadm", source="local")           # 沒有這些概念
    return now


def _by(name, view="all"):
    return next(u for u in user_manager.list_users(view) if u["username"] == name)


def test_disabled_flag_surfaces(seeded):
    assert _by("gone")["dir_disabled"] is True
    assert _by("normal")["dir_disabled"] is False


def test_local_account_state_is_unknown_not_normal(seeded):
    """本機帳號沒有目錄狀態 —— 一律「不知道」。"""
    assert _by("localadm")["dir_disabled"] is None
    assert _by("localadm")["pwd_expires_at"] is None
    assert _by("localadm")["pwd_expiring_soon"] is False


def test_expiry_warning_window(seeded):
    assert _by("soon")["pwd_expiring_soon"] is True
    assert _by("normal")["pwd_expiring_soon"] is False
    assert _by("nopolicy")["pwd_expiring_soon"] is False


def test_already_expired_still_warns(seeded):
    """已經過期的人現在正卡在改密碼畫面前 —— 最需要被看到的就是他。"""
    u = _by("expired")
    assert u["pwd_expiring_soon"] is True
    assert u["pwd_expiry_days"] < 0


def test_views_filter(seeded):
    assert {u["username"] for u in user_manager.list_users("dir_disabled")} == {"gone"}
    assert {u["username"] for u in user_manager.list_users("pwd_expiring")} \
        == {"soon", "expired"}


def test_paged_view_agrees_with_list(seeded):
    """分頁那條路徑也要吃得到新檢視 —— 兩條路徑不一致是最難查的那種 bug。"""
    for v in ("dir_disabled", "pwd_expiring"):
        page = user_manager.list_users_page(view=v)
        assert page["total"] == len(user_manager.list_users(v))
        assert {r["username"] for r in page["rows"]} == \
            {u["username"] for u in user_manager.list_users(v)}


def test_paged_rows_carry_the_state(seeded):
    row = next(r for r in user_manager.list_users_page(view="all")["rows"]
               if r["username"] == "gone")
    assert row["dir_disabled"] is True


def test_warn_window_is_sane():
    assert 3 <= user_manager.PWD_WARN_DAYS <= 30


# ------------------------------------------------------ 畫面

def _tpl() -> str:
    return (Path(user_manager.__file__).resolve().parent.parent / "admin" /
            "templates" / "admin_users.html").read_text(encoding="utf-8")


def test_badges_are_rendered():
    tpl = _tpl()
    assert "AD 已停用" in tpl
    assert "uf-pwdexp" in tpl


def test_expired_and_expiring_read_differently():
    """「密碼 -3 天後到期」是壞掉的字串 —— 過期要換句話說。"""
    tpl = _tpl()
    assert "密碼已過期" in tpl


def test_view_pills_hide_when_zero():
    """不是 AD 環境的話這兩個永遠是 0，不該一直佔著版面。"""
    tpl = _tpl()
    assert "{% if dir_disabled_count or view == 'dir_disabled' %}" in tpl
    assert "{% if pwd_expiring_count or view == 'pwd_expiring' %}" in tpl


def test_router_accepts_the_new_views():
    src = (Path(user_manager.__file__).resolve().parent.parent / "admin" /
           "auth_router.py").read_text(encoding="utf-8")
    i = src.index('view = (qp.get("view")')
    ctx = src[i:i + 300]
    assert "dir_disabled" in ctx and "pwd_expiring" in ctx, \
        "路由沒有放行新檢視 —— 點頁籤會被打回 active，看起來像沒反應"
