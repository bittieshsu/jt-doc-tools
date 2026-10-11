"""API Token 不再明文存放（CodeQL #208；2026-10-11 決定：加密存放，管理頁照樣可以「顯示」）。

`api_tokens.json` 每一張 Token 存三樣東西：

* `token_sha256`：比對用（每一次 API 請求只算雜湊，不必解密）；
* `token_enc`：Fernet 密文（同 LLM 金鑰那一把），只有管理頁「顯示 / 複製」與內建 Token 提示會解開；
* `preview`：遮罩過的前 6 後 4 碼，清單顯示與撤銷用。

判準：

* 檔案裡**找不到任何一張 Token 的原文**（新建的、舊檔升級的都一樣）。
* 比對、撤銷、指定擁有者照常（完整 Token 或遮罩都可以）；停用、過期照常擋。
* 舊版的明文檔（含設定備份匯入的、服務跑著時被換掉的）第一次讀到就轉過來，Token 照樣能用。
* 金鑰換過（`.session_secret` 不是原本那一把）：Token **照樣能用**（比對靠雜湊），只是管理頁不能再顯示原文。
* 設定備份換機：匯出時解密、匯入時用這一台的金鑰重新加密，兩台都能用、都能顯示。
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import time

import pytest


@pytest.fixture()
def mgr(tmp_path, monkeypatch):
    from app.config import settings
    from app.core import api_tokens
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    return api_tokens.ApiTokenManager(), tmp_path / "api_tokens.json"


def _raw(path) -> str:
    return path.read_text(encoding="utf-8")


def test_no_token_is_stored_in_clear_text(mgr):
    m, path = mgr
    builtin = m.first_token()
    tok = m.create("報表排程").token
    raw = _raw(path)
    assert builtin and builtin not in raw, "內建 Token 原文還在檔案裡"
    assert tok not in raw, "新建的 Token 原文還在檔案裡"
    rows = json.loads(raw)["tokens"]
    assert all("token" not in r for r in rows), rows
    row = next(r for r in rows if r["label"] == "報表排程")
    assert row["token_sha256"] == hashlib.sha256(tok.encode()).hexdigest()
    from app.core import secret_box
    assert secret_box.decrypt(row["token_enc"]) == tok
    assert row["preview"] == f"{tok[:6]}…{tok[-4:]}"


def test_lookup_revoke_and_owner_still_work(mgr):
    m, _ = mgr
    tok = m.create("a").token
    assert m.lookup(tok)["label"] == "a"
    assert m.lookup(tok + "x") is None and m.lookup("") is None
    got = m.lookup(tok)
    assert "token" not in got and "token_enc" not in got, "比對結果不需要帶 Token 本身"
    assert m.assign_owner(f"{tok[:6]}…{tok[-4:]}", 7)
    assert m.lookup(tok)["owner_user_id"] == 7
    assert m.assign_owner(tok, 8) and m.lookup(tok)["owner_user_id"] == 8
    other = m.create("b").token
    assert m.revoke(f"{tok[:6]}…{tok[-4:]}") and m.lookup(tok) is None
    assert m.revoke(other) and m.lookup(other) is None
    assert not m.revoke("nope")


def test_disabled_and_expired_are_still_refused(mgr):
    m, path = mgr
    tok = m.create("x").token
    data = json.loads(_raw(path))
    for r in data["tokens"]:
        if r["label"] == "x":
            r["expires_at"] = time.time() - 1
    path.write_text(json.dumps(data), encoding="utf-8")
    assert m.lookup(tok) is None
    for r in data["tokens"]:
        if r["label"] == "x":
            r["expires_at"], r["enabled"] = 0, False
    path.write_text(json.dumps(data), encoding="utf-8")
    assert m.lookup(tok) is None


def test_the_admin_page_can_still_reveal(mgr):
    m, _ = mgr
    tok = m.create("看得到").token
    full = {t["label"]: t for t in m.list_full()}
    assert full["看得到"]["token"] == tok
    assert full["看得到"]["preview"] == f"{tok[:6]}…{tok[-4:]}"
    pub = {t["label"]: t for t in m.list_tokens()}
    assert pub["看得到"]["token_preview"] == f"{tok[:6]}…{tok[-4:]}"
    assert tok not in json.dumps(m.list_tokens()), "公開清單不可以帶原文"


def _old_file(path, tokens):
    path.write_text(json.dumps({"tokens": [
        {"token": t, "label": f"old-{i}", "created_at": 1.0, "last_used_at": 0.0, "expires_at": 0.0,
         "enabled": True, "owner_user_id": 3} for i, t in enumerate(tokens)],
        "enforce": True, "updated_at": 1.0}), encoding="utf-8")
    os.chmod(path, 0o644)


def test_an_old_clear_text_file_is_converted_on_start(tmp_path, monkeypatch):
    from app.config import settings
    from app.core import api_tokens
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    path = tmp_path / "api_tokens.json"
    olds = ["A" * 43, "B" * 43]
    _old_file(path, olds)
    m = api_tokens.ApiTokenManager()
    raw = _raw(path)
    assert not any(t in raw for t in olds), "舊版明文檔沒有轉過來"
    assert all(m.lookup(t)["owner_user_id"] == 3 for t in olds), "轉過來之後 Token 不能用"
    assert m.is_enforced(), "其他設定要留著"
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_a_clear_text_file_dropped_in_while_running_is_converted(mgr):
    """設定備份匯入（舊版備份）是在服務跑著的時候把檔案換掉的。"""
    m, path = mgr
    _old_file(path, ["C" * 43])
    assert m.lookup("C" * 43) is not None
    assert "C" * 43 not in _raw(path)


def test_a_changed_key_keeps_tokens_working_but_not_revealable(mgr):
    m, path = mgr
    tok = m.create("換過金鑰").token
    (path.parent / ".session_secret").write_bytes(os.urandom(32))
    assert m.lookup(tok)["label"] == "換過金鑰", "金鑰換過不可以讓 API 呼叫全部失敗"
    row = next(t for t in m.list_full() if t["label"] == "換過金鑰")
    assert row["token"] == "" and row["preview"] == f"{tok[:6]}…{tok[-4:]}"
    assert m.revoke(row["preview"]) and m.lookup(tok) is None


def test_settings_backup_moves_tokens_between_machines(mgr, monkeypatch):
    """匯出時解密、匯入時用這一台的金鑰重新加密（同 LLM / SSO 的金鑰）。"""
    m, path = mgr
    tok = m.create("搬家").token
    from app.core import settings_export as se
    assert "api_tokens.json" in se._rekey_specs()
    blob = se._rekey_export_blob("api_tokens.json")
    assert tok in blob, "備份檔裡要是明文（換一台機器才解得開）"
    # 換一台機器：不同的金鑰
    (path.parent / ".session_secret").write_bytes(os.urandom(32))
    path.write_text(blob, encoding="utf-8")
    se._rekey_after_import(path)
    assert tok not in _raw(path), "匯入之後沒有重新加密"
    assert m.lookup(tok)["label"] == "搬家"
    assert next(t for t in m.list_full() if t["label"] == "搬家")["token"] == tok, "匯入之後顯示不出原文"


def test_admin_page_shows_preview_and_hides_reveal_when_it_cannot(admin_session):
    from app.core.api_tokens import api_tokens
    c, _, _ = admin_session
    tok = api_tokens.create("pytest-reveal").token
    try:
        html = c.get("/admin/api-tokens").text
        assert f'data-full="{tok}"' in html, "管理頁的「顯示」要拿得到原文"
        assert f"{tok[:6]}…{tok[-4:]}" in html
    finally:
        api_tokens.revoke(tok)
