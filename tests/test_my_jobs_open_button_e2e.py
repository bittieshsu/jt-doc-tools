"""「我的作業」在**真的瀏覽器**裡：打得開的那一列才有「開啟」。

`tests/test_job_view_ok.py` 驗的是伺服器的判斷與那支決定出口的純函式；這一支
把整條路走一次 —— 拋棄式實例、真的資料庫列與暫存檔、真的 `/api/jobs`、
真的 `render()` 把按鈕畫出來 —— 因為「伺服器算對了」跟「畫面照著畫」是兩件事
（本專案 UI 接線的 bug 幾乎都只有真的跑一次 JS 才看得到）。

三列：

* 會議摘要，資料還在 → 有「開啟」、**沒有**「結果已逾期清除」
* 會議摘要，資料已清掉（過了保留期）→ **沒有**「開啟」、寫「結果已逾期清除」
  （原本兩個並排，按「開啟」是 410）
* 逐句翻譯，對照表還在、沒有結果檔 → 有「開啟」、沒有「結果已逾期清除」
  （判斷若改用 `has_result`，這一列的「開啟」會不見）
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, str(ROOT))
from tools.browser_probe import browser as _browser  # noqa: E402
from tools.browser_probe import profile_arg as _profile_arg  # noqa: E402

pytestmark = pytest.mark.skipif(
    _browser() is None or __import__("importlib").util.find_spec("websockets") is None,
    reason="沒有 chromium / websockets —— 這條要真的瀏覽器才驗得到")


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _seed(data: pathlib.Path) -> None:
    """在拋棄式實例的資料目錄裡放三件已完成的作業（來源是本機，認證關閉）。"""
    from app.config import settings
    from app.core import job_store
    from app.core.job_manager import Job

    temp = data / "temp"
    temp.mkdir(parents=True, exist_ok=True)
    alive, gone, trd = (uuid.uuid4().hex for _ in range(3))
    uid_alive, uid_gone = uuid.uuid4().hex, uuid.uuid4().hex
    (temp / f"ms_{uid_alive}_result.json").write_text("{}", encoding="utf-8")
    (temp / f"trd_{trd}.json").write_text("[]", encoding="utf-8")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(settings, "data_dir", data)
        job_store.init()
        for jid, uid, tool, name in (
                (alive, uid_alive, "meeting-summary", "alive-summary"),
                (gone, uid_gone, "meeting-summary", "expired-summary"),
                (trd, "", "translate-doc", "sentence-table")):
            j = Job(id=jid, tool_id=tool, status="done",
                    meta={"filename": name,
                          "view_url": f"/tools/{tool}/?job={jid}",
                          **({"upload_id": uid} if uid else {})})
            j.client_ip = "127.0.0.1"
            job_store.upsert(j)


@pytest.fixture(scope="module")
def rows():
    data = pathlib.Path(tempfile.mkdtemp(prefix="myjobs-open-"))
    _seed(data)
    port, cdp = _free_port(), _free_port()
    env = {**os.environ, "JTDT_DATA_DIR": str(data), "JTDT_CSRF_DISABLE": "1"}
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
        yield _read_rows(cdp, f"http://127.0.0.1:{port}/my-jobs")
    finally:
        # 只收自己起的那兩個行程（不可以用名字批次殺 —— 這台機器有別的專案在跑）
        br.terminate(); srv.terminate()
        try:
            br.wait(timeout=5); srv.wait(timeout=5)
        except Exception:
            br.kill(); srv.kill()
        shutil.rmtree(data, ignore_errors=True)


_READ = r"""
(() => {
  const out = {};
  document.querySelectorAll('.mj-item').forEach((it) => {
    const f = it.querySelector('.mj-file');
    const acts = it.querySelector('.mj-actions');
    out[f ? f.textContent : '?'] = {
      buttons: acts ? [...acts.querySelectorAll('a,button')].map((b) => b.textContent.trim()) : [],
      gone: !!(acts && acts.querySelector('.mj-gone')),
      hrefs: acts ? [...acts.querySelectorAll('a')].map((a) => a.getAttribute('href')) : [],
    };
  });
  return JSON.stringify(out);
})()
"""


def _read_rows(cdp: int, url: str) -> dict:
    import websockets.sync.client as wsc

    req = urllib.request.Request(
        f"http://127.0.0.1:{cdp}/json/new?about:blank", method="PUT")
    with urllib.request.urlopen(req, timeout=10) as r:
        tab = json.loads(r.read())
    errs: list[str] = []
    try:
        with wsc.connect(tab["webSocketDebuggerUrl"], max_size=None,
                         open_timeout=10) as ws:
            n = [0]

            def send(method, params=None):
                n[0] += 1
                ws.send(json.dumps({"id": n[0], "method": method,
                                    "params": params or {}}))
                while True:
                    m = json.loads(ws.recv(timeout=180))
                    if m.get("method") == "Runtime.exceptionThrown":
                        errs.append(str(m["params"]["exceptionDetails"])[:300])
                    if m.get("id") == n[0]:
                        return m

            send("Runtime.enable")
            send("Page.navigate", {"url": url})
            deadline = time.time() + 30
            got: dict = {}
            while time.time() < deadline:
                v = send("Runtime.evaluate", {"expression": _READ,
                                              "returnByValue": True})
                raw = (v.get("result") or {}).get("result", {}).get("value")
                got = json.loads(raw) if raw else {}
                if len(got) >= 3:
                    break
                time.sleep(0.3)
            assert not errs, f"「我的作業」頁面丟了例外：{errs}"
            return got
    finally:
        try:
            urllib.request.urlopen(
                f"http://127.0.0.1:{cdp}/json/close/{tab['id']}", timeout=5).read()
        except Exception:
            pass


def test_all_three_rows_are_on_the_page(rows):
    """先證明真的畫出來了 —— 一列都沒有的話，下面幾條「沒有開啟」會白白成立。"""
    assert set(rows) >= {"alive-summary", "expired-summary", "sentence-table"}, rows


def test_a_row_whose_data_is_still_there_can_be_opened(rows):
    r = rows["alive-summary"]
    assert "開啟" in r["buttons"], r
    assert not r["gone"], r


def test_an_expired_row_no_longer_offers_open(rows):
    r = rows["expired-summary"]
    assert "開啟" not in r["buttons"], (
        f"資料已經清掉了，「開啟」按下去會是 410：{r}")
    assert r["gone"], "要說「結果已逾期清除」"


def test_the_sentence_table_is_openable_without_a_result_file(rows):
    r = rows["sentence-table"]
    assert "開啟" in r["buttons"], (
        f"逐句翻譯沒有結果檔，用 has_result 判斷的話「開啟」會不見：{r}")
    assert not r["gone"], f"打得開的那一列不可以同時寫「結果已逾期清除」：{r}"
    assert any(h and h.startswith("/tools/translate-doc/?job=") for h in r["hrefs"]), r
