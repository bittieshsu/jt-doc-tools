"""Markdown 轉辦公文件：只轉使用者要的格式 ＋ 程式碼語法上色。

## 為什麼

使用者 2026-09-18 在正式機丟一份 43 KB 的 Markdown，**卡超過兩分鐘然後失敗**。
根因不是那份檔案（同一份在閒置的開發機上 **6 秒**就轉完），是：

* **一次全轉三種**：HTML→PDF、HTML→ODT、ODT→DOCX，三次 soffice
  **被鎖序列化、每次上限 120 秒** —— 最壞六分鐘，而畫面寫「可能要 10-30 秒」。
* 那台的 soffice 被我們刻意降優先權（讓網頁保持回應），主機一忙就爆掉上限。
"""
from __future__ import annotations

import importlib
import re

import pytest

R = importlib.import_module("app.tools.markdown_to_doc.router")
themes = importlib.import_module("app.tools.markdown_to_doc.themes")

_MD = "測試\n\n```bash\nsu - zimbra -c \"zmcontrol -v\"\n```\n\n行內 `code` 對照。\n"


# ------------------------------------------------------------ 輸出格式

def test_every_theme_declares_a_code_style_and_an_inline_colour():
    """漏掉一個主題 → 它的程式碼區塊會安靜地退回沒有上色。"""
    for tid in themes.THEMES:
        assert tid in themes.CODE_STYLES, tid
        assert tid in themes.INLINE_CODE_COLORS, tid


def test_the_monochrome_theme_stays_monochrome():
    """**「極簡黑白」的承諾就是沒有顏色** —— 在上面加語法上色等於毀掉它的用途，
    而使用者選它的理由正是不要顏色。"""
    assert themes.code_style("mono") is None
    html = R._render_md_html(_MD, "mono", "t", "")
    colours = set(re.findall(r'style="color: *(#[0-9a-fA-F]{3,6})', html))
    assert colours <= {"#000"}, f"極簡黑白冒出顏色了：{colours}"


def test_a_coloured_theme_actually_highlights():
    html = R._render_md_html(_MD, "classic", "t", "")
    assert len(re.findall(r'<span style="color:', html)) >= 3


def test_highlighted_block_is_not_a_pre_inside_a_pre():
    """**pygments 預設會包一層 `<div class="highlight"><pre>`**，
    而 markdown-it 看到不是 `<pre` 開頭就再包一層 —— 變成 `<pre>` 巢在 `<pre>` 裡。

    那是不合法的 HTML，soffice 會把內層的顏色整個丟掉：
    **HTML 預覽有顏色、轉出來的 PDF 沒有，而且完全不會報錯。**
    """
    html = R._render_md_html(_MD, "classic", "t", "")
    assert not re.search(r"<pre[^>]*>\s*<code[^>]*>\s*<div", html)
    assert "<pre><pre" not in html


def test_inline_code_colour_is_an_inline_style_not_a_css_rule():
    """**這是整個上色能不能進到 PDF 的關鍵。**

    只要 CSS 裡有 `code { color: X }`，soffice 就把整個 `<code>` 當成一段
    字元樣式、**丟掉裡面的上色 span**。實測過 `!important` 它不理、
    `:not(pre) > code` 它不支援（整條規則被丟掉）—— 只有內嵌樣式會被帶過去。
    """
    for tid in themes.THEMES:
        css = themes.THEMES[tid]["css"]
        assert not re.search(r"(?m)^code \{[^}]*color", css), (
            f"{tid} 的 CSS 又出現 code 顏色規則 —— 語法上色會在轉檔時無聲消失")
    html = R._render_md_html(_MD, "classic", "t", "")
    assert '<code style="color: #be185d">' in html


@pytest.mark.parametrize("raw,want", [
    ("", {"pdf", "docx", "odt"}),          # 沒指定＝照舊全轉（API 相容）
    ("pdf", {"pdf"}),
    ("pdf,docx", {"pdf", "docx"}),
    (" PDF , ODT ", {"pdf", "odt"}),
])
def test_format_parsing(raw, want):
    got = {f.strip().lower() for f in raw.split(",") if f.strip()} or set(R._FORMATS)
    assert got == want


def test_unknown_format_is_rejected():
    assert "rtf" not in R._FORMATS


def test_docx_needs_odt_as_an_intermediate():
    """只勾 .docx 時仍然要產 .odt（HTML 直轉 docx 的濾鏡鏈常失敗），
    **但 odt 不可以出現在下載清單裡** —— 使用者沒有要它。"""
    src = importlib.import_module("inspect").getsource(R.convert)
    assert 'need_odt = ("odt" in want) or ("docx" in want)' in src
    assert '"odt" in want and produced["odt"]' in src


def test_timeout_message_does_not_blame_the_user_file():
    """共用的逾時訊息寫的是「這份檔案可能已毀損…請對方提供 PDF 版」——
    那是給「使用者上傳 Office 檔」那條路的。

    **這條路的 HTML 是我們自己產的**，叫使用者去找對方要 PDF 完全沒有意義，
    而他會照著做。
    """
    import ast
    import inspect
    import textwrap
    # **判準放在字串常數上，不是整份原始碼。**
    # 我第一版直接掃原始碼 —— 而我自己寫在旁邊的註解裡**引用了那句錯誤訊息
    # 當反例**，當場被判成違規（use vs mention，本專案第 N 次）。
    tree = ast.parse(textwrap.dedent(inspect.getsource(R.convert)))
    fn = tree.body[0]
    # **每一層的 docstring 都要排掉** —— 我第一版只排了外層，
    # 改成背景作業之後巢狀那個工作函式的 docstring 又把檢查絆倒
    # （use vs mention，同一個坑的第二種形狀）。
    docs = {ast.get_docstring(n, clean=False)
            for n in ast.walk(fn)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef,
                              ast.ClassDef, ast.Module))}
    texts = [n.value for n in ast.walk(fn)
             if isinstance(n, ast.Constant) and isinstance(n.value, str)
             and n.value not in docs]
    blob = " ".join(texts)
    assert "不是你的 Markdown 有問題" in blob
    assert "請對方提供 PDF" not in blob
    # 使用者看得到的訊息裡不可以出現 markdown 語法（會原樣印出星號）
    assert "**" not in blob


# ------------------------------------------------- 真的送請求進去（端到端）

@pytest.fixture(scope="module")
def api():
    """**一定要真的送請求。**

    我第一版只驗 `_render_md_html()` 與轉出來的 PDF 顏色，**從頭到尾沒有
    呼叫過端點** —— 而端點裡一行 `want - _FORMATS`（`set` 減 `tuple`）
    讓**每一次請求都 TypeError**。渲染層全綠、產出也對，但這支工具是全壞的。
    """
    import os
    import tempfile
    os.environ.setdefault("JTDT_DATA_DIR", tempfile.mkdtemp())
    os.environ.setdefault("JTDT_CSRF_DISABLE", "1")
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app, raise_server_exceptions=False)


def _post(api, **data):
    return api.post("/tools/markdown-to-doc/convert",
                    data={"text": _MD, "theme": "classic",
                          "font": "default", **data})


def test_endpoint_accepts_a_request_at_all(api):
    r = _post(api, formats="pdf")
    assert r.status_code != 500, r.text[:200]


@pytest.mark.parametrize("fmts", ["pdf", "docx", "odt", "pdf,docx", ""])
def test_every_format_combination_goes_through(api, fmts):
    r = _post(api, formats=fmts)
    assert r.status_code < 500 or r.status_code == 503, r.text[:200]


def test_unknown_format_is_a_400_not_a_500(api):
    r = _post(api, formats="rtf")
    assert r.status_code == 400, r.text[:200]


def _wait(api, job_id, secs=180):
    import time
    for _ in range(int(secs * 5)):
        j = api.get(f"/api/jobs/{job_id}").json()
        if j.get("status") in ("done", "error", "cancelled", "interrupted"):
            return j
        time.sleep(0.2)
    pytest.fail("作業沒有結束")


def test_convert_returns_a_job_id(api):
    """**改成背景作業**（使用者 2026-09-18 要求可以取消）：
    `/convert` 立刻回 job_id，不再把使用者卡在同步請求上。"""
    r = _post(api, formats="pdf")
    assert r.status_code == 200, r.text[:200]
    d = r.json()
    assert d.get("job_id") and d.get("upload_id")
    assert "downloads" not in d, "結果要從 /result 拿，不要塞在送出的回應裡"


def test_only_the_requested_formats_are_offered(api):
    r = _post(api, formats="pdf")
    assert r.status_code == 200, r.text[:200]
    d = r.json()
    j = _wait(api, d["job_id"])
    if j["status"] != "done":
        pytest.skip(f"這台跑不完（{j.get('message')}）")
    rr = api.get(f"/tools/markdown-to-doc/result/{d['upload_id']}")
    assert rr.status_code == 200, rr.text[:200]
    got = rr.json()
    assert got["downloads"]["pdf"]
    assert got["downloads"]["docx"] is None and got["downloads"]["odt"] is None, \
        "只勾 PDF 卻給了別的格式的下載連結"
    assert got["formats"] == ["pdf"]


def test_the_job_sets_a_result_path(api):
    """**沒設 `result_path` 的話**「我的作業」會顯示已完成卻沒有下載鈕，
    自動存入工作區與保留期清理也都不認得這份產出（本專案在文件翻譯上踩過）。"""
    r = _post(api, formats="pdf")
    j = _wait(api, r.json()["job_id"])
    if j["status"] != "done":
        pytest.skip(f"這台跑不完（{j.get('message')}）")
    # **`has_result` 才是「下載鈕會不會出現」的判準**；
    # `result_filename` 只是下載時看到的檔名。
    assert j.get("has_result") is True, "作業完成卻沒有結果檔 —— 下載鈕不會出現"
    assert (j.get("result_filename") or "").endswith(".pdf"), \
        "下載檔名要看得懂，不要退回內部檔名"


# ------------------------------------------- 框線與內距（HTML 表現屬性）

def test_tables_carry_border_attributes_not_just_css():
    """**soffice 的 CSS 支援很窄，框線要用 HTML 屬性。**

    實測（都在真的 PDF 上量的）：

    | 寫法 | 結果 |
    |---|---|
    | `td { border: 1px solid #ccc }` | **完全沒畫** |
    | `border-width` / `-style` / `-color` 拆開 | 時好時壞（跟順序、同規則有沒有 `padding` 有關） |
    | **`<table border="1" cellpadding="5">`** | **穩定 0.75pt，看得見** |
    """
    html = R._render_md_html("| A | B |\n|---|---|\n| 1 | 2 |\n", "classic", "t", "")
    assert '<table border="1" cellpadding="5" cellspacing="0"' in html


def test_code_blocks_are_indented_and_have_no_border():
    """程式碼區塊的內距 **soffice 給不了**，所以改成「縮排的色帶、不畫框線」。

    實測過三種取得內距的方法，**全部失敗**：

    | 做法 | 結果 |
    |---|---|
    | `pre { padding }` | 被當成縮排，框和字一起右移，中間沒有空隙 |
    | `<div>` 包 `<pre>`，底色給 div | **div 的底色根本不畫** |
    | 包進單格表格用 `cellpadding` | 有內距，**但長行不再折行、整塊超出頁面**（使用者當場回報） |

    第三種是我實際做出來又退掉的 —— **內容被切掉比字貼著框嚴重得多**。

    現在的做法：色帶整塊往右縮排 12pt、不畫框線。沒有框線就沒有「貼著框」，
    而縮排讓程式碼跟正文明顯分開。
    """
    import re
    html = R._render_md_html("```bash\necho hi\n```\n", "classic", "t", "")
    assert "<table" not in html, "程式碼區塊不可以包進表格 —— 長行會不折行而超出頁面"
    assert "<pre><code" in html
    css = themes.THEMES["classic"]["css"]
    # **先去掉 CSS 註解再看** —— 我在規則旁邊寫的說明裡引用了
    # 「`padding` 在 soffice 裡無效」，第一版當場被自己的檢查判成違規
    # （use vs mention，本專案第 N 次）。
    clean = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    pre_rules = re.findall(r"(?m)^pre \{([^}]*)\}", clean)
    blob = " ".join(pre_rules)
    assert "padding" not in blob, "`padding` 在 soffice 裡無效，留著會誤導下一個人"
    assert "margin: 0.8em 0 0.8em 12pt" in blob, "色帶要往右縮排"


def test_no_css_border_rule_fights_the_attribute_border():
    """CSS 的 `border: 1px` 會**額外**畫一組 0.1pt 細線，跟屬性畫的重疊，
    框線看起來會毛毛的。表格與程式碼區塊的框線只由屬性負責。"""
    import re
    for tid, th in themes.THEMES.items():
        css = th["css"]
        for sel in (r"^th, td", r"^table th", r"^table td", r"^td", r"^pre"):
            for blk in re.findall(sel + r"[^{]*\{([^}]*)\}", css, re.M):
                assert "border:" not in blk and "border-width" not in blk, (
                    f"{tid} 的 {sel} 又出現框線規則 —— 會跟屬性畫的重疊")


def test_every_theme_declares_a_code_background():
    for tid in themes.THEMES:
        assert tid in themes.CODE_BG, tid
        assert themes.code_bg(tid).startswith("#")


def test_blank_lines_inside_code_blocks_survive():
    """使用者問「是不是把空行全吃了」—— 沒有。空行在 HTML 裡就保留著，
    PDF 的行距也看得出來（實測 Δ33.8 vs 正常 17.1）。
    看起來很擠是**長行自動折行**造成的，不是空行不見。"""
    import re
    html = R._render_md_html("```bash\nA=1\n\n# 註解\nB=2\n```\n", "classic", "t", "")
    # **不要寫死結束標籤** —— `</code  >` 是合法 HTML，寫死的正規式會
    # 把它當成沒結束、整段吞掉（本專案 issue #15 的家族）。
    from tools.source_text import block_re
    code = block_re("code").search(html).group(1)
    assert "\n\n" in code, "程式碼區塊裡的空行不可以被吃掉"


def test_a_running_conversion_can_be_cancelled(api):
    """**使用者按下停止，作業就要真的收掉。**

    改成背景作業之前，`/convert` 是同步請求 —— 畫面上只能盯著「轉換中…」，
    沒有任何辦法停下來，關掉分頁還會白做一次。
    """
    import time
    r = _post(api, formats="pdf,docx,odt")      # 三種都轉，跑久一點好取消
    assert r.status_code == 200, r.text[:200]
    jid = r.json()["job_id"]

    ok = api.post(f"/api/jobs/{jid}/cancel")
    assert ok.status_code < 400, ok.text[:200]

    for _ in range(300):
        j = api.get(f"/api/jobs/{jid}").json()
        if j["status"] in ("cancelled", "done", "error"):
            break
        time.sleep(0.2)
    else:
        pytest.fail("取消之後作業沒有收斂")
    # 已經跑完才按到的話算正常（時序race），但**不可以卡在 running**
    assert j["status"] in ("cancelled", "done"), j


def test_the_page_can_reconnect_to_a_job():
    """從「我的作業」按「開啟」回來時要接得回去 ——
    **不接的話會落到一片空白頁**（本專案在文件翻譯上踩過）。"""
    import pathlib
    html = pathlib.Path(
        "app/tools/markdown_to_doc/templates/markdown_to_doc.html"
    ).read_text(encoding="utf-8")
    assert "q.get('job')" in html and "jp.track(jid)" in html


def test_the_cancel_button_exists_and_is_wired():
    import pathlib
    html = pathlib.Path(
        "app/tools/markdown_to_doc/templates/markdown_to_doc.html"
    ).read_text(encoding="utf-8")
    assert 'id="md2Cancel"' in html
    assert "jp.cancel()" in html, "停止鈕要呼叫 cancel API，不是只把畫面藏起來"


# ---------- 表格的配色走 HTML 屬性（v1.16.19，使用者截圖回報會議摘要匯出的表格） ----------

_TABLE_MD = ("| 發言者 | 次數 | 字數 |\n|---|---:|---:|\n"
             "| 陳經理 | 21 | 1,084 |\n| 許專員 | 23 | 873 |\n"
             "| 吳副理 | 22 | 797 |\n| 林經理 | 18 | 758 |\n")


def test_table_colours_are_html_attributes():
    """soffice 把 `th { background }` 塗在**文字後面**、不填滿整格；隔行的
    `nth-child` 完全不認。所以表頭底色、框線顏色、隔行底色都要是屬性。"""
    ts = themes.table_style("report")
    html = R._render_md_html(_TABLE_MD, "report", "t", "")
    assert f'bordercolor="{ts["border"]}"' in html
    ths = re.findall(r"<th\b[^>]*>", html)
    assert ths and all(f'bgcolor="{ts["head_bg"]}"' in t for t in ths), ths
    rows = re.findall(r"<tr>(.*?)</tr>", html, re.S)[1:]      # 去掉表頭那一列
    zebra = [f'bgcolor="{ts["zebra"]}"' in r for r in rows]
    assert zebra == [False, True, False, True], f"隔行底色要從第二列開始、一列隔一列：{zebra}"
    # 不上色的主題就不要硬塞屬性
    plain = R._render_md_html(_TABLE_MD, "academic", "t", "")
    assert not re.search(r"<th\b[^>]*bgcolor", plain)
    assert "bgcolor" not in "".join(re.findall(r"<td\b[^>]*>", plain))


def test_the_header_colour_fills_the_whole_cell_in_the_pdf():
    """**判準量在真的 PDF 上**：表頭底色的寬度要等於整張表的寬度。
    原本是一小塊深色只包住字（截圖上看得到），而 HTML 看起來完全正常。"""
    import fitz
    from app.core import office_convert

    if not office_convert.find_soffice():
        pytest.skip("這台機器沒有 Office 引擎")
    import tempfile
    from pathlib import Path
    ts = themes.table_style("report")
    head = tuple(int(ts["head_bg"][i:i + 2], 16) / 255 for i in (1, 3, 5))
    with tempfile.TemporaryDirectory() as td:
        h = Path(td) / "t.html"
        h.write_text(R._render_md_html(_TABLE_MD, "report", "t", ""), encoding="utf-8")
        pdf = Path(td) / "t.pdf"
        office_convert.convert_to_pdf(h, pdf, timeout=120)
        page = fitz.open(pdf)[0]
        draws = page.get_drawings()

    def near(c, ref):
        return c and all(abs(a - b) < 0.03 for a, b in zip(c, ref))

    # 整張表的寬度：框線（線段）的左右極值
    xs = [pt for d in draws if d.get("color") and not near(d["color"], head)
          for it in d["items"] if it[0] == "l" for pt in (it[1].x, it[2].x)]
    assert xs, "PDF 裡找不到表格框線"
    table_w = max(xs) - min(xs)
    # 表頭底色：跟表頭同色、夠高（排除標題底下那條細線）的填色區塊，合併成區間
    spans = sorted((d["rect"].x0, d["rect"].x1) for d in draws
                   if near(d.get("fill"), head) and d["rect"].height > 8)
    assert spans, "PDF 裡找不到表頭底色"
    covered, cur = 0.0, list(spans[0])
    for a, b in spans[1:]:
        if a <= cur[1] + 1:
            cur[1] = max(cur[1], b)
        else:
            covered += cur[1] - cur[0]
            cur = [a, b]
    covered += cur[1] - cur[0]
    assert covered >= table_w * 0.9, (
        f"表頭底色只蓋到 {covered:.0f}pt，整張表 {table_w:.0f}pt —— 又變回只包住字了")
