"""依會議背景裡的專有名詞，找出逐字稿裡可能寫錯的寫法（「建議替換」）。

2026-10-02 使用者決定：會議背景的專有名詞要拿來修逐字稿的錯字。做法是**建議**，
由使用者逐條勾選才替換，替換前的原文留在段落的 `orig_text` —— **不讓模型改寫逐字稿**：

* 模型改寫是看不見的改動：摘要與決議會建立在被改過的字上，讀的人分不出哪幾個字是改的；
* 這裡的判斷是確定性的，說得出「為什麼」：英文是拼寫只差一兩個字母，
  中文是讀音相同、只有一兩個字寫法不同。

判斷要保守 —— 建議錯一條，使用者要花力氣看；錯很多條，這個功能就沒人看了：

* 英文：只比**像專有名詞**的詞（有大寫、數字或連字號），5 個字母以上；
  差 1 個字母（9 個字母以上可以差 2 個）；單複數 / 時態的變化與只差大小寫都不算錯字。
  另外認得「中間多了空白或連字號」（`Offline Mirror` → `OfflineMirror`）。
* 中文：3~8 個字的詞；每一個位置的讀音都要對得上（不分聲調、多音字取任一讀音），
  而且寫法不同的字最多 `len // 3` 個（至少 1）—— 只看讀音的話，常見詞之間撞音太多
  （「公式」「公事」），兩個字的詞因此一律不比。
* 這個寫法本身就出現在會議背景裡的，不建議（背景自己這樣寫，就不是錯字）。
* 同一個寫法像兩個以上的詞時，不建議（不知道該換成哪一個）。
"""
from __future__ import annotations

import re
from collections import defaultdict
from functools import lru_cache
from typing import Iterable, Optional

from ..logging_setup import get_logger

logger = get_logger(__name__)

_CJK_CLASS = "㐀-䶿一-鿿豈-﫿"
_CJK_RUN = re.compile(f"[{_CJK_CLASS}]+")
#: 英文詞：字母開頭，中間可以有 `-` `_` `.`（`Acme-Kevin`、`v1.2`）；所有格的 `'s` 不算在內，
#: 所以 `Bianka's` 換成 `Bianca's` 也照樣換得到。
_LATIN_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:[-_.][A-Za-z0-9]+)*")

MIN_LATIN = 5
MIN_CJK, MAX_CJK = 3, 8
MAX_TERMS = 200
MAX_SUGGESTIONS = 30
MAX_PAIRS = 50
MAX_PAIR_LEN = 100
_INFLECTIONS = ("s", "es", "d", "ed", "ing", "er", "ers")


# ---------------------------------------------------------------- 從會議背景取詞

def _looks_proper(tok: str) -> bool:
    """看起來像專有名詞：有大寫、數字或連字號。全小寫的一般字不比（`budget` / `review`）。"""
    return (any(c.isupper() for c in tok) or any(c.isdigit() for c in tok)
            or "-" in tok)


def extract_terms(context: str) -> list[str]:
    """會議背景裡的候選詞（保留第一次出現的順序，不分大小寫去重）。"""
    terms: list[str] = []
    seen: set[str] = set()

    def add(t: str) -> None:
        k = t.casefold()
        if k not in seen and len(terms) < MAX_TERMS:
            seen.add(k)
            terms.append(t)

    for line in str(context or "").splitlines():
        toks = list(_LATIN_TOKEN.finditer(line))
        group: list[re.Match] = []
        for m in toks + [None]:
            # 相鄰、中間只隔一個空白、而且都像專有名詞的詞 → 多字詞（`Proxmox VE`、`Jason Cheng`）
            if (m is not None and _looks_proper(m.group()) and group
                    and line[group[-1].end():m.start()] == " "):
                group.append(m)
                continue
            if len(group) >= 2:
                add(" ".join(g.group() for g in group))
            group = [m] if m is not None and _looks_proper(m.group()) else []
        for m in toks:
            tok = m.group()
            if _looks_proper(tok) and sum(c.isalpha() for c in tok) >= MIN_LATIN:
                add(tok)
        for m in _CJK_RUN.finditer(line):
            run = m.group()
            if MIN_CJK <= len(run) <= MAX_CJK:
                add(run)
    return terms


# ---------------------------------------------------------------- 英文

def _squash(s: str) -> str:
    return re.sub(r"[\s\-_.]+", "", s).casefold()


def _max_dist(n: int) -> int:
    return 2 if n >= 9 else 1


def _is_inflection(a: str, b: str) -> bool:
    for x, y in ((a, b), (b, a)):
        if x.startswith(y) and x[len(y):] in _INFLECTIONS:
            return True
    return False


def _lev(a: str, b: str, cutoff: int) -> int:
    try:
        from rapidfuzz.distance import Levenshtein
        return Levenshtein.distance(a, b, score_cutoff=cutoff)
    except ImportError:                      # 相依沒裝到時退回純 Python（慢但對）
        prev = list(range(len(b) + 1))
        for i, ca in enumerate(a, 1):
            cur = [i]
            for j, cb in enumerate(b, 1):
                cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
            prev = cur
        return prev[-1]


def _latin_match(cand: str, term: str) -> bool:
    if cand.casefold() == term.casefold():
        return False                          # 一模一樣，或只差大小寫
    sc, st = _squash(cand), _squash(term)
    if not sc or not st:
        return False
    if sc == st:
        return True                           # 只差空白 / 連字號（`Offline Mirror`）
    if sum(c.isalpha() for c in st) < MIN_LATIN:
        return False
    d = _max_dist(len(st))
    if abs(len(sc) - len(st)) > d or _is_inflection(sc, st):
        return False
    return _lev(sc, st, d) <= d


def _latin_candidates(text: str) -> Iterable[str]:
    """一段文字裡的 1~3 個相鄰英文詞（中間只隔一個空白或連字號）。"""
    toks = list(_LATIN_TOKEN.finditer(text))
    for i in range(len(toks)):
        for n in (1, 2, 3):
            if i + n > len(toks):
                break
            seg = toks[i:i + n]
            if any(text[seg[k].end():seg[k + 1].start()] not in (" ", "-")
                   for k in range(n - 1)):
                break
            yield text[seg[0].start():seg[-1].end()]


def _latin_pattern(surface: str, flags: int = 0) -> re.Pattern:
    """整個詞才算（`Mark` 不可以換掉 `Markdown` 的一截）；兩邊是英數字時才要邊界。"""
    pat = re.escape(surface)
    if surface[:1].isascii() and surface[:1].isalnum():
        pat = r"(?<![A-Za-z0-9])" + pat
    if surface[-1:].isascii() and surface[-1:].isalnum():
        pat = pat + r"(?![A-Za-z0-9])"
    return re.compile(pat, flags)


# ---------------------------------------------------------------- 中文

@lru_cache(maxsize=None)
def _readings(ch: str) -> frozenset:
    """一個字所有可能的讀音（不分聲調）。沒有讀音資料時回它自己 —— 只會跟同一個字對上。"""
    try:
        from pypinyin import Style, pinyin
    except ImportError:
        return frozenset((ch,))
    try:
        got = pinyin(ch, style=Style.NORMAL, heteronym=True)
    except Exception:
        return frozenset((ch,))
    vals = frozenset(r for r in (got[0] if got else []) if r and r != ch)
    return vals or frozenset((ch,))


def readings_available() -> bool:
    try:
        import pypinyin  # noqa: F401
        return True
    except ImportError:
        return False


def _reading_index(runs: list[tuple[int, str]]) -> dict[str, list[tuple[int, int]]]:
    """讀音 → 逐字稿裡出現的位置 (第幾段文字, 第幾個字)。建一次，每個詞只看讀音對得上的起點
    —— 逐一滑過整份逐字稿的話，三小時的會議（36 萬字）要 2~3 秒。"""
    idx: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for ri, (_seq, run) in enumerate(runs):
        for i, ch in enumerate(run):
            for r in _readings(ch):
                idx[r].append((ri, i))
    return idx


def _cjk_hits(runs: list[tuple[int, str]], term: str,
              index: Optional[dict] = None) -> dict[str, set[int]]:
    """逐字稿裡讀音跟 `term` 對得上、但寫法不同的地方 → {寫法: 出現的段號}。"""
    L = len(term)
    allow = max(1, L // 3)
    want = [_readings(c) for c in term]
    if index is None:
        index = _reading_index(runs)
    starts: set[tuple[int, int]] = set()
    for r in want[0]:
        starts.update(index.get(r, ()))
    out: dict[str, set[int]] = defaultdict(set)
    for ri, i in starts:
        seq, run = runs[ri]
        win = run[i:i + L]
        if len(win) < L or win == term:
            continue
        diff = 0
        for k in range(L):
            if win[k] == term[k]:
                continue
            if not (_readings(win[k]) & want[k]):
                break
            diff += 1
            if diff > allow:
                break
        else:
            if diff:
                out[win].add(seq)
    return out


# ---------------------------------------------------------------- 對外

def _base_text(seg: dict) -> str:
    o = seg.get("orig_text")
    return o if isinstance(o, str) else str(seg.get("text") or "")


def _example(text: str, at: int, width: int, pad: int = 14) -> str:
    a, b = max(0, at - pad), min(len(text), at + width + pad)
    return ("…" if a else "") + text[a:b] + ("…" if b < len(text) else "")


def suggest(segments: list[dict], context: str) -> dict:
    """回 `{"terms": [...], "suggestions": [{from, to, count, seqs, example}, ...]}`。

    比對的是**替換之前的原文**（`orig_text`），所以使用者改了勾選、重新送出時，
    建議清單不會因為上一次已經換過而消失。
    """
    terms = extract_terms(context)
    if not terms or not segments:
        return {"terms": terms, "suggestions": []}
    term_fold = {t.casefold() for t in terms}
    texts = [(int(s.get("seq") or i), _base_text(s)) for i, s in enumerate(segments, 1)
             if isinstance(s, dict)]

    found: dict[str, dict[str, set[int]]] = defaultdict(lambda: defaultdict(set))

    latin_terms = [t for t in terms if not _CJK_RUN.search(t)]
    if latin_terms:
        for seq, text in texts:
            for cand in set(_latin_candidates(text)):
                for t in latin_terms:
                    if _latin_match(cand, t):
                        found[cand][t].add(seq)

    cjk_terms = [t for t in terms if _CJK_RUN.fullmatch(t)]
    if cjk_terms:
        runs = [(seq, m.group()) for seq, text in texts for m in _CJK_RUN.finditer(text)]
        index = _reading_index(runs)
        for t in cjk_terms:
            for surface, seqs in _cjk_hits(runs, t, index).items():
                found[surface][t] |= seqs

    rows = []
    for surface, by_term in found.items():
        if len(by_term) != 1:
            continue                          # 像兩個以上的詞 → 不知道該換成哪一個
        if surface.casefold() in term_fold:
            continue                          # 它自己就是背景裡的一個詞
        is_cjk = bool(_CJK_RUN.fullmatch(surface))
        # 背景自己這樣寫 → 不是錯字。英文要**整個詞**出現才算（背景寫 `Bianca`
        # 不代表 `Bianc` 是對的寫法）；中文沒有詞界，照字串比。
        if (surface in context if is_cjk
                else _latin_pattern(surface, re.IGNORECASE).search(context or "")):
            continue
        (to, seqs), = by_term.items()
        pat = _latin_pattern(surface) if not is_cjk else None
        count, example, hit_seqs = 0, "", []
        for seq, text in texts:
            if seq not in seqs:
                continue
            ms = (list(pat.finditer(text)) if pat
                  else [m for m in re.finditer(re.escape(surface), text)])
            if not ms:
                continue
            count += len(ms)
            hit_seqs.append(seq)
            if not example:
                example = _example(text, ms[0].start(), len(surface))
        if count:
            rows.append({"from": surface, "to": to, "count": count,
                         "seqs": hit_seqs[:20], "example": example})
    order = {t: i for i, t in enumerate(terms)}
    rows.sort(key=lambda r: (-r["count"], order.get(r["to"], 0), r["from"]))
    return {"terms": terms, "suggestions": rows[:MAX_SUGGESTIONS]}


def clean_pairs(pairs: object) -> list[tuple[str, str]]:
    """使用者勾選的替換 → 合格的 (原寫法, 新寫法)。長的先換，免得短的先吃掉一截。"""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    if not isinstance(pairs, list):
        return out
    for p in pairs[:MAX_PAIRS]:
        if not isinstance(p, dict):
            continue
        f, t = p.get("from"), p.get("to")
        if not isinstance(f, str) or not isinstance(t, str):
            continue
        f, t = f.strip(), t.strip()
        if (len(f) < 2 or not t or len(f) > MAX_PAIR_LEN or len(t) > MAX_PAIR_LEN
                or f == t or f in seen
                or any(not c.isprintable() for c in f + t)):
            continue
        seen.add(f)
        out.append((f, t))
    out.sort(key=lambda p: -len(p[0]))
    return out


def clean_rows(pairs: object) -> list[dict]:
    """「自己加替換」存起來的那幾列：跟 `clean_pairs` 同一套檢查，但**照使用者加入的順序**
    （`clean_pairs` 為了套用把長的排前面 —— 拿來存清單的話，重新打開時列的順序會變）。"""
    ok = set(clean_pairs(pairs))
    out: list[dict] = []
    for p in (pairs if isinstance(pairs, list) else [])[:MAX_PAIRS]:
        if not isinstance(p, dict) or not isinstance(p.get("from"), str) \
                or not isinstance(p.get("to"), str):
            continue
        f, t = p["from"].strip(), p["to"].strip()
        if (f, t) in ok:
            ok.discard((f, t))
            out.append({"from": f, "to": t})
    return out


def apply(segments: list[dict], pairs: object) -> tuple[list[dict], list[dict]]:
    """把勾選的替換套到逐字稿上。回 (新的段落, 實際換了什麼 `[{from, to, count}]`)。

    **每次都從原文重新套**：上一次換過、這次沒勾的，會換回原文。
    換過的段落把原文留在 `orig_text`；沒換到的段落不帶這個欄位。
    """
    cleaned = clean_pairs(pairs)
    pats = [(f, t, _latin_pattern(f) if not _CJK_RUN.fullmatch(f) else None)
            for f, t in cleaned]
    counts: dict[tuple[str, str], int] = defaultdict(int)
    out: list[dict] = []
    for seg in segments:
        if not isinstance(seg, dict):
            continue
        s = dict(seg)
        base = _base_text(s)
        text = base
        for f, t, pat in pats:
            if pat is not None:
                text, n = pat.subn(lambda _m, _t=t: _t, text)
            else:
                n = text.count(f)
                if n:
                    text = text.replace(f, t)
            counts[(f, t)] += n
        s["text"] = text
        if text != base:
            s["orig_text"] = base
        else:
            s.pop("orig_text", None)
        out.append(s)
    applied = [{"from": f, "to": t, "count": counts[(f, t)]}
               for f, t in cleaned if counts[(f, t)]]
    return out, applied


def find(segments: list[dict], surface: object) -> dict:
    """「自己加替換」：使用者輸入的寫法在**整份逐字稿**（替換前的原文）出現幾處。

    2026-10-03 使用者要求：辨識聽錯、上面的建議又抓不到的（`Groxmoxity` → `Proxmox`、
    `POWPOYNT` → `PowerPoint`、`PPQ`）—— 判斷規則刻意保守，抓不到的讓使用者自己指定。

    比對跟 `apply` **同一套**（英文整個詞、中文照字），畫面上的「N 處」就是勾了之後換掉的處數。
    英文**不分大小寫**找（使用者打 `groxmoxity` 也找得到 `Groxmoxity`），回傳實際出現的寫法
    （`variants`）—— 前端把每一種寫法各送一組替換，`apply` 照樣逐字比對。
    """
    s = surface.strip() if isinstance(surface, str) else ""
    if (len(s) < 2 or len(s) > MAX_PAIR_LEN or any(not c.isprintable() for c in s)):
        return {"count": 0, "variants": [], "example": ""}
    cjk = bool(_CJK_RUN.fullmatch(s))
    pat = None if cjk else _latin_pattern(s, re.IGNORECASE)
    count = 0
    seen: dict[str, int] = {}
    example = ""
    for seg in segments:
        if not isinstance(seg, dict):
            continue
        text = _base_text(seg)
        hits = ([(m.start(), m.group()) for m in pat.finditer(text)] if pat is not None
                else [(m.start(), s) for m in re.finditer(re.escape(s), text)])
        for at, got in hits:
            count += 1
            seen[got] = seen.get(got, 0) + 1
            if not example:
                example = _example(text, at, len(got))
    variants = sorted(seen, key=lambda v: -seen[v])[:MAX_PAIRS]
    return {"count": count, "variants": variants, "example": example}


def normalise_applied(rows: object) -> Optional[list[dict]]:
    """存起來 / 匯入時的 `[{from, to, count}]` —— 形狀不對的條目丟掉。"""
    if not isinstance(rows, list):
        return None
    keep = []
    for r in rows[:MAX_PAIRS]:
        if not isinstance(r, dict):
            continue
        f, t, n = r.get("from"), r.get("to"), r.get("count")
        if (isinstance(f, str) and isinstance(t, str) and f and t
                and len(f) <= MAX_PAIR_LEN and len(t) <= MAX_PAIR_LEN
                and isinstance(n, int) and not isinstance(n, bool) and n > 0):
            keep.append({"from": f, "to": t, "count": n})
    return keep
