"""版面在**真的瀏覽器**裡量：欄位寬度、字有沒有被拆成兩行、該看得到的看不看得到。

## 由來（v1.16.6 發版前的截圖目視 ＋ 使用者截圖回報，四件同一天）

| 頁面 | 畫面上的樣子 | 原因 |
|---|---|---|
| 語音服務設定「送出逾時」 | 填「30」的欄位撐成整列寬，說明是黑色正文擠在右邊 | `.form-row input[type=number] { flex: 1 }` 把 `width:110px` 拉長（修法的第一版還寫錯位置，被這支當場抓到）；說明是裸文字 |
| 工作區設定三格、轉逐字稿的人數 | 同上 | 同一個形狀，全站 5 處 |
| 使用者清單「狀態」 | 「● 啟用」被拆成「啟 / 用」兩行 | 表格一寬，那一欄被擠到一個字寬 |
| 語音服務設定頁 / 轉逐字稿工具頁 | jtlw 的全名與專案說明**很難看到 / 根本沒有** | 放在標題列右上角；工具頁沒放 |

**這一類自動化測試一律抓不到** —— 元素都在、沒有 JS 例外、沒有殘留中文。
判準只能落在「量出來的幾何」上：寬度、`getClientRects()` 的行數、可見的外框。

使用者清單那一條**要有資料才量得到**（空清單沒有「狀態」可以被擠）——
之前的截圖跑的都是空的使用者清單，所以一直沒被看到。
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

pytestmark = pytest.mark.skipif(
    _browser() is None or __import__("importlib").util.find_spec("websockets") is None,
    reason="沒有 chromium / websockets —— 這條要真的瀏覽器才驗得到")

#: 量的視窗寬度。截圖目視用 1440 就看得到「啟 / 用」被拆開，這裡取窄一點的
#: 1280 —— 一般筆電的寬度，也比較嚴格。
_WIDTH = 1280
#: 數字欄位的寬度上限（設計值 110px，留一點給瀏覽器的框線與捲動鈕）
_NUM_MAX = 160


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _seed(data: pathlib.Path) -> None:
    # 語音服務設好（不然轉逐字稿工具頁只畫「請先去設定」，連人數欄都沒有）。
    # 位址故意指向沒人接的埠 —— 要的是介面畫出來，不是連得上。
    (data / "jtlw_settings.json").write_text(json.dumps({
        "enabled": True, "base_url": "http://127.0.0.1:1",
        "api_key_enc": "seeded-for-layout-checks",
        "audio_base_url": "http://127.0.0.1:1",
        "profile_id": "meeting.balanced",
        "tasks": ["transcribe", "diarize", "correct"],
        "verify_tls": True, "request_timeout": 30,
    }, ensure_ascii=False), encoding="utf-8")
    # 使用者清單要有人（只跑示範資料的「使用者與群組」那一段，
    # 它會明寫認證關閉 —— 不然有使用者時 fail-secure 會讓整站要登入）
    subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, 'tools'); "
         "import seed_demo_data as s; s.seed_users_and_groups()"],
        cwd=ROOT, env={**os.environ, "JTDT_DATA_DIR": str(data)},
        check=True, capture_output=True, timeout=120)


@pytest.fixture(scope="module")
def live():
    data = tempfile.mkdtemp(prefix="layout-")
    _seed(pathlib.Path(data))
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


def _measure(live, path: str, js: str):
    """開一頁（自己的分頁、固定寬度），等載入完跑一段 JS，回傳它的值。"""
    import websockets.sync.client as wsc

    port, cdp = live
    req = urllib.request.Request(
        f"http://127.0.0.1:{cdp}/json/new?about:blank", method="PUT")
    with urllib.request.urlopen(req, timeout=10) as r:
        tab = json.loads(r.read())
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

            send("Emulation.setDeviceMetricsOverride",
                 {"width": _WIDTH, "height": 900, "deviceScaleFactor": 1,
                  "mobile": False})
            send("Page.enable")
            send("Page.navigate", {"url": f"http://127.0.0.1:{port}{path}"})
            deadline = time.time() + 20
            while time.time() < deadline:
                r = send("Runtime.evaluate", {
                    "expression": "document.readyState", "returnByValue": True})
                if r.get("result", {}).get("result", {}).get("value") == "complete":
                    break
                time.sleep(0.2)
            time.sleep(0.5)       # 讓 DOMContentLoaded 之後的接線跑完
            r = send("Runtime.evaluate", {"expression": js, "returnByValue": True})
            res = r.get("result", {})
            assert "exceptionDetails" not in res, res.get("exceptionDetails")
            return res.get("result", {}).get("value")
    finally:
        try:
            urllib.request.urlopen(
                f"http://127.0.0.1:{cdp}/json/close/{tab['id']}", timeout=5).read()
        except Exception:
            pass


#: **量之前把藏起來的祖先攤開**：轉逐字稿的選項區在上傳之前是 `hidden`，
#: 第一版在那裡量到的寬度是 **0** —— 0 當然小於上限，那一條是空過的。
#: 只動 `hidden` 屬性（同 i18n 逐頁掃描的 `--reveal`），不送出表單、不點按鈕。
_WIDTHS_JS = """(ids => ids.map(id => {
  const el = document.getElementById(id);
  if (!el) return [id, null];
  for (let a = el.closest('[hidden]'); a; a = el.closest('[hidden]')) a.removeAttribute('hidden');
  return [id, Math.round(el.getBoundingClientRect().width)];
}))(%s)"""


@pytest.mark.parametrize("path,ids", [
    ("/admin/jtlw", ["jl-timeout"]),
    ("/admin/workspace", ["ws-quota", "ws-maxfile", "ws-retention"]),
    ("/tools/meeting-transcribe/", ["mtSpk"]),
])
def test_number_fields_are_not_stretched_across_the_row(live, path, ids):
    got = _measure(live, path, _WIDTHS_JS % json.dumps(ids))
    missing = [i for i, w in got if not w]
    assert not missing, (f"{path} 的 {missing} 沒畫出來或寬度是 0 —— "
                         "量到 0 等於沒量，這條會空過")
    wide = [(i, w) for i, w in got if w > _NUM_MAX]
    assert not wide, (
        f"{path} 的數字欄位被撐開了：{wide}（上限 {_NUM_MAX}px）—— 要掛 `field-num`，"
        "不要各自寫寬度（`.form-row input[type=number] {{ flex: 1 }}` 會把它拉長）")


def test_the_timeout_explanation_is_a_hint_not_loose_text(live):
    """說明要在欄位下方的說明行，不可以是接在輸入框後面的裸文字。"""
    got = _measure(live, "/admin/jtlw", """(() => {
      const row = document.getElementById('jl-timeout').closest('.form-row');
      const loose = [...row.childNodes].filter(n => n.nodeType === 3 && n.textContent.trim());
      const hint = row.nextElementSibling;
      return {loose: loose.map(n => n.textContent.trim()),
              hint: hint && hint.classList.contains('jl-hint') ? hint.textContent.trim() : null};
    })()""")
    assert not got["loose"], f"輸入框旁邊還有裸文字：{got['loose']}"
    assert got["hint"], "「送出逾時」下方沒有說明行"


@pytest.mark.parametrize("sel", [".uf-state", ".role-chip"])
def test_user_list_badges_are_not_split_across_lines(live, sel):
    """「● 啟用」與角色徽章不可以被拆成兩行 —— `getClientRects()` 一行就是一個框。

    **兩個要一起驗**：第一版只修了狀態欄，擠的空間就換到隔壁的角色欄
    （「法務資安」→「法務資 / 安」）—— 重拍截圖才看到。
    """
    got = _measure(live, "/admin/users", """(() =>
      [...document.querySelectorAll('%s')].map(e => {
        // inline-block 的徽章自己只有一個框；要看的是**裡面的文字**折成幾行
        const r = document.createRange(); r.selectNodeContents(e);
        const tops = new Set([...r.getClientRects()].map(x => Math.round(x.top)));
        return [e.textContent.trim(), tops.size];
      })
    )()""" % sel)
    assert len(got) >= 3, f"使用者清單只有 {len(got)} 個 {sel} —— 示範資料沒種進去，這條等於沒驗"
    split = [t for t, lines in got if lines != 1]
    assert not split, f"{sel} 被拆成多行：{split}"


@pytest.mark.parametrize("path", ["/admin/jtlw", "/tools/meeting-transcribe/"])
def test_jtlw_full_name_is_where_people_read(live, path):
    """jtlw 的全名與專案說明要**接在說明那一行**，而且真的看得到。

    原本放在標題列右上角：等寬小字、不在眼睛從標題往下讀的路徑上；
    工具頁更是完全沒有 —— 使用者只看得到「語音服務（jtlw）」。
    """
    got = _measure(live, path, """(() => {
      const n = document.querySelector('.jtlw-name');
      if (!n) return null;
      const r = n.getBoundingClientRect();
      const a = n.querySelector('a');
      return {w: r.width, h: r.height, inDesc: !!n.closest('p.muted'),
              text: n.textContent, href: a ? a.getAttribute('href') : null,
              oldCorner: !!document.querySelector('.jl-fullname')};
    })()""")
    assert got, f"{path} 沒有 jtlw 的全名"
    assert got["w"] > 0 and got["h"] > 0, f"{path} 的全名畫在畫面上但沒有佔到空間"
    assert got["inDesc"], f"{path} 的全名不在說明那一行"
    assert "jt-live-whisper" in got["text"]
    assert got["href"] == "https://jasoncheng7115.github.io/jt-live-whisper/"
    assert not got["oldCorner"], "右上角那一份還在 —— 同一句話兩份會漂"


def _luminance(rgb: str) -> float:
    import re as _re
    r, g, b = (int(x) / 255 for x in _re.findall(r"\d+", rgb)[:3])
    f = lambda c: c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4  # noqa: E731
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)


@pytest.mark.parametrize("path,sel", [
    ("/tools/meeting-summary/", "#msPasteBox"),
    # **要指定欄位**：第一版寫 `input[placeholder]`，對到的是側欄的搜尋框
    # （紫底白字、本來就很亮）—— 把全站那條規則拿掉照樣會過，是空的。
    ("/admin/jtlw", "#jl-audio"),
])
def test_placeholder_text_is_lighter_than_the_browser_default(live, path, sel):
    """輸入格裡的範例文字要比瀏覽器預設（`#757575`）淡，一眼看得出「還沒填」。

    多行的範例（會議摘要的貼上框、會議背景）用瀏覽器預設的灰，看起來跟已經填好的
    內容差不多（2026-09-23 使用者要求再淡一點）。**判準用亮度不寫死色碼** ——
    之後誰把顏色微調一點都不會誤報，改回預設或改深才會紅。
    """
    got = _measure(live, path, """(() => {
      const el = document.querySelector(%s);
      return el ? getComputedStyle(el, '::placeholder').color : null;
    })()""" % json.dumps(sel))
    assert got, f"{path} 找不到 {sel} —— 這條等於沒驗"
    assert _luminance(got) > _luminance("rgb(117, 117, 117)") * 1.3, (
        f"{path} 的範例文字是 {got}，跟瀏覽器預設（#757575）差不多或更深")
