"""事務機掃描（eSCL）：能力表、掃描設定、整個掃描流程（對一台假的 eSCL 設備）。

使用者 2026-10-14：小型事務機沒有放掃描檔的信箱，只能由電腦端發起掃描 —— 加「從事務機掃描，直接進工作區」。

* 能力表用 defusedxml 讀：外部實體、大量展開的實體一律當成格式不對（設備的回應可能被竄改）。
* 掃描設定只收能力表裡有的值；XML 只由固定字串與整數組成。
* 流程：狀態不是閒置就等、送紙器沒紙直接講、503 等一下再取、404 是沒有下一頁；
  回的不是 PDF / JPEG、太大、被取消、要登入 → 停下來並**把設備上的掃描工作刪掉**。
* 不送帳密、Cookie，也不送 `Connection: close`（HP 小型機看到它會把回應切在 8 KB）。
* `Location` 只取路徑；指到別台主機或不是 `/eSCL/ScanJobs/<id>` 的一律拒絕。
"""
from __future__ import annotations

import io
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.core import mfp_client as mc
from app.core import mfp_escl as E

NS = 'xmlns:scan="http://schemas.hp.com/imaging/escl/2011/05/03" xmlns:pwg="http://www.pwg.org/schemas/2010/12/sm"'


def _input_caps(formats=("image/jpeg", "application/pdf"), colors=("RGB24", "Grayscale8"),
                res=(200, 300, 600)) -> str:
    return (
        "<scan:MinWidth>300</scan:MinWidth><scan:MaxWidth>2550</scan:MaxWidth>"
        "<scan:MinHeight>300</scan:MinHeight><scan:MaxHeight>3508</scan:MaxHeight>"
        "<scan:SettingProfiles><scan:SettingProfile><scan:ColorModes>"
        + "".join(f"<scan:ColorMode>{c}</scan:ColorMode>" for c in colors)
        + "</scan:ColorModes><scan:DocumentFormats>"
        + "".join(f"<pwg:DocumentFormat>{f}</pwg:DocumentFormat><scan:DocumentFormatExt>{f}</scan:DocumentFormatExt>"
                  for f in formats)
        + "</scan:DocumentFormats><scan:SupportedResolutions><scan:DiscreteResolutions>"
        + "".join(f"<scan:DiscreteResolution><scan:XResolution>{r}</scan:XResolution>"
                  f"<scan:YResolution>{r}</scan:YResolution></scan:DiscreteResolution>" for r in res)
        + "</scan:DiscreteResolutions></scan:SupportedResolutions></scan:SettingProfile></scan:SettingProfiles>"
        "<scan:SupportedIntents><scan:Intent>Document</scan:Intent><scan:Intent>Photo</scan:Intent>"
        "</scan:SupportedIntents>")


def caps_xml(adf=False, duplex=False, **kw) -> bytes:
    body = f"<scan:Platen><scan:PlatenInputCaps>{_input_caps(**kw)}</scan:PlatenInputCaps></scan:Platen>"
    if adf:
        body += f"<scan:Adf><scan:AdfSimplexInputCaps>{_input_caps(**kw)}</scan:AdfSimplexInputCaps>"
        if duplex:
            body += f"<scan:AdfDuplexInputCaps>{_input_caps(**kw)}</scan:AdfDuplexInputCaps>"
        body += "</scan:Adf>"
    return (f'<?xml version="1.0" encoding="UTF-8"?><scan:ScannerCapabilities {NS}>'
            "<pwg:Version>2.63</pwg:Version><pwg:MakeAndModel>Fake LaserJet MFP M1</pwg:MakeAndModel>"
            f"<scan:Manufacturer>Fake</scan:Manufacturer>{body}</scan:ScannerCapabilities>").encode()


def status_xml(state="Idle", adf="") -> bytes:
    a = f"<scan:AdfState>{adf}</scan:AdfState>" if adf else ""
    return (f'<?xml version="1.0" encoding="UTF-8"?><scan:ScannerStatus {NS}>'
            f"<pwg:Version>2.63</pwg:Version><pwg:State>{state}</pwg:State>{a}</scan:ScannerStatus>").encode()


def pdf_bytes(pages=1) -> bytes:
    import fitz
    d = fitz.open()
    for i in range(pages):
        d.new_page(width=595, height=842).insert_text((72, 72), f"page {i + 1}")
    b = d.tobytes()
    d.close()
    return b


def jpeg_bytes(w=2480, h=3508) -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (w // 8, h // 8), "white").resize((w, h)).save(buf, "JPEG", quality=30)
    return buf.getvalue()


class Fake:
    """可設定的假 eSCL 設備。"""

    def __init__(self):
        self.caps = caps_xml()
        self.states = ["Idle"]
        self.adf = ""
        self.post_status = 201
        self.location = None            # None → 預設的 /eSCL/ScanJobs/job1
        self.docs: list = []            # 依序回的東西：bytes（200）、int（狀態碼）
        self.doc_type = "application/pdf"
        self.requests: list[dict] = []

    def serve(self):
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _rec(self, body=b""):
                fake.requests.append({"method": self.command, "path": self.path,
                                      "headers": {k.lower(): v for k, v in self.headers.items()}, "body": body})

            def _send(self, code, body=b"", ctype="text/xml", headers=None):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                if body:
                    self.wfile.write(body)

            def do_GET(self):
                self._rec()
                if self.path == "/eSCL/ScannerCapabilities":
                    return self._send(200, fake.caps)
                if self.path == "/eSCL/ScannerStatus":
                    st = fake.states.pop(0) if len(fake.states) > 1 else fake.states[0]
                    return self._send(200, status_xml(st, fake.adf))
                if self.path.endswith("/NextDocument"):
                    if not fake.docs:
                        return self._send(404)
                    d = fake.docs.pop(0)
                    if isinstance(d, int):
                        return self._send(d)
                    return self._send(200, d, fake.doc_type)
                return self._send(404)

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                self._rec(self.rfile.read(n))
                if self.path != "/eSCL/ScanJobs":
                    return self._send(404)
                loc = fake.location or f"http://127.0.0.1:{self.server.server_address[1]}/eSCL/ScanJobs/job1"
                return self._send(fake.post_status, headers={"Location": loc} if fake.post_status == 201 else {})

            def do_DELETE(self):
                self._rec()
                return self._send(200)

        srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return srv


@pytest.fixture
def fake(monkeypatch):
    real = mc.check_address
    monkeypatch.setattr(mc, "check_address", lambda ip: None if ip == "127.0.0.1" else real(ip))
    monkeypatch.setattr(E, "STATUS_POLL", 0.05)
    monkeypatch.setattr(E, "PAGE_POLL", 0.05)
    f = Fake()
    srv = f.serve()
    port = srv.server_address[1]
    monkeypatch.setattr(mc, "ALLOWED_PORTS", tuple(mc.ALLOWED_PORTS) + (port,))
    f.port = port
    yield f
    srv.shutdown()


def _scan(f, tmp_path, opts=None, **kw):
    caps = E.parse_capabilities(f.caps)
    return E.scan("127.0.0.1", "127.0.0.1", "http", f.port, caps, opts or {"source": "platen"}, tmp_path / "w", **kw)


def _paths(f, method=None):
    return [r["path"] for r in f.requests if method is None or r["method"] == method]


# --------------------------------------------------------------------- 能力表

def test_capabilities_of_a_platen_only_device():
    c = E.parse_capabilities(caps_xml())
    assert c["make_and_model"] == "Fake LaserJet MFP M1" and c["version"] == "2.63"
    assert list(c["sources"]) == ["platen"]
    p = c["sources"]["platen"]
    assert p["color_modes"] == ["RGB24", "Grayscale8"] and p["resolutions"] == [200, 300, 600]
    assert "application/pdf" in p["formats"] and p["max_width"] == 2550
    s = E.summarize(c)
    assert s["sources"]["platen"]["colors"] == ["color", "gray"] and s["sources"]["platen"]["formats"] == ["pdf", "jpeg"]


def test_capabilities_with_a_duplex_feeder():
    c = E.parse_capabilities(caps_xml(adf=True, duplex=True))
    assert set(c["sources"]) == {"platen", "adf", "adf_duplex"}


@pytest.mark.parametrize("bad", [
    b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;">'
    b'<!ENTITY c "&b;&b;&b;&b;&b;&b;&b;&b;">]><scan:ScannerCapabilities>&c;</scan:ScannerCapabilities>',
    b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]><x>&e;</x>',
    b"<html><body>not xml",
    b'<?xml version="1.0"?><Other/>',
    b"",
])
def test_bad_or_hostile_capabilities_are_refused(bad):
    with pytest.raises(E.ScanError) as e:
        E.parse_capabilities(bad)
    assert e.value.code == "ESCL_BAD"


def test_oversized_capabilities_are_refused():
    with pytest.raises(E.ScanError):
        E.parse_capabilities(b"<a>" + b" " * (E.MAX_XML + 1) + b"</a>")


# --------------------------------------------------------------------- 掃描設定

def test_settings_use_only_values_from_the_capabilities():
    caps = E.parse_capabilities(caps_xml())
    xml, eff = E.build_settings(caps, {"source": "platen", "color": "gray", "resolution": "600", "paper": "a4"})
    s = xml.decode()
    assert "<pwg:InputSource>Platen</pwg:InputSource>" in s and "<scan:ColorMode>Grayscale8</scan:ColorMode>" in s
    assert "<scan:XResolution>600</scan:XResolution>" in s and "application/pdf" in s
    assert eff["width"] == 2480 and eff["height"] == 3508
    from defusedxml.ElementTree import fromstring
    fromstring(xml)                                   # 是合法的 XML


@pytest.mark.parametrize("opts,field", [
    ({"source": "adf"}, "source"), ({"source": "<x/>"}, "source"),
    ({"color": "bw"}, "color"), ({"color": "RGB24"}, "color"),
    ({"resolution": 1200}, "resolution"), ({"resolution": "300; drop"}, "resolution"),
    ({"paper": "b4"}, "paper"),
])
def test_settings_outside_the_capabilities_are_refused(opts, field):
    caps = E.parse_capabilities(caps_xml())
    with pytest.raises(E.ScanError) as e:
        E.build_settings(caps, opts)
    assert e.value.code == "SCAN_OPTION_INVALID" and e.value.detail["field"] == field


def test_letter_and_max_are_clamped_to_the_device():
    caps = E.parse_capabilities(caps_xml())
    _x, eff = E.build_settings(caps, {"paper": "max"})
    assert (eff["width"], eff["height"]) == (2550, 3508)


def test_jpeg_only_devices_scan_jpeg():
    caps = E.parse_capabilities(caps_xml(formats=("image/jpeg",)))
    xml, eff = E.build_settings(caps, {})
    assert eff["format"] == "jpeg" and b"image/jpeg" in xml


@pytest.mark.parametrize("loc,ok", [
    ("/eSCL/ScanJobs/abc-123", True),
    ("http://127.0.0.1/eSCL/ScanJobs/abc-123", True),
    ("https://127.0.0.1:443/eSCL/ScanJobs/abc-123/", True),
    ("http://10.9.9.9/eSCL/ScanJobs/abc", False),
    ("/eSCL/ScanJobs/../../admin", False),
    ("/admin/reboot", False),
    ("", False),
])
def test_job_location_must_be_on_the_same_device(loc, ok):
    dev = E._Device("127.0.0.1", "127.0.0.1", "http", 80)
    if ok:
        assert E._job_path(loc, dev).startswith("/eSCL/ScanJobs/")
    else:
        with pytest.raises(E.ScanError):
            E._job_path(loc, dev)


# --------------------------------------------------------------------- 掃描流程

def test_platen_scan_returns_a_pdf_and_sends_nothing_secret(fake, tmp_path):
    fake.docs = [pdf_bytes(1)]
    res = _scan(fake, tmp_path)
    assert res["pages"] == 1 and res["path"].read_bytes().startswith(b"%PDF-")
    assert _paths(fake) == ["/eSCL/ScannerStatus", "/eSCL/ScanJobs", "/eSCL/ScanJobs/job1/NextDocument",
                            "/eSCL/ScanJobs/job1/NextDocument"]
    assert not _paths(fake, "DELETE"), "成功的掃描不用刪工作"
    post = next(r for r in fake.requests if r["method"] == "POST")
    assert post["headers"]["content-type"] == "text/xml" and b"<pwg:InputSource>Platen" in post["body"]
    for r in fake.requests:
        h = r["headers"]
        assert "authorization" not in h and "cookie" not in h
        assert h.get("connection", "").lower() != "close", "不可以送 Connection: close"
        assert h["user-agent"].startswith("jt-doc-tools")


def test_feeder_jpeg_pages_become_one_pdf(fake, tmp_path):
    import fitz
    fake.caps = caps_xml(adf=True, formats=("image/jpeg",))
    fake.doc_type = "image/jpeg"
    fake.docs = [503, jpeg_bytes(), jpeg_bytes(), 503, jpeg_bytes()]
    res = _scan(fake, tmp_path, {"source": "adf", "resolution": 300})
    with fitz.open(res["path"]) as d:
        assert d.page_count == 3
        w, h = d[0].rect.width, d[0].rect.height
    assert abs(w - 595.2) < 2 and abs(h - 842) < 2, "300 dpi 的 A4 JPEG 要換算成 A4 大小的頁面"
    post = next(r for r in fake.requests if r["method"] == "POST")
    assert b"<pwg:InputSource>Feeder" in post["body"]


def test_multi_page_pdfs_are_merged(fake, tmp_path):
    import fitz
    fake.caps = caps_xml(adf=True, duplex=True)
    fake.docs = [pdf_bytes(2), pdf_bytes(1)]
    res = _scan(fake, tmp_path, {"source": "adf_duplex"})
    with fitz.open(res["path"]) as d:
        assert d.page_count == 3
    assert b"<scan:Duplex>true</scan:Duplex>" in next(r for r in fake.requests if r["method"] == "POST")["body"]


def test_empty_feeder_stops_before_creating_a_job(fake, tmp_path):
    fake.caps = caps_xml(adf=True)
    fake.adf = "ScannerAdfEmpty"
    with pytest.raises(E.ScanError) as e:
        _scan(fake, tmp_path, {"source": "adf"})
    assert e.value.code == "SCAN_ADF_EMPTY" and not _paths(fake, "POST")


def test_a_busy_device_is_waited_for_then_given_up(fake, tmp_path, monkeypatch):
    fake.states = ["Processing", "Idle"]
    fake.docs = [pdf_bytes()]
    assert _scan(fake, tmp_path)["pages"] == 1, "設備忙一下下就等它"
    monkeypatch.setattr(E, "STATUS_WAIT", 0.2)
    fake.states = ["Processing"]
    fake.requests.clear()
    with pytest.raises(E.ScanError) as e:
        _scan(fake, tmp_path)
    assert e.value.code == "SCAN_DEVICE_BUSY" and not _paths(fake, "POST")


@pytest.mark.parametrize("status,code", [(401, "SCAN_AUTH_REQUIRED"), (403, "SCAN_AUTH_REQUIRED"),
                                         (409, "SCAN_DEVICE_BUSY"), (503, "SCAN_DEVICE_BUSY"),
                                         (400, "SCAN_REJECTED")])
def test_job_creation_errors_are_explained(fake, tmp_path, status, code):
    fake.post_status = status
    with pytest.raises(E.ScanError) as e:
        _scan(fake, tmp_path)
    assert e.value.code == code


def test_html_instead_of_a_document_is_refused_and_the_job_deleted(fake, tmp_path):
    fake.doc_type = "text/html"
    fake.docs = [b"<html><body>login</body></html>"]
    with pytest.raises(E.ScanError) as e:
        _scan(fake, tmp_path)
    assert e.value.code == "SCAN_BAD_DOCUMENT"
    assert _paths(fake, "DELETE") == ["/eSCL/ScanJobs/job1"]
    assert not list((tmp_path / "w").glob("part*")), "壞掉的檔案要清掉"


def test_too_large_is_refused_and_the_job_deleted(fake, tmp_path, monkeypatch):
    monkeypatch.setattr(E, "MAX_DOC_BYTES", 100)
    fake.docs = [pdf_bytes()]
    with pytest.raises(E.ScanError) as e:
        _scan(fake, tmp_path)
    assert e.value.code == "SCAN_TOO_LARGE" and _paths(fake, "DELETE")
    assert not list((tmp_path / "w").glob("part*")), "寫到一半的檔案要清掉"


def test_cancel_deletes_the_job_on_the_device(fake, tmp_path):
    fake.caps = caps_xml(adf=True)
    fake.docs = [pdf_bytes(), pdf_bytes(), pdf_bytes()]
    got = []

    def cancelled():
        return len(got) >= 1

    with pytest.raises(E.ScanError) as e:
        _scan(fake, tmp_path, {"source": "adf"},
              on_progress=lambda d: got.append(d) if d.get("pages") else None, is_cancelled=cancelled)
    assert e.value.code == "SCAN_CANCELLED" and _paths(fake, "DELETE") == ["/eSCL/ScanJobs/job1"]


def test_no_page_at_all_is_reported(fake, tmp_path):
    fake.docs = []
    with pytest.raises(E.ScanError) as e:
        _scan(fake, tmp_path)
    assert e.value.code == "SCAN_NO_DOCUMENT"


def test_a_location_on_another_host_is_not_followed(fake, tmp_path):
    fake.location = "http://10.9.9.9/eSCL/ScanJobs/job1"
    with pytest.raises(E.ScanError) as e:
        _scan(fake, tmp_path)
    assert e.value.code == "SCAN_PROTOCOL_ERROR"
    assert not any("NextDocument" in p for p in _paths(fake))


def test_addresses_and_ports_are_checked_again_before_scanning(tmp_path):
    caps = E.parse_capabilities(caps_xml())
    with pytest.raises(mc.PolicyError) as e:
        E.scan("127.0.0.1", "127.0.0.1", "http", 80, caps, {}, tmp_path)
    assert e.value.code == "ADDR_LOOPBACK"
    with pytest.raises(mc.PolicyError):
        E.scan("x", "192.168.1.50", "http", 22, caps, {}, tmp_path)


def test_every_code_has_a_message_and_translations():
    import json
    from pathlib import Path
    en = json.loads(Path("app/i18n/en.json").read_text(encoding="utf-8"))
    ja = json.loads(Path("app/i18n/ja.json").read_text(encoding="utf-8"))
    for code, msg in E.MESSAGES.items():
        assert msg in en and msg in ja, f"{code} 沒有英日文：{msg}"
