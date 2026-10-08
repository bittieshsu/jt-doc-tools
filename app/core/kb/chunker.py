"""依公文 / 法規的結構切段。

## 判準

政府文件的結構寫在**行首的項次**裡，而不是排版：

| 層級 | 寫法 | 例 |
|---|---|---|
| 0～2 | `第 N 編 / 章 / 節` | 法規、手冊的大章 |
| 3 | `壹、`～`拾、`、`附件 N、`、`附錄 N、` | 文書處理手冊的章、附件 |
| 4 | `第 N 條`、`第 N 點` | 法規條文、行政規則的點 |
| 5 | `一、` | 手冊的「點」、法規的「款」 |
| 6 | `（一）` | |
| 7 | `1、` `1.` | |
| 8 | `（1）` | |
| 99 | 沒有項次的段落 | |

Word / ODT 的「標題 N」樣式與 Markdown 的 `#` 在**行首沒有項次**時才用
（`標題 1` ＝ 0，以此類推）—— 文件常常樣式是「標題 2」、內文又寫著「貳、」，
兩者都看的話層級會互相打架，以行首的字為準。

切法：

1. 先把整份文件照層級排成一棵樹（新的項次比堆疊頂端的層級高就往上收）。
2. 一個節點連同底下的內容**不超過 `CHUNK_MAX` 字**就整個當一段；
   超過就把它的子節點**依序裝箱**（同一層的兄弟才會被裝在一起），
   子節點自己還太大就往下遞迴。
3. 第 3 層以上（章、附件）**不跟兄弟合併** —— 章是語意上的邊界，
   把「壹、總述」的尾巴和「貳、公文製作」的開頭裝在同一段，
   檢索時兩邊都會被這一段干擾。
4. 單一段落（沒有子節點）本身就超過上限 → 照句號、分號切成約 `PIECE_TARGET`
   字的幾塊，**每一塊都帶著同一個母條文編號**，前後段落互相連結。

每一段都記著上層標題（「柒、文書簡化 ＞ 四十四、減少文書數量應注意事項如下」），
索引時會跟本文一起進去 —— 問題裡的關鍵詞常常只出現在章節標題。

## 數字是起點，不是定論

`CHUNK_MAX`（800 字）來自規格建議的 400～800 tokens：中文一個字大約一個 token，
`embeddinggemma` 的上下文是 2,048 tokens，扣掉標題與前綴之後 800 字有餘裕。
**改了切法（常數或規則）就要把 `CHUNKER_VERSION` 加一** —— 它是 index fingerprint
的一部分，版本不同的索引不可以互相比較，重建索引時會重新切段。

## 「一條一段」模式（`keep_articles=True`）

政府公開資料匯入的行政規則（`kb/gov.py`）要**每一點自己一段**：把第 3 點的尾巴和
第 4 點的開頭裝在同一段，引用時就說不出依據是哪一點。這個模式只在呼叫端明確要求時
才開（`chunk(..., keep_articles=True)`），**預設的切法一個字都沒變**，所以
`CHUNKER_VERSION` 不必改（既有上傳的文件不受影響）。法規（全國法規資料庫）不走這裡
—— 它本來就有結構化的條號，直接在 `law_text.law_chunks()` 一條一段。
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Optional

from .extract import Line

#: 改了切段規則就加一（會讓既有索引需要重建）。
CHUNKER_VERSION = "1"

#: 一段的上限（字）。
CHUNK_MAX = 800
#: 單一長段落硬切時，每一塊的目標字數（比上限小一點，切出來的幾塊才會差不多大）。
PIECE_TARGET = 600
#: 上層標題取前幾個字。
TITLE_MAX = 40
#: 整條上層標題的上限（字）。
HEADING_MAX = 200

#: 「母條文」的層級：法規的條、點（4），手冊的「一、」（5）。
_ARTICLE_RANKS = (4, 5)
#: 這些層級不跟兄弟裝在同一段。
_SECTION_RANK_MAX = 3
#: 沒有項次的段落。
PARA_RANK = 99

_NUM = "0-9一二三四五六七八九十百千零〇"
_CN = "一二三四五六七八九十百"

_RE_BIG = re.compile(rf"^第\s*([{_NUM}][{_NUM}\s]{{0,8}}?)\s*(編|章|節)(?=\s|$)")
_RE_BIG_SHORT = re.compile(rf"^第\s*([{_NUM}][{_NUM}\s]{{0,8}}?)\s*(編|章|節)")
_RE_ART = re.compile(
    rf"^第\s*([{_NUM}][{_NUM}\s]{{0,8}}?)\s*(條|點)(?:\s*之\s*([0-9{_CN}]+))?(?=\s|$|[(])")
_RE_CAP = re.compile(r"^([壹貳參肆伍陸柒捌玖拾]{1,3})\s*、")
_RE_ANNEX = re.compile(rf"^(附件|附錄|附表)\s*([0-9{_CN}]{{1,3}})\s*(?:[、:.]|\s|$)")
_RE_CN = re.compile(rf"^([{_CN}]{{1,4}})\s*、")
_RE_PCN = re.compile(rf"^\(\s*([{_CN}]{{1,4}})\s*\)")
_RE_AR = re.compile(r"^(\d{1,3})\s*[、.](?!\d)")
#: `(1)` 但不收 `(02)`：電話區碼（「（02）3356-6500」）剛好落在行首時會被當成項次。
_RE_PAR = re.compile(r"^\(\s*([1-9]\d{0,2})\s*\)")

_BIG_RANK = {"編": 0, "章": 1, "節": 2}


def _squash(s: str) -> str:
    """「1 3」→「13」（PDF 常把條號的數字拆開排）。"""
    return re.sub(r"\s+", "", s)


def _nfkc(s: str) -> str:
    return unicodedata.normalize("NFKC", s)


def _orig_prefix_len(orig: str, norm_len: int) -> int:
    """正規化字串的前 `norm_len` 個字，對應到原字串的前幾個字。

    比對用 NFKC 之後的字串（全形半形、全形空白都統一），顯示仍用原字串 ——
    文件裡的全形括號、全形數字不應該因為我們切段就被改掉。
    """
    acc = 0
    for i, ch in enumerate(orig):
        if acc >= norm_len:
            return i
        acc += len(_nfkc(ch))
    return len(orig)


def detect(text: str, style_rank: Optional[int] = None
           ) -> Optional[tuple[int, str, str]]:
    """一行是不是一個新的項次開頭。回 `(層級, 標號, 整理過的顯示文字)` 或 None。"""
    t = text.strip()
    n = _nfkc(t)

    def _rest(m: re.Match) -> str:
        return t[_orig_prefix_len(t, m.end()):].strip()

    m = _RE_BIG.match(n) or (_RE_BIG_SHORT.match(n) if len(n) <= 25 else None)
    if m:
        label = "第" + _squash(m.group(1)) + m.group(2)
        rest = _rest(m)
        return _BIG_RANK[m.group(2)], label, (label + " " + rest).strip()
    m = _RE_ART.match(n)
    if m:
        label = "第" + _squash(m.group(1)) + m.group(2)
        if m.group(3):
            label += f"之{m.group(3)}"
        rest = _rest(m)
        return 4, label, (label + " " + rest).strip()
    m = _RE_CAP.match(n)
    if m:
        return 3, m.group(1), f"{m.group(1)}、{_rest(m)}"
    m = _RE_ANNEX.match(n)
    if m:
        label = f"{m.group(1)}{m.group(2)}"
        return 3, label, f"{label}、{_rest(m)}".rstrip("、")
    m = _RE_CN.match(n)
    if m:
        return 5, m.group(1), f"{m.group(1)}、{_rest(m)}"
    m = _RE_PCN.match(n)
    if m:
        return 6, f"（{m.group(1)}）", f"（{m.group(1)}）{_rest(m)}"
    m = _RE_AR.match(n)
    if m:
        return 7, m.group(1), f"{m.group(1)}、{_rest(m)}"
    m = _RE_PAR.match(n)
    if m:
        return 8, f"（{m.group(1)}）", f"（{m.group(1)}）{_rest(m)}"
    if style_rank is not None and 1 <= style_rank <= 6:
        return style_rank - 1, "", t
    return None


# ---------------------------------------------------------------- 樹
@dataclass
class Unit:
    rank: int
    label: str
    text: str
    page_from: Optional[int] = None
    page_to: Optional[int] = None
    children: list["Unit"] = field(default_factory=list)

    def add_line(self, s: str, page: Optional[int]) -> None:
        if self.text == self.label and self.rank in (0, 1, 2, 4):
            # 「第 7 條」單獨一行，條文從下一行開始 —— 標號與本文之間留一個空白
            self.text = self.text + " " + s
        else:
            self.text = _join(self.text, s)
        self._page(page)

    def _page(self, page: Optional[int]) -> None:
        if page is None:
            return
        self.page_from = page if self.page_from is None else min(self.page_from, page)
        self.page_to = page if self.page_to is None else max(self.page_to, page)

    def full_text(self) -> str:
        parts = [self.text] + [c.full_text() for c in self.children]
        return "\n".join(p for p in parts if p)

    def size(self) -> int:
        return len(self.text) + sum(c.size() + 1 for c in self.children)

    def pages(self) -> tuple[Optional[int], Optional[int]]:
        lo, hi = self.page_from, self.page_to
        for c in self.children:
            clo, chi = c.pages()
            if clo is not None:
                lo = clo if lo is None else min(lo, clo)
            if chi is not None:
                hi = chi if hi is None else max(hi, chi)
        return lo, hi

    def title(self) -> str:
        first = self.text.split("\n", 1)[0]
        cut = re.split(r"[：:。]", first, maxsplit=1)[0].strip()
        if len(cut) > TITLE_MAX:
            cut = cut[:TITLE_MAX] + "…"
        return cut


_ASCII_END = re.compile(r"[A-Za-z0-9]$")
_ASCII_START = re.compile(r"^[A-Za-z0-9]")


def _join(a: str, b: str) -> str:
    """接續行：中文之間不補空白，英數接英數才補一個空白。"""
    if not a:
        return b
    if not b:
        return a
    if _ASCII_END.search(a) and _ASCII_START.match(b):
        return a + " " + b
    return a + b


#: 法規名稱：短短一行、以「法」「條例」「辦法」…結尾。
_LAW_TITLE_RE = re.compile(
    r"^[^\s，。：；、（）()]{2,28}(?:法|條例|辦法|細則|規則|要點|規範|準則|須知|規程|通則|作業規定)$")
_LAW_NEXT_RE = re.compile(r"^(?:中華民國|第\s*[1一]\s*[條章編])")


def _law_title_at(lines: list[Line], i: int) -> bool:
    """這一行是不是一部法規的名稱（手冊後面附的「相關法規」就是一部接一部排下去）。

    光看「以辦法結尾」會誤判 —— PDF 換行常常剛好斷在「…作業辦法」。所以還要
    **下一行是沿革（「中華民國 83 年…訂定」）或第 1 條 / 第一章**，兩者同時成立才算。
    不認出來的話，第二部法規的條文會掛在第一部法規的最後一章底下。
    """
    t = unicodedata.normalize("NFKC", (lines[i].text or "").strip())
    if not _LAW_TITLE_RE.match(t):
        return False
    for j in range(i + 1, min(i + 3, len(lines))):
        nxt = unicodedata.normalize("NFKC", (lines[j].text or "").strip())
        if nxt:
            return bool(_LAW_NEXT_RE.match(nxt))
    return False


def build_tree(lines: list[Line]) -> Unit:
    root = Unit(rank=-1, label="", text="")
    stack: list[Unit] = [root]
    cur: Optional[Unit] = None
    for idx, ln in enumerate(lines):
        raw = (ln.text or "").strip()
        if not raw:
            continue
        det = detect(raw, ln.style_rank)
        if det is None and _law_title_at(lines, idx):
            det = (0, "", raw)
        if det:
            rank, label, display = det
            while len(stack) > 1 and stack[-1].rank >= rank:
                stack.pop()
            u = Unit(rank=rank, label=label, text=display)
            u._page(ln.page)
            stack[-1].children.append(u)
            stack.append(u)
            cur = u
        elif cur is None or ln.para_start or cur.rank <= _SECTION_RANK_MAX:
            # 章名、附件名、法規名稱之後的下一行**不接在標題後面** —— 標題本身就是
            # 完整的一行；接上去的話「公文程式條例」會變成「公文程式條例中華民國 17
            # 年…」，上層標題整個被沿革灌爆。
            u = Unit(rank=PARA_RANK, label="", text=raw)
            u._page(ln.page)
            stack[-1].children.append(u)
            cur = u
        else:
            cur.add_line(raw, ln.page)
    return root


# ---------------------------------------------------------------- 切段
_SENT_RE = re.compile(r"(?<=[。！？；!?;])|\n")


def split_long(text: str, target: int = PIECE_TARGET) -> list[str]:
    """照句號、分號切成每塊約 `target` 字；單一句子比上限還長就硬切。"""
    sents = [s for s in _SENT_RE.split(text) if s and s.strip()]
    pieces: list[str] = []
    cur = ""
    for s in sents:
        while len(s) > CHUNK_MAX:
            if cur:
                pieces.append(cur)
                cur = ""
            pieces.append(s[:target])
            s = s[target:]
        if cur and len(cur) + len(s) > target:
            pieces.append(cur)
            cur = ""
        cur += s
    if cur.strip():
        pieces.append(cur)
    return [p.strip() for p in pieces if p.strip()]


def _heading(path: list[str]) -> str:
    h = " ＞ ".join(p for p in path if p)
    return h[:HEADING_MAX]


def _ref_of_group(units: list[Unit]) -> str:
    labels = [u.label for u in units if u.rank in _ARTICLE_RANKS and u.label]
    if not labels:
        return ""
    return labels[0] if len(labels) == 1 else f"{labels[0]}～{labels[-1]}"


def _make(text: str, path: list[str], ref: str, units: list[Unit]) -> dict:
    lo = hi = None
    for u in units:
        ulo, uhi = u.pages()
        if ulo is not None:
            lo = ulo if lo is None else min(lo, ulo)
        if uhi is not None:
            hi = uhi if hi is None else max(hi, uhi)
    return {"text": text.strip(), "heading_path": list(path), "heading": _heading(path),
            "parent_ref": ref, "page_from": lo, "page_to": hi, "part": 1, "parts": 1}


#: 只有題幹、沒有子項目的那一組，短於這個字數就不另成一段 ——
#: 題幹（「四十四、減少文書數量應注意事項如下」）已經在後面每一段的上層標題裡了，
#: 單獨一段只有一行字，檢索時只會變成雜訊。
STEM_ONLY_MIN = 80


def _has_article(node: Unit) -> bool:
    return any(c.rank in _ARTICLE_RANKS or _has_article(c) for c in node.children)


def _split_for_articles(node: Unit, ref: str, keep_articles: bool) -> bool:
    """「一條一段」模式下，一章（或一個沒有條號的段落）**底下有條／點**時，即使整章
    不到上限也要拆開 —— 不然短的一章會整章變成一段，裡面的幾點又裝在一起了。"""
    return (keep_articles and not ref and node.rank not in _ARTICLE_RANKS
            and _has_article(node))


def _emit(node: Unit, path: list[str], ref: str, out: list[dict],
          keep_articles: bool = False) -> None:
    """把 `node` 切成段。`path` 是 node 的上層標題（**不含** node 自己），
    `ref` 是外層的母條文編號（沒有就空字串）。`keep_articles` 見模組說明。"""
    is_root = node.rank < 0
    if (not is_root and node.size() <= CHUNK_MAX
            and not _split_for_articles(node, ref, keep_articles)):
        # 整章一段時，母條文寫成裡面第一點到最後一點（「十八～十九」）
        own_ref = ref or (node.label if node.rank in _ARTICLE_RANKS
                          else _ref_of_group(node.children))
        out.append(_make(node.full_text(), path, own_ref, [node]))
        return

    my_ref = ref or ("" if is_root else (node.label if node.rank in _ARTICLE_RANKS else ""))
    inner_path = path if is_root else path + [node.title()]
    stem = None if is_root else Unit(rank=node.rank, label=node.label, text=node.text,
                                     page_from=node.page_from, page_to=node.page_to)

    # 第一組帶著 node 自己的本文（題幹），上層標題用 node 的上一層；
    # 之後的組本文裡沒有題幹了，上層標題就要含 node 自己。
    state = {"units": [], "texts": [], "len": 0, "path": path if not is_root else inner_path,
             "stem_only": False}

    def flush() -> None:
        texts, units = state["texts"], state["units"]
        if texts:
            stem_only = state["stem_only"] and len(units) == 1
            if not (stem_only and len(texts[0]) < STEM_ONLY_MIN):
                child_units = [u for u in units if u is not stem]
                out.append(_make("\n".join(texts), state["path"],
                                 my_ref or _ref_of_group(child_units), units))
        state.update(units=[], texts=[], len=0, path=inner_path, stem_only=False)

    if stem is not None and stem.text:
        if len(stem.text) > CHUNK_MAX:
            for piece in split_long(stem.text):
                part = Unit(rank=node.rank, label=node.label, text=piece,
                            page_from=node.page_from, page_to=node.page_to)
                out.append(_make(piece, path, my_ref, [part]))
            state["path"] = inner_path
        else:
            state.update(units=[stem], texts=[stem.text], len=len(stem.text), stem_only=True)

    for c in node.children:
        size = c.size()
        if (c.rank <= _SECTION_RANK_MAX and not c.children
                and len(c.text) < STEM_ONLY_MIN):
            # 只有標題、底下什麼都沒有的章名 —— 多半是被印成頁首的同一個章名
            # （一章的第一頁上方印一次、正文又一次）。單獨一段只有幾個字，只會變雜訊。
            continue
        alone = keep_articles and c.rank in _ARTICLE_RANKS and not my_ref
        if size > CHUNK_MAX or c.rank <= _SECTION_RANK_MAX or alone:
            flush()
            if size > CHUNK_MAX or _split_for_articles(c, my_ref, keep_articles):
                _emit(c, inner_path, my_ref, out, keep_articles)
            else:
                own_ref = my_ref or (c.label if c.rank in _ARTICLE_RANKS
                                     else _ref_of_group(c.children))
                out.append(_make(c.full_text(), inner_path, own_ref, [c]))
            continue
        if state["texts"] and state["len"] + size + 1 > CHUNK_MAX:
            flush()
        state["units"].append(c)
        state["texts"].append(c.full_text())
        state["len"] += size + 1
        state["stem_only"] = False
    flush()


def chunk(lines: list[Line], *, keep_articles: bool = False,
          root_path: Optional[list[str]] = None) -> list[dict]:
    """主入口：行 → 段落清單（照文件順序）。

    `keep_articles`：每一條／點自己一段（見模組說明）；`root_path`：每一段的上層標題
    最前面再加這幾層（政府公開資料把法規名稱放在這裡，問題裡寫了名稱才查得到）。
    兩者都不給 ＝ 原本的切法。"""
    out: list[dict] = []
    _emit(build_tree(lines), [p for p in (root_path or []) if p], "", out, keep_articles)
    out = [c for c in out if c["text"]]
    # 同一個母條文被切成好幾段時，標出「第幾段／共幾段」
    i = 0
    while i < len(out):
        ref = out[i]["parent_ref"]
        j = i + 1
        if ref and "～" not in ref:
            while j < len(out) and out[j]["parent_ref"] == ref:
                j += 1
            if j - i > 1:
                for k in range(i, j):
                    out[k]["part"], out[k]["parts"] = k - i + 1, j - i
        i = j
    return out
