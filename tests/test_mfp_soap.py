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
    def start(self, host: str = "127.0.0.1"):
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

        self.srv = ThreadingHTTPServer((host, 0), H)
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
        # v1.17.4 遙控資料盒（Polling Box）：沒有匣號；清單可能先回 PREPARING_NOW
        self.polling = {"doc00211620261020": tiff_bytes(1)}
        self.polling_preparing = 0
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
        if method == "create_pollingbox_document_enumeration":
            if "type" in f or "number" in f or "password" in f:
                return self._resp(ns, method, "<k:result>INVALID_VALUE_ERROR</k:result>")
            r = self._new_enum(ns, method, list(self.polling.items()), int(f.get("range") or 1))
            return r
        if method == "get_pollingbox_document_information":
            e = self.enums.get(f.get("enumeration"))
            if e is None:
                return self._resp(ns, method, "<k:result>INVALID_VALUE_ERROR</k:result>")
            if self.polling_preparing > 0:
                self.polling_preparing -= 1
                return self._resp(ns, method, "<k:result>PREPARING_NOW</k:result>")
            batch, e["items"] = e["items"][:e["range"]], e["items"][e["range"]:]
            res = "ALL_GET_COMPLETE" if not e["items"] else "SUCCESS"
            # 研究文件：name / page_count / register_date 在 common 底下；日期是 DATE_TIME_ENTRY（year 從 1900 起、month 從 0 起）
            tm = ("<k:second>0</k:second><k:minute>48</k:minute><k:hour>17</k:hour><k:day>20</k:day>"
                  "<k:month>9</k:month><k:year>126</k:year><k:wday>2</k:wday><k:yday>292</k:yday>"
                  "<k:is_dst>0</k:is_dst><k:time_zone>0</k:time_zone>")
            inner = "".join(f"<k:document_information><k:common><k:name>{escape(nm)}</k:name><k:page_count>1</k:page_count>"
                            f"<k:register_date>{tm}</k:register_date></k:common><k:data_size>{len(data)}</k:data_size>"
                            f"<k:form>TIFF</k:form><k:final_modify_date>{tm}</k:final_modify_date></k:document_information>"
                            for nm, data in batch)
            return self._resp(ns, method, inner + f"<k:result>{res}</k:result>")
        if method in ("destroy_box_enumeration", "destroy_document_enumeration", "destroy_pollingbox_document_enumeration"):
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
    assert [(r["id"], r["label"], r["shared"]) for r in roots] == [("1", "1 會計", True), ("2", "2 個人", False),
                                                                ("polling", "polling", True)], "最後是遙控資料盒（v1.17.4）"
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
    assert [r["id"] for r in s.roots()] == ["1", "2", "polling"], "設備沒有要求登入就不登入"
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
        # v1.17.0 非票證登入（startSession）：協定版本、登入模式候選（None＝不回這個屬性）、接受哪些登入
        self.protocol = "3"
        self.candidates = None
        self.start_accept = {"none"}          # none / BASIC / RGWUCD:<碼> / RGWEXTUCD:<碼>
        self.start_status = "OK"
        self.start_fault = None
        self.start_inputs: list[str] = []
        # v1.17.4 文件伺服器的資料夾（研究文件 2026-10-20）：fid → 名稱、有沒有密碼、密碼、裡面的文件。
        # 共用資料夾 `fid=0` 一律列出；它的文件就是 `self.docs`（`fromFolderId` 空字串查到的那些）
        self.folders: dict = {}
        self.folder_fault = False
        self.has_password_prop = True          # False：資料夾屬性不回 nf:passwordProtected（不知道有沒有密碼）

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
            return self._env(method, f'<return xsi:type="xsd:int">{self.protocol}</return>')
        if method == "getServiceCapability":
            host = self.auth_url_host or "127.0.0.1"
            props = [_ritem("uauthenticationUrl", f"http://{host}:{self.port}/DH/uauthentication"),
                     _ritem("uauthenticationProviderName", "LocalProvider"), _ritem("uauthenticationDomainName", ""),
                     _ritem("isUserAuthenticatableByTicket", self.ticket_ok),
                     _ritem("maxResultRowBlockSizeForDocIDList", "2"), _ritem("machineType", "IM C3010")]
            if self.candidates is not None:
                props.append(_ritem("startSessionSchemeCandidates", self.candidates))
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
        if method == "startSession":
            si = val.get("stringIn", "")
            self.start_inputs.append(si)
            if self.start_fault:
                return self._fault(self.start_fault)
            if self._start_ok(si):
                return self._env(method, f'<return xsi:type="xsd:string">{self.start_status}</return>'
                                         f'<stringOut xsi:type="xsd:string">{self.sid}</stringOut>')
            return self._env(method, '<return xsi:type="xsd:string">COMMON_NOT_AUTHENTICATED</return>'
                                     '<stringOut xsi:type="xsd:string"></stringOut>')
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
            if val["deepSearch"] != "false":
                return self._fault("COMMON_INVALID_ARGUMENT")
            where = [{_local(k.tag): (k.text or "") for k in it} for it in params["whereAnd"]]
            if val["from"] == "folder":
                if self.folder_fault:
                    return self._fault("COMMON_INVALID_ARGUMENT")
                if where != [{"operator": "RPQO_CONTAINS_ALL", "propName": "folderKind",
                              "propVal": "shrFolder+usrFolder", "propVal2": ""}]:
                    return self._fault("COMMON_INVALID_ARGUMENT")
                ids = ["fid=0"] + list(self.folders)
            elif val["fromFolderId"] == "":
                if where:
                    return self._fault("COMMON_INVALID_ARGUMENT")
                ids = list(self.docs)
            else:
                f = self.folders.get(val["fromFolderId"])
                if f is None:
                    return self._fault("REPOSITORY_BAD_OBJECT_ID")
                if where != [{"operator": "RPQO_CONTAINS_ALL", "propName": "application", "propVal": "scan",
                              "propVal2": ""}]:
                    return self._fault("COMMON_INVALID_ARGUMENT")
                if f["locked"] and val["fromFolderId"] not in self.checked:
                    return self._fault("REPOSITORY_NEED_PASSWORD")
                ids = list(f["docs"])
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
        if method == "getObjectsProps":
            ids = [it.text or "" for it in params["objectIds"]]
            sel = [it.text or "" for it in params["select"]]
            if sel != ["identifier", "title", "createdDate", "modifiedDate"]:
                return self._fault("COMMON_INVALID_ARGUMENT")
            rows = [f'<item {_rarray([_ritem("identifier", i), _ritem("createdDate", "2026-10-20T09:30:00+08:00"), _ritem("modifiedDate", "2026-10-20T10:15:00+08:00")])}</item>'
                    for i in ids if i in self._all()]
            return self._env(method, f'<return {_rarray(rows, "ct:property[]")}</return>')
        if method == "getProps" and (val["objectId"] in self.folders or val["objectId"] == "fid=0"):
            f = self.folders.get(val["objectId"]) or {"title": "共用資料夾", "locked": False, "docs": self.docs}
            other = [_ritem("rgw:docCount", str(len(f["docs"])))]
            if self.has_password_prop:
                other.append(_ritem("nf:passwordProtected", "true" if f["locked"] else "false"))
            props = [_ritem("identifier", val["objectId"]), _ritem("title", f["title"])]
            props.append(f'<item xsi:type="ct:property"><propName>otherProps</propName><propVal {_rarray(other)}</propVal></item>')
            return self._env(method, f'<return {_rarray(props)}</return>')
        if method == "getProps":
            d = self._all().get(val["objectId"])
            if d is None:
                return self._fault("REPOSITORY_BAD_OBJECT_ID")
            other = [_ritem("nf:application", "scan"), _ritem("nf:passwordProtected", "true" if d["locked"] else "false")]
            props = [_ritem("identifier", val["objectId"]), _ritem("title", d["title"]),
                     _ritem("contentLength", "1234"), _ritem("modifiedDate", "2026-10-15T01:02:03Z")]
            props.append(f'<item xsi:type="ct:property"><propName>otherProps</propName><propVal {_rarray(other)}</propVal></item>')
            return self._env(method, f'<return {_rarray(props)}</return>')
        if method == "checkObjectPassword":
            f = self.folders.get(val["objectId"])
            ok = val["password"] == (f["password"] if f else self.password)
            if ok:
                self.checked.add(val["objectId"])
            return self._env(method, f'<return xsi:type="xsd:boolean">{"true" if ok else "false"}</return>')
        if method == "getDocRendition":
            d = self._all().get(val["docId"])
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
            els = self._all()[doc]["elements"]
            start = int(val["startIndex"])
            batch = els[start:start + 1]           # 一次一個：驗 TRUNCATED 的續讀
            status = "TRUNCATED" if start + 1 < len(els) else "SUCCESS"
            lists = [f'<item {_rarray([_ritem("identifier", e)])}</item>' for e in batch]
            return self._env(method, f'<return xsi:type="xsd:string">{status}</return>'
                                     f'<propLists {_rarray(lists, "ct:property[]")}</propLists>'
                                     f'<numOfAllElements>{len(els)}</numOfAllElements>')
        if method in ("getDocContentUrl", "getDocContentUrl2"):
            eid = val["docId"]
            doc = next((k for k, d in self._all().items() if eid in d["elements"] or eid == "R-" + k), None)
            d = self._all()[doc]
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

    def _all(self) -> dict:
        out = dict(self.docs)
        for f in self.folders.values():
            out.update(f["docs"])
        return out

    def _start_ok(self, si: str) -> bool:
        """照分析文件的格式解 stringIn（假伺服器自己的獨立實作，不呼叫被測的程式）。"""
        if si == "":
            return "none" in self.start_accept
        f = dict(x.split("=", 1) for x in si.split(";") if x)
        if not si.endswith(";"):
            return False
        scheme = base64.b64decode(f.get("SCHEME", "")).decode()
        if scheme in ("RGWUCD", "RGWEXTUCD"):
            return f"{scheme}:{base64.b64decode(f['UCD:UserCode']).decode()}" in self.start_accept
        if scheme != "BASIC" or "BASIC" not in self.start_accept:
            return False
        user = base64.b64decode(f["UID:UserName"]).decode()
        raw = base64.b64decode(f["PWD:Password"])
        if f["PES:Encoding"] == "gwpwes003":
            plain = bytes((((b ^ 0x72) << 2) | ((b ^ 0x72) >> 6)) & 0xFF for b in raw)
            if len(plain) < 16 or plain[15:16] != b"z":
                return False
            pw = plain[16:].decode()
        elif f["PES:Encoding"] == "":
            pw = raw.decode()
        else:
            return False
        return self.users.get(user) == pw


@pytest.fixture
def ricoh(monkeypatch):
    real = mc.check_address
    monkeypatch.setattr(mc, "check_address", lambda ip: None if ip == "127.0.0.1" else real(ip))
    f = FakeRicoh().start()
    yield f
    f.stop()


def _ricoh_session(f, trace, user="clerk", password="pw-2", doc_password="", **kw):
    return K.RicohSession("ricoh_soap", "127.0.0.1", "127.0.0.1", "http", f.port, user=user, password=password,
                          doc_password=doc_password, trust_sha256=None, allow_http=True, trace=trace, **kw)


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
    ricoh.classes = ["basic", "query.advancedSearch", "rendition"]          # 兩種登入都沒有
    with pytest.raises(mfp_box.BoxError) as ei:
        _ricoh_session(ricoh, [])
    assert ei.value.code == "RICOH_AUTH_UNSUPPORTED"
    # 畫面只列登入方式，不倒整串功能類別
    assert ei.value.detail["login"] == ["-"] and "classes" not in ei.value.detail
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


# =========================================================================== RICOH 非票證登入（v1.17.0）
# 依 2026-10-19 的分析（內部文件，ricoh-startsession）：`startSession(stringIn, timeLimit)`，第一個結果是狀態
# （成功 `OK`），`stringOut` 才是工作階段代號。客戶實機（IM C3500，面板沒有登入畫面）只列 `basic.startSession`。

_CUSTOMER_CLASSES = ["basic.checkServiceState", "basic.startSession", "basic.keepSession", "basic.putProps",
                     "basic.getObjectsProps", "basic.delete", "basic.getDocContentUrl2", "contentElement",
                     "contentElement.insertEmptyDocElement", "rendition", "rendition.getDocRenditionList", "query",
                     "query.advancedSearch", "ticket", "objectPassword", "objectPassword.setObjectPassword",
                     "lock", "rgw", "rgw.setJobPropsOfGetDocContents", "rgw.cancelJob"]


def _starts(f):
    return [c for c in f.calls if c["method"] == "startSession"]


@pytest.mark.parametrize("mode,kw,want", [
    ("none", {}, ""),
    ("BASIC", {"user": "demo", "password": "Demo-Pass123", "transport": "https"},
     "SCHEME=QkFTSUM=;UID:UserName=ZGVtbw==;PWD:Password=RGVtby1QYXNzMTIz;PES:Encoding=;"),
    ("RGWUCD", {"user_code": "00123456"}, "SCHEME=UkdXVUNE;UCD:UserCode=MDAxMjM0NTY=;"),
    ("RGWEXTUCD", {"user_code": "00123456"}, "SCHEME=UkdXRVhUVUNE;UCD:UserCode=MDAxMjM0NTY=;"),
    ("RGWUCD", {"user_code": "00123456", "protocol": 199}, "SCHEME=QkFTSUM=;UID=;PWD=MDAxMjM0NTY=;"),
])
def test_ricoh_string_in_matches_the_analysis(mode, kw, want):
    kw = {"protocol": 200, "transport": "http", **kw}
    assert K.ricoh_string_in(mode, **kw) == want


def test_ricoh_string_in_keeps_delimiters_inside_base64():
    s = K.ricoh_string_in("BASIC", protocol=200, transport="https", user="測試;:=", password="密碼 &<>")
    assert base64.b64encode("測試;:=".encode()).decode() in s and s.count(";") == 4


@pytest.mark.parametrize("mode,kw", [("BASIC", {"user": "demo", "password": "x", "protocol": 199}),
                                     ("RGWEXTUCD", {"user_code": "1", "protocol": 199}),
                                     ("BASIC", {"user": "", "password": "x"}),
                                     ("RGWUCD", {"user_code": "12a"}), ("RGWUCD", {"user_code": ""}),
                                     ("auto", {})])
def test_ricoh_string_in_refuses_unconfirmed_forms(mode, kw):
    with pytest.raises(mfp_box.BoxError):
        K.ricoh_string_in(mode, **{"protocol": 200, "transport": "https", **kw})


@pytest.mark.parametrize("pw,prefix,hexout", [
    ("Demo-Pass123", b"a" * 15, "2a2a2a2a2a2a2a2a2a2a2a2a2a2a2aec632b29a939662aaeae3efebe"),
    ("Demo-Pass123", b"J" * 15, "e0e0e0e0e0e0e0e0e0e0e0e0e0e0e0ec632b29a939662aaeae3efebe"),
    ("測試密碼", b"a" * 15, "2a2a2a2a2a2a2a2a2a2a2a2a2a2a2aeccb5c594818db0b99d38bda5d"),
])
def test_ricoh_http_password_representation_matches_vendor_vectors(pw, prefix, hexout):
    """分析文件的離線向量（原廠函式在固定亂數下的輸出）。"""
    assert K.ricoh_gwpwes003(pw, prefix).hex() == hexout


@pytest.mark.parametrize("pw", ["ɀ", "a\0b", "x" * 129])
def test_ricoh_http_password_that_cannot_be_represented_is_not_sent(pw):
    with pytest.raises(mfp_box.BoxError) as ei:
        K.ricoh_gwpwes003(pw, b"a" * 15)
    assert ei.value.code == "RICOH_PASSWORD_UNSUPPORTED"


def test_ricoh_random_prefix_is_fresh_each_time():
    a, b = K.ricoh_gwpwes003("Demo-Pass123"), K.ricoh_gwpwes003("Demo-Pass123")
    assert a != b and a[15:] == b[15:]


def test_ricoh_auto_with_no_login_data_uses_no_sign_in_once(ricoh):
    """v1.17.3（客戶：「自動判斷不夠自動」）：自動判斷、帳號與 User Code 都空白 → 用「不需登入」送一次，
    不送任何帳密；標出是自動改用的（畫面要照實說）。"""
    ricoh.classes = _CUSTOMER_CLASSES
    s = _ricoh_session(ricoh, [], user="", password="")
    st = _starts(ricoh)
    assert len(st) == 1 and st[0]["params"]["stringIn"] == "", "不需登入：stringIn 是空的，沒有帳密"
    assert s.auth_mode == "none" and s.auto_fallback_none is True


@pytest.mark.parametrize("how", ["fault", "status"])
def test_ricoh_auto_no_sign_in_rejected_asks_for_an_account_without_retrying(ricoh, how):
    """帳號空白不等於免登入（分析文件 5.2 第 8 條）：設備拒絕時請使用者填帳號或 User Code，**不換別的方式再試**。"""
    ricoh.classes = _CUSTOMER_CLASSES
    if how == "fault":
        ricoh.start_fault = "COMMON_NOT_AUTHENTICATED"
    else:
        ricoh.start_status = "NG"
    with pytest.raises(mfp_box.BoxError) as ei:
        _ricoh_session(ricoh, [], user="", password="")
    assert ei.value.code == "AUTH_REQUIRED_USER", ei.value.code
    assert len(_starts(ricoh)) == 1, "只送一次"
    assert not [c for c in ricoh.calls if c["method"] == "authenticateByPassword"]


def test_ricoh_explicit_no_sign_in_rejected_keeps_its_own_error(ricoh):
    """使用者自己選「不需登入」被拒：照舊回 RICOH_SESSION_REJECTED（不是自動判斷的提示）。"""
    ricoh.classes = _CUSTOMER_CLASSES
    ricoh.start_fault = "COMMON_NOT_AUTHENTICATED"
    with pytest.raises(mfp_box.BoxError) as ei:
        _ricoh_session(ricoh, [], user="", password="", auth_mode="none")
    assert ei.value.code == "RICOH_SESSION_REJECTED"


def test_ricoh_no_login_mode_lists_and_downloads(ricoh, tmp_path):
    ricoh.classes = _CUSTOMER_CLASSES
    trace: list = []
    s = _ricoh_session(ricoh, trace, user="", password="", auth_mode="none")
    st = _starts(ricoh)
    assert len(st) == 1 and st[0]["params"] == {"stringIn": "", "timeLimit": "1800"}
    assert st[0]["types"] == {"stringIn": "xsd:string", "timeLimit": "xsd:unsignedInt"}
    assert st[0]["action"] == f'"{K.RICOH_REPO_NS}#startSession"'
    assert s._sid == ricoh.sid, "工作階段代號取 stringOut，不是狀態 OK"
    names = [e["label"] for e in s.list("documents", [])]
    assert "報價" in names
    seg = next(e["name"] for e in s.list("documents", []) if e["label"] == "報價")
    out = tmp_path / "a.bin"
    s.download("documents", [seg], out, 10 ** 8)
    url2 = [c for c in ricoh.calls if c["method"] == "getDocContentUrl2"]
    assert url2 and not [c for c in ricoh.calls if c["method"] == "getDocContentUrl"], "有 URL2 能力就用 URL2"
    assert url2[0]["params"]["validDurationHint"] == "0" and b"http+post" in url2[0]["body"]
    assert b"https+get" not in url2[0]["body"] and b"http+get" not in url2[0]["body"]
    assert url2[0]["types"]["options"] == "enc:Array", "options 送空陣列，不省略"
    s.close()
    assert ricoh.terminated, "沒有列 terminateSession 也照原廠流程結束工作階段"
    assert next(t for t in trace if t.get("login_mode"))["login_mode"] == "none"


def test_ricoh_no_login_mode_refuses_to_discard_typed_credentials(ricoh):
    ricoh.classes = _CUSTOMER_CLASSES
    for kw in ({"user": "clerk"}, {"user": "", "password": "x"}, {"user": "", "password": "", "user_code": "1"}):
        with pytest.raises(mfp_box.BoxError) as ei:
            _ricoh_session(ricoh, [], auth_mode="none", **{"password": "", **kw})
        assert ei.value.code == "RICOH_AUTH_INPUT_CONFLICT"
    assert not _starts(ricoh)


def test_ricoh_account_without_ticket_uses_basic_over_http_once(ricoh):
    ricoh.classes, ricoh.protocol, ricoh.start_accept = _CUSTOMER_CLASSES, "200", {"BASIC"}
    trace: list = []
    s = _ricoh_session(ricoh, trace, auth_mode="account")
    st = _starts(ricoh)
    assert len(st) == 1 and "PES:Encoding=gwpwes003;" in st[0]["params"]["stringIn"]
    assert not [c for c in ricoh.calls if c["method"] == "authenticateByPassword"]
    blob = json.dumps(trace, ensure_ascii=False)
    assert "pw-2" not in blob and st[0]["params"]["stringIn"] not in blob and "UID:UserName" not in blob, \
        "協定紀錄不可以有密碼或登入字串"
    s.close()


def test_ricoh_wrong_basic_password_is_sent_once(ricoh):
    ricoh.classes, ricoh.protocol, ricoh.start_accept = _CUSTOMER_CLASSES, "200", {"BASIC"}
    with pytest.raises(mfp_box.BoxError) as ei:
        _ricoh_session(ricoh, [], password="wrong", auth_mode="account")
    assert ei.value.code == "RICOH_SESSION_REJECTED" and len(_starts(ricoh)) == 1
    assert ei.value.detail["status"] == "COMMON_NOT_AUTHENTICATED"
    ricoh.calls.clear()
    ricoh.start_fault = "COMMON_NOT_AUTHENTICATED"
    with pytest.raises(mfp_box.BoxError) as ei:
        _ricoh_session(ricoh, [], auth_mode="account")
    assert ei.value.code == "RICOH_SESSION_REJECTED" and len(_starts(ricoh)) == 1


def test_ricoh_basic_needs_protocol_200(ricoh):
    ricoh.classes = _CUSTOMER_CLASSES          # 協定版本 3
    with pytest.raises(mfp_box.BoxError) as ei:
        _ricoh_session(ricoh, [], auth_mode="account")
    assert ei.value.code == "RICOH_AUTH_PROFILE_UNSUPPORTED" and not _starts(ricoh)


def test_ricoh_ok_without_session_id_is_not_a_session(ricoh):
    ricoh.classes = _CUSTOMER_CLASSES
    ricoh.sid = ""
    with pytest.raises(mfp_box.BoxError) as ei:
        _ricoh_session(ricoh, [], user="", password="", auth_mode="none")
    assert ei.value.code == "DAV_BAD_RESPONSE"


def test_ricoh_user_code_type_comes_from_the_device_or_the_user(ricoh):
    ricoh.classes, ricoh.protocol = _CUSTOMER_CLASSES, "200"
    ricoh.start_accept = {"RGWUCD:0042"}
    ricoh.candidates = "BASIC,RGWUCD"
    s = _ricoh_session(ricoh, [], user="", password="", user_code="0042")          # 自動：只宣告一種
    assert _starts(ricoh)[-1]["params"]["stringIn"] == "SCHEME=UkdXVUNE;UCD:UserCode=MDA0Mg==;"
    s.close()
    ricoh.calls.clear()
    ricoh.candidates = "RGWUCD,RGWEXTUCD"
    with pytest.raises(mfp_box.BoxError) as ei:
        _ricoh_session(ricoh, [], user="", password="", user_code="0042")
    assert ei.value.code == "RICOH_AUTH_MODE_REQUIRED" and not _starts(ricoh), "兩種都有：不拿同一個碼試兩次"
    ricoh.candidates = "BASIC,RGWUCD"
    with pytest.raises(mfp_box.BoxError) as ei:
        _ricoh_session(ricoh, [], user="", password="", user_code="0042", auth_mode="user_code_ext")
    assert ei.value.code == "RICOH_AUTH_MODE_MISMATCH" and not _starts(ricoh)


def _with_folders(ricoh):
    """照客戶 IM C3510 的樣子（研究文件 2026-10-20）：共用資料夾＋001（有密碼）＋002（沒密碼）。"""
    ricoh.classes = _CUSTOMER_CLASSES
    ricoh.folders = {
        "fid=1": {"title": "001", "locked": True, "password": "001234",
                  "docs": {"f1-doc": {"title": "請款單", "elements": ["e9"], "scheme": "http+get", "locked": False}}},
        "fid=2": {"title": "002", "locked": False, "password": "",
                  "docs": {"f2-doc": {"title": "出差單", "elements": ["e8"], "scheme": "http+get", "locked": False}}},
    }


def _calls(ricoh, method):
    return [c for c in ricoh.calls if c["method"] == method]


def test_ricoh_lists_folders_besides_the_shared_one(ricoh):
    """客戶實測：共用資料夾有文件時，001、002 也要列出來 —— 資料夾另外查（`from="folder"`），不是文件查詢順便帶出。"""
    _with_folders(ricoh)
    s = _ricoh_session(ricoh, [], user="", password="", auth_mode="none")
    roots = s.roots()
    assert [r["id"] for r in roots] == ["documents", K.enc_name("fid=1"), K.enc_name("fid=2")]
    assert [r["label"] for r in roots[1:]] == ["001 001", "002 002"]
    assert [r.get("locked") for r in roots[1:]] == [True, False]
    q = [c for c in _calls(ricoh, "executeAdvancedSearch") if c["params"]["from"] == "folder"]
    # 假設備一批 2 筆、3 個資料夾 → 分兩批，位移照實際取回的列數往前
    assert [c["params"]["rowOffset"] for c in q] == ["0", "2"]
    assert all(c["params"]["fromFolderId"] == "" and c["params"]["deepSearch"] == "false" for c in q)
    assert {e["label"] for e in s.list("documents", [])} == {"合約", "報價", "會議紀錄"}, "共用資料夾照舊"
    assert not [c for c in _calls(ricoh, "executeAdvancedSearch") if c["params"]["deepSearch"] != "false"]


def test_ricoh_opens_a_folder_without_password_directly(ricoh):
    _with_folders(ricoh)
    s = _ricoh_session(ricoh, [], user="", password="", auth_mode="none")
    s.roots()
    assert [e["label"] for e in s.list(K.enc_name("fid=2"), [])] == ["出差單"]
    assert not _calls(ricoh, "checkObjectPassword"), "沒有密碼的資料夾不先用空密碼驗證"
    q = [c for c in _calls(ricoh, "executeAdvancedSearch") if c["params"]["fromFolderId"] == "fid=2"]
    assert len(q) == 1, "用設備回傳的識別碼查，不是顯示的 002"


def test_ricoh_locked_folder_needs_the_password_and_sends_nothing_without_it(ricoh):
    _with_folders(ricoh)
    s = _ricoh_session(ricoh, [], user="", password="", auth_mode="none")
    s.roots()
    with pytest.raises(mfp_box.BoxError) as ei:
        s.list(K.enc_name("fid=1"), [])
    assert ei.value.code == "FOLDER_PASSWORD_REQUIRED"
    assert not _calls(ricoh, "checkObjectPassword")
    assert not [c for c in _calls(ricoh, "executeAdvancedSearch") if c["params"]["fromFolderId"] == "fid=1"]


def test_ricoh_wrong_folder_password_is_sent_once_and_stops(ricoh):
    _with_folders(ricoh)
    s = _ricoh_session(ricoh, [], user="", password="", auth_mode="none", folder_password="1234")
    s.roots()
    with pytest.raises(mfp_box.BoxError) as ei:
        s.list(K.enc_name("fid=1"), [])
    assert ei.value.code == "FOLDER_PASSWORD_FAILED"
    assert len(_calls(ricoh, "checkObjectPassword")) == 1, "只送一次"
    assert not [c for c in _calls(ricoh, "executeAdvancedSearch") if c["params"]["fromFolderId"] == "fid=1"]


def test_ricoh_folder_password_keeps_leading_zeroes_and_unlocks_listing_and_download(ricoh, tmp_path):
    """原字串送出（不加 gwpwes003、不轉數字）；同一個工作階段只驗一次；取件的新工作階段自己再驗一次。"""
    _with_folders(ricoh)
    s = _ricoh_session(ricoh, [], user="", password="", auth_mode="none", folder_password="001234")
    s.roots()
    entries = s.list(K.enc_name("fid=1"), [])
    assert [e["label"] for e in entries] == ["請款單"]
    s.list(K.enc_name("fid=1"), [])
    chk = _calls(ricoh, "checkObjectPassword")
    assert len(chk) == 1 and chk[0]["params"]["password"] == "001234" and chk[0]["params"]["objectId"] == "fid=1"
    assert chk[0]["types"]["password"] == "xsd:string"
    s.close()
    ricoh.calls.clear()
    ricoh.checked.clear()
    s2 = _ricoh_session(ricoh, [], user="", password="", auth_mode="none", folder_password="001234")
    res = s2.download(K.enc_name("fid=1"), [entries[0]["name"]], tmp_path / "f1.bin", 10 * 1024 * 1024)
    assert res["bytes"] > 0 and len(_calls(ricoh, "checkObjectPassword")) == 1
    s2.close()


def test_ricoh_unknown_protection_is_not_treated_as_unlocked(ricoh):
    """資料夾屬性沒說有沒有密碼：不當成沒密碼。直接查、設備說要密碼 → 請使用者輸入資料夾密碼。"""
    _with_folders(ricoh)
    ricoh.has_password_prop = False
    s = _ricoh_session(ricoh, [], user="", password="", auth_mode="none")
    roots = s.roots()
    assert roots[1]["locked"] is None
    with pytest.raises(mfp_box.BoxError) as ei:
        s.list(K.enc_name("fid=1"), [])
    assert ei.value.code == "FOLDER_PASSWORD_REQUIRED"


def test_ricoh_a_refused_folder_query_keeps_the_shared_folder_and_says_so(ricoh):
    """資料夾清單讀不到不是「沒有資料夾」：共用資料夾照樣列，另外記下原因給畫面。"""
    _with_folders(ricoh)
    ricoh.folder_fault = True
    s = _ricoh_session(ricoh, [], user="", password="", auth_mode="none")
    assert [r["id"] for r in s.roots()] == ["documents"]
    assert s.folder_error == "SOAP_FAULT"


def test_ricoh_dates_come_from_the_batch_properties(ricoh):
    """研究文件第 6 節：`getObjectsProps` 選 createdDate、modifiedDate，依 identifier 對回去。"""
    _with_folders(ricoh)
    s = _ricoh_session(ricoh, [], user="", password="", auth_mode="none")
    s.list(K.enc_name("fid=2"), [])
    g = _calls(ricoh, "getObjectsProps")
    assert len(g) == 1 and g[0]["types"]["objectIds"] == "enc:Array"


def test_ricoh_records_property_names_for_the_first_document(ricoh):
    """實機的日期 / 大小欄位名稱不一定跟預期一樣：協定紀錄記下設備回的屬性名稱（不記內容）。"""
    ricoh.classes = _CUSTOMER_CLASSES
    trace: list = []
    s = _ricoh_session(ricoh, trace, user="", password="", auth_mode="none")
    entries = s.list("documents", [])
    names = next(t for t in trace if t.get("prop_names"))
    assert "title" in names["prop_names"] and "nf:application" in names["other_prop_names"]
    assert all(e["size"] == 1234 and e["modified"] for e in entries)
    assert "合約" not in json.dumps(names, ensure_ascii=False), "只記名稱，不記內容"
    s.close()



# ------------------------------------------------------------------ KYOCERA 遙控資料盒（v1.17.4）

def test_kyocera_polling_box_is_listed_with_its_own_methods(kyo):
    """客戶 ECOSYS M6535cidn 沒有自訂資料盒，掃描存在遙控資料盒：位置清單多一個、列文件用專用方法、不帶匣號。"""
    from datetime import datetime
    kyo.polling_preparing = 2
    s = _kyo_session(kyo, [])
    roots = s.roots()
    assert roots[-1]["id"] == "polling" and roots[-1]["readonly"] is True
    docs = s.list("polling", [])
    assert [d["label"] for d in docs] == ["doc00211620261020"] and docs[0]["pages"] == 1
    assert datetime.fromtimestamp(docs[0]["modified"]).strftime("%Y-%m-%d %H:%M") == "2026-10-20 17:48", \
        "year+1900、month+1"
    methods = [c["method"] for c in kyo.calls]
    assert "create_pollingbox_document_enumeration" in methods
    assert methods.count("get_pollingbox_document_information") == 3, "準備中等一下再讀同一個列舉"
    assert "destroy_pollingbox_document_enumeration" in methods, "一定釋放列舉"
    s.close()


def test_kyocera_polling_box_download_is_not_sent(kyo, tmp_path):
    """匣號還沒有原廠依據：不送 prepare_downloading_data（不猜值）。"""
    s = _kyo_session(kyo, [])
    seg = s.list("polling", [])[0]["name"]
    with pytest.raises(mfp_box.BoxError) as ei:
        s.download("polling", [seg], tmp_path / "x.bin", 10 * 1024 * 1024)
    assert ei.value.code == "KYOCERA_POLLING_DOWNLOAD_UNCONFIRMED"
    assert not [c for c in kyo.calls if c["method"] == "prepare_downloading_data"]
    s.close()


def test_kyocera_polling_box_still_preparing_is_not_an_empty_list(kyo, monkeypatch):
    monkeypatch.setattr(K, "KYO_PREPARE_LIMIT", 0.5)
    kyo.polling_preparing = 100
    s = _kyo_session(kyo, [])
    with pytest.raises(mfp_box.BoxError) as ei:
        s.list("polling", [])
    assert ei.value.code == "SOAP_BUSY"
    assert "destroy_pollingbox_document_enumeration" in [c["method"] for c in kyo.calls]
    s.close()
