"""「存至工作區」按鈕出現的條件，必須跟工作區真正收得下的格式一致。

## 由來

`job_progress.js` 裡寫死過一行 `/\\.(pdf|png|docx|odt)$/`。伺服器端在
v1.14.6 就把 `.xlsx` / `.ods` / `.pptx` / `.odp` 加進工作區了（理由寫在
`workspace.detect_kind` 的說明裡：「PDF 轉簡報」的產出原本存不進去），
**但那一行沒有跟著改**。

後果：伺服器收得下的檔案，畫面上那顆「存至工作區」不會出現。使用者只會
覺得「這個工具怎麼少了一顆鈕」，沒有任何錯誤訊息可以查 —— 是 2026-08-16
使用者自己看到才回報的。

`workspace.py` 裡本來就有一句註解寫著「兩邊各寫一份遲早會不一致」，
講的是 Python 內部的兩份清單；同一件事後來跨到 Python / JS 之間又發生
一次。所以現在清單只有伺服器端一份，前端從 `data-ws-exts` 讀。

## 判準

1. JS 不可以自己寫死副檔名清單。
2. 模板要把伺服器端的清單放進 DOM。
3. 這個工具的產出格式（.xlsx 等）真的要讓按鈕出現。
"""
from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
JS = ROOT / "static" / "js" / "job_progress.js"
TPL = ROOT / "app" / "web" / "templates" / "components" / "job_progress.html"


def test_js_does_not_hardcode_the_extension_list():
    """JS 裡不可以再出現寫死的副檔名清單。

    比對「有沒有一組看起來像副檔名白名單的正規式」——寫死的那一行長成
    `/\\.(pdf|png|docx|odt)$/i`，特徵是括號裡用 `|` 串起一堆已知副檔名。
    """
    src = JS.read_text(encoding="utf-8")
    # 去掉註解（說明裡會引用「原本錯誤的寫法」當反例，掃到會誤報）
    body = re.sub(r"//[^\n]*", "", src)
    bad = re.findall(r"\(\s*(?:pdf|png|docx|odt|xlsx|pptx|odp|ods)"
                     r"(?:\s*\|\s*\w+){2,}\s*\)", body)
    assert not bad, (
        "job_progress.js 又把工作區的副檔名清單寫死了：" + str(bad)
        + "\n請改讀 `data-ws-exts`（由 `workspace_extensions()` 提供），"
          "否則伺服器端一擴充格式，這裡就會無聲地擋掉。")


def test_template_publishes_the_server_list():
    assert "data-ws-exts" in TPL.read_text(encoding="utf-8"), (
        "job_progress.html 要把 `workspace_extensions()` 放進 DOM，"
        "前端才有正確的清單可用")


def test_js_reads_the_list_from_the_dom():
    assert "wsExts" in JS.read_text(encoding="utf-8"), (
        "job_progress.js 要從 `data-ws-exts` 讀清單")


def test_extensions_helper_matches_workspace_allowed():
    """`workspace_extensions()` 要涵蓋 `workspace.ALLOWED` 的每一種。"""
    from app.core import workspace
    from app.main import _tpl_workspace_extensions

    got = set(_tpl_workspace_extensions().split())
    want = {e.lstrip(".") for e in workspace.ALLOWED.values()}
    assert got == want, f"少了 {want - got}；多了 {got - want}"


def test_office_convert_outputs_are_savable():
    """格式互轉常用的產出要存得進工作區。

    這個工具的目標格式有三十幾種，工作區收不下全部是合理的；但**最常用的
    那幾種**（各家族的 ODF 與 OOXML）如果存不進去，那顆按鈕就等於對這個
    工具沒有意義。
    """
    from app.core import workspace

    exts = {e.lstrip(".") for e in workspace.ALLOWED.values()}
    for e in ("docx", "odt", "xlsx", "ods", "pptx", "odp"):
        assert e in exts, f"工作區收不下 .{e}"


def test_rendered_page_carries_the_extension_list():
    """實際 render 出來的頁面要真的帶著清單。

    只檢查模板原始碼不夠 —— `workspace_extensions()` 沒註冊成 Jinja global
    的話，render 當下才會爆，而模板檔看起來完全正常。
    """
    from fastapi.testclient import TestClient

    from app.main import app

    r = TestClient(app).get("/tools/office-convert/")
    assert r.status_code == 200
    m = re.search(r'data-ws-exts="([^"]*)"', r.text)
    assert m, "render 出來的頁面沒有 data-ws-exts"
    assert "xlsx" in m.group(1).split(), m.group(1)
