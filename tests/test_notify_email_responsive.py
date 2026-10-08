"""通知信的卡片要跟著讀信窗格縮（fluid hybrid）。

## 由來

使用者回報：讀信窗格拉窄時，白色卡片停在 560px 不動、右邊被切掉。原本卡片只靠
表格上的 `max-width:560px` ＋ `width="100%"`，在瀏覽器裡量是會縮的 —— 但讀信軟體
不是瀏覽器：有的不認表格上的 max-width、有的沒有 viewport 就用 980px 的虛擬寬度
排版、有的把表格當內容盒模型（`width:100%` 再加左右 padding 就超出窗格）、
Outlook 桌面版（Word 排版引擎）根本不認 max-width。

所以版型改成 email 業界的「fluid hybrid」寫法（見 `notify_email_html` 的說明），
這支把每一層釘住：

* 靜態檢查：卡片 `width="100%"` ＋ `style` 裡 `width:100%;max-width:560px`、
  外層 div 的 max-width、只有 `<!--[if mso]>` 裡才有固定 560 寬、**條件註解以外
  沒有任何比 320 寬的固定寬度**、寬度 100% 的表格不帶左右 padding、viewport、
  長字串折行。
* 真的瀏覽器量：320 / 360 / 600 / 1100 四種寬度，頁面不可以橫向捲動、卡片右緣
  不可以超出窗格、寬的時候卡片剛好 560。量的是**很長、沒有空白的檔名** ——
  那正是會把表格撐寬的東西。
"""
from __future__ import annotations

import html as _html
import json
import os
import re
import shutil
import subprocess
import sys
import uuid

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app.core import notify_email_html as h  # noqa: E402

#: 很長、沒有空白的檔名（中文 ＋ 英數）—— 不會折行的話會把卡片撐寬
LONG_NAME = ("關於本局一一五年度資訊設備汰換採購案之簽辦意見草稿_第三版_final_"
             "reallylongfilename_without_spaces_abcdefghijklmnopqrstuvwxyz"
             "0123456789.odt")
LONG_ERROR = ("Failed to convert " + "x" * 260)

#: 條件註解以外，最寬可以寫死多寬（手機窄窗格是 320）
PHONE = 320


def _mail(**kw) -> str:
    args = dict(site_name="Jason Tools 文件工具箱", ok=True, tool="公文撰擬",
                filename=LONG_NAME, elapsed="1 分 23 秒", note_kind="jobs",
                action_url="https://doc.example.test/my-jobs",
                workspace_url="https://doc.example.test/workspace",
                logo_cid="jtdt-logo", icon_cid="jtdt-tool-icon")
    args.update(kw)
    return h.render(**args)


_MSO = re.compile(r"<!--\[if mso\]>.*?<!\[endif\]-->", re.S)


def _outside_mso(s: str) -> str:
    return _MSO.sub("", s)


def _tables(s: str) -> list[str]:
    return re.findall(r"<table\b[^>]*>", s)


def _card(s: str) -> str:
    cards = [t for t in _tables(_outside_mso(s)) if "background:#ffffff" in t]
    assert len(cards) == 1, f"找不到（或找到不只一張）白色卡片表格：{cards}"
    return cards[0]


def _style(tag: str) -> str:
    m = re.search(r'style="([^"]*)"', tag)
    return m.group(1) if m else ""


# ---------- 靜態：每一層都在 ----------

def test_card_is_fluid_with_a_max_width():
    """卡片：屬性與 style **兩邊都寫** 100% —— 有的消毒程式只留屬性、有的只留 style。"""
    card = _card(_mail())
    assert 'width="100%"' in card, f"卡片沒有 width=\"100%\"：{card}"
    st = _style(card).replace(" ", "")
    assert re.search(r"(?:^|;)width:100%", st), f"卡片 style 沒有 width:100%：{st}"
    assert "max-width:560px" in st, f"卡片沒有 max-width:560px：{st}"


def test_a_wrapper_div_caps_the_width_for_clients_that_ignore_table_max_width():
    s = _outside_mso(_mail())
    assert re.search(r'<div style="max-width:560px;margin:0 auto">', s), \
        "少了外層 max-width 的 div —— 不認表格 max-width 的讀信軟體會攤成整個窗格"


def test_no_fixed_width_frame_for_outlook():
    """**不給 Outlook 桌面版固定 560 寬的外框**（`<!--[if mso]>`）。

    Outlook 不認 max-width，給了固定寬度它就照寫：讀信窗格比 584px 窄時出現橫向捲動 ——
    正是使用者回報的那個問題（「觀看的寬度變少，它沒有跟著縮」）。不給的話 Outlook 是
    整個窗格寬，卡片寬一點但永遠不會被切。"""
    for kw in ({}, {"ok": False, "error": LONG_ERROR, "action_url": ""}):
        s = _mail(**kw)
        assert not _MSO.findall(s), "又加回了 Outlook 專用的固定寬度外框"
        assert 'width="560"' not in s


def test_no_fixed_width_wider_than_a_phone_outside_mso():
    """條件註解以外，任何寫死的寬度都不可以比手機窄窗格寬（max-width 不算）。"""
    for kw in ({}, {"ok": False, "error": LONG_ERROR, "action_url": ""},
               {"note_kind": "workspace"}):
        s = _outside_mso(_mail(**kw))
        attrs = [int(n) for n in re.findall(r'\bwidth="(\d+)"', s)]
        styles = [int(n) for n in
                  re.findall(r'(?<![-\w])(?:min-)?width:\s*(\d+)px', s)]
        too_wide = [n for n in attrs + styles if n > PHONE]
        assert not too_wide, f"條件註解以外有寫死的寬度 {too_wide}（> {PHONE}px）"
        # 掃得到東西（不然「沒有超寬」只是什麼都沒掃到）
        assert attrs and styles, "一個寫死的寬度都沒掃到 —— 檢查本身壞了"


def test_full_width_tables_carry_no_horizontal_padding():
    """`width:100%` 的表格再帶 padding，內容盒模型的讀信軟體會超出窗格 24px。"""
    for t in _tables(_outside_mso(_mail())):
        if 'width="100%"' not in t:
            continue
        st = _style(t)
        assert not re.search(r"(?:^|;)padding(?:-left|-right)?:", st), \
            f"寬度 100% 的表格帶了 padding（要放在格子上）：{t}"


def test_viewport_meta_is_present():
    """手機上的讀信軟體沒有 viewport 會用 980px 虛擬寬度排版。"""
    assert ('<meta name="viewport" content="width=device-width, '
            'initial-scale=1">') in _mail()


def test_long_values_wrap_instead_of_widening_the_card():
    s = _mail()
    tds = [m for m in re.findall(r"<td\b[^>]*>[^<]*", s) if LONG_NAME[:10] in m]
    assert tds, "找不到放檔名的格子"
    st = _style(tds[0])
    assert "word-break:break-word" in st and "overflow-wrap:anywhere" in st, \
        f"檔名那一格不會折行：{st}"
    rows_tbl = [t for t in _tables(s) if "border-top:1px solid" in t
                and "margin-top:14px" in t]
    assert rows_tbl and "table-layout:fixed" in _style(rows_tbl[0]), \
        "欄位表沒有 table-layout:fixed —— 不支援折行的讀信軟體會被長檔名撐寬"


def test_content_is_unchanged_by_the_layout():
    """這次只動版面：句子、按鈕、連結都還在，而且檔名照樣跳脫。"""
    s = _mail(filename='<b>x</b>.pdf')
    for text in ("公文撰擬 已完成", "開啟「我的作業」", "不含檔案內容",
                 "不想再收到可到「我的作業 → 通知設定」關閉。",
                 'href="https://doc.example.test/my-jobs"'):
        assert text in s, f"內容不見了：{text}"
    assert "<b>x</b>.pdf" not in s and "&lt;b&gt;x&lt;/b&gt;.pdf" in s


# ---------- 真的瀏覽器量 ----------

def _browser():
    try:
        from tools.browser_probe import browser, browser_runs
    except Exception:  # noqa: BLE001
        return None
    b = browser()
    # 找得到執行檔不代表跑得起來（CI 機器上是 snap 的空殼，一叫就卡住）
    return b if b and browser_runs(b) else None


@pytest.mark.skipif(_browser() is None, reason="這台沒有跑得起來的 chromium —— 這條要真的瀏覽器才量得到")
def test_card_shrinks_with_the_reading_pane_in_a_browser():
    """四種窗格寬度：不可以橫向捲動、卡片右緣不超出窗格、寬的時候剛好 560。

    headless chromium 的視窗最窄只有 500px，所以把信放進指定寬度的 `srcdoc`
    iframe 裡量（`srcdoc` 跟外層同源，讀得到裡面的版面）。
    """
    from tools.browser_probe import uploadable_dir

    widths = [1100, 600, 360, 320]
    mails = {"ok": _mail(), "error": _mail(ok=False, error=LONG_ERROR, action_url="")}
    frames = "".join(
        f'<iframe id="f{k}{w}" style="width:{w}px;height:900px;border:0" '
        f'srcdoc="{_html.escape(m, quote=True)}"></iframe>'
        for k, m in mails.items() for w in widths)
    page = (
        '<!DOCTYPE html><html><body style="margin:0">' + frames +
        '<pre id="out"></pre><script>addEventListener("load", () => {'
        "const res = {};"
        f"for (const k of {json.dumps(list(mails))}) for (const w of {json.dumps(widths)}) {{"
        "  const doc = document.getElementById('f' + k + w).contentDocument;"
        "  const card = [...doc.querySelectorAll('table')].find("
        "      t => (t.getAttribute('style') || '').includes('background:#ffffff'));"
        "  const r = card.getBoundingClientRect();"
        "  const a = doc.querySelector('a[href$=\"my-jobs\"]');"
        "  res[k + w] = {scroll: doc.documentElement.scrollWidth,"
        "    left: r.left, right: r.right,"
        "    btn: a ? a.getBoundingClientRect().right : null};"
        "}"
        "document.getElementById('out').textContent = 'RES' + JSON.stringify(res);"
        "});</script></body></html>")
    d = uploadable_dir("jtdt-notify-mail-test")
    fn = os.path.join(d, f"mail-{uuid.uuid4().hex}.html")
    prof = os.path.join(d, f"prof-{uuid.uuid4().hex}")
    with open(fn, "w", encoding="utf-8") as f:
        f.write(page)
    try:
        out = subprocess.run(
            [_browser(), "--headless", "--disable-gpu", "--no-sandbox",
             f"--user-data-dir={prof}", "--window-size=1400,900",
             "--virtual-time-budget=5000", "--dump-dom", "file://" + fn],
            capture_output=True, text=True, timeout=120).stdout
    finally:
        os.unlink(fn)
        shutil.rmtree(prof, ignore_errors=True)
    m = re.search(r"RES(\{[^<]*\})", out)
    assert m, f"瀏覽器沒有量到東西：{out[-400:]}"
    res = json.loads(_html.unescape(m.group(1)))
    for k in mails:
        for w in widths:
            g = res[f"{k}{w}"]
            assert g["scroll"] <= w, f"{k} 寬 {w}px：頁面要橫向捲動（{g['scroll']}px）"
            assert g["right"] <= w and g["left"] >= 0, \
                f"{k} 寬 {w}px：卡片超出窗格（{g['left']:.0f}..{g['right']:.0f}）"
            if g["btn"] is not None:
                assert g["btn"] <= g["right"], f"{k} 寬 {w}px：按鈕跑到卡片外"
        # 寬的時候卡片剛好 560（max-width 生效，沒有攤成整個窗格）
        for w in (1100, 600):
            g = res[f"{k}{w}"]
            assert abs((g["right"] - g["left"]) - 560) <= 1, \
                f"{k} 寬 {w}px：卡片不是 560 寬（{g['right'] - g['left']:.0f}）"
        # 窄的時候卡片跟著縮（兩側各留 12px 灰底）
        g = res[f"{k}320"]
        assert g["right"] - g["left"] <= 320 - 24 + 1, \
            f"{k} 寬 320px：卡片沒有跟著縮（{g['right'] - g['left']:.0f}）"
