"""把**整條管線**對真的模型跑一次，印出使用者實際會看到的東西。

分開量測每一塊很重要，但**使用者看到的是組合起來的那一頁** ——
摘要讀起來對不對、章節跟項目搭不搭、心智圖有沒有內容，
只有整個跑一次才看得到（同本專案「驗到產出本身」那條）。
"""
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

from app.core import meeting_insight as M
from app.core.llm_client import LLMClient
from tools.meeting_eval.score import render, score


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", default="long")
    ap.add_argument("--model", default="gemma4:26b")
    ap.add_argument("--base-url", default=os.environ.get(
        "JTDT_EVAL_LLM", "http://localhost:11434/v1"))
    ap.add_argument("--json", default="")
    a = ap.parse_args()

    data = json.loads(Path(f"temp/meeting-eval/{a.corpus}.json")
                      .read_text(encoding="utf-8"))
    segs = data["segments"]
    client = LLMClient(a.base_url, timeout=900)
    ask = lambda p: client.text_query(p, model=a.model, temperature=0.0,
                                      think=False)

    t0 = time.time()
    res = M.full_analysis(segs, ask,
                          on_stage=lambda n, i, k: print(f"  {n} {i}/{k}…",
                                                         end="\r", flush=True))
    took = time.time() - t0
    print(" " * 40, end="\r")

    print(f"{a.corpus}：{len(segs)} 段 / {segs[-1]['end_ms']/60000:.1f} 分鐘"
          f"　{res.calls} 次呼叫　{took:.0f} 秒\n")
    print("── 摘要 " + "─" * 50)
    print(f"  {res.summary.get('text') or '（沒有產出）'}")
    g = res.summary.get("grounded")
    print(f"  依據檢查：{'✅ 沒有來源以外的數字／專有名詞' if g else '⚠ 可疑：' + str(res.summary.get('unsupported'))}")

    print("\n── 章節 " + "─" * 50)
    for c in res.chapters:
        print(f"  {c['start_ms']/60000:5.1f}–{c['end_ms']/60000:5.1f} 分"
              f"（{c['percentage']:4.1f}%）  {c['title']}")

    labels = {"decisions": "決議", "actions": "待辦",
              "risks": "風險", "questions": "未決問題"}
    for kind in M.KINDS:
        got = res.items.get(kind) or []
        if not got:
            continue
        print(f"\n── {labels[kind]}（{len(got)}）" + "─" * 44)
        for it in got:
            extra = ""
            if kind == "actions":
                extra = f"　[負責 {it.get('owner') or '未指定'}／" \
                        f"期限 {it.get('due_text') or '未定'}]"
            print(f"  · {it.get('text')}{extra}")
            print(f"      ↳ 段 {it.get('segment_ids')}")

    print(f"\n── 心智圖 {len(res.mindmap)} 節點／適合的圖 {res.charts}")
    print(f"── 丟掉 {len(res.dropped)} 條")

    print("\n── 對照標準答案 " + "─" * 42)
    print(render(score(data["truth"], res.items)))

    if a.json:
        Path(a.json).write_text(json.dumps(res.to_public(), ensure_ascii=False,
                                           indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
