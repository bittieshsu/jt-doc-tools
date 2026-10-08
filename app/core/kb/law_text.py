"""政府公開資料（法規、行政規則）存進知識庫的格式，與「一條一段」的切法。

## 為什麼不直接丟給一般的切段

一般上傳的文件只有排版，切段要從行首的項次猜結構（`chunker.py`）。全國法規資料庫
給的是**結構化**的資料：哪一列是章節（`ArticleType=C`）、哪一列是條文（`A`）、
條號是什麼。照著結構切，每一條就是**剛好一段**：

* 引用時講得出「第 12 條之 1」—— 一般切法會把短的幾條裝在同一段（上限 800 字），
  那一段的母條文就只能寫「第 1～4 條」，模型引用時會說不出是哪一條。
* 上層標題是「法規名稱 ＞ 第一章 總則 ＞ 第一節 法例」—— 問題裡寫了法規名稱
  （「行政程序法的送達」）時，名稱只出現在標題裡，標題要進索引才查得到。
* 一條超過 `chunker.CHUNK_MAX` 才拆，**拆出來的每一塊都帶著同一個條號**。

## 存進去的「原檔」

知識庫每一份文件都要有原檔（重新切段、下載原檔都靠它）。法規存成我們自己寫的
**Markdown**：人打開來看得懂（下載原檔就是一份可讀的法規全文），程式也能**原樣讀回**
（重建索引時從原檔重新切段，結果要跟第一次一模一樣）：

```
# 法規名稱
- 法規位階：法律
- …（其他欄位，一行一個）
---
## 第 一 章 總則          ← 章節（C）
### 第 12 條之 1          ← 條文（A）
條文的一項一行
```

內文的行若剛好以 `#`、`\\` 開頭或整行是 `---`，前面補一個 `\\`（讀回時拿掉）——
不然一行「# 號」的條文內容會被讀成標題。

行政規則（國發會）沒有結構化的條號，只有一段全文（裡面寫著「一、」「（一）」）：
存成同樣的檔頭 ＋ 全文，切段時走一般的樹狀切法、但**每一點自己一段**
（`chunker.chunk(keep_articles=True)`）。

## 條號

* `第 12-1 條` → `第 12 條之 1`（全國法規資料庫約 3,300 條法律、3,500 條命令是
  這種寫法；一般切段的規則認不得連字號，條號會變成「第 12 條」接一段怪文字）。
* 條號只有數字（命令裡約 900 條，例如 `1`）：內文開頭若是「一、」「（一）」，
  那就是點次本身（不另外加標題）；不是的話（多半是表格）就不寫條號。

## 硬換行

命令的 XML 把條文內容**照固定寬度斷行**、下一行縮排四格（「…如期實施憲政\\n
    案及國家總動員法…」）。不接起來的話「憲政案」三個字跨兩行，關鍵字檢索的
相鄰兩字（「政案」）就不見了。判準：縮排開頭、而且不是項次（「一、」「（一）」
「1.」…）的行，接回上一行。用框線字畫的表格（┌─┐│）原樣保留（接起來就不成表了）。
"""
from __future__ import annotations

import re
import unicodedata
from typing import Iterable, Optional

from . import chunker
from .extract import Line

#: 存成知識庫原檔時的格式代碼（記在 `kb_gov_versions.format`，處理時照它分流）。
FORMAT_LAW = "law"
FORMAT_RULES = "rules"
FORMATS = (FORMAT_LAW, FORMAT_RULES)

#: 這兩種切法自己的版本。**改了 `law_chunks()` / `rules_chunks()` 的規則就加一** ——
#: 存進資料庫的 `chunker_version` 是「一般切段版本 ＋ 這個」，重建索引時版本不同的會重新切段。
FORMAT_VERSIONS = {FORMAT_LAW: "law1", FORMAT_RULES: "rules1"}

_HEAD_END = "---"
_META_RE = re.compile(r"^- ([^：]{1,20})：(.*)$")
_BOX_RE = re.compile(r"[┌┐└┘├┤┬┴┼─│━┃╋]")
#: 項次（行首）：「一、」「（一）」「(一)」「1.」「1、」「(1)」「１．」「第一款」
_ITEM_RE = re.compile(
    r"^(?:[一二三四五六七八九十百]{1,4}\s*[、．.]"
    r"|[（(]\s*[一二三四五六七八九十百\d]{1,4}\s*[）)]"
    r"|\d{1,3}\s*[、．.](?!\d)"
    r"|第\s*[一二三四五六七八九十百\d]{1,4}\s*[款目項])")
_CN_NUM = "一二三四五六七八九十百零〇"
_BIG_RE = re.compile(rf"^第\s*[\d{_CN_NUM}]+\s*(編|章|節|款|目)")
_BIG_RANK = {"編": 0, "章": 1, "節": 2, "款": 3, "目": 4}
_ART_HYPHEN_RE = re.compile(r"^第\s*(\d+)\s*-\s*(\d+)\s*條$")
_ART_PLAIN_RE = re.compile(r"^第\s*(\d+)\s*條$")


# ---------------------------------------------------------------- 日期
def iso_date(yyyymmdd: str) -> str:
    """`20070321` → `2007-03-21`；不是合法日期回空字串。"""
    s = re.sub(r"\D", "", yyyymmdd or "")
    if len(s) != 8:
        return ""
    y, m, d = int(s[:4]), int(s[4:6]), int(s[6:])
    if not (1900 <= y <= 2200 and 1 <= m <= 12 and 1 <= d <= 31):
        return ""
    import datetime
    try:
        datetime.date(y, m, d)
    except ValueError:
        return ""
    return f"{y:04d}-{m:02d}-{d:02d}"


def roc_date(yyyymmdd: str) -> str:
    """`20070321` → `民國96年3月21日`；不是合法日期回空字串。"""
    iso = iso_date(yyyymmdd)
    if not iso:
        return ""
    y, m, d = (int(x) for x in iso.split("-"))
    if y <= 1911:
        return ""
    return f"民國{y - 1911}年{m}月{d}日"


#: 全國法規資料庫用 `99991231` 表示「施行日期由行政院（以命令）定之」—— **不是**生效日期。
UNDETERMINED_EFFECTIVE = "99991231"


# ---------------------------------------------------------------- 條號與內文
def article_label(no: str) -> str:
    """條號整理成顯示用的寫法：`第 12-1 條` → `第 12 條之 1`；只有數字回空字串。"""
    s = unicodedata.normalize("NFKC", (no or "")).strip()
    if not s or re.fullmatch(r"\d+", s):
        return ""
    m = _ART_HYPHEN_RE.match(s)
    if m:
        return f"第 {int(m.group(1))} 條之 {int(m.group(2))}"
    m = _ART_PLAIN_RE.match(s)
    if m:
        return f"第 {int(m.group(1))} 條"
    return re.sub(r"\s+", " ", s)


# ---------------------------------------------------------------- 法規位階
# 國發會的行政規則 XML 寫的是代碼：`05:行政規則§159,II,1`（行政程序法第 159 條第 2 項第 1 款）。
# 原樣列在篩選與清單上看不懂（2026-10-08 使用者截圖：「這顯示怪怪的」）。
_LEVEL_CODE_RE = re.compile(r"^\d+\s*[:：]\s*")
_LEVEL_SECTION_RE = re.compile(r"^(.*?)\s*§\s*(\d+)\s*,\s*([IVXLC]+)\s*,\s*(\d+)$")
_ROMAN = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100}
#: 行政程序法第 159 條第 2 項：第 1 款是機關內部的規定、第 2 款是解釋性規定與裁量基準
_LEVEL_KINDS = {(159, 2, 1): "機關內部規定", (159, 2, 2): "解釋性規定、裁量基準"}


def _roman(s: str) -> int:
    total = 0
    for i, ch in enumerate(s):
        v = _ROMAN[ch]
        total += -v if i + 1 < len(s) and _ROMAN[s[i + 1]] > v else v
    return total


def _level_parts(raw: str) -> tuple[str, Optional[tuple[int, int, int]]]:
    s = _LEVEL_CODE_RE.sub("", unicodedata.normalize("NFKC", raw or "").strip())
    m = _LEVEL_SECTION_RE.match(s)
    if not m:
        return s, None
    return m.group(1).strip(), (int(m.group(2)), _roman(m.group(3)), int(m.group(4)))


def level_label(raw: str) -> str:
    """篩選與清單用的短名稱：`05:行政規則§159,II,1` → `機關內部規定`；法律、命令照原樣。"""
    head, sec = _level_parts(raw)
    if sec is None:
        return head
    return _LEVEL_KINDS.get(sec) or f"{head}（第 {sec[0]} 條第 {sec[1]} 項第 {sec[2]} 款）"


def level_basis(raw: str) -> str:
    """短名稱背後的依據（滑鼠移上去看）；不是代碼的回空字串。"""
    _, sec = _level_parts(raw)
    return f"行政程序法第 {sec[0]} 條第 {sec[1]} 項第 {sec[2]} 款" if sec else ""


def level_text(raw: str) -> str:
    """寫進知識庫內文的完整寫法：`行政規則（行政程序法第 159 條第 2 項第 1 款：機關內部規定）`。"""
    head, sec = _level_parts(raw)
    if sec is None:
        return head
    kind = _LEVEL_KINDS.get(sec)
    return f"{head}（{level_basis(raw)}{'：' + kind if kind else ''}）"


def _is_box(line: str) -> bool:
    return bool(_BOX_RE.search(line))


def unwrap(content: str) -> list[str]:
    """條文內容 → 一項一行（接回硬換行，表格保留）。"""
    out: list[str] = []
    for raw in (content or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if not raw.strip():
            continue
        if _is_box(raw):
            out.append(raw.rstrip())
            continue
        indented = raw[:1] in (" ", "\t")
        t = raw.strip()
        if indented and out and not _is_box(out[-1]) and not _ITEM_RE.match(
                unicodedata.normalize("NFKC", t)):
            out[-1] = chunker._join(out[-1], t)
        else:
            out.append(t)
    return out


def _clean(s: object, limit: int = 2000) -> str:
    s = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", str(s or ""))
    return re.sub(r"\s+", " ", s).strip()[:limit]


def _escape(line: str) -> str:
    return "\\" + line if line.startswith(("#", "\\")) or line == _HEAD_END else line


def _unescape(line: str) -> str:
    return line[1:] if line.startswith("\\") else line


# ---------------------------------------------------------------- 寫出
def _header(name: str, meta: list[tuple[str, str]]) -> list[str]:
    out = ["# " + _clean(name, 300)]
    for k, v in meta:
        v = _clean(v, 4000)
        if v:
            out.append(f"- {k}：{v}")
    out.append(_HEAD_END)
    return out


def render_law(rec: dict, *, attribution: str = "") -> str:
    """一部法規（`gov_packages` 的紀錄）→ 存進知識庫的 Markdown。"""
    eff = rec.get("effective") or ""
    meta = [
        ("法規位階", level_text(rec.get("level") or "")),
        ("法規類別", rec.get("category") or ""),
        ("法規代碼", rec.get("key") or ""),
        ("異動日期", roc_date(rec.get("modified") or "")),
        ("生效日期", roc_date(eff) if eff != UNDETERMINED_EFFECTIVE else "由主管機關另定"),
        ("施行說明", rec.get("effective_note") or ""),
        ("廢止", "已廢止" if rec.get("abolished") else ""),
        ("法規網址", rec.get("url") or ""),
        ("資料來源", attribution),
    ]
    lines = _header(rec.get("name") or "", meta)
    fore = unwrap(rec.get("foreword") or "")
    if fore:
        lines += ["", "## 前言"] + [_escape(p) for p in fore]
    for a in rec.get("articles") or []:
        if a.get("type") == "C":
            t = _clean(a.get("content"), 300)
            if t:
                lines += ["", "## " + t]
            continue
        paras = unwrap(a.get("content") or "")
        label = article_label(a.get("no") or "")
        if not paras and not label:
            continue
        lines += ["", "### " + (label or "　")] + [_escape(p) for p in paras]
    atts = [x for x in rec.get("attachments") or [] if x.get("url")]
    if atts:
        lines += ["", "## 附件（只列連結，內容沒有放進知識庫）"]
        for x in atts:
            lines.append(_escape(f"- {_clean(x.get('name'), 200) or '附件'}：{_clean(x.get('url'), 1000)}"))
    return "\n".join(lines) + "\n"


def render_rules(rec: dict, *, attribution: str = "") -> str:
    """一則行政規則（只有全文）→ 存進知識庫的 Markdown。"""
    eff = rec.get("effective") or ""
    meta = [
        ("法規位階", level_text(rec.get("level") or "")),
        ("法規類別", rec.get("category") or ""),
        ("法規代碼", rec.get("key") or ""),
        ("異動日期", roc_date(rec.get("modified") or "")),
        ("生效日期", roc_date(eff)),
        ("廢止", "已廢止" if rec.get("abolished") else ""),
        ("法規網址", rec.get("url") or ""),
        ("資料來源", attribution),
    ]
    lines = _header(rec.get("name") or "", meta)
    body = (rec.get("text") or "").replace("\r\n", "\n").replace("\r", "\n")
    for raw in body.split("\n"):
        t = raw.rstrip()
        lines.append(_escape(t.strip()) if not _is_box(t) else _escape(t))
    atts = [x for x in rec.get("attachments") or [] if x.get("url")]
    if atts:
        lines += ["", "附件（只列連結，內容沒有放進知識庫）："]
        for x in atts:
            lines.append(_escape(f"- {_clean(x.get('name'), 200) or '附件'}：{_clean(x.get('url'), 1000)}"))
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- 讀回
def _split(text: str) -> tuple[str, dict, list[str]]:
    """原檔 → (名稱, 檔頭欄位, 本文的行)。不是我們寫的格式丟 ValueError。"""
    rows = (text or "").replace("\r\n", "\n").split("\n")
    if not rows or not rows[0].startswith("# "):
        raise ValueError("不是政府公開資料的格式")
    name = rows[0][2:].strip()
    meta: dict[str, str] = {}
    i = 1
    while i < len(rows) and rows[i] != _HEAD_END:
        m = _META_RE.match(rows[i])
        if m:
            meta[m.group(1)] = m.group(2)
        i += 1
    if i >= len(rows):
        raise ValueError("不是政府公開資料的格式")
    return name, meta, rows[i + 1:]


def parse_law(text: str) -> dict:
    """`render_law()` 寫出的原檔讀回結構：`{"name", "meta", "items"}`，
    items 是 `("C", 標題)` / `("A", 條號, [各項])` / `("F", [前言各段])`。"""
    name, meta, rows = _split(text)
    items: list[tuple] = []
    cur: Optional[list] = None
    for raw in rows:
        if raw.startswith("## "):
            title = raw[3:].strip()
            if title == "前言":
                cur = ["F", []]
                items.append(cur)
            elif title.startswith("附件（"):
                cur = None
            else:
                items.append(("C", title))
                cur = None
            continue
        if raw.startswith("### "):
            label = raw[4:].strip()
            cur = ["A", "" if label == "　" else label, []]
            items.append(cur)
            continue
        if not raw.strip() or cur is None:
            continue
        cur[-1].append(_unescape(raw))
    return {"name": name, "meta": meta,
            "items": [tuple(x) if isinstance(x, list) else x for x in items]}


def _chapter_rank(title: str) -> int:
    m = _BIG_RE.match(unicodedata.normalize("NFKC", title))
    return _BIG_RANK[m.group(1)] if m else 1


def _ref_of(label: str, paras: list[str]) -> str:
    """母條文編號（同 `chunker` 的寫法：「第12條之1」「一」）。"""
    if label:
        det = chunker.detect(label)
        return det[1] if det else re.sub(r"\s+", "", label)
    if paras:
        det = chunker.detect(paras[0])
        if det and det[0] in (4, 5) and det[1]:
            return det[1]
    return ""


def law_chunks(text: str) -> list[dict]:
    """法規原檔 → 段落清單（一條一段；太長才拆，拆出來的每一塊都帶同一個條號）。"""
    doc = parse_law(text)
    name = doc["name"]
    stack: list[tuple[int, str]] = []
    out: list[dict] = []

    def path() -> list[str]:
        return [name] + [t for _, t in stack]

    def emit(body: str, ref: str) -> None:
        p = path()
        pieces = [body] if len(body) <= chunker.CHUNK_MAX else chunker.split_long(body)
        group = []
        for piece in pieces:
            d = {"text": piece.strip(), "heading_path": p, "heading": chunker._heading(p),
                 "parent_ref": ref, "page_from": None, "page_to": None, "part": 1, "parts": 1}
            if d["text"]:
                group.append(d)
        for i, d in enumerate(group, start=1):
            if len(group) > 1:
                d["part"], d["parts"] = i, len(group)
        out.extend(group)

    for it in doc["items"]:
        if it[0] == "C":
            rank = _chapter_rank(it[1])
            while stack and stack[-1][0] >= rank:
                stack.pop()
            stack.append((rank, it[1]))
        elif it[0] == "F":
            if it[1]:
                emit("前言\n" + "\n".join(it[1]), "")
        else:
            _, label, paras = it
            if not paras and not label:
                continue
            body = (label + " " + paras[0] if label and paras else (label or paras[0]))
            if len(paras) > 1:
                body += "\n" + "\n".join(paras[1:])
            emit(body, _ref_of(label, paras))
    return out


def rules_lines(text: str) -> tuple[str, list[Line]]:
    """行政規則原檔 → (名稱, 本文的行)。本文開頭重複寫一次的名稱拿掉（已經在上層標題裡）。"""
    name, _meta, rows = _split(text)
    lines: list[Line] = []
    norm_name = re.sub(r"\s+", "", unicodedata.normalize("NFKC", name))
    for raw in rows:
        if raw.startswith("附件（只列連結"):
            break
        t = _unescape(raw)
        if not t.strip():
            continue
        if not lines and re.sub(r"\s+", "", unicodedata.normalize("NFKC", t)) == norm_name:
            continue
        # 行政規則全文的每一行都是真的換段（不是 PDF 的排版斷行）：一律當新段落，
        # 「一、緣起」與下一行的內文之間才會保留換行，而不是黏成「一、緣起依據…」。
        lines.append(Line(t if _is_box(t) else t.strip(), para_start=True))
    return name, lines


def rules_chunks(text: str) -> list[dict]:
    name, lines = rules_lines(text)
    return chunker.chunk(lines, keep_articles=True, root_path=[name])


def chunks_for(fmt: str, data: bytes) -> list[dict]:
    """依格式切段（`indexer` 用）。"""
    text = data.decode("utf-8")
    if fmt == FORMAT_LAW:
        return law_chunks(text)
    if fmt == FORMAT_RULES:
        return rules_chunks(text)
    raise ValueError(fmt)


def chunker_version(fmt: Optional[str]) -> str:
    """存進 `kb_versions.chunker_version` 的值。**每次呼叫都讀目前的 `CHUNKER_VERSION`**
    （測試會在執行期改它）。"""
    base = chunker.CHUNKER_VERSION
    return f"{base}+{FORMAT_VERSIONS[fmt]}" if fmt in FORMAT_VERSIONS else base


def char_count(chunks: Iterable[dict]) -> int:
    return sum(len(c.get("text") or "") for c in chunks)
