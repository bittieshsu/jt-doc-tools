"""翻譯對照字典 —— 企業內部的專有名詞怎麼翻（或不翻）。

## 為什麼不寫進 prompt

把 `Acer→宏碁` 寫在指令裡只是「拜託模型照做」，它可能不照做，而且**沒照做
你不會發現**。而且翻成繁中的指令部分已經 1,179 字元、每批內容上限才 1,200
—— 字典再塞進去只會讓批次變小、漏段重試變多（v1.14.83 實測過那條路，
批次調大反而更慢）。

## 做法：術語保護（送出前抽換、收回後還原）

    原文     The Acer server runs Foxconn firmware.
    送給模型 The ⟪1⟫ server runs ⟪2⟫ firmware.
    模型回覆 ⟪1⟫ 伺服器執行 ⟪2⟫ 韌體。
    產出     宏碁 伺服器執行 富士康 韌體。

模型根本看不到那兩個詞，不可能翻錯；**prompt 一個字都不加**，字典有幾千條
也不影響批次大小。`keep`（不要翻）與 `translate`（照這樣翻）共用同一套機制，
差別只在還原時填回原文還是譯文。

**唯一的風險是模型把佔位符弄丟或改壞**，所以還原前一定要驗（見
`restore()`）：少一個就整段退回不保護重翻。**產出裡絕對不可以殘留 `⟪1⟫`**
—— 那比翻錯還明顯。
"""
from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterable, Optional

from ..config import settings

#: 佔位符。**一定要用 ASCII。**
#:
#: v1.15.19 第一版用 `⟪1⟫`，在真的模型上 **5 句全滅**：gemma4:26b 的
#: tokenizer 遇到不在詞彙表裡的字元時，會把**位元組 token 的字面寫法**
#: 吐成文字 ——
#:
#:     送出   The ⟪1⟫ server runs ⟪2⟫ firmware.
#:     回覆   <0xE2><0x9F><0xAA>1⟫ 伺服器執行 …
#:
#: `E2 9F AA` 正是 `⟪` 的 UTF-8 位元組（同 v1.14.87 的 `⟦<0xC2>5⟧`）。
#: 於是每一句都還原失敗、退回不保護重翻 —— **功能等於沒有，還白花一倍的
#: LLM 請求**，而且假模型測不出來。
#:
#: 實測七種寫法（`tests/test_translation_glossary.py` 記著結果）：
#: `⟪1⟫` 與 `#1#` 失敗（後者被模型讀成「第 1 號」），
#: `[[T1]]` / `⟦T1⟧` / `[[1]]` / `<<1>>` / `{{1}}` 逐句與批次都全過。
#: 選 `[[T1]]`：ASCII 對各家 tokenizer 都安全，`T` 前綴讓它不會撞到
#: 文件裡的 `[[頁面連結]]`，也不會被批次協定的 `⟦編號⟧` 誤認。
_PH_OPEN, _PH_CLOSE = "[[T", "]]"
#: 還原時容許模型在標記裡多塞空白（實測會發生），但不容許它換成別的括號。
_PH_RE = re.compile(r"\[\[\s*T\s*(\d+)\s*\]\]")
#: 模型偶爾會把不成字的位元組吐成 `<0xE2>` 這種字面寫法（見上）。批次解析
#: 早就在做同樣的清理 —— 還原之前也要做，否則只要吐一個就整段退回。
_BYTE_TOKEN_RE = re.compile(r"<0x[0-9A-Fa-f]{2}>")

#: 條目上限。字典是要進每一段文字做比對的，無上限的話一份大字典會讓
#: 每次翻譯都變慢，而且管理頁也載不動。
MAX_TERMS = 5000
#: 單一詞條的長度上限（原文 / 譯文各自）。
MAX_TERM_LEN = 200

MODES = ("translate", "keep")

#: 可選的語言。**跟翻譯工具用同一份** —— 同一份清單放兩個地方一定會漂
#: （這個專案為此吃過好幾次虧）。管理頁的下拉直接讀這裡。
def lang_choices() -> list[dict]:
    from ..tools.translate_doc.router import _LANG_NAMES
    return [{"code": c, "name": n} for c, n in _LANG_NAMES.items()]

#: 這些字元不可以出現在詞條裡：`⟦⟧` 是批次協定的標記，混進去會讓逐段解析
#: 錯亂；控制字元與換行會破壞逐行的批次格式。
_FORBIDDEN = re.compile(r"[⟦⟧\x00-\x1f\x7f]")


@dataclass
class Term:
    """一條對照。**有方向**：`src_lang` 的 `source` 翻成 `tgt_lang` 時要用 `target`。"""

    source: str
    target: str
    src_lang: str
    tgt_lang: str
    mode: str = "translate"
    case_sensitive: bool = False
    note: str = ""
    enabled: bool = True

    def replacement(self) -> str:
        """還原時要填回去的字串。"""
        return self.source if self.mode == "keep" else self.target


def _clean(s: str) -> str:
    return _FORBIDDEN.sub("", (s or "").strip())


def normalise(raw: dict) -> Term:
    """把一筆使用者輸入整理成 `Term`；不合法就丟 `ValueError`。

    **管理員是可信的，但打錯字不該把翻譯功能弄壞** —— 詞條會被拿去組正規
    表示式、也會影響批次的逐行格式，所以控制字元與協定用的括號一律去掉。
    """
    source = _clean(raw.get("source", ""))
    mode = (raw.get("mode") or "translate").strip()
    if mode not in MODES:
        raise ValueError(f"模式只能是 {' / '.join(MODES)}")
    target = source if mode == "keep" else _clean(raw.get("target", ""))
    if not source:
        raise ValueError("原文不可以空白")
    if mode == "translate" and not target:
        raise ValueError("譯文不可以空白（要保持原樣請選「不要翻譯」）")
    if len(source) > MAX_TERM_LEN or len(target) > MAX_TERM_LEN:
        raise ValueError(f"詞條長度不可超過 {MAX_TERM_LEN} 字")
    src_lang = _clean(raw.get("src_lang", ""))
    tgt_lang = _clean(raw.get("tgt_lang", ""))
    if not src_lang or not tgt_lang:
        raise ValueError("原文語言與譯文語言都要填")
    if src_lang == tgt_lang:
        raise ValueError("原文語言與譯文語言不可以相同")
    return Term(
        source=source, target=target, src_lang=src_lang, tgt_lang=tgt_lang,
        mode=mode, case_sensitive=bool(raw.get("case_sensitive")),
        note=_clean(raw.get("note", ""))[:200],
        enabled=bool(raw.get("enabled", True)),
    )


def key_of(term: Term) -> tuple[str, str, str]:
    """同一個語言對裡，原文相同就算同一條（比對時不分大小寫才不會重複建）。"""
    return (term.src_lang, term.tgt_lang, term.source.casefold())


# ---------------------------------------------------------------- 儲存

_LOCK = threading.RLock()
#: 依 **mtime + 大小**失效。這份資料每次翻譯都要用，沒有快取的話等於每段
#: 文字讀一次檔（剛在 v1.15.17 為上傳上限踩過同一個坑）。
_CACHE: tuple[Optional[tuple[float, int]], list[Term]] | None = None


def _path() -> Path:
    return settings.data_dir / "translation_glossary.json"


def _stamp(p: Path) -> Optional[tuple[float, int]]:
    try:
        st = p.stat()
        return (st.st_mtime, st.st_size)
    except OSError:
        return None                      # 還沒有字典＝空的


def load() -> list[Term]:
    global _CACHE
    p = _path()
    stamp = _stamp(p)
    cached = _CACHE
    if cached is not None and cached[0] == stamp:
        return cached[1]
    terms: list[Term] = []
    if stamp is not None:
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
            for item in (raw or {}).get("terms", []):
                try:
                    terms.append(normalise(item))
                except (ValueError, AttributeError):
                    continue             # 壞掉的單一條目不該讓整份字典失效
        except (OSError, ValueError):
            terms = []
    _CACHE = (stamp, terms)
    return terms


def save(terms: Iterable[Term]) -> list[Term]:
    global _CACHE
    out = list(terms)
    if len(out) > MAX_TERMS:
        raise ValueError(f"字典最多 {MAX_TERMS} 條（目前 {len(out)} 條）")
    with _LOCK:
        p = _path()
        p.parent.mkdir(parents=True, exist_ok=True)
        # **先寫暫存檔再換過去**（`os.replace` 在 POSIX 與 Windows 都是原子的）。
        # 直接覆寫的話，寫到一半斷電 / 行程被殺就留下一個截斷的檔案 ——
        # `load()` 會安靜地當成空字典，翻譯從此不再套用任何詞條，而且**沒有
        # 任何錯誤訊息**。這份資料是管理員一條一條建的，不可以這樣掉。
        tmp = p.with_suffix(".json.tmp")
        payload = json.dumps({"version": 1, "terms": [asdict(t) for t in out]},
                             ensure_ascii=False, indent=2)
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        tmp.replace(p)
        _CACHE = None
    return out


# ---------------------------------------------------------------- 比對

_ASCII_WORD = re.compile(r"[A-Za-z0-9]")


def _bounded(source: str) -> str:
    """把詞條包成正規表示式。

    拉丁文字要**詞邊界**（`Acer` 不可以命中 `Acerbic`、`IT` 不可以命中
    `ITEM`）；中日韓沒有詞邊界，用純子字串 —— 硬加 `\\b` 反而會讓
    「宏碁」在中文句子裡整個匹配不到。
    """
    pat = re.escape(source)
    if _ASCII_WORD.match(source[0]):
        pat = r"(?<![A-Za-z0-9])" + pat
    if _ASCII_WORD.match(source[-1]):
        pat = pat + r"(?![A-Za-z0-9])"
    return pat


class Matcher:
    """某一個語言對的比對器。編譯一次，反覆用。"""

    def __init__(self, terms: list[Term]):
        # **最長優先**：`Acer Chromebook` 要贏過 `Acer`。正規表示式的
        # 選擇分支是由左到右取第一個成功的，所以順序就是優先序。
        self.terms = sorted(terms, key=lambda t: len(t.source), reverse=True)
        self._exact: dict[str, Term] = {}
        self._fold: dict[str, Term] = {}
        parts: list[str] = []
        for t in self.terms:
            if t.case_sensitive:
                self._exact.setdefault(t.source, t)
                parts.append(_bounded(t.source))
            else:
                self._fold.setdefault(t.source.casefold(), t)
                parts.append(f"(?i:{_bounded(t.source)})")
        self._re = re.compile("|".join(parts)) if parts else None

    def __bool__(self) -> bool:
        return self._re is not None

    def _lookup(self, matched: str) -> Optional[Term]:
        t = self._exact.get(matched)
        if t is not None:
            return t
        return self._fold.get(matched.casefold())

    def find(self, text: str) -> list[Term]:
        """這段文字命中哪些條目（去重，保持出現順序）。"""
        if not self._re or not text:
            return []
        seen: dict[int, Term] = {}
        for m in self._re.finditer(text):
            t = self._lookup(m.group(0))
            if t is not None:
                seen.setdefault(id(t), t)
        return list(seen.values())


def matcher_for(src_lang: str, tgt_lang: str,
                terms: Optional[list[Term]] = None) -> Matcher:
    pool = load() if terms is None else terms
    return Matcher([t for t in pool
                    if t.enabled and t.src_lang == src_lang
                    and t.tgt_lang == tgt_lang])


def count_for(src_lang: str, tgt_lang: str) -> int:
    """這個語言對有幾條可用 —— 畫面上要告訴使用者「有 N 條可套用」。"""
    return len(matcher_for(src_lang, tgt_lang).terms)


def pair_counts() -> dict[str, int]:
    """`{"en|zh-TW": 12, ...}` —— 每個語言對各有幾條可用。

    工具頁的「套用字典」勾選要**只在選到的語言對真的有條目時才出現**：
    沒設定就給一個永遠沒作用的勾選，只會讓人以為自己設錯了。
    """
    out: dict[str, int] = {}
    for t in load():
        if not t.enabled:
            continue
        out[f"{t.src_lang}|{t.tgt_lang}"] = out.get(f"{t.src_lang}|{t.tgt_lang}", 0) + 1
    return out


# ---------------------------------------------------------------- 保護 / 還原

def protect(text: str, matcher: Matcher) -> tuple[str, dict[int, str]]:
    """把命中的詞換成佔位符。回傳 (換過的文字, {編號: 要填回去的字串})。

    **沒有命中就原樣回傳、對照表是空的** —— 這條路徑上一個位元組都不會變，
    沒設字典的安裝行為完全不受影響。
    """
    if not matcher or not text:
        return text, {}
    # 原文本身就長得像佔位符時**不要保護**（不然還原會把它換成別的詞）。
    # 幾乎不會發生，但發生時的後果是安靜改壞內容。
    if _PH_RE.search(text):
        return text, {}
    mapping: dict[int, str] = {}
    counter = 0

    def _sub(m: re.Match) -> str:
        nonlocal counter
        term = matcher._lookup(m.group(0))
        if term is None:
            return m.group(0)
        counter += 1
        mapping[counter] = term.replacement()
        return f"{_PH_OPEN}{counter}{_PH_CLOSE}"

    return matcher._re.sub(_sub, text), mapping


def restore(text: str, mapping: dict[int, str]) -> tuple[str, bool]:
    """把佔位符換回指定的字串。回傳 (結果, 是否完整)。

    **不完整就要讓呼叫端退回不保護重翻** —— 模型偶爾會弄丟或改壞標記，
    這時硬把剩下的填回去會產出少了字的句子，而且沒有人看得出來。
    產出裡也絕對不可以殘留 `⟪1⟫`：那比翻錯還明顯。
    """
    if not mapping:
        return text, True
    # 先把位元組 token 的字面寫法清掉（`<0xE2>` 之類）—— 模型吐一個就會讓
    # 整段退回，而那是雜訊不是內容。批次解析早就在做同樣的事。
    text = _BYTE_TOKEN_RE.sub("", text or "")
    # **每個編號都要剛好出現一次。** 只比對「有哪些編號」的話，模型把同一個
    # 標記吐兩次也會判成完整，然後產出一句多了一個詞的話 —— 而原文裡同一個
    # 詞出現兩次時本來就會拿到兩個不同的編號，所以「剛好一次」才是不變量。
    found: dict[int, int] = {}
    for m in _PH_RE.finditer(text or ""):
        n = int(m.group(1))
        found[n] = found.get(n, 0) + 1
    if set(found) != set(mapping) or any(c != 1 for c in found.values()):
        return text, False
    return _fill(text, mapping), True


#: 中日韓文字（含全形標點）。中文詞之間不放空白，英文詞才要。
_CJK = re.compile(r"[\u3000-\u303f\u3400-\u4dbf\u4e00-\u9fff"
                  r"\uf900-\ufaff\uff00-\uffef\u3040-\u30ff\uac00-\ud7af]")


def _fill(text: str, mapping: dict[int, str]) -> str:
    """把佔位符換成指定的字串，順便**收掉中文詞兩邊多出來的空白**。

    英文原文是 `The [[T1]] server`，模型很自然會譯成 `[[T1]] 伺服器`——
    填回中文詞就變成「宏碁 伺服器」。中文詞之間不放空白，那個空格很刺眼
    （英文詞則相反：「安裝 jt-doc-tools 之前」的空白是對的，所以只有
    **兩邊都是中日韓文字**時才收）。
    """
    out: list[str] = []
    pos = 0
    for m in _PH_RE.finditer(text):
        rep = mapping[int(m.group(1))]
        chunk = text[pos:m.start()]
        # 前面：譯文以中日韓字開頭，而前面是「中日韓字 + 空白」→ 收掉空白
        if (rep and _CJK.match(rep[0]) and chunk.endswith(" ")
                and len(chunk) >= 2 and _CJK.match(chunk[-2])):
            chunk = chunk[:-1]
        out.append(chunk)
        out.append(rep)
        pos = m.end()
        # 後面：譯文以中日韓字結尾，而後面是「空白 + 中日韓字」→ 收掉空白
        if (rep and _CJK.match(rep[-1]) and text[pos:pos + 2].startswith(" ")
                and len(text) > pos + 1 and _CJK.match(text[pos + 1])):
            pos += 1
    out.append(text[pos:])
    return "".join(out)


def has_placeholder(text: str) -> bool:
    """收尾的保險：任何要交給使用者的文字都不可以還帶著佔位符。"""
    return bool(_PH_RE.search(text or "")) or _PH_OPEN in (text or "")
