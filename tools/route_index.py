#!/usr/bin/env python3
"""列舉 FastAPI 應用的所有路由 —— **同時支援新舊版 Starlette**。

## 由來

Starlette 1.6 起，`include_router()` 掛進去的路由**不再攤平在 `app.routes`**，
而是包成 `_IncludedRouter` 物件（沒有 `.path`、也沒有 `.routes`，真正的路由在
`original_router.routes`，前綴在 `include_context.prefix`）。

2026-09-06 這件事在 CI 上炸出來：`requirements.txt` 寫的是
`starlette>=1.3.1,<2`，開發機的 uv.lock 鎖在 **1.3.1**，CI 從範圍解析裝到
**1.6.0** → `test_broken_input_no_500` 在收集階段就
`AttributeError: '_IncludedRouter' object has no attribute 'path'`。

**真正危險的不是那個紅燈，是安靜的涵蓋率流失**：實測新版底下，頂層只看得到
**3 條** `/tools/` 路由（舊版是好幾百條）。如果只是「跳過沒有 .path 的物件」，
那些逐路由參數化的守門會收集到寥寥幾條**然後全綠** —— 這個專案已經被
「空轉還全綠」騙過好幾次了。

所以這裡**一律遞迴進去**，而且提供 `assert_sane()` 讓呼叫端確認自己真的
拿到了東西。
"""
from __future__ import annotations

from typing import Any, Iterator


def iter_routes(app: Any) -> Iterator[Any]:
    """走訪應用的所有路由物件（每個都保證有 `.path`）。

    路徑已經接上 include 時的前綴，所以拿到的就是**對外實際的網址**。
    """
    yield from _walk(getattr(app, "routes", []) or [], "")


def _walk(routes, prefix: str) -> Iterator[Any]:
    for r in routes:
        # Starlette 1.6+：include_router 的結果
        orig = getattr(r, "original_router", None)
        if orig is not None:
            ctx = getattr(r, "include_context", None)
            sub_prefix = getattr(ctx, "prefix", "") or ""
            yield from _walk(getattr(orig, "routes", []) or [],
                             prefix + sub_prefix)
            continue
        path = getattr(r, "path", None)
        if path is None:
            continue
        if prefix:
            r = _Prefixed(r, prefix + path)
        yield r


class _Prefixed:
    """把 include 前綴接回路徑，其餘屬性原樣轉發。"""

    __slots__ = ("_r", "path")

    def __init__(self, route, path: str):
        self._r = route
        self.path = path

    def __getattr__(self, name):
        return getattr(self._r, name)

    def __repr__(self):
        return f"<Route {self.path} {sorted(getattr(self._r, 'methods', []) or [])}>"


def route_paths(app: Any) -> set[str]:
    return {r.path for r in iter_routes(app)}


def assert_sane(app: Any, minimum: int = 200) -> None:
    """確認列舉真的拿到東西 —— **空轉跟全部通過長得一模一樣**。

    逐路由參數化的守門一定要呼叫這個，否則哪天列舉方式又跟不上框架，
    測試會安靜地縮成幾條然後照樣綠燈。
    """
    n = len(route_paths(app))
    assert n >= minimum, (
        f"只列舉到 {n} 條路由（預期至少 {minimum}）—— 列舉方式八成跟不上 "
        f"Starlette 的版本了，逐路由的守門正在空轉。")


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
    from app.main import app as _app
    ps = sorted(route_paths(_app))
    print(f"共 {len(ps)} 條路由")
    for p in ps[:20]:
        print(" ", p)
