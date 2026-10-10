"""知識庫檢索：先過濾（權限、資料集、啟用中的版本），再關鍵字 ＋ 向量，向量優先。

## 順序（規格 12）

1. **過濾在前**：只看這個人看得到、資料集開著、版本是「啟用」的段落。
   過濾放在檢索之後的話，「前 20 筆」可能全是他看不到的，結果變成空的。
2. 關鍵字（FTS5 逐字）與向量各取最多 `PER_ROUTE` 筆。
3. **有向量索引時照向量的名次排**；關鍵字那一路只在向量找到的（過了下限的）不到 `k` 筆時
   補在後面。沒有向量索引、或嵌入服務這次沒回應時，只用關鍵字。
4. 回最多 `k` 段。**不為了湊數塞無關的**：過不了下面門檻的不會進候選。

### 為什麼不是兩路合併（RRF）

原本是關鍵字與向量等權的 RRF（`Σ 1 / (RRF_K + 名次)`）。2026-10-09 用實際部署的公文知識庫
（1,503 段、Nemotron-3-Embed-8B）量 42 題語意題（前 6 筆全部盲判過）＋ 22 題條號查詢：

| 做法 | nDCG@6 | 中篇需求 | 條號查詢 MRR |
|---|---|---|---|
| 只照向量 | **0.885** | **0.884** | 0.880 |
| 等權 RRF（原本） | 0.787 | 0.733 | 0.909 |
| 關鍵字權重 0.3 | 0.847 | 0.810 | 0.955 |
| 關鍵字權重 0.1 | 0.872 | 0.850 | 0.917 |

**每一種關鍵字權重都輸只照向量**（先前 qwen3-embedding、EmbeddingGemma 也一樣）。
條號查詢（「訴願法第 25 條」）每一種做法的前 6 筆都找得到那一條，只差排第幾。
另外試過「問題寫第 N 條時，把關鍵字幾乎全中的那一段放第一」，條號 MRR 反而掉到 0.856 ——
關鍵字全中的常常是別的法規的同號條文，沒有採用。

## 分數不是正確率

`score` 只代表**檢索排序**（由名次換算，`1 / (RRF_K + 名次)`），不是「這段話支持你的論點的機率」。
畫面與文件都要這樣講 —— 規格：「相似度只能表示檢索排序，不能標成法律正確率」。

## 門檻（`tools/kb_eval/` 用《文書處理手冊》20 題 ＋ 5 題無關問題量出來的）

**關鍵字**：問題切成相鄰兩字，每個詞依 IDF 加權（知識庫裡根本沒出現的詞權重 0）。

* 覆蓋率（命中的權重 ÷ 全部權重）≥ `KW_MIN_COVERAGE`，而且
* 命中的權重總和要夠：以「一個只出現在一段裡的詞」的權重為 1，
  ≥ `KW_MIN_MASS` 才算數。**只看覆蓋率不夠** —— 無關的問題（「如何烤出好吃的
  戚風蛋糕」）只有「如何」這個詞在知識庫裡出現過，覆蓋率是 100%，實測第一版
  每一題無關問題都回滿 8 段。
* 總和介於 `KW_WEAK_MASS` 與 `KW_MIN_MASS` 之間的（例如「發文字號共有幾碼」只有
  「發文」「字號」這種常見詞對得上）**只在向量那一路也找到它時**才算 —— 兩路都說
  有關，才比較可能真的有關。

**向量**：**不用固定的 cosine 門檻，也不用 z 值**。實測（188 段）：

| | 無關問題的第一名 cosine | 相關問題命中段的 cosine |
|---|---|---|
| qwen3-embedding:8b | 0.29～0.43 | 0.55～0.86 |
| embeddinggemma-GTAIDE | 0.07～0.18 | 0.37～0.68 |

兩個模型都分得開，但**分界差了一倍以上** —— 固定門檻換個模型就錯。z 值（比平均
高幾個標準差）分不開：無關問題的第一名 z 值一樣有 3.3（樣本裡的最大值本來就會
落在 3σ 附近）。做法是**每一份索引自己校正**：拿 `NULL_QUERIES`（一組跟公文
八竿子打不著的問題）去查，取它們第一名 cosine 的最大值 ＋ `NULL_MARGIN`
當這份索引的下限（`vector_floor()`）。換模型、加文件都會自動跟著變。
"""
from __future__ import annotations

import base64
import contextlib
import json
import math
import re
import threading
import time
from typing import Optional

from ...logging_setup import get_logger
from .. import cjk_fts
from . import access, store

logger = get_logger(__name__)

#: 每一路最多取幾筆候選。
PER_ROUTE = 20
#: 名次換成排序分數用的常數（`1 / (RRF_K + 名次)`；原本 RRF 合併用的，數值沿用，分數尺度不變）。
RRF_K = 60
#: 問題最長幾個字（再長也只是更多雜訊；也防有人塞一整份文件進來）。
MAX_QUERY = 500
#: 回傳筆數上限。
MAX_K = 30

#: 關鍵字路線：IDF 加權後至少這個比例的檢索詞要出現在段落裡。
KW_MIN_COVERAGE = 0.30
#: 命中權重總和（以「只出現在一段的詞」為 1）至少多少才單獨算數。
KW_MIN_MASS = 1.05
#: 低於 KW_MIN_MASS、但至少這麼多的，要向量那一路也找到才算。
KW_WEAK_MASS = 0.5
#: FTS 先撈多少筆再算覆蓋率（OR 查詢會撈到很多只沾一兩個字的段落）。
KW_CANDIDATES = 300
#: **問題裡的詞幾乎都出現在同一段**也算數，不管那些詞多常見：上面那個門檻是拿「只出現在一段的詞」
#: 當一倍，知識庫一大（匯入整批法規之後一千多段），「機密」「文書」「解密」這種詞每個都出現在
#: 幾十段裡，權重都小 —— 一段裡三個都有，加起來照樣不到一倍，只用關鍵字時一筆都找不到
#: （2026-10-08 匯入政府公開資料後實測：「機密文書 解密」0 筆，連在一起寫的「機密文書解密」
#: 反而有，因為跨詞的「書解」剛好很少見）。至少兩個詞、加權覆蓋率與「出現的詞佔問題的幾成」
#: 都要夠，不相干的問題剛好沾到兩個常見詞的不算。
KW_ALL_COVERAGE = 0.90
KW_ALL_TERMS = 0.60

#: 校正向量下限用的「無關問題」。**刻意挑跟政府行政、法規、公文都無關的主題**
#: （料理、寵物、運動、遊戲、音樂…）—— 知識庫裡要是真的有某一類，例如機關的
#: 觀光業務法規，「週末去哪裡露營」就不再是無關問題，下限會被拉高、相關結果變少。
#: **這一組跟評估工具的無關問題刻意不同**（不然評估會被自己的校正騙過）。
NULL_QUERIES = (
    "戚風蛋糕烤完為什麼會塌下來",
    "貓咪一直掉毛該怎麼辦",
    "昨晚籃球比賽最後誰贏了",
    "這款手機遊戲的角色要怎麼升級",
    "吉他的大橫按和弦怎麼按比較不會痛",
    "義大利麵要煮幾分鐘才會有嚼勁",
    "recommend a good sci-fi novel to read this summer",
    "how do I fix a flat bicycle tire",
)
#: 下限 ＝ 無關問題第一名 cosine 的最大值 ＋ 這個餘裕。
NULL_MARGIN = 0.02

SCORE_NOTE = "分數只代表檢索排序，不是正確率或法律效力的判斷。"

#: 標出符合處（檢索測試的高亮）：出現在超過這個比例段落裡的詞太常見，單獨出現時不標 ——
#: 實際部署的公文知識庫實測「機關」在 77% 的段落、「條」在 79%，照標的話滿頁都是。
#: 它跟別的詞接在一起時（「上級機關」）照樣一起標。
HL_COMMON = 0.5


# ---------------------------------------------------------------- 允許的版本
def _allowed_versions(user_id: Optional[int], dataset_ids: Optional[list]) -> dict[str, dict]:
    """這個人這次可以用的版本 → 該版本與資料集的資訊。"""
    vis = access.visible_dataset_ids(user_id)
    want = None
    if dataset_ids is not None:
        want = {d for d in dataset_ids if store.is_id(d)}
        if not want:
            return {}
    c = store.conn()
    rows = c.execute(
        "SELECT v.id AS vid, v.title, v.version_label, v.published_on, v.effective_on, "
        "v.source_url, v.publisher, d.id AS did, d.name AS dname, d.category, "
        "g.attribution AS attribution, g.notice AS notice "
        "FROM kb_versions v JOIN kb_datasets d ON d.id = v.dataset_id "
        "LEFT JOIN kb_gov_versions g ON g.version_id = v.id "
        "WHERE v.status='active' AND d.enabled=1").fetchall()
    out: dict[str, dict] = {}
    for r in rows:
        if vis is not None and r["did"] not in vis:
            continue
        if want is not None and r["did"] not in want:
            continue
        out[r["vid"]] = dict(r)
    return out


# ---------------------------------------------------------------- 關鍵字
def _weight(df: int, n_docs: int) -> float:
    """IDF 權重。**整個知識庫都沒出現過的詞權重是 0**：問題被切成相鄰兩字時，
    跨詞的組合（「語對」「關怎」「麼寫」）在文件裡根本不存在，照 IDF 公式它們
    反而權重最高，一算覆蓋率就把每一段都拉到門檻以下 —— 第一版就是這樣，
    「函的期望語對上級機關怎麼寫」一筆都找不到。"""
    if df <= 0:
        return 0.0
    return math.log((n_docs - df + 0.5) / (df + 0.5) + 1.0)


def _idf(c, terms: list[str], n_docs: int) -> dict[str, float]:
    w: dict[str, float] = {}
    for t in terms:
        try:
            df = c.execute("SELECT count(*) FROM kb_fts WHERE kb_fts MATCH ?",
                           (cjk_fts.term_to_phrase(t),)).fetchone()[0]
        except Exception:
            df = 0
        w[t] = _weight(df, n_docs)
    return w


def keyword_hits(q: str, allowed: dict[str, dict], limit: int = PER_ROUTE) -> list[dict]:
    """回 `[{id, coverage, mass, strong}]`，依覆蓋率（同分照 bm25）。

    `strong` 為 False 的是「弱命中」：只有在向量那一路也找到時才算數（見模組說明）。
    """
    terms = cjk_fts.query_terms(q)
    if not terms or not allowed:
        return []
    c = store.conn()
    vids = json.dumps(sorted(allowed))
    n_docs = c.execute("SELECT count(*) FROM kb_chunks").fetchone()[0] or 1
    if store.has_fts(c):
        match = cjk_fts.match_query(terms)
        rows = c.execute(
            "SELECT k.id, k.heading, k.text FROM kb_fts JOIN kb_chunks k ON k.rid = kb_fts.rowid "
            "WHERE kb_fts MATCH ? AND k.version_id IN (SELECT value FROM json_each(?)) "
            "ORDER BY bm25(kb_fts) LIMIT ?", (match, vids, KW_CANDIDATES)).fetchall()
        cands = [(r["id"], (r["heading"] or "") + "\n" + r["text"]) for r in rows]
        weights = _idf(c, terms, n_docs)
    else:
        # 沒有 FTS5 的 SQLite：逐段比對（知識庫是手冊級的量，幾千段撐得住）
        all_rows = c.execute("SELECT id, heading, text FROM kb_chunks").fetchall()
        comp_all = [cjk_fts.compact((r["heading"] or "") + "\n" + r["text"]) for r in all_rows]
        weights = {t: _weight(sum(1 for x in comp_all if t in x), n_docs) for t in terms}
        rows = c.execute("SELECT id, heading, text FROM kb_chunks "
                         "WHERE version_id IN (SELECT value FROM json_each(?))",
                         (vids,)).fetchall()
        cands = [(r["id"], (r["heading"] or "") + "\n" + r["text"]) for r in rows]
    unit = _weight(1, n_docs) or 1.0
    out = []
    for cid, text in cands:
        comp = cjk_fts.compact(text)
        cov = cjk_fts.coverage(terms, comp, weights)
        present = [t for t in terms if t in comp]
        mass = sum(weights.get(t, 0.0) for t in present) / unit
        n_hit = sum(1 for t in present if weights.get(t, 0.0) > 0)
        all_in = (n_hit >= 2 and cov >= KW_ALL_COVERAGE
                  and len(present) / len(terms) >= KW_ALL_TERMS)
        if cov >= KW_MIN_COVERAGE and (mass >= KW_WEAK_MASS or all_in):
            out.append({"id": cid, "coverage": cov, "mass": mass,
                        "strong": mass >= KW_MIN_MASS or all_in})
    # 先依覆蓋率、再依 FTS 原本的順序（bm25）—— sort 是穩定的
    out.sort(key=lambda d: -round(d["coverage"], 3))
    return out[:limit]


# ---------------------------------------------------------------- 向量
#
# 向量整份放在記憶體（一個 numpy 矩陣），每次查詢對全部算一次內積。
# 規模是量過的（2026-10-09，現行法律＋命令全部匯入＝161,781 段、4096 維）：
# 矩陣 2.65 GB、整份乘一次 0.08 秒。原本的寫法有三個地方在那個規模下撐不住：
#
# * 查詢時先挑出看得到的列（`mat[idx]`）再乘 —— **每次複製一份 2.65 GB**，
#   一次查詢 7～9 秒、記憶體多 2.65 GB（兩個人同時查，10 GB 的主機就不夠）。
#   現在先整份乘、再把看不到的分數設成負無限大。
# * 載入時 `fetchall` 把每一列的位元組留著、再 `vstack` 複製一份 —— 常駐 5.3 GB（兩倍）。
#   現在先算列數、配好陣列、逐列填進去；**停用的版本不載**（查不到的東西不必佔記憶體）。
#   要重載時**先放掉舊的**再載新的（新舊同時在記憶體也是兩倍），而且只讓一個執行緒載。
# * 每寫一批向量、每匯入一份文件，世代計數就加一，下一次查詢整份重載（那個規模要 18 秒）。
#   大量寫入（政府資料匯入、文件匯入、重建索引）期間沿用手上的矩陣，最久 `BULK_RELOAD_EVERY` 秒
#   才重載一次；做完之後下一次查詢再載。這段時間新加的段落先只有關鍵字找得到。
#   **沿用舊矩陣不會放出看不到的東西**：結果最後照資料庫目前的狀態過濾（`search_detail`）。
_VEC_LOCK = threading.Lock()
_VEC_BUILD_LOCK = threading.Lock()
_VEC_CACHE: dict[tuple[str, str], dict] = {}

#: 大量寫入期間，向量矩陣最久沿用多少秒才重載。
BULK_RELOAD_EVERY = 600.0
_BULK = 0
_BULK_LOCK = threading.Lock()


@contextlib.contextmanager
def bulk_update():
    """大量寫入期間包住它：查詢沿用手上的向量矩陣，不必每寫一筆就整份重載。"""
    global _BULK
    with _BULK_LOCK:
        _BULK += 1
    try:
        yield
    finally:
        with _BULK_LOCK:
            _BULK -= 1


def _usable(hit: Optional[dict], dim: int, gen: int) -> bool:
    if not hit or hit["dim"] != dim:
        return False
    if hit["gen"] == gen:
        return True
    return _BULK > 0 and time.monotonic() - hit["loaded_at"] < BULK_RELOAD_EVERY


def _build_entry(fp: str, dim: int, gen: int) -> dict:
    """從資料庫讀出這個 fingerprint、啟用中版本的向量，逐列填進預先配好的矩陣。"""
    import numpy as np
    c = store.conn()
    where = ("FROM kb_vectors v JOIN kb_chunks k ON k.id = v.chunk_id "
             "JOIN kb_versions ver ON ver.id = k.version_id "
             "WHERE v.fingerprint=? AND v.dim=? AND ver.status='active'")
    n = int(c.execute("SELECT count(*) " + where, (fp, dim)).fetchone()[0])
    mat = np.empty((n, dim), dtype=np.float32)
    codes = np.empty(n, dtype=np.int32)
    ids: list[str] = []
    vids: list[str] = []
    vid_code: dict[str, int] = {}
    extra: list = []          # 算完列數之後又多出來的（同時有匯入）
    need = dim * 4
    i = 0
    for r in c.execute("SELECT v.chunk_id, k.version_id, v.vec " + where, (fp, dim)):
        blob = r[2]
        if len(blob) != need:
            continue
        code = vid_code.setdefault(r[1], len(vid_code))
        row = np.frombuffer(blob, dtype=np.float32)
        if i < n:
            mat[i] = row
            codes[i] = code
        else:
            extra.append((row.copy(), code))
        ids.append(r[0])
        vids.append(r[1])
        i += 1
    if extra:
        mat = np.vstack([mat, np.stack([x[0] for x in extra])])
        codes = np.concatenate([codes, np.asarray([x[1] for x in extra], dtype=np.int32)])
    elif i < n:
        mat, codes = mat[:i], codes[:i]
    return {"gen": gen, "dim": dim, "ids": ids, "vids": vids, "mat": mat, "codes": codes,
            "vid_code": vid_code, "loaded_at": time.monotonic(), "floor": None}


def _entry(fp: str, dim: int) -> dict:
    key = (str(store.db_path()), fp)
    gen = store.generation()
    with _VEC_LOCK:
        hit = _VEC_CACHE.get(key)
    if _usable(hit, dim, gen):
        return hit
    hit = None
    with _VEC_BUILD_LOCK:          # 同時好幾個查詢進來，只讓一個去載
        gen = store.generation()
        with _VEC_LOCK:
            hit = _VEC_CACHE.get(key)
            if _usable(hit, dim, gen):
                return hit
            # 先放掉手上的舊矩陣（含換模型前的 fingerprint）再載 —— 新舊同時在記憶體會是兩倍。
            # 正式環境只有一份資料庫，所以整份清掉就是「放掉舊的」。
            _VEC_CACHE.clear()
        hit = None
        e = _build_entry(fp, dim, gen)
        with _VEC_LOCK:
            _VEC_CACHE[key] = e
        return e


def _load_vectors(fp: str, dim: int):
    """回 (段落 id 清單, 版本 id 清單, 矩陣)。依世代計數快取在記憶體（只含啟用中的版本）。"""
    e = _entry(fp, dim)
    return e["ids"], e["vids"], e["mat"]


def null_vectors(cli, fp: str):
    """這份索引的「無關問題」查詢向量。第一次用到時嵌入一次、存進 kb_meta。"""
    import numpy as np
    key = "null_qv:" + fp
    raw = store.meta_get(key)
    if raw:
        try:
            d = json.loads(raw)
            if d.get("queries") == list(NULL_QUERIES):
                arr = np.frombuffer(base64.b64decode(d["vecs"]), dtype=np.float32)
                return arr.reshape(len(NULL_QUERIES), -1)
        except (ValueError, KeyError, TypeError):
            pass
    m = cli.embed([cli.query_text(q) for q in NULL_QUERIES])
    store.meta_set(key, json.dumps({
        "queries": list(NULL_QUERIES),
        "vecs": base64.b64encode(np.ascontiguousarray(m, dtype=np.float32).tobytes()).decode("ascii"),
    }))
    return m


def vector_floor(cli, fp: str, dim: int) -> Optional[float]:
    """這份索引的 cosine 下限：無關問題第一名 cosine 的最大值 ＋ 餘裕。

    用**整份**知識庫（啟用中的段落）算，不是這個人看得到的那幾段 —— 下限不該因為誰在查而不同。
    跟著載入的矩陣算一次就記住（整份乘十幾個無關問題，十六萬段要零點幾秒，不必每次查詢都算）。
    """
    e = _entry(fp, dim)
    if not e["ids"]:
        return None
    if e["floor"] is not None:
        return e["floor"]
    nv = null_vectors(cli, fp)
    if nv.shape[1] != e["mat"].shape[1]:
        return None
    top = (e["mat"] @ nv.T).max(axis=0)
    e["floor"] = float(top.max()) + NULL_MARGIN
    return e["floor"]


def vector_hits(qv, fp: str, allowed: dict[str, dict], limit: int = PER_ROUTE,
                floor: Optional[float] = None) -> list[dict]:
    """回 `[{id, cos}]`，依 cosine 由高到低，只留 ≥ `floor` 的。

    **只用 fingerprint 相同的向量**（換過模型的舊向量一筆都不會被拿來比）。
    **不複製子矩陣**：整份乘一次，看不到的分數設成負無限大（理由見本節開頭）。
    """
    import numpy as np
    if not allowed:
        return []
    e = _entry(fp, int(qv.shape[0]))
    if not e["ids"]:
        return []
    want = [e["vid_code"][v] for v in allowed if v in e["vid_code"]]
    if not want:
        return []
    mask = np.isin(e["codes"], np.asarray(want, dtype=np.int32))
    n_ok = int(mask.sum())
    if not n_ok:
        return []
    sims = e["mat"] @ np.asarray(qv, dtype=np.float32)
    sims[~mask] = -np.inf
    k = min(int(limit), n_ok)
    if k <= 0:
        return []
    top = np.argpartition(-sims, k - 1)[:k]
    top = top[np.argsort(-sims[top], kind="stable")]
    ids = e["ids"]
    out = []
    for j in top:
        cos = float(sims[j])
        if floor is not None and cos < floor:
            break
        out.append({"id": ids[int(j)], "cos": cos})
    return out


# ---------------------------------------------------------------- 合併與輸出
def _locator_text(ch: dict) -> str:
    pf, pt = ch.get("page_from"), ch.get("page_to")
    if pf:
        return f"第 {pf} 頁" if not pt or pt == pf else f"第 {pf}～{pt} 頁"
    return ch.get("heading") or ""


def result_dict(ch: dict, info: dict, *, active: bool = True) -> dict:
    cat = info.get("category")
    label, purpose = store.CATEGORIES.get(cat, (cat, "format_reference"))
    loc: dict = {}
    if ch.get("page_from"):
        loc["pages"] = [ch["page_from"], ch.get("page_to") or ch["page_from"]]
    if ch.get("heading"):
        loc["section"] = ch["heading"]
    if ch.get("parent_ref"):
        loc["parent_ref"] = ch["parent_ref"]
    return {
        "chunk_id": ch["id"],
        "version_id": ch["version_id"],
        "dataset_id": info.get("did") or info.get("dataset_id"),
        "dataset_name": info.get("dname") or info.get("dataset_name"),
        "category": cat, "category_label": label,
        "purpose": purpose, "purpose_label": store.PURPOSES.get(purpose, purpose),
        "title": info.get("title"),
        "version_label": info.get("version_label") or "",
        "published_on": info.get("published_on") or "",
        "effective_on": info.get("effective_on") or "",
        "publisher": info.get("publisher") or "",
        "source_url": info.get("source_url") or "",
        # 政府公開資料匯入的版本：顯名文字（授權條款要求的出處）與施行日期說明；其他文件是空字串
        "attribution": info.get("attribution") or "",
        "notice": info.get("notice") or "",
        "heading": ch.get("heading") or "",
        "heading_path": ch.get("heading_path") or [],
        "parent_ref": ch.get("parent_ref") or "",
        "part": ch.get("part", 1), "parts": ch.get("parts", 1),
        "locator": loc,
        "locator_text": _locator_text(ch),
        "text": ch["text"],
        "prev_id": ch.get("prev_id") or "",
        "next_id": ch.get("next_id") or "",
        "active": bool(active),
    }


# ---------------------------------------------------------------- 標出符合處
def _doc_freqs(c, terms: list[str]) -> dict[str, int]:
    """每個檢索詞出現在幾段裡（跟關鍵字那一路同一套比對）。"""
    out: dict[str, int] = {}
    if store.has_fts(c):
        for t in terms:
            try:
                out[t] = c.execute("SELECT count(*) FROM kb_fts WHERE kb_fts MATCH ?",
                                   (cjk_fts.term_to_phrase(t),)).fetchone()[0]
            except Exception:
                out[t] = 0
        return out
    comp_all = [cjk_fts.compact((r["heading"] or "") + "\n" + r["text"])
                for r in c.execute("SELECT heading, text FROM kb_chunks").fetchall()]
    return {t: sum(1 for x in comp_all if t in x) for t in terms}


def highlight_terms(query: str) -> dict[str, float]:
    """問題拆出的檢索詞 → 它在知識庫裡出現的比例（0～1）。知識庫裡根本沒有的詞不回。"""
    terms = cjk_fts.query_terms(query)
    if not terms:
        return {}
    c = store.conn()
    n = c.execute("SELECT count(*) FROM kb_chunks").fetchone()[0] or 1
    return {t: df / n for t, df in _doc_freqs(c, terms).items() if df > 0}


def _word_char(ch: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-z]+", cjk_fts.normalize(ch) or ""))


def highlight_spans(text: str, terms: dict[str, float]) -> list[list[int]]:
    """這段內文裡哪幾段字要標出來：`[[起, 迄), ...]`，位置是**原文**的字元索引。

    比對跟關鍵字那一路相同（正規化、拿掉空白之後比子字串），所以換行或空白夾在
    兩個字中間照樣標得到；換算回原文時，一段標記從第一個字涵蓋到最後一個字。
    一段連在一起的符合處，至少要有一個「兩個字以上、而且不是太常見」的詞才標
    （見 `HL_COMMON`）—— 單獨一個字、或單獨一個到處都有的詞不標。
    英數詞要整個詞符合（`2` 不標在 `2026` 裡面）。
    """
    if not text or not terms:
        return []
    chars: list[str] = []
    where: list[int] = []
    for i, ch in enumerate(text):
        if ch.isspace():
            continue
        for nc in cjk_fts.normalize(ch):
            if not nc.isspace():
                chars.append(nc)
                where.append(i)
    s = "".join(chars)
    if not s:
        return []
    hit = [False] * len(s)
    strong = [False] * len(s)
    for t, ratio in terms.items():
        if not t:
            continue
        word = t.isascii()
        good = len(t) >= 2 and ratio <= HL_COMMON
        start = s.find(t)
        while start >= 0:
            end = start + len(t)
            # 英數詞的邊界要看**原文**：拿掉空白之後「vmware esxi」的 esxi 前面緊接著 e
            a0, b0 = where[start], where[end - 1]
            if not word or ((a0 == 0 or not _word_char(text[a0 - 1]))
                            and (b0 + 1 >= len(text) or not _word_char(text[b0 + 1]))):
                for k in range(start, end):
                    hit[k] = True
                    if good:
                        strong[k] = True
            start = s.find(t, start + 1)
    spans: list[list[int]] = []
    k = 0
    while k < len(s):
        if not hit[k]:
            k += 1
            continue
        j = k
        while j + 1 < len(s) and hit[j + 1]:
            j += 1
        if any(strong[k:j + 1]):
            a, b = where[k], where[j] + 1
            if spans and spans[-1][1] >= a:
                spans[-1][1] = max(spans[-1][1], b)
            else:
                spans.append([a, b])
        k = j + 1
    return spans


def search_detail(query: str, *, user_id: Optional[int],
                  dataset_ids: Optional[list] = None, k: int = 8,
                  highlight: bool = False) -> dict:
    """完整版：`{"results": [...], "mode": "hybrid"|"keyword", "note": "...",
    "score_note": SCORE_NOTE}`。`mode` 講出這次有沒有用到向量。

    `highlight=True`（檢索測試頁）：每一筆另附 `highlights`（`highlight_spans`）。
    公文撰擬查參考資料時不用（多幾次計數查詢，畫面也用不到）。"""
    from . import embed
    q = (query or "").strip()[:MAX_QUERY]
    k = max(1, min(int(k or 8), MAX_K))
    out = {"results": [], "mode": "keyword", "note": "", "score_note": SCORE_NOTE,
           "query": q}
    if not q:
        return out
    allowed = _allowed_versions(user_id, dataset_ids)
    snap = embed.active_snapshot()
    if snap is None:
        out["note"] = "還沒有建立向量索引，這次只用關鍵字找。"
    if not allowed:
        if snap is not None:
            out["mode"] = "hybrid"
        return out

    kw = keyword_hits(q, allowed)
    vec: list[dict] = []
    if snap is not None:
        try:
            cli = embed.EmbedClient.from_snapshot(snap)
            qv = cli.embed_query(q)
            if int(qv.shape[0]) != int(snap.get("dim") or 0):
                raise embed.EmbedError("查詢向量的維度跟索引不同（嵌入服務換了模型？），請重建索引。")
            floor = vector_floor(cli, snap["fingerprint"], int(snap["dim"]))
            vec = vector_hits(qv, snap["fingerprint"], allowed, floor=floor)
            out["mode"] = "hybrid"
        except embed.EmbedError as e:
            logger.warning("知識庫檢索：向量那一路失敗，只用關鍵字（%s）", e)
            out["note"] = "嵌入服務這次沒有回應，只用關鍵字找。"

    # 弱命中只有在向量那一路也找到時才算（兩路都說有關才比較可能真的有關）
    vec_ids = {h["id"] for h in vec}
    kw_ok = [h for h in kw if h["strong"] or h["id"] in vec_ids]
    kw_ids = {h["id"] for h in kw_ok}
    via: dict[str, list[str]] = {}
    order: list[str] = []
    # 有向量時照向量的名次（理由與量測見模組說明「為什麼不是兩路合併」）
    for h in vec:
        order.append(h["id"])
        via[h["id"]] = ["vector"] + (["keyword"] if h["id"] in kw_ids else [])
    # 關鍵字補在後面：向量找到的不到 k 筆時才看得到；沒有向量時就是全部
    for h in kw_ok:
        if h["id"] not in via:
            order.append(h["id"])
            via[h["id"]] = ["keyword"]
    ranked = [(cid, 1.0 / (RRF_K + n)) for n, cid in enumerate(order[:k], start=1)]
    if not ranked:
        return out
    c = store.conn()
    rows = c.execute("SELECT * FROM kb_chunks WHERE id IN (SELECT value FROM json_each(?))",
                     (json.dumps([cid for cid, _ in ranked]),)).fetchall()
    by_id = {r["id"]: store.chunk_row(r) for r in rows}
    hl = highlight_terms(q) if highlight else {}
    for i, (cid, sc) in enumerate(ranked, start=1):
        ch = by_id.get(cid)
        if not ch or ch["version_id"] not in allowed:
            continue
        d = result_dict(ch, allowed[ch["version_id"]])
        d.update(rank=i, score=round(sc, 6), matched_by=via.get(cid, []), mode=out["mode"])
        if highlight:
            d["highlights"] = highlight_spans(d["text"], hl)
        out["results"].append(d)
    return out


def search(query: str, *, user_id: Optional[int], dataset_ids: Optional[list] = None,
           k: int = 8) -> list[dict]:
    """給工具用的版本：只回段落清單（每一筆的 `mode` 講出這次有沒有用到向量）。"""
    return search_detail(query, user_id=user_id, dataset_ids=dataset_ids, k=k)["results"]


def get_chunk(chunk_id: str, *, user_id: Optional[int]) -> Optional[dict]:
    """取一段原文與定位。**看不到（權限）與不存在都回 None。**

    停用中的版本照樣取得到（歷史草稿要能顯示當時的依據），但 `active` 是 False；
    權限撤銷或資料被刪掉就取不到（規格 S02：「歷史原文預覽均不再洩露」）。
    """
    ch = store.get_chunk_row(chunk_id)
    if not ch:
        return None
    r = store.conn().execute(
        "SELECT v.id AS vid, v.status, v.title, v.version_label, v.published_on, "
        "v.effective_on, v.source_url, v.publisher, d.id AS did, d.name AS dname, "
        "d.category, d.enabled, g.attribution AS attribution, g.notice AS notice "
        "FROM kb_versions v JOIN kb_datasets d ON d.id=v.dataset_id "
        "LEFT JOIN kb_gov_versions g ON g.version_id = v.id "
        "WHERE v.id=?", (ch["version_id"],)).fetchone()
    if not r or not access.can_see_dataset(user_id, r["did"]):
        return None
    d = result_dict(ch, dict(r), active=(r["status"] == "active" and bool(r["enabled"])))
    d["status"] = r["status"]
    return d


def open_original(version_id: str, *, user_id: Optional[int]) -> Optional[dict]:
    """原檔的位置（給呼叫端做下載）。權限不足 / 不存在回 None。"""
    try:
        v = store.get_version(version_id)
    except store.KBNotFound:
        return None
    if not access.can_see_dataset(user_id, v["dataset_id"]):
        return None
    p = store.stored_path(v)
    if not p.is_file():
        return None
    media = {".pdf": "application/pdf",
             ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
             ".odt": "application/vnd.oasis.opendocument.text",
             ".txt": "text/plain; charset=utf-8", ".md": "text/markdown; charset=utf-8"}
    return {"path": p, "filename": v["filename"], "media_type": media.get(v["ext"], "application/octet-stream")}


def list_datasets(*, user_id: Optional[int]) -> list[dict]:
    """這個人看得到、而且**有啟用中文件**的資料集。

    資料庫檔還不存在（從來沒用過知識庫）就是沒有資料集 —— 不要為了回答這一句把它建出來
    （公文撰擬頁每次打開都會問）。"""
    if not store.db_path().exists():
        return []
    vis = access.visible_dataset_ids(user_id)
    c = store.conn()
    rows = c.execute(
        "SELECT d.*, COUNT(v.id) AS docs, COALESCE(SUM(v.chunk_count),0) AS chunks "
        "FROM kb_datasets d JOIN kb_versions v ON v.dataset_id=d.id AND v.status='active' "
        "WHERE d.enabled=1 GROUP BY d.id ORDER BY d.name").fetchall()
    out = []
    for r in rows:
        if vis is not None and r["id"] not in vis:
            continue
        label, purpose = store.CATEGORIES.get(r["category"], (r["category"], "format_reference"))
        out.append({"id": r["id"], "name": r["name"], "category": r["category"],
                    "category_label": label, "purpose": purpose,
                    "purpose_label": store.PURPOSES.get(purpose, purpose),
                    "description": r["description"], "active_docs": r["docs"],
                    "active_chunks": r["chunks"]})
    return out
