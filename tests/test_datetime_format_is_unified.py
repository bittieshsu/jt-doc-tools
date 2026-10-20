"""全站日期時間顯示格式統一（v1.17.1）：YYYY/MM/DD HH:mm、24 小時制。

畫面上的時間只由 `static/js/jt_time.js`（瀏覽器）與 `app/main.py:_tpl_localdt`（伺服器）產生。
其他地方自己用 `toLocaleString` 組時間，就會依語系出現「下午 3:05」或 AM / PM，各頁長得不一樣。
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
HELPER = ROOT / "static" / "js" / "jt_time.js"

# 不是「顯示給人看的時間」：檔名、<input type="date"> 的值、日曆元件
_ALLOWED = {
    ("app/tools/mfp_fetch/templates/mfp_fetch.html", "'scan-'"),           # 預設檔名
    ("app/tools/pdf_stamp/templates/pdf_stamp.html", "_isoToday"),        # 日期章輸入框的值
    ("app/tools/pdf_decrypt/templates/pdf_decrypt.html", "getFullYear"),  # 日曆與候選密碼
}

_DATE_LOCALE = re.compile(r"toLocale(?:Date|Time)String\(|\bhour12\b|\bhour\s*:\s*'2-digit'")
# 數字的 toLocaleString（千分位：空參數或 'en-US'）可以；日期的不行。同一行有 Date，或帶語系 / 選項參數就算日期
_DATE_TOLOCALE = re.compile(r"Date\b.*\.toLocaleString\(|\.toLocaleString\((?!\)|'en-US'\))")
_HAND_BUILT = re.compile(r"\.getHours\(\)|\.getMinutes\(\)")


def _sources():
    for base in ("app", "static/js"):
        for p in (ROOT / base).rglob("*"):
            if p.suffix not in (".html", ".js") or "vendor" in p.parts or p == HELPER:
                continue
            yield p


def _allowed(rel: str, line: str) -> bool:
    return any(rel == f and key in line for f, key in _ALLOWED)


def test_no_page_formats_datetimes_by_itself():
    bad = []
    for p in _sources():
        rel = p.relative_to(ROOT).as_posix()
        lines = p.read_text(encoding="utf-8").splitlines()
        for i, line in enumerate(lines, 1):
            if line.lstrip().startswith(("//", "*", "{#")):
                continue
            ctx = "\n".join(lines[max(0, i - 3):i])
            if (_DATE_LOCALE.search(line) or _DATE_TOLOCALE.search(line) or _HAND_BUILT.search(line)) \
                    and not _allowed(rel, ctx):
                bad.append(f"{rel}:{i}: {line.strip()[:100]}")
    assert not bad, "改用 jtTime.dt / date / time / md（static/js/jt_time.js）：\n" + "\n".join(bad)


def test_helper_is_loaded_on_every_page():
    base = (ROOT / "app/web/templates/base.html").read_text(encoding="utf-8")
    head = base.split("</head>", 1)[0]
    assert '<script src="/static/js/jt_time.js"></script>' in head, "要在 <head> 載入，內文裡的 inline script 才用得到"


def test_server_filter_uses_the_same_format():
    from app.main import _tpl_localdt
    import time
    ts = time.mktime((2026, 10, 19, 23, 5, 7, 0, 0, -1))
    assert _tpl_localdt(ts) == "2026/10/19 23:05"
    assert _tpl_localdt(None) == "—"


@pytest.mark.skipif(not shutil.which("node"), reason="需要 node")
def test_helper_output_is_24h_and_slashed():
    js = HELPER.read_text(encoding="utf-8") + r"""
    const jt = globalThis.window.jtTime;
    const d = new Date(2026, 9, 19, 23, 5, 7);
    const out = [jt.dt(d), jt.dt(d, {seconds: true}), jt.date(d), jt.time(d), jt.md(d),
                 jt.dt(d.getTime() / 1000), jt.dt(d.getTime()), jt.dt(''), jt.dt(null), jt.dt('garbage'),
                 jt.time(new Date(2026, 0, 1, 3, 4)), jt.dt(String(d.getTime() / 1000))];
    console.log(JSON.stringify(out));
    """
    r = subprocess.run(["node", "-e", "globalThis.window = {};" + js], capture_output=True, text=True,
                       timeout=20, env={"TZ": "Asia/Taipei", "PATH": "/usr/bin:/bin:/usr/local/bin"})
    assert r.returncode == 0, r.stderr
    import json
    assert json.loads(r.stdout) == [
        "2026/10/19 23:05", "2026/10/19 23:05:07", "2026/10/19", "23:05", "10/19 23:05",
        "2026/10/19 23:05", "2026/10/19 23:05", "", "", "", "03:04", "2026/10/19 23:05"]


@pytest.mark.parametrize("bad", ["2026/10/19 下午11:05:07", "10/19/2026, 11:05 PM", "2026-10-19 23:05",
                                 "2026年10月19日 23:05", "2026/10/19 9:05", "午後11:05", "11:05 am",
                                 "2026-10-19"])
def test_display_checker_catches_wrong_formats(bad):
    """發版前跑的畫面檢查（tools/check_datetime_display.py）自己的判準要抓得到。"""
    from tools.check_datetime_display import check_text
    assert check_text(bad), bad


@pytest.mark.parametrize("ok", ["2026/10/19 23:05", "2026/10/19 23:05:07", "10/19 23:05", "23:05:07",
                                "v1.17.1", "共 1,234 筆", "16:9", "2026/10/19",
                                "scan-20261019-230507.pdf", "report-2026-10-19.csv"])
def test_display_checker_accepts_the_site_format(ok):
    from tools.check_datetime_display import check_text
    assert not check_text(ok), ok
