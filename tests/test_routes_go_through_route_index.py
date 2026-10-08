"""列路由一律走 `tools.route_index.iter_routes`，不可以直接讀 `app.routes`。

新版 FastAPI / Starlette 把 `include_router()` 掛進來的路由包成 `_IncludedRouter`
（沒有 `.path`），`app.routes` 不再攤平。直接讀的話有兩種壞法，都只在 CI（pip 照
`requirements.txt` 裝到範圍內最新版）才看得到，開發機（`uv.lock` 鎖舊版）永遠是綠的：

* 比對「端點清單」的檢查變紅（2026-10-08 `test_kb_access` 看到的路由是空的）；
* 涵蓋類的檢查**安靜地少驗**大部分端點（`test_api_doc_coverage` 原本就是這樣，紅不起來）。

2026-09-06 為同一件事寫了 `route_index`，之後新寫的測試又直接讀 `app.routes` ——
有工具沒有檢查，等於沒有。
"""
from __future__ import annotations

import re
from pathlib import Path

from tools.source_text import strip_py_comments

ROOT = Path(__file__).resolve().parent.parent
_DIRECT = re.compile(r"\bapp\.routes\b|\bapp\.router\.routes\b")
#: 寫 helper 的那一支本身、以及這一支（說明裡提到那個寫法）
_ALLOWED = {"tools/route_index.py", "tests/test_routes_go_through_route_index.py"}


def _files():
    for d in ("tests", "tools", "scripts"):
        yield from sorted((ROOT / d).rglob("*.py"))


def test_nobody_reads_app_routes_directly():
    bad, scanned = [], 0
    for p in _files():
        rel = p.relative_to(ROOT).as_posix()
        if rel in _ALLOWED or "__pycache__" in rel:
            continue
        scanned += 1
        code = strip_py_comments(p.read_text(encoding="utf-8"))
        for i, line in enumerate(code.split("\n"), 1):
            if _DIRECT.search(line):
                bad.append(f"{rel}:{i}: {line.strip()[:100]}")
    assert scanned > 300, f"只掃到 {scanned} 支 —— 範圍不對"
    assert not bad, ("直接讀 app.routes（新版 FastAPI 下看不到 include 進來的路由），"
                     "改用 tools.route_index.iter_routes：\n  " + "\n  ".join(bad))


def test_the_scan_has_teeth():
    assert _DIRECT.search("for r in m.app.routes:")
    assert _DIRECT.search("{r.path for r in app_main.app.routes}")
    assert not _DIRECT.search("for r in iter_routes(m.app):")
