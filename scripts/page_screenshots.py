#!/usr/bin/env python3
"""全站頁面截圖 + 接觸表（發版前**一定要逐張目視**）。

為什麼要有這一支：既有的 `page_visual_check.py` 斷言的是「可見控制項有沒有
消失」，`test_template_block_placement.py` 之類的守門看的是原始碼形狀 ——
**版面長歪了它們全部照樣綠燈**。v1.14.60 有一張卡片攤成整個視窗寬、左邊壓到
側欄底下，所有自動檢查都過，是使用者截圖回報才發現的。凡是「畫面看起來不對」
這一類，只有真的用眼睛看才抓得到。

涵蓋範圍**包含管理頁**（既有的視覺回歸一張管理頁都沒有）。

用法：
    # 1) 起一個 auth-off 的拋棄式實例（見 TEST_PLAN.md §1.8）
    #    JTDT_DATA_DIR=$(mktemp -d) JTDT_CSRF_DISABLE=1 uvicorn app.main:app --port 8799
    # 2) 抓圖 + 產生接觸表
    python scripts/page_screenshots.py --base http://127.0.0.1:8799
    # 3) **逐張看過** temp/shots/<run>/sheet-*.png（每張九格，附頁面路徑）

輸出：`temp/shots/<YYYYmmdd-HHMMSS>/`（單頁 PNG + `sheet-NN.png` + `index.json`）。
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MAX_PAGE_PX = 4000        # 超長的頁面截到這裡為止，接觸表才看得清楚
SHEET_COLS, SHEET_ROWS = 3, 3
CELL_W, CELL_H = 460, 340


# 管理區的 GET 路由裡混著一堆**不是頁面**的東西（JSON API、CSV 匯出、部署腳本）。
# 第一次跑的時候有 20 格拼出來是純文字的 JSON —— 對「用眼睛看版面」完全是雜訊，
# 會讓人放棄逐張看。判準**不要用路徑猜**（`/admin/users` 是頁面、
# `/admin/directory/user` 是 API，字串上分不開），直接問伺服器回的是不是 HTML。
def _is_html(url: str) -> bool:
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.headers.get("content-type", "").startswith("text/html")
    except Exception:
        return False


def _pages(base: str) -> list[tuple[str, str]]:
    """工具頁 + 一般頁 + **全部管理頁**（從實際路由表列舉，不寫死）。"""
    sys.path.insert(0, str(REPO))
    from app.tool_registry import discover_tools
    routes = json.load(urllib.request.urlopen(base + "/openapi.json"))["paths"]
    admin = sorted(p for p in routes
                   if p.startswith("/admin/") and "{" not in p and "get" in routes[p]
                   and _is_html(base + p))
    tools = [f"/tools/{t.metadata.id}/" for t in sorted(discover_tools(), key=lambda t: t.metadata.id)]
    extra = ["/", "/my-jobs", "/workspace"]
    return ([("tool", p) for p in tools]
            + [("page", p) for p in extra]
            + [("admin", p) for p in admin])


async def _capture(base: str, cdp_port: int, outdir: Path,
                   locale: str = "") -> list[dict]:
    import httpx
    import websockets

    proc = subprocess.Popen(
        ["/usr/bin/chromium-browser", "--headless", "--no-sandbox", "--disable-gpu",
         f"--remote-debugging-port={cdp_port}", "--remote-allow-origins=*",
         "--hide-scrollbars", "--window-size=1440,1000", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        ws_url = None
        for _ in range(40):
            await asyncio.sleep(0.5)
            try:
                tabs = [t for t in httpx.get(f"http://127.0.0.1:{cdp_port}/json/list", timeout=2).json()
                        if t.get("type") == "page"]
                if tabs:
                    ws_url = tabs[0]["webSocketDebuggerUrl"]
                    break
            except Exception:
                pass
        if not ws_url:
            raise SystemExit("連不上 CDP（chromium 沒起來？）")

        async with websockets.connect(ws_url, max_size=80 * 1024 * 1024) as ws:
            mid = 0

            async def cmd(method, params=None):
                nonlocal mid
                mid += 1
                await ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
                while True:
                    msg = json.loads(await ws.recv())
                    if msg.get("id") == mid:
                        if "error" in msg:
                            raise RuntimeError(msg["error"])
                        return msg.get("result", {})

            await cmd("Page.enable")
            await cmd("Runtime.enable")
            if locale:
                # 英文介面要另外看一輪：英文比中文寬約 1.7 倍，按鈕 / 下拉 /
                # 表格標題最容易被擠爆，而**自動化測試一律抓不到版面問題**。
                await cmd("Network.enable")
                host = base.split("//", 1)[-1].split(":")[0].split("/")[0]
                await cmd("Network.setCookie",
                          {"name": "jtdt_locale", "value": locale,
                           "domain": host, "path": "/"})
            index: list[dict] = []
            for kind, path in _pages(base):
                await cmd("Page.navigate", {"url": base + path})
                await asyncio.sleep(1.6)
                metrics = await cmd("Page.getLayoutMetrics")
                height = min(int(metrics["cssContentSize"]["height"]), MAX_PAGE_PX)
                await cmd("Emulation.setDeviceMetricsOverride",
                          {"width": 1440, "height": height, "deviceScaleFactor": 1, "mobile": False})
                await asyncio.sleep(0.4)
                shot = await cmd("Page.captureScreenshot", {"captureBeyondViewport": True})
                name = (path.strip("/").replace("/", "_") or "home") + ".png"
                (outdir / name).write_bytes(base64.b64decode(shot["data"]))
                await cmd("Emulation.clearDeviceMetricsOverride")
                index.append({"kind": kind, "path": path, "file": name, "height": height})
            return index
    finally:
        proc.terminate()


def _sheets(outdir: Path, index: list[dict]) -> int:
    """把單頁圖拼成每張九格的接觸表，格子上方印路徑。"""
    from PIL import Image, ImageDraw

    try:
        from PIL import ImageFont
        font = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 13)
    except Exception:
        font = None

    per = SHEET_COLS * SHEET_ROWS
    made = 0
    for start in range(0, len(index), per):
        chunk = index[start:start + per]
        sheet = Image.new("RGB", (SHEET_COLS * CELL_W, SHEET_ROWS * (CELL_H + 22)), "#0f172a")
        draw = ImageDraw.Draw(sheet)
        for i, item in enumerate(chunk):
            col, row = i % SHEET_COLS, i // SHEET_COLS
            x, y = col * CELL_W, row * (CELL_H + 22)
            draw.text((x + 6, y + 4), item["path"], fill="#e2e8f0", font=font)
            with Image.open(outdir / item["file"]) as im:
                im = im.convert("RGB")
                im.thumbnail((CELL_W - 10, CELL_H - 6))
                sheet.paste(im, (x + 5, y + 22))
        sheet.save(outdir / f"sheet-{made + 1:02d}.png")
        made += 1
    return made


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8799")
    ap.add_argument("--cdp-port", type=int, default=9401)
    ap.add_argument("--locale", default="",
                    help="設 jtdt_locale cookie（例 en）再抓圖；空白 = 站台預設（繁中）")
    args = ap.parse_args()

    outdir = REPO / "temp" / "shots" / time.strftime("%Y%m%d-%H%M%S")
    outdir.mkdir(parents=True, exist_ok=True)
    index = asyncio.run(_capture(args.base, args.cdp_port, outdir, args.locale))
    (outdir / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=1))
    n = _sheets(outdir, index)
    kinds = {}
    for it in index:
        kinds[it["kind"]] = kinds.get(it["kind"], 0) + 1
    print(f"{len(index)} 頁（{kinds}）→ {outdir}")
    print(f"接觸表 {n} 張：{outdir}/sheet-*.png —— **請逐張看過**")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
