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
# **資料目錄要固定**，不可以每次 mkdtemp —— 管理頁會把資料目錄的路徑印在畫面上
# （匯出目錄、字型目錄…），路徑每次都不一樣的話整份比對永遠是紅的。
_DATA = ROOT / "temp" / "i18n-baseline-data"
_DATA.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("JTDT_DATA_DIR", str(_DATA))
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
    """工具頁 + 一般頁 + **管理頁**。

    管理頁一開始沒收 —— 結果是「管理區改壞了這條安全網看不到」。
    管理頁從路由表列舉（不寫死），只收 GET 且沒有路徑參數的。
    """
    from app.tool_registry import discover_tools
    import app.main as app_main
    pages = ["/", "/my-jobs", "/workspace", "/login", "/search?q=pdf"]
    pages += [f"/tools/{t.metadata.id}/" for t in discover_tools()]
    admin = sorted({r.path for r in app_main.app.routes
                    if getattr(r, "path", "").startswith("/admin")
                    and "{" not in r.path
                    and "GET" in (getattr(r, "methods", None) or set())})
    return pages + admin


def _render() -> dict[str, bytes]:
    from fastapi.testclient import TestClient
    import app.main as app_main
    out = {}
    with TestClient(app_main.app) as c:
        # 語言明確設成繁體中文（預設就是，但寫出來比較不會誤判）
        c.cookies.set("jtdt_locale", "zh-Hant")
        for p in _pages():
            r = c.get(p, follow_redirects=True)
            # **只收 HTML**。管理區的 GET 路由裡混著 JSON API（系統狀態、
            # 作業佇列…），那些回應帶時間戳，每次都不一樣 —— 收進來的話
            # 這條安全網永遠是紅的，等於沒有。
            if not r.headers.get("content-type", "").startswith("text/html"):
                continue
            name = p.strip("/").replace("/", "_").replace("?", "_") or "home"
            out[f"{name}.html"] = _normalise(r.content)
    return out


#: `<script>` 內容可以選擇性排除。包 JS 字串（`tr('…')`）**一定會改到 script
#: 的位元組**，那是預期內的；但「畫面上看得到的東西有沒有變」仍然要能單獨驗，
#: 所以 `--ignore-scripts` 把 script 整段換成一個標記再比 —— 這條在包 JS 那幾
#: 批是唯一還有意義的判準（JS 的行為改用 `temp/i18n-cdp/cdp_i18n_test.py` 驗）。
_SCRIPT = re.compile(rb"<script\b[^>]*>.*?</script>", re.S | re.I)


def main() -> int:
    save = "--save" in sys.argv
    if not save and "--compare" not in sys.argv:
        print(__doc__)
        return 2
    pages = _render()
    if "--ignore-scripts" in sys.argv:
        pages = {k: _SCRIPT.sub(b"<SCRIPT/>", v) for k, v in pages.items()}
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
    ignore = "--ignore-scripts" in sys.argv
    diff = []
    for name, body in pages.items():
        old = OUT / name
        if not old.exists():
            diff.append(f"{name}（基準裡沒有這頁）")
        elif (_SCRIPT.sub(b"<SCRIPT/>", old.read_bytes()) if ignore
              else old.read_bytes()) != body:
            a = (_SCRIPT.sub(b"<SCRIPT/>", old.read_bytes()) if ignore
                 else old.read_bytes())
            b = body
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
