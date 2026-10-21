"""共用事務機：關掉瀏覽器再打開，清單照樣有（客戶 2026-10-21）。

客戶：AD 帳號 jchang，C3510 的成員是 jchang＋Domain Users、6535 / C3500 的成員是 Domain Users；
自己也新增了同位址的 C3510。「出現過一次，重複開啟、關閉瀏覽器後就消失了」。

這裡照那個情況在真的瀏覽器裡跑：同一個瀏覽器設定檔、記住登入（持續的 cookie），
開頁 → 關掉整個瀏覽器 → 用同一個設定檔再開 → 再開頁（三輪），每一輪三台共用的都要在，
自己那台也在。帳號照 AD 登入的路徑同步（DN、Domain Users 當主要群組）。
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

from tests.test_mfp_fetch_page_browser import _Tab, _browser, _free_port, _open

pytestmark = pytest.mark.skipif(
    _browser() is None or __import__("importlib").util.find_spec("websockets") is None,
    reason="沒有 chromium / websockets")

DU = "CN=Domain Users,CN=Users,DC=corp,DC=example"

_LAUNCH = """
import sys, json
sys.path.insert(0, {root!r})
from app.core import auth_db, auth_settings, auth_ldap, roles, sessions, mfp_store, permissions
auth_db.init()
roles.seed_builtin_roles()
pw = "TestAdmin1234"
auth_settings.enable_local_with_admin(admin_username="jtdt-admin", admin_display_name="管理員",
                                      admin_password=pw, admin_password_confirm=pw, actor_ip="127.0.0.1")
u = auth_ldap._sync_directory_user("jchang", {{"display_name": "張先生", "user_dn": "CN=Jchang,OU=IT,DC=corp,DC=example",
                                               "group_dns": [{du!r}], "email": ""}}, "ad")
uid = u["user_id"]
gid = auth_db.conn().execute("select id from groups where external_dn=?", ({du!r},)).fetchone()[0]
def shared(host, name):
    return mfp_store.create(mfp_store.SHARED, None, host, None, None, name, None)["id"]
a, b, c = shared("192.168.9.34", "6535"), shared("192.168.9.32", "C3500"), shared("192.168.9.31", "C3510")
mfp_store.set_members(a, [("group", str(gid))])
mfp_store.set_members(b, [("group", str(gid))])
mfp_store.set_members(c, [("user", str(uid)), ("group", str(gid))])
mfp_store.create("u%d" % uid, uid, "192.168.9.31", None, None, "C3510", None)
tok, _ = sessions.issue(uid, remember=True, ip="127.0.0.1", ua="edge")
open({out!r}, "w").write(json.dumps({{"tok": tok}}))
import uvicorn
uvicorn.run("app.main:app", host="127.0.0.1", port={port}, log_level="warning")
"""


def _start_browser(profile, cdp):
    return subprocess.Popen([_browser(), "--headless=new", "--no-sandbox", "--disable-gpu",
                             f"--user-data-dir={profile}", f"--remote-debugging-port={cdp}",
                             "--remote-allow-origins=*", "about:blank"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _wait(url):
    for _ in range(120):
        try:
            urllib.request.urlopen(url, timeout=1)
            return True
        except Exception:
            time.sleep(0.5)
    return False


def _stop(p):
    p.terminate()
    try:
        p.wait(timeout=5)
    except Exception:
        p.kill()


def test_shared_devices_survive_closing_and_reopening_the_browser():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    d = tempfile.mkdtemp(prefix="jtdt-reopen-")
    out, profile = os.path.join(d, "tok.json"), os.path.join(d, "profile")
    port = _free_port()
    env = {**os.environ, "JTDT_DATA_DIR": os.path.join(d, "data"), "JTDT_CSRF_DISABLE": "1"}
    os.makedirs(env["JTDT_DATA_DIR"])
    srv = subprocess.Popen([sys.executable, "-c", _LAUNCH.format(root=str(root), du=DU, out=out, port=port)],
                           cwd=root, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        assert _wait(f"http://127.0.0.1:{port}/healthz") and os.path.exists(out), "實例起不來"
        tok = json.load(open(out, encoding="utf-8"))["tok"]
        base = f"http://127.0.0.1:{port}"
        for round_ in range(3):
            cdp = _free_port()
            br = _start_browser(profile, cdp)
            try:
                assert _wait(f"http://127.0.0.1:{cdp}/json/version"), "瀏覽器起不來"
                with _open(cdp) as ws:
                    t = _Tab(ws)
                    for dom in ("Runtime", "Page", "Network"):
                        t.send(f"{dom}.enable")
                    if round_ == 0:      # 第一次「登入」：之後靠瀏覽器自己記住的 cookie
                        t.send("Network.setCookie", {"name": "jtdt_session", "value": tok, "url": base,
                                                     "expires": time.time() + 86400})
                    t.send("Page.navigate", {"url": base + "/tools/mfp-fetch/"})
                    assert t.wait_for("!!document.querySelector('#mfListWrap table')"), \
                        f"第 {round_ + 1} 次開：清單沒有出來 " + str(t.js("location.href"))
                    rows = t.js("[...document.querySelectorAll('#mfListWrap tbody tr')].map(r => "
                                "[r.cells[0].firstChild.textContent.trim(), !!r.querySelector('.mf-src.shared')])")
                    assert sorted(map(tuple, rows)) == [("6535", True), ("C3500", True), ("C3510", False), ("C3510", True)], \
                        f"第 {round_ + 1} 次開：{rows}"
                    assert not t.errs, t.errs
                    # 正常關掉整個瀏覽器（像使用者按 X）：cookie 會寫回設定檔；直接砍行程不會
                    try:
                        t.send("Browser.close")
                    except Exception:  # noqa: BLE001 —— 關掉時連線會斷
                        pass
                try:
                    br.wait(timeout=10)
                except Exception:  # noqa: BLE001
                    pass
            finally:
                _stop(br)
    finally:
        _stop(srv)
        shutil.rmtree(d, ignore_errors=True)
