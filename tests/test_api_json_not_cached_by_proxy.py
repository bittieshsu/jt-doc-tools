"""API 的 JSON 不可以被前面的快取代理存起來（v1.17.6）。

客戶 2026-10-21：共用事務機「出現過一次，關掉瀏覽器重開就消失」；伺服器端照客戶的情況（AD 帳號、
Domain Users 主要群組、同位址的自己那台、重新登入、大小寫不同）重跑都正確。差在中間：客戶走反向代理，
API 的 JSON 原本沒有 `Cache-Control`，開了快取的代理會拿網址當鍵把回應存起來 ——
加了成員之後拿到的還是舊清單，**而且不同帳號會拿到同一份**（鍵裡沒有登入的 cookie）。

這裡用真的 nginx（`proxy_cache`，跟一般反向代理開快取的設定一樣）擋在真的實例前面：
①使用者加進成員之後，經過代理重新讀清單要看得到 ②兩個帳號經過代理各自拿到自己的清單。
沒有 nginx 就跳過。
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

from tests.test_mfp_fetch_page_browser import _free_port

NGINX = shutil.which("nginx") or ("/usr/sbin/nginx" if os.path.exists("/usr/sbin/nginx") else None)
pytestmark = pytest.mark.skipif(NGINX is None, reason="沒有 nginx")

_LAUNCH = """
import sys, json
sys.path.insert(0, {root!r})
from app.core import auth_db, auth_settings, roles, sessions, user_manager, mfp_store
auth_db.init()
roles.seed_builtin_roles()
pw = "TestAdmin1234"
auth_settings.enable_local_with_admin(admin_username="jtdt-admin", admin_display_name="管理員",
                                      admin_password=pw, admin_password_confirm=pw, actor_ip="127.0.0.1")
a = user_manager.create_local("member-a", "A", "UserPass1234", roles=["clerk"])
b = user_manager.create_local("member-b", "B", "UserPass1234", roles=["clerk"])
c = mfp_store.create(mfp_store.SHARED, None, "192.168.1.31", None, None, "C3510", None)
ta, _ = sessions.issue(a, remember=False, ip="127.0.0.1", ua="edge")
tb, _ = sessions.issue(b, remember=False, ip="127.0.0.1", ua="edge")
open({out!r}, "w").write(json.dumps({{"a": ta, "b": tb, "ua": a, "conn": c["id"]}}))
import uvicorn
uvicorn.run("app.main:app", host="127.0.0.1", port={port}, log_level="warning")
"""

_NGINX_CONF = """
worker_processes 1;
error_log {d}/error.log;
pid {d}/nginx.pid;
events {{ worker_connections 64; }}
http {{
  access_log off;
  client_body_temp_path {d}/body; proxy_temp_path {d}/proxy; fastcgi_temp_path {d}/fcgi;
  uwsgi_temp_path {d}/uwsgi; scgi_temp_path {d}/scgi;
  proxy_cache_path {d}/cache keys_zone=jt:1m;
  server {{
    listen 127.0.0.1:{pport};
    location / {{
      proxy_pass http://127.0.0.1:{aport};
      proxy_set_header Host $host;
      proxy_cache jt;
      proxy_cache_valid 200 10m;
    }}
  }}
}}
"""


@pytest.fixture(scope="module")
def stack():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    d = tempfile.mkdtemp(prefix="jtdt-proxycache-")
    out = os.path.join(d, "ids.json")
    aport, pport = _free_port(), _free_port()
    env = {**os.environ, "JTDT_DATA_DIR": os.path.join(d, "data"), "JTDT_CSRF_DISABLE": "1"}
    os.makedirs(env["JTDT_DATA_DIR"])
    srv = subprocess.Popen([sys.executable, "-c", _LAUNCH.format(root=str(root), out=out, port=aport)],
                           cwd=root, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    conf = os.path.join(d, "nginx.conf")
    with open(conf, "w", encoding="utf-8") as f:
        f.write(_NGINX_CONF.format(d=d, pport=pport, aport=aport))
    ngx = subprocess.Popen([NGINX, "-p", d, "-c", conf, "-g", "daemon off;"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(120):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{aport}/healthz", timeout=1)
                urllib.request.urlopen(f"http://127.0.0.1:{pport}/healthz", timeout=1)
                if os.path.exists(out):
                    break
            except Exception:
                pass
            time.sleep(0.5)
        else:
            pytest.skip("實例或 nginx 起不來")
        ids = json.load(open(out, encoding="utf-8"))
        yield f"http://127.0.0.1:{pport}", ids, env["JTDT_DATA_DIR"], root
    finally:
        ngx.terminate()
        srv.terminate()
        for p in (ngx, srv):
            try:
                p.wait(timeout=5)
            except Exception:
                p.kill()
        shutil.rmtree(d, ignore_errors=True)


def _list(base, tok):
    req = urllib.request.Request(base + "/tools/mfp-fetch/api/connections",
                                 headers={"Cookie": f"jtdt_session={tok}", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return [x["display_name"] for x in json.loads(r.read())["connections"]], r.headers.get("Cache-Control", "")


def test_lists_through_a_caching_proxy_are_fresh_and_per_account(stack):
    base, ids, data, root = stack
    names, cc = _list(base, ids["a"])
    assert names == [] and "no-store" in cc, (names, cc)
    # 管理員把 A 加進成員（直接寫資料庫，等同在管理頁按儲存成員）
    subprocess.run([sys.executable, "-c",
                    "import sys; sys.path.insert(0, %r)\n"
                    "from app.core import mfp_store\n"
                    "mfp_store.set_members(%d, [('user', '%d')])" % (str(root), ids["conn"], ids["ua"])],
                   check=True, env={**os.environ, "JTDT_DATA_DIR": data})
    names, _ = _list(base, ids["a"])
    assert names == ["C3510"], f"經過快取代理，加了成員之後還是舊清單：{names}"
    names, _ = _list(base, ids["b"])
    assert names == [], f"經過快取代理，B 拿到了 A 的清單：{names}"
