"""設定備份匯入到另一台：不可以鎖住這台、個人資料不可以交給別人（issue #55，2026-10-08 實測）。

實測（全新的 B 台匯入 A 台全部的備份）兩件事：
1. 認證設定匯入後 B 變成「本機帳號登入」，但帳號不在設定備份裡 → 沒有人登得進來。
2. 工作區、通知偏好、乘車證明、送件前檢核我方資料、API Token 的擁有者都用**使用者編號**認人，
   原樣寫回 → A 台 3 號的東西到 B 台變成 B 台 3 號那個人的。

修法：備份附 `identity.json`（編號 → 帳號名稱與來源），匯入時換成這一台同一個帳號的編號，
對不上的不還原並講出來；認證設定在這台沒有本機管理員時不套用。
"""
from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import pytest

from app.core import settings_export as se


@pytest.fixture
def env(auth_off, tmp_path, monkeypatch):
    d = tmp_path / "data"
    d.mkdir()
    monkeypatch.setattr("app.config.settings.data_dir", d)
    # 帳號資料庫跟著資料夾走：這是「另一台」，一切從頭建
    from app.core import audit_db, auth_db, permissions, roles
    auth_db.init()
    audit_db.init()
    roles.seed_builtin_roles()
    permissions.invalidate_cache()
    # 全新安裝：認證關閉（設定檔要真的存在 —— 不存在而資料庫有帳號時，程式會 fail-secure 當成本機登入）
    from app.core import auth_settings
    auth_settings._CACHE = None
    cfg = auth_settings.get()
    cfg["backend"] = "off"
    auth_settings.save(cfg)
    yield d
    permissions.invalidate_cache()
    from app.core import auth_settings
    auth_settings._CACHE = None


def _user(name: str, *, admin: bool = False, uid: int | None = None) -> int:
    from app.core import auth_db, db, permissions, user_manager
    new = user_manager.create_local(name, name, "Passw0rd!Long")
    if uid is not None:
        conn = auth_db.conn()
        with db.tx(conn):
            conn.execute("UPDATE users SET id=? WHERE id=?", (uid, new))
        new = uid
    if admin:
        permissions.assign_role("user", str(new), "admin")
    return new


def _group(name: str) -> int:
    from app.core import group_manager
    return group_manager.create_local(name)


def _backup(path: Path, cats: dict[str, dict[str, bytes]], *, identity=None, rbac=None) -> Path:
    """做一份「另一台匯出的」備份。`cats` 是 {類別: {zip 內路徑: 內容}}。"""
    entries = {cid: list(files) for cid, files in cats.items()}
    if rbac is not None:
        entries["rbac"] = [se.RBAC_NAME]
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(se.MANIFEST_NAME, json.dumps({
            "kind": "jtdt-settings-export", "schema_version": 2, "app_version": "test",
            "categories": [{"id": c, "label": c} for c in entries],
            "entries_by_category": entries}))
        if identity is not None:
            zf.writestr(se.IDENTITY_NAME, json.dumps(identity))
        if rbac is not None:
            zf.writestr(se.RBAC_NAME, json.dumps(rbac))
        for files in cats.values():
            for n, b in files.items():
                zf.writestr(n, b)
    return path


def _import(path: Path) -> dict:
    with zipfile.ZipFile(path) as zf:
        ids = list(json.loads(zf.read(se.MANIFEST_NAME))["entries_by_category"])
    return se.import_from_zip(path, selected_ids=ids)


def _ident(**users) -> dict:
    return {"version": 1, "users": {str(k): {"username": v, "source": "local"} for k, v in users.items()},
            "groups": {}}


def _h(uid) -> str:
    return hashlib.blake2b(str(uid).encode("utf-8"), digest_size=16).hexdigest()


AUTH_LOCAL = json.dumps({"backend": "local"}).encode()


def _backend_after_restart() -> str:
    """匯入之後畫面寫「建議重啟服務套用新設定」—— 認證設定有快取，模擬重啟再讀。"""
    from app.core import auth_settings
    auth_settings._CACHE = None
    return auth_settings.get_backend()


# ------------------------------------------------------------------ 不可以鎖住這台

def test_auth_settings_are_not_applied_without_a_local_admin(env, tmp_path):
    from app.core import auth_settings
    r = _import(_backup(tmp_path / "b.zip", {"auth": {"data/auth_settings.json": AUTH_LOCAL}}))
    assert _backend_after_restart() == "off", "全新的這台被改成本機登入，但一個帳號都沒有"
    assert "auth" not in r["restored_categories"]
    assert any("認證設定" in s["text"] and "沒有人登得進來" in s["text"] for s in r["skipped"]), r["skipped"]


def test_auth_settings_apply_when_a_local_admin_exists(env, tmp_path):
    from app.core import auth_settings
    _user("odc-admin", admin=True)
    r = _import(_backup(tmp_path / "b.zip", {"auth": {"data/auth_settings.json": AUTH_LOCAL}}))
    assert _backend_after_restart() == "local"
    assert "auth" in r["restored_categories"] and not r["skipped"]


def test_a_non_admin_local_account_is_not_enough(env, tmp_path):
    from app.core import auth_settings
    _user("plain")
    _import(_backup(tmp_path / "b.zip", {"auth": {"data/auth_settings.json": AUTH_LOCAL}}))
    assert _backend_after_restart() == "off"


# ------------------------------------------------------------------ 個人資料跟著帳號走

def test_personal_data_follows_the_account_not_the_number(env, tmp_path):
    """備份裡 alice 是 9001 號；這台的 9001 號是 bob、alice 是另一個號碼 ——
    alice 的東西要到 alice 那裡，**bob 一個都不可以拿到**。"""
    bob = _user("bob", uid=9001)
    alice = _user("alice")
    assert alice != bob
    files = {
        "workspace": {"data/workspace/u9001/doc.pdf": b"alice-file", "data/workspace.json": b"{}"},
        "notify_prefs": {"data/notify_prefs/u9001.json": b'{"x": 1}'},
        "submission_check": {"data/submission_check/self_entities/9001.json": b"[]"},
        "scan_prefs": {f"data/transit_proof_settings/{_h(9001)}.json": b"{}"},
        "scan_buffers": {f"data/transit_proof_buffer/{_h(9001)}.json": b"{}"},
    }
    r = _import(_backup(tmp_path / "b.zip", files, identity=_ident(**{"9001": "alice"})))
    assert not r["skipped"], r["skipped"]
    assert (env / "workspace" / f"u{alice}" / "doc.pdf").read_bytes() == b"alice-file"
    assert not (env / "workspace" / f"u{bob}").exists(), "alice 的工作區交給了 bob"
    assert (env / "notify_prefs" / f"u{alice}.json").exists()
    assert not (env / "notify_prefs" / f"u{bob}.json").exists()
    assert (env / "submission_check" / "self_entities" / f"{alice}.json").exists()
    assert not (env / "submission_check" / "self_entities" / f"{bob}.json").exists()
    assert (env / "transit_proof_settings" / f"{_h(alice)}.json").exists()
    assert not (env / "transit_proof_settings" / f"{_h(bob)}.json").exists()
    assert (env / "transit_proof_buffer" / f"{_h(alice)}.json").exists()
    assert (env / "workspace.json").exists(), "工作區設定（不屬於任何人）照常還原"


def test_data_of_people_who_are_not_here_is_not_restored(env, tmp_path):
    bob = _user("bob", uid=9001)
    r = _import(_backup(tmp_path / "b.zip",
                        {"workspace": {"data/workspace/u9001/doc.pdf": b"alice-file"}},
                        identity=_ident(**{"9001": "alice"})))
    assert not (env / "workspace" / f"u{bob}").exists()
    assert "workspace" not in r["restored_categories"]
    assert any("alice@local" in s["text"] for s in r["skipped"]), r["skipped"]


def test_shared_single_user_data_is_still_restored(env, tmp_path):
    """認證關閉時的共用資料不屬於任何人 —— 舊版備份（沒有帳號對照）也照常還原。"""
    files = {"workspace": {"data/workspace/__single__/a.pdf": b"shared"},
             "notify_prefs": {"data/notify_prefs/__single__.json": b"{}"},
             "scan_prefs": {"data/transit_proof_settings/default.json": b"{}"}}
    r = _import(_backup(tmp_path / "b.zip", files))
    assert (env / "workspace" / "__single__" / "a.pdf").read_bytes() == b"shared"
    assert (env / "notify_prefs" / "__single__.json").exists()
    assert (env / "transit_proof_settings" / "default.json").exists()
    assert not r["skipped"]


def test_old_backups_without_identity_do_not_hand_out_personal_data(env, tmp_path):
    bob = _user("bob", uid=9001)
    r = _import(_backup(tmp_path / "b.zip", {"workspace": {"data/workspace/u9001/doc.pdf": b"x"}}))
    assert not (env / "workspace" / f"u{bob}").exists(), "舊版備份把 9001 號的工作區交給了這台的 9001 號"
    assert any("舊版備份" in s["text"] for s in r["skipped"]), r["skipped"]


# ------------------------------------------------------------------ API Token 的擁有者

def test_token_owner_is_mapped_or_cleared(env, tmp_path):
    bob = _user("bob", uid=9001)
    alice = _user("alice")
    tokens = {"tokens": [
        {"token": "A" * 43, "label": "alice-script", "created_at": 1, "owner_user_id": 9001},
        {"token": "C" * 43, "label": "carol-script", "created_at": 1, "owner_user_id": 9002},
        {"token": "D" * 43, "label": "legacy", "created_at": 1, "owner_user_id": None}],
        "enforce": True}
    r = _import(_backup(tmp_path / "b.zip", {"api_tokens": {"data/api_tokens.json": json.dumps(tokens).encode()}},
                        identity=_ident(**{"9001": "alice", "9002": "carol"})))
    got = {t["label"]: t["owner_user_id"]
           for t in json.loads((env / "api_tokens.json").read_text(encoding="utf-8"))["tokens"]}
    assert got["alice-script"] == alice, "Token 變成這台同編號那個人（bob）的權限"
    assert got["carol-script"] is None, "擁有者不在這台，Token 不可以留著原本的編號"
    assert got["legacy"] is None
    assert any("carol-script" in s["text"] and "重新指定擁有者" in s["text"] for s in r["skipped"]), r["skipped"]
    assert bob not in got.values()


# ------------------------------------------------------------------ 個人與群組的角色指派

def test_user_and_group_roles_are_carried_by_name(env, tmp_path):
    from app.core import permissions
    alice = _user("alice")
    gid = _group("財務部")
    rbac = {"roles": [], "role_perms": [], "role_seed_snapshot": [],
            "ou_subject_roles": [], "ou_subject_perms": [],
            "user_subject_roles": [["50", "finance"], ["50", "admin"], ["51", "sales"]],
            "group_subject_roles": [["70", "sales"]],
            "user_subject_perms": [["50", "pdf-merge"]], "group_subject_perms": []}
    ident = {"version": 1, "users": {"50": {"username": "alice", "source": "local"},
                                     "51": {"username": "ghost", "source": "local"}},
             "groups": {"70": {"name": "財務部", "source": "local"}}}
    r = _import(_backup(tmp_path / "b.zip", {}, identity=ident, rbac=rbac))
    got = set(permissions.list_roles_for_subject("user", str(alice)))
    assert "finance" in got and "admin" not in got, got
    assert "sales" in permissions.list_roles_for_subject("group", str(gid))
    joined = "\n".join(x["text"] for x in r["skipped"])
    assert "admin" in joined and "不從備份匯入" in joined, "管理員不可以從備份給"
    assert "ghost@local" in joined


def test_the_dump_carries_assignments_but_no_passwords(env):
    from app.core import permissions
    alice = _user("alice")
    permissions.assign_role("user", str(alice), "finance")
    dump = se._rbac_dump()
    assert [str(alice), "finance"] in dump["user_subject_roles"]
    ident = se._identity_dump()
    assert ident["users"][str(alice)] == {"username": "alice", "source": "local"}
    blob = json.dumps([dump, ident]).lower()
    assert "password" not in blob and "hash" not in blob and "totp" not in blob


# ------------------------------------------------------------------ 同一台還原照常

def test_restoring_on_the_same_machine_keeps_everything(env, tmp_path):
    from app.core import permissions
    alice = _user("alice")
    permissions.assign_role("user", str(alice), "finance")
    (env / "workspace" / f"u{alice}").mkdir(parents=True)
    (env / "workspace" / f"u{alice}" / "f.pdf").write_bytes(b"mine")
    out = tmp_path / "out"
    out.mkdir()
    res = se.export_to_zip(out / "b.zip", selected_ids=["workspace", "rbac"])
    with zipfile.ZipFile(res["out_path"]) as zf:
        assert se.IDENTITY_NAME in zf.namelist()
    (env / "workspace" / f"u{alice}" / "f.pdf").unlink()
    r = se.import_from_zip(Path(res["out_path"]), selected_ids=["workspace", "rbac"])
    assert (env / "workspace" / f"u{alice}" / "f.pdf").read_bytes() == b"mine"
    assert "finance" in permissions.list_roles_for_subject("user", str(alice))
    assert not r["skipped"], r["skipped"]


def test_skip_reasons_are_templates_the_page_can_translate(env, tmp_path):
    """畫面 `tr(樣板)` 再填參數 —— 整句組好的中文英日介面翻不動。"""
    bob = _user("bob", uid=9001)
    r = _import(_backup(tmp_path / "b.zip",
                        {"workspace": {"data/workspace/u9001/a.pdf": b"x", "data/workspace/u9001/b.pdf": b"y"}},
                        identity=_ident(**{"9001": "alice"})))
    (item,) = r["skipped"]
    assert item["template"] == se.SKIP_MESSAGES["no_user"] and item["args"] == ["工作區", "alice@local"]
    assert item["count"] == 2, "同一個原因的檔案要合成一條、帶件數"
    assert not (env / "workspace" / f"u{bob}").exists()


def test_skip_messages_and_labels_are_translated():
    from pathlib import Path as P
    for lang in ("en", "ja"):
        d = json.loads(P(f"app/i18n/{lang}.json").read_text(encoding="utf-8"))
        missing = [k for k in list(se.SKIP_MESSAGES.values()) + list(se.SKIP_LABELS) if k not in d]
        assert not missing, (lang, missing)
