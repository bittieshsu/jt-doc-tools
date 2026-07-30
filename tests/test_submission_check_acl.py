"""送件檢核（submission-check）的案件 ACL 測試。

## 為什麼單獨一份

案件裡放的是客戶送來的證件影本、財力證明這類東西 —— 洩漏的後果比一般轉檔檔案
嚴重。而它的 ACL 是**自己寫的一套**（`_check_case_acl`），沒有走 `upload_owner`
那條共用路徑，所以共用路徑修好的問題它不會自動受惠，必須獨立驗。

## 這份要守住的三件事

1. **無主案件不可以變成大家都能看**。清單頁是嚴格比對（安全），但用 case_id
   直接開的路徑原本寫成「有 owner 才比對」→ `owner_uid` 是 None 時任何登入者
   都能讀寫刪。無主案件在兩種情況下真的會出現：先在未啟用認證時建立、之後才
   開認證；或舊版建立的案件。
2. **admin / 稽核員的判定要真的有效**。原本讀的是 session 字典裡不存在的鍵
   （`effective_tools` / `role`）→ 永遠 False；稽核員那條還 import 了一個不存在
   的模組。兩者都被 `except` 吃掉，所以「壞掉」與「沒權限」從外面看完全一樣。
3. **稽核員唯讀**。看得到全部案件，但不可以刪、不可以改、不可以覆寫判定。
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException, Request



# ---------- 工具 ----------

def _sc():
    """取模組本身。

    `from app.tools.submission_check import router` 會拿到**那個 APIRouter 物件**
    （套件 __init__ 匯入了它），不是模組 —— 用 import_module 明確取模組。
    """
    import importlib
    return importlib.import_module("app.tools.submission_check.router")


def _req(user):
    """做一個只帶 state.user 的假 Request（ACL 只看這個）。"""
    scope = {"type": "http", "method": "GET", "path": "/", "headers": []}
    r = Request(scope)
    r.state.user = user
    return r


def _mkuser(username: str, admin: bool = False, auditor: bool = False) -> dict:
    """建一個真的使用者，回**與 sessions.lookup 相同形狀**的字典。

    這裡刻意不自己捏一個「方便測試」的字典 —— 這個 bug 正是因為程式讀了字典裡
    不存在的鍵，而測試用的假字典剛好有那些鍵，所以測試綠燈、實際永遠 False。
    """
    from app.core import permissions, roles as _roles, user_manager
    # 內建角色由 app 啟動時 seed；這份測試不發 HTTP 請求，所以要自己來
    # （沒有 roles 列，subject_roles 的外鍵會直接失敗）。
    _roles.seed_builtin_roles()
    uid = user_manager.create_local(username, username, "TestPass1234")
    roles = (["admin"] if admin else []) + (["auditor"] if auditor else [])
    if roles:
        permissions.set_subject_roles("user", str(uid), roles)
    return {"user_id": uid, "username": username, "display_name": username,
            "source": "local", "is_admin_seed": False}


# ---------- 1. 無主案件 ----------

def test_ownerless_case_is_not_readable_by_any_logged_in_user(auth_off):
    """認證開啟後，未啟用認證時建立的案件不可以變成大家都能看。"""
    sc = _sc()
    alice = _mkuser("alice")
    case = {"case_id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "owner_uid": None}
    with pytest.raises(HTTPException) as e:
        sc._check_case_acl(case, _req(alice))
    assert e.value.status_code in (403, 404)


def test_ownerless_case_still_accessible_to_admin(auth_off):
    """admin 要能處理無主案件（否則舊資料會變成沒人救得回來）。"""
    sc = _sc()
    admin = _mkuser("root", admin=True)
    sc._check_case_acl({"case_id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "owner_uid": None}, _req(admin))


def test_ownerless_case_when_auth_off_is_fine(auth_off):
    """未啟用認證時整站是單人模式，ACL 不該擋。"""
    sc = _sc()
    sc._check_case_acl({"case_id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "owner_uid": None}, _req(None))


# ---------- 2. admin / 稽核員判定要真的有效 ----------

def test_admin_detection_uses_real_session_shape(auth_off):
    """admin 判定必須對「真的 session 字典」有效。

    原本讀 `effective_tools` / `role` —— `sessions.lookup()` 兩個都不回，
    所以 admin 一直被當成一般使用者。
    """
    sc = _sc()
    admin = _mkuser("root", admin=True)
    assert sc._is_admin(admin) is True
    assert sc._is_admin(_mkuser("bob")) is False


def test_auditor_detection_does_not_import_missing_module(auth_off):
    """稽核員判定原本 import `app.core.perm`（不存在的模組）→ 永遠 False。"""
    sc = _sc()
    auditor = _mkuser("audit1", auditor=True)
    assert sc._is_auditor(auditor) is True
    assert sc._is_auditor(_mkuser("carol")) is False


def test_admin_can_read_other_users_case(auth_off):
    sc = _sc()
    alice = _mkuser("alice")
    admin = _mkuser("root", admin=True)
    case = {"case_id": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", "owner_uid": alice["user_id"]}
    sc._check_case_acl(case, _req(admin))          # 不可丟例外


def test_auditor_can_read_other_users_case(auth_off):
    sc = _sc()
    alice = _mkuser("alice")
    auditor = _mkuser("audit1", auditor=True)
    case = {"case_id": "cccccccccccccccccccccccccccccccc", "owner_uid": alice["user_id"]}
    sc._check_case_acl(case, _req(auditor))


# ---------- 3. 一般使用者互相隔離 ----------

def test_other_user_cannot_read_someone_elses_case(auth_off):
    sc = _sc()
    alice = _mkuser("alice")
    bob = _mkuser("bob")
    case = {"case_id": "dddddddddddddddddddddddddddddddd", "owner_uid": alice["user_id"]}
    with pytest.raises(HTTPException):
        sc._check_case_acl(case, _req(bob))


def test_owner_can_read_own_case(auth_off):
    sc = _sc()
    alice = _mkuser("alice")
    sc._check_case_acl({"case_id": "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
                        "owner_uid": alice["user_id"]}, _req(alice))


# ---------- 4. 稽核員唯讀 ----------

def test_auditor_is_read_only_for_writes(auth_off):
    """稽核員可讀全部，但寫入路徑要擋掉（分權原則）。"""
    sc = _sc()
    auditor = _mkuser("audit1", auditor=True)
    alice = _mkuser("alice")
    case = {"case_id": "ffffffffffffffffffffffffffffffff", "owner_uid": alice["user_id"]}
    with pytest.raises(HTTPException) as e:
        sc._check_case_acl(case, _req(auditor), write=True)
    assert e.value.status_code == 403


def test_admin_write_is_allowed(auth_off):
    sc = _sc()
    admin = _mkuser("root", admin=True)
    alice = _mkuser("alice")
    sc._check_case_acl({"case_id": "00000000000000000000000000000000",
                        "owner_uid": alice["user_id"]}, _req(admin), write=True)


def test_owner_write_is_allowed(auth_off):
    sc = _sc()
    alice = _mkuser("alice")
    sc._check_case_acl({"case_id": "11111111111111111111111111111111",
                        "owner_uid": alice["user_id"]}, _req(alice), write=True)


# ---------- 5. 清單過濾（原本就安全，釘住不要退化） ----------

def test_case_list_filter_excludes_ownerless_for_normal_user(auth_off):
    """清單頁用的是嚴格比對，無主案件不可以出現在一般使用者的清單裡。"""
    import json
    import shutil
    from app.tools.submission_check import case_manager as cm
    alice = _mkuser("alice")
    root = cm._root()          # 走真正的 data dir（測試套件已隔離到 temp）
    made = []
    try:
        for cid, owner in (("22222222222222222222222222222222", None),
                           ("33333333333333333333333333333333", alice["user_id"]),
                           ("44444444444444444444444444444444", 99999)):
            d = root / cid
            d.mkdir(parents=True, exist_ok=True)
            made.append(d)
            (d / "case.json").write_text(
                json.dumps({"case_id": cid, "owner_uid": owner, "files": [],
                            "status": "draft"}, ensure_ascii=False),
                encoding="utf-8")
        got = {c["case_id"] for c in cm.list_cases(owner_uid=alice["user_id"])}
        assert "22222222222222222222222222222222" not in got, "無主案件不可出現在一般使用者清單"
        assert "44444444444444444444444444444444" not in got, "別人的案件不可出現"
        assert "33333333333333333333333333333333" in got, "自己的案件要看得到"
    finally:
        for d in made:
            shutil.rmtree(d, ignore_errors=True)


# ---------- 6. 所有 case 端點都有 ACL ----------

def test_every_case_endpoint_checks_acl():
    """新增 case 端點時忘了呼叫 ACL 是最容易發生的漏 —— 用原始碼比對釘住。

    比對方式是「函式主體裡有沒有 `_check_case_acl`」；`delete_case` 例外處理，
    它有自己的一段（含稽核員唯讀判斷），所以允許它用 `write=True` 的形式。
    """
    import inspect
    import re
    sc = _sc()
    src = inspect.getsource(sc)
    # 抓每個吃 case_id 的路由函式
    blocks = re.split(r"\n@router\.", src)
    missing = []
    for b in blocks[1:]:
        head = b.split("\n")[0]
        if "{case_id}" not in head:
            continue
        fn = re.search(r"async def (\w+)", b)
        if not fn:
            continue
        body = b.split("\n", 1)[1]
        if "_check_case_acl" not in body:
            missing.append(fn.group(1))
    assert not missing, f"這些吃 case_id 的端點沒有做 ACL：{missing}"


# ---------- 7. 不可分辨「不存在」與「不是你的」 ----------

def test_non_owner_response_is_indistinguishable_from_not_found(auth_off):
    """非擁有者收到的狀態碼與訊息，必須與「案件不存在」完全相同。

    否則就等於提供一個查詢介面：拿任意 case_id 打過來，403 代表「這個案件存在，
    只是不是你的」。案件編號本身就是資訊（某某客戶有沒有在這裡送過件）。
    """
    import pytest as _pytest
    sc = _sc()
    alice = _mkuser("alice")
    bob = _mkuser("bob")

    with _pytest.raises(HTTPException) as owned:
        sc._check_case_acl({"case_id": "b" * 32, "owner_uid": alice["user_id"]},
                           _req(bob))
    with _pytest.raises(HTTPException) as ownerless:
        sc._check_case_acl({"case_id": "c" * 32, "owner_uid": None}, _req(bob))

    assert owned.value.status_code == 404
    assert owned.value.status_code == ownerless.value.status_code
    assert owned.value.detail == ownerless.value.detail
    # 也要與端點對「真的不存在」時回的訊息一致
    assert owned.value.detail == "case 不存在"


def test_auditor_write_refusal_may_stay_403(auth_off):
    """稽核員的唯讀限制回 403 是可以的 —— 他本來就讀得到，不洩漏新資訊。"""
    import pytest as _pytest
    sc = _sc()
    auditor = _mkuser("audit9", auditor=True)
    alice = _mkuser("alice9")
    with _pytest.raises(HTTPException) as e:
        sc._check_case_acl({"case_id": "d" * 32, "owner_uid": alice["user_id"]},
                           _req(auditor), write=True)
    assert e.value.status_code == 403
