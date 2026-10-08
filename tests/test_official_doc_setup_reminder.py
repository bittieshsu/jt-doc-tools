"""公文撰擬頁上方提醒管理員：還有哪些資料要先下載。

2026-10-08 使用者：「公文撰擬上面，要提醒管理員需要去設定下載匯入 公文範本資料集」。

公文範本、機關地址簿（公文撰擬設定）與政府公開資料（公文知識庫）都不隨程式散布，要管理員按下載。
沒做的話一般使用者只看到「範本那一格沒有東西」，看不出是缺資料。

要守住的事：

* 管理員（與認證關閉的單人模式）看得到，**一般使用者看不到**（他做不了這件事）。
* 只列還沒做的那幾項，每一項有連結到做那件事的頁面；全部做好就整塊不出現。
* 打開頁面**不可以建出公文知識庫的資料庫**（管理員可能從來沒用過知識庫）。
"""
from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from tests._kb_support import kb_isolated  # noqa: F401

BASE = "/tools/official-doc/"


def _box(html: str) -> str | None:
    m = re.search(r'<div class="warn-box" id="odSetup">(.*?)</div>\s*</div>', html, re.S)
    return m.group(1) if m else None


def test_admin_sees_what_is_missing_with_links(admin_session, kb_isolated):
    c, _, _ = admin_session
    box = _box(c.get(BASE).text)
    assert box, "管理員要看得到提醒"
    assert "公文範本與機關地址簿還沒下載" in box and 'href="/admin/official-doc"' in box
    assert "法規與文書規範還沒匯入公文知識庫" in box and 'href="/admin/knowledge/gov"' in box
    from app.core.kb import store
    assert not store.db_path().exists(), "打開頁面就建出了公文知識庫的資料庫"


def test_plain_users_do_not_see_it(admin_session, kb_isolated):
    from app.core import sessions, user_manager
    uid = user_manager.create_local("odplain", "一般", "PlainPass1234", roles=["default-user"])
    tok, _ = sessions.issue(uid, remember=False, ip="127.0.0.1", ua="pytest")
    import app.main as app_main
    c = TestClient(app_main.app)
    c.cookies.set(sessions.COOKIE_NAME, tok)
    r = c.get(BASE)
    assert r.status_code == 200
    assert _box(r.text) is None, "一般使用者做不了這件事，不該看到"


def test_single_user_mode_sees_it(client, auth_off, kb_isolated):
    assert _box(client.get(BASE).text)


def test_only_what_is_missing_is_listed(admin_session, kb_isolated, monkeypatch):
    from app.core import official_doc_sources as ods
    c, _, _ = admin_session
    monkeypatch.setattr(ods, "list_templates", lambda: [{"name": "簽"}])
    box = _box(c.get(BASE).text)
    assert "機關地址簿還沒下載" in box and "公文範本" not in box
    monkeypatch.setattr(ods, "has_address_book", lambda: True)
    box = _box(c.get(BASE).text)
    assert "還沒下載" not in box and "法規與文書規範" in box


def test_nothing_shows_when_everything_is_ready(admin_session, kb_isolated, monkeypatch):
    from app.core import official_doc_sources as ods
    from app.core.kb import store
    c, _, _ = admin_session
    monkeypatch.setattr(ods, "list_templates", lambda: [{"name": "簽"}])
    monkeypatch.setattr(ods, "has_address_book", lambda: True)
    store.db_path().parent.mkdir(parents=True, exist_ok=True)
    store.db_path().write_bytes(b"")
    monkeypatch.setattr(store, "gov_items",
                        lambda gid: {"A0030055": {"dataset_id": "d1"}} if gid == "moj" else {})
    assert _box(c.get(BASE).text) is None


def test_the_new_text_is_translated():
    import json
    from pathlib import Path
    keys = ("還有資料要管理員先下載", "（只有管理員看得到這個提醒）",
            "公文範本與機關地址簿還沒下載：匯出時沒有機關範本可以套用，填受文者時也不會建議機關全銜。",
            "公文範本還沒下載：匯出時沒有機關範本可以套用。",
            "機關地址簿還沒下載：填受文者時不會建議機關全銜。",
            "前往公文撰擬設定",
            "法規與文書規範還沒匯入公文知識庫：撰擬時沒有法規可以參考，也不會提醒引用了已廢止的法規。",
            "前往政府公開資料")
    for lang in ("en", "ja"):
        d = json.loads(Path(f"app/i18n/{lang}.json").read_text(encoding="utf-8"))
        missing = [k for k in keys if not d.get(k)]
        assert not missing, f"{lang}.json 少了 {missing}"
