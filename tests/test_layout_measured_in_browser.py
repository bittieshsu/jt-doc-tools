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


def _measure(live, path, js: str, width: int = _WIDTH, locale: str | None = None,
             mobile: bool | None = None):
    """開一頁（自己的分頁、固定寬度），等載入完跑一段 JS，回傳它的值。

    `path` 給一串的話，同一個分頁依序開每一頁、回傳 `{路徑: 值}`（掃整批頁面時用，
    不然每一頁開一個分頁太慢）。`locale` 設介面語言的 cookie。
    `mobile` 預設照寬度判斷；量**沒有 viewport 宣告的頁面**（例如主題預覽）時要給 False ——
    行動模式下那種頁面的版面寬度是 980px，量到的不是框裡看到的樣子。"""
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
                 {"width": width, "height": 900, "deviceScaleFactor": 1,
                  "mobile": (width < 600) if mobile is None else mobile})
            send("Page.enable")
            if locale:
                send("Network.setCookie", {"name": "jtdt_locale", "value": locale,
                                           "domain": "127.0.0.1", "path": "/"})

            def one(p):
                send("Page.navigate", {"url": f"http://127.0.0.1:{port}{p}"})
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
                assert "exceptionDetails" not in res, (p, res.get("exceptionDetails"))
                return res.get("result", {}).get("value")

            if isinstance(path, str):
                return one(path)
            return {p: one(p) for p in path}
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


_CTX_JS = """(() => {
  const d = document.getElementById('msCtxWrap');
  if (!d) return null;
  d.open = true;
  const cs = getComputedStyle(d);
  const card = d.closest('.panel') || d.parentElement;
  const sum = d.querySelector('summary');
  const txt = [...sum.childNodes].filter(n => n.nodeType === 3 && n.textContent.trim()).pop();
  const rg = document.createRange(); rg.selectNodeContents(txt);
  const ta = document.getElementById('msCtxBox').getBoundingClientRect();
  const p = d.querySelector('p').getBoundingClientRect();
  const r = d.getBoundingClientRect();
  return {bg: cs.backgroundColor, cardBg: getComputedStyle(card).backgroundColor,
          left: parseFloat(cs.borderLeftWidth), top: parseFloat(cs.borderTopWidth),
          right: parseFloat(cs.borderRightWidth), bottom: parseFloat(cs.borderBottomWidth),
          shadow: cs.boxShadow, textLeft: rg.getBoundingClientRect().left,
          ta: [ta.left, ta.right, ta.top, ta.bottom], p: [p.left, p.right],
          box: [r.left, r.right, r.top, r.bottom], padR: parseFloat(cs.paddingRight)};
})()"""


def test_meeting_context_reads_as_one_group_not_a_card(live):
    """會議背景（選填）的標題、說明、輸入框要看得出是**一組**（使用者 2026-10-02），
    但**不可以變成卡片裡的另一張卡片**（使用者 2026-09-19：四邊框讓它看起來不在
    「1. 上傳逐字稿」裡面）。兩次回報要同時成立，所以判準兩邊都量：

    * 有自己的底色（跟所在卡片不同）＋ 左邊一條色線 —— 分組的訊號；
    * 另外三邊沒有框線、沒有陰影 —— 那兩樣是「卡片」的訊號；
    * 說明與輸入框都在這一塊**裡面**，輸入框左緣對齊標題文字（看得出是標題底下的內容），
      而且**不超出右緣**（寬度 100% 加上左縮排會撐出去）。
    """
    g = _measure(live, "/tools/meeting-summary/", _CTX_JS)
    assert g, "找不到會議背景那一區 —— 這條等於沒驗"
    assert g["bg"] not in ("rgba(0, 0, 0, 0)", "transparent"), "沒有底色，看不出是一組"
    assert g["bg"] != g["cardBg"], f"底色跟所在卡片一樣（{g['bg']}），看不出是一組"
    assert g["left"] >= 2, "左邊沒有色線"
    assert g["top"] == g["right"] == g["bottom"] == 0, (
        "畫了四邊框 —— 會變成「卡片裡又一張卡片」（2026-09-19 回報過）")
    assert g["shadow"] in ("none", ""), "加了陰影 —— 那是卡片的訊號"
    bl, br, bt, bb = g["box"]
    tl, tr, tt, tb = g["ta"]
    assert bl <= g["p"][0] and g["p"][1] <= br, "說明跑到這一塊外面"
    assert bl < tl and tr <= br - g["padR"] + 1 and bt < tt and tb < bb, (
        f"輸入框不在這一塊裡面：框 {g['box']}、輸入框 {g['ta']}")
    assert abs(tl - g["textLeft"]) <= 2, (
        f"輸入框左緣 {tl:.0f} 沒對齊標題文字 {g['textLeft']:.0f}")


def test_meeting_context_on_a_phone_uses_the_full_width(live):
    """窄螢幕不縮排：輸入框要撐到這一塊的右緣 —— 縮排與寬度若各自覆寫，
    寫在後面的輸入框規則會把寬度蓋回去（第一版就是這樣，手機上窄了 39px）。"""
    g = _measure(live, "/tools/meeting-summary/", _CTX_JS, width=390)
    assert g, "找不到會議背景那一區 —— 這條等於沒驗"
    bl, br, _, _ = g["box"]
    tl, tr, _, _ = g["ta"]
    assert tr <= br - g["padR"] + 1, f"輸入框超出右緣：框 {g['box']}、輸入框 {g['ta']}"
    assert br - g["padR"] - tr <= 2, f"手機上輸入框沒有撐滿（右邊空了 {br - g['padR'] - tr:.0f}px）"
    assert tl - bl <= 20, f"手機上還在縮排（左邊空了 {tl - bl:.0f}px）"


#: 「結果」卡片跟它上面那一張卡片之間的距離。量的時候把結果卡片攤開、進度列維持藏著
#: （還沒送出、或從「我的作業」打開時的樣子）。
_GAP_JS = """(id => {
  const b = document.getElementById(id);
  if (!b) return null;
  let a = b.previousElementSibling;
  while (a && !a.classList.contains('panel')) a = a.previousElementSibling;
  if (!a) return null;
  // 兩張都攤開（轉逐字稿的選項卡片在上傳之前也是藏著的，量到 0 就等於沒量）
  a.removeAttribute('hidden');
  b.removeAttribute('hidden');
  document.querySelectorAll('.job-progress').forEach(j => j.setAttribute('hidden', ''));
  const ra = a.getBoundingClientRect(), rb = b.getBoundingClientRect();
  if (!ra.height || !rb.height) return null;
  return Math.round(rb.top - ra.bottom);
})(%s)"""


@pytest.mark.parametrize("path,result_id", [
    ("/tools/meeting-summary/", "msResult"),
    ("/tools/meeting-transcribe/", "mtResult"),
    ("/tools/doc-translate/", "dtResult"),
])
def test_the_result_card_is_not_glued_to_the_card_above(live, path, result_id):
    """「分析結果」卡片跟上一張卡片之間要有間距（2026-10-02 使用者截圖回報「太近了」）。

    會議摘要的進度列夾在兩張卡片**中間**，`.panel + .panel` 那條接不到，進度列藏著時
    兩張卡片就貼在一起；另外兩支的進度列在第一張卡片**裡面**，本來就沒事 ——
    三支一起量，以後誰把進度列搬出來也會被抓到。"""
    gap = _measure(live, path, _GAP_JS % json.dumps(result_id))
    assert gap is not None, f"{path} 找不到結果卡片或它上面那一張 —— 這條等於沒驗"
    assert gap >= 12, f"{path} 結果卡片跟上一張卡片只隔 {gap}px"


# ---------------------------------------------------------------- 欄位標題不可以伸進輸入框
# 2026-10-03 使用者截圖：轉逐字稿的「專有名詞或會議背景」九個字，超過中文欄位標題的 96px
# （`platform.css` 的 `.form-row label` 不換行），字伸到右邊的輸入框上。中文的寬度是照四到六個字
# 定的，**之後誰把標題寫長一點就會再發生一次** —— 所以全站工具頁一起量，不只量這一頁。
#
# 判準是「標題的字（`scrollWidth`）有沒有伸進**同一列**下一個元件的範圍」，不是單看
# `scrollWidth > clientWidth`：勾選框整個包在 label 裡、後面沒有別的元件的那種，字超出
# 標題欄也沒有蓋到東西（掃全站時有三處是這樣，畫面上看不出問題）。
# 攤開藏起來的區塊時**只攤開那一列的祖先**，不動列裡面的東西 —— 轉逐字稿的人數標題裡
# 有兩段互斥的文字（一段藏著），全部攤開的話兩段會接在一起、量出一個不存在的溢出。

_LABEL_OVERLAP_JS = r"""(() => {
  const out = {measured: 0, bad: [], lang: document.documentElement.lang};
  document.querySelectorAll('.form-row').forEach(row => {
    for (let a = row.closest('[hidden]'); a; a = row.closest('[hidden]')) a.removeAttribute('hidden');
    for (let d = row.closest('details:not([open])'); d; d = row.closest('details:not([open])')) d.open = true;
  });
  document.querySelectorAll('.form-row > label').forEach(l => {
    const r = l.getBoundingClientRect();
    const n = l.nextElementSibling;
    if (!r.width || !n) return;
    const nr = n.getBoundingClientRect();
    if (!nr.width || nr.top >= r.bottom - 1 || nr.bottom <= r.top + 1) return;   // 不在同一列
    out.measured++;
    const over = Math.round(r.left + l.scrollWidth - nr.left);
    if (over > 1) out.bad.push([l.textContent.trim().slice(0, 30), over]);
  });
  return out;
})()"""


def _tool_pages() -> list[str]:
    from app.tool_registry import discover_tools
    return sorted(f"/tools/{t.metadata.id}/" for t in discover_tools())


def test_no_field_label_runs_into_its_field_on_any_tool_page(live):
    """中文介面（標題欄寬度固定、不換行的那一個）逐頁量全站工具頁。"""
    got = _measure(live, _tool_pages(), _LABEL_OVERLAP_JS)
    measured = sum(v["measured"] for v in got.values() if v)
    # 「量了 0 個」跟「全部合格」在輸出裡長得一樣 —— 2026-10-03 實量 50 頁 48 個，取一半當下限
    assert measured >= 24, f"只量到 {measured} 個欄位標題，掃描本身可能壞了"
    bad = {p: v["bad"] for p, v in got.items() if v and v["bad"]}
    assert not bad, f"欄位標題的字伸進了旁邊的輸入框（溢出 px）：{bad}"


@pytest.mark.parametrize("locale", ["zh-Hant", "en", "ja"])
def test_the_terms_label_stays_out_of_the_textarea(live, locale):
    """轉逐字稿的「專有名詞或會議背景」三種語言都要量 —— 英日文的標題欄比較寬、會換行，
    中文的不換行，三種的壞法不一樣。"""
    got = _measure(live, "/tools/meeting-transcribe/", _LABEL_OVERLAP_JS, locale=locale)
    assert got["lang"].startswith(locale.split("-")[0]), (
        f"介面語言沒有切過去（{got['lang']}）—— 這一條等於量了中文")
    assert got["measured"] >= 3, f"轉逐字稿只量到 {got['measured']} 個欄位標題"
    assert not got["bad"], f"{locale}：欄位標題的字伸進了輸入框：{got['bad']}"


_SWATCH_JS = """(function(){
  var t = function (d) { var c = getComputedStyle(d).backgroundColor;
                         return c && c !== 'rgba(0, 0, 0, 0)' && c !== 'transparent'; };
  return Array.from(document.querySelectorAll('.md2-theme')).map(function (card) {
    var dots = Array.from(card.querySelectorAll('.md2-sw > i'));
    var r = dots.length ? dots[0].getBoundingClientRect() : {width: 0, height: 0};
    return {id: card.dataset.theme, n: dots.length, colored: dots.filter(t).length,
            w: Math.round(r.width), h: Math.round(r.height)};
  });
})()"""


def test_markdown_theme_cards_show_their_colours(live):
    """「Markdown 轉辦公文件」每張主題卡片前面一排三個色票（2026-10-03 加主題時一起做）。

    **顏色要真的畫出來**：色票走 CSSOM 設 —— 寫成行內 `style` 屬性會被 CSP 丟掉，
    色塊還在、大小也對，只是透明的。所以判準是算出來的底色，不是元素在不在。"""
    from app.tools.markdown_to_doc import themes
    got = _measure(live, "/tools/markdown-to-doc/", _SWATCH_JS)
    assert [c["id"] for c in got] == list(themes.THEMES), got
    for c in got:
        assert c["n"] == 3 and c["colored"] == 3, f"{c['id']} 的色票沒畫出來：{c}"
        assert c["w"] >= 10 and c["h"] >= 10, f"{c['id']} 的色票太小：{c}"


#: 主題預覽：每個元素都要落在預覽框裡（`html` 是 `overflow:hidden`，伸出去的部分直接被切掉）。
#: 跟 `html` 的框比，兩邊在同一個座標系 —— 預覽有 `zoom`，拿視窗寬度比會差一個倍率。
_PREVIEW_CLIP_JS = """(function(){
  var H = document.documentElement.getBoundingClientRect(), bad = [], n = 0;
  Array.from(document.body.querySelectorAll('*')).forEach(function (e) {
    var r = e.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) return;
    n++;
    if (r.left < H.left - 1 || r.right > H.right + 1)
      bad.push(e.tagName + ' ' + Math.round(r.left - H.left) + '..' + Math.round(r.right - H.left)
               + ' / ' + Math.round(H.width) + ' ' + (e.textContent || '').trim().slice(0, 12));
  });
  return {n: n, bad: bad.slice(0, 5), total: bad.length};
})()"""


def test_theme_previews_keep_everything_inside_the_frame(live):
    """會議摘要的版面主題預覽（210 寬的縮圖）不可以有東西伸出框外被切掉。

    商務報告的標題色帶寫的是「往左右各伸 `PAGE_MARGIN_X` 到頁邊」—— 在文件裡那是頁邊距，
    預覽沒有頁邊距、只有 body 的內距，照原樣伸出去就把標題的第一個字切掉了
    （2026-10-04 使用者截圖回報）。**判準是畫面上的框**，不是 CSS 寫了什麼。"""
    from app.tools.markdown_to_doc import themes
    paths = [f"/tools/meeting-summary/theme-preview/{t}" for t in themes.THEMES]
    got = _measure(live, paths, _PREVIEW_CLIP_JS, width=210, mobile=False)
    for p, g in got.items():
        assert g and g["n"] >= 20, f"{p} 只量到 {g and g['n']} 個元素 —— 預覽沒有畫出來"
        assert not g["total"], f"{p} 有 {g['total']} 個元素伸出預覽框：{g['bad']}"
