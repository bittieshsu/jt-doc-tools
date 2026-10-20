#!/usr/bin/env python3
"""全站日期時間「畫面上實際長怎樣」的檢查（v1.17.1 起列為發版必跑）。

`tests/test_datetime_format_is_unified.py` 看的是原始碼（有沒有自己組時間）；這一支看**畫面**：
起一個拋棄式實例、放進帶時間的資料（作業、稽核、API Token、工作區檔案），用瀏覽器把每一頁
用中、英、日三種介面語言打開，讀出畫面上的文字，檢查：

* 時間一律是 `YYYY/MM/DD HH:mm[:ss]`、`MM/DD HH:mm` 或 `HH:mm[:ss]`，24 小時制；
* 不可以出現 `上午` / `下午` / `午前` / `午後` / `AM` / `PM` 接在時間旁邊，也不可以有 `2026-10-19 13:05` 或 `2026-10-19` 這種橫線寫法（檔名裡的不算）；
* 頁面沒有 JavaScript 例外（`jtTime is not defined` 之類 —— 換掉格式化函式最可能壞的地方）。

用法：
    python tools/check_datetime_display.py            # 全部頁面 × 三種語言
    python tools/check_datetime_display.py --only /admin/audit,/my-jobs

回傳 0 = 全部通過；非 0 會列出每一頁的問題。報告不截斷。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOCALES = ("zh-TW", "en", "ja")

# 畫面上「看起來像時間」的東西
_TIME = r"\d{1,2}:\d{2}(?::\d{2})?"
_GOOD = re.compile(rf"(?:\d{{4}}/\d{{2}}/\d{{2}} |\d{{2}}/\d{{2}} )?{_TIME}")
_BAD = [
    (re.compile(rf"\d{{4}}-\d{{1,2}}-\d{{1,2}}[ T]{_TIME}"), "日期用橫線（要用斜線）"),
    (re.compile(rf"(?:上午|下午|午前|午後)\s*{_TIME}|{_TIME}\s*(?:上午|下午|午前|午後)"), "12 小時制（上午 / 下午）"),
    (re.compile(rf"{_TIME}\s*(?:AM|PM|am|pm)\b"), "12 小時制（AM / PM）"),
    (re.compile(rf"\b\d{{1,2}}/\d{{1,2}}/\d{{4}},?\s*{_TIME}"), "月 / 日 / 年 的順序"),
    (re.compile(rf"\d{{4}}/\d{{1,2}}/\d{{1,2}}\s+\d:\d{{2}}\b"), "時沒有補零"),
    (re.compile(rf"\d{{4}}年\d{{1,2}}月\d{{1,2}}日\s*{_TIME}"), "語系的長日期寫法"),
    (re.compile(r"(?<![\w.-])20\d{2}-\d{2}-\d{2}(?![\w-])"), "日期用橫線（要用斜線）"),
]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


_SEED = r'''
import sys, time, types
sys.path.insert(0, {root!r})
from app.core import auth_db, roles, api_tokens, audit_db, job_store, workspace
auth_db.init(); roles.seed_builtin_roles()
now = time.time()
tok = api_tokens.api_tokens.create("datetime-check")
api_tokens.api_tokens.lookup(tok.token)                      # 留下「最後使用」時間
for i, ev in enumerate(("login_success", "tool_invoke", "settings_change")):
    audit_db.log_event(ev, username="checker", ip="127.0.0.1", target="pdf-merge",
                       details={{"n": i}})
audit_db.shutdown() if hasattr(audit_db, "shutdown") else None
job_store.init()
for i, (age, status) in enumerate(((60, "completed"), (3 * 86400, "completed"), (7200, "failed"))):
    t = now - age
    job_store.upsert(types.SimpleNamespace(
        id=f"dtcheck{{i:026d}}", tool_id="pdf-merge", status=status, progress=1.0,
        message="", error=None if status == "completed" else "x", result_path=None,
        result_filename="merged.pdf", owner_id=None, owner_label="checker", client_ip="127.0.0.1",
        meta={{}}, created_at=t - 5, updated_at=t, started_at=t - 4))
workspace.save_bytes_for_key(workspace.key_for_user_id(None), b"%PDF-1.4\n%%EOF\n", "sample.pdf")
print("seeded")
'''


def _start(tmp: Path) -> tuple[subprocess.Popen, int]:
    port = _free_port()
    env = dict(os.environ, JTDT_DATA_DIR=str(tmp), JTDT_HOST="127.0.0.1", JTDT_PORT=str(port),
               JTDT_CSRF_DISABLE="1")
    seed = tmp / "_seed.py"
    seed.write_text(_SEED.format(root=str(ROOT)), encoding="utf-8")
    out = subprocess.run([sys.executable, str(seed)], cwd=ROOT, env=env,
                         capture_output=True, text=True, timeout=120)
    if out.returncode != 0:
        raise SystemExit(f"放測試資料失敗：\n{out.stdout}\n{out.stderr}")
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app",
                             "--host", "127.0.0.1", "--port", str(port)],
                            cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(60):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=2)
            return proc, port
        except Exception:
            time.sleep(1)
    proc.terminate()
    raise SystemExit("拋棄式實例起不來")


def check_text(text: str) -> list[str]:
    """回傳畫面文字裡格式不對的片段（給測試直接呼叫）。"""
    found = []
    for rx, why in _BAD:
        for m in rx.finditer(text):
            found.append(f"{why}：{m.group(0)!r}")
    return found


async def _scan(base: str, pages: list[str]) -> tuple[list[str], int]:
    import httpx
    import websockets
    sys.path.insert(0, str(ROOT))
    from tools.browser_probe import profile_arg

    cdp = _free_port()
    proc = subprocess.Popen(
        ["/usr/bin/chromium-browser", "--headless", "--no-sandbox", "--disable-gpu", profile_arg(),
         f"--remote-debugging-port={cdp}", "--remote-allow-origins=*", "--window-size=1440,1000",
         "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    problems: list[str] = []
    seen_times = 0
    try:
        ws_url = None
        for _ in range(40):
            await asyncio.sleep(0.5)
            try:
                tabs = [t for t in httpx.get(f"http://127.0.0.1:{cdp}/json/list", timeout=2).json()
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
            errors: list[str] = []

            async def cmd(method, params=None):
                nonlocal mid
                mid += 1
                await ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
                while True:
                    msg = json.loads(await ws.recv())
                    if msg.get("method") == "Runtime.exceptionThrown":
                        d = msg["params"]["exceptionDetails"]
                        errors.append((d.get("exception") or {}).get("description") or d.get("text", ""))
                    if msg.get("id") == mid:
                        if "error" in msg:
                            raise RuntimeError(msg["error"])
                        return msg.get("result", {})

            await cmd("Page.enable")
            await cmd("Runtime.enable")
            await cmd("Network.enable")
            await cmd("Emulation.setTimezoneOverride", {"timezoneId": "Asia/Taipei"})
            for loc in LOCALES:
                await cmd("Network.setCookie", {"name": "jtdt_locale", "value": loc,
                                                "domain": "127.0.0.1", "path": "/"})
                await cmd("Emulation.setLocaleOverride", {"locale": loc})
                for path in pages:
                    errors.clear()
                    await cmd("Page.navigate", {"url": base + path})
                    await asyncio.sleep(2.0)
                    r = await cmd("Runtime.evaluate", {
                        "expression": "JSON.stringify({ok: typeof window.jtTime === 'object',"
                                      " text: document.body ? document.body.innerText : ''})",
                        "returnByValue": True})
                    got = json.loads(r["result"]["value"])
                    where = f"[{loc}] {path}"
                    if not got["ok"]:
                        problems.append(f"{where}：沒有載入 jtTime")
                    for e in errors:
                        problems.append(f"{where}：JavaScript 例外 {e.splitlines()[0][:160]}")
                    for p in check_text(got["text"]):
                        problems.append(f"{where}：{p}")
                    seen_times += len(_GOOD.findall(got["text"]))
    finally:
        proc.terminate()
    return problems, seen_times


def _pages(base: str) -> list[str]:
    sys.path.insert(0, str(ROOT))
    from app.tool_registry import discover_tools
    routes = json.load(urllib.request.urlopen(base + "/openapi.json"))["paths"]

    def is_html(p):
        try:
            with urllib.request.urlopen(base + p, timeout=10) as r:
                return r.headers.get("content-type", "").startswith("text/html")
        except Exception:
            return False
    admin = sorted(p for p in routes if p.startswith("/admin/") and "{" not in p
                   and "get" in routes[p] and is_html(p))
    tools = [f"/tools/{t.metadata.id}/" for t in sorted(discover_tools(), key=lambda t: t.metadata.id)]
    return ["/", "/my-jobs", "/workspace"] + tools + admin


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--only", default="", help="只看這些頁（逗號分隔）")
    args = ap.parse_args()
    with tempfile.TemporaryDirectory(prefix="jtdt-dtcheck-") as tmp:
        proc, port = _start(Path(tmp))
        try:
            base = f"http://127.0.0.1:{port}"
            pages = [p for p in args.only.split(",") if p] or _pages(base)
            problems, seen = asyncio.run(_scan(base, pages))
        finally:
            proc.terminate()
            proc.wait(timeout=20)
    print(f"看了 {len(pages)} 頁 × {len(LOCALES)} 種語言，畫面上讀到 {seen} 個時間")
    for p in problems:                    # 不截斷
        print("  ✗", p)
    if seen == 0:
        print("  ✗ 一個時間都沒讀到 —— 測試資料沒進去，這次檢查不算數")
        return 2
    print("全部通過" if not problems else f"{len(problems)} 個問題")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
