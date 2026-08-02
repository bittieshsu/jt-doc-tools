"""批次停用「目錄已無 / AD 端已停用」的帳號，以及排程自動停用。

## 由來

客戶反映「AD 帳號管理還有精進空間」。目錄同步只增不減，離職的人在本站的帳號、
角色、群組全都還在。看得到（前一輪做了）之後，下一步是要能**處理**：

* 手動：「目錄已無」可能好幾百人橫跨十幾頁，只能勾當前頁的話實務上沒有人會去做。
* 排程：每次同步後自動處理。

## 安全閥是這一份的重點

自動停用是破壞性的，而且錯的時候一次錯一大片：service account 密碼過期、搜尋
base DN 被改、目錄只回了一部分 —— 每一種都會讓「所有人都不見了」。所以：

1. 一次最多動目錄帳號總數的 20%，超過就**整批中止**（不是只做前 20%）。
2. 絕不動 seed 管理員；停用後至少留一個啟用中的管理員。
3. 只在**完整**掃描之後才跑（帶名稱過濾的同步不算）。
4. 只停用不刪除 —— 人回來時按一下就恢復。
"""
from __future__ import annotations

import inspect
import time
from pathlib import Path

import pytest

from app.core import (auth_db, db, directory_cleanup, directory_sync, roles,
                      user_manager)


def _mk(conn, username, *, enabled=1, seen=None, disabled=None, source="ad"):
    cur = conn.execute(
        "INSERT INTO users(username, display_name, source, external_dn, enabled, "
        "is_admin_seed, created_at, directory_seen_at, dir_disabled) "
        "VALUES (?,?,?,?,?,0,?,?,?)",
        (username, username, source, f"cn={username},dc=x", enabled,
         time.time(), seen, disabled))
    return cur.lastrowid


@pytest.fixture
def env(auth_off, monkeypatch):
    auth_db.init()
    roles.seed_builtin_roles()
    conn = auth_db.conn()
    with db.tx(conn):
        conn.execute("DELETE FROM users")
    monkeypatch.setattr(user_manager, "last_full_directory_scan_at",
                        lambda: time.time())
    return conn


def _seed_missing(conn, n_missing: int, n_present: int):
    """n_missing 個「目錄已無」+ n_present 個正常的（安全閥的分母）。"""
    old = time.time() - 86400
    now = time.time() + 60
    with db.tx(conn):
        for i in range(n_missing):
            _mk(conn, f"gone{i}", seen=old)
        for i in range(n_present):
            _mk(conn, f"here{i}", seen=now)


# ------------------------------------------------------------ 基本行為

def test_disables_the_missing_accounts(env):
    _seed_missing(env, 2, 50)
    res = directory_cleanup.disable_view("missing")
    assert res["ok"] and not res["aborted"]
    assert res["disabled"] == 2
    assert {u["username"] for u in user_manager.list_users("all")
            if not u["enabled"]} == {"gone0", "gone1"}


def test_accounts_and_permissions_survive(env):
    """只停用不刪除 —— 人回來時管理員按一下就恢復。"""
    _seed_missing(env, 1, 50)
    uid = next(u["id"] for u in user_manager.list_users("all")
               if u["username"] == "gone0")
    from app.core import permissions
    permissions.set_subject_roles("user", str(uid), ["clerk"])
    directory_cleanup.disable_view("missing")
    assert user_manager.get_by_id(uid) is not None
    assert "clerk" in (user_manager.get_by_id(uid)["roles"] or [])


def test_already_disabled_are_not_counted(env):
    """已經停用的不該再算進去 —— 會讓安全閥的分子虛胖，也讓回報數字騙人。"""
    conn = env
    with db.tx(conn):
        _mk(conn, "old", enabled=0, seen=time.time() - 86400)
        for i in range(50):
            _mk(conn, f"here{i}", seen=time.time() + 60)
    res = directory_cleanup.disable_view("missing")
    assert res["candidates"] == 0
    assert res["disabled"] == 0


def test_dry_run_changes_nothing(env):
    _seed_missing(env, 2, 50)
    res = directory_cleanup.disable_view("missing", dry_run=True)
    assert res["candidates"] == 2
    assert res["disabled"] == 0
    assert all(u["enabled"] for u in user_manager.list_users("all"))


def test_dir_disabled_view(env):
    conn = env
    with db.tx(conn):
        _mk(conn, "addis", disabled=1, seen=time.time() + 60)
        for i in range(50):
            _mk(conn, f"here{i}", disabled=0, seen=time.time() + 60)
    res = directory_cleanup.disable_view("dir_disabled")
    assert res["disabled"] == 1


@pytest.mark.parametrize("view", ["", "all", "active", "; DROP TABLE users",
                                  "directory"])
def test_unknown_view_does_nothing(env, view):
    """**不可以開放任意檢視** —— 那等於讓呼叫端決定要停用誰（例如 all）。"""
    _seed_missing(env, 2, 50)
    res = directory_cleanup.disable_view(view)
    assert res["disabled"] == 0
    assert res["aborted"]
    assert all(u["enabled"] for u in user_manager.list_users("all"))


# ------------------------------------------------------------ 安全閥

def test_safety_valve_aborts_the_whole_batch(env):
    """超過 20% 時要**整批中止**，不是只做前 20%。

    真正的情境是「同步本身壞了」—— 那時候動任何一個人都是錯的。
    """
    _seed_missing(env, 30, 70)     # 30/100 = 30% > 20%
    res = directory_cleanup.disable_view("missing")
    assert res["aborted"] and not res["ok"]
    assert res["disabled"] == 0
    assert all(u["enabled"] for u in user_manager.list_users("all")), \
        "中止了卻還是動了人"
    assert "20%" in res["reason"]


def test_safety_valve_reason_is_actionable(env):
    """訊息要說得出「為什麼會這樣」，不然管理員只會直接 force。"""
    _seed_missing(env, 30, 70)
    reason = directory_cleanup.disable_view("missing")["reason"]
    assert "服務帳號" in reason or "搜尋範圍" in reason


def test_force_overrides_the_valve(env):
    _seed_missing(env, 30, 70)
    res = directory_cleanup.disable_view("missing", force=True)
    assert not res["aborted"]
    assert res["disabled"] == 30


def test_valve_uses_enabled_directory_accounts_as_the_denominator(env):
    """分母要是「目錄帳號」，不是全體帳號 —— 混進本機帳號會讓比例失真。"""
    src = inspect.getsource(directory_cleanup._directory_account_total)
    assert "source IN ('ldap','ad')" in src
    assert "enabled=1" in src


# ------------------------------------------------------------ 管理員保護

def test_never_touches_the_seed_admin(env):
    conn = env
    old = time.time() - 86400
    with db.tx(conn):
        conn.execute(
            "INSERT INTO users(username, display_name, source, external_dn, "
            "enabled, is_admin_seed, created_at, directory_seen_at) "
            "VALUES ('jtdt-admin','admin','ad','cn=a,dc=x',1,1,?,?)",
            (time.time(), old))
        for i in range(50):
            _mk(conn, f"here{i}", seen=time.time() + 60)
    res = directory_cleanup.disable_view("missing")
    assert res["disabled"] == 0
    assert any(s["reason"] == "內建管理員帳號" for s in res["skipped"])
    assert user_manager.get_by_id(
        next(u["id"] for u in user_manager.list_users("all")
             if u["username"] == "jtdt-admin"))["enabled"]


def test_aborts_rather_than_disable_the_last_admin(env):
    """把最後一個管理員關掉之後就只能用 CLI 救 —— 寧可整批不做。"""
    conn = env
    old = time.time() - 86400
    with db.tx(conn):
        uid = _mk(conn, "onlyadmin", seen=old)
        for i in range(50):
            _mk(conn, f"here{i}", seen=time.time() + 60)
    from app.core import permissions
    permissions.set_subject_roles("user", str(uid), ["admin"])
    permissions.invalidate_cache()
    res = directory_cleanup.disable_view("missing")
    assert res["aborted"]
    assert user_manager.get_by_id(uid)["enabled"]


# ------------------------------------------------------------ 排程

def test_scheduled_is_off_by_default():
    assert (directory_sync._DEFAULTS.get("auto_disable") or "off") == "off"


def test_scheduled_does_nothing_when_off(env):
    _seed_missing(env, 2, 50)
    assert directory_cleanup.run_scheduled(lambda: {"auto_disable": "off"},
                                           lambda p: None) is None
    assert all(u["enabled"] for u in user_manager.list_users("all"))


def test_scheduled_runs_when_configured(env):
    _seed_missing(env, 2, 50)
    saved = {}
    res = directory_cleanup.run_scheduled(
        lambda: {"auto_disable": "missing"}, lambda p: saved.update(p))
    assert res["disabled"] == 2
    assert "last_auto_disable" in saved, "結果沒有寫回設定，畫面上看不到"


def test_scheduled_rejects_unknown_values(env):
    """設定檔被改成別的字串時要當成關閉，不可以拿去當檢視名稱用。"""
    _seed_missing(env, 2, 50)
    assert directory_cleanup.run_scheduled(lambda: {"auto_disable": "all"},
                                           lambda p: None) is None
    assert all(u["enabled"] for u in user_manager.list_users("all"))


def test_settings_writer_whitelists(auth_off):
    s = directory_sync.save_settings(auto_disable="all")
    assert s["auto_disable"] == "off"
    s = directory_sync.save_settings(auto_disable="missing")
    assert s["auto_disable"] == "missing"
    directory_sync.save_settings(auto_disable="off")


def test_only_runs_after_a_full_scan():
    """帶名稱過濾的同步只看得到一部分目錄 —— 拿它當基準會把整個組織停用。"""
    src = inspect.getsource(directory_sync.run_sync)
    i = src.index("run_scheduled")
    ctx = src[max(0, i - 500):i]
    assert 'us.get("full_scan")' in ctx, "沒有確認這一次是完整掃描"


def test_runs_after_stamp_not_before():
    """判定基準（last_full_scan_at）是 `_stamp` 寫進去的。

    順序顛倒的話，會拿**上一次**的基準去判斷這一次的結果 —— 剛剛才同步到的人
    會被當成「找不到」。
    """
    src = inspect.getsource(directory_sync.run_sync)
    assert src.index("_stamp(ok=True") < src.index("run_scheduled")


def test_scheduler_failure_does_not_fail_the_sync():
    src = inspect.getsource(directory_sync.run_sync)
    i = src.index("run_scheduled")
    assert "except Exception" in src[i - 400:i + 400]


# ------------------------------------------------------------ 端點與畫面

def test_endpoint_exists_and_shares_the_same_logic():
    """兩邊各寫一份安全閥遲早會不一致 —— 端點要呼叫同一個模組。"""
    src = (Path(user_manager.__file__).resolve().parent.parent / "admin" /
           "auth_router.py").read_text(encoding="utf-8")
    assert '@router.post("/users/bulk/disable-view")' in src
    i = src.index("users_bulk_disable_view")
    assert "directory_cleanup.disable_view" in src[i:i + 900]


def test_abort_is_reported_as_200_with_a_reason():
    """中止要回得了原因。400 在前端只會被顯示成「操作失敗」。"""
    src = (Path(user_manager.__file__).resolve().parent.parent / "admin" /
           "auth_router.py").read_text(encoding="utf-8")
    i = src.index("users_bulk_disable_view")
    assert "status_code=200" in src[i:i + 1200]


def test_ui_asks_with_the_real_number():
    """按下去之前要先 dry-run 拿到「實際會動幾個人」再問。

    只問「確定嗎」而不說數字，等於沒有問。
    """
    tpl = (Path(user_manager.__file__).resolve().parent.parent / "admin" /
           "templates" / "admin_users.html").read_text(encoding="utf-8")
    i = tpl.index("依檢視全部停用")
    body = tpl[i:i + 2500]
    assert "dry_run: true" in body
    assert "pre.candidates" in body


def test_ui_surfaces_the_abort_reason():
    tpl = (Path(user_manager.__file__).resolve().parent.parent / "admin" /
           "templates" / "admin_users.html").read_text(encoding="utf-8")
    i = tpl.index("依檢視全部停用")
    assert "pre.aborted" in tpl[i:i + 2500]


def test_group_page_defaults_to_off():
    tpl = (Path(user_manager.__file__).resolve().parent.parent / "admin" /
           "templates" / "admin_groups.html").read_text(encoding="utf-8")
    i = tpl.index('id="dsAutoDisable"')
    first_option = tpl[i:i + 300]
    assert first_option.index('value="off"') < first_option.index('value="missing"'), \
        "自動停用不是預設選項 —— 破壞性操作不可以放在第一個"


def test_button_counts_only_what_it_will_actually_disable():
    """按鈕上的數字要是「還啟用中的人數」，不是這個檢視的總筆數。

    用總筆數的話，停用完之後按鈕還寫著「共 3 人」，再按一次卻什麼都沒發生 ——
    在真實瀏覽器上實測時就是這個症狀。
    """
    src = (Path(user_manager.__file__).resolve().parent.parent / "admin" /
           "auth_router.py").read_text(encoding="utf-8")
    assert "_dcl.candidates(v)" in src, "沒有算出實際會動到的人數"

    tpl = (Path(user_manager.__file__).resolve().parent.parent / "admin" /
           "templates" / "admin_users.html").read_text(encoding="utf-8")
    i = tpl.index('uf-disable-all" data-view="missing"')
    ctx = tpl[max(0, i - 200):i + 200]
    assert "actionable.missing" in ctx
    assert "missing_count" not in ctx, "按鈕還在用檢視總筆數"


def test_button_disappears_when_there_is_nothing_to_do():
    tpl = (Path(user_manager.__file__).resolve().parent.parent / "admin" /
           "templates" / "admin_users.html").read_text(encoding="utf-8")
    assert "{% if actionable.missing %}" in tpl
    assert "{% if actionable.dir_disabled %}" in tpl
