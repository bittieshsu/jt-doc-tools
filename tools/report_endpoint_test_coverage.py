#!/usr/bin/env python3
"""哪些端點目前沒有任何自動化測試碰過 —— **提示，不是判決**。

`tests/test_test_plan_coverage.py` 守的是「測試計畫有沒有寫到這支端點」，
那是字面比對，精確。**這支不一樣**：它用字串比對去猜「測試碼裡有沒有打過
這條路徑」，會漏（測試用 f-string 組路徑）也會誤報（尾段是常見字）。

所以它**不是守門測試**，只是排優先序用的清單 —— 想知道「先補哪支的自動化
測試比較划算」時跑它。判涵蓋率不可以靠啟發式（v1.14.63 的教訓：寬一點是
永遠綠的假測試，嚴一點把驗得更嚴的工具誤報成缺口）。

    python tools/report_endpoint_test_coverage.py [--all]
"""
from __future__ import annotations

import os
import pathlib
import re
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
os.environ.setdefault("JTDT_DATA_DIR", tempfile.mkdtemp(prefix="jtdt-report-"))
sys.path.insert(0, str(ROOT))

SKIP = ("/static", "/admin", "/openapi", "/docs", "/redoc", "/assets")


def main() -> int:
    import app.main as app_main
    from app.tool_registry import discover_tools

    files = {p.name: p.read_text(encoding="utf-8", errors="ignore")
             for p in (ROOT / "tests").glob("*.py")}
    joined = "\n".join(files.values())
    names = {t.metadata.id: t.metadata.name for t in discover_tools()}

    def touched(path: str) -> bool:
        stem = path.split("{")[0].rstrip("/")
        if stem and stem in joined:
            return True
        parts = path.split("/")
        if len(parts) > 3 and parts[1] == "tools":
            tid, tail = parts[2], stem.split("/")[-1]
            if tail and tail != tid:
                pat = re.compile(r'["/]' + re.escape(tail) + r'["/\s]')
                return any(tid in txt and pat.search(txt) for txt in files.values())
        return False

    rows = []
    for r in app_main.app.routes:
        p = getattr(r, "path", "")
        if not p or "/api/" in p or "{rest:path}" in p or p.startswith(SKIP):
            continue
        m = "/".join(sorted((getattr(r, "methods", None) or set()) - {"HEAD", "OPTIONS"}))
        rows.append((p, m, touched(p)))
    rows = sorted(set(rows))

    show_all = "--all" in sys.argv
    miss = [r for r in rows if not r[2]]
    print(f"非 API 端點 {len(rows)} 支｜測試碼裡找得到路徑的 {len(rows) - len(miss)} 支"
          f"｜找不到的 {len(miss)} 支\n")
    for p, m, ok in (rows if show_all else miss):
        tid = p.split("/")[2] if p.startswith("/tools/") and len(p.split("/")) > 2 else ""
        mark = "✓" if ok else " "
        print(f"  {mark} {m:12} {p}" + (f"   [{names.get(tid, '')}]" if tid in names else ""))
    print("\n（找不到不等於沒測到：測試用 f-string 組路徑時這裡看不到。"
          "反過來也一樣，尾段是常見字時會誤判成有測。）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
