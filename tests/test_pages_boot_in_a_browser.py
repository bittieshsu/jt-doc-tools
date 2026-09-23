"""每一頁都要在**真的瀏覽器**裡開得起來，而且主控台不可以有錯誤。

## 為什麼非有這一支不可（v1.15.36，使用者回報）

「掃描修正」上線一版之後，使用者拉檔案進去沒反應、點選檔案也沒反應。
根因是那支模板漏了兩行 `<script src>`，`new FileUpload(...)` 在行內腳本的
第一行丟 `ReferenceError`，**整段腳本停在那裡** —— 上傳沒接線、選項面板不會
出現、作業進度也不會動。

**當時每一關都是綠的**：

| 關卡 | 為什麼看不到 |
|---|---|
| `pytest`（6800+ 支）| 沒有任何一支測試會把工具頁「開起來跑 JS」 |
| `test_template_js_syntax`（`node --check`）| 漏載腳本**語法完全合法** |
| 全站截圖逐張目視 | **畫面長得完全正常** —— 上傳區是 `<label for>` 包
  `<input type=file>`，純 HTML 就點得開檔案選擇器 |
| 端點測試 | 頁面回 200，API 也都好好的 |

也就是說：**「頁面渲染得出來」跟「頁面活著」是兩件事**，而我們從來只驗前者。

這一類還有一整個家族本專案都踩過：CSP 擋掉動態注入的 `<style>`（沒有 JS 例外、
元件變成無樣式的 DOM）、`tr` 被同名變數遮蔽、樣板把程式碼當文字印出來、
id 撞名讓 `getElementById` 拿到別的元素。**它們的共同點是「畫面看起來正常」**，
只有真的跑一次 JS 才看得到。

## 判準

逐頁開起來，收兩種訊號：

1. `Runtime.exceptionThrown` —— 沒被接住的例外（`ReferenceError` 就是這種）
2. `Log.entryAdded` 裡 level=error 的 —— 含 **CSP 違規**與載入失敗的資源
   （CSP 違規只進主控台，不會有 JS 例外）

**任何一頁有任何一筆就算失敗。** 這條刻意嚴格：主控台有錯誤代表那一頁
有一段程式碼沒跑到，而「沒跑到的是哪一段」永遠只有使用者會發現。

沒有瀏覽器的環境**誠實 skip**（不是 pass）——並且另外有一條驗「真的走過
每一頁」，否則「掃 0 頁」跟「每一頁都乾淨」在 pytest 輸出裡一模一樣。
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

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, str(ROOT))
from tools.browser_probe import browser as _browser  # noqa: E402




def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


pytestmark = pytest.mark.skipif(
    _browser() is None or __import__("importlib").util.find_spec("websockets") is None,
    reason="沒有 chromium / websockets —— 這條要真的瀏覽器才驗得到")


def _seed_setup_gated_tools(data: "pathlib.Path") -> None:
    """**把需要外部設定的工具先設定好** —— 不然這一關永遠看不到它們的真正介面。

    `requires_setup` 的工具（目前是「會議錄音轉逐字稿」）在沒設定時只渲染
    一句「請先去設定」，連上傳區都不畫。所以這支掃描器在**沒有設定**的
    拋棄式實例上，從頭到尾掃的都是那個空殼 —— 而真正的那一份
    **從來沒有在瀏覽器裡開過**。

    2026-09-22 就這樣漏掉一個：進度區只放了一個空的 `<div>`，
    `JobProgress` 在 `null.addEventListener` 丟 `TypeError`，
    整段行內腳本停住、「開始」按鈕沒有被接上事件 —— **按了完全沒反應**，
    而這一關全綠（它看到的是空殼）。v1.15.36 的「文件擺正」是一模一樣的錯。

    這裡只要讓 `is_configured()` 成立就好（它只看有沒有值），
    位址故意指向一個不會有人接的埠 —— 我們要的是**介面畫出來**，
    不是真的連得上。
    """
    (data / "jtlw_settings.json").write_text(json.dumps({
        "enabled": True, "base_url": "http://127.0.0.1:1",
        "api_key_enc": "seeded-for-the-page-boot-sweep",
        "audio_base_url": "http://127.0.0.1:1",
        "profile_id": "meeting.balanced",
        "tasks": ["transcribe", "diarize", "correct"],
        "verify_tls": True, "request_timeout": 30,
    }, ensure_ascii=False), encoding="utf-8")


@pytest.fixture(scope="module")
def live():
    """跑一個拋棄式實例（auth 關閉、獨立資料目錄）＋ 一個無頭瀏覽器。"""
    data = tempfile.mkdtemp(prefix="pageboot-")
    _seed_setup_gated_tools(pathlib.Path(data))
    port, cdp = _free_port(), _free_port()
    env = {**os.environ, "JTDT_DATA_DIR": data, "JTDT_CSRF_DISABLE": "1"}
    srv = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    br = subprocess.Popen(
        [_browser(), "--headless=new", "--no-sandbox", "--disable-gpu",
         f"--remote-debugging-port={cdp}", "--remote-allow-origins=*",
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
        yield port, cdp
    finally:
        br.terminate(); srv.terminate()
        try:
            br.wait(timeout=5); srv.wait(timeout=5)
        except Exception:
            br.kill(); srv.kill()
        shutil.rmtree(data, ignore_errors=True)


def _paths() -> list[str]:
    sys.path.insert(0, ROOT)
    from app.tool_registry import discover_tools
    out = ["/", "/my-jobs", "/workspace"]
    out += [f"/tools/{t.metadata.id}/" for t in discover_tools()]
    return out


PATHS = _paths()


def _visit(cdp: int, url: str) -> list[str]:
    """開一頁，回傳主控台上的錯誤（例外 + error 等級的記錄）。

    **每一頁用自己的分頁。** 第一版所有頁共用同一個分頁，於是前一頁的事件會
    漏到下一頁的收集視窗裡 —— 症狀就是本專案很熟的那個：**單跑全綠、合跑失敗，
    而且失敗的是哪一頁還會變**。看到這個指紋就先懷疑共用狀態。
    """
    import websockets.sync.client as wsc

    # 開一個全新的分頁（用完關掉），完全隔離
    req = urllib.request.Request(
        f"http://127.0.0.1:{cdp}/json/new?about:blank", method="PUT")
    with urllib.request.urlopen(req, timeout=10) as r:   # 新版 Chrome 只收 PUT
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
                    m = json.loads(ws.recv())
                    if m.get("id") == n[0]:
                        return m
                    _collect(m, errs)

            send("Runtime.enable")
            send("Log.enable")
            send("Page.enable")
            send("Page.navigate", {"url": url})

            # 等到 load 事件，再多給一點時間讓 DOMContentLoaded 之後才跑的
            # 程式碼（全站的側欄接線、各工具的 IIFE）把事情做完。
            loaded = False
            deadline = time.time() + 20
            while time.time() < deadline:
                try:
                    m = json.loads(ws.recv(timeout=1))  # type: ignore[call-arg]
                except TimeoutError:
                    if loaded:
                        break
                    continue
                except Exception:
                    break
                _collect(m, errs)
                if m.get("method") == "Page.loadEventFired":
                    loaded = True
                    deadline = min(deadline, time.time() + 1.5)
            assert loaded or errs, f"{url} 在 20 秒內沒有觸發 load 事件"
    finally:
        try:
            urllib.request.urlopen(
                f"http://127.0.0.1:{cdp}/json/close/{tab['id']}", timeout=5).read()
        except Exception:
            pass
    return errs


def _collect(msg: dict, errs: list[str]) -> None:
    method = msg.get("method")
    if method == "Runtime.exceptionThrown":
        d = msg["params"]["exceptionDetails"]
        errs.append("例外：" + (d.get("exception", {}).get("description")
                              or d.get("text", "?"))[:200])
    elif method == "Log.entryAdded":
        e = msg["params"]["entry"]
        if e.get("level") == "error":
            errs.append(f"主控台：{e.get('text', '')[:200]}")


# 用一個模組層級的清單記錄真的走過幾頁 —— 「掃 0 頁」跟「都乾淨」
# 在 pytest 輸出裡長得一模一樣。
_VISITED: list[str] = []


@pytest.mark.parametrize("path", PATHS)
def test_page_has_no_console_errors(live, path):
    port, cdp = live
    errs = _visit(cdp, f"http://127.0.0.1:{port}{path}")
    _VISITED.append(path)
    assert not errs, (
        f"{path} 的主控台有錯誤：\n  " + "\n  ".join(errs)
        + "\n主控台有錯誤＝那一頁有一段程式碼沒跑到。**畫面可能看起來完全正常**"
          "（掃描修正那次就是：上傳區點得開檔案選擇器，選完卻什麼都沒發生）。")


def test_the_sweep_actually_opened_every_page(live):
    """檢查自己要有牙齒：確認真的逐頁走過，不是一頁都沒開。"""
    assert len(PATHS) > 40, f"只列出 {len(PATHS)} 頁，比對基準本身就不對"
    assert len(_VISITED) >= len(PATHS), (
        f"只走過 {len(_VISITED)} 頁，應該要有 {len(PATHS)} 頁")


def test_the_setup_gated_tools_really_show_their_real_ui(live):
    """**先證明種子有效** —— 沒有這一條，`_seed_setup_gated_tools` 哪天失效
    （設定檔名改了、判準多一項）就會靜靜地退回掃空殼，
    而上面那一輪照樣全綠（「掃 0 個檔」跟「掃過都乾淨」長得一樣，第 N 次）。

    判準是**那個介面真的被畫出來**（上傳區與進度區的標記），
    不是「頁面回 200」—— 未設定的版本也是 200。
    """
    port, _ = live
    html = urllib.request.urlopen(
        f"http://127.0.0.1:{port}/tools/meeting-transcribe/", timeout=10).read().decode()
    assert 'id="mtUp"' in html, (
        "「會議錄音轉逐字稿」還是渲染成「請先去設定」的空殼 —— "
        "種子沒生效，這一關等於沒掃到它真正的介面")
    assert "job-bar-inner" in html and "job-reset" in html, (
        "進度區沒有共用元件的標記 —— `JobProgress` 會在 "
        "`null.addEventListener` 丟 TypeError，而按鈕就此接不上")
