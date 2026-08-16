"""格式用語要一致：「辦公文件」是統稱，「文書檔」是其中一類。

## 由來

原本的用語很雜 —— 同一件事在不同地方叫「文書」「文書檔」「Office 文件」
「文件檔」「Word / Excel / PowerPoint / ODF」。使用者看到「文書轉 PDF」與
「PDF 轉文書檔」會以為是同一類東西的來回轉換，其實範圍不同。

統一後的規則：

* **文書檔** = `.docx` / `.odt`
* **試算表** = `.xlsx` / `.ods`
* **簡報**   = `.pptx` / `.odp`
* **PDF**    = `.pdf`
* **辦公文件** = 以上四類的**統稱**（工具能同時收多種格式時用）

「辦公文件」字面上包含「文件」，所以子項刻意不叫「文件」而叫「文書檔」——
兩個詞才分得開。

## 這份測試擋什麼

新增工具時很容易又寫回舊說法。這裡直接掃工具的名稱與描述。
"""
from __future__ import annotations

import re

import pytest

#: 這些說法已經淘汰，不可以出現在工具名稱或描述裡。
STALE = {
    "文書轉": "辦公文件轉",
    "轉文書\b": "轉辦公文件",
    "Office 文件": "辦公文件",
    "文件檔": "辦公文件 或 文書檔",
    "Word / Excel / PowerPoint": "辦公文件",
    "Word、Excel": "辦公文件",
}


@pytest.fixture(scope="module")
def tools():
    from app.tool_registry import discover_tools
    return list(discover_tools())


def test_tool_names_use_current_terminology(tools):
    bad = []
    for t in tools:
        name = t.metadata.name or ""
        for stale, better in STALE.items():
            if re.search(stale, name):
                bad.append(f"{t.metadata.id}：名稱「{name}」用了舊說法，請改用 {better}")
    assert not bad, "\n".join(bad)


def test_tool_descriptions_use_current_terminology(tools):
    bad = []
    for t in tools:
        desc = t.metadata.description or ""
        for stale, better in STALE.items():
            if re.search(stale, desc):
                bad.append(f"{t.metadata.id}：描述用了「{stale}」，請改用 {better}")
    assert not bad, "\n".join(bad)


def test_umbrella_and_subtype_do_not_collide(tools):
    """統稱與子項不可以互相取代 —— 這是當初改名的原因。

    「辦公文件」指四類的集合，「文書檔」只指 .docx / .odt。
    如果某支工具只輸出 .docx / .odt 卻自稱「辦公文件」，使用者會以為
    它也能輸出試算表或簡報。
    """
    single_output = {
        "pdf-to-office": "文書檔",     # 只輸出 .docx / .odt
        "pdf-to-slides": "簡報",       # 只輸出 .pptx / .odp
    }
    bad = []
    for t in tools:
        want = single_output.get(t.metadata.id)
        if not want:
            continue
        name = t.metadata.name or ""
        if "辦公文件" in name:
            bad.append(f"{t.metadata.id}：只輸出{want}，名稱卻用了統稱「辦公文件」")
        if want not in name:
            bad.append(f"{t.metadata.id}：名稱「{name}」應該點明是「{want}」")
    assert not bad, "\n".join(bad)


def test_home_page_explains_the_terminology():
    """首頁要有格式說明 —— 使用者才知道兩個詞的差別。"""
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent
    home = (root / "app" / "web" / "templates" / "home.html").read_text(encoding="utf-8")
    assert "fmt-note" in home, "首頁少了格式用語說明區塊"
    for term in ("文書檔", "試算表", "簡報", "辦公文件", "統稱"):
        assert term in home, f"格式說明少了「{term}」"


#: 更名前的工具名稱。這些字串**不可以再出現在任何樣板的畫面文字裡**。
#: （`app/main.py` 的搜尋別名是刻意保留的例外 —— 使用者打舊名也要找得到。）
RETIRED_TOOL_NAMES = [
    "文書轉 PDF",
    "文書轉圖片",
    "Markdown 轉文書",
    "PDF 轉簡報檔",
]


def test_no_retired_tool_names_in_templates():
    """樣板裡人工寫的散文最容易漏掉。

    這一輪更名時，註冊表 / 前台樣板 / 介紹站 / README 都掃過了，卻漏掉
    **管理區**兩頁 —— 轉檔引擎設定頁與作業佇列頁的說明文字裡寫著工具名。
    那不在註冊表裡，grep 工具 id 也找不到，只有掃畫面文字才抓得到。
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent
    bad = []
    for tpl in sorted(root.glob("app/**/templates/**/*.html")):
        text = tpl.read_text(encoding="utf-8", errors="replace")
        for old in RETIRED_TOOL_NAMES:
            if old in text:
                line = next((i for i, ln in enumerate(text.splitlines(), 1)
                             if old in ln), 0)
                bad.append(f"{tpl.relative_to(root)}:{line} 仍寫著舊名「{old}」")
    assert not bad, "\n".join(bad)
