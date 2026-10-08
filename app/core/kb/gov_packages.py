"""讀政府公開資料的下載檔：全國法規資料庫（JSON / XML 兩種標籤）、國發會行政規則 XML。

## 收哪幾種（看內容判斷，不看網址）

| 來源 | 外層 | 內容 |
|---|---|---|
| `law.moj.gov.tw/api/ch/law/json` | zip | `ChLaw.json`：`{"UpdateDate", "Laws": [...]}` |
| `law.moj.gov.tw/api/ch/order/xml` | zip | `ChOrder.xml`：`<Laws><Law><LawName>…` |
| `sendlaw.moj.gov.tw/PublicData/…`（舊鏡像） | zip | `FalV.xml`：`<LAWS><法規><法規名稱>…<法規內容><條文>…` |
| 國發會主管行政規則（data.gov.tw 39506） | 無 | `<LAWS><法規><法規名稱>…<法規內容>全文</法規內容>` |

網址可以改（管理員可能把命令改成 JSON、或改用舊鏡像），所以**格式看內容判斷**。

## 記憶體

命令的 XML 解開 112 MB、JSON 版更大（整份 `json.loads` 實測尖峰 265 MB）。這裡兩種都
**一筆一筆讀**：XML 用 `iterparse` 讀完一筆就清掉，JSON 自己逐筆 `raw_decode`
（緩衝最多幾 MB）。實測命令 XML 整份掃一遍約 6 秒，整個行程最高約 56 MB。

## 安全

* XML 一律走 `defusedxml`（不收 DTD、實體 —— 實體展開可以把幾 KB 撐成幾 GB）。
* zip 先過 `zip_guard`（解開後的總量上限由呼叫端給，比全站預設小）。
* 讀進來的字串只拿來顯示與存成文字，**沒有任何一個欄位會變成檔案路徑**
  （存檔的名稱一律是我們產生的 id）。
"""
from __future__ import annotations

import io
import json
import re
import zipfile
from pathlib import Path
from typing import Callable, Iterator, Optional

from .. import zip_guard

#: 解開後的總量上限（命令 XML 實際約 120 MB）。
MAX_UNCOMPRESSED = 400 * 1024 * 1024
#: JSON 逐筆讀時，單一筆最多可以多大（實際最大的一部約 0.5 MB）。
MAX_RECORD_CHARS = 32 * 1024 * 1024
#: 一個下載檔最多幾筆（實際：法律 1,347、命令 10,451）。
MAX_RECORDS = 50_000
_READ = 1 << 20


class PackageError(ValueError):
    """下載檔的內容不合格（訊息給管理員看）。"""


_PCODE_RE = re.compile(r"[?&]pcode=([A-Za-z0-9]{1,20})")
_ID_RE = re.compile(r"[?&]id=([A-Za-z0-9]{1,20})")


def key_of(url: str, name: str) -> str:
    """穩定的識別：全國法規資料庫用 pcode（改名也不變）、國發會用 `id=`，都沒有才用名稱。"""
    m = _PCODE_RE.search(url or "") or _ID_RE.search(url or "")
    if m:
        return m.group(1).upper()
    return "N:" + re.sub(r"\s+", "", name or "")[:80]


def _txt(s: Optional[str]) -> str:
    return (s or "").strip()


def _digits(s: Optional[str]) -> str:
    d = re.sub(r"\D", "", s or "")
    return d if len(d) == 8 else ""


_HIST_RE = re.compile(r"^\s*\d{1,3}\s*[.．、]", re.M)


def _history_n(s: Optional[str]) -> int:
    """沿革有幾筆（「1.…公布」「2.…修正」）—— 版本標籤寫「公布」還是「修正」用。"""
    return len(_HIST_RE.findall(s or ""))


# ---------------------------------------------------------------- 開檔
def _open_payload(path: Path, *, max_uncompressed: int) -> tuple[io.BufferedIOBase, str, Optional[zipfile.ZipFile]]:
    """回 (二進位串流, 格式 'json'|'xml', zip 物件或 None)。呼叫端負責關閉。"""
    with open(path, "rb") as fh:
        head = fh.read(8)
    if head.startswith(b"PK"):
        try:
            zf = zipfile.ZipFile(path)
        except (zipfile.BadZipFile, OSError, ValueError):
            raise PackageError("zip 檔毀損，打不開") from None
        try:
            zip_guard.check(zf, max_uncompressed=max_uncompressed)
        except zip_guard.ZipBombError as e:
            zf.close()
            raise PackageError(str(e)) from None
        members = [i for i in zf.infolist() if not i.is_dir()
                   and i.filename.lower().endswith((".json", ".xml"))]
        if not members:
            zf.close()
            raise PackageError("zip 裡沒有 .json 或 .xml 的資料檔")
        info = max(members, key=lambda i: i.file_size)
        kind = "json" if info.filename.lower().endswith(".json") else "xml"
        try:
            return zf.open(info), kind, zf
        except (zipfile.BadZipFile, OSError, NotImplementedError, RuntimeError):
            zf.close()
            raise PackageError("zip 檔毀損，打不開") from None
    fh = open(path, "rb")
    first = head.lstrip(b"\xef\xbb\xbf \r\n\t")[:1]
    if first == b"{" or first == b"[":
        return fh, "json", None
    if first == b"<":
        return fh, "xml", None
    fh.close()
    raise PackageError("看不出檔案格式（要 zip、JSON 或 XML）")


def iter_records(path: Path, *, max_uncompressed: int = MAX_UNCOMPRESSED,
                 on_update_date: Optional[Callable[[str], None]] = None,
                 cancelled: Optional[Callable[[], bool]] = None) -> Iterator[dict]:
    """逐筆讀出法規 / 行政規則（正規化成同一種紀錄）。

    紀錄：`{"key","name","level","url","category","modified","effective","effective_note",
    "abolished","foreword","attachments":[{name,url}],"articles":[{type,no,content}] | "text"}`。
    """
    stream, kind, zf = _open_payload(path, max_uncompressed=max_uncompressed)
    n = 0
    try:
        gen = _iter_json(stream, on_update_date) if kind == "json" else _iter_xml(stream, on_update_date)
        for rec in gen:
            n += 1
            if n > MAX_RECORDS:
                raise PackageError(f"資料筆數超過上限（{MAX_RECORDS} 筆）")
            if cancelled and n % 200 == 0 and cancelled():
                raise PackageError("已取消。")
            yield rec
    finally:
        try:
            stream.close()
        finally:
            if zf is not None:
                zf.close()
    if n == 0:
        raise PackageError("檔案裡沒有任何一筆法規資料")


# ---------------------------------------------------------------- JSON（逐筆）
_LAWS_KEY_RE = re.compile(r'"Laws"\s*:\s*\[')
_UPDATE_RE = re.compile(r'"UpdateDate"\s*:\s*"([^"]{0,60})"')


def _iter_json(stream, on_update_date) -> Iterator[dict]:
    text = io.TextIOWrapper(stream, encoding="utf-8-sig", errors="replace")
    dec = json.JSONDecoder()
    buf = ""
    eof = False

    def more() -> None:
        nonlocal buf, eof
        chunk = text.read(_READ)
        if not chunk:
            eof = True
        buf += chunk

    m = None
    while m is None:
        m = _LAWS_KEY_RE.search(buf)
        if m is None:
            if eof:
                raise PackageError("看不出是全國法規資料庫的 JSON（找不到 Laws 清單）")
            if len(buf) > 8 * _READ:
                raise PackageError("看不出是全國法規資料庫的 JSON（找不到 Laws 清單）")
            more()
    um = _UPDATE_RE.search(buf[:m.start()])
    if um and on_update_date:
        on_update_date(um.group(1))
    pos = m.end()
    while True:
        while True:
            while pos < len(buf) and buf[pos] in " \t\r\n,":
                pos += 1
            if pos < len(buf) or eof:
                break
            buf, pos = buf[pos:], 0
            more()
        if pos >= len(buf):
            raise PackageError("JSON 不完整（檔案被截斷？）")
        if buf[pos] == "]":
            return
        try:
            obj, end = dec.raw_decode(buf, pos)
        except json.JSONDecodeError:
            if eof:
                raise PackageError("JSON 格式不正確") from None
            buf, pos = buf[pos:], 0
            if len(buf) > MAX_RECORD_CHARS:
                raise PackageError("單一筆法規資料大得不合理，已停止") from None
            more()
            continue
        pos = end
        if pos > 4 * _READ:
            buf, pos = buf[pos:], 0
        if isinstance(obj, dict):
            rec = _from_json(obj)
            if rec:
                yield rec


def _from_json(o: dict) -> Optional[dict]:
    name = _txt(o.get("LawName"))
    if not name:
        return None
    url = _txt(o.get("LawURL"))
    arts = []
    raw_arts = o.get("LawArticles")
    if isinstance(raw_arts, list):
        for a in raw_arts:
            if isinstance(a, dict):
                arts.append({"type": "C" if _txt(a.get("ArticleType")) == "C" else "A",
                             "no": _txt(a.get("ArticleNo")),
                             "content": str(a.get("ArticleContent") or "")})
    atts = []
    for a in o.get("LawAttachements") or []:
        if isinstance(a, dict) and _txt(a.get("FileURL")):
            atts.append({"name": _txt(a.get("FileName")), "url": _txt(a.get("FileURL"))})
    return {
        "key": key_of(url, name), "name": name, "level": _txt(o.get("LawLevel")),
        "url": url, "category": _txt(o.get("LawCategory")),
        "modified": _digits(o.get("LawModifiedDate")),
        "effective": _digits(o.get("LawEffectiveDate")),
        "effective_note": _txt(o.get("LawEffectiveNote")),
        "abolished": "廢" in _txt(o.get("LawAbandonNote")),
        "foreword": str(o.get("LawForeword") or ""),
        "history_n": _history_n(o.get("LawHistories")),
        "attachments": atts, "articles": arts,
    }


# ---------------------------------------------------------------- XML（逐筆）
_ITEM_TAGS = ("Law", "法規")
_FIELDS = {
    # 正規化欄位 → (英文標籤, 中文標籤…)
    "name": ("LawName", "法規名稱"),
    "level": ("LawLevel", "法規位階", "法規性質"),
    "url": ("LawURL", "法規網址"),
    "category": ("LawCategory", "法規類別"),
    "modified": ("LawModifiedDate", "法規異動日期", "最新異動日期"),
    "effective": ("LawEffectiveDate", "生效日期"),
    "effective_note": ("LawEffectiveNote", "生效內容"),
    "abolished": ("LawAbandonNote", "廢止註記"),
    "foreword": ("LawForeword", "前言"),
    "histories": ("LawHistories", "沿革內容"),
}


def _field(el, key: str) -> str:
    for tag in _FIELDS[key]:
        v = el.find(tag)
        if v is not None:
            return "".join(v.itertext())
    return ""


def _attachments(el) -> list[dict]:
    names, urls = [], []
    for tag in ("LawAttachements", "附件"):
        for box in el.findall(tag):
            for sub in box.iter():
                if sub.tag in ("FileName", "檔案名稱"):
                    names.append(_txt(sub.text))
                elif sub.tag in ("FileURL", "下載網址"):
                    urls.append(_txt(sub.text))
    out = []
    for i, u in enumerate(urls):
        if u:
            out.append({"name": names[i] if i < len(names) else "", "url": u})
    return out


def _articles(el) -> tuple[list[dict], Optional[str]]:
    """（條文清單, 全文）—— 結構化的給條文清單，國發會那種只有全文的給全文。"""
    box = el.find("LawArticles")
    if box is not None:
        arts = []
        for a in box.findall("Article"):
            content = a.find("ArticleContent")
            if content is None:
                content = a.find("ArticleConctent")     # 官方 XML 的拼字就是這樣
            arts.append({"type": "C" if _txt(a.findtext("ArticleType")) == "C" else "A",
                         "no": _txt(a.findtext("ArticleNo")),
                         "content": "".join(content.itertext()) if content is not None else ""})
        return arts, None
    body = el.find("法規內容")
    if body is None:
        return [], None
    if len(body):
        arts = []
        for sub in body:
            if sub.tag == "編章節":
                arts.append({"type": "C", "no": "", "content": "".join(sub.itertext())})
            elif sub.tag == "條文":
                c = sub.find("條文內容")
                arts.append({"type": "A", "no": _txt(sub.findtext("條號")),
                             "content": "".join(c.itertext()) if c is not None else ""})
        return arts, None
    return [], "".join(body.itertext())


def _iter_xml(stream, on_update_date) -> Iterator[dict]:
    from defusedxml.ElementTree import iterparse
    root = None
    try:
        for ev, el in iterparse(stream, events=("start", "end"), forbid_dtd=True):
            if ev == "start":
                if root is None:
                    root = el
                    ud = el.attrib.get("UpdateDate") or el.attrib.get("匯出時間") or ""
                    if ud and on_update_date:
                        on_update_date(ud[:60])
                continue
            if el.tag not in _ITEM_TAGS:
                continue
            name = _txt(_field(el, "name"))
            if name:
                url = _txt(_field(el, "url"))
                arts, text = _articles(el)
                rec = {
                    "key": key_of(url, name), "name": name,
                    "level": _txt(_field(el, "level")), "url": url,
                    "category": _txt(_field(el, "category")),
                    "modified": _digits(_field(el, "modified")),
                    "effective": _digits(_field(el, "effective")),
                    "effective_note": _txt(_field(el, "effective_note")),
                    "abolished": "廢" in _txt(_field(el, "abolished")),
                    "foreword": _field(el, "foreword"),
                    "history_n": _history_n(_field(el, "histories")),
                    "attachments": _attachments(el), "articles": arts,
                }
                if text is not None:
                    rec["text"] = text
                yield rec
            el.clear()
            if root is not None:
                root.clear()
    except PackageError:
        raise
    except Exception as e:      # noqa: BLE001 — expat / defusedxml 的各種例外
        name = type(e).__name__
        if "Forbidden" in name or "DTD" in name or "Entities" in name:
            raise PackageError("XML 含有 DTD 或實體宣告，為了安全拒絕處理") from None
        raise PackageError("XML 格式不正確") from None


def index_entry(rec: dict, package_id: str) -> dict:
    """紀錄 → 清單用的摘要（不含內文）。"""
    arts = [a for a in rec.get("articles") or [] if a.get("type") == "A"]
    chars = sum(len(a.get("content") or "") for a in arts) + len(rec.get("text") or "")
    return {
        "key": rec["key"], "name": rec["name"][:200], "level": rec.get("level", "")[:40],
        "category": rec.get("category", "")[:120], "modified": rec.get("modified", ""),
        "effective": rec.get("effective", ""), "abolished": bool(rec.get("abolished")),
        "articles": len(arts), "chars": chars, "pkg": package_id,
    }


# ---------------------------------------------------------------- PDF（行政院釋例）
_ROC_UPDATE_RE = re.compile(r"更新日期\s*[:：]\s*(\d{2,3})\s*[.．/年]\s*(\d{1,2})\s*[.．/月]\s*(\d{1,2})")


def pdf_info(data: bytes) -> dict:
    """PDF 的頁數與第一頁寫的「更新日期：115.8.31」（換成西元 `20260831`）。"""
    import fitz
    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception:
        raise PackageError("不是 PDF 檔，或檔案毀損") from None
    try:
        if doc.page_count < 1:
            raise PackageError("PDF 沒有任何一頁")
        head = ""
        for i in range(min(2, doc.page_count)):
            head += doc[i].get_text() or ""
        m = _ROC_UPDATE_RE.search(head)
        updated = ""
        if m:
            y, mo, d = int(m.group(1)) + 1911, int(m.group(2)), int(m.group(3))
            if 1 <= mo <= 12 and 1 <= d <= 31:
                updated = f"{y:04d}{mo:02d}{d:02d}"
        return {"pages": doc.page_count, "updated": updated}
    finally:
        doc.close()
