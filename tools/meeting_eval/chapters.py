"""章節 / 心智圖 / 選圖 —— 拿真的模型跑一次，把結果印出來用眼睛看。

**沒有標準答案的東西不要假裝有分數。** 章節切得好不好是主觀的，
所以這支只印出來讓人判斷，並**機械驗證幾條硬性質**：
連續、不重疊、涵蓋全場、每個心智圖節點都指得回逐字稿。
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from app.core import meeting_insight as M
from app.core.llm_client import LLMClient


def score_chapters(chapters: list[dict], blocks: list[dict],
                   segments: list[dict]) -> dict:
    """章節切得好不好 —— **兩個方向都要看**。

    * **碎裂度**＝章節數 ÷ 議題數。真實會議連續談同一個主題好幾分鐘，
      切成一堆兩段長的小章節，時間軸就沒有用了。
    * **純度**＝每個章節裡最多有多少比例落在同一個議題。
      章節橫跨兩個議題的話，點進去看到的是別的事。

    **只看碎裂度會被「整場一章」騙過去**（碎裂度 0.25 很漂亮，純度很差）；
    只看純度會被「每段一章」騙過去（純度 100%，完全沒用）。
    """
    if not chapters or not blocks:
        return {"fragmentation": 0.0, "purity": 0.0, "chapters": len(chapters)}
    topic_of = {}
    for b in blocks:
        for q in range(b["start_seq"], b["end_seq"] + 1):
            topic_of[q] = b["topic"]
    purities = []
    for c in chapters:
        counts: dict[str, int] = {}
        n = 0
        for q in range(c["start_seq"], c["end_seq"] + 1):
            tp = topic_of.get(q)
            if tp:
                counts[tp] = counts.get(tp, 0) + 1
                n += 1
        if n:
            purities.append(max(counts.values()) / n)
    return {"fragmentation": len(chapters) / len(blocks),
            "purity": sum(purities) / len(purities) if purities else 0.0,
            "chapters": len(chapters), "blocks": len(blocks)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", default="long")
    ap.add_argument("--model", default="gemma4:26b")
    ap.add_argument("--base-url",
                    default=os.environ.get("JTDT_EVAL_LLM",
                                           "http://localhost:11434/v1"))
    a = ap.parse_args()

    data = json.loads(Path(f"temp/meeting-eval/{a.corpus}.json")
                      .read_text(encoding="utf-8"))
    segs = data["segments"]
    client = LLMClient(a.base_url, timeout=600)
    ask = lambda p: client.text_query(p, model=a.model, temperature=0.0, think=False)

    chaps = M.build_chapters(segs, ask)
    total_ms = segs[-1]["end_ms"] - segs[0]["start_ms"]
    print(f"{a.corpus}：{len(segs)} 段 / {total_ms/60000:.1f} 分鐘 "
          f"→ {len(chaps)} 個章節\n")
    for c in chaps:
        print(f"  {c['start_ms']/60000:5.1f}–{c['end_ms']/60000:5.1f} 分"
              f"（{c['percentage']:4.1f}%，段 {c['start_seq']}–{c['end_seq']}）"
              f"  {c['title']}")

    # 硬性質（這幾條不是主觀的）
    assert chaps, "至少要有一個章節"
    assert chaps[0]["start_seq"] == segs[0]["seq"]
    assert chaps[-1]["end_seq"] == segs[-1]["seq"]
    for x, y in zip(chaps, chaps[1:]):
        assert y["start_seq"] == x["end_seq"] + 1, "章節必須連續且不重疊"
    assert abs(sum(c["percentage"] for c in chaps) - 100) < 1.5
    print("\n  ✅ 連續、不重疊、涵蓋全場，佔比加總 100%")

    truth_items = {k: [{"text": i["gist"], "segment_ids": i["support"]}
                       for i in v] for k, v in data["truth"].items()
                   if k in M.KINDS}
    nodes = M.build_mindmap(chaps, truth_items)
    assert all(n["segment_ids"] for n in nodes), "每個節點都要指得回逐字稿"
    print(f"  ✅ 心智圖 {len(nodes)} 個節點，全部可追溯")
    print(f"  適合的圖：{M.suitable_charts(chaps, truth_items, M.speaker_stats(segs))}")

    sc = score_chapters(chaps, data["truth"].get("blocks") or [], segs)
    print(f"\n  議題 {sc.get('blocks')} 個 → 章節 {sc['chapters']} 個"
          f"　碎裂度 {sc['fragmentation']:.1f}（1.0 最好）"
          f"　純度 {sc['purity']:.0%}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
