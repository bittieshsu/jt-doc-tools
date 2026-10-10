"""轉逐字稿「從工作區載入」—— **真的在瀏覽器裡挑一個錄音檔，送到（假的）JTLW 跑完**。

驗收不是「按鈕有出現」（使用者 2026-09-23 交代時就寫明了）。這裡走一遍使用者會做的事：

1. 錄音檔已經在「我的工作區」裡（種資料時存進去）；
2. 打開轉逐字稿頁、按「從工作區載入」、挑那一個錄音檔；
3. 按「開始轉逐字稿」，等到逐字稿畫出來。

然後驗四件事：挑選視窗裡錄音檔顯示的是**圖示**（沒有去要一張註定空白的縮圖）；
整個過程**沒有把檔案下載到瀏覽器、也沒有再上傳一次**（只打了 `/from-workspace`）；
對方收到的 sha256 跟工作區那一份一樣；照簽章網址拉回來的內容也一模一樣。

**不連真的 JTLW**：對方是測試自己起的假服務（`test_meeting_transcribe.FakeJtlw`）。
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request
import wave

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, str(ROOT))
from tools.browser_probe import browser as _browser  # noqa: E402
from tools.browser_probe import profile_arg as _profile_arg  # noqa: E402

pytestmark = pytest.mark.skipif(
    _browser() is None or __import__("importlib").util.find_spec("websockets") is None,
    reason="沒有 chromium / websockets —— 這條要真的瀏覽器才驗得到")


def _wav() -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"".join(struct.pack("<h", (i * 53) % 3000 - 1500) for i in range(6000)))
    return buf.getvalue()


WAV = _wav()
NAME = "週會錄音.wav"

_SEED = r"""
import sys
from app.core import jtlw_settings, workspace as ws
ws.save_settings({"enabled": True, "per_user_quota_mb": 500, "max_file_mb": 50,
                  "max_audio_mb": 500, "retention_hours": -1})
jtlw_settings.save({"enabled": True, "base_url": sys.argv[1], "api_key_enc": "jtlw_test_key",
                    "audio_base_url": sys.argv[2], "profile_id": jtlw_settings.DEFAULT_PROFILE,
                    "retry_window_hours": 24, "tasks": list(jtlw_settings.DEFAULT_TASKS)})
data = open(sys.argv[3], "rb").read()
meta = ws.save_bytes_for_key(ws.key_for_user_id(None), data, sys.argv[4], "手動上傳")
print(meta["file_id"])
"""


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture(scope="module")
def live():
    from tests.test_meeting_transcribe import FakeJtlw
    data = tempfile.mkdtemp(prefix="mtws-")
    port, cdp = _free_port(), _free_port()
    with FakeJtlw() as fake:
        wav_path = os.path.join(data, "seed.wav")
        with open(wav_path, "wb") as f:
            f.write(WAV)
        # **不關 CSRF**：從工作區接檔是一個新的 POST，要真的帶得上 token 才算數
        env = {**os.environ, "JTDT_DATA_DIR": data}
        env.pop("JTDT_CSRF_DISABLE", None)
        seeded = subprocess.run(
            [sys.executable, "-c", _SEED, fake.base, f"http://127.0.0.1:{port}", wav_path, NAME],
            cwd=ROOT, env=env, check=True, capture_output=True, text=True, timeout=120)
        file_id = seeded.stdout.strip().splitlines()[-1]
        srv = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app",
             "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
            cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        br = subprocess.Popen(
            [_browser(), "--headless=new", "--no-sandbox", "--disable-gpu",
             _profile_arg(), f"--remote-debugging-port={cdp}", "--remote-allow-origins=*",
             "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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
            yield {"port": port, "cdp": cdp, "fake": fake, "file_id": file_id}
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


def _tab(live):
    import websockets.sync.client as wsc
    req = urllib.request.Request(f"http://127.0.0.1:{live['cdp']}/json/new?about:blank",
                                 method="PUT")
    with urllib.request.urlopen(req, timeout=10) as r:
        tab = json.loads(r.read())
    ws = wsc.connect(tab["webSocketDebuggerUrl"], max_size=None, open_timeout=10)
    n = [0]

    def send(method, params=None):
        n[0] += 1
        ws.send(json.dumps({"id": n[0], "method": method, "params": params or {}}))
        while True:
            m = json.loads(ws.recv(timeout=180))
            if m.get("id") == n[0]:
                return m.get("result", {})

    def ev(expr):
        res = send("Runtime.evaluate", {"expression": expr, "returnByValue": True,
                                        "awaitPromise": True})
        assert "exceptionDetails" not in res, res.get("exceptionDetails")
        return res.get("result", {}).get("value")

    def wait(expr, timeout=60, what=""):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if ev(expr):
                    return True
            except AssertionError:
                pass
            time.sleep(0.3)
        raise AssertionError(f"等不到：{what or expr}")

    return ws, send, ev, wait


def test_pick_a_recording_from_the_workspace_and_transcribe_it(live):
    ws, send, ev, wait = _tab(live)
    try:
        send("Page.navigate", {"url": f"http://127.0.0.1:{live['port']}/tools/meeting-transcribe/"})
        wait("!!document.querySelector('#mtUp .ws-load-btn') && "
             "!document.querySelector('#mtUp .ws-load-btn').hidden && "
             "!!window.openWorkspacePicker", what="「從工作區載入」按鈕出現並接上")
        # 記下頁面打了哪些網址：**不可以**把檔案下載下來（/workspace/file/）再上傳（/upload）
        ev("""(() => { window.__urls = [];
            const of = window.fetch;
            window.fetch = function (u) { window.__urls.push(String(u && u.url || u)); return of.apply(this, arguments); };
            const oo = XMLHttpRequest.prototype.open;
            XMLHttpRequest.prototype.open = function (m, u) { window.__urls.push(m + ' ' + u); return oo.apply(this, arguments); };
            return true; })()""")
        ev("document.querySelector('#mtUp .ws-load-btn').click(), true")
        wait("document.querySelectorAll('#ws-picker-modal .ws-pick-card').length > 0",
             what="挑選視窗列出工作區的錄音檔")
        card = ev("""(() => { const c = document.querySelector('#ws-picker-modal .ws-pick-card');
            const np = c.querySelector('.ws-noprev'); const r = np && np.getBoundingClientRect();
            return {name: c.querySelector('.ws-pick-name').textContent,
                    img: !!c.querySelector('img'), icon: !!(np && np.querySelector('svg')),
                    w: r ? r.width : 0, h: r ? r.height : 0,
                    ext: np ? np.querySelector('.ws-noprev-ext').textContent : ''}; })()""")
        assert card["name"] == NAME, card
        assert not card["img"], "錄音檔還在要縮圖（只會拿到一張空白圖）"
        assert card["icon"] and card["w"] > 20 and card["h"] > 20, f"沒有畫出圖示：{card}"
        assert card["ext"] == "WAV", card
        ev("document.querySelector('#ws-picker-modal .ws-pick-card').click(), true")
        wait("!document.getElementById('mtOpts').hidden && "
             f"document.getElementById('mtFileInfo').textContent.includes({json.dumps(NAME)})",
             what="接過來之後出現選項區與檔名")
        ev("document.getElementById('mtStart').click(), true")
        wait("!document.getElementById('mtResult').hidden && "
             "document.querySelectorAll('#mtSegs > *').length > 0",
             timeout=120, what="逐字稿畫出來")

        urls = ev("window.__urls")
        assert any("/tools/meeting-transcribe/from-workspace" in u for u in urls), urls
        assert not any("/tools/meeting-transcribe/upload" in u for u in urls), \
            f"從工作區載入卻又上傳了一次：{urls}"
        assert not any("/workspace/file/" in u for u in urls), \
            f"把錄音檔整份下載到瀏覽器了：{urls}"

        src = live["fake"].seen["body"]["source"]
        assert src["sha256"] == hashlib.sha256(WAV).hexdigest()
        assert src["size_bytes"] == len(WAV)
        with urllib.request.urlopen(src["url"], timeout=10) as r:
            assert r.read() == WAV, "對方拉到的不是工作區那一份"
    finally:
        ws.close()
