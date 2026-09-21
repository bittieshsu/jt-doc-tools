"""校正的錯誤有多少會傳到決議卡上？

**為什麼要量**：我們的摘要 / 決議 / 待辦疊在 `final`（校正後的逐字稿）上。
語音服務量出來 `standard` 每 100 行約有 13 行（中文）的字詞被改成別的意思，
**而且讀起來完全通順**。那些錯誤會不會變成一張引用看起來完全合法的決議卡？
**我們自己的引用驗證抓不到**（文字確實在那個段落裡，只是意思變了）。

## ⚠ 第一版的量法是錯的，留著當教訓

第一版是「同一份腳本跑兩次（`truth` 一次、`standard` 一次），比兩邊的項目」。
**對照組（兩邊都用 `truth`）跑出 66.7% 的「差異」—— 比實驗組還高。**
因為那量到的是**模型自己每次抽出來的講法不一樣**：

    更新 Proxmox VE 的叢集憑證   vs   Proxmox VE 的叢集憑證更新
    記憶體只有 6 GB              vs   記憶體只有六 GB

那是換句話說，不是語意改變。**沒有對照組的話我會把 60% 當成校正的傷害報出去。**

## 現在的量法：確定性的，而且只要跑一次模型

1. 只在 `standard`（客戶實際會拿到的那一層）上抽一次項目。
2. 每一條項目都有 `segment_ids` —— 去看**那幾段**的 `truth` 與 `standard`。
3. 用編輯距離判斷那一段被校正**改壞了**還是**改了但沒改對**
   （跟語音服務同一套分類，數字才對得起來）。
4. 只要有一段中標，這條項目就是**建立在被改壞的文字上**。

沒有模型的隨機性，也不需要跑兩次。
"""
from __future__ import annotations

import argparse
import collections
import difflib
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from app.core import meeting_insight as mi  # noqa: E402
from app.core.llm_client import LLMClient  # noqa: E402

#: 語料不在公開樹裡（`docs-share/` 是內部往來文件，不會同步出去），
#: 所以這支工具在 clone 下來的樹上跑不動 —— 它是開發機用的量測工具。
#:
#: **用 glob 找，不要寫死路徑** —— 目錄名帶著合作對象的名字，
#: 而 `tools/` 會同步進公開樹。
_FOUND = sorted((ROOT / "docs-share").glob("*/*/correction-corpus"))
CORPUS_DIR = _FOUND[0] if _FOUND else ROOT / "docs-share" / "correction-corpus"
#: 語音服務換校正模型之後，同一批語料每個模型各有一份 ——
#: **他們給的任何數字都要連同當時用的模型一起看**（對方 2026-09-18 的提醒，
#: 而我們自己量摘要模型時也得到同一個結論）。
CORPUS = CORPUS_DIR / "corpus.json"

#: 標點、空白、大小寫的差異不算「動到內容」（跟語音服務的判定一致）。
_PUNCT = re.compile(r"[\s　,.;:!?、，。；：！？「」『』（）()\"'…—\-~·]+")


def _bare(s: str) -> str:
    return _PUNCT.sub("", (s or "")).lower()


def _dist(a: str, b: str) -> float:
    """0 = 完全一樣，越大越遠。"""
    return 1.0 - difflib.SequenceMatcher(None, a, b).ratio()


def classify(row: dict, field: str) -> str:
    """這一行被校正動成什麼樣？跟語音服務同一套分類。"""
    truth, asr, out = _bare(row["truth"]), _bare(row["asr"]), _bare(row[field])
    if out == asr:
        return "沒動到內容"
    d_asr, d_out = _dist(asr, truth), _dist(out, truth)
    if d_out < d_asr - 1e-9:
        return "改對了"
    if d_out > d_asr + 1e-9:
        return "改壞了"
    return "改了但沒改對"


#: 這兩類對下游的後果一樣：決議卡上那句話的意思不是會議上講的。
DAMAGED = ("改壞了", "改了但沒改對")


def meetings(rows: list[dict], lang: str, cond: str) -> dict:
    out: dict[tuple, list[dict]] = collections.defaultdict(list)
    for r in rows:
        if r["lang"] == lang and r["cond"] == cond:
            out[(r["set"], r["voice"])].append(r)
    return {k: sorted(v, key=lambda r: r["line"]) for k, v in out.items()}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lang", default="zh")
    ap.add_argument("--cond", default="noisy")
    ap.add_argument("--model", default="gemma4:26b")
    ap.add_argument("--base-url",
                    default=os.environ.get("JTDT_EVAL_LLM",
                                          "http://localhost:11434/v1"))
    ap.add_argument("--field", default="standard",
                    help="客戶實際拿到的那一層（standard / punctuation_only）")
    ap.add_argument("--groups", type=int, default=0)
    ap.add_argument("--corpus", default="corpus.json",
                    help="語料檔（每個校正模型一份）")
    ap.add_argument("--out", default="")
    a = ap.parse_args()

    rows = json.loads((CORPUS_DIR / a.corpus).read_text(encoding="utf-8"))
    ms = meetings(rows, a.lang, a.cond)
    keys = sorted(ms)[: a.groups or None]

    client = LLMClient(base_url=a.base_url, api_key=None, timeout=900)
    calls = {"n": 0}

    def ask(prompt: str) -> str:
        calls["n"] += 1
        return client.text_query(prompt, model=a.model, think=False)

    line_kinds = collections.Counter()
    totals = collections.Counter()
    hits = []
    for k in keys:
        rws = ms[k]
        segs = [{"seq": i, "text": (r[a.field] or "").strip()}
                for i, r in enumerate(rws, 1) if (r[a.field] or "").strip()]
        by_seq = {i: r for i, r in enumerate(rws, 1)}
        for r in rws:
            line_kinds[classify(r, a.field)] += 1

        print(f"[{'/'.join(k)}] {len(segs)} 行 …", flush=True)
        out = mi.full_analysis(segs, ask).to_public()
        n = 0
        for kind, items in out["items"].items():
            for it in items:
                n += 1
                totals["items"] += 1
                bad = [(q, classify(by_seq[q], a.field))
                       for q in (it.get("segment_ids") or []) if q in by_seq]
                bad = [(q, c) for q, c in bad if c in DAMAGED]
                if bad:
                    totals["damaged"] += 1
                    hits.append({"group": list(k), "kind": kind,
                                 "text": it.get("text"), "segments": bad})
                    print(f"   ✗ [{kind}] {it.get('text')}", flush=True)
                    for q, c in bad:
                        print(f"       第 {q} 段（{c}）", flush=True)
                        print(f"         會議上講的：{by_seq[q]['truth']}", flush=True)
                        print(f"         我們看到的：{by_seq[q][a.field]}", flush=True)
        print(f"   抽出 {n} 條，其中 {len([h for h in hits if h['group'] == list(k)])} "
              f"條建立在被改壞的文字上", flush=True)

    print("\n" + "=" * 62)
    print(f"語言 {a.lang}｜條件 {a.cond}｜逐字稿層 {a.field}｜"
          f"語料 {a.corpus}｜摘要模型 {a.model}")
    print(f"{len(keys)} 場會議｜模型請求 {calls['n']} 次")
    tot_lines = sum(line_kinds.values()) or 1
    print("\n逐字稿本身（跟語音服務的分類一致）：")
    for kk in ("沒動到內容", "改對了", "改壞了", "改了但沒改對"):
        v = line_kinds.get(kk, 0)
        print(f"  {kk:<12} {v:>4}  ({v/tot_lines*100:.1f}%)")
    ti = totals["items"] or 1
    print(f"\n抽出的項目 {totals['items']} 條")
    print(f"  **建立在被改壞的文字上：{totals['damaged']} 條"
          f"（{totals['damaged']/ti*100:.1f}%）**")
    print("\n注意：這裡量的是「項目引用的段落被改壞了」，**不是**「項目本身的意思錯了」")
    print("      —— 被改壞的那一段不一定正好是決定語意的那一句。這個數字是**上限**。")
    if a.out:
        Path(a.out).write_text(json.dumps(
            {"lang": a.lang, "cond": a.cond, "field": a.field, "model": a.model,
             "lines": dict(line_kinds), "totals": dict(totals), "hits": hits},
            ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\n明細 → {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
