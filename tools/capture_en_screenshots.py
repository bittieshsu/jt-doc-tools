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

#: 要**先上傳一份檔案**才有東西可看的頁面。空的上傳頁當產品截圖沒有意義
#: （2026-09-05 使用者回報：中文版的截圖都是「工具實際在用」的畫面）。
#: 檔案一律用合成的示範 PDF —— `temp_pdfs/` 裡是客戶資料，截圖要公開。
NEEDS_FILE = {
    "pdf-editor": "#peFile, input[type=file]",
    "pdf-ocr": "input[type=file]",
    "pdf-to-image": "input[type=file]",
    "pdf-to-office": "input[type=file]",
    "stamp": "input[type=file]",
    "watermark": "input[type=file]",
    "deident-1": "input[type=file]",
}
SAMPLE = REPO / "temp" / "en-shots" / "sample-quotation.pdf"


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
            await cmd("Runtime.enable")
            await cmd("Network.enable")
            host = base.split("//", 1)[-1].split(":")[0].split("/")[0]
            await cmd("Network.setCookie", {"name": "jtdt_locale", "value": "en",
                                            "domain": host, "path": "/"})
            await cmd("Emulation.setDeviceMetricsOverride",
                      {"width": WIDTH, "height": HEIGHT,
                       "deviceScaleFactor": 1, "mobile": False})
            OUT.mkdir(parents=True, exist_ok=True)
            await cmd("DOM.enable")
            for name, path in SHOTS.items():
                await cmd("Page.navigate", {"url": base + path})
                await asyncio.sleep(2.0)
                if name in NEEDS_FILE and SAMPLE.is_file():
                    # 用 CDP 直接把檔案塞進 <input type=file> —— headless 沒有
                    # 檔案選擇器，只能這樣做。塞完要自己發 change 事件，
                    # 頁面才會開始處理（少了它畫面完全不動，看起來像沒作用）。
                    try:
                        doc = await cmd("DOM.getDocument", {"depth": -1})
                        node = await cmd("DOM.querySelector", {
                            "nodeId": doc["root"]["nodeId"],
                            "selector": "input[type=file]"})
                        if node.get("nodeId"):
                            await cmd("DOM.setFileInputFiles", {
                                "files": [str(SAMPLE)], "nodeId": node["nodeId"]})
                            await cmd("Runtime.evaluate", {"expression":
                                "document.querySelector('input[type=file]')"
                                ".dispatchEvent(new Event('change',{bubbles:true}))"})
                            await asyncio.sleep(4.0)
                            # 大部分工具**還要按一下主要按鈕**才會真的開始 ——
                            # 只塞檔案的話畫面停在「已選好檔案」，跟空的上傳頁
                            # 一樣沒有內容可看。
                            await cmd("Runtime.evaluate", {"expression": '''
                              (() => {
                                const vis = (e) => e && e.offsetParent !== null
                                  && !e.disabled;
                                const b = [...document.querySelectorAll(
                                  '.btn-primary, button[type=submit]')].find(vis);
                                if (b) { b.click(); return true; }
                                return false;
                              })()'''})
                            await asyncio.sleep(12.0)  # 等預覽 / 轉檔跑完
                    except Exception:
                        pass
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
