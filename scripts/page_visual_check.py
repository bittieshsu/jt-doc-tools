#!/usr/bin/env python3
"""每頁畫面 + 關鍵元素可見性回歸檢查（發版前跑）。

目的：抓「元素 / 功能靜默消失」這一類 regression。例如 v1.12.30 的 CSP 樣式
重構把元素的 display:none 從 inline 樣式改成 CSS 規則後，用 `style.display=''`
顯示元素的舊 JS 就失效，讓「下載」按鈕、臨時資產縮圖、個資限用章預覽等
「存檔 / 選圖後才出現」的元素一直不顯示（v1.12.71 修）。純像素比對對字型 /
時間戳 / 動態內容太吵，所以本工具的主檢查是「可見互動元素清單」比對 + 關鍵
狀態斷言，截圖僅供人工對照。

需要：一個 headless chromium（dev1：chromium-browser）+ 一個 auth-off 的
本機實例（見 TEST_PLAN.md §1.8），且該實例工作區內要有一個 PDF 供 pdf-editor
狀態檢查（腳本會自動注入）。

用法：
  # 1) 起 auth-off 實例（臨時 data dir）
  #    JTDT_DATA_DIR=$(mktemp -d) JTDT_CSRF_DISABLE=1 uvicorn app.main:app --port 8799
  # 2) 建立 / 更新 baseline（UI 有意改動後才更新）
  python scripts/page_visual_check.py --base http://127.0.0.1:8799 --update
  # 3) 比對（發版前）——有元素消失就 exit 1
  python scripts/page_visual_check.py --base http://127.0.0.1:8799

輸出：截圖 + 清單存 temp/visual/<run>/；baseline 清單存
tests/visual/baseline_inventory.json（進版控，供跨版本比對）。
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import io
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BASELINE = REPO / "tests" / "visual" / "baseline_inventory.json"

# 所有工具的落地頁（/tools/<id>/）。每頁至少要有「主要動作可見」。
#
# **清單從註冊表現算，不在這裡寫死。** 原本這裡有一份手寫的 39 個 id，
# 而註冊表早已是 46 個 —— office-convert / pdf-bookmark / pdf-border /
# pdf-page-size / pdf-seam-stamp / pdf-to-slides / transit-proof 七支
# **從來沒有被這個檢查掃過**，而漏掉是無聲的：報告照樣印「全部通過」。
# 這是本專案反覆出現的同一類 bug（同一份清單存在兩個地方）。
def _registered_tool_ids() -> list[str]:
    import sys as _sys
    _sys.path.insert(0, str(REPO))
    from app.tool_registry import discover_tools
    return sorted(t.metadata.id for t in discover_tools())


TOOL_IDS = _registered_tool_ids()

# 額外頁面（非工具落地頁）
EXTRA_PAGES = [
    ("home", "/"),
    ("workspace", "/workspace"),
]

# 收集「可見互動元素」清單的 JS。回傳 buttons（可見按鈕文字）、inputs（可見
# 輸入的 type/name/id）、hasDropzone、visibleControlCount。用 offsetParent!==null
# + computed display!==none 判定「真的看得到」（能抓 CSS 規則造成的隱藏）。
INVENTORY_JS = r"""
(() => {
  const isVis = (el) => {
    if (!el) return false;
    if (el.hidden) return false;
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden' || parseFloat(cs.opacity) === 0) return false;
    // offsetParent 為 null 代表被隱藏（position:fixed 例外，但控制項少見）
    if (el.offsetParent === null && cs.position !== 'fixed') return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };
  const norm = (s) => (s || '').replace(/\s+/g, ' ').trim().slice(0, 40);
  const btns = [];
  document.querySelectorAll('button, a.btn, input[type=submit]').forEach(b => {
    if (!isVis(b)) return;
    const t = norm(b.textContent) || norm(b.value) || norm(b.getAttribute('aria-label')) || ('#' + (b.id || '?'));
    if (t) btns.push(t);
  });
  const inputs = [];
  document.querySelectorAll('input, select, textarea').forEach(i => {
    if (!isVis(i)) return;
    if (i.type === 'hidden') return;
    inputs.push((i.tagName.toLowerCase()) + ':' + (i.type || '') + ':' + (i.name || i.id || ''));
  });
  const hasDropzone = !!document.querySelector('.drop-zone');
  // 版面溢出：卡片比內容欄還寬 / 整頁跑出水平捲軸。
  // v1.14.60 把一張卡片放進 `{% block scripts %}`（在 <main> 外面），
  // 它就攤成整個視窗寬、左邊壓到側欄底下 —— 沒有任何錯誤訊息，
  // 「元素有沒有消失」也照樣全綠，只有用眼睛看的人發現。
  const de = document.documentElement;
  const main = document.querySelector('main');
  const wide = [];
  if (main) {
    const mr = main.getBoundingClientRect();
    document.querySelectorAll('main .panel, .panel').forEach(el => {
      if (!isVis(el)) return;
      const r = el.getBoundingClientRect();
      if (r.width > mr.width + 2 || r.left < mr.left - 2 || r.right > mr.right + 2) {
        wide.push(norm((el.querySelector('h2') || {}).textContent) || '(無標題卡片)');
      }
    });
  }
  return {
    buttons: btns.sort(),
    inputCount: inputs.length,
    hasDropzone,
    visibleControlCount: btns.length + inputs.length,
    title: document.title,
    overflowX: Math.max(0, de.scrollWidth - de.clientWidth),
    cardsOutsideMain: wide.sort(),
  };
})()
"""


def _cdp_page_ws(cdp_port: int) -> str:
    d = json.load(urllib.request.urlopen(f"http://127.0.0.1:{cdp_port}/json"))
    pages = [t for t in d if t.get("type") == "page"]
    if not pages:
        raise RuntimeError("no chromium page target")
    return pages[0]["webSocketDebuggerUrl"]


class CDP:
    def __init__(self, ws):
        self.ws = ws
        self.i = 0

    async def cmd(self, method, **params):
        import websockets  # noqa: F401 (imported lazily so --help works without it)
        self.i += 1
        mid = self.i
        await self.ws.send(json.dumps({"id": mid, "method": method, "params": params}))
        while True:
            msg = json.loads(await self.ws.recv())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})

    async def ev(self, expr, await_promise=True):
        r = await self.cmd("Runtime.evaluate", expression=expr,
                           awaitPromise=await_promise, returnByValue=True)
        if "exceptionDetails" in r:
            raise RuntimeError("JS exc: " + json.dumps(r["exceptionDetails"])[:200])
        return r["result"].get("value")

    async def shot(self, path: Path):
        r = await self.cmd("Page.captureScreenshot", format="png")
        path.write_bytes(base64.b64decode(r["data"]))


async def _wait(cdp, expr, timeout=20):
    for _ in range(timeout * 4):
        try:
            if await cdp.ev(expr):
                return True
        except Exception:
            pass
        await asyncio.sleep(0.25)
    return False


def _inject_workspace_pdf(base: str) -> bool:
    """確保工作區至少有一個 PDF（pdf-editor 狀態檢查用）。回傳是否成功。"""
    try:
        import httpx
        import fitz
    except Exception:
        return False
    try:
        c = httpx.Client(timeout=20)
        r = c.get(f"{base}/workspace/api/list?accept=pdf")
        if r.status_code == 200 and (r.json().get("files")):
            return True
        d = fitz.open()
        d.new_page(width=595, height=842)
        pdf = d.tobytes()
        d.close()
        r = c.post(f"{base}/workspace/save",
                   files={"file": ("visual_check.pdf", pdf, "application/pdf")},
                   data={"name": "visual_check.pdf", "source_tool": "visual-check"})
        return r.status_code == 200
    except Exception:
        return False


async def _run(base: str, cdp_port: int, outdir: Path, update: bool):
    import websockets
    ws = _cdp_page_ws(cdp_port)
    baseline = json.loads(BASELINE.read_text()) if BASELINE.exists() else {}
    new_inv: dict[str, dict] = {}
    failures: list[str] = []
    warnings: list[str] = []

    have_ws_pdf = _inject_workspace_pdf(base)

    async with websockets.connect(ws, max_size=40_000_000) as sock:
        cdp = CDP(sock)
        await cdp.cmd("Page.enable")
        await cdp.cmd("Runtime.enable")
        await cdp.cmd("Emulation.setDeviceMetricsOverride", width=1440, height=980,
                      deviceScaleFactor=1, mobile=False)

        pages = [(tid, f"/tools/{tid}/") for tid in TOOL_IDS] + list(EXTRA_PAGES)
        for name, path in pages:
            url = base.rstrip("/") + path
            await cdp.cmd("Page.navigate", url=url)
            await asyncio.sleep(1.4)
            await _wait(cdp, "document.readyState === 'complete'")
            await asyncio.sleep(0.4)
            try:
                inv = await cdp.ev(INVENTORY_JS)
            except Exception as e:
                failures.append(f"{name}: 頁面載入 / inventory 失敗 ({e})")
                continue
            new_inv[name] = inv
            await cdp.shot(outdir / f"{name}.png")

            # 與 baseline 比對：baseline 有、現在不見的可見按鈕 → 功能可能消失
            base_inv = baseline.get(name)
            if base_inv:
                lost = sorted(set(base_inv.get("buttons", [])) - set(inv.get("buttons", [])))
                if lost:
                    failures.append(f"{name}: 消失的可見按鈕 {lost}")
                dc = base_inv.get("visibleControlCount", 0) - inv.get("visibleControlCount", 0)
                if dc > 0 and not lost:
                    warnings.append(f"{name}: 可見控制項數量少了 {dc}（{base_inv.get('visibleControlCount')}→{inv.get('visibleControlCount')}）")
            # 每個工具落地頁至少要有一個可見的主要動作
            if path.startswith("/tools/") and inv.get("visibleControlCount", 0) == 0:
                failures.append(f"{name}: 頁面沒有任何可見的互動控制項")
            # 版面溢出（v1.14.61 使用者回報「最下面卡片超過畫面」）
            if inv.get("cardsOutsideMain"):
                failures.append(f"{name}: 卡片超出內容欄 {inv['cardsOutsideMain']}")
            if inv.get("overflowX", 0) > 2:
                failures.append(f"{name}: 整頁有水平捲動（多出 {inv['overflowX']}px）")

        # ---- 關鍵狀態斷言：抓「動作後才出現」的元素（download-after-save 類）----
        if have_ws_pdf:
            ok = await _assert_editor_download_after_save(cdp, base, outdir)
            if ok is False:
                failures.append("pdf-editor: 存檔後「下載」按鈕仍不可見（download-after-save regression）")
            elif ok is None:
                warnings.append("pdf-editor: 狀態檢查無法完成（略過，非失敗）")
        else:
            warnings.append("pdf-editor: 工作區無 PDF，略過 download-after-save 狀態檢查")

    # 輸出
    (outdir / "inventory.json").write_text(json.dumps(new_inv, ensure_ascii=False, indent=1))
    if update:
        BASELINE.parent.mkdir(parents=True, exist_ok=True)
        BASELINE.write_text(json.dumps(new_inv, ensure_ascii=False, indent=1))
        print(f"[updated] baseline 已更新：{BASELINE}（{len(new_inv)} 頁）")

    print(f"\n截圖 + 清單：{outdir}")
    for w in warnings:
        print("  [warn]", w)
    if failures:
        print(f"\n❌ FAIL（{len(failures)}）：")
        for f in failures:
            print("  -", f)
        return 1
    print(f"\n✅ PASS — {len(new_inv)} 頁，無元素消失。")
    return 0


async def _assert_editor_download_after_save(cdp, base, outdir) -> "bool|None":
    """pdf-editor：從工作區載入 → 儲存並預覽 → #btnDownload 必須可見。"""
    try:
        await cdp.cmd("Page.navigate", url=base.rstrip("/") + "/tools/pdf-editor/")
        await asyncio.sleep(2.0)
        if not await _wait(cdp, "!!document.querySelector('.ws-load-btn')"):
            return None
        await cdp.ev("document.querySelector('.ws-load-btn').click(); true", await_promise=False)
        if not await _wait(cdp, "!!document.querySelector('.ws-pick-card')"):
            return None
        await cdp.ev("document.querySelector('.ws-pick-card').click(); true", await_promise=False)
        if not await _wait(cdp, "document.querySelectorAll('.pe-page').length>0 && !document.getElementById('editor-panel').hidden"):
            return None
        await asyncio.sleep(0.8)
        await cdp.ev("document.getElementById('btnSave').click(); true", await_promise=False)
        vis = await _wait(cdp, "(()=>{const d=document.getElementById('btnDownload');return !!(d&&d.offsetParent)})()", timeout=25)
        await cdp.shot(outdir / "_state_editor_download.png")
        return bool(vis)
    except Exception:
        return None


def _launch_chromium(cdp_port: int, profile: Path):
    for exe in ("chromium-browser", "chromium", "google-chrome"):
        try:
            subprocess.Popen(
                [exe, "--headless=new", f"--remote-debugging-port={cdp_port}",
                 f"--user-data-dir={profile}", "--window-size=1440,980",
                 "--no-sandbox", "--disable-gpu", "--hide-scrollbars", "about:blank"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True)
            for _ in range(40):
                try:
                    urllib.request.urlopen(f"http://127.0.0.1:{cdp_port}/json/version", timeout=1)
                    return exe
                except Exception:
                    time.sleep(0.5)
        except FileNotFoundError:
            continue
    raise RuntimeError("找不到可用的 chromium（chromium-browser / chromium / google-chrome）")


def main():
    ap = argparse.ArgumentParser(description="每頁畫面 + 關鍵元素可見性回歸檢查")
    ap.add_argument("--base", default="http://127.0.0.1:8799", help="auth-off 實例 base URL")
    ap.add_argument("--cdp-port", type=int, default=9223)
    ap.add_argument("--update", action="store_true", help="把本次清單存成新 baseline")
    ap.add_argument("--outdir", default=None, help="截圖輸出目錄（預設 temp/visual/<ts>）")
    args = ap.parse_args()

    ts = time.strftime("%Y%m%d-%H%M%S")
    outdir = Path(args.outdir) if args.outdir else (REPO / "temp" / "visual" / ts)
    outdir.mkdir(parents=True, exist_ok=True)
    profile = REPO / "temp" / "visual" / f".chromium-{args.cdp_port}"

    exe = _launch_chromium(args.cdp_port, profile)
    print(f"[chromium] {exe} @ CDP {args.cdp_port}")
    try:
        rc = asyncio.run(_run(args.base, args.cdp_port, outdir, args.update))
    finally:
        subprocess.run(["pkill", "-f", f"remote-debugging-port={args.cdp_port}"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    sys.exit(rc)


if __name__ == "__main__":
    main()
