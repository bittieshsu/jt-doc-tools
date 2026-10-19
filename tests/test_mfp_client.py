"""事務機取件：連線測試的網路用戶端（目的地政策＋逐步診斷）。

這支會讓伺服器去連使用者指定的位址 —— 等於一條 SSRF 管道，所以政策兩個方向都測：
該擋的（回送、metadata、公網、伺服器自己、非網頁埠、DNS rebinding）要擋，內網設備要放。

診斷用**真的網路連線**測：本機起一台假的「事務機網頁」（HTTP 與自簽憑證 HTTPS）。
假頁面的內容是自己編的，測的是用戶端行為（跟轉址、不跨來源、不帶帳密、遮蔽、逾時），
不是宣稱看得懂真機。測試時放行 127.0.0.1 與隨機埠；正式規則另外有測試釘住。
"""
from __future__ import annotations

import datetime as dt
import ipaddress
import json
import socket
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.core import mfp_client as mc

# --------------------------------------------------------------------- 政策


@pytest.mark.parametrize("raw", ["http://10.0.0.5", "10.0.0.5/index", "admin@10.0.0.5",
                                 "10.0.0.5:80", "", "   ", "a b", "-bad.example", "1.2.3"])
def test_host_input_must_be_a_bare_address(raw):
    with pytest.raises(mc.PolicyError) as e:
        mc.parse_host(raw)
    assert e.value.code == "HOST_INVALID"


@pytest.mark.parametrize("raw,want", [("10.0.0.5", "10.0.0.5"), (" 192.168.77.20 ", "192.168.77.20"),
                                      ("mfp3.corp.local", "mfp3.corp.local"),
                                      ("[fd00::5]", "fd00::5"), ("MFP3.Corp.Local.", "mfp3.corp.local")])
def test_host_input_accepts_ips_and_names(raw, want):
    assert mc.parse_host(raw) == want


@pytest.mark.parametrize("port", [22, 25, 139, 445, 3306, 5432, 6379, 9100, 0, 70000, "x"])
def test_only_web_ports_can_be_tested(port):
    with pytest.raises(mc.PolicyError) as e:
        mc.parse_port_scheme(port, None)
    assert e.value.code == "PORT_NOT_ALLOWED"


def test_port_and_scheme_defaults():
    assert mc.parse_port_scheme(None, None) == [("https", 443), ("http", 80)]
    assert mc.parse_port_scheme(8443, None) == [("https", 8443)]
    assert mc.parse_port_scheme(8080, None) == [("http", 8080)]
    assert mc.parse_port_scheme(None, "http") == [("http", 80)]
    with pytest.raises(mc.PolicyError):
        mc.parse_port_scheme(None, "ftp")


@pytest.mark.parametrize("ip,code", [
    ("127.0.0.1", "ADDR_LOOPBACK"), ("::1", "ADDR_LOOPBACK"), ("::ffff:127.0.0.1", "ADDR_LOOPBACK"),
    ("169.254.169.254", "ADDR_LINK_LOCAL"), ("fe80::1", "ADDR_LINK_LOCAL"),
    ("224.0.0.1", "ADDR_MULTICAST"), ("0.0.0.0", "ADDR_RESERVED"), ("0.1.2.3", "ADDR_RESERVED"),
    ("8.8.8.8", "ADDR_PUBLIC"), ("100.64.1.1", "ADDR_PUBLIC"), ("2001:4860::8888", "ADDR_PUBLIC"),
])
def test_addresses_that_must_be_refused(ip, code, monkeypatch):
    monkeypatch.setattr(mc, "_self_addresses", lambda: frozenset())
    assert mc.check_address(ip) == code


@pytest.mark.parametrize("ip", ["192.168.77.20", "10.1.2.3", "172.16.5.5", "fd00::20"])
def test_lan_devices_are_allowed(ip, monkeypatch):
    monkeypatch.setattr(mc, "_self_addresses", lambda: frozenset())
    assert mc.check_address(ip) is None


def test_the_servers_own_address_is_refused(monkeypatch):
    monkeypatch.setattr(mc, "_self_addresses", lambda: frozenset({"192.168.77.30"}))
    assert mc.check_address("192.168.77.30") == "ADDR_SELF"


def test_self_addresses_are_really_listed():
    """不 mock 的那一份：本機至少有一個位址被列出來（不然 ADDR_SELF 永遠不會觸發）。"""
    mc._SELF_CACHE = (0.0, frozenset())
    addrs = mc._self_addresses()
    assert addrs and any(ipaddress.ip_address(a).is_loopback for a in addrs)


def test_one_bad_dns_answer_refuses_the_whole_name(monkeypatch):
    """DNS 回一個內網、一個 metadata 位址 → 整個拒絕（只挑合格的連，等於讓 DNS 決定連哪裡）。"""
    monkeypatch.setattr(mc, "_self_addresses", lambda: frozenset())
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.77.20", 443)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 443))])
    with pytest.raises(mc.PolicyError) as e:
        mc.probe("mfp.example.lan")
    assert e.value.code == "ADDR_LINK_LOCAL"
    assert {a["ip"] for a in e.value.detail["addresses"]} == {"192.168.77.20", "169.254.169.254"}


# --------------------------------------------------------------------- 假的事務機網頁

SEEN: list[dict] = []
COOKIE_VALUE = "S3CRET-COOKIE-VALUE-123"


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        SEEN.append({"path": self.path, "headers": dict(self.headers)})
        p = self.path
        if p == "/":
            self.send_response(302)
            self.send_header("Location", "/home/index.html?lang=ja&session=abc999")
            self.send_header("Set-Cookie", f"JSID={COOKIE_VALUE}; Path=/; HttpOnly")
            self.end_headers()
            return
        if p.startswith("/home/index.html"):
            body = ("<html><head><title>Apeos C5571 - Internet Services</title>"
                    "<meta http-equiv='refresh' content='0;URL=/top.html'></head>"
                    "<body><script>var x='password=hunter2';</script>"
                    "FUJIFILM Business Innovation <frameset><frame src='/frame1.html'>"
                    "<frame src='http://evil.example/steal'></frameset>"
                    "<form action='/login.cgi?token=t0k3n' method='post'>"
                    "<input name='userId'><input name='passwd' type='password' value='zzz'>"
                    "<input type='hidden' name='csrf' value='CSRFVALUE'></form>"
                    "Firmware Version: 72.18.5</body></html>").encode("utf-8")
            self._ok(body)
            return
        if p in ("/top.html", "/frame1.html"):
            self._ok(f"<html><title>{p}</title><body>page {p}</body></html>".encode())
            return
        if p == "/big":
            self._ok(b"<html><title>big</title>" + b"x" * (mc.MAX_BODY + 5000))
            return
        self.send_response(404)
        self.end_headers()

    def _ok(self, body: bytes):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Server", "Apache")
        self.end_headers()
        self.wfile.write(body)


def _serve(ctx: ssl.SSLContext | None = None):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    if ctx is not None:
        srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv


@pytest.fixture
def open_lo(monkeypatch):
    """測試時放行 127.0.0.1 與隨機埠（正式規則在上面那幾條測）。"""
    real = mc.check_address
    monkeypatch.setattr(mc, "check_address", lambda ip: None if ip == "127.0.0.1" else real(ip))
    SEEN.clear()

    def allow(port):
        monkeypatch.setattr(mc, "ALLOWED_PORTS", tuple(mc.ALLOWED_PORTS) + (port,))
    return allow


def test_http_probe_follows_same_origin_only_and_records_everything(open_lo):
    srv = _serve()
    port = srv.server_address[1]
    open_lo(port)
    try:
        rep = mc.probe("127.0.0.1", port, "http")
    finally:
        srv.shutdown()
    paths = [s["path"] for s in SEEN]
    assert paths[0] == "/" and any(p.startswith("/home/index.html") for p in paths)
    assert "/top.html" in paths and "/frame1.html" in paths, "同一台設備的 meta refresh 與框架要跟"
    att = rep["attempts"][0]
    codes = [s["code"] for s in att["steps"]]
    assert "TCP_OK" in codes and "HTTP_OK" in codes and "HTTP_REDIRECT_FOLLOWED" in codes
    assert "HTTP_REDIRECT_NOT_FOLLOWED" in codes, "跨來源的框架不可以跟"
    # 辨識
    d = rep["detection"]
    assert (d["family"], d["model"], d["status"]) == ("Apeos", "C5571", "supported")
    assert d["firmware"] == "72.18.5"
    # 表單結構有記（名稱與型別），值沒有
    s = json.dumps(rep, ensure_ascii=False)
    assert '"name": "passwd"' in s and '"type": "password"' in s
    for secret in (COOKIE_VALUE, "abc999", "t0k3n", "CSRFVALUE", "hunter2", "zzz"):
        assert secret not in s, f"報告裡出現了秘密：{secret}"
    assert "JSID=" in s, "Cookie 名稱要留著（除錯用），只遮值"
    assert rep["summary"]["reachable"] is True


def test_probe_never_sends_credentials_or_cookies(open_lo):
    srv = _serve()
    port = srv.server_address[1]
    open_lo(port)
    try:
        mc.probe("127.0.0.1", port, "http")
    finally:
        srv.shutdown()
    assert SEEN, "假伺服器一個請求都沒收到"
    for req in SEEN:
        keys = {k.lower() for k in req["headers"]}
        assert not keys & {"cookie", "authorization", "proxy-authorization"}, req
        assert req["headers"].get("User-Agent", "").startswith("jt-doc-tools")


def test_body_is_capped(open_lo, monkeypatch):
    srv = _serve()
    port = srv.server_address[1]
    open_lo(port)
    monkeypatch.setattr(mc, "_http_get", _wrap_path(mc._http_get, "/big"))
    try:
        rep = mc.probe("127.0.0.1", port, "http")
    finally:
        srv.shutdown()
    http_steps = [s for s in rep["attempts"][0]["steps"] if s["step"] == "http"]
    assert http_steps[0]["detail"]["bytes"] == mc.MAX_BODY and http_steps[0].get("note") == "HTTP_TOO_LARGE"


def _wrap_path(fn, path):
    def inner(sock, host_hdr, _p, budget):
        return fn(sock, host_hdr, path, budget)
    return inner


def test_closed_port_is_reported_as_refused(open_lo):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    open_lo(port)
    rep = mc.probe("127.0.0.1", port, "http")
    step = rep["attempts"][0]["steps"][0]
    assert (step["step"], step["ok"], step["code"]) == ("tcp", False, "TCP_REFUSED")
    assert rep["summary"]["reachable"] is False and rep["detection"]["status"] == "unreachable"


def test_a_silent_device_times_out_instead_of_hanging(open_lo, monkeypatch):
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)
    port = srv.getsockname()[1]
    open_lo(port)
    monkeypatch.setattr(mc, "READ_TIMEOUT", 1.0)
    try:
        rep = mc.probe("127.0.0.1", port, "http")
    finally:
        srv.close()
    codes = [s["code"] for s in rep["attempts"][0]["steps"]]
    assert "HTTP_TIMEOUT" in codes
    assert rep["summary"]["elapsed_ms"] < 10000


# --------------------------------------------------------------------- HTTPS（自簽憑證）

def _stdlib_ssl_context_class():
    try:
        import truststore._api as _t
        return getattr(_t, "_original_SSLContext", ssl.SSLContext)
    except Exception:  # noqa: BLE001
        return ssl.SSLContext


def _self_signed(tmp_path):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Apeos-C5571-Test")])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(1)
            .not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=30))
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
                           critical=False)
            .sign(key, hashes.SHA256()))
    c, k = tmp_path / "c.pem", tmp_path / "k.pem"
    c.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    k.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption()))
    # 伺服器端用原本的 stdlib 類別：服務啟動時 truststore 把 `ssl.SSLContext` 換掉了，
    # 那個版本不能拿來當伺服器。**用戶端**照正式環境的樣子跑在 truststore 底下。
    ctx = _stdlib_ssl_context_class()(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(c), str(k))
    return ctx


def test_self_signed_https_reports_why_and_still_identifies(open_lo, tmp_path):
    srv = _serve(_self_signed(tmp_path))
    port = srv.server_address[1]
    open_lo(port)
    try:
        rep = mc.probe("127.0.0.1", port, "https")
    finally:
        srv.shutdown()
    att = rep["attempts"][0]
    tls = next(s for s in att["steps"] if s["step"] == "tls")
    assert tls["code"] == "TLS_VERIFY_FAILED" and tls["ok"] is False
    assert tls["detail"]["reason"]["verify_message"], "要寫出驗證失敗的確切原因"
    cert = tls["detail"]["certificate"]
    assert "Apeos-C5571-Test" in cert["subject"] and cert["self_signed"] is True
    assert cert["san"] == ["IP:127.0.0.1"] and not cert["expired"] and cert["public_key"]
    assert att["reachable"] is True and rep["detection"]["model"] == "C5571"
    http = [s for s in att["steps"] if s["step"] == "http"]
    assert all(s["detail"]["tls_verified"] is False for s in http), "未驗證的連線要標出來"
    assert rep["summary"]["tls"] == {"verified": False}


def test_https_against_a_plain_http_port_fails_cleanly(open_lo):
    srv = _serve()
    port = srv.server_address[1]
    open_lo(port)
    try:
        rep = mc.probe("127.0.0.1", port, "https")
    finally:
        srv.shutdown()
    tls = next(s for s in rep["attempts"][0]["steps"] if s["step"] == "tls")
    assert tls["code"] == "TLS_HANDSHAKE_FAILED"


def test_every_code_has_a_fixed_message():
    for k, v in mc.MESSAGES.items():
        assert v and "{" not in v, k


# ---- 實機：HP LaserJet MFP M141w（Virata-EmWeb）—— 2026-10-14 對真的設備跑連線測試抓到
# 請求帶 `Connection: close` 時，它的 chunked 回應送到 8192 位元組就把連線切掉（不送結尾）；
# 不帶就完整送出（實量 22 KB、結尾正常）。curl 不送那個標頭，所以 curl 看得到首頁，
# 我們的測試卻回「不是網頁服務」—— 錯的是我們。

_EMWEB_TITLE = "HP LaserJet MFP M141w"


def _emweb_page() -> bytes:
    filler = "".join(f"<div class='nav{i}'>menu item {i}</div>\n" for i in range(900))
    return (f"<html><head><meta charset='utf-8'><title>{_EMWEB_TITLE}</title></head>"
            f"<body>{filler}<p id='end'>END-OF-PAGE</p></body></html>").encode()


def _chunked(body: bytes, size: int = 1000) -> bytes:
    out = b""
    for i in range(0, len(body), size):
        part = body[i:i + size]
        out += f"{len(part):08x}\r\n".encode() + part + b"\r\n"
    return out + b"0\r\n\r\n"


def _emweb_server(always_cut: bool = False):
    """原始 socket 寫的假 EmWeb：帶 Connection: close（或 always_cut）就在 8192 位元組切斷。"""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    seen: list[str] = []

    def handle(c):
        with c:
            req = b""
            while b"\r\n\r\n" not in req:
                d = c.recv(4096)
                if not d:
                    return
                req += d
            seen.append(req.decode("latin-1"))
            data = (b"HTTP/1.1 200 OK\r\nServer: Virata-EmWeb/R6_2_1\r\nTransfer-Encoding: chunked\r\n"
                    b"Content-Type: text/html;charset=utf-8\r\n\r\n") + _chunked(_emweb_page())
            if always_cut or b"connection: close" in req.lower():
                data = data[:8192]
            c.sendall(data)
            if not (always_cut or b"connection: close" in req.lower()):
                c.settimeout(2)            # keep-alive：等對方先關（真的設備就是這樣）
                try:
                    c.recv(1)
                except OSError:
                    pass

    def loop():
        while True:
            try:
                c, _ = srv.accept()
            except OSError:
                return
            threading.Thread(target=handle, args=(c,), daemon=True).start()

    threading.Thread(target=loop, daemon=True).start()
    return srv, seen


def test_a_device_that_cuts_close_requests_short_still_shows_its_whole_page(open_lo):
    srv, seen = _emweb_server()
    port = srv.getsockname()[1]
    open_lo(port)
    try:
        rep = mc.probe("127.0.0.1", port, "http")
    finally:
        srv.close()
    assert seen and all("connection: close" not in r.lower() for r in seen), \
        "不可以送 Connection: close（這類設備會把回應切在 8192 位元組）"
    http = next(s for s in rep["attempts"][0]["steps"] if s["step"] == "http")
    assert http["code"] == "HTTP_OK", http
    assert http["detail"]["bytes"] == len(_emweb_page()), "首頁要完整讀到"
    assert http["detail"]["title"] == _EMWEB_TITLE
    assert rep["summary"]["reachable"] is True
    assert rep["detection"]["vendor"] == "HP" and rep["detection"]["status"] == "unsupported_vendor"


def test_a_cut_off_response_is_reported_as_incomplete_not_as_not_http(open_lo):
    """真的被切斷時：已經收到的照樣拿來辨識，診斷寫「回應不完整」，不是「這個連接埠可能不是網頁服務」。"""
    srv, _ = _emweb_server(always_cut=True)
    port = srv.getsockname()[1]
    open_lo(port)
    try:
        rep = mc.probe("127.0.0.1", port, "http")
    finally:
        srv.close()
    http = next(s for s in rep["attempts"][0]["steps"] if s["step"] == "http")
    assert http["code"] == "HTTP_OK" and http.get("note") == "HTTP_INCOMPLETE", http
    assert 0 < http["detail"]["bytes"] < len(_emweb_page())
    assert http["detail"]["title"] == _EMWEB_TITLE, "標題在前 8 KB 裡，要讀得出來"
    assert rep["summary"]["reachable"] is True
    assert mc.MESSAGES.get("HTTP_INCOMPLETE"), "新的代碼要有固定訊息"


# ---- 實機：Epson L3150 —— 首頁用 JavaScript 寫出 meta refresh（2026-10-14 使用者回報型號沒偵測出來）

_EPSON_STUB = b"""<html><head><script type="text/javascript">
var path = location.href;
if (-1 < path.indexOf("PRESENTATION",0)) {
document.write("<meta http-equiv = 'refresh' content='0; URL= ./HTML/TOP/INDEX.HTML'>");
} else {
document.write("<meta http-equiv = 'refresh' content='0; URL= ./PRESENTATION/HTML/TOP/INDEX.HTML'>");
}
if (false) { document.write("<meta http-equiv='refresh' content='0; URL=/logout.cgi'>"); }
</script></head><body><noscript>Enable JavaScript.</noscript></body></html>"""


class _EpsonHandler(BaseHTTPRequestHandler):
    seen: list = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        _EpsonHandler.seen.append(self.path)
        if self.path == "/":
            body = _EPSON_STUB
        elif self.path == "/PRESENTATION/HTML/TOP/INDEX.HTML":
            body = b"<html><head><title>L3150 Series</title></head><body>Basic Settings</body></html>"
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Server", "EPSON_Linux UPnP/1.0 Epson UPnP SDK/1.0")
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def test_a_redirect_written_by_javascript_is_followed_to_find_the_model(open_lo):
    _EpsonHandler.seen = []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _EpsonHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    open_lo(port)
    try:
        rep = mc.probe("127.0.0.1", port, "http")
    finally:
        srv.shutdown()
    assert "/PRESENTATION/HTML/TOP/INDEX.HTML" in _EpsonHandler.seen, _EpsonHandler.seen
    assert "/logout.cgi" not in _EpsonHandler.seen, "看起來會改變設備狀態的網址不可以跟"
    d = rep["detection"]
    assert (d["vendor"], d["model"]) == ("Epson", "L3150 Series"), d


# ---- 客戶實機（2026-10-16）：FUJIFILM Apeos C5571 —— 首頁 144 位元組，用 JavaScript 直接改網址轉到真正的首頁

_FUJI_STUB = b'<html><head><script type="text/javascript">location.href="/home/index.html";</script></head></html>'


class _FujiHandler(BaseHTTPRequestHandler):
    seen: list = []
    home = b"<html><head><title>Apeos C5571 - Internet Services</title></head><body>FUJIFILM</body></html>"

    def log_message(self, *a):
        pass

    def do_GET(self):
        _FujiHandler.seen.append(self.path)
        body = {"/": _FUJI_STUB, "/home/index.html": _FujiHandler.home}.get(self.path)
        if body is None:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _fuji_probe(open_lo, monkeypatch, escl=None):
    _FujiHandler.seen = []
    monkeypatch.setattr(mc, "_probe_escl", lambda *a, **k: escl)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _FujiHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    open_lo(port)
    try:
        return mc.probe("127.0.0.1", port, "http")
    finally:
        srv.shutdown()


def test_a_location_href_stub_is_followed_and_kept_in_the_report(open_lo, monkeypatch):
    rep = _fuji_probe(open_lo, monkeypatch)
    assert "/home/index.html" in _FujiHandler.seen, _FujiHandler.seen
    d = rep["detection"]
    assert (d["vendor"], d["family"], d["model"], d["status"]) == ("FUJIFILM", "Apeos", "C5571", "supported"), d
    first = next(s for s in rep["attempts"][0]["steps"] if s["step"] == "http")
    assert "location.href" in first["detail"].get("stub_html", ""), "空白小頁面要在報告裡留原始內容"
    assert first["detail"]["script_redirects"] == ["/home/index.html"]


def test_location_assignments_are_only_read_on_near_empty_pages():
    big = b"<html><head><title>Status</title></head><body>" + b"Ready. " * 100 + \
          b"<script>function go(){location.href='/admin/setup.html';}</script></body></html>"
    assert mc.parse_page(big, "text/html")["script_redirects"] == [], "一般頁面上的 JavaScript 網址不跟"
    for js in (b"<script>location.replace('/logout.cgi')</script>", b"<script>location.href='javascript:void(0)'</script>",
               b"<script>var p = location.href; if (p == '/x') {}</script>"):
        assert mc.parse_page(js, "text/html")["script_redirects"] == [], js


def test_escl_manufacturer_identifies_the_vendor_when_the_home_page_says_nothing(open_lo, monkeypatch):
    """首頁轉不過去（或轉過去也沒有廠牌字樣）時，eSCL 的 Manufacturer＋MakeAndModel 也夠辨識。"""
    monkeypatch.setattr(_FujiHandler, "home", b"<html><body>Loading</body></html>")
    rep = _fuji_probe(open_lo, monkeypatch, escl={"scheme": "http", "port": 0, "model": "Apeos C5571",
                                                  "caps": {"manufacturer": "FUJIFILM", "sources": {}}})
    d = rep["detection"]
    assert (d["vendor"], d["model"], d["status"]) == ("FUJIFILM", "C5571", "supported"), d
