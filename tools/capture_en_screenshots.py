#!/usr/bin/env python3
"""替介紹站的**英文版**抓一組英文介面的截圖。

英文版頁面原本引用的是中文介面的截圖 —— 讀者看到的介面跟他實際會看到的不一樣，
而這正是「有沒有真的支援英文」最直接的證據。

抓的是一個 auth-off 的拋棄式實例（跟 `scripts/page_screenshots.py` 同一套做法），
把 `jtdt_locale` cookie 設成 en 再截。**視窗高度固定**，不抓整頁 —— 介紹站的
截圖是要放在卡片裡的示意圖，整頁長圖縮下去什麼都看不清楚。

用法：
    # 先起實例：JTDT_DATA_DIR=$(mktemp -d) uvicorn app.main:app --port 8799
    python tools/capture_en_screenshots.py --base http://127.0.0.1:8799
輸出：`github/docs/screenshots/en/<名字>.png`
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import subprocess
from pathlib import Path

import sys as _sys, pathlib as _pathlib
_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent.parent))
from tools.repo_paths import public_root as _public_root

REPO = Path(__file__).resolve().parent.parent
OUT = _public_root(REPO) / "docs" / "screenshots" / "en"

#: 檔名 → 頁面路徑。名字**跟中文版那組對齊**，index-en.html 只要改資料夾就好。
SHOTS: dict[str, str] = {
    "index": "/",
    "fill": "/tools/pdf-fill/",
    "stamp": "/tools/pdf-stamp/",
    "watermark": "/tools/pdf-watermark/",
    "pdf-editor": "/tools/pdf-editor/",
    "pdf-ocr": "/tools/pdf-ocr/",
    "pdf-to-image": "/tools/pdf-to-image/",
    "pdf-to-office": "/tools/pdf-to-office/",
    "einvoice-scan": "/tools/einvoice-scan/",
    "translate-doc": "/tools/translate-doc/",
    "deident-1": "/tools/doc-deident/",
    "deident-2": "/tools/text-deident/",
    "fonts": "/admin/fonts",
    "premissions": "/admin/permissions",
    "users-multi-realm": "/admin/users",
}
WIDTH, HEIGHT = 1400, 1020


async def _capture(base: str, cdp_port: int) -> list[str]:
    import httpx
    import websockets

    proc = subprocess.Popen(
        ["/usr/bin/chromium-browser", "--headless", "--no-sandbox", "--disable-gpu",
         f"--remote-debugging-port={cdp_port}", "--remote-allow-origins=*",
         "--hide-scrollbars", f"--window-size={WIDTH},{HEIGHT}", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    done: list[str] = []
    try:
        ws_url = None
        for _ in range(40):
            await asyncio.sleep(0.5)
            try:
                tabs = [t for t in httpx.get(
                    f"http://127.0.0.1:{cdp_port}/json/list", timeout=2).json()
                    if t.get("type") == "page"]
                if tabs:
                    ws_url = tabs[0]["webSocketDebuggerUrl"]
                    break
            except Exception:
                pass
        if not ws_url:
            raise SystemExit("連不上 CDP")
        async with websockets.connect(ws_url, max_size=80 * 1024 * 1024) as ws:
            mid = 0

            async def cmd(method, params=None):
                nonlocal mid
                mid += 1
                await ws.send(json.dumps({"id": mid, "method": method,
                                          "params": params or {}}))
                while True:
                    msg = json.loads(await ws.recv())
                    if msg.get("id") == mid:
                        if "error" in msg:
                            raise RuntimeError(msg["error"])
                        return msg.get("result", {})

            await cmd("Page.enable")
            await cmd("Network.enable")
            host = base.split("//", 1)[-1].split(":")[0].split("/")[0]
            await cmd("Network.setCookie", {"name": "jtdt_locale", "value": "en",
                                            "domain": host, "path": "/"})
            await cmd("Emulation.setDeviceMetricsOverride",
                      {"width": WIDTH, "height": HEIGHT,
                       "deviceScaleFactor": 1, "mobile": False})
            OUT.mkdir(parents=True, exist_ok=True)
            for name, path in SHOTS.items():
                await cmd("Page.navigate", {"url": base + path})
                await asyncio.sleep(2.0)
                shot = await cmd("Page.captureScreenshot", {})
                (OUT / f"{name}.png").write_bytes(base64.b64decode(shot["data"]))
                done.append(name)
        return done
    finally:
        proc.terminate()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8799")
    ap.add_argument("--cdp-port", type=int, default=9421)
    args = ap.parse_args()
    done = asyncio.run(_capture(args.base, args.cdp_port))
    print(f"抓了 {len(done)} 張 -> {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
