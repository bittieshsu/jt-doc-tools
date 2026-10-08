#!/usr/bin/env python3
"""公文撰擬的評估：拿 `cases.json` 的案例跑真的模型，量「有沒有照寫、有沒有編」。

**先有判準才調提示**（同會議摘要那次的做法）。四個指標：

* 照寫率 —— 案例指定要照寫的金額、數量、日期、字詞，草稿裡都在的比例
* 編造數 —— 草稿裡出現 `must_not`（不可以出現的寫法）的次數。**這是最重要的一個**：
  漏寫的後果是少一點東西；編造的後果是承辦人照著一件沒發生的事送出去
* 待補 —— 應該標成〔待補〕／〔待確認〕的項目，有沒有標
* 檢查器的旗標 —— `check_draft` 抓了哪些（拿來對照：該抓的抓到沒、有沒有誤報）

用法（位址與模型**一律從參數或環境變數給**，這支會同步到公開樹，不寫內部位址）：

    .venv/bin/python tools/official_doc_eval/run_eval.py \\
        --base-url http://<推論機>:11434/v1 --model gemma4:26b --runs 2

結果寫到 `temp/official_doc_eval/<時間>-<模型>.json`（`temp/` 不公開）。
**合成案例只能證明「我們自己出的考卷」** —— 拿到真實案例要補進 cases.json 重跑。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.core import official_doc as od          # noqa: E402
from app.core.llm_client import LLMClient       # noqa: E402

HERE = Path(__file__).resolve().parent


def _run_case(case: dict, ask) -> dict:
    inp = case["inputs"]
    t0 = time.time()
    try:
        if case["mode"] == "sign":
            d = od.run_sign(inp["narrative"], ask, unit=inp.get("unit", ""),
                            length=inp.get("length", "normal"))
        elif case["mode"] == "letter":
            d = od.run_letter(inp["narrative"], ask, org=inp.get("org", ""),
                              receiver=inp.get("receiver", ""), relation=inp.get("relation", "unknown"),
                              closing=inp.get("closing", ""), attachments=inp.get("attachments", ""),
                              length=inp.get("length", "normal"))
        else:
            d = od.run_endorse(inp["source"], inp["direction"], ask,
                               outline=bool(inp.get("outline")), fmt=inp.get("fmt", "compact"),
                               length=inp.get("length", "normal"), units=inp.get("units", ""),
                               internal_deadline=inp.get("internal_deadline", ""))
    except Exception as e:                       # noqa: BLE001
        return {"id": case["id"], "error": f"{type(e).__name__}: {e}", "secs": round(time.time() - t0, 1)}
    return _score(case, d, time.time() - t0)


def _score(case: dict, d: od.Draft, secs: float) -> dict:
    body = od.strip_frame(d.text)
    qty = {(q.value, q.unit) for q in od.find_quantities(body)}
    dates = od.find_dates(body)
    kept, missed = [], []
    for v, unit in case.get("must_keep_qty", []):
        ok = (Decimal(v), unit) in qty
        (kept if ok else missed).append(f"{v}{unit}")
    for y, m, dd in case.get("must_keep_dates", []):
        ok = any(x.month == m and x.day == dd and (y is None or x.year in (None, y)) for x in dates)
        (kept if ok else missed).append(f"{y or ''}/{m}/{dd}")
    norm = od._norm_cmp(body)
    for t in case.get("must_keep_text", []):
        (kept if od._norm_cmp(t) in norm else missed).append(t)
    fabricated = [p for p in case.get("must_not", []) if re.search(p, body)]
    ph = od.PLACEHOLDER_RE.findall(body)
    # 待補可以寫在草稿裡（〔待補：…〕），也可以由程式的完整性檢查列出來 —— 兩種都算有提醒
    notes = [f"{a}{b}" for a, b in ph] + [i.message for i in d.issues if i.severity == "todo"]
    ph_missing = [k for k in case.get("expect_placeholder", [])
                  if not any(re.search(k, n) if k else True for n in notes)]
    conflict_picked = None
    if case.get("conflict_values"):
        present = [v for v in case["conflict_values"] if v in body]
        conflict_picked = len(present) == 1          # 只寫了其中一個 ＝ 自己挑了
    return {
        "id": case["id"], "secs": round(secs, 1), "calls": d.calls,
        "kept": kept, "missed": missed, "fabricated": fabricated,
        "placeholders": [f"{a}：{b}" for a, b in ph], "placeholder_missing": ph_missing,
        "conflict_picked": conflict_picked,
        "flags": [f"{i.severity}:{i.code}:{i.snippet}" for i in d.issues],
        "facts": [(f["label"], f["status"], f.get("value")) for f in d.facts],
        "text": d.text,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default=os.environ.get("JTDT_EVAL_LLM_URL", ""))
    ap.add_argument("--api-key", default=os.environ.get("JTDT_EVAL_LLM_KEY", ""))
    ap.add_argument("--model", default=os.environ.get("JTDT_EVAL_MODEL", "gemma4:26b"))
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--only", default="", help="只跑 id 含這段字的案例")
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    if not a.base_url:
        ap.error("要給 --base-url（或環境變數 JTDT_EVAL_LLM_URL）")
    cases = json.loads((HERE / "cases.json").read_text(encoding="utf-8"))["cases"]
    if a.only:
        cases = [c for c in cases if a.only in c["id"]]
    client = LLMClient(base_url=a.base_url, api_key=a.api_key or None, timeout=600)

    # 跟工具裡用的一樣：打轉就提早停、格式不對而重問時換一點溫度（`app/tools/official_doc/router.py` 的 `ask`）
    def ask(prompt: str) -> str:
        return client.text_query(prompt, model=a.model, think=False,
                                 max_tokens=od.MAX_OUTPUT_TOKENS, stop_when=od.is_runaway)

    def retry(prompt: str) -> str:
        return client.text_query(prompt, model=a.model, think=False, max_tokens=od.MAX_OUTPUT_TOKENS,
                                 temperature=od.RETRY_TEMPERATURE, stop_when=od.is_runaway)

    ask.retry = retry

    results = []
    for r in range(a.runs):
        for c in cases:
            res = _run_case(c, ask)
            res["run"] = r + 1
            results.append(res)
            flag = "ERR" if "error" in res else (
                "FAB" if res["fabricated"] else ("MISS" if res["missed"] else "ok"))
            print(f"[{r + 1}] {flag:4} {res['id']:<28} {res.get('secs')}s "
                  f"missed={res.get('missed')} fab={res.get('fabricated')} "
                  f"ph_missing={res.get('placeholder_missing')} picked={res.get('conflict_picked')}",
                  flush=True)
    ok = [x for x in results if "error" not in x]
    keep_total = sum(len(x["kept"]) + len(x["missed"]) for x in ok)
    keep_ok = sum(len(x["kept"]) for x in ok)
    summary = {
        "model": a.model, "runs": a.runs, "cases": len(cases), "errors": len(results) - len(ok),
        "keep_rate": round(keep_ok / keep_total, 3) if keep_total else None,
        "fabrications": sum(len(x["fabricated"]) for x in ok),
        "cases_with_fabrication": sum(1 for x in ok if x["fabricated"]),
        "placeholder_missing": sum(len(x["placeholder_missing"]) for x in ok),
        "conflict_picked": sum(1 for x in ok if x.get("conflict_picked")),
        "median_secs": sorted(x["secs"] for x in results)[len(results) // 2] if results else None,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    out = Path(a.out) if a.out else (ROOT / "temp" / "official_doc_eval" /
                                     f"{time.strftime('%Y%m%d-%H%M%S')}-{re.sub(r'[^A-Za-z0-9._-]', '_', a.model)}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"summary": summary, "results": results}, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    print("報告：", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
