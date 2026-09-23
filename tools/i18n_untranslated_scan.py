#!/usr/bin/env python3
"""用真的瀏覽器把每一頁切成某個語言，掃出**畫面上還是中文**的字。

為什麼不用靜態掃描：模板裡沒包 `tr()` 的字串靜態掃得到，但**畫面上的中文**還有
另外三種來源 —— ①JS 動態產生的節點 ②`title` / `placeholder` / `aria-label`
這些屬性 ③伺服器回傳的 JSON 被塞進 DOM。這三種靜態掃描一律看不到，而使用者
看得一清二楚。

**不該翻的不可以翻**：品牌名、語言選項本身（「繁體中文」）、字型的中文名稱、
統編資料庫裡的公司名 —— 這些出現在畫面上是**正確的**，掃描要排除，否則清單
會被雜訊淹掉、真正沒翻的反而被忽略。

用法：
    python tools/i18n_untranslated_scan.py --locale en --base http://127.0.0.1:8799
    python tools/i18n_untranslated_scan.py --locale ja --base http://127.0.0.1:8799
輸出：`temp/i18n-scan/<run>/report.json` + 螢幕上的摘要。

**日文的判準跟英文不一樣**：英文頁上「有漢字」就是沒翻，日文頁上漢字是正常的。
日文改看兩個訊號：
  ① 這串字**剛好是語系檔的鍵** —— 代表我們明明有譯文，畫面上卻顯示中文
     （`tr()` 沒包到、或字典沒載到）。這是**確定的** bug。
  ② 這串字含有現代日文不會用的中文詞（`NOT_JAPANESE`）—— 那是
     「整段沒收進語系檔」的訊號。這是**啟發式**的，抓得到「整段是中文」，
     抓不到「翻得不好」。
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

#: 現代日文不會用的中文詞。日文譯文 100% 含漢字，所以「有沒有漢字」對日文
#: 完全不是判準；改成抓這些字當作「整段忘了翻」的訊號。
#:
#: **`的` 不可以放進來** —— 「一般的」「自動的」「現代的」都是正確的日文
#: （第一版放了它，當場三條誤報）。這是啟發式的判準，只抓得到「整段留著
#: 中文」，抓不到「翻得不好」。
#:
#: 只留這一份 —— `tests/test_i18n_catalog.py` 直接 import 它
#: （本專案「同一份清單寫兩個地方一定會漂」）。
NOT_JAPANESE = ("這", "那個", "嗎", "們", "什麼", "沒有", "可以",
                "這樣", "一下", "請按", "喔", "很")

# 出現在英文介面上仍然正確的中文（不是漏翻）。
ALLOW_SUBSTR = (
    "Jason Tools 文件工具箱",   # 品牌名
    "繁體中文",                 # 語言切換選項本身
    "文件工具箱",
    # **語言的自稱**：語言選單與「文件語言」下拉一律用該語言自己的寫法
    #（英文使用者看到「Japanese」也不知道那是不是他要的；日文使用者看得懂
    # 「日本語」）。`ui_locale.LOCALE_NAMES` 是唯一來源 —— 翻掉才是錯的。
    "日本語",
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
  // **`<option>` 也要認 `data-i18n="skip"`** —— 有些下拉的選項文字就是
  //  要送給伺服器 / 印到產出上的**值**（個資限用章的用途就是印在章上的字），
  //  翻掉的話畫面上選 A、印出來是 B。原本這一段沒有這道檢查，那幾條就變成
  //  永遠掛在報告上的誤報，而誤報一多這份檢查就會被當雜訊忽略。
  for (const el of document.querySelectorAll('option')) {
    if (el.closest('[data-i18n="skip"]')) continue;
    push(el.textContent, 'option');
  }
  return JSON.stringify(out);
})()
"""


def _pages(base: str) -> list[str]:
    from app.main import app
    from app.tool_registry import discover_tools
    from route_index import iter_routes as _iter
    admin = sorted({r.path for r in _iter(app)
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


def _catalog_keys(locale: str) -> set[str]:
    """該語言語系檔裡**譯文跟原文不一樣、而且本身不是某條譯文**的鍵。

    畫面上出現這些字就是「有譯文卻沒用到」。兩種誤報都要排掉，
    **誤報一多這份檢查就會被當雜訊忽略**（用詞檢查那次的教訓）：

    * **譯文跟原文一樣**：日文有大量詞跟中文寫法完全相同（通知 / 設定 /
      項目 / 標準 / 位置 / 容量 / 科目…）。第一版只看「是不是鍵」，
      82 頁全部中標、617 條裡幾乎都是這一類。
    * **它本身就是另一條的譯文**：`字元` 的日文譯文剛好是 `文字`，而 `文字`
      自己也是一個鍵（譯成「テキスト」）—— 於是正確翻好的欄位標題被判成
      「沒翻」。同類的還有 `小時`→`時間`、`必填`→`必須`。
    """
    f = REPO / "app" / "i18n" / f"{locale}.json"
    if not f.is_file():
        return set()
    data = json.loads(f.read_text(encoding="utf-8"))
    values = {v.strip() for v in data.values()}
    return {k for k, v in data.items()
            if v.strip() != k.strip() and k.strip() not in values}


def _keep(t: str, locale: str, keys: set[str]) -> bool:
    if t in ALLOW_EXACT:
        return False
    raw = t
    for a in ALLOW_SUBSTR:
        t = t.replace(a, "")
    if not CJK.search(t):
        return False
    if locale == "ja":
        # 日文頁上漢字是正常的 —— 見模組開頭那兩個訊號。
        return raw.strip() in keys or any(w in t for w in NOT_JAPANESE)
    return True



#: 掃之前先把「藏起來的」攤開。
#:
#: TEST_PLAN §0.6 記著：瀏覽器逐頁掃**看不到**對話框、要點開的面板、
#: 有資料才出現的表格、送出後的結果區 —— 而使用者一眼就看到
#: （2026-09-05 被使用者連續截了十幾張圖打臉）。
#:
#: 這一段把 DOM 裡**已經存在但沒顯示**的那些攤開來，補上其中一大類。
#: 執行期才建出來的節點（真的按下去才生成的對話框）仍然掃不到 ——
#: **這個方法也有它看不到的東西，不要當成「翻完了」的證明。**
#:
#: 刻意**不改變任何狀態**：只動 `hidden` / `display` / `open`，不送出表單、
#: 不點按鈕 —— 掃描器不可以在被掃的實例上留下資料。
_REVEAL_JS = """(() => {
  let n = 0;
  document.querySelectorAll('[hidden]').forEach(el => {
    el.toggleAttribute('hidden', false); n++;
  });
  document.querySelectorAll('details:not([open])').forEach(el => {
    el.open = true; n++;
  });
  document.querySelectorAll('*').forEach(el => {
    const cs = getComputedStyle(el);
    if (cs.display === 'none') { el.style.setProperty('display', 'block', 'important'); n++; }
    else if (cs.visibility === 'hidden') { el.style.setProperty('visibility', 'visible', 'important'); n++; }
  });
  return n;
})()"""

async def _scan(base: str, cdp_port: int, paths: list[str],
                locale: str, reveal: bool = False) -> dict:
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
            await cmd("Network.setCookie", {"name": "jtdt_locale", "value": locale,
                                            "domain": host, "path": "/"})
            keys = _catalog_keys(locale)
            report: dict = {}
            for path in paths:
                await cmd("Page.navigate", {"url": base + path})
                await asyncio.sleep(1.7)
                if reveal:
                    await cmd("Runtime.evaluate",
                              {"expression": _REVEAL_JS, "returnByValue": True})
                    await asyncio.sleep(0.4)     # 攤開之後版面會重排
                r = await cmd("Runtime.evaluate",
                              {"expression": JS, "returnByValue": True})
                try:
                    items = json.loads(r["result"]["value"])
                except Exception:
                    items = []
                skip = ALLOW_BY_PAGE.get(path, ())
                hits = [i for i in items
                        if _keep(i["text"], locale, keys) and i["where"] not in skip]
                if hits:
                    report[path] = hits
            return report
    finally:
        proc.terminate()


def main() -> int:
    from app.core import ui_locale

    langs = [c for c in ui_locale.SUPPORTED if c != ui_locale.DEFAULT_LOCALE]
    ap = argparse.ArgumentParser()
    ap.add_argument("--locale", default="en", choices=langs)
    ap.add_argument("--base", default="http://127.0.0.1:8799")
    ap.add_argument("--cdp-port", type=int, default=9412)
    ap.add_argument("--reveal", action="store_true",
                    help="掃之前先把藏起來的面板 / 區塊攤開（會有較多誤報，"
                         "結果要逐條看過）")
    args = ap.parse_args()

    paths = [p for p in _pages(args.base) if _is_html(args.base, p)]
    print(f"掃 {len(paths)} 頁（語言 {args.locale}）…")
    report = asyncio.run(_scan(args.base, args.cdp_port, paths,
                               args.locale, reveal=args.reveal))
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
