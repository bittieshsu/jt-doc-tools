"""KYOCERA / RICOH 直接取件（SOAP，實驗功能）—— 用照研究文件寫的假設備驗流程與安全邊界。

假設備照研究文件（內部 `docs-share/mfp-fetch/ricoh-kyocera-direct-protocol-20261015/`）的方法、欄位回應；
**不是實機樣本**。這裡驗的是「我們送的東西跟文件一致、收到各種回應時的行為對」：

* 請求：KYOCERA SOAP 1.2（wsa:Action、token 放 WS-Security 的 Username）；RICOH SOAP 1.1 encoded（SOAPAction、xsi:type）。
* 登入：帳密只送到認證服務、只送一次；錯就停；不需要帳號的情況不送帳密；RICOH 認證網址不在這台設備就不連。
* 列舉 / 查詢：分批讀到「結束」為止、最後一批也處理；一定釋放列舉、結束工作階段。
* 下載：設備準備中會等（有上限）、多次回應各存一份不串接、宣告大小不符就停；附件形式不支援就明講；
  RICOH 下載網址一定在同一台設備、POST 的 body 照回應還原。
* 協定紀錄裡沒有密碼、token、票證、工作階段代碼、下載網址。
"""
from __future__ import annotations

import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from xml.sax.saxutils import escape

import pytest

from app.core import mfp_box
from app.core import mfp_client as mc
from app.core import mfp_soap as K
from tests.test_mfp_box import tiff_bytes

ENV12, ENV11 = K.ENV12, K.ENV11


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _leafs(el) -> dict:
    out = {}
    for d in el.iter():
        if len(d) == 0:
            out.setdefault(_local(d.tag), (d.text or ""))
    return out


class _Server:
    def start(self):
        fake = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _send(self, code, body=b"", ctype="application/soap+xml; charset=utf-8"):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if body:
                    self.wfile.write(body)

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                code, out, ctype = fake.handle_post(self.path, dict(self.headers), body)
                self._send(code, out, ctype)

            def do_GET(self):
                code, out, ctype = fake.handle_get(self.path, dict(self.headers))
                self._send(code, out, ctype)

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.port = self.srv.server_address[1]
        return self

    def stop(self):
        self.srv.shutdown()


# =========================================================================== 假 KYOCERA

class FakeKyocera(_Server):
    def __init__(self):
        self.auth_method = "LOCAL_AUTHENTICATION"
        self.users = {"clerk": "pw-1"}
        self.boxes = {
            1: {"name": "會計", "owner": "", "password": "", "docs": {"合約": tiff_bytes(2), "報價 / 2026": tiff_bytes(1)}},
            2: {"name": "個人", "owner": "clerk", "password": "1234", "docs": {"私人": tiff_bytes(1)}},
        }
        self.token = "TOKEN-XYZ"
        self.calls: list[dict] = []
        self.enums: dict[str, list] = {}
        self.destroyed: list[str] = []
        self.ops: dict[str, dict] = {}
        self.preparing = 1          # 第一次取資料回「準備中」幾次
        self.parts_per_doc = 1      # 一份文件分幾次回（多次回應的情況）
        self.final_with_data = True  # 最後一批用 ALL_GET_COMPLETE 同時帶資料
        self.wrong_size = False
        self.multipart = False
        self.logged_out = False
        self.n = 0

    def _resp(self, ns, method, inner: str, code=200) -> tuple:
        x = (f'<?xml version="1.0" encoding="utf-8"?><env:Envelope xmlns:env="{ENV12}" xmlns:k="{ns}">'
             f'<env:Body><k:{method}Response>{inner}</k:{method}Response></env:Body></env:Envelope>')
        return code, x.encode(), "application/soap+xml; charset=utf-8"

    def handle_get(self, path, headers):
        return 404, b"", "text/plain"

    def handle_post(self, path, headers, body):
        from defusedxml.ElementTree import fromstring
        if path not in K.KYO_PATHS.values():
            return 404, b"", "text/plain"
        root = fromstring(body)
        b = next(el for el in root if _local(el.tag) == "Body")
        req = next(iter(b))
        method = _local(req.tag)[:-len("Request")]
        hdr = next((el for el in root if _local(el.tag) == "Header"), None)
        action = token = None
        if hdr is not None:
            for d in hdr.iter():
                if _local(d.tag) == "Action":
                    action = d.text
                if _local(d.tag) == "Username":
                    token = d.text
        f = _leafs(req)
        self.calls.append({"path": path, "method": method, "action": action, "token": token,
                           "ctype": headers.get("Content-Type"), "fields": f, "body": body,
                           "top": {_local(k.tag): (k.text or "") for k in req if len(k) == 0}})
        ns = req.tag[1:].split("}")[0]
        need = self.auth_method in ("LOCAL_AUTHENTICATION", "NETWORK_AUTHENTICATION")
        if method == "get_authentication_status":
            return self._resp(ns, method, f"<k:authentication_method>{self.auth_method}</k:authentication_method>"
                                          "<k:result>SUCCESS</k:result>")
        if method == "login_system":
            if self.users.get(f.get("userid")) != f.get("password"):
                return self._resp(ns, method, "<k:result>INVALID_PASSWORD_OR_USERID_ERROR</k:result>")
            return self._resp(ns, method, f"<k:usertoken><k:encode>BASE64</k:encode><k:token>{self.token}</k:token>"
                                          "</k:usertoken><k:result>SUCCESS</k:result>")
        if need and token != self.token:
            return self._resp(ns, method, "<k:result>INVALID_TOKEN_ERROR</k:result>")
        if method == "logout_system":
            self.logged_out = True
            return self._resp(ns, method, "<k:result>SUCCESS</k:result>")
        if method in ("destroy_box_enumeration", "destroy_document_enumeration"):
            self.destroyed.append(f.get("enumeration"))
            return self._resp(ns, method, "<k:result>SUCCESS</k:result>")
        if method == "create_box_enumeration":
            items = [(num, b) for num, b in sorted(self.boxes.items())]
            return self._new_enum(ns, method, items, int(f.get("range") or 1))
        if method == "create_document_enumeration":
            num = int(f.get("number") or 0)
            box = self.boxes.get(num)
            if box is None:
                return self._resp(ns, method, "<k:result>BOX_NOT_FOUND_ERROR</k:result>")
            if box["password"] and f.get("password") != box["password"]:
                return self._resp(ns, method, "<k:result>INVALID_PASSWORD_ERROR</k:result>")
            return self._new_enum(ns, method, list(box["docs"].items()), int(f.get("range") or 1))
        if method in ("get_box_information", "get_document_information"):
            e = self.enums.get(f.get("enumeration"))
            if e is None:
                return self._resp(ns, method, "<k:result>INVALID_VALUE_ERROR</k:result>")
            batch, e["items"] = e["items"][:e["range"]], e["items"][e["range"]:]
            res = "ALL_GET_COMPLETE" if not e["items"] else "SUCCESS"
            if method == "get_box_information":
                inner = "".join(f"<k:information><k:common><k:number>{num}</k:number><k:name>{escape(bx['name'])}</k:name>"
                                f"<k:owner>{bx['owner']}</k:owner></k:common><k:document_count>{len(bx['docs'])}"
                                "</k:document_count></k:information>" for num, bx in batch)
            else:
                inner = "".join(f"<k:document_information><k:common><k:name>{escape(nm)}</k:name><k:page_count>2</k:page_count>"
                                f"<k:data_size>{len(data)}</k:data_size><k:final_modify_date>2026-10-15T09:30:00"
                                "</k:final_modify_date></k:common></k:document_information>" for nm, data in batch)
            return self._resp(ns, method, inner + f"<k:result>{res}</k:result>")
        if method == "prepare_downloading_data":
            num = int(f.get("box_number") or 0)
            box = self.boxes.get(num)
            if box is None:
                return self._resp(ns, method, "<k:result>BOX_NOT_FOUND_ERROR</k:result>")
            if box["password"] and f.get("password") != box["password"]:
                return self._resp(ns, method, "<k:result>INVALID_PASSWORD_ERROR</k:result>")
            data = box["docs"].get(f.get("file_name"))
            if data is None:
                return self._resp(ns, method, "<k:result>DOCUMENT_NOT_FOUND_ERROR</k:result>")
            self.n += 1
            op = f"OP{self.n}"
            self.ops[op] = {"parts": [data] * self.parts_per_doc, "preparing": self.preparing, "fields": f}
            return self._resp(ns, method, f"<k:operation_id>{op}</k:operation_id><k:result>SUCCESS</k:result>")
        if method == "start_downloading_data":
            op = self.ops.get(f.get("operation_id"))
            if op is None:
                return self._resp(ns, method, "<k:result>INVALID_OPERATION_ID_ERROR</k:result>")
            if op["preparing"] > 0:
                op["preparing"] -= 1
                return self._resp(ns, method, "<k:result>PREPARING_NOW</k:result>")
            if not op["parts"]:
                return self._resp(ns, method, "<k:result>ALL_GET_COMPLETE</k:result>")
            if self.multipart:
                return 200, b"--x\r\n\r\n--x--", 'multipart/related; boundary="x"; type="application/xop+xml"'
            data = op["parts"].pop(0)
            last = not op["parts"]
            res = "ALL_GET_COMPLETE" if (last and self.final_with_data) else "SUCCESS"
            size = len(data) + (5 if self.wrong_size else 0)
            return self._resp(ns, method, f"<k:data><k:data_size>{size}</k:data_size><k:file_name>doc.tif</k:file_name>"
                                          f"<k:binary_data>{base64.b64encode(data).decode()}</k:binary_data>"
                                          f"<k:file_type>TIFF</k:file_type></k:data><k:result>{res}</k:result>")
        return self._resp(ns, method, "<k:result>INTERNAL_ERROR</k:result>")

    def _new_enum(self, ns, method, items, rng):
        self.n += 1
        eid = f"E{self.n}"
        self.enums[eid] = {"items": items, "range": max(1, rng)}
        return self._resp(ns, method, f"<k:enumeration>{eid}</k:enumeration><k:result>SUCCESS</k:result>")


@pytest.fixture
def kyo(monkeypatch):
    real = mc.check_address
    monkeypatch.setattr(mc, "check_address", lambda ip: None if ip == "127.0.0.1" else real(ip))
    monkeypatch.setattr(K.time, "sleep", lambda s: None)
    f = FakeKyocera().start()
    monkeypatch.setattr(K, "KYO_PORTS", {"https": f.port, "http": f.port})
    yield f
    f.stop()


def _kyo_session(kyo, trace, user="clerk", password="pw-1", box_password=""):
    return K.KyoceraSession("kyocera_soap_http", "127.0.0.1", "127.0.0.1", "http", user=user, password=password,
                            box_password=box_password, trust_sha256=None, allow_http=True, trace=trace)


def test_kyocera_requests_follow_the_research(kyo):
    trace = []
    s = _kyo_session(kyo, trace)
    s.roots()
    s.close()
    first, login = kyo.calls[0], kyo.calls[1]
    assert first["method"] == "get_authentication_status" and first["token"] is None
    assert first["action"] == K.KYO_NS["auth"] + "/get_authentication_status"
    assert first["ctype"].startswith("application/soap+xml")
    assert login["method"] == "login_system" and login["fields"]["userid"] == "clerk"
    assert login["fields"]["authentication_method"] == "LOCAL_AUTHENTICATION" and login["token"] is None
    after = [c for c in kyo.calls[2:]]
    assert after and all(c["token"] == kyo.token for c in after), "登入後每個請求都要在 WS-Security 帶回傳的 token"
    assert after[-1]["method"] == "logout_system" and kyo.logged_out
    # 登入密碼只出現在登入那一個請求
    assert sum(b"pw-1" in c["body"] for c in kyo.calls) == 1


def test_kyocera_lists_boxes_and_documents_and_releases_enumerations(kyo):
    trace = []
    s = _kyo_session(kyo, trace)
    roots = s.roots()
    assert [(r["id"], r["label"], r["shared"]) for r in roots] == [("1", "1 會計", True), ("2", "2 個人", False)]
    docs = s.list("1", [])
    assert [d["label"] for d in docs] == ["合約", "報價 / 2026"], "名稱有斜線也要列得出來"
    assert all(K.dec_name(d["name"]) == d["label"] for d in docs) and docs[0]["size"] > 0 and docs[0]["modified"]
    created = [c["fields"].get("enumeration") for c in kyo.calls if c["method"].startswith("create_")]
    assert len(kyo.destroyed) == 2, "列舉一定要釋放"
    s.close()


def test_kyocera_downloads_after_preparing(kyo, tmp_path):
    trace = []
    s = _kyo_session(kyo, trace)
    name = s.list("1", [])[0]["name"]
    got = s.download("1", [name], tmp_path / "a.bin", 10 * 1024 * 1024)
    s.close()
    assert (tmp_path / "a.bin").read_bytes() == kyo.boxes[1]["docs"]["合約"] and "parts" not in got
    prep = next(c for c in kyo.calls if c["method"] == "prepare_downloading_data")
    assert prep["fields"]["box_number"] == "1"
    assert (prep["top"]["file_name"], prep["top"]["type"], prep["top"]["file_separation"]) == ("合約", "TIFF", "OFF")
    starts = [c for c in kyo.calls if c["method"] == "start_downloading_data"]
    assert len(starts) == 2, "第一次「準備中」要再問"


def test_kyocera_several_responses_are_kept_as_separate_parts(kyo, tmp_path):
    kyo.parts_per_doc, kyo.final_with_data = 2, False
    s = _kyo_session(kyo, [])
    got = s.download("1", [K.enc_name("合約")], tmp_path / "a.bin", 10 * 1024 * 1024)
    s.close()
    assert len(got["parts"]) == 2 and all(p.read_bytes() == kyo.boxes[1]["docs"]["合約"] for p in got["parts"]), \
        "多次回應各存一份，不串接"


@pytest.mark.parametrize("setup,code", [
    (lambda f: setattr(f, "wrong_size", True), "DOC_INCOMPLETE"),
    (lambda f: setattr(f, "multipart", True), "SOAP_ATTACHMENT_UNSUPPORTED"),
    (lambda f: setattr(f, "preparing", 10 ** 6), "SOAP_BUSY"),
])
def test_kyocera_download_problems_stop_cleanly(kyo, tmp_path, setup, code):
    setup(kyo)
    s = _kyo_session(kyo, [])
    with pytest.raises(mfp_box.BoxError) as ei:
        s.download("1", [K.enc_name("合約")], tmp_path / "a.bin", 10 * 1024 * 1024)
    s.close()
    assert ei.value.code == code


def test_kyocera_auth_cases(kyo):
    with pytest.raises(mfp_box.BoxError) as ei:
        _kyo_session(kyo, [], user="")
    assert ei.value.code == "AUTH_REQUIRED_USER" and not any(c["method"] == "login_system" for c in kyo.calls), \
        "設備要求登入但沒填帳號：不送任何帳密"
    kyo.calls.clear()
    with pytest.raises(mfp_box.BoxError) as ei:
        _kyo_session(kyo, [], password="wrong")
    assert ei.value.code == "AUTH_FAILED"
    assert sum(c["method"] == "login_system" for c in kyo.calls) == 1, "認證失敗不重試"
    kyo.auth_method = "OFF"
    kyo.calls.clear()
    s = _kyo_session(kyo, [], user="")
    assert [r["id"] for r in s.roots()] == ["1", "2"], "設備沒有要求登入就不登入"
    assert kyo.calls and not any(c["method"] == "login_system" for c in kyo.calls)


def test_kyocera_box_password(kyo, tmp_path):
    s = _kyo_session(kyo, [])
    with pytest.raises(mfp_box.BoxError) as ei:
        s.list("2", [])
    assert ei.value.code == "BOX_PASSWORD_FAILED"
    s.close()
    s = _kyo_session(kyo, [], box_password="1234")
    assert [d["label"] for d in s.list("2", [])] == ["私人"]
    s.download("2", [K.enc_name("私人")], tmp_path / "p.bin", 10 * 1024 * 1024)
    s.close()


def test_kyocera_rejects_bad_names_and_roots(kyo):
    s = _kyo_session(kyo, [])
    for root, path in (("1/2", []), ("x", []), ("1", ["sub"]), ("1", ["../x"])):
        with pytest.raises(mfp_box.BoxError):
            s.list(root, path)
    with pytest.raises(mfp_box.BoxError):
        K.dec_name("not-encoded")
    s.close()


def test_kyocera_trace_has_no_secrets(kyo, tmp_path):
    trace = []
    s = _kyo_session(kyo, trace, box_password="1234")
    s.list("2", [])
    s.download("2", [K.enc_name("私人")], tmp_path / "p.bin", 10 * 1024 * 1024)
    s.close()
    dump = json.dumps(trace, ensure_ascii=False)
    for secret in ("pw-1", "1234", kyo.token):
        assert secret not in dump, f"協定紀錄裡有秘密：{secret}"
    assert any(t.get("call") == "login_system" for t in trace) and all("ms" in t for t in trace)


# =========================================================================== 假 RICOH

def _ritem(name, value) -> str:
    return (f'<item xsi:type="ct:property"><propName xsi:type="xsd:string">{escape(name)}</propName>'
            f'<propVal xsi:type="xsd:string">{escape(str(value))}</propVal></item>')


def _rarray(items: list[str], t="ct:property") -> str:
    return f'xsi:type="soapenc:Array" soapenc:arrayType="{t}[{len(items)}]">' + "".join(items)


class FakeRicoh(_Server):
    def __init__(self):
        self.users = {"clerk": "pw-2"}
        self.ticket = base64.b64encode(b"TICKET-ABC").decode()
        self.sid = "SID-777"
        self.auth_url_host = None           # None＝這台；其他值＝別的主機
        self.ticket_ok = "true"
        self.docs = {
            "doc-1": {"title": "合約", "elements": ["e1", "e2"], "scheme": "http+get", "locked": False},
            "doc/2": {"title": "報價", "elements": ["e3"], "scheme": "http+post", "locked": False},
            "doc-3": {"title": "會議紀錄", "elements": [], "scheme": "http+get", "locked": True},
        }
        self.password = "9999"
        self.checked: set = set()
        self.calls: list[dict] = []
        self.download_scheme_override = None
        self.protocol_version_fault = False
        self.classes = ["basic", "basic.startSessionByAuthTicket", "query.advancedSearch", "contentElement",
                        "rendition", "objectPassword"]
        self.url_host_override = None
        self.terminated = False

    def _env(self, method, inner, ns=K.RICOH_REPO_NS) -> tuple:
        x = (f'<?xml version="1.0" encoding="utf-8"?><soapenv:Envelope xmlns:soapenv="{ENV11}" '
             f'xmlns:soapenc="{K.ENC11}" xmlns:xsi="{K.XSI}" xmlns:xsd="{K.XSD}" xmlns:ct="{K.RICOH_CT}" '
             f'xmlns:m="{ns}"><soapenv:Body><m:{method}Response>{inner}</m:{method}Response></soapenv:Body>'
             '</soapenv:Envelope>')
        return 200, x.encode(), "text/xml; charset=utf-8"

    def _fault(self, text) -> tuple:
        x = (f'<soapenv:Envelope xmlns:soapenv="{ENV11}"><soapenv:Body><soapenv:Fault><faultcode>soapenv:Server</faultcode>'
             f'<faultstring>{text}</faultstring><detail><code>{text}</code></detail></soapenv:Fault></soapenv:Body></soapenv:Envelope>')
        return 500, x.encode(), "text/xml; charset=utf-8"

    def handle_get(self, path, headers):
        self.calls.append({"path": path, "method": "GET"})
        if path.startswith("/DH/dl/"):
            return 200, tiff_bytes(1), "image/tiff"
        return 404, b"", "text/plain"

    def handle_post(self, path, headers, body):
        from defusedxml.ElementTree import fromstring
        if path == "/DH/dlpost":
            self.calls.append({"path": path, "method": "POSTDL", "body": body, "ctype": headers.get("Content-Type")})
            if body != b"<download id='e3'/>":
                return 400, b"", "text/plain"
            from tests.test_mfp_escl import pdf_bytes
            return 200, pdf_bytes(1), "application/pdf"
        if path not in (K.RICOH_REPO_PATH, "/DH/uauthentication"):
            return 404, b"", "text/plain"
        root = fromstring(body)
        b = next(el for el in root if _local(el.tag) == "Body")
        req = next(iter(b))
        method = _local(req.tag)
        params = {_local(k.tag): k for k in req}
        val = {k: (v.text or "") for k, v in params.items()}
        self.calls.append({"path": path, "method": method, "action": headers.get("SOAPAction"), "body": body,
                           "ctype": headers.get("Content-Type"), "params": val,
                           "types": {k: v.get(f"{{{K.XSI}}}type") for k, v in params.items()}})
        if method == "getProtocolVersion":
            if self.protocol_version_fault:
                return self._fault("COMMON_UNKNOWN_METHOD")
            return self._env(method, '<return xsi:type="xsd:int">3</return>')
        if method == "getServiceCapability":
            host = self.auth_url_host or "127.0.0.1"
            props = [_ritem("uauthenticationUrl", f"http://{host}:{self.port}/DH/uauthentication"),
                     _ritem("uauthenticationProviderName", "LocalProvider"), _ritem("uauthenticationDomainName", ""),
                     _ritem("isUserAuthenticatableByTicket", self.ticket_ok),
                     _ritem("maxResultRowBlockSizeForDocIDList", "2"), _ritem("machineType", "IM C3010")]
            cls = "".join(f"<item>{c}</item>" for c in self.classes)
            return self._env(method, f'<return {_rarray(props)}</return>'
                                     f'<supportedClasses xsi:type="soapenc:Array" soapenc:arrayType="xsd:string[{len(self.classes)}]">'
                                     f'{cls}</supportedClasses>')
        if method == "authenticateByPassword":
            if path != "/DH/uauthentication":
                return self._fault("WRONG_ENDPOINT")
            if self.users.get(val.get("authName")) != val.get("password"):
                return self._fault("UAUTH_INVALID_PASSWORD")
            return self._env(method, f'<return xsi:type="xsd:base64Binary">{self.ticket}</return>', K.RICOH_AUTH3_NS)
        if method == "startSessionByAuthTicket":
            if val.get("authTicket") != self.ticket:
                return self._fault("COMMON_BAD_TICKET")
            return self._env(method, f'<return xsi:type="xsd:string">{self.sid}</return>')
        if val.get("sessionId") != self.sid:
            return self._fault("COMMON_BAD_SESSION_ID")
        if method == "terminateSession":
            self.terminated = True
            return self._env(method, '<return xsi:type="xsd:boolean">true</return>')
        if method == "executeAdvancedSearch":
            ids = list(self.docs)
            off, size = int(val["rowOffset"]), int(val["resultRowBlockSize"])
            batch = ids[off:off + size]
            status = "QUERY_AT_END" if off + size >= len(ids) else "SUCCESS"
            # 一列用 multiRef（href / id）—— 剖析器要會解參照
            rows, refs = [], []
            for n, i in enumerate(batch):
                if n == 0:
                    rows.append(f'<item href="#r{off}"/>')
                    refs.append(f'<multiRef id="r{off}" {_rarray([_ritem("identifier", i)])}</multiRef>')
                else:
                    rows.append(f'<item {_rarray([_ritem("identifier", i)])}</item>')
            inner = (f'<return xsi:type="xsd:string">{status}</return><resultSetId>RS</resultSetId>'
                     f'<numOfResults>{len(ids)}</numOfResults><rowList {_rarray(rows, "ct:property[]")}</rowList>')
            code, x, ct = self._env(method, inner)
            x = x.replace(b"</soapenv:Body>", "".join(refs).encode() + b"</soapenv:Body>")
            return code, x, ct
        if method == "getProps":
            d = self.docs.get(val["objectId"])
            if d is None:
                return self._fault("REPOSITORY_BAD_OBJECT_ID")
            other = [_ritem("nf:application", "scan"), _ritem("nf:passwordProtected", "true" if d["locked"] else "false")]
            props = [_ritem("identifier", val["objectId"]), _ritem("title", d["title"]),
                     _ritem("contentLength", "1234"), _ritem("modifiedDate", "2026-10-15T01:02:03Z")]
            props.append(f'<item xsi:type="ct:property"><propName>otherProps</propName><propVal {_rarray(other)}</propVal></item>')
            return self._env(method, f'<return {_rarray(props)}</return>')
        if method == "checkObjectPassword":
            ok = val["password"] == self.password
            if ok:
                self.checked.add(val["objectId"])
            return self._env(method, f'<return xsi:type="xsd:boolean">{"true" if ok else "false"}</return>')
        if method == "getDocRendition":
            d = self.docs.get(val["docId"])
            if d is None:
                return self._fault("REPOSITORY_BAD_OBJECT_ID")
            if d["locked"] and val["docId"] not in self.checked:
                return self._fault("REPOSITORY_NEED_PASSWORD")
            item = params["renditionRequest"][0]
            pref = {_local(k.tag): k.text for k in item}
            if pref != {"name": "nf:renditionType", "value": "original", "preference": "0", "optional": "false"}:
                return self._fault("REPOSITORY_BAD_RENDITION")
            return self._env(method, f'<return {_rarray([_ritem("identifier", "R-" + val["docId"])])}</return>')
        if method == "getDocElementList":
            doc = val["docId"][2:]
            els = self.docs[doc]["elements"]
            start = int(val["startIndex"])
            batch = els[start:start + 1]           # 一次一個：驗 TRUNCATED 的續讀
            status = "TRUNCATED" if start + 1 < len(els) else "SUCCESS"
            lists = [f'<item {_rarray([_ritem("identifier", e)])}</item>' for e in batch]
            return self._env(method, f'<return xsi:type="xsd:string">{status}</return>'
                                     f'<propLists {_rarray(lists, "ct:property[]")}</propLists>'
                                     f'<numOfAllElements>{len(els)}</numOfAllElements>')
        if method == "getDocContentUrl":
            eid = val["docId"]
            doc = next((k for k, d in self.docs.items() if eid in d["elements"] or eid == "R-" + k), None)
            d = self.docs[doc]
            scheme = self.download_scheme_override or d["scheme"]
            host = self.url_host_override or "127.0.0.1"
            if scheme == "http+post":
                url = f"http://{host}:{self.port}/DH/dlpost"
                param = [_ritem("body", base64.b64encode(f"<download id='{eid}'/>".encode()).decode())]
            else:
                url = f"http://{host}:{self.port}/DH/dl/{eid}?t=SECRETURLTOKEN"
                param = []
            return self._env(method, f'<return xsi:type="xsd:string">{escape(url)}</return>'
                                     f'<scheme xsi:type="xsd:string">{scheme}</scheme>'
                                     f'<parameter {_rarray(param)}</parameter>')
        return self._fault("UNKNOWN_METHOD")


@pytest.fixture
def ricoh(monkeypatch):
    real = mc.check_address
    monkeypatch.setattr(mc, "check_address", lambda ip: None if ip == "127.0.0.1" else real(ip))
    f = FakeRicoh().start()
    yield f
    f.stop()


def _ricoh_session(f, trace, user="clerk", password="pw-2", doc_password=""):
    return K.RicohSession("ricoh_soap", "127.0.0.1", "127.0.0.1", "http", f.port, user=user, password=password,
                          doc_password=doc_password, trust_sha256=None, allow_http=True, trace=trace)


def test_ricoh_signs_in_with_a_ticket_and_ends_the_session(ricoh):
    s = _ricoh_session(ricoh, [])
    s.close()
    seq = [(c["path"], c["method"]) for c in ricoh.calls]
    assert seq == [("/DH/repository", "getProtocolVersion"), ("/DH/repository", "getServiceCapability"),
                   ("/DH/uauthentication", "authenticateByPassword"),
                   ("/DH/repository", "startSessionByAuthTicket"), ("/DH/repository", "terminateSession")]
    cap, auth, start = ricoh.calls[1], ricoh.calls[2], ricoh.calls[3]
    assert cap["action"] == f'"{K.RICOH_REPO_NS}#getServiceCapability"' and cap["ctype"].startswith("text/xml")
    assert cap["params"]["sessionId"] == "" and cap["types"]["sessionId"] == "xsd:string"
    assert auth["action"] == f'"{K.RICOH_AUTH3_NS}#authenticateByPassword"'
    assert list(auth["params"]) == ["providerName", "domainName", "authName", "password", "duration", "options"]
    assert auth["params"]["providerName"] == "LocalProvider" and auth["types"]["duration"] == "xsd:int"
    assert start["params"]["authTicket"] == ricoh.ticket and start["types"]["authTicket"] == "xsd:base64Binary"
    assert start["params"]["timeLimit"] == "1800" and ricoh.terminated
    assert sum(b"pw-2" in c.get("body", b"") for c in ricoh.calls) == 1, "密碼只送給認證服務一次"


def test_ricoh_auth_cases(ricoh):
    with pytest.raises(mfp_box.BoxError) as ei:
        _ricoh_session(ricoh, [], user="")
    assert ei.value.code == "AUTH_REQUIRED_USER"
    assert [c["method"] for c in ricoh.calls] == ["getProtocolVersion", "getServiceCapability"]
    ricoh.calls.clear()
    with pytest.raises(mfp_box.BoxError) as ei:
        _ricoh_session(ricoh, [], password="wrong")
    assert ei.value.code == "AUTH_FAILED" and sum(c["method"] == "authenticateByPassword" for c in ricoh.calls) == 1
    ricoh.calls.clear()
    ricoh.auth_url_host = "evil.example"
    with pytest.raises(mfp_box.BoxError) as ei:
        _ricoh_session(ricoh, [])
    assert ei.value.code == "RICOH_AUTH_URL_UNTRUSTED"
    assert not any(b"pw-2" in c.get("body", b"") for c in ricoh.calls), "認證網址不在這台設備：密碼一個字都不送"
    ricoh.auth_url_host, ricoh.ticket_ok = None, "false"
    with pytest.raises(mfp_box.BoxError) as ei:
        _ricoh_session(ricoh, [])
    assert ei.value.code == "RICOH_AUTH_UNSUPPORTED"


def test_ricoh_lists_documents_in_batches(ricoh):
    s = _ricoh_session(ricoh, [])
    docs = s.list("documents", [])
    s.close()
    assert {d["label"] for d in docs} == {"合約", "報價", "會議紀錄"}
    assert {K.dec_name(d["name"]) for d in docs} == {"doc-1", "doc/2", "doc-3"}, "識別碼有斜線也要能用"
    assert next(d for d in docs if d["label"] == "會議紀錄")["locked"] is True
    searches = [c for c in ricoh.calls if c["method"] == "executeAdvancedSearch"]
    assert [c["params"]["rowOffset"] for c in searches] == ["0", "2"], "依實收筆數往下讀，讀到 QUERY_AT_END"
    assert searches[0]["params"]["from"] == "document" and searches[0]["params"]["fromFolderId"] == ""


def test_ricoh_downloads_original_elements_by_get_and_post(ricoh, tmp_path):
    s = _ricoh_session(ricoh, [])
    s.list("documents", [])
    got = s.download("documents", [K.enc_name("doc-1")], tmp_path / "a.bin", 10 * 1024 * 1024)
    assert len(got["parts"]) == 2, "兩個內容元素各一個檔案（中間 TRUNCATED 也要續讀）"
    got2 = s.download("documents", [K.enc_name("doc/2")], tmp_path / "b.bin", 10 * 1024 * 1024)
    assert (tmp_path / "b.bin").read_bytes().startswith(b"%PDF") and "parts" not in got2
    post = next(c for c in ricoh.calls if c["method"] == "POSTDL")
    assert post["body"] == b"<download id='e3'/>" and post["ctype"].startswith("text/xml"), "POST body 照回應的 Base64 還原"
    assert s.display_name("documents", [K.enc_name("doc/2")]) == "報價"
    s.close()


def test_ricoh_document_passwords(ricoh, tmp_path):
    s = _ricoh_session(ricoh, [])
    with pytest.raises(mfp_box.BoxError) as ei:
        s.download("documents", [K.enc_name("doc-3")], tmp_path / "c.bin", 10 * 1024 * 1024)
    assert ei.value.code == "DOC_PASSWORD_REQUIRED"
    s.close()
    s = _ricoh_session(ricoh, [], doc_password="0000")
    with pytest.raises(mfp_box.BoxError) as ei:
        s.download("documents", [K.enc_name("doc-3")], tmp_path / "c.bin", 10 * 1024 * 1024)
    assert ei.value.code == "DOC_PASSWORD_FAILED"
    s.close()
    s = _ricoh_session(ricoh, [], doc_password="9999")
    s.download("documents", [K.enc_name("doc-3")], tmp_path / "c.bin", 10 * 1024 * 1024)
    s.close()
    assert (tmp_path / "c.bin").stat().st_size > 0, "沒有內容元素時，表示本身就是要下載的內容"


@pytest.mark.parametrize("attr,value", [("url_host_override", "evil.example"), ("download_scheme_override", "ftp+get")])
def test_ricoh_download_url_must_be_this_device(ricoh, tmp_path, attr, value):
    setattr(ricoh, attr, value)
    s = _ricoh_session(ricoh, [])
    with pytest.raises(mfp_box.BoxError) as ei:
        s.download("documents", [K.enc_name("doc-1")], tmp_path / "a.bin", 10 * 1024 * 1024)
    s.close()
    assert ei.value.code == "RICOH_URL_UNSUPPORTED"
    assert not any(c["method"] in ("GET", "POSTDL") for c in ricoh.calls), "不在這台設備的網址一律不連"


def test_ricoh_trace_has_no_secrets(ricoh, tmp_path):
    trace = []
    s = _ricoh_session(ricoh, trace, doc_password="9999")
    s.list("documents", [])
    s.download("documents", [K.enc_name("doc-1")], tmp_path / "a.bin", 10 * 1024 * 1024)
    s.close()
    dump = json.dumps(trace, ensure_ascii=False)
    for secret in ("pw-2", "9999", ricoh.ticket, ricoh.sid, "SECRETURLTOKEN"):
        assert secret not in dump, f"協定紀錄裡有秘密：{secret}"
    assert any(t.get("call") == "getServiceCapability" for t in trace)


def test_rejected_inputs():
    assert K.dec_name(K.enc_name("報價 / 2026")) == "報價 / 2026"
    for bad in ("", "~", "~a/b", "~" + "A" * 300, "abc"):
        with pytest.raises(mfp_box.BoxError):
            K.dec_name(bad)


def test_ricoh_uses_only_the_features_the_device_lists(ricoh, tmp_path):
    """研究文件：按服務能力實際列出的功能類別決定能用什麼，不因為元件有那個方法就呼叫。"""
    ricoh.classes = ["basic", "query.advancedSearch", "rendition"]          # 沒有票證登入
    with pytest.raises(mfp_box.BoxError) as ei:
        _ricoh_session(ricoh, [])
    assert ei.value.code == "RICOH_AUTH_UNSUPPORTED"
    assert not any(b"pw-2" in c.get("body", b"") for c in ricoh.calls), "不支援票證登入：密碼一個字都不送"

    ricoh.classes = ["basic", "basic.startSessionByAuthTicket", "rendition"]   # 沒有查詢
    s = _ricoh_session(ricoh, [])
    with pytest.raises(mfp_box.BoxError) as ei:
        s.list("documents", [])
    assert ei.value.code == "RICOH_FEATURE_UNSUPPORTED"
    assert not any(c["method"] == "executeAdvancedSearch" for c in ricoh.calls)
    s.close()

    ricoh.classes = ["basic", "basic.startSessionByAuthTicket", "query.advancedSearch", "rendition"]   # 沒有內容元素
    s = _ricoh_session(ricoh, [])
    got = s.download("documents", [K.enc_name("doc-1")], tmp_path / "a.bin", 10 * 1024 * 1024)
    s.close()
    assert "parts" not in got and not any(c["method"] == "getDocElementList" for c in ricoh.calls), \
        "設備沒列內容元素：直接下載表示本身，不呼叫元素清單"

    s = _ricoh_session(ricoh, [], doc_password="9999")                         # 沒有文件密碼
    with pytest.raises(mfp_box.BoxError) as ei:
        s.download("documents", [K.enc_name("doc-3")], tmp_path / "c.bin", 10 * 1024 * 1024)
    s.close()
    assert ei.value.code == "RICOH_FEATURE_UNSUPPORTED"
    assert not any(c["method"] == "checkObjectPassword" for c in ricoh.calls), "不支援就不送文件密碼"


def test_ricoh_without_protocol_version_still_works(ricoh):
    """有的機型可能沒有 getProtocolVersion：照樣往下走。"""
    ricoh.protocol_version_fault = True
    s = _ricoh_session(ricoh, [])
    assert {d["label"] for d in s.list("documents", [])} == {"合約", "報價", "會議紀錄"}
    s.close()


@pytest.mark.parametrize("first", ["app.core.mfp_soap", "app.core.mfp_share", "app.core.mfp_scan",
                                   "app.tools.mfp_fetch.router"])
def test_messages_and_fatal_codes_are_registered_whichever_module_loads_first(first):
    """`mfp_soap` 跟 `mfp_share` 互相匯入：先匯入哪一個，訊息與「整件就停」的代碼都要在（新的直譯器裡驗）。"""
    import subprocess
    import sys
    code = (f"import {first}\n"
            "from app.core import mfp_share as S\n"
            "assert S.MESSAGES['SOAP_FAULT'] and S.ShareError('RICOH_AUTH_UNSUPPORTED').message != 'RICOH_AUTH_UNSUPPORTED'\n"
            "assert {'SOAP_UNAVAILABLE', 'SESSION_EXPIRED', 'BOX_PASSWORD_FAILED'} <= S.FATAL\n")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr[-800:]
