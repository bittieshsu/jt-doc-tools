#!/usr/bin/env python3
"""重建 TEST_PLAN.md 的「全部自動化測試一覽」。

**為什麼要自動產生**：212 支測試檔裡，測試計畫原本只提到 96 支 —— 另外
116 支等於沒有出現在發版門檻的視野裡。人工維護那張表一定會漂（這個專案
已經吃過很多次虧），所以一句話說明**直接取每支測試檔自己的 module
docstring 第一行** —— 說明跟程式在同一個檔案裡，改了不會對不上。

    python tools/build_test_plan_index.py        # 就地更新 TEST_PLAN.md
    python tools/build_test_plan_index.py --check  # 只檢查是否過期（CI 用）
"""
from __future__ import annotations

import ast
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
PLAN = ROOT / "TEST_PLAN.md"
BEGIN = "<!-- BEGIN test-index (由 tools/build_test_plan_index.py 產生，不要手改) -->"
END = "<!-- END test-index -->"


def _rows() -> list[tuple[str, str]]:
    out = []
    for p in sorted((ROOT / "tests").glob("test_*.py")):
        src = p.read_text(encoding="utf-8")
        doc = ast.get_docstring(ast.parse(src)) or ""
        first = doc.strip().splitlines()[0].strip() if doc.strip() else "（沒有說明）"
        first = first.rstrip("。").replace("|", "｜")
        out.append((p.name, first))
    return out


def _block() -> str:
    rows = _rows()
    lines = [
        BEGIN,
        "",
        f"共 **{len(rows)} 支測試檔**。說明取自每支檔案自己的開頭說明，",
        "跑 `python tools/build_test_plan_index.py` 重建。",
        "",
        "> 這裡**刻意不列函式數** —— 那個數字每加一條測試就會變，",
        "> 會讓「一覽表過期」的守門在每次寫測試時都紅一次（純噪音）。",
        "> 要看實際跑了幾項看 pytest 的結尾摘要；README 的徽章另有守門。",
        "",
        "| 測試檔 | 守的是什麼 |",
        "|---|---|",
    ]
    lines += [f"| `{n}` | {d} |" for n, d in rows]
    lines += ["", END]
    return "\n".join(lines)


def main() -> int:
    text = PLAN.read_text(encoding="utf-8")
    block = _block()
    if BEGIN in text and END in text:
        new = re.sub(re.escape(BEGIN) + r".*?" + re.escape(END), lambda _: block,
                     text, flags=re.S)
    else:
        print("找不到標記區塊，請先在 TEST_PLAN.md 放好 BEGIN/END", file=sys.stderr)
        return 2
    if "--check" in sys.argv:
        if new != text:
            print("測試一覽已過期，請跑 python tools/build_test_plan_index.py")
            return 1
        print("測試一覽是最新的")
        return 0
    PLAN.write_text(new, encoding="utf-8")
    print(f"已更新：{len(_rows())} 支測試檔")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
