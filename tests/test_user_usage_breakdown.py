"""系統狀態「使用者檔案用量」可以展開看各類別（2026-10-09 使用者：「現在有看每個使用者用量
但可以再往下點入深看該使用者的各種類的使用量是多少嗎」）。

統計多算了三類原本沒算進去的：我的工作區（`workspace/u<編號>`）、公文撰擬案件
（`official_doc_cases/<案件>/meta.json` 的 owner_uid）、會議錄音（`speech_audio/<上傳編號>.bin`，
歸屬跟上傳同一份紀錄）。每一列另外帶 `categories`（依類別）與 `tools_30d`（近 30 天依工具）。
"""
from __future__ import annotations

import json
import shutil
import uuid

import pytest

from app.config import settings
from app.core import host_stats


@pytest.fixture
def seeded():
    from app.core import auth_db, roles, user_manager
    auth_db.init()
    roles.seed_builtin_roles()
    name = "usage-" + uuid.uuid4().hex[:8]
    uid = user_manager.create_local(name, name, "Passw0rd!xyz")
    made = []
    d = settings.data_dir
    # 工作區兩個檔
    ws = d / "workspace" / f"u{uid}" / uuid.uuid4().hex
    ws.mkdir(parents=True)
    (ws / "file.pdf").write_bytes(b"x" * 1000)
    (ws / "meta.json").write_text("{}", encoding="utf-8")
    made.append(ws.parent)
    # 公文撰擬案件
    case = d / "official_doc_cases" / uuid.uuid4().hex
    case.mkdir(parents=True)
    meta = json.dumps({"owner_uid": uid})
    (case / "meta.json").write_text(meta, encoding="utf-8")
    (case / "result.json").write_bytes(b"y" * 3000)
    made.append(case)
    # 會議錄音（歸屬跟上傳同一份紀錄）
    up = uuid.uuid4().hex
    owners = d / "temp" / ".owners"
    owners.mkdir(parents=True, exist_ok=True)
    (owners / f"{up}.json").write_text(json.dumps({"user_id": uid, "ts": 0}), encoding="utf-8")
    made.append(owners / f"{up}.json")
    sp = d / "speech_audio"
    sp.mkdir(parents=True, exist_ok=True)
    (sp / f"{up}.bin").write_bytes(b"z" * 5000)
    made.append(sp / f"{up}.bin")
    # 暫存上傳
    tmp = d / "temp" / f"{up}_src.pdf"
    tmp.write_bytes(b"t" * 700)
    made.append(tmp)
    yield name, len(meta)
    for p in made:
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
        else:
            p.unlink(missing_ok=True)
    user_manager.delete(uid)


def test_each_user_row_carries_a_breakdown_by_category(seeded):
    name, meta_len = seeded
    out = host_stats._compute_user_file_stats()
    row = next(r for r in out["users"] if r["username"] == name)
    cats = {c["key"]: (c["count"], c["bytes"]) for c in row["categories"]}
    assert cats["workspace"] == (2, 1002), cats
    assert cats["cases"] == (2, 3000 + meta_len), cats
    assert cats["speech"] == (1, 5000), cats
    assert cats["temp"] == (1, 700), cats
    # 類別加起來就是那一列的合計（不會漏算或重算）
    assert sum(c["count"] for c in row["categories"]) == row["count"]
    assert sum(c["bytes"] for c in row["categories"]) == row["bytes"]
    # 大的排前面
    sizes = [c["bytes"] for c in row["categories"]]
    assert sizes == sorted(sizes, reverse=True)
    assert row["tools_30d"] == [] or all("tool" in t and "name" in t for t in row["tools_30d"])


def test_every_category_key_has_a_label():
    """畫面上的類別名稱由伺服器送（`USAGE_CATEGORIES`）；統計會出現的每一個 key 都要有名稱。"""
    keys = {k for k, _ in host_stats.USAGE_CATEGORIES}
    assert {"temp", "workspace", "cases", "speech", "fill_history", "stamp_history",
            "watermark_history"} <= keys


def test_clicking_a_user_opens_the_breakdown_in_a_browser(tmp_path):
    """真的在瀏覽器裡按使用者名稱：下面展開一列，左邊依類別（名稱是翻譯過的類別名，不是 key）、
    右邊近 30 天依工具；再按一次收起來。"""
    import os
    import socket
    import subprocess
    import sys
    import time
    import urllib.request
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(root))
    from tools import browser_probe
    br_path = browser_probe.browser()
    if not br_path:
        pytest.skip("沒有 chromium")
    try:
        import websockets.sync.client as wsc
    except ImportError:
        pytest.skip("沒有 websockets")

    def free():
        s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p

    data = tmp_path / "data"
    (data / "temp").mkdir(parents=True)
    (data / "temp" / "stray_file.bin").write_bytes(b"q" * 4096)       # 沒有擁有者紀錄 → 未追蹤暫存
    (data / "auth_settings.json").write_text(json.dumps({"backend": "off"}), encoding="utf-8")
    port, cdp = free(), free()
    env = {**os.environ, "JTDT_DATA_DIR": str(data), "JTDT_CSRF_DISABLE": "1"}
    srv = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
                            "--port", str(port), "--log-level", "warning"],
                           cwd=root, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    br = subprocess.Popen([br_path, "--headless=new", "--no-sandbox", "--disable-gpu",
                           f"--remote-debugging-port={cdp}", "--remote-allow-origins=*", "about:blank"],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(120):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1)
                urllib.request.urlopen(f"http://127.0.0.1:{cdp}/json/version", timeout=1)
                break
            except Exception:
                time.sleep(0.5)
        else:
            pytest.skip("實例或瀏覽器起不來")
        req = urllib.request.Request(f"http://127.0.0.1:{cdp}/json/new?about:blank", method="PUT")
        with urllib.request.urlopen(req, timeout=10) as r:
            tab = json.loads(r.read())
        with wsc.connect(tab["webSocketDebuggerUrl"], max_size=None, open_timeout=10) as ws:
            n = [0]
            errs = []

            def send(method, params=None):
                n[0] += 1
                ws.send(json.dumps({"id": n[0], "method": method, "params": params or {}}))
                while True:
                    m = json.loads(ws.recv(timeout=60))
                    if m.get("method") == "Runtime.exceptionThrown":
                        errs.append(str(m["params"]["exceptionDetails"])[:300])
                    if m.get("id") == n[0]:
                        return m

            def js(expr):
                r = send("Runtime.evaluate", {"expression": expr, "returnByValue": True, "awaitPromise": True})
                return r.get("result", {}).get("result", {}).get("value")

            def wait(expr, t=30):
                end = time.time() + t
                while time.time() < end:
                    v = js(expr)
                    if v:
                        return v
                    time.sleep(0.3)
                return None

            send("Runtime.enable")
            send("Page.navigate", {"url": f"http://127.0.0.1:{port}/admin/system-status"})
            assert wait("document.querySelectorAll('#ssUserStats .ss-user-btn').length > 0"), "用量表沒有畫出來"
            js("document.querySelector('#ssUserStats .ss-user-btn').click(); true")
            got = wait("""(function () { var d = document.querySelector('#ssUserStats tr.ss-detail');
                if (!d) return null;
                return {heads: Array.from(d.querySelectorAll('h4')).map(function (h) { return h.textContent; }),
                        labels: Array.from(d.querySelectorAll('.ss-mini td:first-child')).map(function (t) {
                            return t.textContent; }),
                        expanded: document.querySelector('#ssUserStats .ss-user-btn').getAttribute('aria-expanded')};
                })()""")
            assert got, "按了使用者名稱沒有展開明細"
            assert got["heads"] == ["目前佔用（依類別）", "近 30 天上傳（依工具）"], got
            assert "暫存檔（處理中的上傳與產出）" in got["labels"], got
            assert got["expanded"] == "true"
            js("document.querySelector('#ssUserStats .ss-user-btn').click(); true")
            assert js("document.querySelectorAll('#ssUserStats tr.ss-detail').length") == 0
            assert not errs, errs
        urllib.request.urlopen(f"http://127.0.0.1:{cdp}/json/close/{tab['id']}", timeout=5).read()
    finally:
        for p in (br, srv):
            p.terminate()
            try:
                p.wait(timeout=10)
            except Exception:
                p.kill()
