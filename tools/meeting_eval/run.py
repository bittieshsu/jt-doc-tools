"""拿**真的模型**跑一次會議分析，然後計分。

> **假模型驗不了「模型會不會照做」。** 本專案在翻譯對照字典上踩過：
> 佔位符 `⟪1⟫` 所有測試都綠（假模型當然會原樣保留），拿 gemma4:26b
> 實跑五句全部失敗。凡是「模型要照著做某件事」的設計，一定要拿真的模型測 ——
> 而「每一條都要附段號」正是這種設計。

用法：

    python -m tools.meeting_eval.run --corpus short --model gemma4:26b
    python -m tools.meeting_eval.run --corpus xlong --window 4000 --json out.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from app.core import meeting_insight as M
from app.core.llm_client import LLMClient
from tools.meeting_eval.score import render, score

CORPUS_DIR = Path("temp/meeting-eval")
DEFAULT_BASE = os.environ.get("JTDT_EVAL_LLM", "http://localhost:11434/v1")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", default="short")
    ap.add_argument("--model", default="gemma4:26b")
    ap.add_argument("--base-url", default=DEFAULT_BASE)
    ap.add_argument("--window", type=int, default=M.DEFAULT_WINDOW_CHARS)
    ap.add_argument("--overlap", type=int, default=M.DEFAULT_OVERLAP_CHARS)
    ap.add_argument("--threshold", type=float, default=M.DEFAULT_CITE_THRESHOLD)
    ap.add_argument("--timeout", type=float, default=600.0)
    ap.add_argument("--no-second-pass", action="store_true")
    ap.add_argument("--json", default="")
    a = ap.parse_args(argv)

    path = CORPUS_DIR / f"{a.corpus}.json"
    if not path.exists():
        print(f"找不到 {path}，先跑 `python -m tools.meeting_eval.corpus`", file=sys.stderr)
        return 2
    data = json.loads(path.read_text(encoding="utf-8"))
    segs = data["segments"]

    client = LLMClient(a.base_url, timeout=a.timeout)
    calls = {"n": 0, "chars_in": 0, "chars_out": 0}
    raws: list[str] = []

    def ask(prompt: str) -> str:
        calls["n"] += 1
        calls["chars_in"] += len(prompt)
        out = client.text_query(prompt, model=a.model, temperature=0.0, think=False)
        calls["chars_out"] += len(out or "")
        raws.append(out or "")
        return out

    def progress(i: int, n: int) -> None:
        print(f"  視窗 {i}/{n}…", end="\r", flush=True)

    t0 = time.time()
    res = M.analyse(segs, ask, window_chars=a.window, overlap_chars=a.overlap,
                    threshold=a.threshold, progress=progress,
                    second_pass=not a.no_second_pass)
    took = time.time() - t0
    print(" " * 30, end="\r")

    print(f"語料 {a.corpus}：{len(segs)} 段 / "
          f"{sum(len(s['text']) for s in segs):,} 字 / "
          f"{segs[-1]['end_ms']/60000:.1f} 分鐘")
    print(f"模型 {a.model}　視窗 {a.window} 字（重疊 {a.overlap}）　"
          f"門檻 {a.threshold}")
    print(f"{res.windows} 個視窗、{calls['n']} 次呼叫、{took:.1f} 秒"
          f"（{took/max(1,calls['n']):.1f} 秒/次）")
    print()
    rep = score(data["truth"], res.items)
    print(render(rep))
    if res.dropped:
        print(f"\n  引用驗不過而丟掉 {len(res.dropped)} 條：")
        for d in res.dropped[:8]:
            print(f"    [{d['kind']}] {str(d['text'])[:42]}… — {d['reason']}")

    if a.json:
        Path(a.json).write_text(json.dumps(
            {"args": vars(a), "took": took, "calls": calls["n"],
             "items": res.items, "dropped": res.dropped,
             "totals": rep.totals, "content_fails": rep.content_fails,
             "raws": raws}, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n明細 → {a.json}")
    return 0 if rep.ok() else 1


if __name__ == "__main__":
    raise SystemExit(main())
