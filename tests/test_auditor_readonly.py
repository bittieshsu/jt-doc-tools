"""稽核員必須是唯讀角色 —— 而 admin 不該因為隱私規則而失去管理能力。

## 這支的由來

`require_admin` 的稽核員前綴判斷**不分 HTTP method**，於是：

* `POST /admin/history/{kind}/{id}/delete`（真的刪紀錄與檔案）→ **只有稽核員
  叫得動，admin 被 403**。等於「只該看」的角色具備銷毀證據能力，而唯一能覆核
  的人被擋在外面。
* `POST /admin/system-status/databases/backup` → 稽核員可觸發寫入，並且因為備份
  只保留 7 份，連續呼叫 7 次即可把既有備份全部輪替掉（資料庫毀損時的救援路徑）。

`deps.py` 自己的註解寫著「稽核員自己不能刪（UI 沒刪除端點）」—— 端點其實存在。
註解與行為不符是最難發現的一類問題。

## 修正後的模型

| 前綴 | 讀（GET/HEAD） | 寫（POST/DELETE…） |
|---|---|---|
| AUDITOR_EXCLUSIVE（含使用者隱私） | 只有稽核員 | 只有 admin |
| AUDITOR_SHARED | admin 或稽核員 | 只有 admin |

「依 id 刪除」不等於「偷看內容」，所以把寫入動作歸還 admin 並不違反原本的隱私
設計；稽核員則回歸唯讀。
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app import main as app_main


def _auditor_client():
    """建一個具稽核員角色的帳號並登入。"""
    from app.core import permissions, sessions, user_manager
    uid = user_manager.create_local("ro-auditor", "稽核員", "AuditPass1234")
    permissions.set_subject_roles("user", str(uid), ["auditor"])
    tok, _ = sessions.issue(uid, remember=False, ip="127.0.0.1", ua="pytest")
    c = TestClient(app_main.app)
    c.cookies.set(sessions.COOKIE_NAME, tok)
    return uid, c


def test_auditor_cannot_delete_history(admin_session):
    """稽核員是唯讀的 —— 不可刪除任何歷史紀錄。"""
    _uid, ca = _auditor_client()
    r = ca.post("/admin/history/fill/abc123/delete")
    assert r.status_code == 403, f"稽核員竟然可以刪除歷史（{r.status_code}）"


def test_admin_can_delete_history(admin_session):
    """刪除是管理動作 —— admin 必須做得到（依 id 刪除不等於偷看內容）。

    404 / 400 代表通過了權限閘只是找不到那筆；403 才是被擋。
    """
    c, _, _ = admin_session
    r = c.post("/admin/history/fill/no-such-id/delete")
    assert r.status_code != 403, "admin 被隱私規則擋住了管理動作"


def test_auditor_can_still_read_history(admin_session):
    """讀取仍是稽核員專屬（隱私設計不變）。"""
    _uid, ca = _auditor_client()
    assert ca.get("/admin/history/fill").status_code == 200


def test_admin_still_cannot_read_history(admin_session):
    """admin 不該偷看使用者的真實檔案 —— 這條原本的設計要保留。"""
    c, _, _ = admin_session
    assert c.get("/admin/history/fill").status_code == 403


def test_auditor_cannot_trigger_database_backup(admin_session):
    """稽核員不可觸發寫入動作。備份只保留 7 份，連按 7 次就能把既有備份
    全部輪替掉 —— 那正好是稽核記錄毀損時的救援路徑。"""
    _uid, ca = _auditor_client()
    r = ca.post("/admin/system-status/databases/backup")
    assert r.status_code == 403, f"稽核員可以觸發備份（{r.status_code}）"


def test_auditor_can_read_system_status(admin_session):
    _uid, ca = _auditor_client()
    assert ca.get("/admin/system-status").status_code == 200


def test_admin_can_trigger_database_backup(admin_session):
    c, _, _ = admin_session
    r = c.post("/admin/system-status/databases/backup")
    assert r.status_code == 200, r.text
