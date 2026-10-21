"""API Token 頁的「綁定使用者」—— 在真的瀏覽器裡跑一次（v1.17.6）。

使用者 2026-10-21：建了 MCP 用的 Token 卻找不到地方綁使用者。這裡照畫面操作：
沒綁的那張顯示「未綁定」→ 按「綁定」→ 對話框搜尋 → 選人 → 儲存 → 重新載入後顯示 `帳號@來源`；
再按「改綁」→「解除綁定」→ 確認 → 回到「未綁定」。中英文各跑一次，不可以有 JS 錯誤。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

import pytest

from tests.test_mfp_fetch_page_browser import _Tab, _browser, _free_port, _open, _profile_arg  # noqa: F401

pytestmark = pytest.mark.skipif(
    _browser() is None or __import__("importlib").util.find_spec("websockets") is None,
    reason="沒有 chromium / websockets —— 這條要真的瀏覽器才驗得到")

_LAUNCH = """
import sys
sys.path.insert(0, {root!r})
from app.core import api_tokens as at, auth_db, auth_settings, roles, sessions, user_manager
auth_db.init()
roles.seed_builtin_roles()
pw = "TestAdmin1234"
auth_settings.enable_local_with_admin(admin_username="jtdt-admin", admin_display_name="管理員",
                                      admin_password=pw, admin_password_confirm=pw, actor_ip="127.0.0.1")
user_manager.create_local("ai-owner", "AI 帳號", "UserPass1234")
at.api_tokens.create("mcp")
admin_id = auth_db.conn().execute("select id from users where username='jtdt-admin'").fetchone()[0]
tok, _ = sessions.issue(admin_id, remember=False, ip="127.0.0.1", ua="pytest")
open({tokfile!r}, "w").write(tok)
import uvicorn
uvicorn.run("app.main:app", host="127.0.0.1", port={port}, log_level="warning")
"""


@pytest.fixture(scope="module")
def live():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    data = tempfile.mkdtemp(prefix="tokowner-")
    tokfile = os.path.join(data, "tok.txt")
    port, cdp = _free_port(), _free_port()
    env = {**os.environ, "JTDT_DATA_DIR": data, "JTDT_CSRF_DISABLE": "1"}
    srv = subprocess.Popen([sys.executable, "-c", _LAUNCH.format(root=str(root), tokfile=tokfile, port=port)],
                           cwd=root, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    br = subprocess.Popen(
        [_browser(), "--headless=new", "--no-sandbox", "--disable-gpu",
         _profile_arg(), f"--remote-debugging-port={cdp}", "--remote-allow-origins=*", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(120):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1)
                urllib.request.urlopen(f"http://127.0.0.1:{cdp}/json/version", timeout=1)
                if os.path.exists(tokfile):
                    break
            except Exception:
                pass
            time.sleep(0.5)
        else:
            pytest.skip("實例或瀏覽器起不來")
        yield port, cdp, open(tokfile, encoding="utf-8").read().strip()
    finally:
        br.terminate()
        srv.terminate()
        try:
            br.wait(timeout=5)
            srv.wait(timeout=5)
        except Exception:
            br.kill()
            srv.kill()
        shutil.rmtree(data, ignore_errors=True)


_ROW = "[...document.querySelectorAll('tr[data-token]')].find(r => r.querySelector('strong').textContent === 'mcp')"
# 列出現只代表 HTML 解析到了；頁面程式在最後面，掛好事件之前按下去會被吃掉
_READY = "document.readyState === 'complete' && typeof openOwner === 'function'"


@pytest.mark.parametrize("locale,unbound,query", [("zh-Hant", "未綁定", "AI 帳號"), ("en", "Not bound", "ai-owner")])
def test_bind_and_unbind_on_the_page(live, locale, unbound, query):
    port, cdp, tok = live
    base = f"http://127.0.0.1:{port}"
    with _open(cdp) as ws:
        t = _Tab(ws)
        for d in ("Runtime", "Log", "Page", "Network"):
            t.send(f"{d}.enable")
        t.send("Network.setCookie", {"name": "jtdt_locale", "value": locale, "url": base})
        t.send("Network.setCookie", {"name": "jtdt_session", "value": tok, "url": base})
        t.send("Page.navigate", {"url": base + "/admin/api-tokens"})
        assert t.wait_for(f"!!({_ROW}) && {_READY}"), "Token 清單沒有載入"
        assert unbound in t.js(f"({_ROW}).querySelector('.tok-owner').innerText")
        t.js(f"({_ROW}).querySelector('.btn-owner').click()")
        assert t.wait_for("document.getElementById('ownerDlg').open"), "對話框沒有打開"
        assert t.wait_for("document.querySelectorAll('#ownerList input[type=radio]').length >= 2"), "沒有列出使用者"
        t.shot(f"api_tokens_owner_dialog_{locale}", 1280)
        # 回應順序反過來（空白搜尋晚回來）也要顯示最後一次搜尋的結果
        t.js("""(() => { const real = window.fetch; window.fetch = (u, o) => String(u).includes('owner-candidates?q=&') || String(u).endsWith('owner-candidates?q=')
              ? new Promise(res => setTimeout(() => res(real(u, o)), 1500)) : real(u, o); return true; })()""")
        t.js("ownerSearch(); true")     # 不回傳 promise：t.js 會等它，等於沒有亂序
        t.js(f"const q = document.getElementById('ownerQ'); q.value = {query!r}; q.dispatchEvent(new Event('input'))")
        time.sleep(2)
        assert t.wait_for("document.querySelectorAll('#ownerList input[type=radio]').length === 1"), \
            t.js("document.getElementById('ownerList').innerText")
        assert t.js("document.getElementById('ownerSave').disabled"), "還沒選人不能儲存"
        t.js("document.querySelector('#ownerList input[type=radio]').click()")
        t.js("document.getElementById('ownerSave').click()")
        assert t.wait_for(f"!!({_ROW}) && ({_ROW}).querySelector('.tok-owner').innerText.includes('ai-owner@local') && {_READY}"), \
            "儲存後要顯示 帳號@來源"
        t.shot(f"api_tokens_owner_{locale}_1280", 1280)
        # 解除綁定：確認框不可以被對話框蓋住（按得到「確定」）
        t.js(f"({_ROW}).querySelector('.btn-owner').click()")
        assert t.wait_for("document.getElementById('ownerDlg').open")
        t.js("document.getElementById('ownerClear').click()")
        assert t.wait_for("!!document.querySelector('.modal-ok')"), "解除要先確認"
        assert t.js("!document.getElementById('ownerDlg').open"), "確認框出現時對話框要先關掉，不然會被蓋住"
        t.js("document.querySelector('.modal-ok').click()")
        assert t.wait_for(f"!!({_ROW}) && ({_ROW}).querySelector('.tok-owner').innerText.includes({unbound!r})")
        assert not t.errs, t.errs


def test_mfp_page_says_which_account_is_signed_in(live):
    """事務機掃描取件頁寫出目前登入的帳號（客戶 2026-10-21：同名的 AD / SSO 帳號不同，共用事務機時有時無）。"""
    port, cdp, tok = live
    base = f"http://127.0.0.1:{port}"
    with _open(cdp) as ws:
        t = _Tab(ws)
        for d in ("Runtime", "Log", "Page", "Network"):
            t.send(f"{d}.enable")
        t.send("Network.setCookie", {"name": "jtdt_locale", "value": "zh-Hant", "url": base})
        t.send("Network.setCookie", {"name": "jtdt_session", "value": tok, "url": base})
        t.send("Page.navigate", {"url": base + "/tools/mfp-fetch/"})
        assert t.wait_for("!document.getElementById('mfYou').hidden"), "要寫出目前登入的帳號"
        txt = t.js("document.getElementById('mfYou').innerText")
        assert "jtdt-admin@local" in txt and "不同帳號" in txt, txt
        assert not t.errs, t.errs
