"""哪些工具支援背景作業 —— 一律**推導**，不維護清單。

## 為什麼不加一個 `supports_background` 旗標

那種手工清單在這個專案漂掉過很多次（文件涵蓋、內建角色、設定備份分類），而且
漂掉時**沒有任何錯誤訊息** —— 只有使用者踩到才會發現。

真相只有一個地方：這支工具的 router 有沒有呼叫 `job_manager.submit`。畫面上要
標示的話就從這裡推導，並用這份測試釘住「推導出來的結果」與「實際狀況」一致。

## 目前的標示方式

**不在工具清單的卡片上加徽章。** 41 個工具標 20 個會很吵，而且瀏覽工具時這個
資訊沒有用 —— 使用者會想知道「能不能關掉這頁」的時刻，是他**已經送出、正看著
進度列**的時候。所以提示做在共用進度列裡（`components/job_progress.html` 的
`.job-bg-note`），由「有沒有拿到 job_id」自動決定，不需要任何per-tool設定。
"""
from __future__ import annotations

import pathlib


def _scan() -> tuple[set[str], set[str]]:
    """(有背景作業, 沒有背景作業) —— 依 router 是否呼叫 job_manager.submit。"""
    has, without = set(), set()
    for d in sorted(pathlib.Path("app/tools").iterdir()):
        r = d / "router.py"
        if not r.is_file():
            continue
        tool = d.name.replace("_", "-")
        (has if "job_manager.submit" in r.read_text(encoding="utf-8")
         else without).add(tool)
    return has, without


def test_scan_finds_both_kinds():
    has, without = _scan()
    assert len(has) >= 15, f"只掃到 {len(has)} 個有背景作業的工具，掃描可能失效"
    assert without, "掃不到任何沒有背景作業的工具，掃描可能失效"


def test_known_heavy_tools_have_background_jobs():
    """會跑很久的工具一定要有背景作業 —— 少了就是「關掉頁面就白做了」。"""
    has, _ = _scan()
    must = {
        "pdf-to-office", "pdf-to-slides", "office-to-pdf",   # Office 引擎
        "pdf-ocr",                                           # OCR
        "translate-doc",                                     # 逐句 LLM
        "pdf-compress", "pdf-merge", "pdf-split",            # 大檔常見
        "pdf-stamp", "pdf-watermark", "pdf-fill",            # 批次
        "submission-check",
    }
    missing = sorted(must - has)
    assert not missing, f"這些工具應該要有背景作業：{missing}"


def test_progress_component_carries_the_hint():
    """「可以關掉這一頁」的提示做在共用進度列裡。

    做在共用元件而不是各工具的模板，才會自動涵蓋每一個用作業的工具；
    哪天有人新增工具也不必記得補。
    """
    tpl = pathlib.Path("app/web/templates/components/job_progress.html")
    js = pathlib.Path("static/js/job_progress.js")
    assert "job-bg-note" in tpl.read_text(encoding="utf-8")
    src = js.read_text(encoding="utf-8")
    assert "bgNote" in src
    # 開始跑時顯示、結束時收起（四個結束分支都要）
    assert src.count("this.bgNote.hidden = true") >= 4
    assert "this.bgNote.hidden = false" in src


def test_hint_is_not_duplicated_in_individual_tools():
    """個別工具不要各自再寫一句 —— 兩份文案遲早會不一致。"""
    dupes = []
    for f in pathlib.Path("app/tools").rglob("*.html"):
        t = f.read_text(encoding="utf-8")
        if "可以關掉這一頁" in t or "可以關掉這頁" in t:
            dupes.append(str(f))
    assert not dupes, f"這些工具自己又寫了一次提示：{dupes}"
