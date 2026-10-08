#!/usr/bin/env python3
"""知識庫檢索的評估：只關鍵字 vs 各 embedding 模型（只向量）vs 混合，量 top-k 命中率。

**先有判準才調門檻**（同會議摘要、公文撰擬的做法）。這支在一個**拋棄式的資料目錄**
裡跑完整條路（匯入 → 切段 → 逐字索引 → 重建向量索引 → 檢索），用的是產品自己的
程式（`app.core.kb`），不是另外寫一份檢索 —— 量到的就是產品的行為。

## 語料

語料**不隨程式散布**（規格 10：取得與散布原文的權利要分別確認）。請自己下載，例如
行政院「文書處理手冊」專區的《文書處理手冊》與《文書處理相關釋例–函釋》PDF，
放在 `temp/kb_eval/corpus/`（`temp/` 不公開）。題目在 `questions.json`，
每一題標了應該命中的章節。

## 用法（位址與模型**一律從參數給**，這支會同步到公開樹，不寫內部位址）

    .venv/bin/python tools/kb_eval/run_eval.py \\
        --corpus temp/kb_eval/corpus/handbook.pdf --corpus temp/kb_eval/corpus/rulings.pdf \\
        --base-url http://<推論機>:11434 --kind ollama \\
        --model qwen3-embedding:8b --model nomic-embed-text:latest

`--model 名稱` 會套用 `app/core/kb/embed.py` 的 `PRESETS` 建議前綴；
寫成 `名稱::raw` 表示不加任何前綴（拿來比較前綴有沒有用）。
結果寫到 `temp/kb_eval/results/<時間>.json` 與同名 `.md`。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent


def _parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--corpus", action="append", required=True, help="語料檔（可重複）")
    ap.add_argument("--questions", default=str(HERE / "questions.json"))
    ap.add_argument("--base-url", default=os.environ.get("KB_EVAL_BASE_URL", ""),
                    help="嵌入服務位址（也可以用環境變數 KB_EVAL_BASE_URL）")
    ap.add_argument("--kind", default="ollama", choices=("ollama", "openai"))
    ap.add_argument("--model", action="append", default=[], help="embedding 模型（可重複）")
    ap.add_argument("--api-key", default=os.environ.get("KB_EVAL_API_KEY", ""))
    ap.add_argument("--k", type=int, default=8)
    ap.add_argument("--batch", type=int, default=16)
    ap.add_argument("--out", default=str(ROOT / "temp" / "kb_eval" / "results"))
    ap.add_argument("--keep-data", action="store_true", help="跑完不刪拋棄式資料目錄")
    return ap.parse_args()


def main() -> int:
    args = _parse_args()
    data_dir = Path(tempfile.mkdtemp(prefix="kb-eval-"))
    # **一定要在 import app 之前設好** —— 設定在 import 當下就讀資料目錄
    os.environ["JTDT_DATA_DIR"] = str(data_dir)
    sys.path.insert(0, str(ROOT))
    try:
        return _run(args, data_dir)
    finally:
        if not args.keep_data:
            shutil.rmtree(data_dir, ignore_errors=True)


def _hit(results: list[dict], expect: list[list[str]], compact) -> int:
    """回第一個命中的名次（1 起算），沒有命中回 0。"""
    for rank, r in enumerate(results, start=1):
        blob = compact((r.get("heading") or "") + "\n" + (r.get("text") or ""))
        for group in expect:
            if all(compact(s) in blob for s in group):
                return rank
    return 0


def _run(args, data_dir: Path) -> int:
    from app.core import cjk_fts
    from app.core.kb import embed, indexer, retrieval, store

    qs = json.loads(Path(args.questions).read_text(encoding="utf-8"))
    questions, irrelevant = qs["questions"], qs.get("irrelevant", [])

    # ---- 匯入語料（關鍵字索引）
    ds = store.create_dataset({"name": "評估語料", "category": "writing_rules"})
    t0 = time.monotonic()
    for f in args.corpus:
        p = Path(f)
        data = p.read_bytes()
        v = store.create_version(ds["id"], title=p.stem, filename=p.name, ext=p.suffix.lower(),
                                 data=data, sha256=hashlib.sha256(data).hexdigest(), meta={})
        res = indexer.process_version(v["id"])
        if not res.get("ok"):
            print(f"✗ 匯入 {p.name} 失敗：{res}")
            return 2
        store.transition(v["id"], allowed_from=("ready",), to="active", activated_by="eval")
    n_chunks = store.conn().execute("SELECT COUNT(*) FROM kb_chunks").fetchone()[0]
    print(f"語料 {len(args.corpus)} 份 → {n_chunks} 段（{time.monotonic() - t0:.1f} 秒）")

    allowed = retrieval._allowed_versions(None, None)
    k = args.k
    report: dict = {"k": k, "chunks": n_chunks, "corpus": [Path(c).name for c in args.corpus],
                    "questions": len(questions), "runs": []}

    def _rows(ids: list[str]) -> list[dict]:
        if not ids:
            return []
        rows = store.conn().execute(
            "SELECT * FROM kb_chunks WHERE id IN (SELECT value FROM json_each(?))",
            (json.dumps(ids),)).fetchall()
        by = {r["id"]: store.chunk_row(r) for r in rows}
        return [by[i] for i in ids if i in by]

    def evaluate(label: str, fn) -> dict:
        per = []
        t = time.monotonic()
        for q in questions:
            res = fn(q["q"])
            rank = _hit(res[:k], q["expect"], cjk_fts.compact)
            per.append({"id": q["id"], "rank": rank, "n": len(res[:k])})
        neg = [len(fn(q)[:k]) for q in irrelevant]
        hits = sum(1 for p in per if p["rank"])
        mrr = sum(1 / p["rank"] for p in per if p["rank"]) / max(1, len(per))
        run = {"label": label, "hits": hits, "total": len(per),
               "hit_rate": round(hits / max(1, len(per)), 3), "mrr": round(mrr, 3),
               "irrelevant_results": neg,
               "avg_results": round(sum(p["n"] for p in per) / max(1, len(per)), 1),
               "query_ms": round((time.monotonic() - t) * 1000 / max(1, len(per) + len(neg)), 1),
               "per_question": per}
        print(f"  {label:<46} 命中 {hits}/{len(per)}  MRR {mrr:.3f}  "
              f"平均回 {run['avg_results']} 段  無關問題回 {neg}")
        return run

    print("== 只用關鍵字")
    report["runs"].append(evaluate(
        # 跟產品沒有向量時一樣：弱命中不算（見 retrieval 的說明）
        "keyword", lambda q: _rows([h["id"] for h in retrieval.keyword_hits(q, allowed)
                                    if h["strong"]][:k])))

    for spec in args.model:
        raw = spec.endswith("::raw")
        model = spec[:-5] if raw else spec
        pre = {"query_prefix": "", "document_prefix": ""} if raw else (
            embed.preset_for(model) or {"query_prefix": "", "document_prefix": ""})
        embed.clear_active()
        embed.save({"kind": args.kind, "base_url": args.base_url, "model": model,
                    "key_input": args.api_key, "batch_size": args.batch, **pre})
        label = model + (" (無前綴)" if raw else "")
        print(f"== {label}")
        t = time.monotonic()
        try:
            res = indexer.rebuild()
        except indexer.RebuildError as e:
            print(f"  ✗ 建索引失敗：{e}")
            report["runs"].append({"label": label, "error": str(e)})
            continue
        build_s = time.monotonic() - t
        per_chunk_ms = build_s * 1000 / max(1, res["chunks"])
        print(f"  建索引 {build_s:.1f} 秒（每段 {per_chunk_ms:.1f} ms，維度 {res['dim']}）")
        snap = embed.active_snapshot()
        cli = embed.EmbedClient.from_snapshot(snap)

        floor = retrieval.vector_floor(cli, snap["fingerprint"], snap["dim"])
        print(f"  向量下限（無關問題校正）：{floor:.3f}")

        def vec_only(q: str) -> list[dict]:
            qv = cli.embed_query(q)
            return _rows([h["id"] for h in retrieval.vector_hits(
                qv, snap["fingerprint"], allowed, limit=k, floor=floor)])

        def hybrid(q: str) -> list[dict]:
            return retrieval.search(q, user_id=None, k=k)

        r1 = evaluate(f"{label} 只向量", vec_only)
        r2 = evaluate(f"{label} 混合", hybrid)
        for r in (r1, r2):
            r.update(model=model, prefixes=pre, dim=res["dim"], vector_floor=round(floor, 4),
                     build_seconds=round(build_s, 1), per_chunk_ms=round(per_chunk_ms, 1))
        report["runs"] += [r1, r2]

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    (out / f"{stamp}.json").write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                       encoding="utf-8")
    lines = [f"# 知識庫檢索評估（{stamp}）", "",
             f"語料：{', '.join(report['corpus'])}，共 {n_chunks} 段；題目 {len(questions)} 題；k = {k}", "",
             "| 方式 | 命中 | MRR | 平均回幾段 | 無關問題回幾段 | 每段嵌入 |",
             "|---|---|---|---|---|---|"]
    for r in report["runs"]:
        if "error" in r:
            lines.append(f"| {r['label']} | 失敗：{r['error']} | | | | |")
            continue
        lines.append(f"| {r['label']} | {r['hits']}/{r['total']} | {r['mrr']} | {r['avg_results']} | "
                     f"{r['irrelevant_results']} | {r.get('per_chunk_ms', '—')} ms |")
    (out / f"{stamp}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"結果：{out / (stamp + '.md')}")
    if args.keep_data:
        print(f"資料目錄留著：{data_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
