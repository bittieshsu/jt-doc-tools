#!/usr/bin/env python3
"""用真的瀏覽器把每一頁切成英文，掃出**畫面上還是中文**的字。

為什麼不用靜態掃描：模板裡沒包 `tr()` 的字串靜態掃得到，但**畫面上的中文**還有
另外三種來源 —— ①JS 動態產生的節點 ②`title` / `placeholder` / `aria-label`
這些屬性 ③伺服器回傳的 JSON 被塞進 DOM。這三種靜態掃描一律看不到，而使用者
看得一清二楚。

**不該翻的不可以翻**：品牌名、語言選項本身（「繁體中文」）、字型的中文名稱、
統編資料庫裡的公司名 —— 這些出現在畫面上是**正確的**，掃描要排除，否則清單
會被雜訊淹掉、真正沒翻的反而被忽略。

用法：
    python tools/i18n_untranslated_scan.py --base http://127.0.0.1:8799
輸出：`temp/i18n-scan/<run>/report.json` + 螢幕上的摘要。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

CJK = re.compile("[㐀-鿿]")

# 出現在英文介面上仍然正確的中文（不是漏翻）。
ALLOW_SUBSTR = (
    "Jason Tools 文件工具箱",   # 品牌名
    "繁體中文",                 # 語言切換選項本身
    "文件工具箱",
)
# 這些整串都是資料不是介面（字型名、範例統編資料）
ALLOW_EXACT = {"繁", "中", "字 Ag 1", "Ag 1"}
#: 這些位置顯示的是**領域資料**不是介面文字 —— 翻掉會讓功能安靜失效
#: （同義詞是拿去比對客戶表單的中文欄位標籤；統編查詢列的是公司名）。
#: 不排除的話清單會被幾百條資料淹掉，真正沒翻的反而看不到。
ALLOW_BY_PAGE = {
    "/admin/synonyms": ("text:textarea", "attr:placeholder"),
    "/admin/vat-db": ("text:td",),
    "/tools/vat-lookup/": ("text:td",),
}

JS = """
(() => {
  const out = [];
  const seen = new Set();
  const CJKRE = /[\\u3400-\\u9fff]/;
  const push = (t, where) => {
    t = (t || '').replace(/\\s+/g, ' ').trim();
    if (!t || !CJKRE.test(t)) return;
    const k = where + ' ' + t;
    if (seen.has(k)) return;
    seen.add(k);
    out.push({text: t, where: where});
  };
  const visible = (el) => {
    if (!el) return false;
    const s = getComputedStyle(el);
    return !(s.display === 'none' || s.visibility === 'hidden');
  };
  const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  let n;
  while ((n = w.nextNode())) {
    const p = n.parentElement;
    if (!p) continue;
    const tag = p.tagName;
    if (tag === 'SCRIPT' || tag === 'STYLE' || tag === 'NOSCRIPT') continue;
    if (!visible(p)) continue;
    // data-i18n="skip"：那一整區顯示的是領域資料（比對用的中文關鍵字），
    // 出現中文是正確的，翻掉才會壞。
    if (p.closest('[data-i18n="skip"]')) continue;
    push(n.nodeValue, 'text:' + tag.toLowerCase());
  }
  const attrs = ['title', 'placeholder', 'aria-label', 'alt', 'data-tip'];
  for (const el of document.querySelectorAll('[title],[placeholder],[aria-label],[alt],[data-tip]')) {
    for (const a of attrs) {
      const v = el.getAttribute(a);
      if (v) push(v, 'attr:' + a);
    }
  }
  for (const el of document.querySelectorAll('option')) push(el.textContent, 'option');
  return JSON.stringify(out);
})()
"""


def _pages(base: str) -> list[str]:
    from app.main import app
    from app.tool_registry import discover_tools
    admin = sorted({r.path for r in app.routes
                    if getattr(r, "path", "").startswith("/admin")
                    and "GET" in getattr(r, "methods", set())
                    and "{" not in getattr(r, "path", "")})
    tools = [f"/tools/{t.metadata.id}/"
             for t in sorted(discover_tools(), key=lambda t: t.metadata.id)]
    return ["/", "/my-jobs", "/workspace"] + tools + admin


def _is_html(base: str, path: str) -> bool:
    import urllib.request
    try:
        with urllib.request.urlopen(base + path, timeout=10) as r:
            return r.headers.get("content-type", "").startswith("text/html")
    except Exception:
        return False


def _keep(t: str) -> bool:
    if t in ALLOW_EXACT:
        return False
    for a in ALLOW_SUBSTR:
        t = t.replace(a, "")
    return bool(CJK.search(t))


async def _scan(base: str, cdp_port: int, paths: list[str]) -> dict:
    import httpx
    import websockets

    proc = subprocess.Popen(
        ["/usr/bin/chromium-browser", "--headless", "--no-sandbox", "--disable-gpu",
         f"--remote-debugging-port={cdp_port}", "--remote-allow-origins=*",
         "--window-size=1440,1000", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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
            report: dict = {}
            for path in paths:
                await cmd("Page.navigate", {"url": base + path})
                await asyncio.sleep(1.7)
                r = await cmd("Runtime.evaluate",
                              {"expression": JS, "returnByValue": True})
                try:
                    items = json.loads(r["result"]["value"])
                except Exception:
                    items = []
                skip = ALLOW_BY_PAGE.get(path, ())
                hits = [i for i in items
                        if _keep(i["text"]) and i["where"] not in skip]
                if hits:
                    report[path] = hits
            return report
    finally:
        proc.terminate()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8799")
    ap.add_argument("--cdp-port", type=int, default=9412)
    args = ap.parse_args()

    paths = [p for p in _pages(args.base) if _is_html(args.base, p)]
    print(f"掃 {len(paths)} 頁…")
    report = asyncio.run(_scan(args.base, args.cdp_port, paths))
    out = REPO / "temp" / "i18n-scan" / time.strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    total = sum(len(v) for v in report.values())
    print(f"{len(report)} 頁還有中文，共 {total} 條 -> {out/'report.json'}")
    for path, hits in sorted(report.items(), key=lambda kv: -len(kv[1]))[:15]:
        print(f"  {len(hits):4}  {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
