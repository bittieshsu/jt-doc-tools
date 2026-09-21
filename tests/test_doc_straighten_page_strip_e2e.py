"""掃描修正：**多頁 / 多檔的每一頁都要看得到**，而且模式切回去要真的切回去。

兩件使用者 2026-09-14 回報的事：

1. 「如果拉多個檔 或多頁 都要有預覽」 —— 原本只有一個**數字輸入框**：
   要用打字的，看不到有哪些頁、看不出哪幾頁調過，多檔併成一份之後
   更分不出哪一頁是哪個檔來的。
2. 「我切自己拉 但是按回自動抓時 沒有回到自動抓的修正後畫面」
   「範圍也沒有回到自動抓的範圍」 —— 切模式只呼叫 `renderQuad()`
   （那支只管疊圖的顯示），**沒有重跑預覽**；而且 `overrides[頁].quad`
   還留著，所以就算重跑也還是拿使用者拉的那組座標。

第 2 點的修法是「**模式是開關不是刪除鍵**」：自動模式下不把使用者拉的
座標送出去（預覽與送出都要濾），但**座標留著** —— 切回手動時他拉好的點
還在，不會白拉一次。

判準都放在**畫面上真的看得到的東西**：縮圖的格數、檔案分隔、
以及「切回自動之後的狀態列跟第一次自動算出來的一模一樣」。
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from tools.browser_probe import (  # noqa: E402
    browser as _browser,
    uploadable_dir as _uploadable_dir,
)






def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


pytestmark = pytest.mark.skipif(
    _browser() is None
    or __import__("importlib").util.find_spec("websockets") is None
    or __import__("importlib").util.find_spec("cv2") is None,
    reason="沒有 chromium / websockets / OpenCV —— 這條要真的瀏覽器才驗得到")


def _material() -> tuple[str, str]:
    """一份兩頁的 PDF ＋ 一張歪的照片 —— **兩個檔案、共三頁**。"""
    import cv2
    import fitz
    import numpy as np

    d = _uploadable_dir()
    pdf = os.path.join(d, "strip_two_pages.pdf")
    doc = fitz.open()
    for i in range(2):
        pg = doc.new_page(width=595, height=842)
        pg.insert_text((72, 120), f"Doc page {i + 1}", fontsize=20, fontname="helv")
    doc.save(pdf)
    doc.close()

    png = os.path.join(d, "strip_photo.png")
    img = np.full((900, 1200, 3), (40, 60, 110), np.uint8)
    sheet = np.full((640, 480, 3), 245, np.uint8)
    cv2.putText(sheet, "Photo sheet", (40, 200), cv2.FONT_HERSHEY_SIMPLEX,
                1.2, (30, 30, 30), 2)
    m = cv2.getRotationMatrix2D((240, 320), 9, 1.0)
    m[0, 2] += 360
    m[1, 2] += 130
    img = cv2.warpAffine(sheet, m, (1200, 900), dst=img,
                         borderMode=cv2.BORDER_TRANSPARENT)
    cv2.imwrite(png, img)
    return pdf, png


@pytest.fixture(scope="module")
def page():
    data = tempfile.mkdtemp(prefix="dsstrip-")
    port, cdp = _free_port(), _free_port()
    env = {**os.environ, "JTDT_DATA_DIR": data, "JTDT_CSRF_DISABLE": "1"}
    srv = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
         "--port", str(port), "--log-level", "warning"],
        cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    br = subprocess.Popen(
        [_browser(), "--headless=new", "--no-sandbox", "--disable-gpu",
         f"--remote-debugging-port={cdp}", "--remote-allow-origins=*", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    ws = None
    try:
        for _ in range(160):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1)
                urllib.request.urlopen(f"http://127.0.0.1:{cdp}/json/version", timeout=1)
                break
            except Exception:
                time.sleep(0.5)
        else:
            pytest.skip("實例或瀏覽器起不來")

        import websockets.sync.client as wsc
        req = urllib.request.Request(
            f"http://127.0.0.1:{cdp}/json/new?about:blank", method="PUT")
        with urllib.request.urlopen(req, timeout=10) as r:
            tab = json.loads(r.read())
        ws = wsc.connect(tab["webSocketDebuggerUrl"], max_size=None, open_timeout=10)
        n = [0]

        def send(method, params=None):
            n[0] += 1
            i = n[0]
            ws.send(json.dumps({"id": i, "method": method, "params": params or {}}))
            while True:
                m = json.loads(ws.recv())
                if m.get("id") == i:
                    return m

        def ev(expr):
            res = send("Runtime.evaluate",
                       {"expression": expr, "returnByValue": True})["result"]
            if "exceptionDetails" in res:
                exc = res["exceptionDetails"].get("exception", {}).get("description")
                raise AssertionError(f"頁面丟例外：{str(exc)[:300]}")
            return res.get("result", {}).get("value")

        pdf, png = _material()
        send("Runtime.enable")
        send("Page.enable")
        send("DOM.enable")
        send("Page.navigate",
             {"url": f"http://127.0.0.1:{port}/tools/doc-straighten/"})
        time.sleep(4)
        doc = send("DOM.getDocument", {"depth": -1})
        nid = send("DOM.querySelector",
                   {"nodeId": doc["result"]["root"]["nodeId"],
                    "selector": "input[type=file]"})["result"].get("nodeId")
        if not nid:
            pytest.skip("找不到上傳欄位")
        send("DOM.setFileInputFiles", {"files": [pdf, png], "nodeId": nid})
        for _ in range(60):
            time.sleep(1)
            if ev("(()=>{const s=document.getElementById('dsStrip');"
                  "return !!s && !s.hidden})()"):
                break
        else:
            pytest.skip("縮圖列沒出來（這台機器太慢？）")
        yield ev
    finally:
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
        br.terminate(); srv.terminate()
        for p in (br, srv):
            try:
                p.wait(timeout=6)
            except Exception:
                p.kill()
        shutil.rmtree(data, ignore_errors=True)


def _settled(ev, want_page: str | None = None) -> str:
    """等預覽算完 —— 判準是狀態列出現**殘留角**（那是這支工具的驗收指標）。"""
    for _ in range(45):
        time.sleep(1)
        s = ev("document.getElementById('dsPvStat').textContent") or ""
        if "殘留" in s and (want_page is None
                            or ev("document.getElementById('dsPvPage').value") == want_page):
            return s
    raise AssertionError(f"預覽一直沒算完（狀態列：{s!r}）")


def test_every_page_of_every_file_gets_a_thumbnail(page):
    """兩個檔案、共三頁 → 縮圖列要有**三格**。"""
    ev = page
    assert ev("document.querySelectorAll('#dsStrip .ds-pg').length") == 3


def test_each_source_file_is_marked_in_the_strip(page):
    """多檔併成一份之後，**哪一頁是哪個檔來的從 PDF 本身看不出來**。

    所以上傳的回應要帶 `sources`，縮圖列才標得出每個檔案的起點。
    """
    ev = page
    assert ev("document.querySelectorAll('#dsStrip .ds-pg.is-file-start').length") == 2
    assert ev("document.querySelectorAll('#dsStrip .ds-pg')[2].title") == "strip_photo.png"
    assert ev("document.querySelectorAll('#dsStrip .ds-pg')[0].title") \
        == "strip_two_pages.pdf"


def test_clicking_a_thumbnail_previews_that_page(page):
    """點縮圖就要切過去並重算 —— 不是只把數字框改掉。"""
    ev = page
    _settled(ev, "1")
    assert ev("(()=>{const b=document.querySelector('#dsStrip .ds-pg.is-on');"
              "return b ? b.dataset.page : null})()") == "1"
    ev("document.querySelectorAll('#dsStrip .ds-pg')[2].click()")
    _settled(ev, "3")
    assert ev("(()=>{const b=document.querySelector('#dsStrip .ds-pg.is-on');"
              "return b ? b.dataset.page : null})()") == "3", "縮圖列沒跟著換頁"


def test_switching_back_to_auto_really_goes_back_to_the_auto_result(page):
    """**這條是使用者回報的那個 bug。**

    切到手動、拉四個角、再切回自動 → 右邊的結果與四邊形都要回到自動那一版。
    判準是「狀態列跟第一次自動算出來的一模一樣」——
    只驗「有沒有重算」的話，拿手動座標重算一次也會過。
    """
    ev = page
    ev("document.querySelectorAll('#dsStrip .ds-pg')[2].click()")
    auto = _settled(ev, "3")
    assert "自己拉的四個角" not in auto, "一開始就不該是手動的結果"

    ev("document.getElementById('dsQuadEdit').click()")
    time.sleep(2.5)
    moved = ev("""(()=>{
      const ps=[[0.05,0.05],[0.95,0.08],[0.93,0.92],[0.07,0.90]];
      const hs=document.querySelectorAll('#dsHandles .ds-h');
      const art=document.getElementById('dsArt').getBoundingClientRect();
      hs.forEach((h,i)=>{
        const x=art.left+ps[i][0]*art.width, y=art.top+ps[i][1]*art.height;
        h.dispatchEvent(new PointerEvent('pointerdown',{clientX:x,clientY:y,bubbles:true}));
        document.dispatchEvent(new PointerEvent('pointermove',{clientX:x,clientY:y,bubbles:true}));
        document.dispatchEvent(new PointerEvent('pointerup',{clientX:x,clientY:y,bubbles:true}));
      });
      return hs.length})()""")
    assert moved == 4, "四個手柄不在"
    manual = None
    for _ in range(45):
        time.sleep(1)
        s = ev("document.getElementById('dsPvStat').textContent") or ""
        if "自己拉的四個角" in s:
            manual = s
            break
    assert manual, "拖完之後沒有變成手動的結果"
    assert ev("document.querySelectorAll('#dsStrip .ds-pg')[2]"
              ".querySelector('.ds-pg-mark').textContent") == "自拉四角", \
        "縮圖列沒標出這一頁調整過"

    ev("document.querySelector('input[name=\"dsQuadMode\"][value=auto]').click()")
    back = None
    for _ in range(45):
        time.sleep(1)
        s = ev("document.getElementById('dsPvStat').textContent") or ""
        if "殘留" in s and "自己拉的四個角" not in s:
            back = s
            break
    assert back, "切回自動之後畫面還停在手動的結果"
    assert back == auto, (
        f"切回自動的結果跟原本的自動結果不一樣：\n  自動 {auto!r}\n  切回 {back!r}")


def test_the_manual_corners_are_kept_when_switching_back_and_forth(page):
    """**模式是開關不是刪除鍵。**

    切回自動只是「這次不用它」，使用者拉好的四個角要留著 ——
    不然來回切一次就白拉一次。
    """
    ev = page
    ev("document.getElementById('dsQuadEdit').click()")
    time.sleep(2.5)
    assert ev("document.getElementById('dsPvStat').textContent")
    pts = ev("document.getElementById('dsQuadLine').getAttribute('points')") or ""
    assert len(pts.split()) == 4, f"切回手動之後四個角不見了：{pts!r}"
    # 拉到的位置應該還是剛才那組（左上大約在 5%）
    x0 = float(pts.split()[0].split(",")[0])
    assert x0 < 15, f"四個角被重設成自動抓的那組了（左上 x={x0}）"


def test_rotating_then_dragging_corners_still_lands_on_the_paper(page):
    """**使用者 2026-09-14 回報的那個**：轉向過之後自己拉四個角，產出跑掉。

    座標系當時有兩個：使用者拉的是**轉向後**那張圖上的點，伺服器卻用
    **未轉**的長寬換算、然後又把它轉了一次。核心層的數字在
    `test_doc_straighten.py` 裡（產出 834×358／墨點 41.2%，應為 471×629／1.9%）；
    這裡守的是**畫面這一端**：轉向之後切到手動，四個手柄要落在紙上
    （也就是伺服器回的四個角是轉向後的座標系），而且預覽要算得出來。
    """
    ev = page
    # **自己把狀態重設**：這個 fixture 是 module 範圍的，前面幾條測試在第 3 頁
    # 留下了手動拉的四個角。不重設的話這條會用到別人的座標 ——
    # 症狀就是本專案很熟的那個指紋「**單跑全綠、合跑失敗**」
    #（我第一版就是這樣：單跑過、整份跑紅在「51% 是暗的」）。
    ev("document.getElementById('dsPvPage').value='3';"
       "document.getElementById('dsPvPage').dispatchEvent(new Event('change'))")
    _settled(ev, "3")
    ev("document.getElementById('dsQuadEdit').click()")
    time.sleep(1.5)
    ev("document.getElementById('dsQuadReset').click()")      # 重新自動抓
    _settled(ev, "3")
    ev("document.querySelector('[data-rot=\"90\"]').click()")
    after_rot = _settled(ev, "3")
    assert "轉了 90" in after_rot, f"轉向沒生效：{after_rot!r}"

    assert ev("document.getElementById('dsQuadEdit').checked") is True, "應該還在手動模式"
    time.sleep(2.0)
    pts = ev("document.getElementById('dsQuadLine').getAttribute('points')") or ""
    assert len(pts.split()) == 4, f"轉向之後切到手動，四個角不見了：{pts!r}"
    xs, ys = [], []
    for pair in pts.split():
        x, y = (float(v) for v in pair.split(","))
        xs.append(x)
        ys.append(y)
    # 四個角應該框出「一張紙」——不是一條細片，也不是整個畫面
    frac = ((max(xs) - min(xs)) / 100.0) * ((max(ys) - min(ys)) / 100.0)
    assert 0.05 < frac < 0.98, (
        f"轉向之後的四個角框出 {frac:.0%} 的畫面（{pts}）—— 座標系又錯了")
    assert min(xs) >= -1 and max(xs) <= 101 and min(ys) >= -1 and max(ys) <= 101, \
        f"四個角跑到畫面外：{pts}"

    # **驗產出本身**（§0.5）：疊圖的座標對了不代表裁出來的是紙。
    # 座標系搞錯時框到的大半是深色桌面 —— 實測墨點比例 41.2%（正確 1.9%）。
    # 只驗四邊形的位置的話，這一版的 bug 照樣全綠（變異驗證確認過）。
    # 真的把四個角拖一遍（只按下不移動的話不會產生覆寫，測不到手動那條路）
    # **拖到目前四邊形的位置附近** —— 隨便拖到畫面的 10%~90% 的話，
    # 那個框本來就會吃進一大片桌面，量到的「暗像素多」是我自己拖的結果，
    # 不是座標系的問題（第一版就是這樣紅的，51% 暗）。
    moved = ev("""(()=>{
      const ps=document.getElementById('dsQuadLine').getAttribute('points')
        .split(' ').map(s=>s.split(',').map(Number).map(v=>v/100));
      const hs=document.querySelectorAll('#dsHandles .ds-h');
      const art=document.getElementById('dsArt').getBoundingClientRect();
      hs.forEach((h,i)=>{
        const x=art.left+ps[i][0]*art.width, y=art.top+ps[i][1]*art.height;
        h.dispatchEvent(new PointerEvent('pointerdown',{clientX:x,clientY:y,bubbles:true}));
        document.dispatchEvent(new PointerEvent('pointermove',{clientX:x,clientY:y,bubbles:true}));
        document.dispatchEvent(new PointerEvent('pointerup',{clientX:x,clientY:y,bubbles:true}));
      });
      return hs.length})()""")
    assert moved == 4

    # **拉到原位，座標就不該變。**
    #
    # 2026-09-15 實測：拉完第一個角之後整張圖往上跳 40~50 px（狀態那一行
    # 從兩列縮回一列），剩下三個角因此全部落在低 19% 的位置 ——
    # 拉出來的四邊形被 `quad_is_sane()` 判成不合理（內角極差 40.4° > 40°）
    # 而**整組丟掉**，畫面卻還寫著「用的是你自己拉的四個角」。
    #
    # 只驗「暗像素少」抓不到這一條（被丟掉之後退回整頁，而整頁在這份合成
    # 素材上剛好也不算暗）—— 這條才是直接的判準。
    after = ev("document.getElementById('dsQuadLine').getAttribute('points')") or ""
    got = [tuple(float(v) for v in pair.split(",")) for pair in after.split()]
    was = [tuple(float(v) for v in pair.split(",")) for pair in pts.split()]
    assert len(got) == 4, f"拖完之後四個角不見了：{after!r}"
    drift = max(max(abs(a[0] - b[0]), abs(a[1] - b[1])) for a, b in zip(got, was))
    assert drift < 1.5, (
        f"把四個角拖到它們原本的位置，座標卻跑掉了 {drift:.1f} 個百分點"
        f"（拖之前 {pts} / 拖之後 {after}）—— 拖曳途中版面有東西改變高度")

    for _ in range(45):
        time.sleep(1)
        if "自己拉的四個角" in (ev("document.getElementById('dsPvStat').textContent") or ""):
            break
    else:
        raise AssertionError("拖完之後沒有變成手動的結果")
    # **預算要撐得住機器在忙**：這台同時有別的專案在跑，算圖 ＋ 瀏覽器載圖
    # 在尖峰時會超過 30 秒。這條紅的時候不是產品壞了，是等不夠久 ——
    # 而「等不夠久」與「真的壞了」在原本的訊息裡長得一模一樣。
    dark = None
    for _ in range(90):
        time.sleep(1)
        dark = ev("""(()=>{
          const im = document.getElementById('dsPvAfter');
          if (!im || im.hidden || !im.complete || !im.naturalWidth) return null;
          const c = document.createElement('canvas');
          c.width = 120; c.height = Math.max(1, Math.round(120*im.naturalHeight/im.naturalWidth));
          const g = c.getContext('2d');
          g.drawImage(im, 0, 0, c.width, c.height);
          const d = g.getImageData(0, 0, c.width, c.height).data;
          let n = 0;
          for (let i = 0; i < d.length; i += 4) if (d[i] < 128) n++;
          return Math.round(1000 * n / (d.length / 4)) / 10;   // 暗像素 %
        })()""")
        if dark is not None:
            break
    if dark is None:
        # **失敗時要說得出是哪一種沒載到** —— 元素不在、被藏起來、還沒載完、
        # 載了但寬度是 0，四種的下一步完全不同。
        why = ev("""(()=>{
          const im = document.getElementById('dsPvAfter');
          if (!im) return '元素不在';
          if (im.hidden) return '元素被藏起來（流程沒走到）';
          if (!im.complete) return '圖還沒載完（src=' + (im.src || '').slice(-40) + '）';
          if (!im.naturalWidth) return '載完了但寬度是 0（圖壞了或是 404）';
          return '說不上來';
        })()""")
        raise AssertionError(f"修正後的圖量不到 —— {why}")
    assert dark < 25, (
        f"修正後有 {dark}% 的像素是暗的 —— 框到桌面了（座標系錯了）")
