"""「我的作業 → 完成通知」的 Zulip 欄位 —— 在真的瀏覽器裡跑一次（v1.17.0）。

使用者 2026-10-18：「預設請填 email 不能留空」「使用者可以改，但預設必須要抓帳號本身的」。

* 帳號有信箱、沒填過 Zulip email → 欄位**已經填好**帳號信箱（不是只放在提示裡）。
* 改成別的 → 存檔後重新載入，是改過的那個。
* 清空再存 → 補回帳號信箱（不可以留空）。
"""
from __future__ import annotations

import json
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

ACCOUNT = "clerk-zulip@example.test"

_LAUNCH = """
import sys, json
sys.path.insert(0, {root!r})
from app.core import auth_db, auth_settings, notify_settings as ns, roles, sessions, user_manager
auth_db.init()
roles.seed_builtin_roles()
pw = "TestAdmin1234"
auth_settings.enable_local_with_admin(admin_username="jtdt-admin", admin_display_name="管理員",
                                      admin_password=pw, admin_password_confirm=pw, actor_ip="127.0.0.1")
uid = user_manager.create_local("clerk", "職員", "UserPass1234")
user_manager.update(uid, email={account!r})
ns.save({{"enabled": True, "channels": {{"zulip": {{"enabled": True, "zulip_site": {site!r},
          "zulip_bot_email": "it-notify-bot@z.example", "zulip_api_key": "good-key", "zulip_stream": ""}}}}}})
import app.core.zulip_link as zl
zl.POLL_INTERVAL = 0.5
tok, _ = sessions.issue(uid, remember=False, ip="127.0.0.1", ua="pytest")
admin_id = auth_db.conn().execute("select id from users where username='jtdt-admin'").fetchone()[0]
atok, _ = sessions.issue(admin_id, remember=False, ip="127.0.0.1", ua="pytest")
open({tokfile!r}, "w").write(tok + " " + atok)
import uvicorn
uvicorn.run("app.main:app", host="127.0.0.1", port={port}, log_level="warning")
"""


@pytest.fixture(scope="module")
def live():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    from tests.test_zulip_link import FakeZulip
    fake = FakeZulip().start()
    data = tempfile.mkdtemp(prefix="mynotify-")
    tokfile = os.path.join(data, "tok.txt")
    port, cdp = _free_port(), _free_port()
    env = {**os.environ, "JTDT_DATA_DIR": data, "JTDT_CSRF_DISABLE": "1"}
    srv = subprocess.Popen([sys.executable, "-c", _LAUNCH.format(root=str(root), account=ACCOUNT, tokfile=tokfile,
                                                                 port=port, site=fake.site)],
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
        toks = open(tokfile, encoding="utf-8").read().split()
        yield port, cdp, toks[0], toks[1], fake
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
        fake.srv.shutdown()


def _zulip_input(t):
    return t.js("(() => { const i = document.querySelector('.mn-to[data-field=\"zulip_to\"]'); return i ? i.value : null; })()")


def _reload(t, base):
    t.send("Page.navigate", {"url": base + "/my-jobs"})
    assert t.wait_for("!!document.querySelector('.mn-to[data-field=\"zulip_to\"]')"), "完成通知裡要有 Zulip 欄位"


def _save(t):
    t.js("document.getElementById('mn-save').click()")
    assert t.wait_for("!document.getElementById('mn-saved').hidden"), "存檔沒有完成"


def test_zulip_field_is_prefilled_with_the_account_email(live):
    port, cdp, tok, _admin, _fake = live
    base = f"http://127.0.0.1:{port}"
    with _open(cdp) as ws:
        t = _Tab(ws)
        for d in ("Runtime", "Log", "Page", "Network"):
            t.send(f"{d}.enable")
        t.send("Network.setCookie", {"name": "jtdt_locale", "value": "zh-Hant", "url": base})
        t.send("Network.setCookie", {"name": "jtdt_session", "value": tok, "url": base})
        _reload(t, base)
        assert _zulip_input(t) == ACCOUNT, "預設就填好帳號的信箱，不是空的"
        hint = t.js("document.querySelector('.mn-to[data-field=\"zulip_to\"]').parentElement.innerText")
        assert "預設是帳號的信箱" in hint, hint
        # 改成別的 → 存 → 重新載入是改過的
        t.js("document.querySelector('.mn-to[data-field=\"zulip_to\"]').value = 'chat-me@example.test'")
        _save(t)
        _reload(t, base)
        assert _zulip_input(t) == "chat-me@example.test"
        # 清空 → 存 → 補回帳號信箱
        t.js("document.querySelector('.mn-to[data-field=\"zulip_to\"]').value = ''")
        _save(t)
        _reload(t, base)
        assert _zulip_input(t) == ACCOUNT, "不可以留空：清空存檔要補回帳號信箱"
        stored = json.loads(urllib.request.urlopen(urllib.request.Request(
            base + "/api/my/notify", headers={"Cookie": f"jtdt_session={tok}"})).read())
        assert stored["prefs"].get("zulip_to") == ACCOUNT, stored["prefs"]
        assert not [e for e in t.errs if "favicon" not in e], t.errs



def test_admin_zulip_test_needs_a_channel_or_a_test_recipient(live):
    """管理頁（使用者 2026-10-18）：團隊頻道沒填 → 出現「測試收訊者」，沒填按測試會說要填；填了頻道 → 欄位收起來。"""
    port, cdp, _tok, atok, _fake = live
    base = f"http://127.0.0.1:{port}"
    with _open(cdp) as ws:
        t = _Tab(ws)
        for d in ("Runtime", "Log", "Page", "Network"):
            t.send(f"{d}.enable")
        t.send("Network.setCookie", {"name": "jtdt_locale", "value": "zh-Hant", "url": base})
        t.send("Network.setCookie", {"name": "jtdt_session", "value": atok, "url": base})
        t.send("Page.navigate", {"url": base + "/admin/notify"})
        assert t.wait_for("!!document.querySelector('[data-zulip-test]')"), "管理頁要有 Zulip 的測試收訊者欄位"
        # 等頁面的程式跑完（按鈕的事件掛上）再按 —— 元素先出現、程式在頁尾，太早按什麼都不會發生
        assert t.wait_for("document.readyState === 'complete'")
        assert not t.js("document.querySelector('[data-zulip-test]').hidden"), "沒有團隊頻道時要出現"
        t.js("""(() => { const b = document.querySelector('[data-zulip-test]').closest('.nt-ch-body');
                 b.querySelector('.nt-test-btn').click(); return true; })()""")
        res_js = "document.querySelector('[data-zulip-test]').closest('.nt-ch-body').querySelector('.nt-test-res').textContent"
        assert t.wait_for(f"{res_js}.includes('測試收訊者')"), t.js(res_js)
        t.js("""(() => { const i = document.querySelector('input[data-f="zulip_stream"]'); i.value = 'general';
                 i.dispatchEvent(new Event('input')); return true; })()""")
        assert t.js("document.querySelector('[data-zulip-test]').hidden"), "填了團隊頻道就收起測試收訊者"
        assert not [e for e in t.errs if "favicon" not in e], t.errs



def test_linking_zulip_by_dm_to_the_bot(live):
    """綁定（Zulip 維護人員建議的方案）：按綁定 → 畫面上的代碼 → 在 Zulip 私訊 Bot → 即將綁定：名稱 → 確認 → 已綁定。"""
    port, cdp, tok, _admin, fake = live
    base = f"http://127.0.0.1:{port}"
    with _open(cdp) as ws:
        t = _Tab(ws)
        for d in ("Runtime", "Log", "Page", "Network"):
            t.send(f"{d}.enable")
        t.send("Network.setCookie", {"name": "jtdt_locale", "value": "zh-Hant", "url": base})
        t.send("Network.setCookie", {"name": "jtdt_session", "value": tok, "url": base})
        _reload(t, base)
        assert t.wait_for("!!document.getElementById('mn-zl') && document.getElementById('mn-zl').innerText.includes('綁定 Zulip')"), \
            "Zulip 那一列要有「綁定 Zulip」"
        t.js("[...document.querySelectorAll('#mn-zl button')].find(b => b.innerText.includes('綁定 Zulip')).click()")
        assert t.wait_for("!!document.querySelector('#mn-zl code')"), t.js("document.getElementById('mn-zl').innerText")
        code = t.js("document.querySelector('#mn-zl code').innerText").strip()
        box = t.js("document.getElementById('mn-zl').innerText")
        assert code.startswith("JTDT-") and "IT 通知" in box, box
        fake.dm(9, code)                                       # 使用者在 Zulip 私訊 Bot
        assert t.wait_for("document.getElementById('mn-zl').innerText.includes('即將綁定')", 30), \
            t.js("document.getElementById('mn-zl').innerText")
        assert "Jason Cheng" in t.js("document.getElementById('mn-zl').innerText")
        t.shot("zulip_link_confirm_1280", 1280)
        t.js("[...document.querySelectorAll('#mn-zl button')].find(b => b.innerText.includes('確認綁定')).click()")
        assert t.wait_for("document.getElementById('mn-zl').innerText.includes('已綁定 Zulip')"), \
            t.js("document.getElementById('mn-zl').innerText")
        assert t.js("document.querySelector('.mn-to[data-field=\"zulip_to\"]').hidden"), "綁定後 email 欄位用不到了，要收起來"
        assert t.js("document.querySelector('.mn-zl-email').hidden")
        t.shot("zulip_link_done_1280", 1280)
        t.shot("zulip_link_done_390", 390)
        assert not [e for e in t.errs if "favicon" not in e], t.errs
