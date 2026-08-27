"""這台機器實際能收多大的檔案 —— 系統狀態頁的「可上傳的檔案大小」。

使用者問「我們可以拉多大的檔案進來？是在哪裡設定？」—— 查下來答案散在
十幾個地方：反向代理的設定在別台機器上、各工具的上限寫死在程式裡、工作區
的額度在管理設定。管理員只能翻程式碼才知道。

**反向代理那一層沒有 header 可以讀**（HTTP 沒有這種標準 header），但
`Expect: 100-continue` 問得到：只送標頭、不送 body，對方回 `100 Continue`
或 `413`。二分收斂幾次就有答案，而且完全不傳輸資料。
"""
from __future__ import annotations

import socket
import threading

import pytest

from app.core import upload_limits as ul


class _FakeServer:
    """會依 Content-Length 決定回 100 還是 413 的極小 HTTP 伺服器。"""

    def __init__(self, limit_bytes: int | None):
        self.limit = limit_bytes
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        self.requests = 0
        self._stop = False
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        while not self._stop:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            try:
                data = conn.recv(4096).decode("latin-1", "replace")
                self.requests += 1
                length = 0
                for line in data.split("\r\n"):
                    if line.lower().startswith("content-length:"):
                        length = int(line.split(":", 1)[1].strip() or 0)
                if self.limit is not None and length > self.limit:
                    conn.sendall(b"HTTP/1.1 413 Request Entity Too Large\r\n"
                                 b"Connection: close\r\n\r\n")
                else:
                    conn.sendall(b"HTTP/1.1 100 Continue\r\n\r\n")
            except Exception:
                pass
            finally:
                try:
                    conn.close()
                except Exception:
                    pass

    def close(self):
        self._stop = True
        try:
            self.sock.close()
        except Exception:
            pass


def test_probe_finds_the_limit_without_sending_any_body():
    """量得出上限，而且**一個位元組的 body 都不送**（這是整個做法的重點）。"""
    srv = _FakeServer(limit_bytes=100 * 1024 * 1024)     # 100 MB
    try:
        got = ul.probe_max_body_mb("127.0.0.1", srv.port, False, "/healthz")
    finally:
        srv.close()
    assert got["ok"] is True
    assert got["unlimited"] is False
    assert got["max_mb"] == 100, got
    # 二分搜尋要收斂得夠快 —— 每一次都是一個連線，太多次代表演算法寫壞了
    assert got["requests"] <= 25, f"探測了 {got['requests']} 次，太多"


def test_probe_reports_no_limit_when_there_is_none():
    srv = _FakeServer(limit_bytes=None)
    try:
        got = ul.probe_max_body_mb("127.0.0.1", srv.port, False, "/healthz")
    finally:
        srv.close()
    assert got["ok"] is True and got["unlimited"] is True
    assert "沒有設定大小限制" in got["detail"]


def test_probe_says_it_cannot_tell_rather_than_guessing():
    """**問不到要說問不到。**

    回一個看起來像答案的數字最糟 —— 管理員照著一個錯的值去設定，比沒有這個
    功能還糟。連不上時必須 ok=False。
    """
    # 綁一個沒有人在聽的埠
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    got = ul.probe_max_body_mb("127.0.0.1", port, False, "/healthz")
    assert got["ok"] is False
    assert got["max_mb"] is None
    assert got["unlimited"] is False


def test_tiny_limit_is_reported_not_swallowed():
    srv = _FakeServer(limit_bytes=100 * 1024)            # 100 KB，比 1 MB 還小
    try:
        got = ul.probe_max_body_mb("127.0.0.1", srv.port, False, "/healthz")
    finally:
        srv.close()
    assert got["ok"] is True and got["max_mb"] == 0
    assert "極小" in got["detail"]


# --- 應用程式這一側的清單 ----------------------------------------------

def test_app_side_limits_are_listed():
    rows = ul.app_side_limits()
    keys = {r["key"] for r in rows}
    # 這幾項是使用者實際會撞到的，少列一項就等於沒答到他的問題
    for must in ("app_global", "ws_file", "ws_quota", "autosave",
                 "office_convert", "logo"):
        assert must in keys, f"清單少了 {must}"


def test_configurable_flag_is_honest():
    """說「可調」卻其實寫死在程式裡最糟 —— 管理員會去設定頁找一個不存在的欄位。"""
    rows = {r["key"]: r for r in ul.app_side_limits()}
    assert rows["ws_file"]["configurable"] is True
    assert rows["ws_quota"]["configurable"] is True
    for k in ("autosave", "office_convert", "logo", "einvoice"):
        assert rows[k]["configurable"] is False, f"{k} 其實是寫死的"


def test_app_has_no_global_limit_and_says_so():
    """目前應用程式端**沒有**全域上傳上限 —— 這件事要講出來，不要留白。

    直連本機埠（內網部署很常見）時等於完全沒有限制，管理員應該知道。
    """
    row = next(r for r in ul.app_side_limits() if r["key"] == "app_global")
    assert row["value_mb"] is None
    assert "沒有" in row["note"]


# --- 端點 ---------------------------------------------------------------

def test_probe_endpoint_does_not_accept_a_target(admin_session):
    """**不可以讓呼叫端指定要連哪裡** —— 那就是一個現成的 SSRF 入口。"""
    import inspect
    from app.admin import auth_router
    src = inspect.getsource(auth_router)
    i = src.index("async def upload_limit_probe")
    body = src[i:i + 2500]
    assert "request.headers.get(\"host\")" in body, "沒有綁在當前連線的 Host 上"
    for bad in ('body.get("host")', 'body.get("url")', "params.get("):
        assert bad not in body, f"端點接受了外部指定的目標：{bad}"


def test_probe_endpoint_requires_admin(admin_session):
    """**啟用認證之後**未登入不可以呼叫。

    要用 `admin_session` 這個 fixture —— 它會把認證打開。沒有它的話認證是
    關閉的（單機模式沒有帳號概念，管理區本來就開放），測試會拿到 200 而
    看起來像「沒有保護」，其實是測試前提不對。
    """
    from fastapi.testclient import TestClient
    import app.main as app_main
    anon = TestClient(app_main.app)          # 全新 client，沒有 session cookie
    r = anon.post("/admin/api/upload-limit/probe", follow_redirects=False)
    assert r.status_code in (302, 303, 401, 403, 404), r.status_code
