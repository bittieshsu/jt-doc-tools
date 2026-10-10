"""公文知識庫的向量檢索在大量資料下撐得住（2026-10-09 使用者問「全部匯入做向量索引會不會有
容量或效能問題」，量到三個問題、使用者說「修」）。

量的規模：現行法律＋命令全部匯入 161,781 段、4096 維 —— 矩陣 2.65 GB、整份乘一次 0.08 秒，
而原本一次查詢要 7～9 秒、常駐 5.3 GB、匯入期間每查一次就整份重載 18 秒。

這裡用小矩陣驗**形狀**（判準都是相對於矩陣大小，不寫死秒數 —— 計時的測試在忙碌的機器上是假警報）：

* 查詢**不複製子矩陣**：查詢期間新配的記憶體遠小於矩陣本身。
* 載入**不佔兩倍**：峰值不到矩陣的 1.5 倍；**停用的版本不載**。
* 要重載時**先放掉舊的**才載新的（`_build_entry` 被叫的那一刻舊矩陣已經沒有人拿著）。
* 大量寫入期間沿用手上的矩陣，做完或超過 `BULK_RELOAD_EVERY` 才重載；
  **沿用舊矩陣也不會放出看不到的東西**（停用的版本照樣查不到）。
* 同時好幾個查詢進來只載一次；向量下限跟著矩陣算一次就記住。
* 匯入、重建、政府資料匯入三處都包在 `bulk_update()` 裡。
"""
from __future__ import annotations

import gc
import hashlib
import threading
import time
import tracemalloc
import weakref

import numpy as np
import pytest

from app.core.kb import retrieval, store
from tests._kb_support import kb_isolated  # noqa: F401

DIM = 1024


def _unit(rng, n: int, dim: int = DIM) -> np.ndarray:
    m = rng.standard_normal((n, dim)).astype(np.float32)
    m /= np.linalg.norm(m, axis=1, keepdims=True)
    return m


def _version(ds_id: str, n: int, rng, *, fp: str = "fp-test", status: str = "active",
             tag: str = "") -> tuple[str, list[str], np.ndarray]:
    data = f"測試內容 {tag} {rng.random()}".encode("utf-8")
    v = store.create_version(ds_id, title="測試 " + tag, filename="x.txt", ext=".txt", data=data,
                             sha256=hashlib.sha256(data).hexdigest(), meta={})
    ids = store.replace_chunks(v["id"], [{"text": f"第 {i} 段 {tag}"} for i in range(n)],
                               chunker_version="t", page_count=1, chars=n)
    mat = _unit(rng, n)
    store.put_vectors(fp, [(cid, mat[i].tobytes()) for i, cid in enumerate(ids)], DIM)
    store.set_status(v["id"], status)
    return v["id"], ids, mat


@pytest.fixture
def kb(kb_isolated):
    retrieval._VEC_CACHE.clear()
    ds = store.create_dataset({"name": "規模測試", "category": "business_law"})
    yield ds
    retrieval._VEC_CACHE.clear()


def _allowed(*vids: str) -> dict[str, dict]:
    return {v: {} for v in vids}


# ---------------------------------------------------------------- 結果正確
def test_hits_match_a_brute_force_over_the_allowed_versions(kb):
    rng = np.random.default_rng(1)
    va, ia, ma = _version(kb["id"], 300, rng, tag="a")
    vb, ib, mb = _version(kb["id"], 200, rng, tag="b")
    vc, ic, mc = _version(kb["id"], 100, rng, tag="c")
    q = _unit(rng, 1)[0]
    # 只看得到 a 與 c；b 的段落就算分數最高也不可以出現
    mb[0] = q
    store.put_vectors("fp-test", [(ib[0], mb[0].tobytes())], DIM)
    got = retrieval.vector_hits(q, "fp-test", _allowed(va, vc), limit=20)
    ids = ia + ic
    sims = np.vstack([ma, mc]) @ q
    order = np.argsort(-sims)[:20]
    assert [h["id"] for h in got] == [ids[i] for i in order]
    assert [round(h["cos"], 5) for h in got] == [round(float(sims[i]), 5) for i in order]
    assert ib[0] not in {h["id"] for h in got}


def test_the_floor_cuts_the_list_and_limit_is_honoured(kb):
    rng = np.random.default_rng(2)
    va, ia, ma = _version(kb["id"], 50, rng)
    q = ma[7]
    got = retrieval.vector_hits(q, "fp-test", _allowed(va), limit=5, floor=0.99)
    assert [h["id"] for h in got] == [ia[7]]
    assert len(retrieval.vector_hits(q, "fp-test", _allowed(va), limit=5)) == 5
    assert len(retrieval.vector_hits(q, "fp-test", _allowed(va), limit=500)) == 50


def test_nothing_allowed_or_nothing_indexed_returns_empty(kb):
    rng = np.random.default_rng(3)
    va, _, ma = _version(kb["id"], 10, rng)
    assert retrieval.vector_hits(ma[0], "fp-test", {}) == []
    assert retrieval.vector_hits(ma[0], "fp-test", _allowed("0" * 32)) == []
    assert retrieval.vector_hits(ma[0], "other-fp", _allowed(va)) == []


# ---------------------------------------------------------------- 只載啟用中的
def test_only_active_versions_are_loaded(kb):
    rng = np.random.default_rng(4)
    va, ia, _ = _version(kb["id"], 40, rng, tag="on")
    _version(kb["id"], 30, rng, status="inactive", tag="off")
    _version(kb["id"], 20, rng, status="ready", tag="ready")
    ids, vids, mat = retrieval._load_vectors("fp-test", DIM)
    assert sorted(ids) == sorted(ia)
    assert set(vids) == {va}
    assert mat.shape == (40, DIM)


# ---------------------------------------------------------------- 記憶體
def _peak(fn) -> int:
    gc.collect()
    tracemalloc.start()
    try:
        tracemalloc.reset_peak()
        base = tracemalloc.get_traced_memory()[0]
        fn()
        return tracemalloc.get_traced_memory()[1] - base
    finally:
        tracemalloc.stop()


def test_a_query_does_not_copy_the_matrix(kb):
    rng = np.random.default_rng(5)
    va, _, ma = _version(kb["id"], 3000, rng, tag="a")
    vb, _, _ = _version(kb["id"], 10, rng, tag="b")
    mat = retrieval._load_vectors("fp-test", DIM)[2]
    q = ma[0]
    retrieval.vector_hits(q, "fp-test", _allowed(va))         # 先載好
    peak = _peak(lambda: retrieval.vector_hits(q, "fp-test", _allowed(va)))
    assert peak < mat.nbytes / 8, (peak, mat.nbytes)


def test_loading_does_not_hold_two_copies(kb):
    rng = np.random.default_rng(6)
    _version(kb["id"], 3000, rng)
    retrieval._VEC_CACHE.clear()
    box = {}
    peak = _peak(lambda: box.setdefault("e", retrieval._entry("fp-test", DIM)))
    nbytes = box["e"]["mat"].nbytes
    assert box["e"]["mat"].shape == (3000, DIM)
    assert peak < nbytes * 1.5, (peak, nbytes)


def test_a_reload_lets_go_of_the_old_matrix_first(kb, monkeypatch):
    rng = np.random.default_rng(7)
    _version(kb["id"], 200, rng, tag="a")
    ref = weakref.ref(retrieval._entry("fp-test", DIM)["mat"])
    assert ref() is not None
    _version(kb["id"], 10, rng, tag="b")                      # 世代計數變了 → 要重載
    real = retrieval._build_entry
    seen = {}

    def build(*a, **k):
        gc.collect()
        seen["old_alive"] = ref() is not None
        return real(*a, **k)

    monkeypatch.setattr(retrieval, "_build_entry", build)
    e = retrieval._entry("fp-test", DIM)
    assert e["mat"].shape[0] == 210
    assert seen == {"old_alive": False}


# ---------------------------------------------------------------- 大量寫入期間
def _count_builds(monkeypatch) -> list:
    calls: list = []
    real = retrieval._build_entry

    def build(*a, **k):
        calls.append(a)
        return real(*a, **k)

    monkeypatch.setattr(retrieval, "_build_entry", build)
    return calls


def test_writes_reload_the_matrix_outside_a_bulk_job(kb, monkeypatch):
    rng = np.random.default_rng(8)
    va, _, ma = _version(kb["id"], 20, rng, tag="a")
    calls = _count_builds(monkeypatch)
    retrieval.vector_hits(ma[0], "fp-test", _allowed(va))
    vb, ib, mb = _version(kb["id"], 5, rng, tag="b")
    got = retrieval.vector_hits(mb[0], "fp-test", _allowed(va, vb), limit=1)
    assert got[0]["id"] == ib[0]
    assert len(calls) == 2


def test_a_bulk_job_keeps_the_matrix_until_it_ends(kb, monkeypatch):
    rng = np.random.default_rng(9)
    va, _, ma = _version(kb["id"], 20, rng, tag="a")
    calls = _count_builds(monkeypatch)
    retrieval.vector_hits(ma[0], "fp-test", _allowed(va))
    with retrieval.bulk_update():
        for i in range(5):
            vb, ib, mb = _version(kb["id"], 5, rng, tag=f"b{i}")
            got = retrieval.vector_hits(mb[0], "fp-test", _allowed(va, vb), limit=3)
            # 新加的那份還不在手上的矩陣裡（先只有關鍵字找得到），舊的照常查得到
            assert ib[0] not in {h["id"] for h in got}
            assert got
        assert len(calls) == 1
    got = retrieval.vector_hits(mb[0], "fp-test", _allowed(va, vb), limit=1)
    assert got[0]["id"] == ib[0]
    assert len(calls) == 2


def test_a_long_bulk_job_still_reloads_every_so_often(kb, monkeypatch):
    rng = np.random.default_rng(10)
    va, _, ma = _version(kb["id"], 20, rng, tag="a")
    calls = _count_builds(monkeypatch)
    now = [1000.0]
    monkeypatch.setattr(retrieval.time, "monotonic", lambda: now[0])
    retrieval.vector_hits(ma[0], "fp-test", _allowed(va))
    with retrieval.bulk_update():
        vb, ib, mb = _version(kb["id"], 5, rng, tag="b")
        now[0] += retrieval.BULK_RELOAD_EVERY - 1
        retrieval.vector_hits(mb[0], "fp-test", _allowed(va, vb))
        assert len(calls) == 1
        now[0] += 2
        got = retrieval.vector_hits(mb[0], "fp-test", _allowed(va, vb), limit=1)
        assert got[0]["id"] == ib[0]
        assert len(calls) == 2


def test_a_stale_matrix_never_returns_a_deactivated_version(kb):
    rng = np.random.default_rng(11)
    va, ia, ma = _version(kb["id"], 20, rng, tag="a")
    vb, ib, mb = _version(kb["id"], 20, rng, tag="b")
    retrieval.vector_hits(ma[0], "fp-test", _allowed(va, vb))
    with retrieval.bulk_update():
        store.set_status(vb, "inactive")
        allowed = retrieval._allowed_versions(None, None)
        assert vb not in allowed and va in allowed
        got = retrieval.vector_hits(mb[0], "fp-test", allowed, limit=40)
        assert got and not ({h["id"] for h in got} & set(ib))


def test_bulk_update_nests_and_unwinds_on_errors():
    assert retrieval._BULK == 0
    with pytest.raises(RuntimeError):
        with retrieval.bulk_update():
            with retrieval.bulk_update():
                assert retrieval._BULK == 2
                raise RuntimeError("x")
    assert retrieval._BULK == 0


# ---------------------------------------------------------------- 並行與下限
def test_concurrent_queries_load_once(kb, monkeypatch):
    rng = np.random.default_rng(12)
    va, _, ma = _version(kb["id"], 100, rng)
    real = retrieval._build_entry
    calls = []

    def slow_build(*a, **k):
        calls.append(1)
        time.sleep(0.3)
        return real(*a, **k)

    monkeypatch.setattr(retrieval, "_build_entry", slow_build)
    errors = []

    def go():
        try:
            assert retrieval.vector_hits(ma[0], "fp-test", _allowed(va))
        except Exception as e:      # noqa: BLE001
            errors.append(e)

    ts = [threading.Thread(target=go) for _ in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(30)
    assert not errors
    assert len(calls) == 1


class _Cli:
    pass


def test_the_floor_is_computed_once_per_loaded_matrix(kb, monkeypatch):
    rng = np.random.default_rng(13)
    va, _, ma = _version(kb["id"], 50, rng, tag="a")
    nulls = _unit(rng, 4)
    calls = []

    def fake_null(cli, fp):
        calls.append(fp)
        return nulls

    monkeypatch.setattr(retrieval, "null_vectors", fake_null)
    f1 = retrieval.vector_floor(_Cli(), "fp-test", DIM)
    f2 = retrieval.vector_floor(_Cli(), "fp-test", DIM)
    assert f1 == f2 == pytest.approx(float((ma @ nulls.T).max()) + retrieval.NULL_MARGIN)
    assert len(calls) == 1
    _version(kb["id"], 5, rng, tag="b")                      # 重載之後要重算
    retrieval.vector_floor(_Cli(), "fp-test", DIM)
    assert len(calls) == 2


# ---------------------------------------------------------------- 三處都包住
def test_import_rebuild_and_gov_sync_run_inside_bulk_update(kb_isolated, monkeypatch):
    from app.core.kb import gov, indexer
    seen: dict[str, int] = {}

    monkeypatch.setattr(indexer, "_rebuild_locked",
                        lambda job, snapshot=None: seen.setdefault("rebuild", retrieval._BULK) or {})
    indexer.rebuild()

    monkeypatch.setattr(gov, "_sync",
                        lambda gid, actor, job: seen.setdefault("gov", retrieval._BULK) or {})
    gov.sync("moj-law")

    from app.core import job_manager as jm
    captured = {}

    class _Job:
        id = "j1"
        cancelled = False
        progress = 0.0
        message = ""

    def fake_submit(tool_id, fn, **k):
        captured["fn"] = fn
        return _Job()

    monkeypatch.setattr(jm.job_manager, "submit", fake_submit)
    monkeypatch.setattr(store, "set_job_id", lambda *a, **k: None)
    monkeypatch.setattr(indexer, "process_version",
                        lambda vid, job=None: (seen.setdefault("import", retrieval._BULK), {"ok": True})[1])
    indexer.submit_import(["a" * 32])
    captured["fn"](_Job())

    assert seen == {"rebuild": 1, "gov": 1, "import": 1}
    assert retrieval._BULK == 0
