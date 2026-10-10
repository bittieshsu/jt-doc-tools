"""知識庫改名「公文知識庫」（2026-10-08 使用者：「知識庫功能 是針對公文用的吧」「這樣名稱是不是要換」）。

目前只有公文撰擬會查它，名稱照用途取，管理員才找得到。**只換顯示的名稱** ——
網址（`/admin/knowledge`）、API、資料目錄都不動（書籤、腳本、既有資料照常）；
側欄搜尋打舊名「知識庫」照樣找得到。
"""
from __future__ import annotations

import re

from tests._kb_support import kb_isolated  # noqa: F401
from tools import source_text


def _sidebar_item(html: str) -> str:
    m = re.search(r'<a[^>]*href="/admin/knowledge"[^>]*>(.*?)</a>', html, re.S)
    assert m, "側欄沒有 /admin/knowledge 那一項"
    return m.group(1)


def test_pages_use_the_new_name(admin_session, kb_isolated):
    c, _, _ = admin_session
    html = c.get("/admin/knowledge").text
    h1 = re.search(r"<h1>(.*?)</h1>", html, re.S).group(1)
    assert "公文知識庫" in h1
    assert "公文知識庫" in source_text.blocks(html, "title")[0]
    assert "公文知識庫" in _sidebar_item(html), "側欄還是舊名"
    llm = c.get("/admin/llm-settings").text
    assert "前往公文知識庫" in llm and "前往知識庫" not in llm
    gov = c.get("/admin/knowledge/gov").text
    assert "回到公文知識庫" in gov


def test_the_tool_checkbox_uses_the_new_name(client, auth_off, monkeypatch):
    from app.core import kb
    monkeypatch.setattr(kb, "list_datasets", lambda *, user_id: [{"id": "x"}])
    html = client.get("/tools/official-doc/").text
    assert "參考公文知識庫" in html and ">參考知識庫<" not in html


def test_old_name_still_finds_it_and_the_url_did_not_move():
    from app import main as app_main
    items = [x for x in app_main._ADMIN_NAV_ITEMS if x.get("url") == "/admin/knowledge"] \
        if hasattr(app_main, "_ADMIN_NAV_ITEMS") else []
    if not items:
        src = open(app_main.__file__, encoding="utf-8").read()
        m = re.search(r'\{"icon": "book", "name": "公文知識庫".*?"keywords": (".*?)\},', src, re.S)
        assert m, "側欄設定裡找不到公文知識庫那一項"
        kw = m.group(1)
        assert '"/admin/knowledge"' in m.group(0)
    else:
        kw = items[0]["keywords"]
        assert items[0]["name"] == "公文知識庫"
    assert "知識庫" in kw and "公文知識庫" in kw, "搜尋舊名要找得到"


def test_the_new_name_is_translated():
    import json
    from pathlib import Path
    for lang, word in (("en", "Official document knowledge base"), ("ja", "公文ナレッジベース")):
        d = json.loads(Path(f"app/i18n/{lang}.json").read_text(encoding="utf-8"))
        assert d["公文知識庫"] == word
        assert "參考公文知識庫" in d and "公文知識庫目前狀態" in d


def test_the_two_official_doc_settings_sit_next_to_each_other_in_the_sidebar(admin_session, kb_isolated):
    """側欄「設定」裡「公文撰擬設定」緊接在「公文知識庫」上面（2026-10-09 使用者：
    「請把 公文撰擬設定 跟 公文知識庫 排在上下 不要離很多項」）。

    判準看畫出來的側欄（使用者看到的是那個），不看清單：兩個連結之間不可以夾著別的管理頁。
    """
    c, _, _ = admin_session
    html = c.get("/admin/knowledge").text
    links = re.findall(r'<a[^>]*href="(/admin/[^"#?]*)"', html)
    assert "/admin/official-doc" in links and "/admin/knowledge" in links, links
    i, j = links.index("/admin/official-doc"), links.index("/admin/knowledge")
    assert j == i + 1, f"中間夾著：{links[i + 1:j] if j > i else links[j:i + 1]}"
