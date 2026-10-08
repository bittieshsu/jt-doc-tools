"""中日韓文字的 SQLite FTS5 **逐字**索引 —— 正規化、斷詞、查詢字串。

## 為什麼逐字

FTS5 內建的 `unicode61` 斷詞器以空白與標點分詞，中文一整句沒有空白 → 整句變成
**一個** token，打兩個字就查不到。做法是把每個中日韓字前後補空白（`unicode61`
會把每個字當一個 token），查詢時再把關鍵字拆成相鄰兩字的片語。

## 出處

斷詞與「台 / 臺」正規化照抄 `app/core/vat_db.py` 的 `_fts_tokenize` /
`_normalize_variants`（統編查詢 v1.12.16 起的做法）。**刻意沒有改 vat_db 去用這一支**：
那邊的 FTS 索引有 schema 版本（`_FTS_SCHEMA_VER`），斷詞只要差一個字元，
170 萬筆的索引就要整份重建 —— 為了「共用」去動它不值得。這裡多做了一件事：
**NFKC 正規化**（全形英數 → 半形、`（一）` → `(一)`、相容表意文字 → 統一碼），
因為公文與規範的 PDF 常常同一份文件裡全形半形混用（「( 十二)」與「（十二）」），
不正規化的話同一個項次會對不上。統編那邊是名稱與地址，用不到。

## 查詢怎麼組

`match_query()` 把使用者的問題拆成「相鄰兩字」的片語，用 `OR` 串起來 ——
自然語言的問題（「函的期望語對上級機關怎麼寫」）不可能整句出現在文件裡，
用 `AND` 會一筆都查不到。代價是 `OR` 會撈出很多只沾到一兩個字的段落，
所以呼叫端要再用 `coverage()` 算「問題裡的詞有多少比例出現在這一段」來篩
（見 `app/core/kb/search.py`）。
"""
from __future__ import annotations

import re
import unicodedata

#: 中日韓統一表意文字（含擴展 A、相容區）＋ 日文假名。**照 vat_db 的範圍**。
#: 第三段的起點要寫成跳脫碼：`豈` 有一般與相容兩個字形，直接打字的話很容易
#: 打成 U+8C48（一般區），範圍就多出兩萬多個碼位（v1.16.59 CodeQL #203 那次）。
_CJK_RE = re.compile("[぀-ヿ㐀-䶿一-鿿豈-﫿]")

#: 異體字正規化：索引與查詢兩邊套同一套。政府文件多寫「臺」、使用者多打「台」。
_VARIANT_MAP = str.maketrans({"臺": "台"})

#: 英數詞（正規化之後）。
_WORD_RE = re.compile(r"[0-9a-z]+")


def normalize(s: str) -> str:
    """NFKC ＋ 台 / 臺 ＋ 英文小寫。索引與查詢兩邊都要先過這一道。"""
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", s)
    return s.translate(_VARIANT_MAP).lower()


def tokenize(s: str) -> str:
    """把正規化後的字串變成「可以餵給 FTS5 的欄位內容」：每個中日韓字前後補空白。"""
    s = normalize(s)
    if not s:
        return ""
    out = []
    for ch in s:
        if _CJK_RE.match(ch):
            out.append(" ")
            out.append(ch)
            out.append(" ")
        else:
            out.append(ch)
    return re.sub(r"\s+", " ", "".join(out)).strip()


def query_terms(q: str) -> list[str]:
    """使用者的問題 → 檢索詞（去重、保留順序）。

    * 連續的中日韓字 → 相鄰兩字（`公文格式` → `公文`、`文格`、`格式`）；
      只有一個字的那一段就用那一個字。
    * 英數 → 整個詞。
    """
    s = normalize(q)
    terms: list[str] = []
    seen: set[str] = set()

    def _add(t: str) -> None:
        if t and t not in seen:
            seen.add(t)
            terms.append(t)

    run: list[str] = []

    def _flush() -> None:
        if len(run) == 1:
            _add(run[0])
        for i in range(len(run) - 1):
            _add(run[i] + run[i + 1])
        run.clear()

    i = 0
    while i < len(s):
        ch = s[i]
        if _CJK_RE.match(ch):
            run.append(ch)
            i += 1
            continue
        _flush()
        m = _WORD_RE.match(s, i)
        if m:
            _add(m.group(0))
            i = m.end()
        else:
            i += 1
    _flush()
    return terms


def term_to_phrase(term: str) -> str:
    """一個檢索詞 → FTS5 的片語（中日韓字之間補空白，整個用雙引號包起來）。

    檢索詞只會是中日韓字或 `[0-9a-z]`（`query_terms` 保證），所以不會有引號
    要跳脫；這裡仍然把引號拿掉，防的是將來有人改了 `query_terms`。
    """
    return '"' + tokenize(term).replace('"', "") + '"'


def match_query(terms: list[str]) -> str:
    """多個檢索詞 → `"a b" OR "c d" OR word`。沒有詞就回空字串（呼叫端不要查）。"""
    return " OR ".join(term_to_phrase(t) for t in terms if t)


def compact(s: str) -> str:
    """正規化後拿掉所有空白 —— 給 `coverage()` 做子字串比對用。"""
    return re.sub(r"\s+", "", normalize(s))


def coverage(terms: list[str], text_compact: str,
             weights: dict[str, float] | None = None) -> float:
    """問題裡的檢索詞，有多少比例（依權重）出現在這段文字裡。0～1。

    `text_compact` 要先過 `compact()`。權重通常是 IDF —— 「的期」「怎麼」這種
    哪裡都有的詞權重低，「期望語」「上級機關」這種權重高。
    """
    if not terms:
        return 0.0
    w = weights or {}
    total = hit = 0.0
    for t in terms:
        wt = w.get(t, 1.0)
        total += wt
        if t in text_compact:
            hit += wt
    return hit / total if total > 0 else 0.0
