"""設定備份頁：匯入之後「沒有還原的項目」真的列在畫面上（issue #55）。

伺服器回的是樣板＋參數（`SKIP_MESSAGES`），由頁面 `tr()` 再組字 —— 只驗端點的話，
頁面沒接上或把物件直接塞成 `[object Object]` 都看不出來，所以真的在瀏覽器裡走一次：
選檔 → 讀取備份內容 → 匯入 → 確認。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

import pytest

from tests.test_kb_embedding_settings_location import _Tab, _free_port, _needs_browser
from tools import browser_probe
from tools.browser_probe import profile_arg as _profile_arg  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def live():
    data = tempfile.mkdtemp(prefix="setimp-")
    port, cdp = _free_port(), _free_port()
    env = {**os.environ, "JTDT_DATA_DIR": data, "JTDT_CSRF_DISABLE": "1"}
    srv = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
         "--port", str(port), "--log-level", "warning"],
        cwd=str(ROOT), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    br = subprocess.Popen(
        [browser_probe.browser(), "--headless=new", "--no-sandbox", "--disable-gpu",
         _profile_arg(), f"--remote-debugging-port={cdp}", "--remote-allow-origins=*", "about:blank"],
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
        yield port, cdp, Path(data)
    finally:
        br.terminate()
        srv.terminate()
        try:
            br.wait(timeout=5)
            srv.wait(timeout=5)
        except Exception:
            br.kill()
            srv.kill()
        shutil.rmtree(data, ignore_errors=True)


def _backup_zip() -> Path:
    """另一台匯出的：認證是本機登入、alice（9001 號）的工作區 —— 這台全新、一個帳號都沒有。"""
    d = Path(browser_probe.uploadable_dir("setimp"))
    p = d / "other-host.zip"
    entries = {"auth": ["data/auth_settings.json"],
               "workspace": ["data/workspace/u9001/a.pdf", "data/workspace/u9001/b.pdf"]}
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("manifest.json", json.dumps({
            "kind": "jtdt-settings-export", "schema_version": 2, "app_version": "test",
            "categories": [{"id": c, "label": c} for c in entries], "entries_by_category": entries}))
        zf.writestr("identity.json", json.dumps({"version": 1, "groups": {},
                                                 "users": {"9001": {"username": "alice", "source": "local"}}}))
        zf.writestr("data/auth_settings.json", json.dumps({"backend": "local"}))
        zf.writestr("data/workspace/u9001/a.pdf", b"x")
        zf.writestr("data/workspace/u9001/b.pdf", b"y")
    return p


@_needs_browser
@pytest.mark.parametrize("locale, words", [
    ("zh-Hant", ("沒有人登得進來", "這台沒有帳號「alice@local」", "（2 個檔案）")),
    ("en", ("lock everyone out", "there is no account “alice@local”", "(2 files)")),
])
def test_skipped_items_are_listed_after_import(live, locale, words):
    port, cdp, data = live
    zp = _backup_zip()
    t = _Tab(cdp)
    try:
        t.send("Network.enable")
        t.send("Network.setCookie", {"name": "jtdt_locale", "value": locale,
                                     "url": f"http://127.0.0.1:{port}/"})
        t.go(f"http://127.0.0.1:{port}/admin/settings-export")
        assert t.wait_for("!!document.getElementById('expMoveHost')"), "沒有「整台搬家」的說明"
        doc = t.send("DOM.getDocument", {"depth": -1})["result"]["root"]["nodeId"]
        node = t.send("DOM.querySelector", {"nodeId": doc, "selector": "#importFile"})["result"]["nodeId"]
        t.send("DOM.setFileInputFiles", {"nodeId": node, "files": [str(zp)]})
        t.js("document.getElementById('importFile').dispatchEvent(new Event('change', {bubbles: true}))")
        t.js("document.getElementById('btnPreview').click()")
        assert t.wait_for("document.querySelectorAll('.imp-cat').length === 2"), "讀不到備份內容"
        t.js("document.getElementById('btnImport').click()")
        assert t.wait_for("!!document.querySelector('.modal-ok')")
        t.js("document.querySelector('.modal-ok').click()")
        assert t.wait_for("!document.getElementById('importSkipped').hidden", 20), \
            t.js("document.getElementById('importStatus').textContent")
        text = t.js("document.getElementById('importSkipped').textContent")
        for w in words:
            assert w in text, (w, text)
        assert "object Object" not in text and "{0}" not in text and "{1}" not in text
        # 真的沒還原：這台沒有 alice，工作區不可以落到任何人身上；認證照樣是關的
        assert not list((data / "workspace").glob("u*")) if (data / "workspace").exists() else True
        st = json.loads((data / "auth_settings.json").read_text(encoding="utf-8")) \
            if (data / "auth_settings.json").exists() else {"backend": "off"}
        assert st.get("backend") == "off"
        t.drain()
        assert not t.errs, t.errs
    finally:
        t.close()
