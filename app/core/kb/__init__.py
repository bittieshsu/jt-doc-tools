"""知識庫（RAG）—— 給工具用的介面。

第一個使用者是「公文撰擬」，但這裡**不知道公文撰擬的存在**：任何工具都可以拿
`search()` 的結果當依據。管理在 `/admin/knowledge`（`app/admin/knowledge_routes.py`）。

```python
from app.core import kb
kb.search(query, *, user_id, dataset_ids=None, k=8) -> list[dict]
kb.search_detail(query, *, user_id, dataset_ids=None, k=8) -> dict   # 多了 mode / note
kb.get_chunk(chunk_id, *, user_id) -> dict | None
kb.list_datasets(*, user_id) -> list[dict]
kb.status(*, user_id=<不給＝管理員視角>) -> dict
kb.open_original(version_id, *, user_id) -> dict | None   # 給呼叫端做「下載原檔」
```

**全部是同步函式**（會讀 SQLite、可能呼叫嵌入服務）—— 在 async 端點裡要
`await asyncio.to_thread(kb.search, …)`，不可以直接呼叫（會卡住整個網站）。

**權限**：每一個入口都重新判斷 `user_id` 看得到哪些資料集（`access.py`）；
看不到與不存在回一樣的結果。`user_id` 是伺服器端認出的使用者（認證關閉時 None），
**不可以從請求參數取**。

**分數**：`score` 只代表檢索排序，不是正確率（`SCORE_NOTE`）。

**政府公開資料**（全國法規資料庫、國發會行政規則、行政院釋例）由 `kb/gov.py` 匯入成一般的
資料集與版本（`/admin/knowledge/gov`）；檢索結果的 `attribution` / `notice` 是那些版本的出處
與施行日期說明（其他文件是空字串）。`kb.gov.abolished_laws()` 列出已下載清單裡已廢止的法規。

檢索的模組叫 `retrieval` 不叫 `search` —— 這裡把 `search()` 函式匯出來之後，
`from app.core.kb import search` 拿到的是函式不是模組（同名遮蔽；評估工具第一次
就撞到 `'function' object has no attribute`）。
"""
from __future__ import annotations

from typing import Optional

from .retrieval import (SCORE_NOTE, get_chunk, list_datasets, open_original,  # noqa: F401
                     search, search_detail)

_UNSET = object()


def status(*, user_id: "Optional[int] | object" = _UNSET) -> dict:
    """知識庫目前的狀態（給畫面顯示「知識庫目前狀態」）。

    `user_id` 不給 ＝ 管理員視角（每個資料集都列出來）；給了就只列那個人看得到的
    —— **給一般使用者看的畫面一定要傳 `user_id`**，不然會把他看不到的資料集名稱
    也列出來（名稱本身就可能是機密，例如「○○案調查資料」）。
    """
    from . import embed, indexer, store
    pub = embed.get_public()
    act = pub["active"]
    if user_id is _UNSET:
        rows = store.list_datasets_admin()
        datasets = [{"id": d["id"], "name": d["name"], "category": d["category"],
                     "category_label": d["category_label"], "purpose": d["purpose"],
                     "purpose_label": d["purpose_label"], "enabled": d["enabled"],
                     "active_docs": d["doc_counts"].get("active", 0),
                     "active_chunks": d["active_chunks"]} for d in rows]
    else:
        datasets = list_datasets(user_id=user_id)  # type: ignore[arg-type]
    vec_missing = 0
    if act and act.get("fingerprint"):
        vec_missing = store.conn().execute(
            "SELECT COUNT(*) FROM kb_chunks k JOIN kb_versions v ON v.id=k.version_id "
            "WHERE v.status='active' AND NOT EXISTS (SELECT 1 FROM kb_vectors x "
            "WHERE x.chunk_id=k.id AND x.fingerprint=?)", (act["fingerprint"],)).fetchone()[0]
    rb = indexer.rebuild_state()
    return {
        "datasets": datasets,
        "active_chunks": sum(d.get("active_chunks", 0) for d in datasets),
        "embedding": {
            "configured": pub["configured"],
            "vector_ready": bool(act and act.get("fingerprint")),
            "model": (act or {}).get("model", ""),
            "dim": (act or {}).get("dim", 0),
            "fingerprint": (act or {}).get("fingerprint", ""),
            "built_at": (act or {}).get("built_at"),
            "needs_rebuild": pub["needs_rebuild"],
            "vectors_missing": vec_missing,
        },
        "rebuild": {k: rb.get(k) for k in ("running", "done", "total", "error",
                                            "started_at", "finished_at")},
        "mode": "hybrid" if act and act.get("fingerprint") else "keyword",
        "score_note": SCORE_NOTE,
    }
