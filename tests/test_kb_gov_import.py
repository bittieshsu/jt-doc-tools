"""知識庫：匯入政府公開資料（全國法規資料庫、國發會行政規則、行政院釋例）。

## 守的是什麼

1. **開頁面不連外**：管理頁、狀態、搜尋清單一次都不可以下載（判準是下載函式
   一次都沒被叫到）；只有按「下載資料」「檢查更新」才會連出去。
2. **整包下載、在本機挑**：清單從下載檔讀出來（JSON 逐筆、XML 逐筆、三種標籤），
   用 pcode 選，選了才匯入。
3. **一條一段**：每一條自己一段，上層標題是「法規名稱 ＞ 章 ＞ 節」、母條文是條號；
   `第 12-1 條` 變成 `第 12 條之 1`；太長的條拆開、每一塊帶同一個條號。
4. **施行日期 99991231 不是生效日期**：生效日期留空、說明存起來。
5. **已廢止不刪**：停用、標示，舊版本留著。
6. **檢查更新只在異動日期變了才建新版本**；新的啟用、舊的停用但查得到。
7. **大小上限、zip 炸彈、DTD／實體、內網位址**一律擋，失敗時上一份資料不動。
8. **顯名**每個版本各存一份；用途對（法規＝業務依據、規則與釋例＝格式與用語參考）。
9. 選取量大要確認；選取的代碼要合法而且在清單裡。
10. 一般切段（使用者上傳的文件）**一個字都不變**。

**測試一律不連外網**：假伺服器綁 127.0.0.1，並把「只准公開位址」換成「只准 127.0.0.1」；
素材的法規名稱、代碼都是編的。
"""
from __future__ import annotations

import io
import json
import sqlite3
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.core import safe_fetch
from app.core.kb import chunker, gov, gov_packages as gp, law_text, retrieval, store
from tests._kb_support import HANDBOOK_TXT, kb_isolated  # noqa: F401

LONG = "".join(f"第{i}款所稱之事項，應依主管機關之規定辦理並報請核備。" for i in range(1, 41))


def _law(pcode, name, arts, *, modified="20070321", effective="", note="", abolished="",
         level="法律", histories="1.中華民國十七年制定公布\r\n2.中華民國九十六年修正公布", atts=None):
    return {"LawLevel": level, "LawName": name,
            "LawURL": f"https://law.moj.gov.tw/LawClass/LawAll.aspx?pcode={pcode}",
            "LawCategory": "行政＞測試", "LawModifiedDate": modified,
            "LawEffectiveDate": effective, "LawEffectiveNote": note,
            "LawAbandonNote": abolished, "LawHasEngVersion": "N", "EngLawName": "",
            "LawAttachements": atts or [], "LawHistories": histories, "LawForeword": "",
            "LawArticles": [{"ArticleType": t, "ArticleNo": n, "ArticleContent": c} for t, n, c in arts]}


LAW1_ARTS = [
    ("C", "", "   第 一 章 總則"),
    ("A", "第 1 條", "稱公文者，謂處理公務之文書。"),
    ("A", "第 2 條", "公文程式之類別如下：\r\n一、令：公布法律時用之。\r\n二、函：各機關間公文往復時用之。"),
    ("C", "", "      第 一 節 簽署"),
    ("A", "第 12-1 條", "機關公文得以電子文件行之。"),
    ("C", "", "   第 二 章 附則"),
    ("A", "第 13 條", LONG),
    ("A", "第 14 條", "本條例自公布日施行。"),
]


def law_zip(*, law1_modified="20070321", law1_extra="", abolish3=False, law1_abolish=False) -> bytes:
    arts = list(LAW1_ARTS)
    if law1_extra:
        arts.append(("A", "第 15 條", law1_extra))
    laws = [
        _law("T0000001", "範例公文條例", arts, modified=law1_modified,
             abolished="廢" if law1_abolish else "",
             atts=[{"FileName": "附表一.PDF", "FileURL": "https://law.moj.gov.tw/LawClass/LawGetFile.ashx?FileId=1"}]),
        _law("T0000002", "範例施行法", [("A", "第 1 條", "本法之施行日期由行政院定之。"),
                                     ("A", "第 2 條", "本法第三條修正條文施行日期另定之。")],
             modified="20251111", effective="99991231",
             note="1.一百十四年修正之第 3 條施行日期由行政院定之。"),
        _law("T0000003", "已廢止範例條例", [("A", "第 1 條", "本條例已不再適用。")],
             modified="19970507", abolished="廢" if abolish3 else "廢"),
    ]
    raw = json.dumps({"UpdateDate": "2026/9/24 上午 12:00:00", "Laws": laws},
                     ensure_ascii=False).encode("utf-8")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("ChLaw.json", b"\xef\xbb\xbf" + raw)
        z.writestr("schema.csv", "name,title\n")
        z.writestr("manifest.csv", "name,schema\n")
    return buf.getvalue()


ORDER_XML = """<?xml version="1.0" encoding="utf-8"?>
<Laws UpdateDate="2026/9/24 上午 12:00:00">
  <Law>
    <LawLevel>命令</LawLevel>
    <LawName>範例作業辦法</LawName>
    <LawURL>https://law.moj.gov.tw/LawClass/LawAll.aspx?pcode=T0000010</LawURL>
    <LawCategory>行政＞測試</LawCategory>
    <LawModifiedDate>20100511</LawModifiedDate>
    <LawEffectiveDate>
    </LawEffectiveDate>
    <LawEffectiveNote>
    </LawEffectiveNote>
    <LawAbandonNote>
    </LawAbandonNote>
    <LawAttachements>
      <File><FileName>附件一.PDF</FileName><FileURL>https://law.moj.gov.tw/LawClass/LawGetFile.ashx?FileId=9</FileURL></File>
    </LawAttachements>
    <LawHistories>1.中華民國九十九年五月十一日訂定發布</LawHistories>
    <LawForeword>
    </LawForeword>
    <LawArticles>
      <Article><ArticleType>A</ArticleType><ArticleNo>第 1 條</ArticleNo><ArticleConctent>本辦法依範例公文條例第十二條之一訂定之。</ArticleConctent></Article>
      <Article><ArticleType>A</ArticleType><ArticleNo>2</ArticleNo><ArticleConctent>二、機關公文電子交換作業應依照規定積極進行，凡規避及
    妨礙交換等行為，均應依法懲處。
</ArticleConctent></Article>
      <Article><ArticleType>A</ArticleType><ArticleNo>3</ArticleNo><ArticleConctent>┌──┬──┐
│項目│費率│
└──┴──┘</ArticleConctent></Article>
    </LawArticles>
  </Law>
</Laws>
"""


def order_zip(xml: str = ORDER_XML) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("ChOrder.xml", xml.encode("utf-8"))
    return buf.getvalue()


SENDLAW_XML = """<?xml version="1.0" encoding="UTF-8"?>
<LAWS UpdateDate="2026/9/24 上午 12:00:00">
  <法規>
    <法規性質>法律</法規性質>
    <法規名稱>範例公文條例</法規名稱>
    <法規網址>https://law.moj.gov.tw/LawClass/LawAll.aspx?pcode=T0000001</法規網址>
    <法規類別>行政</法規類別>
    <最新異動日期>20070321</最新異動日期>
    <生效日期></生效日期>
    <生效內容><![CDATA[]]></生效內容>
    <廢止註記></廢止註記>
    <附件><檔案><檔案名稱>附表一.PDF</檔案名稱><下載網址>https://law.moj.gov.tw/LawClass/LawGetFile.ashx?FileId=1</下載網址></檔案></附件>
    <沿革內容><![CDATA[1.制定公布]]></沿革內容>
    <前言><![CDATA[]]></前言>
    <法規內容>
      <編章節>   第 一 章 總則</編章節>
      <條文><條號>第 1 條</條號><條文內容><![CDATA[稱公文者，謂處理公務之文書。]]></條文內容></條文>
      <條文><條號>第 12-1 條</條號><條文內容><![CDATA[機關公文得以電子文件行之。]]></條文內容></條文>
    </法規內容>
  </法規>
</LAWS>
"""


def sendlaw_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("FalV.xml", SENDLAW_XML.encode("utf-8"))
    return buf.getvalue()


NDC_XML = """<?xml version="1.0" encoding="UTF-8"?>
<LAWS 匯出時間="20260202_092838897">
  <法規>
    <法規位階>05:行政規則§159,II,1</法規位階>
    <法規名稱>範例文書作業規範</法規名稱>
    <法規網址>https://theme.ndc.gov.tw/lawout/LawContent.aspx?id=GL900001</法規網址>
    <法規類別>測試類</法規類別>
    <法規異動日期>20201211</法規異動日期>
    <生效日期 />
    <廢止註記>
    </廢止註記>
    <附件>
      <檔案名稱>附件一.pdf</檔案名稱>
      <下載網址>https://theme.ndc.gov.tw/lawout/Download.ashx?FileID=1</下載網址>
    </附件>
    <沿革內容><![CDATA[1.中華民國100年1月1日函訂定
2.中華民國109年12月11日函修正]]></沿革內容>
    <法規內容型態>02:單篇型</法規內容型態>
    <法規內容><![CDATA[
範例文書作業規範

第一章　總　　則
一、全程管制：
（一）公文應全程列入管制。
二、全面管制：
（一）各單位列入管制範圍。
第二章　稽催
三、稽催方式：以公文系統通知承辦人。
]]></法規內容>
  </法規>
</LAWS>
"""


def ey_pdf(updated: str = "115.8.31") -> bytes:
    import fitz
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    y = 60
    for ln in ("文書處理相關釋例", f"更新日期：{updated}", "主旨 日期 說明",
               "詢問簽稿併陳如何註明會辦單位一案",
               "一、依文書處理手冊第19點規定，簽稿併陳應於簽之擬辦段敘明。"):
        page.insert_text((60, y), ln, fontname="china-t", fontsize=11)
        y += 18
    buf = io.BytesIO()
    doc.save(buf)
    doc.close()
    return buf.getvalue()


# ---------------------------------------------------------------- 假伺服器
class _Srv:
    def __init__(self):
        self.routes: dict[str, tuple[int, bytes]] = {}
        self.hits: list[str] = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                outer.hits.append(self.path)
                code, body = outer.routes.get(self.path, (404, b""))
                self.send_response(code)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def srv():
    s = _Srv()
    yield s
    s.close()


@pytest.fixture
def g(kb_isolated, monkeypatch):  # noqa: F811
    """乾淨的政府公開資料狀態（知識庫在暫存目錄、沒有代理、沒有進行中的作業）。"""
    for v in ("http_proxy", "https_proxy", "all_proxy", "no_proxy",
              "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setattr(gov, "FETCH_TIMEOUT_S", 5.0)
    gov._RUNNING.clear()
    gov._CACHE.clear()
    yield gov
    gov._RUNNING.clear()
    gov._CACHE.clear()


@pytest.fixture
def allow_fake(monkeypatch):
    """只放行假伺服器所在的 127.0.0.1；其他位址照樣被擋。"""
    monkeypatch.setattr(safe_fetch, "_ip_allowed", lambda ip: str(ip) == "127.0.0.1")


def _point(pid: str, url: str, alt: str = "") -> None:
    gov.set_package_urls(pid, {"url": url, "alt_url": alt})


def _serve_moj(srv, law: bytes = None, order: bytes = None) -> None:
    srv.routes["/law.zip"] = (200, law if law is not None else law_zip())
    srv.routes["/order.zip"] = (200, order if order is not None else order_zip())
    _point("moj-law", srv.url("/law.zip"))
    _point("moj-order", srv.url("/order.zip"))


def _download(gid: str) -> None:
    for pid in gov.GROUPS[gid]["packages"]:
        gov.download_package(pid)


def _chunks(dataset_name: str) -> list[dict]:
    c = store.conn()
    did = c.execute("SELECT id FROM kb_datasets WHERE name=?", (dataset_name,)).fetchone()["id"]
    vid = c.execute("SELECT id FROM kb_versions WHERE dataset_id=? AND status='active'",
                    (did,)).fetchone()["id"]
    return store.list_chunks(vid, limit=500)


def _versions(name: str) -> list[dict]:
    c = store.conn()
    did = c.execute("SELECT id FROM kb_datasets WHERE name=?", (name,)).fetchone()["id"]
    return store.list_versions(did)


# ---------------------------------------------------------------- 1. 不自己連外
def test_page_status_and_search_never_connect(g, admin_session, monkeypatch):
    calls: list = []

    def _no_net(url, **kw):
        calls.append(url)
        raise AssertionError("開頁面不可以連外")

    monkeypatch.setattr(safe_fetch, "fetch_public", _no_net)
    c, _, _ = admin_session
    r = c.get("/admin/knowledge/gov")
    assert r.status_code == 200 and "govGroups" in r.text
    st = c.get("/admin/knowledge/api/gov/status").json()
    assert {x["id"] for x in st["groups"]} == {"moj", "ndc", "ey"}
    assert all(not x["downloaded"] for x in st["groups"])
    assert c.get("/admin/knowledge/api/gov/moj/search?q=公文").json()["items"] == []
    assert c.get("/admin/knowledge/api/gov/moj/selection").status_code == 200
    assert c.get("/admin/knowledge").status_code == 200
    time.sleep(0.3)
    assert calls == []
    assert not (store.kb_dir() / "gov" / "packages").exists()
    assert not (store.kb_dir() / "gov" / "config.json").exists(), "只是開頁面就寫了設定檔"


def test_builtin_sources_licence_and_link_only_handbook(g):
    # 還沒下載時，預設建議也叫得出名稱（不是一串代碼），而且不標成「找不到」
    assert set(gov.GROUPS["moj"]["default_names"]) == set(gov.GROUPS["moj"]["defaults"])
    items = gov.selected_items("moj")
    assert [i["key"] for i in items] == list(gov.GROUPS["moj"]["defaults"])
    assert items[0]["name"] == "公文程式條例" and items[0]["pending"] and not items[0]["missing"]
    st = gov.status()
    by = {x["id"]: x for x in st["groups"]}
    assert by["moj"]["category"] == "business_law"
    assert by["ndc"]["category"] == "writing_rules" and by["ey"]["category"] == "writing_rules"
    for x in st["groups"]:
        assert x["license"] == "政府資料開放授權條款－第1版"
    urls = {p["id"]: p["url"] for x in st["groups"] for p in x["packages"]}
    assert urls["moj-law"] == "https://law.moj.gov.tw/api/ch/law/json"
    assert urls["moj-order"] == "https://law.moj.gov.tw/api/ch/order/xml"
    # 文書處理手冊：只有連結，**不在任何下載清單裡**
    hb = st["link_only"][0]
    assert hb["name"] == "文書處理手冊" and "保有所有權利" in hb["note"]
    assert all("ecb75289" not in u for u in urls.values())
    assert len(gov.GROUPS["moj"]["defaults"]) == 20


# ---------------------------------------------------------------- 2. 清單與選取
def test_listing_from_packages_and_selecting_by_pcode(g, allow_fake, srv):
    _serve_moj(srv)
    _download("moj")
    idx = gov.merged_index("moj")
    assert set(idx) == {"T0000001", "T0000002", "T0000003", "T0000010"}
    assert idx["T0000003"]["abolished"] and not idx["T0000001"]["abolished"]
    assert idx["T0000010"]["level"] == "命令"
    # 預設的 20 部（真的 pcode）在這份假資料裡都找不到 —— 要講出來，不可以默默當成選好了
    st = {x["id"]: x for x in gov.status()["groups"]}["moj"]
    assert len(st["missing_defaults"]) == 20
    assert gov.search("moj", "範例")["total"] == 4
    assert [i["key"] for i in gov.search("moj", "T00000")["items"]] != []
    assert gov.search("moj", "t0000010")["items"][0]["name"] == "範例作業辦法"
    # 台／臺、全形半形不分
    assert gov.search("moj", "範例 作業")["items"][0]["key"] == "T0000010"
    keys = gov.set_selection("moj", ["T0000001", "T0000010", "T0000001"])
    assert keys == ["T0000001", "T0000010"]
    assert gov.get_selection("moj") == ["T0000001", "T0000010"]
    with pytest.raises(gov.GovError):
        gov.set_selection("moj", ["../../etc/passwd"])
    with pytest.raises(gov.GovError):
        gov.set_selection("moj", ["Z9999999"])          # 不在清單裡
    assert gov.reset_selection("moj") == list(gov.GROUPS["moj"]["defaults"])


def test_large_selection_needs_confirmation(g, allow_fake, srv, monkeypatch):
    _serve_moj(srv)
    _download("moj")
    monkeypatch.setattr(gov, "WARN_ITEMS", 1)
    with pytest.raises(gov.NeedsConfirm) as e:
        gov.set_selection("moj", ["T0000001", "T0000010"])
    assert e.value.count == 2
    assert gov.selection_is_default("moj"), "沒確認就存進去了"
    assert gov.set_selection("moj", ["T0000001", "T0000010"], confirm=True) == ["T0000001", "T0000010"]
    monkeypatch.setattr(gov, "MAX_SELECTED", 1)
    with pytest.raises(gov.GovError):
        gov.set_selection("moj", ["T0000001", "T0000010"], confirm=True)


# ---------------------------------------------------------------- 3. 一條一段
def test_import_makes_one_segment_per_article(g, allow_fake, srv):
    _serve_moj(srv)
    _download("moj")
    gov.set_selection("moj", ["T0000001", "T0000010"])
    s = gov.sync("moj", actor="tester")
    assert s["new"] == 2 and s["failed"] == 0
    ch = _chunks("範例公文條例")
    refs = [c["parent_ref"] for c in ch]
    assert refs[:3] == ["第1條", "第2條", "第12條之1"]
    by = {c["parent_ref"]: c for c in ch}
    # 第 2 條連同它的款在同一段、不跟第 1 條裝在一起
    assert by["第2條"]["text"].startswith("第 2 條 公文程式之類別如下：")
    assert "二、函" in by["第2條"]["text"] and "稱公文者" not in by["第2條"]["text"]
    # 上層標題：法規名稱 ＞ 章 ＞ 節
    assert by["第1條"]["heading_path"] == ["範例公文條例", "第 一 章 總則"]
    assert by["第12條之1"]["heading_path"] == ["範例公文條例", "第 一 章 總則", "第 一 節 簽署"]
    assert by["第12條之1"]["text"].startswith("第 12 條之 1 ")
    # 換章之後節要收掉
    long_parts = [c for c in ch if c["parent_ref"] == "第13條"]
    assert len(long_parts) >= 2, "一條超過上限要拆開"
    assert all(c["parts"] == len(long_parts) for c in long_parts)
    assert [c["part"] for c in long_parts] == list(range(1, len(long_parts) + 1))
    assert long_parts[0]["heading_path"] == ["範例公文條例", "第 二 章 附則"]
    assert all(len(c["text"]) <= chunker.CHUNK_MAX for c in ch)
    # 附件只列連結，不進索引
    assert not any("LawGetFile" in c["text"] for c in ch)
    # 命令：數字條號 ＋ 內文開頭「二、」、硬換行接回、表格保留
    oc = _chunks("範例作業辦法")
    assert [c["parent_ref"] for c in oc] == ["第1條", "二", ""]
    assert "凡規避及妨礙交換等行為" in oc[1]["text"]
    assert oc[2]["text"].splitlines()[0].startswith("┌")


def test_article_label_and_unwrap():
    assert law_text.article_label("第 12-1 條") == "第 12 條之 1"
    assert law_text.article_label("第 174-1 條") == "第 174 條之 1"
    assert law_text.article_label("第 7 條") == "第 7 條"
    assert law_text.article_label("3") == ""
    assert law_text.unwrap("一、甲乙\n    丙丁。\n  （一）戊己。\n二、庚。") == [
        "一、甲乙丙丁。", "（一）戊己。", "二、庚。"]
    assert law_text.unwrap("┌─┐\n│甲│\n└─┘") == ["┌─┐", "│甲│", "└─┘"]


def test_law_text_round_trip_escapes_markdown_lookalikes():
    rec = {"key": "T1", "name": "範例條例", "level": "法律", "modified": "20200101",
           "articles": [{"type": "A", "no": "第 1 條", "content": "# 號不是標題\n---\n\\反斜線"}]}
    md = law_text.render_law(rec, attribution="出處")
    doc = law_text.parse_law(md)
    assert doc["items"] == [("A", "第 1 條", ["# 號不是標題", "---", "\\反斜線"])]
    ch = law_text.law_chunks(md)
    assert len(ch) == 1 and ch[0]["text"].startswith("第 1 條 # 號不是標題")


# ---------------------------------------------------------------- 4. 版本資訊與顯名
def test_version_meta_attribution_and_purpose(g, allow_fake, srv):
    _serve_moj(srv)
    _download("moj")
    gov.set_selection("moj", ["T0000001", "T0000002"])
    gov.sync("moj")
    v = _versions("範例公文條例")[0]
    assert v["status"] == "active"
    assert v["version_label"] == "民國96年3月21日修正"
    assert v["published_on"] == "2007-03-21" and v["effective_on"] == ""
    assert v["source_url"] == "https://law.moj.gov.tw/LawClass/LawAll.aspx?pcode=T0000001"
    gv = store.gov_version(v["id"])
    assert gv["license"] == "政府資料開放授權條款－第1版"
    assert "法務部全國法規資料庫" in gv["attribution"]
    assert "資料更新日期 民國115年9月24日" in gv["attribution"]
    assert "法規內容以各主管機關公布者為準" in gv["attribution"]
    # 用途：法規 ＝ 業務依據（公文撰擬算它是依據）
    hits = retrieval.search("公文程式之類別如下", user_id=None)
    assert hits and hits[0]["purpose"] == "substantive_basis"
    # 檢索結果也帶著出處（工具要顯示時不必再查一次）
    assert hits[0]["attribution"] == gv["attribution"]
    assert retrieval.get_chunk(hits[0]["chunk_id"], user_id=None)["attribution"] == gv["attribution"]
    from app.core import official_doc
    refs = official_doc.normalise_references(hits)
    assert official_doc.reference_sources(refs), "法規要算業務依據"


def test_undetermined_effective_date_is_a_notice_not_a_date(g, allow_fake, srv):
    _serve_moj(srv)
    _download("moj")
    gov.set_selection("moj", ["T0000002"])
    gov.sync("moj")
    v = _versions("範例施行法")[0]
    assert v["effective_on"] == "", "99991231 不可以當成生效日期"
    gv = store.gov_version(v["id"])
    assert "施行日期由主管機關另定" in gv["notice"]
    assert "第 3 條施行日期由行政院定之" in gv["notice"]
    info = gov.gov_info_for_versions(v["dataset_id"])
    assert info[v["id"]]["notice"] == gv["notice"]


# ---------------------------------------------------------------- 5. 已廢止
def test_abolished_law_is_imported_disabled_and_marked(g, allow_fake, srv):
    _serve_moj(srv)
    _download("moj")
    gov.set_selection("moj", ["T0000003"])
    s = gov.sync("moj")
    assert s["new"] == 1 and s["abolished"] == 1
    v = _versions("已廢止範例條例")[0]
    assert v["status"] == "inactive", "已廢止的不可以啟用"
    assert store.gov_version(v["id"])["abolished"]
    d = store.get_dataset(v["dataset_id"])
    assert d["description"].startswith("（已廢止）")
    assert retrieval.search("本條例已不再適用", user_id=None) == [], "停用的版本不可以被查到"
    assert {"key": "T0000003", "name": "已廢止範例條例", "modified": "19970507"} in gov.abolished_laws()


def test_law_that_gets_abolished_later_is_disabled_not_deleted(g, allow_fake, srv):
    _serve_moj(srv)
    _download("moj")
    gov.set_selection("moj", ["T0000001"])
    gov.sync("moj")
    old = _versions("範例公文條例")[0]
    old_chunk = store.list_chunks(old["id"], limit=1)[0]["id"]
    _serve_moj(srv, law=law_zip(law1_modified="20260101", law1_abolish=True))
    _download("moj")
    s = gov.sync("moj")
    assert s["updated"] == 1 and s["abolished"] == 1
    vs = _versions("範例公文條例")
    assert len(vs) == 2 and {v["status"] for v in vs} == {"inactive"}
    # 舊草稿引用的段落還讀得到（只是不再被查到）
    got = retrieval.get_chunk(old_chunk, user_id=None)
    assert got is not None and got["active"] is False


# ---------------------------------------------------------------- 6. 檢查更新
def test_update_only_creates_a_version_when_modified_date_changes(g, allow_fake, srv):
    _serve_moj(srv)
    _download("moj")
    gov.set_selection("moj", ["T0000001", "T0000010"])
    assert gov.sync("moj")["new"] == 2
    again = gov.sync("moj")
    assert again["unchanged"] == 2 and again["new"] == 0 and again["updated"] == 0
    assert len(_versions("範例公文條例")) == 1
    # 整包重新下載（整包的更新日期變了）但每部法規的異動日期沒變 → 不建新版本
    _serve_moj(srv, law=law_zip())
    _download("moj")
    assert gov.sync("moj")["unchanged"] == 2
    # 第 1 部的異動日期變了 → 只有它建新版本，新的啟用、舊的停用
    _serve_moj(srv, law=law_zip(law1_modified="20260101", law1_extra="新增的條文。"))
    _download("moj")
    s = gov.sync("moj")
    assert s["updated"] == 1 and s["unchanged"] == 1
    vs = _versions("範例公文條例")
    assert len(vs) == 2
    assert sorted(v["status"] for v in vs) == ["active", "inactive"]
    assert next(v for v in vs if v["status"] == "active")["version_label"] == "民國115年1月1日修正"
    assert len(_versions("範例作業辦法")) == 1
    assert any(c["parent_ref"] == "第15條" for c in _chunks("範例公文條例"))


def test_deleted_dataset_is_recreated_on_next_import(g, allow_fake, srv):
    _serve_moj(srv)
    _download("moj")
    gov.set_selection("moj", ["T0000010"])
    gov.sync("moj")
    did = store.conn().execute("SELECT id FROM kb_datasets WHERE name='範例作業辦法'").fetchone()["id"]
    store.delete_dataset(did)
    assert store.gov_item("moj", "T0000010")["dataset_id"] is None
    assert gov.sync("moj")["new"] == 1
    assert _chunks("範例作業辦法")


# ---------------------------------------------------------------- 7. 擋下來的東西
def test_size_limit_stops_download_and_keeps_previous(g, allow_fake, srv, monkeypatch):
    _serve_moj(srv)
    _download("moj")
    before = gov.package_index("moj-law")["sha256"]
    monkeypatch.setitem(gov.PACKAGES["moj-law"], "max_mb", 0)
    with pytest.raises(gov.GovError) as e:
        gov.download_package("moj-law")
    assert "太大" in str(e.value) and "已停止下載" in str(e.value), "下載那一層就要停，不是收完再丟"
    assert gov.package_index("moj-law")["sha256"] == before
    # 手動上傳走同一個上限
    with pytest.raises(gov.GovError) as e:
        gov.install_package("moj-law", law_zip(), origin="upload")
    assert "太大" in str(e.value)
    assert gov.package_index("moj-law")["sha256"] == before


def test_zip_bomb_is_rejected_and_keeps_previous(g, allow_fake, srv):
    _serve_moj(srv)
    _download("moj")
    before = gov.package_index("moj-law")["sha256"]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("ChLaw.json", b"\0" * (70 * 1024 * 1024))
    srv.routes["/law.zip"] = (200, buf.getvalue())
    with pytest.raises(gov.GovError) as e:
        gov.download_package("moj-law")
    assert "異常" in str(e.value)
    assert gov.package_index("moj-law")["sha256"] == before


def test_xml_with_entities_is_rejected(g):
    evil = ('<?xml version="1.0"?><!DOCTYPE l [<!ENTITY a "aaaaaaaaaa">'
            '<!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;">]><Laws><Law><LawName>&b;</LawName></Law></Laws>')
    with pytest.raises(gov.GovError) as e:
        gov.install_package("moj-order", evil.encode("utf-8"), origin="upload")
    assert "DTD" in str(e.value)
    assert gov.package_index("moj-order") is None


def test_wrong_kind_of_file_is_rejected(g):
    with pytest.raises(gov.GovError):
        gov.install_package("moj-law", NDC_XML.encode("utf-8"), origin="upload")
    with pytest.raises(gov.GovError):
        gov.install_package("ey-mailbox", b"<html>not a pdf</html>", origin="upload")
    with pytest.raises(gov.GovError):
        gov.install_package("moj-law", b"garbage bytes", origin="upload")


def test_internal_address_is_blocked_before_connecting(g, srv, monkeypatch):
    srv.routes["/law.zip"] = (200, law_zip())
    _point("moj-law", srv.url("/law.zip"))
    # 沒有 allow_fake：127.0.0.1 是本機位址，一律擋 —— 而且是在連線之前
    with pytest.raises(gov.GovError):
        gov.download_package("moj-law")
    assert srv.hits == []
    with pytest.raises(gov.GovError):
        gov.set_package_urls("moj-law", {"url": "file:///etc/passwd"})


def test_alt_url_is_tried_when_primary_fails(g, allow_fake, srv):
    srv.routes["/old.zip"] = (200, sendlaw_zip())
    _point("moj-law", srv.url("/missing.zip"), srv.url("/old.zip"))
    info = gov.download_package("moj-law")
    assert info["origin"] == "alt_url" and info["count"] == 1
    gov.set_selection("moj", ["T0000001"])
    gov.sync("moj")
    refs = [c["parent_ref"] for c in _chunks("範例公文條例")]
    assert refs == ["第1條", "第12條之1"], "舊鏡像的中文標籤也要讀得懂"


def test_urls_are_editable_and_reset(g):
    p = gov.set_package_urls("ndc-rules-1", {"url": "https://example.gov.tw/新的.xml"})
    assert p["customized"] and p["url"].startswith("https://example.gov.tw/%E6")
    p = gov.reset_package("ndc-rules-1")
    assert not p["customized"] and p["url"] == gov.PACKAGES["ndc-rules-1"]["url"]


# ---------------------------------------------------------------- 8. 國發會規則與行政院釋例
def test_ndc_rules_one_segment_per_point(g, allow_fake, srv):
    srv.routes["/r1.xml"] = (200, NDC_XML.encode("utf-8"))
    srv.routes["/r2.xml"] = (200, NDC_XML.replace("GL900001", "GL900002")
                             .replace("範例文書作業規範", "範例格式參考規範").encode("utf-8"))
    _point("ndc-rules-1", srv.url("/r1.xml"))
    _point("ndc-rules-2", srv.url("/r2.xml"))
    _download("ndc")
    assert gov.get_selection("ndc") == [], "預設名稱在假資料裡一個都沒有"
    gov.set_selection("ndc", ["GL900001"])
    s = gov.sync("ndc")
    assert s["new"] == 1
    ch = _chunks("範例文書作業規範")
    assert [c["parent_ref"] for c in ch] == ["一", "二", "三"], "每一點自己一段（短的一章也要拆開）"
    assert ch[0]["heading_path"][0] == "範例文書作業規範"
    assert "一、全程管制：\n（一）公文應全程列入管制。" == ch[0]["text"]
    assert not any("Download.ashx" in c["text"] for c in ch)
    hits = retrieval.search("稽催方式以公文系統通知承辦人", user_id=None)
    assert hits and hits[0]["purpose"] == "format_reference"
    v = _versions("範例文書作業規範")[0]
    assert v["version_label"] == "民國109年12月11日修正"
    assert store.gov_version(v["id"])["attribution"].startswith("國家發展委員會，")


def test_ey_pdf_import_reads_the_update_date(g, allow_fake, srv):
    srv.routes["/ey1.pdf"] = (200, ey_pdf("115.8.31"))
    srv.routes["/ey2.pdf"] = (200, ey_pdf("115.4.30"))
    _point("ey-mailbox", srv.url("/ey1.pdf"))
    _point("ey-interp", srv.url("/ey2.pdf"))
    _download("ey")
    s = gov.sync("ey")
    assert s["new"] == 2
    v = _versions("文書處理相關釋例（院長電子信箱）")[0]
    assert v["status"] == "active" and v["version_label"] == "民國115年8月31日更新"
    assert v["published_on"] == "2026-08-31" and v["ext"] == ".pdf"
    assert "（115年8月31日更新）" in store.gov_version(v["id"])["attribution"]
    assert gov.sync("ey")["unchanged"] == 2
    srv.routes["/ey1.pdf"] = (200, ey_pdf("115.9.30"))
    gov.download_package("ey-mailbox")
    assert gov.sync("ey")["updated"] == 1


# ---------------------------------------------------------------- 9. 背景作業與管理端點
def _wait_idle(c, timeout=60.0) -> dict:
    end = time.time() + timeout
    while time.time() < end:
        st = c.get("/admin/knowledge/api/gov/status").json()
        if not st["running"]:
            return st
        time.sleep(0.1)
    raise AssertionError("背景作業沒有在時限內結束")


def test_routes_download_select_import_and_audit(g, allow_fake, srv, admin_session):
    c, _, _ = admin_session
    _serve_moj(srv)
    r = c.post("/admin/knowledge/api/gov/moj/import")
    assert r.status_code == 400, "還沒下載不可以匯入"
    r = c.post("/admin/knowledge/api/gov/moj/download")
    assert r.status_code == 200 and r.json()["job_id"]
    st = _wait_idle(c)
    m = {x["id"]: x for x in st["groups"]}["moj"]
    assert m["available"] == 4 and m["last"]["ok"]
    r = c.post("/admin/knowledge/api/gov/moj/selection", json={"keys": ["T0000001"]})
    assert r.status_code == 200
    items = c.get("/admin/knowledge/api/gov/moj/selection").json()["items"]
    assert [i["key"] for i in items] == ["T0000001"]
    r = c.post("/admin/knowledge/api/gov/moj/import")
    assert r.status_code == 200
    st = _wait_idle(c)
    m = {x["id"]: x for x in st["groups"]}["moj"]
    assert m["imported_count"] == 1 and m["last"]["summary"]["new"] == 1
    did = store.gov_item("moj", "T0000001")["dataset_id"]
    vs = c.get(f"/admin/knowledge/api/datasets/{did}/versions").json()["versions"]
    assert vs[0]["gov"]["license"] == "政府資料開放授權條款－第1版"
    assert "法務部全國法規資料庫" in vs[0]["gov"]["attribution"]
    # 量很大要確認（回 409，前端再帶 confirm 送一次）
    import app.core.kb.gov as gm
    old = gm.WARN_ITEMS
    gm.WARN_ITEMS = 1
    try:
        r = c.post("/admin/knowledge/api/gov/moj/selection", json={"keys": ["T0000001", "T0000010"]})
        assert r.status_code == 409 and r.json()["need_confirm"] and r.json()["count"] == 2
        r = c.post("/admin/knowledge/api/gov/moj/selection",
                   json={"keys": ["T0000001", "T0000010"], "confirm": True})
        assert r.status_code == 200
    finally:
        gm.WARN_ITEMS = old
    r = c.post("/admin/knowledge/api/gov/packages/moj-law", json={"url": "ftp://x/y"})
    assert r.status_code == 400
    r = c.post("/admin/knowledge/api/gov/packages/nope", json={"url": "https://a.gov.tw/"})
    assert r.status_code == 404
    from app.core import audit_db
    rows = []
    for _ in range(100):
        rows = audit_db.conn().execute(
            "SELECT event_type, details_json FROM audit_events WHERE target='knowledge_gov'").fetchall()
        if len({r_["event_type"] for r_ in rows}) >= 2:
            break
        time.sleep(0.05)
    kinds = {r_["event_type"] for r_ in rows}
    assert {"kb_import", "settings_change"} <= kinds
    assert any('"download"' in r_["details_json"] for r_ in rows)


def test_upload_goes_through_the_same_checks(g, admin_session):
    c, _, _ = admin_session
    r = c.post("/admin/knowledge/api/gov/packages/moj-order/upload",
               files={"file": ("ChOrder.zip", order_zip(), "application/zip")})
    assert r.status_code == 200
    st = _wait_idle(c)
    p = {x["id"]: x for x in st["groups"][0]["packages"]}["moj-order"]
    assert p["downloaded"] and p["count"] == 1 and p["origin"] == "upload"
    r = c.post("/admin/knowledge/api/gov/packages/moj-law/upload",
               files={"file": ("x.zip", b"not a zip", "application/zip")})
    assert r.status_code == 200
    st = _wait_idle(c)
    p = {x["id"]: x for x in st["groups"][0]["packages"]}["moj-law"]
    assert not p["downloaded"] and p["last"] and not p["last"]["ok"]


def test_only_one_job_at_a_time(g):
    gov._RUNNING.update({"group": "moj", "action": "download", "started_at": time.time(),
                         "job_id": ""})
    with pytest.raises(gov.GovBusy):
        gov.start("ndc", "download")


# ---------------------------------------------------------------- 10. 一般切段不變、重建索引
def test_default_chunking_is_unchanged():
    from app.core.kb.extract import extract
    lines = extract(HANDBOOK_TXT.encode("utf-8"), ".txt").lines
    assert [c["parent_ref"] for c in chunker.chunk(lines)] == ["一～二", "十八～十九", "六十二"]
    assert [c["parent_ref"] for c in chunker.chunk(lines, keep_articles=True)] == [
        "一", "二", "十八", "十九", "六十二"]
    assert chunker.CHUNKER_VERSION == "1"


def test_rebuild_rechunks_only_when_the_law_format_version_changes(g, allow_fake, srv, monkeypatch):
    from app.core.kb import indexer
    from tests._kb_support import FakeEmbed, make_pdf, setup_embed
    _serve_moj(srv)
    _download("moj")
    gov.set_selection("moj", ["T0000001"])
    gov.sync("moj")
    ds = store.create_dataset({"name": "上傳的手冊", "category": "writing_rules"})
    from tests._kb_support import add_doc
    add_doc(ds["id"], HANDBOOK_TXT.encode("utf-8"), ".txt")
    calls: list[str] = []
    real = law_text.chunks_for
    monkeypatch.setattr(law_text, "chunks_for", lambda fmt, data: calls.append(fmt) or real(fmt, data))
    with FakeEmbed() as fe:
        setup_embed(fe.base)
        indexer.rebuild()
        assert calls == [], "版本沒變不可以重新切段"
        monkeypatch.setitem(law_text.FORMAT_VERSIONS, "law", "law2")
        indexer.rebuild()
        assert calls == ["law"]
    vers = {r["chunker_version"] for r in store.conn().execute(
        "SELECT chunker_version FROM kb_versions").fetchall()}
    assert vers == {"1", "1+law2"}
    assert [c["parent_ref"] for c in _chunks("範例公文條例")][:3] == ["第1條", "第2條", "第12條之1"]


def test_migration_from_v1_keeps_existing_data(kb_isolated):  # noqa: F811
    root = store.kb_dir()
    root.mkdir(parents=True, exist_ok=True)
    path = root / "kb.sqlite"
    con = sqlite3.connect(path)
    store._m1(con)
    now = time.time()
    con.execute("INSERT INTO kb_datasets(id, name, category, created_at, updated_at) VALUES (?,?,?,?,?)",
                ("a" * 32, "既有資料集", "writing_rules", now, now))
    con.execute("PRAGMA user_version=1")
    con.commit()
    con.close()
    store._INITED.discard(str(path))
    assert store.get_dataset("a" * 32)["name"] == "既有資料集"
    names = {r[0] for r in store.conn().execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert {"kb_gov_items", "kb_gov_versions"} <= names
    assert store.conn().execute("PRAGMA user_version").fetchone()[0] == 2


# ---------------------------------------------------------------- 11. 讀檔的細節
def test_json_is_read_record_by_record(tmp_path, monkeypatch):
    p = tmp_path / "law.zip"
    p.write_bytes(law_zip())
    monkeypatch.setattr(gp, "_READ", 97)        # 很小的緩衝：每一筆都要跨好幾次讀取
    uds = []
    recs = list(gp.iter_records(p, on_update_date=uds.append))
    assert [r["key"] for r in recs] == ["T0000001", "T0000002", "T0000003"]
    assert uds == ["2026/9/24 上午 12:00:00"]
    assert recs[0]["articles"][4]["no"] == "第 12-1 條"
    assert recs[0]["history_n"] == 2
    bad = tmp_path / "bad.json"
    bad.write_bytes(json.dumps({"Laws": [{"LawName": "甲"}]}, ensure_ascii=False).encode()[:-3])
    with pytest.raises(gp.PackageError):
        list(gp.iter_records(bad))
