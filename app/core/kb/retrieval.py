"""知識庫檢索：先過濾（權限、資料集、啟用中的版本），再關鍵字 ＋ 向量，RRF 合併。

## 順序（規格 12）

1. **過濾在前**：只看這個人看得到、資料集開著、版本是「啟用」的段落。
   過濾放在檢索之後的話，「前 20 筆」可能全是他看不到的，結果變成空的。
2. 關鍵字（FTS5 逐字）與向量各取最多 `PER_ROUTE` 筆。
3. RRF（Reciprocal Rank Fusion）合併：`Σ 1 / (RRF_K + 名次)`。
   兩路的分數尺度完全不同（覆蓋率與 cosine），RRF 只看名次，不必調權重。
4. 回最多 `k` 段。**不為了湊數塞無關的**：過不了下面門檻的不會進候選。

## 分數不是正確率

`score` 只代表**檢索排序**（RRF 值），不是「這段話支持你的論點的機率」。
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
import json
import math
import threading
from typing import Optional

from ...logging_setup import get_logger
from .. import cjk_fts
from . import access, store

logger = get_logger(__name__)

#: 每一路最多取幾筆候選。
PER_ROUTE = 20
#: RRF 的平滑常數（文獻慣用 60）。
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
_VEC_LOCK = threading.Lock()
_VEC_CACHE: dict[tuple[str, str], tuple] = {}


def _load_vectors(fp: str, dim: int):
    """回 (段落 id 清單, 版本 id 清單, 矩陣)。依世代計數快取在記憶體。"""
    import numpy as np
    key = (str(store.db_path()), fp)
    gen = store.generation()
    with _VEC_LOCK:
        hit = _VEC_CACHE.get(key)
        if hit and hit[0] == gen and hit[4] == dim:
            return hit[1], hit[2], hit[3]
    rows = store.conn().execute(
        "SELECT v.chunk_id, k.version_id, v.dim, v.vec FROM kb_vectors v "
        "JOIN kb_chunks k ON k.id = v.chunk_id WHERE v.fingerprint=?", (fp,)).fetchall()
    ids, vids, vecs = [], [], []
    for r in rows:
        if r["dim"] != dim:
            continue      # 維度不同的向量不可以拿來比（fingerprint 本來就排除，這是第二道）
        ids.append(r["chunk_id"])
        vids.append(r["version_id"])
        vecs.append(np.frombuffer(r["vec"], dtype=np.float32))
    mat = np.vstack(vecs) if vecs else np.zeros((0, dim), dtype=np.float32)
    with _VEC_LOCK:
        _VEC_CACHE[key] = (gen, ids, vids, mat, dim)
        # 舊的 fingerprint 不會再用到，順手丟掉（重建之後換 fingerprint）
        for k in [k for k in _VEC_CACHE if k[0] == key[0] and k[1] != fp]:
            _VEC_CACHE.pop(k, None)
    return ids, vids, mat


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

    用**整份**知識庫的段落算（不是這個人看得到的那幾段）—— 下限不該因為誰在查而不同。
    """
    ids, _vids, mat = _load_vectors(fp, dim)
    if not ids:
        return None
    nv = null_vectors(cli, fp)
    if nv.shape[1] != mat.shape[1]:
        return None
    top = (mat @ nv.T).max(axis=0)
    return float(top.max()) + NULL_MARGIN


def vector_hits(qv, fp: str, allowed: dict[str, dict], limit: int = PER_ROUTE,
                floor: Optional[float] = None) -> list[dict]:
    """回 `[{id, cos}]`，依 cosine 由高到低，只留 ≥ `floor` 的。

    **只用 fingerprint 相同的向量**（換過模型的舊向量一筆都不會被拿來比）。
    """
    import numpy as np
    if not allowed:
        return []
    ids, vids, mat = _load_vectors(fp, int(qv.shape[0]))
    if not ids:
        return []
    mask = np.fromiter((v in allowed for v in vids), dtype=bool, count=len(vids))
    if not mask.any():
        return []
    idx = np.nonzero(mask)[0]
    sims = mat[idx] @ qv.astype(np.float32)
    order = np.argsort(-sims)
    out = []
    for j in order[:limit]:
        cos = float(sims[j])
        if floor is not None and cos < floor:
            break
        out.append({"id": ids[idx[j]], "cos": cos})
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


def search_detail(query: str, *, user_id: Optional[int],
                  dataset_ids: Optional[list] = None, k: int = 8) -> dict:
    """完整版：`{"results": [...], "mode": "hybrid"|"keyword", "note": "...",
    "score_note": SCORE_NOTE}`。`mode` 講出這次有沒有用到向量。"""
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

    vec_ids = {h["id"] for h in vec}
    scores: dict[str, float] = {}
    via: dict[str, list[str]] = {}
    for rank, h in enumerate(kw, start=1):
        # 弱命中只有在向量那一路也找到時才算（兩路都說有關才比較可能真的有關）
        if not h["strong"] and h["id"] not in vec_ids:
            continue
        scores[h["id"]] = scores.get(h["id"], 0.0) + 1.0 / (RRF_K + rank)
        via.setdefault(h["id"], []).append("keyword")
    for rank, h in enumerate(vec, start=1):
        scores[h["id"]] = scores.get(h["id"], 0.0) + 1.0 / (RRF_K + rank)
        via.setdefault(h["id"], []).append("vector")
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])[:k]
    if not ranked:
        return out
    c = store.conn()
    rows = c.execute("SELECT * FROM kb_chunks WHERE id IN (SELECT value FROM json_each(?))",
                     (json.dumps([cid for cid, _ in ranked]),)).fetchall()
    by_id = {r["id"]: store.chunk_row(r) for r in rows}
    for i, (cid, sc) in enumerate(ranked, start=1):
        ch = by_id.get(cid)
        if not ch or ch["version_id"] not in allowed:
            continue
        d = result_dict(ch, allowed[ch["version_id"]])
        d.update(rank=i, score=round(sc, 6), matched_by=via.get(cid, []), mode=out["mode"])
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
    """這個人看得到、而且**有啟用中文件**的資料集。"""
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
