"""LDAP / AD（以及 OIDC 身分提供者）不可以把整個服務卡住（v1.16.94）。

## 由來（客戶回報，2026-10-15）

客戶在「驗證設定」按「連線測試」與「帳號測試」，兩顆都停在「測試中…」，而且**整個 jtdt 都卡死**。

* 測試端點、一般登入、代理 SSO、管理頁的目錄功能都是 `async def`，裡面卻**直接呼叫會等網路的 LDAP 函式**。
  等的那段時間事件迴圈被佔住，**所有人的請求一起停** —— 一台 DC 慢，全站停。
* 多台 DC 用 ldap3 的 `ServerPool(active=True)`：在 ldap3 裡 `True` 是「永遠重試」（每 10 秒一輪），
  DC 全部連不上時永遠不結束。

## 判準

* 呼叫 LDAP 的地方一律丟到執行緒（`asyncio.to_thread`），用靜態檢查守住每一條路徑 —— 漏一條就是那條卡死全站。
* 實際打一次：測試端點在等 LDAP 的時候，其他請求照常回應。
* DC 全部連不上時有上限時間就放棄，不會無限重試。
* 憑證名稱不符時講得出「憑證上是什麼、你填的是什麼」（同一位客戶上一個問題）。
"""
from __future__ import annotations

import ast
import asyncio
import time
from pathlib import Path

import pytest

from app.core import auth_ldap

ROOT = Path(__file__).resolve().parent.parent

#: auth_ldap 裡不碰網路的純函式（直接呼叫沒關係）
_PURE = {"get_ou_subjects_for_dn", "uac_disabled", "filetime_to_unix", "sid_to_string",
         "primary_group_sid", "_split_servers", "_dir_root_base", "AuthError", "AuthTemporaryError"}


def _blocking_call(node: ast.Call) -> str | None:
    """這個呼叫會不會等 LDAP（或密碼雜湊這類慢動作）。是的話回名稱。"""
    f = node.func
    if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
        mod, attr = f.value.id, f.attr
        if mod == "auth_ldap" and attr not in _PURE:
            return f"auth_ldap.{attr}"
        if mod in ("_auth", "auth") and attr == "authenticate":
            return f"{mod}.authenticate"
        if mod == "proxy_sso" and attr == "resolve_user":
            return "proxy_sso.resolve_user"
        # OIDC：會連身分提供者（探索文件、換 token、簽章金鑰）
        if mod in ("oidc", "_oidc") and attr in ("discover", "build_auth_url", "exchange_code",
                                                  "verify_id_token", "logout_url"):
            return f"{mod}.{attr}"
    if isinstance(f, ast.Name) and f.id == "_sso_logout_redirect":
        return "_sso_logout_redirect"       # 包著 oidc.logout_url 的同步小函式
    return None


def _async_offenders(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: list[str] = []

    def walk(node, in_async: bool):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.AsyncFunctionDef):
                walk(child, True)
            elif isinstance(child, (ast.FunctionDef, ast.Lambda)):
                walk(child, False)          # 巢狀的同步函式本身就是丟進執行緒的那一份
            else:
                if in_async and isinstance(child, ast.Call):
                    name = _blocking_call(child)
                    if name:
                        out.append(f"{path.relative_to(ROOT)}:{child.lineno} {name}")
                walk(child, in_async)
    walk(tree, False)
    return out


def test_no_async_handler_waits_on_ldap_in_the_event_loop():
    files = [p for p in (ROOT / "app").rglob("*.py") if "__pycache__" not in p.parts]
    offenders = [o for p in files for o in _async_offenders(p)]
    assert offenders == [], ("這些 async 函式直接呼叫會等 LDAP 的函式，等的時候全站停住；"
                             "改成 await asyncio.to_thread(...)：\n" + "\n".join(offenders))


def test_the_guard_has_teeth():
    """靜態檢查本身要抓得到 —— 不然上面那條永遠綠。"""
    src = ("async def h(request):\n"
           "    auth_ldap.test_connection({})\n"
           "    _auth.authenticate('a', 'b')\n"
           "    await asyncio.to_thread(auth_ldap.test_connection, {})\n"
           "    def inner():\n"
           "        return auth_ldap.sync_all_groups()\n")
    p = ROOT / "tests" / "_tmp_guard_probe.py"
    p.write_text(src, encoding="utf-8")
    try:
        found = _async_offenders(p)
    finally:
        p.unlink()
    assert [f.split(" ")[1] for f in found] == ["auth_ldap.test_connection", "_auth.authenticate"]


def test_other_requests_keep_working_while_ldap_is_slow(admin_session, monkeypatch):
    """實際打一次：連線測試在等 LDAP（這裡用睡 1.5 秒代替）的時候，健康檢查要在它**等完之前**就回來。

    判準是時間點，不是「健康檢查花幾秒」：事件迴圈被佔住時，連「等 LDAP 開始」的那一步都要等它結束才輪得到，
    之後再量健康檢查當然很快 —— 第一版就是這樣在修正前照樣綠。"""
    import threading
    import httpx
    import app.main as app_main
    c, *_ = admin_session
    started, marks = threading.Event(), {}

    def slow(cfg):
        started.set()
        time.sleep(1.5)
        marks["ldap_done"] = time.monotonic()
        return {"ok": True, "elapsed_ms": 1500, "who": "", "vendor": ""}
    monkeypatch.setattr(auth_ldap, "test_connection", slow)

    async def run():
        transport = httpx.ASGITransport(app=app_main.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver",
                                     cookies=dict(c.cookies)) as ac:
            slow_req = asyncio.create_task(ac.post("/admin/auth-settings/ldap-test-connection",
                                                   json={"ldap": {"server_url": "ldaps://dc1.example.test"}}))
            while not started.is_set():
                await asyncio.sleep(0.01)
            r = await ac.get("/healthz")
            marks["health_done"] = time.monotonic()
            res = await slow_req
            return r.status_code, res.status_code
    status, slow_status = asyncio.run(run())
    assert status == 200 and slow_status == 200
    assert marks["health_done"] < marks["ldap_done"], \
        "LDAP 在等的時候健康檢查回不來：事件迴圈被佔住，全站都會卡"


def test_unreachable_dcs_give_up_in_bounded_time():
    """多台 DC 都連不上時要有上限時間放棄（ldap3 的 active=True 是永遠重試）。"""
    pool = auth_ldap._build_server({"server_url": "ldap://127.0.0.1:9\nldap://127.0.0.1:7"})
    assert pool.active is not True and isinstance(pool.active, int) and pool.active >= 1
    t0 = time.monotonic()
    with pytest.raises(auth_ldap.AuthError):
        auth_ldap.test_connection({"server_url": "ldap://127.0.0.1:9\nldap://127.0.0.1:7",
                                   "service_dn": "CN=svc,DC=example,DC=test", "service_password": "x",
                                   "use_tls": False})
    assert time.monotonic() - t0 < 15, "DC 都連不上卻等太久"


_CERT_ERR = ("socket ssl wrapping error: certificate {'subject': (), 'issuer': ((('domainComponent', 'local'),), "
             "(('domainComponent', 'example'),), (('commonName', 'example-DC-02-CA'),)), 'version': 3, "
             "'subjectAltName': (('DNS', 'dc-01.example.local'),), 'caIssuers': ('ldap:///CN=example-DC-02-CA',)} "
             "doesn't match any name in ['192.0.2.5'] ")


def test_certificate_name_mismatch_is_explained():
    msg = auth_ldap.explain_ldap_error(Exception(_CERT_ERR))
    assert "dc-01.example.local" in msg and "192.0.2.5" in msg, msg
    assert "{" not in msg, "不要把整份憑證字典倒給使用者"


def test_other_errors_keep_their_text():
    assert "timed out" in auth_ldap.explain_ldap_error(Exception("socket connection error: timed out"))
