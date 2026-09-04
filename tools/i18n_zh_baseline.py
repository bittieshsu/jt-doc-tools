#!/usr/bin/env python3
"""i18n 的安全網：**繁體中文的畫面必須一個位元組都不變**。

使用者定的最高宗旨是「不能為了加 i18n 改壞現有功能」。把字串包成
`{{ tr('…') }}` 這種改動一次會動到幾百行樣板，肉眼看不完，而且壞掉的樣子
往往是**畫面看起來正常、只是少了一個空白或多了一層跳脫** —— 逐像素比對
既慢又抓不到這種差異。

所以判準是**渲染出來的 HTML 位元組完全相同**：比逐像素嚴格，而且快。

    python tools/i18n_zh_baseline.py --save     # 改之前
    ...改樣板...
    python tools/i18n_zh_baseline.py --compare  # 改之後（要 0 份不同）

存檔位置 `temp/i18n-baseline/`（不上 git）。
"""
from __future__ import annotations

import os
import pathlib
import re
import shutil
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "temp" / "i18n-baseline"
os.environ.setdefault("JTDT_CSRF_DISABLE", "1")
os.environ.setdefault("JTDT_DATA_DIR", tempfile.mkdtemp(prefix="jtdt-i18n-base-"))
sys.path.insert(0, str(ROOT))


# 每次請求都會變的東西（CSRF token、CSP nonce、作業 id 之類）先正規化掉，
# 否則整份比對永遠是紅的 —— 那樣這條安全網等於沒有。
_VOLATILE = [
    (re.compile(rb'(name="csrf-token" content=")[^"]*'), rb"\1<TOKEN>"),
    (re.compile(rb'(name="csrf_token" value=")[^"]*'), rb"\1<TOKEN>"),
    (re.compile(rb'(nonce=")[^"]*'), rb"\1<NONCE>"),
    # 版本號：bump 之後每一頁都會不同，會把真正的差異淹掉
    (re.compile(rb"v\d+\.\d+\.\d+"), rb"vX.Y.Z"),
]


def _normalise(body: bytes) -> bytes:
    for pat, rep in _VOLATILE:
        body = pat.sub(rep, body)
    return body


def _pages() -> list[str]:
    from app.tool_registry import discover_tools
    pages = ["/", "/my-jobs", "/workspace", "/login", "/search?q=pdf"]
    pages += [f"/tools/{t.metadata.id}/" for t in discover_tools()]
    return pages


def _render() -> dict[str, bytes]:
    from fastapi.testclient import TestClient
    import app.main as app_main
    out = {}
    with TestClient(app_main.app) as c:
        # 語言明確設成繁體中文（預設就是，但寫出來比較不會誤判）
        c.cookies.set("jtdt_locale", "zh-Hant")
        for p in _pages():
            r = c.get(p, follow_redirects=True)
            name = p.strip("/").replace("/", "_").replace("?", "_") or "home"
            out[f"{name}.html"] = _normalise(r.content)
    return out


def main() -> int:
    save = "--save" in sys.argv
    if not save and "--compare" not in sys.argv:
        print(__doc__)
        return 2
    pages = _render()
    if save:
        if OUT.exists():
            shutil.rmtree(OUT)
        OUT.mkdir(parents=True)
        for name, body in pages.items():
            (OUT / name).write_bytes(body)
        print(f"已存基準：{len(pages)} 頁 → {OUT}")
        return 0

    if not OUT.exists():
        print("還沒存基準，先跑 --save", file=sys.stderr)
        return 2
    diff = []
    for name, body in pages.items():
        old = OUT / name
        if not old.exists():
            diff.append(f"{name}（基準裡沒有這頁）")
        elif old.read_bytes() != body:
            a, b = old.read_bytes(), body
            i = next((i for i in range(min(len(a), len(b))) if a[i] != b[i]), min(len(a), len(b)))
            diff.append(f"{name}（第 {i} 位元組起不同）\n"
                        f"      舊：{a[max(0, i-60):i+60]!r}\n"
                        f"      新：{b[max(0, i-60):i+60]!r}")
    if diff:
        print(f"✗ 繁體中文的畫面變了 —— {len(diff)} 頁不同：")
        for d in diff:
            print("   ", d)
        return 1
    print(f"✓ 繁體中文完全沒變（{len(pages)} 頁位元組相同）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
