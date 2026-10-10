"""公文撰擬的官方資料來源（範本 zip / 機關地址簿）。

## 守的是什麼

1. **預設不下載**（使用者 2026-10-07：「預設沒下，要管理員去按」）—— 新安裝
   開管理頁、查狀態、工具來查範本與地址簿，都**不可以**連外。
2. **管理員填的網址是 SSRF 的入口**：內網位址、`file://`、重新導向到內網、
   DNS 先回公開位址再回 127.0.0.1（rebinding）—— 一律擋下，而且**擋在連線之前**
   （判準是假伺服器一次都沒被連到，不是「回應看起來被擋了」）。
3. **失敗保留舊的**：下載壞掉 / 內容不合格時，上一份資料一個位元組都不動。
4. zip 裡的檔名有 Big5（沒設 UTF-8 旗標）與 UTF-8 兩種，都要解得對；
   壞掉的 odt、不是 odt 的檔案要略過並講出原因；zip 炸彈要擋。
5. 手動上傳走同一套驗證。
6. 地址簿逐字比對、台／臺通用、全銜優先。

**測試一律不連外網**：假伺服器綁在 127.0.0.1，並把「只准公開位址」那一道
替換成「只准 127.0.0.1」—— 其他所有位址（含 127.0.0.2、169.254.169.254、
10.x）照樣被擋，所以重新導向到內網那條仍然測得到。
"""
from __future__ import annotations

import io
import json
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.core import official_doc_sources as ods
from app.core import safe_fetch

ROOT = pathlib.Path(__file__).resolve().parent.parent

_PROXY_VARS = ("http_proxy", "https_proxy", "all_proxy", "no_proxy",
               "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")


# ---------------------------------------------------------------- 素材

def _odt(marker: str = "x") -> bytes:
    """最小的 .odt（mimetype ＋ content.xml ＋ manifest）。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(zipfile.ZipInfo("mimetype"), "application/vnd.oasis.opendocument.text")
        z.writestr("content.xml",
                   '<?xml version="1.0" encoding="UTF-8"?>'
                   '<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"'
                   ' xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" office:version="1.3">'
                   f'<office:body><office:text><text:p>{marker}</text:p></office:text></office:body>'
                   '</office:document-content>', compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("META-INF/manifest.xml",
                   '<?xml version="1.0" encoding="UTF-8"?>'
                   '<manifest:manifest xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0"'
                   ' manifest:version="1.3"><manifest:file-entry manifest:full-path="/"'
                   ' manifest:media-type="application/vnd.oasis.opendocument.text"/></manifest:manifest>',
                   compress_type=zipfile.ZIP_DEFLATED)
    return buf.getvalue()


ODT_HAN = _odt("函")
ODT_CERT = _odt("在職證明書")
BIG5_NAME = "一般公文表單/函.odt".encode("big5")


def _templates_zip() -> bytes:
    """官方那份的形狀：Big5 檔名（**沒設 UTF-8 旗標**）＋ UTF-8 檔名，
    再混一份壞掉的 odt 與一個不是 odt 的檔案。

    Python 的 zipfile 寫非 ASCII 檔名一律設 UTF-8 旗標，所以 Big5 那一份
    先用同樣長度的 ASCII 佔位，寫完再把位元組換掉（本機標頭與中央目錄各一次）。
    """
    placeholder = b"P" * (len(BIG5_NAME) - 4) + b".odt"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(placeholder.decode("ascii"), ODT_HAN)
        z.writestr("人事表單/在職證明書.odt", ODT_CERT)
        z.writestr("一般公文表單/壞掉的.odt", b"PK\x03\x04 this is not really a zip")
        z.writestr("說明.txt", "這不是範本")
    data = buf.getvalue()
    assert data.count(placeholder) == 2
    data = data.replace(placeholder, BIG5_NAME)
    # 前提：真的做出了「沒設 UTF-8 旗標的 Big5 檔名」—— 不然下面那條驗的是別的事
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        info = z.infolist()[0]
        assert not info.flag_bits & 0x800
        assert info.filename.encode("cp437") == BIG5_NAME
    return data


BOOK = [
    {"orgId": "Q10000000A", "orgName": "嘉禾市政府秘書處", "statusCode": "T"},
    {"orgId": "Q1000000", "orgName": "嘉禾市政府", "statusCode": "T"},
    {"orgId": "Q20000000B", "orgName": "臺東縣嘉禾區公所", "statusCode": "T"},
    {"orgId": "Q30000000C", "orgName": "臺北示範局", "statusCode": "T"},
    {"orgId": "Q40000000D", "orgName": "", "statusCode": "T"},
]
BOOK_JSON = json.dumps(BOOK, ensure_ascii=False).encode("utf-8")
BOOK_CSV = ("﻿ORGID,ORGNAME,STATUSCODE,UPDATETIME\n"
            "Q50000000E,嘉禾縣警察局,T,2026-01-01 00:00:00\n"
            "Q60000000F,台中示範處,T,2026-01-01 00:00:00\n").encode("utf-8")


# ---------------------------------------------------------------- 假伺服器

class _Srv:
    """假的下載站。`routes` 是 path → (狀態碼, 標頭, 內容)；`hits` 記錄被要了什麼。"""

    def __init__(self, host: str = "127.0.0.1"):
        self.host = host
        self.routes: dict[str, tuple[int, dict, bytes]] = {}
        self.hits: list[str] = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                outer.hits.append(self.path)
                r = outer.routes.get(self.path.split("?", 1)[0])
                if r is None:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                status, headers, body = r
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.httpd = ThreadingHTTPServer((host, 0), H)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def url(self, path: str) -> str:
        return f"http://{self.host}:{self.port}{path}"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def srv():
    s = _Srv()
    yield s
    s.close()


@pytest.fixture
def clean(monkeypatch):
    """每一條測試從「新安裝」開始：沒有來源設定、沒有下載過的資料、沒有代理。"""
    for v in _PROXY_VARS:
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setattr(ods, "FETCH_TIMEOUT_S", 5.0)

    def wipe():
        shutil.rmtree(settings.data_dir / "official_doc", ignore_errors=True)
        try:
            (settings.data_dir / "official_doc_sources.json").unlink()
        except FileNotFoundError:
            pass
        ods._CACHE.clear()
        ods._RUNNING.clear()

    wipe()
    yield
    wipe()


@pytest.fixture
def allow_fake(monkeypatch):
    """只放行假伺服器所在的 127.0.0.1；**其他所有位址照樣被擋**。"""
    monkeypatch.setattr(safe_fetch, "_ip_allowed", lambda ip: str(ip) == "127.0.0.1")


def _point(sid: str, url: str) -> None:
    ods.update_source(sid, {"url": url})


T = "archives-templates"
A = "archives-address-book"


# ---------------------------------------------------------------- 預設不下載

def test_fresh_install_has_nothing_and_never_connects(clean, auth_off, monkeypatch):
    """新安裝：開管理頁、查狀態、工具查範本 / 地址簿 / 顯名 —— **一次都不可以連外**。"""
    calls: list[str] = []

    def _no_network(url, **kw):
        calls.append(url)
        raise AssertionError("新安裝不可以自己連外")

    monkeypatch.setattr(safe_fetch, "fetch_public", _no_network)
    c = TestClient(__import__("app.main", fromlist=["app"]).app)
    assert c.get("/admin/official-doc").status_code == 200
    st = c.get("/admin/official-doc/status").json()
    assert ods.list_templates() == []
    assert ods.search_orgs("嘉禾") == []
    assert ods.attribution() == []
    assert ods.get_template(T, "函") is None
    time.sleep(0.3)        # 有人在背景偷開執行緒的話給它時間連出去
    assert calls == [], f"新安裝自己連外了：{calls}"
    assert [s["id"] for s in st["sources"]] == [T, A], "內建的兩個來源要在"
    for s in st["sources"]:
        assert not s["installed"] and not s["running"]
        assert s["last_attempt"] is None, f"{s['id']} 有下載紀錄 —— 有人自己去下載了"
    assert not (settings.data_dir / "official_doc").exists()
    assert not (settings.data_dir / "official_doc_sources.json").exists(), \
        "只是開頁面就寫了設定檔"


def test_builtin_sources_carry_the_official_urls_and_licence(clean):
    by = {s["id"]: s for s in ods.list_sources()}
    t, a = by[T], by[A]
    assert t["kind"] == ods.KIND_TEMPLATES and a["kind"] == ods.KIND_ADDRESS_BOOK
    # 網址裡的中文要編好（http.client 只收 ASCII）
    assert t["url"] == ("https://www.archives.gov.tw/opendata/"
                        + quote("Web版公文製作系統表單範本.zip"))
    assert a["url"].endswith("/regcenter/pub/addressbook/file/all_active_utf8.json")
    for s in (t, a):
        assert s["license"] == "政府資料開放授權條款－第1版"
        assert s["publisher"] == "國家發展委員會檔案管理局"
        assert s["dataset_url"].startswith("https://data.gov.tw/dataset/")


# ---------------------------------------------------------------- 範本

def test_download_templates_big5_and_utf8_names(clean, allow_fake, srv):
    srv.routes["/t.zip"] = (200, {"Content-Type": "application/octet-stream"},
                            _templates_zip())
    _point(T, srv.url("/t.zip"))
    res = ods.download_now(T)
    assert res["ok"], res
    assert res["count"] == 2
    reasons = {s["path"]: s["reason"] for s in res["skipped"]}
    assert reasons.get("一般公文表單/壞掉的.odt") == "檔案毀損"
    assert reasons.get("說明.txt") == "不是 .odt"

    got = {(t["category"], t["name"]) for t in ods.list_templates()}
    assert got == {("一般公文表單", "函"), ("人事表單", "在職證明書")}, got
    assert ods.get_template(T, "函") == ODT_HAN
    assert ods.get_template(T, "人事表單/在職證明書.odt") == ODT_CERT
    assert ods.get_template(T, "不存在") is None
    assert ods.get_template("../../etc", "函") is None
    # 存成我們編的檔名，zip 裡的路徑沒有變成檔案系統路徑
    files = sorted(p.name for p in
                   (settings.data_dir / "official_doc/sources" / T / "current/templates").iterdir())
    assert files == ["0001.odt", "0002.odt"]
    # 顯名
    attr = ods.attribution()
    assert len(attr) == 1 and "國家發展委員會檔案管理局" in attr[0] \
        and "筆硯公文製作系統表單範本" in attr[0] and "政府資料開放授權條款－第1版" in attr[0]


def test_disabled_source_is_hidden_from_the_tool(clean, allow_fake, srv):
    srv.routes["/t.zip"] = (200, {}, _templates_zip())
    _point(T, srv.url("/t.zip"))
    assert ods.download_now(T)["ok"]
    ods.update_source(T, {"enabled": False})
    assert ods.list_templates() == []
    assert ods.get_template(T, "函") is None
    assert ods.attribution() == []
    # 資料保留，重新啟用就回來
    ods.update_source(T, {"enabled": True})
    assert len(ods.list_templates()) == 2


def test_chinese_in_the_url_is_encoded(clean, allow_fake, srv):
    srv.routes["/files/" + quote("Web版範本.zip")] = (200, {}, _templates_zip())
    _point(T, srv.url("/files/Web版範本.zip"))
    assert ods.get_source(T)["url"].endswith("/files/" + quote("Web版範本.zip"))
    assert ods.download_now(T)["ok"]


def test_failed_download_keeps_the_previous_copy(clean, allow_fake, srv):
    srv.routes["/t.zip"] = (200, {}, _templates_zip())
    _point(T, srv.url("/t.zip"))
    assert ods.download_now(T)["ok"]
    before = ods.list_templates()
    sha_before = ods.status()["sources"][0]["last_ok"]["sha256"]

    # ① 伺服器出錯
    srv.routes["/t.zip"] = (500, {}, b"oops")
    r = ods.download_now(T)
    assert not r["ok"] and "HTTP 500" in r["message"]
    # ② 拿到的不是 zip
    srv.routes["/t.zip"] = (200, {}, b"<html>maintenance</html>")
    r = ods.download_now(T)
    assert not r["ok"] and "不是 zip" in r["message"]
    # ③ zip 裡沒有任何可以用的範本
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("readme.txt", "x")
    srv.routes["/t.zip"] = (200, {}, buf.getvalue())
    r = ods.download_now(T)
    assert not r["ok"] and "沒有任何可以用的 .odt" in r["message"]

    assert ods.list_templates() == before, "下載失敗卻把舊資料換掉了"
    assert ods.get_template(T, "函") == ODT_HAN
    st = ods.status()["sources"][0]
    assert st["installed"]
    assert st["last_attempt"]["ok"] is False
    assert st["last_ok"]["sha256"] == sha_before
    leftovers = [p.name for p in (settings.data_dir / "official_doc/sources" / T).iterdir()
                 if p.name.startswith(".")]
    assert leftovers == [], f"暫存沒有清掉：{leftovers}"


def _bomb_zip() -> bytes:
    """檔頭宣稱解開後 2 GB 的 zip（中央目錄的「未壓縮大小」改大）。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("一般公文表單/函.odt", ODT_HAN)
    data = bytearray(buf.getvalue())
    i = data.find(b"PK\x01\x02")
    assert i > 0
    data[i + 24:i + 28] = (0x7FFF0000).to_bytes(4, "little")
    return bytes(data)


def test_zip_bomb_is_rejected(clean, allow_fake, srv):
    srv.routes["/b.zip"] = (200, {}, _bomb_zip())
    _point(T, srv.url("/b.zip"))
    r = ods.download_now(T)
    assert not r["ok"]
    assert "異常龐大" in r["message"], r["message"]
    assert not ods.status()["sources"][0]["installed"]


def test_too_big_download_stops(clean, allow_fake, srv, monkeypatch):
    monkeypatch.setitem(ods.MAX_BYTES, ods.KIND_TEMPLATES, 1000)
    srv.routes["/t.zip"] = (200, {}, _templates_zip())
    _point(T, srv.url("/t.zip"))
    r = ods.download_now(T)
    assert not r["ok"] and "太大" in r["message"]


# ---------------------------------------------------------------- 地址簿

def test_address_book_json_search(clean, allow_fake, srv):
    srv.routes["/ab.json"] = (200, {"Content-Type": "application/octet-stream"}, BOOK_JSON)
    _point(A, srv.url("/ab.json"))
    r = ods.download_now(A)
    assert r["ok"] and r["count"] == 4, r          # 沒有名稱的那一筆不收

    names = [o["orgName"] for o in ods.search_orgs("嘉禾")]
    assert names[:2] == ["嘉禾市政府", "嘉禾市政府秘書處"], names   # 全銜優先、短的在前
    assert "臺東縣嘉禾區公所" in names
    assert ods.search_orgs("嘉禾市政府")[0] == {"orgId": "Q1000000", "orgName": "嘉禾市政府",
                                              "nameMarks": [[0, 5]], "idMarks": []}
    # 台 / 臺 通用；標亮的位置是**原文**那幾個字（「臺北示範」）
    hit = ods.search_orgs("台北示範")
    assert [o["orgName"] for o in hit] == ["臺北示範局"] and hit[0]["nameMarks"] == [[0, 4]]
    # 好幾個詞：每一個都標
    hit = ods.search_orgs("臺東 公所")
    assert [o["orgName"] for o in hit] == ["臺東縣嘉禾區公所"] and hit[0]["nameMarks"] == [[0, 2], [6, 8]]
    # 代碼開頭：標的是代碼那一段，名稱沒有符合的字就不標
    hit = ods.search_orgs("q3000")[0]
    assert hit["orgName"] == "臺北示範局" and hit["idMarks"] == [[0, 5]] and hit["nameMarks"] == []
    assert ods.search_orgs("不存在的機關") == []
    assert ods.search_orgs("   ") == []
    assert len(ods.search_orgs("嘉禾", limit=1)) == 1
    # 停用就不查
    ods.update_source(A, {"enabled": False})
    assert ods.search_orgs("嘉禾") == []


def test_address_book_csv_and_wrapped_json(clean, allow_fake, srv):
    srv.routes["/ab.csv"] = (200, {}, BOOK_CSV)
    _point(A, srv.url("/ab.csv"))
    r = ods.download_now(A)
    assert r["ok"] and r["count"] == 2, r
    assert [o["orgName"] for o in ods.search_orgs("臺中")] == ["台中示範處"]
    assert ods.search_orgs("嘉禾")[0]["orgId"] == "Q50000000E"
    # 包在物件裡的 JSON 也認得
    srv.routes["/ab.csv"] = (200, {}, json.dumps({"data": BOOK}, ensure_ascii=False).encode())
    assert ods.download_now(A)["ok"]
    assert ods.search_orgs("嘉禾市政府")[0]["orgName"] == "嘉禾市政府"


def test_address_book_rejects_things_that_are_not_an_address_book(clean, allow_fake, srv):
    for body, expect in ((_templates_zip(), "zip 檔"),
                         (b"a,b,c\n1,2,3\n", "找不到機關名稱欄位"),
                         (b'[{"orgId": "1", "orgName": ""}]', "沒有任何一筆有機關名稱"),
                         (b'{"oops": ', "JSON 格式不正確")):
        srv.routes["/ab"] = (200, {}, body)
        _point(A, srv.url("/ab"))
        r = ods.download_now(A)
        assert not r["ok"] and expect in r["message"], (expect, r)


# ---------------------------------------------------------------- SSRF

def test_internal_address_is_blocked_before_connecting(clean, srv):
    """**沒有**放行 127.0.0.1：伺服器明明在，一次都不可以被連到。"""
    srv.routes["/t.zip"] = (200, {}, _templates_zip())
    _point(T, srv.url("/t.zip"))
    r = ods.download_now(T)
    assert not r["ok"]
    assert "內部網路" in r["message"], r["message"]
    assert srv.hits == [], f"內網位址被連到了：{srv.hits}"


def test_redirect_to_an_internal_address_is_blocked(clean, allow_fake, srv):
    """公開網址 302 到內網是最常見的繞法 —— 每一次重新導向都要重驗。"""
    try:
        inner = _Srv("127.0.0.2")      # Linux 上整段 127/8 都在 lo 上
    except OSError:
        inner = None
    target = inner.url("/secret") if inner else "http://169.254.169.254/latest/meta-data/"
    if inner:
        inner.routes["/secret"] = (200, {}, _templates_zip())
    try:
        srv.routes["/go"] = (302, {"Location": target}, b"")
        _point(T, srv.url("/go"))
        r = ods.download_now(T)
        assert not r["ok"]
        assert "內部網路" in r["message"], r["message"]
        assert srv.hits == ["/go"]
        if inner:
            assert inner.hits == [], "重新導向到內網被跟過去了"
    finally:
        if inner:
            inner.close()


def test_dns_rebinding_is_caught_at_connect_time(clean, monkeypatch):
    """送出前驗的時候是公開位址、連線時變成 127.0.0.1 —— 要在連線那一刻擋。"""
    real = socket.getaddrinfo
    calls = {"n": 0}

    def flip(host, port, *a, **kw):
        if host == "files.rebind.example.com":
            calls["n"] += 1
            ip = "93.184.216.34" if calls["n"] == 1 else "127.0.0.1"
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]
        return real(host, port, *a, **kw)

    monkeypatch.setattr(socket, "getaddrinfo", flip)
    with pytest.raises(safe_fetch.BlockedDestination):
        safe_fetch.fetch_public("http://files.rebind.example.com/t.zip",
                                max_bytes=1000, timeout=2)
    assert calls["n"] >= 2, "連線時沒有重新解析（直接用了第一次的結果）"


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/", "http://localhost/", "http://LOCALHOST./",
    "http://10.0.0.1/", "http://192.168.1.1/", "http://172.16.0.1/",
    "http://169.254.169.254/latest/meta-data/", "http://100.64.0.1/",
    "http://0.0.0.0/", "http://[::1]/", "http://[::ffff:127.0.0.1]/",
    "http://[64:ff9b::7f00:1]/", "http://[fe80::1]/", "http://[fc00::1]/",
    "http://2130706433/", "http://0x7f.1/", "http://printer.local/",
    "http://nas.lan/", "http://intranet.corp/",
])
def test_internal_destinations_are_blocked(url):
    with pytest.raises(safe_fetch.BlockedDestination):
        safe_fetch.check_public_destination(url)


@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "ftp://example.com/x.zip", "gopher://x/",
    "javascript:alert(1)", "//example.com/x",
])
def test_non_http_schemes_are_rejected(url):
    with pytest.raises((safe_fetch.UnsafeUrlScheme, safe_fetch.BlockedDestination)):
        safe_fetch.fetch_public(url, max_bytes=10)


def test_url_with_credentials_or_control_chars_is_rejected():
    for u in ("https://user:pw@example.com/x.zip", "https://example.com/a\nb",
              "https://example.com/" + "a" * 3000):
        with pytest.raises(safe_fetch.BlockedDestination):
            safe_fetch.normalize_public_url(u)


def test_ip_is_public_checks_embedded_ipv4():
    assert safe_fetch.ip_is_public("8.8.8.8")
    assert safe_fetch.ip_is_public("2001:4860:4860::8888")
    for ip in ("127.0.0.1", "10.1.2.3", "169.254.169.254", "100.64.0.1",
               "224.0.0.1", "::1", "64:ff9b::7f00:1", "2002:7f00:1::1"):
        assert not safe_fetch.ip_is_public(ip), ip


def test_saving_a_non_http_url_is_refused(clean):
    for u in ("file:///etc/passwd", "ftp://example.com/x", "https://u:p@example.com/x"):
        with pytest.raises(ods.SourceError):
            ods.update_source(T, {"url": u})
    assert ods.get_source(T)["url"].startswith("https://www.archives.gov.tw/")


def test_a_hand_edited_config_cannot_smuggle_file_urls(clean):
    """設定檔被直接改成 file:// —— 讀回來時內建來源退回預設、自訂來源整筆丟掉。"""
    (settings.data_dir / "official_doc_sources.json").write_text(json.dumps({"sources": [
        {"id": T, "url": "file:///etc/passwd", "enabled": True},
        {"id": "custom-evil", "kind": ods.KIND_TEMPLATES, "name": "x",
         "url": "file:///etc/shadow"},
        {"id": "../escape", "kind": ods.KIND_TEMPLATES, "name": "y",
         "url": "https://example.com/x.zip"},
    ]}), encoding="utf-8")
    srcs = ods.list_sources()
    assert [s["id"] for s in srcs] == [T]
    assert srcs[0]["url"].startswith("https://www.archives.gov.tw/")


# ---------------------------------------------------------------- 設定增刪改

def test_add_update_delete_restore(clean, auth_off):
    c = TestClient(__import__("app.main", fromlist=["app"]).app)
    r = c.post("/admin/official-doc/sources", json={
        "kind": ods.KIND_ADDRESS_BOOK, "name": "某縣地址簿",
        "url": "https://example.com/某縣.csv", "publisher": "某縣政府",
        "license": "政府資料開放授權條款－第1版"})
    assert r.status_code == 200, r.text
    new = r.json()["source"]
    assert new["id"].startswith("custom-") and not new["builtin"]
    assert new["url"] == "https://example.com/" + quote("某縣.csv")

    assert c.post("/admin/official-doc/sources", json={
        "kind": "nope", "name": "x", "url": "https://example.com/"}).status_code == 400
    assert c.post("/admin/official-doc/sources", json={
        "kind": ods.KIND_TEMPLATES, "name": "x", "url": "file:///etc/passwd"}).status_code == 400
    assert c.post("/admin/official-doc/sources", json={
        "kind": ods.KIND_TEMPLATES, "name": "", "url": "https://example.com/"}).status_code == 400

    # 內建來源：網址改得動，名稱 / 提供機關 / 授權（顯名的事實）改不動
    r = c.post(f"/admin/official-doc/sources/{T}", json={
        "url": "https://mirror.example.com/t.zip", "name": "亂改", "license": "亂改"})
    assert r.status_code == 200
    t = ods.get_source(T)
    assert t["url"] == "https://mirror.example.com/t.zip"
    assert t["name"] == "筆硯公文製作系統表單範本" and t["license"] == "政府資料開放授權條款－第1版"
    # 自訂來源：都改得動
    r = c.post(f"/admin/official-doc/sources/{new['id']}", json={"name": "改名了", "enabled": False})
    assert r.json()["source"]["name"] == "改名了" and r.json()["source"]["enabled"] is False

    # 刪內建
    assert c.post(f"/admin/official-doc/sources/{A}/delete").status_code == 200
    assert ods.get_source(A) is None
    assert c.post(f"/admin/official-doc/sources/{A}/delete").status_code == 404
    assert c.post("/admin/official-doc/sources/..%2F..%2Fetc/delete").status_code == 404

    # 還原預設：內建的回來、網址回到預設；自訂的不動
    assert c.post("/admin/official-doc/restore-defaults").status_code == 200
    ids = [s["id"] for s in ods.list_sources()]
    assert ids == [T, A, new["id"]], ids
    assert ods.get_source(T)["url"].startswith("https://www.archives.gov.tw/")
    assert ods.get_source(new["id"])["name"] == "改名了"


def test_deleting_a_source_removes_its_data(clean, allow_fake, srv):
    srv.routes["/t.zip"] = (200, {}, _templates_zip())
    _point(T, srv.url("/t.zip"))
    assert ods.download_now(T)["ok"]
    d = settings.data_dir / "official_doc/sources" / T
    assert d.is_dir()
    ods.delete_source(T)
    assert not d.exists()
    assert ods.list_templates() == []
    # 還原預設之後是「沒下載」的狀態，不會自己去下載
    ods.restore_defaults()
    assert not ods.status()["sources"][0]["installed"]


def test_busy_source_cannot_be_deleted_or_started_twice(clean):
    ods._RUNNING[T] = {"started_at": time.time()}
    with pytest.raises(ods.SourceBusy):
        ods.delete_source(T)
    with pytest.raises(ods.SourceBusy):
        ods.start_download(T)
    with pytest.raises(ods.SourceBusy):
        ods.install_upload(T, _templates_zip(), "t.zip")


# ---------------------------------------------------------------- 上傳 / 背景下載（HTTP）

def test_upload_goes_through_the_same_checks(clean, auth_off):
    c = TestClient(__import__("app.main", fromlist=["app"]).app)
    base = "/admin/official-doc/sources"
    r = c.post(f"{base}/{T}/upload", files={"file": ("x.zip", b"not a zip", "application/zip")})
    assert r.status_code == 400 and "不是 zip" in r.json()["detail"]
    r = c.post(f"{base}/{T}/upload", files={"file": ("b.zip", _bomb_zip(), "application/zip")})
    assert r.status_code == 400 and "異常龐大" in r.json()["detail"]
    r = c.post(f"{base}/{T}/upload", files={"file": ("t.zip", _templates_zip(), "application/zip")})
    assert r.status_code == 200, r.text
    assert r.json()["count"] == 2 and r.json()["origin"] == "upload"
    assert ods.get_template(T, "函") == ODT_HAN

    r = c.post(f"{base}/{A}/upload", files={"file": ("t.zip", _templates_zip(), "application/zip")})
    assert r.status_code == 400 and "zip 檔" in r.json()["detail"]
    r = c.post(f"{base}/{A}/upload", files={"file": ("ab.json", BOOK_JSON, "application/json")})
    assert r.status_code == 200 and r.json()["count"] == 4
    assert c.get("/admin/official-doc/search-orgs", params={"q": "嘉禾"}).json()["results"][0] \
        == {"orgId": "Q1000000", "orgName": "嘉禾市政府", "nameMarks": [[0, 2]], "idMarks": []}

    st = {s["id"]: s for s in c.get("/admin/official-doc/status").json()["sources"]}
    assert st[A]["last_ok"]["filename"] == "ab.json"
    assert st[T]["last_attempt"]["ok"] is True
    tl = c.get(f"{base}/{T}/templates").json()
    assert {t["name"] for t in tl["templates"]} == {"函", "在職證明書"}
    assert {s["reason"] for s in tl["skipped"]} == {"檔案毀損", "不是 .odt"}


def test_upload_size_limit(clean, auth_off, monkeypatch):
    monkeypatch.setitem(ods.MAX_BYTES, ods.KIND_TEMPLATES, 1000)
    c = TestClient(__import__("app.main", fromlist=["app"]).app)
    r = c.post(f"/admin/official-doc/sources/{T}/upload",
               files={"file": ("t.zip", _templates_zip(), "application/zip")})
    assert r.status_code == 413


def test_background_download_through_the_page(clean, auth_off, allow_fake, srv):
    """按「下載」→ 立刻回來 → 輪詢狀態直到完成（跟畫面走同一條路）。"""
    srv.routes["/ab.json"] = (200, {}, BOOK_JSON)
    _point(A, srv.url("/ab.json"))
    c = TestClient(__import__("app.main", fromlist=["app"]).app)
    r = c.post(f"/admin/official-doc/sources/{A}/download")
    assert r.status_code == 200 and r.json()["started"]
    for _ in range(100):
        st = {s["id"]: s for s in c.get("/admin/official-doc/status").json()["sources"]}
        if not st[A]["running"]:
            break
        time.sleep(0.05)
    assert st[A]["installed"] and st[A]["count"] == 4
    assert st[A]["last_attempt"]["ok"] is True
    assert st[A]["last_attempt"]["origin"] == "download"


def test_non_admin_is_refused(clean, auth_off):
    from app.core import auth_settings, permissions, roles, sessions, user_manager
    import app.main as app_main
    pw = "TestAdmin1234"
    auth_settings.enable_local_with_admin(
        admin_username="jtdt-admin", admin_display_name="管理員",
        admin_password=pw, admin_password_confirm=pw, actor_ip="127.0.0.1")
    roles.seed_builtin_roles()
    uid = user_manager.create_local("plainuser", "一般人", "UserPass1234")
    permissions.set_subject_roles("user", str(uid), ["default-user"])
    tok, _ = sessions.issue(uid, remember=False, ip="127.0.0.1", ua="pytest")
    c = TestClient(app_main.app)
    c.cookies.set(sessions.COOKIE_NAME, tok)
    for method, path, kw in (
            ("GET", "/admin/official-doc", {}),
            ("GET", "/admin/official-doc/status", {}),
            ("GET", "/admin/official-doc/search-orgs?q=a", {}),
            ("GET", f"/admin/official-doc/sources/{T}/templates", {}),
            ("POST", "/admin/official-doc/sources", {"json": {"kind": ods.KIND_TEMPLATES,
                                                             "name": "x", "url": "https://e.com/"}}),
            ("POST", f"/admin/official-doc/sources/{T}", {"json": {"url": "https://e.com/x"}}),
            ("POST", f"/admin/official-doc/sources/{T}/delete", {}),
            ("POST", f"/admin/official-doc/sources/{T}/download", {}),
            ("POST", f"/admin/official-doc/sources/{T}/upload",
             {"files": {"file": ("t.zip", b"x", "application/zip")}}),
            ("POST", "/admin/official-doc/restore-defaults", {})):
        r = c.request(method, path, follow_redirects=False, **kw)
        assert r.status_code == 403, f"{method} {path} → {r.status_code}"
    assert ods.get_source(T)["url"].startswith("https://www.archives.gov.tw/")
    assert ods._RUNNING == {}


# ---------------------------------------------------------------- 真的瀏覽器

def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def test_admin_page_runs_in_a_real_browser(monkeypatch):
    """管理頁在真的瀏覽器裡開一次：有資料的狀態下展開範本清單、試查地址簿、
    打開新增表單 —— 主控台不可以有任何錯誤（含 CSP 違規）。"""
    sys.path.insert(0, str(ROOT))
    from tools.browser_probe import browser as _browser, profile_arg as _profile_arg
    if _browser() is None:
        pytest.skip("沒有 chromium —— 這條要真的瀏覽器")
    try:
        import websockets.sync.client as wsc
    except ImportError:
        pytest.skip("沒有 websockets")

    data = pathlib.Path(tempfile.mkdtemp(prefix="odsrc-browser-"))
    # 先用同一套程式把兩個來源灌進那個資料目錄（「有資料」那條畫面路徑才走得到）
    monkeypatch.setattr(settings, "data_dir", data)
    ods._CACHE.clear()
    ods._RUNNING.clear()
    assert ods.install_upload(T, _templates_zip(), "t.zip")["count"] == 2
    assert ods.install_upload(A, BOOK_JSON, "ab.json")["count"] == 4
    ods._CACHE.clear()
    monkeypatch.undo()

    port, cdp = _free_port(), _free_port()
    env = {**os.environ, "JTDT_DATA_DIR": str(data), "JTDT_CSRF_DISABLE": "1"}
    srv = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
         "--port", str(port), "--log-level", "warning"],
        cwd=str(ROOT), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    br = subprocess.Popen(
        [_browser(), "--headless=new", "--no-sandbox", "--disable-gpu",
         _profile_arg(), f"--remote-debugging-port={cdp}", "--remote-allow-origins=*", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(120):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1)
                urllib.request.urlopen(f"http://127.0.0.1:{cdp}/json/version", timeout=1)
                break
            except Exception:
                time.sleep(0.5)
        else:
            pytest.skip("實例或瀏覽器起不來")
        req = urllib.request.Request(f"http://127.0.0.1:{cdp}/json/new?about:blank",
                                     method="PUT")
        with urllib.request.urlopen(req, timeout=10) as r:
            tab = json.loads(r.read())
        errs: list[str] = []
        with wsc.connect(tab["webSocketDebuggerUrl"], max_size=None, open_timeout=10) as ws:
            n = [0]

            def collect(m):
                if m.get("method") == "Runtime.exceptionThrown":
                    d = m["params"]["exceptionDetails"]
                    errs.append("例外：" + str((d.get("exception") or {}).get("description")
                                                or d.get("text"))[:200])
                elif m.get("method") == "Log.entryAdded" and \
                        m["params"]["entry"].get("level") == "error":
                    errs.append("主控台：" + m["params"]["entry"].get("text", "")[:200])

            def send(method, params=None):
                n[0] += 1
                ws.send(json.dumps({"id": n[0], "method": method, "params": params or {}}))
                while True:
                    m = json.loads(ws.recv(timeout=60))
                    if m.get("id") == n[0]:
                        return m
                    collect(m)

            def js(expr):
                r = send("Runtime.evaluate", {"expression": expr, "returnByValue": True,
                                              "awaitPromise": True})
                return (r.get("result") or {}).get("result", {}).get("value")

            def drain(sec):
                end = time.time() + sec
                while time.time() < end:
                    try:
                        collect(json.loads(ws.recv(timeout=0.2)))
                    except TimeoutError:
                        pass

            send("Runtime.enable")
            send("Log.enable")
            send("Page.enable")
            send("Page.navigate", {"url": f"http://127.0.0.1:{port}/admin/official-doc"})
            for _ in range(100):
                if js("document.readyState") == "complete" and \
                        js("document.querySelectorAll('#odRows tr').length") >= 2:
                    break
                time.sleep(0.1)
            drain(0.8)
            rows = js("Array.from(document.querySelectorAll('#odRows > tr[data-sid]'))"
                      ".map(r => r.dataset.sid)")
            assert rows == [T, A], rows
            assert "2 份範本" in js("document.querySelector('#odRows').textContent")
            # 展開範本清單
            js("document.querySelector('details.od-details summary').click()")
            for _ in range(50):
                if js("document.querySelectorAll('.od-tpl-list li').length") == 2:
                    break
                time.sleep(0.1)
            assert js("Array.from(document.querySelectorAll('.od-tpl-list li'))"
                      ".map(l => l.textContent).sort().join(',')") == "函,在職證明書"
            # 試查地址簿
            js("const q = document.getElementById('odOrgQ'); q.value = '嘉禾';"
               "q.dispatchEvent(new Event('input'))")
            for _ in range(50):
                if js("document.querySelectorAll('#odOrgs li').length") >= 1:
                    break
                time.sleep(0.1)
            first = js("document.querySelector('#odOrgs li').textContent")
            assert first and "嘉禾市政府" in first and "秘書處" not in first, first
            # 新增表單
            js("document.getElementById('odAddToggle').click()")
            assert js("document.getElementById('odAddBox').hidden") is False
            drain(0.5)
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{cdp}/json/close/{tab['id']}",
                                   timeout=5).read()
        except Exception:
            pass
        assert errs == [], errs
    finally:
        br.terminate()
        srv.terminate()
        try:
            br.wait(timeout=5)
            srv.wait(timeout=5)
        except Exception:
            br.kill()
            srv.kill()
        shutil.rmtree(data, ignore_errors=True)


def test_match_marks_positions_are_in_the_original_text():
    """符合處的位置以**原文**的字元（code point）計：台臺、全形半形照比對的規則對得上；
    擴充 B 區的字（UTF-16 佔兩格）不會讓後面的位置錯開；重疊的併成一段。"""
    from app.core import cjk_fts
    n = cjk_fts.normalize
    assert ods.match_marks("臺北市政府財政局", [n("台北"), n("財政")]) == [[0, 2], [5, 7]]
    assert ods.match_marks("𠀋財政局", [n("財政")]) == [[1, 3]]
    assert ods.match_marks("ＡＢＣ局", [n("abc")]) == [[0, 3]]
    assert ods.match_marks("財政局財政", [n("財政"), n("政局")]) == [[0, 5]]
    assert ods.match_marks("嘉禾市政府", []) == [] and ods.match_marks("", [n("嘉")]) == []
