"""檔案分享取件（實驗功能）：Canon Advanced Box（WebDAV / SMB）、Konica 公用信箱（SMB）、SHARP NAS（SMB）。

使用者 2026-10-15：五品牌研究「1 2 都做 標實驗」。依據是原廠手冊公開的路徑（不是猜的）；沒有實機，
這裡驗的是**我們這一側的規則**：

* WebDAV（假的 Canon 設備）：只送 PROPFIND / GET；`/share`、`/users` 以外的請求路徑在送出前就擋；
  認證只送一次、沒填帳號先問；設備回的 href 只收所選位置底下、正好一層的；登入頁冒充檔案要擋。
* SMB（假的 pysmb 連線，不用真的伺服器）：要先同意「不加密」才連；伺服器選 SMB1 就在認證之前中止；
  認證失敗只連一次；**只呼叫列分享、列目錄、讀檔**（假連線的寫入 / 刪除方法一被呼叫就記下來，測試要求一次都沒有）；
  Konica 用信箱編號當帳號（照使用者輸入）、分享名稱從設備列出的分享裡找九位編號開頭的。
* 路徑一律是我們驗過的名稱（`..`、斜線、反斜線、控制字元都不收）。
"""
from __future__ import annotations

import base64
import io
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote

import pytest

from app.core import mfp_client as mc
from app.core import mfp_share as S
from tests.test_mfp_box import tiff_bytes, _resp, _ms


# --------------------------------------------------------------------- 假的 Canon Advanced Box（WebDAV）

class FakeCanon:
    def __init__(self):
        self.user, self.password = "", ""          # 空的＝不用認證
        from tests.test_mfp_escl import pdf_bytes
        self.tree = {"share": {"掃描": {"合約.tif": tiff_bytes(2)}, "報價.pdf": pdf_bytes(1)},
                     "users": {"clerk": {"a.tif": tiff_bytes(1)}}}
        self.extra: list[str] = []
        self.login_page_for: set[str] = set()
        self.home: bytes | None = None      # 首頁（連線測試辨識廠牌用；不用認證）
        self.requests: list[dict] = []

    def _node(self, segs):
        n = self.tree
        for s in segs:
            if not isinstance(n, dict) or s not in n:
                return None
            n = n[s]
        return n

    def serve(self):
        fake = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _send(self, code, body=b"", ctype="text/xml; charset=utf-8", headers=None):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                if body:
                    self.wfile.write(body)

            def _gate(self):
                n = int(self.headers.get("Content-Length") or 0)
                if n:
                    self.rfile.read(n)
                fake.requests.append({"method": self.command, "path": self.path,
                                      "auth": self.headers.get("Authorization")})
                if fake.user:
                    want = "Basic " + base64.b64encode(f"{fake.user}:{fake.password}".encode()).decode()
                    if self.headers.get("Authorization") != want:
                        self._send(401, b"<html>login</html>", "text/html", {"WWW-Authenticate": 'Basic realm="Advanced Box"'})
                        return None
                return [unquote(s) for s in self.path.split("?")[0].strip("/").split("/") if s]

            def do_PROPFIND(self):
                segs = self._gate()
                if segs is None:
                    return
                node = fake._node(segs)
                if not isinstance(node, dict):
                    return self._send(404)
                base = "/" + "/".join(quote(s) for s in segs) + "/"
                parts = [_resp(base, {}, True)]
                for name, child in node.items():
                    if isinstance(child, dict):
                        parts.append(_resp(base + quote(name) + "/", {"D:displayname": name}, True))
                    else:
                        parts.append(_resp(base + quote(name), {"D:getcontentlength": str(len(child)),
                                                                "D:getlastmodified": "Wed, 14 Oct 2026 08:00:00 GMT"}))
                return self._send(207, _ms(parts + fake.extra))

            def do_GET(self):
                if self.path == "/" and fake.home is not None:
                    return self._send(200, fake.home, "text/html; charset=utf-8")
                segs = self._gate()
                if segs is None:
                    return
                node = fake._node(segs)
                if not isinstance(node, bytes):
                    return self._send(404)
                if segs[-1] in fake.login_page_for:
                    return self._send(200, b"<html>please log in</html>", "text/html")
                return self._send(200, node, "application/octet-stream")

            def _other(self):
                fake.requests.append({"method": self.command, "path": self.path, "auth": None})
                self._send(405)

            do_DELETE = do_PUT = do_POST = do_MOVE = do_PROPPATCH = do_MKCOL = _other

        srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return srv


@pytest.fixture
def canon(monkeypatch):
    real = mc.check_address
    monkeypatch.setattr(mc, "check_address", lambda ip: None if ip == "127.0.0.1" else real(ip))
    f = FakeCanon()
    srv = f.serve()
    port = srv.server_address[1]
    monkeypatch.setattr(mc, "ALLOWED_PORTS", tuple(mc.ALLOWED_PORTS) + (port,))
    f.port = port
    yield f
    srv.shutdown()


def _dav(f, user="", password="", trace=None):
    return S.open_session("canon_webdav", host="127.0.0.1", ip="127.0.0.1", scheme="http", port=f.port, user=user,
                          password=password, box=None, box_password="", trust_sha256=None, allow_http=True,
                          allow_plain=False, trace=trace if trace is not None else [])


def test_canon_lists_folders_first_with_unicode_names(canon):
    s = _dav(canon)
    assert [r["id"] for r in s.roots()] == ["share", "users"]
    assert [r["shared"] for r in s.roots()] == [True, False], "共享空間要標共享、個人空間不是"
    rows = s.list("share", [])
    assert [(r["name"], r["dir"]) for r in rows] == [("掃描", True), ("報價.pdf", False)]
    inner = s.list("share", ["掃描"])
    assert [r["name"] for r in inner] == ["合約.tif"] and inner[0]["size"] > 0
    assert canon.requests[-1]["path"] == "/share/" + quote("掃描", safe="") + "/"


@pytest.mark.parametrize("href", ["/users/clerk/a.tif", "/share/掃描/合約.tif", "http://192.168.1.99/share/x.pdf",
                                  "/share/%252e%252e/x.pdf", "/share/a/../x.pdf", "/share/x.pdf?y=1", "/other/x.pdf"])
def test_hrefs_outside_this_folder_are_ignored(canon, href):
    canon.extra.append(_resp(href, {}))
    names = [r["name"] for r in _dav(canon).list("share", [])]
    assert names == ["掃描", "報價.pdf"], href


def test_canon_auth_is_sent_once_and_asked_for_first(canon):
    canon.user, canon.password = "clerk", "pw"
    with pytest.raises(S.ShareError) as e:
        _dav(canon).list("share", [])
    assert e.value.code == "AUTH_REQUIRED_USER" and not [r for r in canon.requests if r["auth"]]
    canon.requests.clear()
    with pytest.raises(S.ShareError) as e:
        _dav(canon, "clerk", "wrong").list("share", [])
    assert e.value.code == "AUTH_FAILED" and len([r for r in canon.requests if r["auth"]]) == 1
    assert [r["name"] for r in _dav(canon, "clerk", "pw").list("users", ["clerk"])] == ["a.tif"]


def test_canon_download_and_login_page(canon, tmp_path):
    s = _dav(canon)
    got = s.download("share", ["掃描", "合約.tif"], tmp_path / "a.bin", 10 ** 9)
    assert (tmp_path / "a.bin").read_bytes() == canon.tree["share"]["掃描"]["合約.tif"] and got["sha256"]
    canon.login_page_for.add("報價.pdf")
    with pytest.raises(S.ShareError) as e:
        s.download("share", ["報價.pdf"], tmp_path / "b.bin", 10 ** 9)
    assert e.value.code == "SHARE_LOGIN_PAGE" and not (tmp_path / "b.bin").exists()
    with pytest.raises(S.ShareError) as e:
        s.download("share", ["不見了.pdf"], tmp_path / "c.bin", 10 ** 9)
    assert e.value.code == "SHARE_FILE_GONE"


def test_canon_sends_only_reads_and_fixed_paths(canon, tmp_path):
    s = _dav(canon)
    s.list("share", [])
    s.download("share", ["報價.pdf"], tmp_path / "a", 10 ** 9)
    assert {r["method"] for r in canon.requests} <= {"PROPFIND", "GET"}
    for m in ("DELETE", "PUT", "MOVE", "MKCOL"):
        with pytest.raises(ValueError):
            s.dav.request(m, "/share/x")
    for bad in ("/share/../etc", "/share/./x", "/admin", "/share/a%2Fb", "/share/a%5Cb", "/users/a?b"):
        with pytest.raises(ValueError):
            s.dav.request("GET", bad)


@pytest.mark.parametrize("path", [[".."], ["a/b"], ["a\\b"], ["a\x00"], [""], ["."], "a/b", [" a"], ["a"] * 13, [1]])
def test_paths_are_only_checked_names(path):
    with pytest.raises(S.ShareError) as e:
        S.check_path(path)
    assert e.value.code == "SHARE_PATH_INVALID"


def test_root_must_be_one_of_this_route(canon):
    with pytest.raises(S.ShareError) as e:
        _dav(canon).list("etc", [])
    assert e.value.code == "SHARE_ROOT_INVALID"


# --------------------------------------------------------------------- 假的 SMB（pysmb 的介面）

class _Dev:
    def __init__(self, name, type=0, special=False):
        self.name, self.type, self.isSpecial = name, type, special


class _File:
    def __init__(self, name, data=None):
        self.filename, self.data = name, data
        self.isDirectory = data is None
        self.file_size = len(data or b"")
        self.last_write_time = 1760428800.0


class FakeSmbServer:
    def __init__(self):
        self.shares = {"share": {"a.tif": tiff_bytes(1), "sub": {}}, "users": {}, "IPC$": None}
        self.users = {("", ""): False}
        self.accounts = {}          # (user, domain) → password
        self.smb1 = False
        self.smb2 = True
        self.calls: list[tuple] = []
        self.forbidden: set[str] = set()


def fake_class(srv: FakeSmbServer):
    from smb.smb_structs import OperationFailure

    class Conn:
        def __init__(self, username, password, my_name, remote_name, domain="", use_ntlm_v2=True,
                     sign_options=2, is_direct_tcp=False):
            srv.calls.append(("init", username, domain, use_ntlm_v2, sign_options, is_direct_tcp))
            self.u, self.p, self.d = username, password, domain
            self.isUsingSMB2 = srv.smb2

        def connect(self, ip, port=139, sock_family=None, timeout=60):
            srv.calls.append(("connect", ip, port))
            if srv.smb1:
                self._handleNegotiateResponse_SMB1(None)
            return srv.accounts.get((self.u, self.d)) == self.p

        def _handleNegotiateResponse_SMB1(self, message):
            pass

        def listShares(self, timeout=30):
            srv.calls.append(("listShares",))
            return [_Dev(n, 3 if n.endswith("$") else 0, n.endswith("$")) for n in srv.shares]

        def _node(self, share, path):
            if share in srv.forbidden:
                raise OperationFailure("STATUS_ACCESS_DENIED 0xc0000022", [])
            n = srv.shares.get(share)
            for s in [x for x in path.split("/") if x]:
                if not isinstance(n, dict) or s not in n:
                    raise OperationFailure("STATUS_OBJECT_NAME_NOT_FOUND 0xc0000034", [])
                n = n[s]
            return n

        def listPath(self, share, path, timeout=30, **kw):
            srv.calls.append(("listPath", share, path))
            n = self._node(share, path)
            return [_File("."), _File("..")] + [_File(k, None if isinstance(v, dict) else v) for k, v in n.items()]

        def retrieveFile(self, share, path, file_obj, timeout=30, **kw):
            srv.calls.append(("retrieveFile", share, path))
            data = self._node(share, path)
            for i in range(0, len(data), 1000):
                file_obj.write(data[i:i + 1000])
            return 0, len(data)

        def close(self):
            srv.calls.append(("close",))

        def _write(self, *a, **k):
            srv.calls.append(("WRITE",))
            raise AssertionError("不可以寫入 / 刪除 / 改名")

        storeFile = deleteFiles = rename = createDirectory = deleteDirectory = resetFileAttributes = _write

    if srv.smb1:
        import app.core.mfp_share as m

        class Strict(Conn):
            def _handleNegotiateResponse_SMB1(self, message):
                raise m._SMB1Refused()
        return Strict
    return Conn


@pytest.fixture
def smb(monkeypatch):
    pytest.importorskip("smb")
    real = mc.check_address
    monkeypatch.setattr(mc, "check_address", lambda ip: None if ip == "127.0.0.1" else real(ip))
    srv = FakeSmbServer()
    monkeypatch.setattr(S, "_strict_connection_class", lambda: fake_class(srv))
    return srv


def _smb(route="canon_smb", user="clerk", password="pw", box=None, box_password="", allow_plain=True, trace=None):
    return S.open_session(route, host="127.0.0.1", ip="127.0.0.1", scheme=None, port=None, user=user,
                          password=password, box=box, box_password=box_password, trust_sha256=None,
                          allow_http=False, allow_plain=allow_plain, trace=trace if trace is not None else [])


def test_smb_needs_consent_before_connecting(smb):
    with pytest.raises(S.ShareError) as e:
        _smb(allow_plain=False)
    assert e.value.code == "SMB_PLAIN_NEEDS_CONSENT" and not smb.calls


def test_smb_auth_failure_connects_once(smb):
    smb.accounts[("clerk", "")] = "right"
    with pytest.raises(S.ShareError) as e:
        _smb(password="wrong")
    assert e.value.code == "SMB_AUTH_FAILED"
    assert [c[0] for c in smb.calls].count("connect") == 1
    init = next(c for c in smb.calls if c[0] == "init")
    assert init[3] is True and init[4] == 1 and init[5] is True, "NTLMv2、能簽章就簽章、直接走 445"


def test_smb1_is_refused_before_authentication(smb):
    smb.smb1 = True
    smb.accounts[("clerk", "")] = "pw"
    with pytest.raises(S.ShareError) as e:
        _smb()
    assert e.value.code == "SMB1_REFUSED"


def test_a_session_that_is_not_smb2_is_refused(smb):
    smb.smb2 = False
    smb.accounts[("clerk", "")] = "pw"
    with pytest.raises(S.ShareError) as e:
        _smb()
    assert e.value.code == "SMB1_REFUSED"


def test_the_strict_class_aborts_on_an_smb1_negotiate_response():
    pytest.importorskip("smb")
    klass = S._strict_connection_class()
    conn = klass("u", "p", "JTDT", "SRV", is_direct_tcp=True)
    with pytest.raises(S._SMB1Refused):
        conn._handleNegotiateResponse_SMB1(None)


def test_domain_accounts(smb):
    smb.accounts[("clerk", "CORP")] = "pw"
    _smb(user="CORP\\clerk")
    assert ("init", "clerk", "CORP", True, 1, True) in smb.calls
    assert S.split_account("clerk@corp.example") == ("clerk@corp.example", "")


def test_smb_only_reads(smb, tmp_path):
    smb.accounts[("clerk", "")] = "pw"
    s = _smb()
    assert [r["id"] for r in s.roots()] == ["share", "users"], "Canon 只列 share / users，特殊分享不列"
    rows = s.list("share", [])
    assert [(r["name"], r["dir"]) for r in rows] == [("sub", True), ("a.tif", False)]
    s.download("share", ["a.tif"], tmp_path / "a", 10 ** 9)
    assert (tmp_path / "a").read_bytes() == smb.shares["share"]["a.tif"]
    s.close()
    assert ("WRITE",) not in smb.calls
    assert {c[0] for c in smb.calls} <= {"init", "connect", "listShares", "listPath", "retrieveFile", "close"}


def test_smb_errors_become_reasons(smb, tmp_path):
    smb.accounts[("clerk", "")] = "pw"
    s = _smb()
    smb.forbidden.add("users")
    with pytest.raises(S.ShareError) as e:
        s.list("users", [])
    assert e.value.code == "SHARE_FORBIDDEN"
    with pytest.raises(S.ShareError) as e:
        s.download("share", ["nope.pdf"], tmp_path / "x", 10 ** 9)
    assert e.value.code == "SHARE_FILE_GONE"
    with pytest.raises(S.ShareError) as e:
        s.download("share", ["a.tif"], tmp_path / "y", 100)
    assert e.value.code == "SHARE_FILE_TOO_LARGE"


def test_sharp_lists_whatever_shares_the_device_has(smb):
    smb.shares = {"public": {"a.pdf": b"%PDF-1.4"}, "shared": {}, "IPC$": None}
    smb.accounts[("clerk", "")] = "pw"
    roots = _smb("sharp_smb").roots()
    assert [(r["id"], r["shared"]) for r in roots] == [("public", True), ("shared", True)]


def test_konica_uses_the_box_number_and_the_listed_share(smb):
    smb.shares = {"000000012_Scan": {"a.tif": tiff_bytes(1)}, "000000001_Other": {}, "IPC$": None}
    smb.accounts[("12", "")] = "1234"
    s = _smb("konica_smb", user="", password="", box="12", box_password="1234")
    assert S.konica_box_share(s, "12") == "000000012_Scan"
    assert ("init", "12", "", True, 1, True) in smb.calls, "帳號是使用者輸入的信箱編號（不自己補零）"
    with pytest.raises(S.ShareError) as e:
        S.konica_box_share(s, "3")
    assert e.value.code == "BOX_SHARE_NOT_FOUND"
    with pytest.raises(S.ShareError) as e:
        _smb("konica_smb", box="", box_password="x")
    assert e.value.code == "BOX_NUMBER_REQUIRED"


def test_routes_per_vendor():
    assert S.routes_for("Canon") == ("canon_webdav", "canon_smb")
    assert S.routes_for("Konica Minolta") == ("konica_smb",)
    assert S.routes_for("Sharp") == ("sharp_smb",)
    assert S.routes_for("Kyocera") == ("kyocera_soap", "kyocera_soap_http")     # v1.16.95，直接取件（SOAP）
    assert S.routes_for("Ricoh") == ("ricoh_soap",)
    assert S.routes_for("FUJIFILM") == () and S.routes_for(None) == ()


@pytest.mark.parametrize("raw,want", [
    ("Wed, 14 Oct 2026 08:00:00 GMT", 1791964800.0),
    ("2026-10-14T08:00:00Z", 1791964800.0),
    ("2026-10-14T16:00:00+08:00", 1791964800.0),
    ("", None), ("昨天", None), ("1800-01-01T00:00:00Z", None),
])
def test_device_times_become_timestamps_for_the_browser(raw, want):
    """時間交給瀏覽器依使用者那邊的時區顯示（伺服器時區可能不一樣）。"""
    from app.core import mfp_box
    assert mfp_box.epoch(raw) == want


def test_listings_carry_timestamps(canon, smb):
    rows = _dav(canon).list("share", [])
    assert rows[1]["modified"] == 1791964800.0 and rows[0]["modified"] is None
    smb.accounts[("clerk", "")] = "pw"
    srows = _smb().list("share", [])
    assert srows[1]["modified"] == 1760428800.0
