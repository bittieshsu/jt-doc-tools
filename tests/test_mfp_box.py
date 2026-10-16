"""FUJIFILM 機內信箱取件（WebDAV，**推測實作**）—— 對一台假的信箱設備。

使用者 2026-10-14：暫時沒有實機，「用推測方式撰寫」；取件預設存 PDF。協定的推測依據見 `app/core/mfp_box.py`。
這些測試驗的是**我們這一側的規則**（假設備照推測的格式回應）—— 通過不代表真機相容：

* 只送 PROPFIND 與 GET（請求層就擋；整個流程跑完，假設備只收到這兩種）。
* 認證依 realm 決定送哪一組帳密，**只送一次**；沒填就先問、不送空的；認不得就停，不猜。
* Digest 照 RFC 7616 算（假設備自己驗）；`stale=true` 用新的 nonce 再送一次（這不算換密碼重試）。
* 設備回的 href 只收同一台、同一個信箱底下、格式固定的路徑；二次編碼、`..`、別台主機一律不收。
* XML 的外部實體、DTD 展開拒絕；回的不是 TIFF / JPEG / PDF（例如登入頁）就停；太大就停並清掉暫存。
* HTTP 要先同意才連；HTTPS 自簽憑證要核對過的指紋（不符就停，沒送任何請求）。
* 帳密不出現在協定紀錄、`repr`。
"""
from __future__ import annotations

import base64
import hashlib
import io
import re
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.core import mfp_box as B
from app.core import mfp_client as mc
from app.core import mfp_output

NS = "http://www.fujifilm.com/fb/2021/04/boxAccess"


def tiff_bytes(pages=3, mode="1") -> bytes:
    from PIL import Image, ImageDraw
    frames = []
    for i in range(pages):
        im = Image.new(mode, (850, 1100), 1 if mode == "1" else "white")
        ImageDraw.Draw(im).text((60, 60 + i * 30), f"DOC-A / PAGE-{i + 1:02d}", fill=0)
        frames.append(im)
    buf = io.BytesIO()
    kw = {"compression": "group4"} if mode == "1" else {}
    frames[0].save(buf, "TIFF", save_all=True, append_images=frames[1:], dpi=(200, 200), **kw)
    return buf.getvalue()


def jpeg_bytes(w=850, h=1100) -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (w, h), "white").save(buf, "JPEG", quality=40, dpi=(200, 200))
    return buf.getvalue()


def _resp(href: str, props: dict, collection: bool = False) -> str:
    px = "".join(f"<{k}>{v}</{k}>" for k, v in props.items())
    rt = "<D:resourcetype><D:collection/></D:resourcetype>" if collection else "<D:resourcetype/>"
    return (f"<D:response><D:href>{href}</D:href><D:propstat><D:prop>{rt}{px}</D:prop>"
            "<D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response>")


def _ms(parts: list[str]) -> bytes:
    return (f'<?xml version="1.0" encoding="utf-8"?><D:multistatus xmlns:D="DAV:" xmlns:F="{NS}">'
            + "".join(parts) + "</D:multistatus>").encode()


class FakeBox:
    """可設定的假 FUJIFILM 信箱（WebDAV）。"""

    def __init__(self):
        self.seg = "box-1"                 # 設備的寫法（可以改成 box-001）
        self.realm = None                  # None → 不用認證
        self.scheme = "basic"              # basic / digest / negotiate
        self.user, self.password = "box", "1234"
        self.nonce = "n1"
        self.stale_once = False
        self.docs: dict[str, dict] = {}
        self.box_props = {"D:displayname": "Box 1"}
        self.status: dict[str, int] = {}   # 路徑 → 強制回的狀態碼
        self.redirect: dict[str, str] = {}
        self.reject_406 = None             # (method, body, headers) → True 就回 406
        self.extra: list[str] = []         # 額外塞進信箱清單的 <D:response>
        self.raw_list: bytes | None = None
        self.delete_on_get = False
        self.home: bytes | None = None     # 首頁（連線測試辨識型號用；不用認證）
        self.requests: list[dict] = []

    # ---- 文件
    def add_whole(self, doc_id, data=None, name="Scan 1", ext="tif", **props):
        self.docs[str(doc_id)] = {"name": name, "whole": data if data is not None else tiff_bytes(),
                                  "ext": ext, "pages": {}, "props": props}

    def add_pages(self, doc_id, pages: dict, name="Scan 2", **props):
        self.docs[str(doc_id)] = {"name": name, "whole": None, "ext": None, "pages": pages, "props": props}

    def listing(self) -> bytes:
        if self.raw_list is not None:
            return self.raw_list
        parts = [_resp(f"/dav/{self.seg}/", self.box_props, True)]
        for i, d in self.docs.items():
            props = {"D:displayname": d["name"], **{f"F:{k}": v for k, v in d["props"].items()}}
            if d["whole"] is not None:
                props["D:getcontenttype"] = "image/tiff" if d["ext"] == "tif" else "image/jpeg"
                props["D:getcontentlength"] = str(len(d["whole"]))
                parts.append(_resp(f"/dav/{self.seg}/{i}.{d['ext']}", props))
            else:
                parts.append(_resp(f"/dav/{self.seg}/{i}/", props, True))
        return _ms(parts + self.extra)

    def folder(self, doc_id: str) -> bytes:
        d = self.docs[doc_id]
        parts = [_resp(f"/dav/{self.seg}/{doc_id}/", {}, True),
                 _resp(f"/dav/{self.seg}/{doc_id}/thumbnail/", {}, True),
                 _resp(f"/dav/{self.seg}/{doc_id}/thumbnail/1-1.jpg", {"D:getcontenttype": "image/jpeg"})]
        for nm in d["pages"]:
            parts.append(_resp(f"/dav/{self.seg}/{doc_id}/{nm}", {"D:getcontenttype": "image/tiff"}))
        return _ms(parts)

    # ---- 認證
    def _authorized(self, h) -> tuple[bool, bool]:
        """（通過嗎、要不要回 stale）。"""
        got = h.headers.get("Authorization") or ""
        if self.scheme == "basic":
            want = "Basic " + base64.b64encode(f"{self.user}:{self.password}".encode()).decode()
            return got == want, False
        if not got.startswith("Digest "):
            return False, False
        p = dict((m.group(1), m.group(2) or m.group(3)) for m in
                 re.finditer(r'(\w+)=(?:"([^"]*)"|([^,\s]*))', got[7:]))
        ha1 = hashlib.md5(f"{p.get('username')}:{self.realm}:{self.password}".encode()).hexdigest()
        ha2 = hashlib.md5(f"{h.command}:{p.get('uri')}".encode()).hexdigest()
        want = hashlib.md5(f"{ha1}:{p.get('nonce')}:{p.get('nc')}:{p.get('cnonce')}:auth:{ha2}".encode()).hexdigest()
        ok = p.get("response") == want and p.get("username") == self.user and p.get("uri") == h.path
        if ok and self.stale_once and p.get("nonce") == "n1":
            self.stale_once = False
            self.nonce = "n2"
            return False, True
        return ok and p.get("nonce") == self.nonce, False

    def challenge(self, stale=False) -> str:
        if self.scheme == "basic":
            return f'Basic realm="{self.realm}"'
        if self.scheme == "negotiate":
            return "Negotiate"
        return (f'Digest realm="{self.realm}", nonce="{self.nonce}", qop="auth", algorithm=MD5'
                + (", stale=true" if stale else ""))

    def serve(self, tls_ctx=None):
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

            def _gate(self) -> bool:
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                fake.requests.append({"method": self.command, "path": self.path, "body": body,
                                      "headers": {k.lower(): v for k, v in self.headers.items()}})
                if self.path in fake.status:
                    self._send(fake.status[self.path])
                    return False
                # 依請求內容回 406（模擬只接受某一種請求格式的設備；v1.16.99）
                if fake.reject_406 is not None and fake.reject_406(self.command, body,
                                                                   {k.lower(): v for k, v in self.headers.items()}):
                    self._send(406)
                    return False
                if self.path in fake.redirect:
                    self._send(301, headers={"Location": fake.redirect[self.path]})
                    return False
                if fake.realm is not None:
                    ok, stale = fake._authorized(self)
                    if not ok:
                        self._send(401, b"<html>login</html>", "text/html",
                                   {"WWW-Authenticate": fake.challenge(stale)})
                        return False
                return True

            def do_PROPFIND(self):
                if not self._gate():
                    return
                p = self.path.rstrip("/")
                if re.fullmatch(rf"/dav/{fake.seg}", p) or (fake.seg != "box-1" and p == "/dav/box-1"):
                    return self._send(207, fake.listing())
                m = re.fullmatch(rf"/dav/{fake.seg}/(\d+)", p)
                if m and m.group(1) in fake.docs:
                    return self._send(207, fake.folder(m.group(1)))
                return self._send(404)

            def do_GET(self):
                if self.path == "/" and fake.home is not None:
                    return self._send(200, fake.home, "text/html; charset=utf-8")
                if not self._gate():
                    return
                m = re.fullmatch(rf"/dav/{fake.seg}/(\d+)(?:\.(\w+)|/(\d+-\d+\.\w+))", self.path)
                if not m or m.group(1) not in fake.docs:
                    return self._send(404)
                d = fake.docs[m.group(1)]
                data = d["whole"] if m.group(2) else d["pages"].get(m.group(3))
                if data is None:
                    return self._send(404)
                if fake.delete_on_get:
                    fake.docs.pop(m.group(1), None)
                ctype = "text/html" if data.startswith(b"<") else "image/tiff"
                return self._send(200, data, ctype)

            def _other(self):
                fake.requests.append({"method": self.command, "path": self.path, "headers": {}, "body": b""})
                self._send(405)

            do_DELETE = do_PUT = do_POST = do_MOVE = do_PROPPATCH = _other

        srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        if tls_ctx is not None:
            srv.socket = tls_ctx.wrap_socket(srv.socket, server_side=True)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return srv


@pytest.fixture
def box(monkeypatch):
    real = mc.check_address
    monkeypatch.setattr(mc, "check_address", lambda ip: None if ip == "127.0.0.1" else real(ip))
    f = FakeBox()
    srv = f.serve()
    port = srv.server_address[1]
    monkeypatch.setattr(mc, "ALLOWED_PORTS", tuple(mc.ALLOWED_PORTS) + (port,))
    f.port = port
    yield f
    srv.shutdown()


def _open(f, creds=None, **kw):
    kw.setdefault("allow_http", True)
    return B.open_box("127.0.0.1", "127.0.0.1", "http", f.port, box=1, creds=creds or B.Creds(), **kw)


def _methods(f):
    return {r["method"] for r in f.requests}


def _auth_sent(f):
    return [r for r in f.requests if r["headers"].get("authorization")]


# --------------------------------------------------------------------- 解析

def test_challenges_with_several_schemes_and_quoted_values():
    cs = B.parse_challenges(['Digest realm="box-1", nonce="a,b", qop="auth,auth-int", algorithm=MD5, Basic realm="x"',
                             'Negotiate'])
    assert [c["scheme"] for c in cs] == ["digest", "basic", "negotiate"]
    assert cs[0]["params"] == {"realm": "box-1", "nonce": "a,b", "qop": "auth,auth-int", "algorithm": "MD5"}
    assert cs[1]["params"]["realm"] == "x"


@pytest.mark.parametrize("realm,kind", [("box-1", "box"), ("BOX-012", "box"), ("box", "box"),
                                        ("user-Local", "user"), ("admin", "user"), ("Apeos C5571", "unknown"),
                                        ("", "unknown")])
def test_realm_kinds(realm, kind):
    assert B.realm_kind(realm) == kind


@pytest.mark.parametrize("v", ["0", "1000", "-1", "1a", "", None, "1e3"])
def test_box_numbers_outside_1_to_999_are_refused(v):
    with pytest.raises(B.BoxError) as e:
        B.parse_box(v)
    assert e.value.code == "BOX_INVALID"


# --------------------------------------------------------------------- 開信箱、列文件

def test_an_open_box_lists_its_documents(box):
    box.add_whole(5, name="報價單", Pages="3", Compression="MMR", deleteAfterGet="false")
    box.add_pages(7, {"1-1.tif": tiff_bytes(1)}, name="合約", Pages="1")
    s = _open(box)
    pub = s.public()
    assert pub["box"] == 1 and pub["box_name"] == "Box 1" and pub["delete_after_get"] is None
    docs = {d["id"]: d for d in pub["documents"]}
    assert docs["5"]["name"] == "報價單" and docs["5"]["pages"] == 3 and docs["5"]["how"] == "whole"
    assert docs["5"]["format"] == "TIF" and docs["5"]["compression"] == "MMR"
    assert docs["5"]["delete_after_get"] is False
    assert docs["7"]["how"] == "pages"
    # v1.16.99（客戶實機 406 後依原廠工具分析改）：先探測（只問 resourcetype、X-Singlepage: On），再用指定屬性列文件
    probe, listing = box.requests[0], box.requests[1]
    assert probe["method"] == "PROPFIND" and probe["path"] == "/dav/box-1" and probe["headers"]["depth"] == "1"
    assert b"<d:resourcetype/>" in probe["body"] and b"allprop" not in probe["body"]
    assert probe["headers"].get("x-singlepage") == "On" and probe["headers"]["content-type"] == "text/xml"
    assert b"fujifilm.com/fb/2021/04/ssm/storageService" in probe["body"], "先用新的命名空間"
    assert b"<sss:Pages/>" in listing["body"] and b"allprop" not in listing["body"]
    assert "x-singlepage" not in listing["headers"], "列文件不送 X-Singlepage（不是送 Off）"
    assert "authorization" not in probe["headers"] and "authorization" not in listing["headers"], "不用認證就不帶帳密"
    assert s.via() == {"protocol": "WebDAV", "scheme": "http", "port": box.port}


def test_box_password_goes_as_user_box_exactly_once(box):
    box.realm = "box-1"
    box.add_whole(1)
    s = _open(box, B.Creds(box_password="1234"))
    assert list(s.docs) == ["1"]
    # 帳密只選一次：探測（帶帳密重送）與列文件用的是同一組，不是換組合重試
    sent = _auth_sent(box)
    assert len(sent) == 2 and len({r["headers"]["authorization"] for r in sent}) == 1
    assert sent[0]["headers"]["authorization"].startswith("Basic ")


def test_a_wrong_password_is_not_retried(box):
    box.realm = "box-1"
    with pytest.raises(B.BoxError) as e:
        _open(box, B.Creds(box_password="wrong", user="someone", password="x"))
    assert e.value.code == "AUTH_FAILED"
    assert len(_auth_sent(box)) == 1, "失敗就停：不換組合、不重試（避免帳號被鎖）"


def test_a_missing_password_is_asked_for_without_sending_anything(box):
    box.realm = "box-1"
    with pytest.raises(B.BoxError) as e:
        _open(box)
    assert e.value.code == "AUTH_REQUIRED_BOX" and not _auth_sent(box)
    box.realm = "user-Local"
    box.requests.clear()
    with pytest.raises(B.BoxError) as e:
        _open(box, B.Creds(box_password="1234"))
    assert e.value.code == "AUTH_REQUIRED_USER" and not _auth_sent(box), "信箱密碼不可以拿去當設備帳密"


def test_a_device_account_with_digest(box):
    box.realm, box.scheme = "user-Local", "digest"
    box.user, box.password = "clerk01", "pw:with\"quote"
    box.add_whole(3)
    s = _open(box, B.Creds(user="clerk01", password="pw:with\"quote"))
    assert list(s.docs) == ["3"]
    files = B.download(s, "3", box_tmp(s), budget_bytes=10 ** 9, is_cancelled=lambda: False,
                       on_progress=lambda d: None)
    assert files[0]["kind"] == "tiff"
    assert all(r["headers"]["authorization"].startswith("Digest ") for r in _auth_sent(box))


def box_tmp(_s, _n=[0]):
    import tempfile
    from pathlib import Path
    _n[0] += 1
    return Path(tempfile.mkdtemp(prefix="mfpbox-")) / f"d{_n[0]}"


def test_a_stale_digest_nonce_is_resent_once_with_the_same_password(box):
    box.realm, box.scheme, box.stale_once = "box-1", "digest", True
    box.user = "box"
    box.add_whole(1)
    s = _open(box, B.Creds(box_password="1234"))
    # 探測（舊 nonce → stale）→ 探測（新 nonce）→ 列文件
    assert list(s.docs) == ["1"] and len(_auth_sent(box)) == 3
    assert {re.search(r'username="([^"]+)"', r["headers"]["authorization"]).group(1) for r in _auth_sent(box)} == {"box"}


@pytest.mark.parametrize("realm,scheme", [("Apeos C5571", "basic"), ("box-1", "negotiate")])
def test_unknown_realms_and_schemes_are_not_guessed(box, realm, scheme):
    box.realm, box.scheme = realm, scheme
    with pytest.raises(B.BoxError) as e:
        _open(box, B.Creds(box_password="1234", user="u", password="p"))
    assert e.value.code == "AUTH_MODE_UNSUPPORTED" and not _auth_sent(box)
    assert e.value.detail["challenges"], "設備回的認證資訊要留在細節裡（實機對照用）"


@pytest.mark.parametrize("status,code", [(404, "BOX_NOT_FOUND"), (403, "BOX_FORBIDDEN"), (405, "WEBDAV_UNAVAILABLE"),
                                         (501, "WEBDAV_UNAVAILABLE"), (406, "DAV_REQUEST_NOT_ACCEPTED"),
                                         (500, "DAV_UNEXPECTED_STATUS")])
def test_status_codes_become_clear_reasons(box, status, code):
    box.status["/dav/box-1"] = status
    with pytest.raises(B.BoxError) as e:
        _open(box)
    assert e.value.code == code


def test_a_redirect_to_the_same_box_is_followed_once(box):
    box.redirect["/dav/box-1"] = f"http://127.0.0.1:{box.port}/dav/box-1/"
    box.add_whole(1)
    s = _open(box)
    assert list(s.docs) == ["1"] and box.requests[1]["path"] == "/dav/box-1/"


@pytest.mark.parametrize("to", ["/dav/box-2/", "/login.html", "http://192.168.1.99/dav/box-1/", "https://127.0.0.1/dav/box-1/"])
def test_redirects_anywhere_else_are_refused(box, to):
    box.redirect["/dav/box-1"] = to
    with pytest.raises(B.BoxError) as e:
        _open(box)
    assert e.value.code == "DAV_REDIRECT_REFUSED" and len(box.requests) == 1


def test_a_zero_padded_box_path_from_the_device_is_followed(box):
    box.seg = "box-001"
    box.add_whole(9)
    s = _open(box)
    assert list(s.docs) == ["9"]
    files = B.download(s, "9", box_tmp(s), budget_bytes=10 ** 9, is_cancelled=lambda: False,
                       on_progress=lambda d: None)
    assert files and box.requests[-1]["path"] == "/dav/box-001/9.tif"


@pytest.mark.parametrize("href", [
    "http://192.168.1.99/dav/box-1/5.tif",       # 別台主機
    "https://127.0.0.1/dav/box-1/5.tif",         # 別的協定
    "/dav/box-2/5.tif",                          # 別的信箱
    "/dav/box-1/../box-2/5.tif",
    "/dav/box-1/%2e%2e/5.tif",
    "/dav/box-1/%252e%252e/5.tif",               # 二次編碼
    "/dav/box-1/5.tif?x=1",
    "/dav/box-1\\5.tif",
    "http://user:pw@127.0.0.1/dav/box-1/5.tif",
    "/dav/box-1/5.exe",
    "/dav/box-1/abc.tif",
    "/dav/box-1/5/6/7.tif",
    "/dav/box-1/x/../5.tif",                     # `..` 合併網址時會被吃掉、看起來像信箱裡的文件
    "/dav/box-1/./5.tif",
])
def test_hrefs_outside_the_box_or_odd_are_ignored(box, href):
    box.extra.append(_resp(href, {"D:displayname": "x"}))
    s = _open(box)
    assert s.docs == {} and s.ignored == 1, href


@pytest.mark.parametrize("bad", [
    b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;">]>'
    b'<D:multistatus xmlns:D="DAV:">&b;</D:multistatus>',
    b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]><D:multistatus xmlns:D="DAV:">&e;</D:multistatus>',
    b"<html><body>login</body></html>",
    b'<?xml version="1.0"?><other xmlns="DAV:"/>',
])
def test_hostile_or_odd_xml_is_refused(box, bad):
    box.raw_list = bad
    with pytest.raises(B.BoxError) as e:
        _open(box)
    assert e.value.code == "DAV_BAD_RESPONSE"


def test_properties_from_unknown_namespaces_are_ignored():
    data = (b'<?xml version="1.0"?><D:multistatus xmlns:D="DAV:" xmlns:X="urn:other">'
            b'<D:response><D:href>/dav/box-1/1.tif</D:href><D:propstat><D:prop>'
            b'<X:deleteAfterGet>true</X:deleteAfterGet><D:displayname>a</D:displayname></D:prop>'
            b'<D:status>HTTP/1.1 200 OK</D:status></D:propstat>'
            b'<D:propstat><D:prop><D:getcontentlength/></D:prop><D:status>HTTP/1.1 404 Not Found</D:status>'
            b'</D:propstat></D:response></D:multistatus>')
    entries, qnames = B.parse_multistatus(data)
    assert entries[0]["props"] == {"displayname": "a"}, "別的命名空間、404 的屬性都不收"
    assert "{urn:other}deleteAfterGet" in qnames, "看到的屬性名稱要留給協定紀錄"


# --------------------------------------------------------------------- 取件

def test_a_whole_tiff_and_a_page_folder(box):
    box.add_whole(1, data=tiff_bytes(3))
    box.add_pages(2, {"2-1.tif": tiff_bytes(1), "1-1.tif": tiff_bytes(1), "1-1.jpg": jpeg_bytes(),
                      "10-1.jpg": jpeg_bytes()})
    s = _open(box)
    w = B.download(s, "1", box_tmp(s), budget_bytes=10 ** 9, is_cancelled=lambda: False, on_progress=lambda d: None)
    assert len(w) == 1 and w[0]["kind"] == "tiff" and w[0]["pages"] == 3 and w[0]["dpi"] == 200
    assert w[0]["sha256"] == hashlib.sha256(box.docs["1"]["whole"]).hexdigest()
    p = B.download(s, "2", box_tmp(s), budget_bytes=10 ** 9, is_cancelled=lambda: False, on_progress=lambda d: None)
    assert [f["source"] for f in p] == ["1-1.tif", "2-1.tif", "10-1.jpg"], \
        "依兩個數字排序；同一個位置有 TIFF 就不取 JPEG；縮圖不取"
    assert not any("thumbnail" in r["path"] for r in box.requests if r["method"] == "GET")


def test_only_propfind_and_get_ever_reach_the_device(box):
    box.realm = "box-1"
    box.add_whole(1)
    box.add_pages(2, {"1-1.tif": tiff_bytes(1)})
    s = _open(box, B.Creds(box_password="1234"))
    for d in ("1", "2"):
        B.download(s, d, box_tmp(s), budget_bytes=10 ** 9, is_cancelled=lambda: False, on_progress=lambda d: None)
    s.list()
    assert _methods(box) <= {"PROPFIND", "GET"}
    for m in ("DELETE", "PUT", "MOVE", "PROPPATCH", "POST"):
        with pytest.raises(ValueError):
            s.dav.request(m, "/dav/box-1/1.tif")
    assert _methods(box) <= {"PROPFIND", "GET"}


@pytest.mark.parametrize("path", ["/dav/box-1/../x", "/admin", "/dav/box-1/1.tif?a", "/dav/box-1/1/thumbnail/1-1.jpg",
                                  "http://x/dav/box-1"])
def test_request_targets_are_only_the_fixed_shapes(box, path):
    s = _open(box)
    with pytest.raises(ValueError):
        s.dav.request("GET", path)


def test_a_login_page_instead_of_a_file_is_refused(box, tmp_path):
    box.add_whole(1, data=b"<html><form><input type=password></form></html>")
    s = _open(box)
    with pytest.raises(B.BoxError) as e:
        B.download(s, "1", tmp_path / "d", budget_bytes=10 ** 9, is_cancelled=lambda: False, on_progress=lambda d: None)
    assert e.value.code == "DOC_BAD_FILE"
    assert not list((tmp_path / "d").iterdir())


def test_a_document_over_the_limit_stops_and_cleans_up(box, tmp_path, monkeypatch):
    monkeypatch.setattr(B, "MAX_DOC_BYTES", 1000)
    box.add_whole(1, data=tiff_bytes(3, mode="L"))
    s = _open(box)
    with pytest.raises(B.BoxError) as e:
        B.download(s, "1", tmp_path / "d", budget_bytes=10 ** 9, is_cancelled=lambda: False, on_progress=lambda d: None)
    assert e.value.code == "DOC_TOO_LARGE" and not list((tmp_path / "d").iterdir())


def test_a_document_that_disappeared_is_reported(box, tmp_path):
    box.add_whole(1)
    s = _open(box)
    box.docs.clear()
    with pytest.raises(B.BoxError) as e:
        B.download(s, "1", tmp_path / "d", budget_bytes=10 ** 9, is_cancelled=lambda: False, on_progress=lambda d: None)
    assert e.value.code == "DOC_NOT_FOUND"


def test_cancel_stops_between_files(box, tmp_path):
    box.add_pages(2, {"1-1.tif": tiff_bytes(1), "2-1.tif": tiff_bytes(1)})
    s = _open(box)
    with pytest.raises(B.BoxError) as e:
        B.download(s, "2", tmp_path / "d", budget_bytes=10 ** 9, is_cancelled=lambda: True, on_progress=lambda d: None)
    assert e.value.code == "CANCELLED"


# --------------------------------------------------------------------- 連線安全

def test_plain_http_needs_consent_before_anything_is_sent(box):
    with pytest.raises(B.BoxError) as e:
        _open(box, allow_http=False)
    assert e.value.code == "PLAIN_HTTP_NEEDS_CONSENT" and not box.requests


def _self_signed():
    import datetime
    import tempfile
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Apeos-test")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=30)).sign(key, hashes.SHA256()))
    d = tempfile.mkdtemp(prefix="mfpcert-")
    cp, kp = f"{d}/c.pem", f"{d}/k.pem"
    with open(cp, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    with open(kp, "wb") as f:
        f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                  serialization.NoEncryption()))
    # 伺服器端用原本的 stdlib 類別（truststore 換掉的版本不能當伺服器；用戶端照正式環境跑在 truststore 底下）
    from tests.test_mfp_client import _stdlib_ssl_context_class
    ctx = _stdlib_ssl_context_class()(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cp, kp)
    return ctx, hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()


def test_a_self_signed_certificate_needs_the_checked_fingerprint(monkeypatch):
    real = mc.check_address
    monkeypatch.setattr(mc, "check_address", lambda ip: None if ip == "127.0.0.1" else real(ip))
    ctx, fp = _self_signed()
    f = FakeBox()
    f.realm = "box-1"
    f.add_whole(1)
    srv = f.serve(tls_ctx=ctx)
    port = srv.server_address[1]
    monkeypatch.setattr(mc, "ALLOWED_PORTS", tuple(mc.ALLOWED_PORTS) + (port,))
    try:
        def go(trust):
            return B.open_box("127.0.0.1", "127.0.0.1", "https", port, box=1,
                              creds=B.Creds(box_password="1234"), trust_sha256=trust)
        with pytest.raises(B.BoxError) as e:
            go(None)
        assert e.value.code == "TLS_UNTRUSTED" and e.value.detail["certificate"]["sha256"] == fp
        assert not f.requests, "還沒核對指紋，一個請求都不可以送"
        with pytest.raises(B.BoxError) as e:
            go("ab" * 32)
        assert e.value.code == "TLS_IDENTITY_FAILED" and not f.requests
        s = go(":".join(fp[i:i + 2] for i in range(0, 64, 2)).upper())    # 畫面上的寫法也認得
        assert list(s.docs) == ["1"] and s.tls() == {"verified": False, "pinned": True, "sha256": fp}
    finally:
        srv.shutdown()


def test_secrets_never_reach_the_trace_or_repr(box):
    box.realm = "box-1"
    box.password = "S3cret-Box"
    box.add_whole(1)
    trace: list = []
    s = _open(box, B.Creds(box_password="S3cret-Box", user="u", password="S3cret-User"), trace=trace)
    assert repr(B.Creds(box_password="S3cret-Box")) == "<Creds>" and "S3cret" not in repr(s.dav.auth)
    blob = repr(trace)
    assert "S3cret" not in blob and "Basic " not in blob and "authorization" not in blob.lower()
    assert any(t.get("auth") == "basic" for t in trace), "協定紀錄要看得出用了哪一種認證"
    assert any(c.get("realm") == "box-1" for t in trace for c in t.get("challenges", []))


# --------------------------------------------------------------------- 合成 PDF

def test_tiff_and_jpeg_become_one_pdf_with_real_page_sizes(tmp_path):
    import fitz
    t = tmp_path / "a.tif"
    t.write_bytes(tiff_bytes(3))
    j = tmp_path / "b.jpg"
    j.write_bytes(jpeg_bytes())
    out = tmp_path / "o.pdf"
    n = mfp_output.to_pdf([(t, "tiff"), (j, "jpeg")], out)
    assert n == 4
    with fitz.open(out) as d:
        assert d.page_count == 4
        for pg in d:
            assert abs(pg.rect.width - 850 * 72 / 200) < 1, "照 200 dpi 換算頁面大小"


@pytest.mark.parametrize("data,kind", [(b"%PDF-1.7\n", "pdf"), (b"\xff\xd8\xff\xe0", "jpeg"), (b"II*\x00", "tiff"),
                                       (b"MM\x00*", "tiff"), (b"II+\x00", "tiff"), (b"<html>", None), (b"GIF89a", None)])
def test_file_kinds_are_read_from_the_header(tmp_path, data, kind):
    p = tmp_path / "x"
    p.write_bytes(data + b"\x00" * 16)
    assert mfp_output.sniff(p) == kind


def test_a_truncated_or_huge_image_is_invalid(tmp_path, monkeypatch):
    p = tmp_path / "x.jpg"
    p.write_bytes(jpeg_bytes()[:400])
    with pytest.raises(mfp_output.OutputError):
        mfp_output.inspect(p, "jpeg")
    monkeypatch.setattr(mfp_output, "MAX_PIXELS", 1000)
    q = tmp_path / "y.tif"
    q.write_bytes(tiff_bytes(1))
    with pytest.raises(mfp_output.OutputError):
        mfp_output.inspect(q, "tiff")



# --------------------------------------------------------------------- 請求格式（v1.16.99，客戶實機 Apeos C5571 對 allprop 回 406）

_MODERN = b"fujifilm.com/fb/2021/04"
_LEGACY = b"fujixerox.co.jp"


def test_a_406_on_the_modern_probe_tries_the_legacy_probe_once_without_credentials(box):
    box.realm = "box-1"
    box.add_whole(1)
    box.reject_406 = lambda m, body, h: _MODERN in body
    s = _open(box, B.Creds(box_password="1234"))
    assert list(s.docs) == ["1"]
    first, second = box.requests[0], box.requests[1]
    assert _MODERN in first["body"] and _LEGACY in second["body"], "新的被拒才換舊的"
    assert "authorization" not in first["headers"] and "authorization" not in second["headers"], "換格式不帶帳密"
    assert all(_LEGACY in r["body"] for r in box.requests[1:]), "認證與列文件沿用被接受的那一套"


def test_both_probes_refused_stops_with_the_stage_and_no_credentials(box):
    box.realm = "box-1"
    box.reject_406 = lambda m, body, h: True
    trace = []
    with pytest.raises(B.BoxError) as e:
        B.open_box("127.0.0.1", "127.0.0.1", "http", box.port, box=1, creds=B.Creds(box_password="1234"),
                   allow_http=True, trace=trace)
    assert e.value.code == "DAV_REQUEST_NOT_ACCEPTED"
    assert e.value.detail.get("stage") == "probe" and e.value.detail.get("auth_sent") is False
    assert len(box.requests) == 2 and not _auth_sent(box), "兩套各試一次就停，帳密一個字都沒送"
    assert [t.get("profile") for t in trace] == ["modern", "legacy"] and all(t.get("stage") == "probe" for t in trace)
    assert all(t.get("singlepage") == "On" and t.get("auth_sent") is False for t in trace)
    assert "1234" not in str(trace)


def test_a_406_on_the_list_stops_without_switching_formats_with_credentials(box):
    box.realm = "box-1"
    box.add_whole(1)
    box.reject_406 = lambda m, body, h: b"<sss:Pages/>" in body
    with pytest.raises(B.BoxError) as e:
        _open(box, B.Creds(box_password="1234"))
    assert e.value.code == "DAV_REQUEST_NOT_ACCEPTED" and e.value.detail.get("stage") == "list"
    lists = [r for r in box.requests if b"<sss:Pages/>" in r["body"]]
    assert len(lists) == 1, "列文件被拒就停，不帶帳密換格式重送"


def test_a_207_without_the_mailbox_itself_is_not_success(box):
    box.add_whole(1)
    box.reject_406 = None
    box.listing_override = None
    real = box.listing
    box.listing = lambda: real().replace(b"<D:href>/dav/box-1/</D:href>", b"<D:href>/dav/box-9/</D:href>")
    with pytest.raises(B.BoxError) as e:
        _open(box)
    assert e.value.code == "DAV_BAD_RESPONSE"


def test_other_brands_keep_the_generic_webdav_request():
    """Canon Advanced Box 等走 `_Dav` 的其他廠牌：請求格式不受 FUJIFILM 的改動影響。"""
    from app.core import mfp_share
    body, ctype, extra = mfp_share._CanonDav("h", "127.0.0.1", "http", 80, trust_sha256=None, allow_http=True,
                                             trace=[])._propfind_spec()
    assert b"allprop" in body and ctype == 'text/xml; charset="utf-8"' and extra == {}
