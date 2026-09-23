"""LLM 停用時：**預設反灰**，管理員另外勾「停用時一併隱藏」才隱藏（v1.16.11）。

使用者 2026-09-23：「停用不代表要隱藏，停用只是反灰；另外勾選隱藏才會隱藏。」

**兩個方向都要驗**：只驗「勾了就看不到」的話，把停用一律改成隱藏也會全綠 ——
而那正是使用者明確說不要的。所以每一處都驗三種狀態：

| LLM | 停用時一併隱藏 | 應該 |
|---|---|---|
| 停用 | 沒勾 | **看得到、反灰**（看得到為什麼不能用） |
| 停用 | 勾了 | 看不到 |
| 啟用 | 勾了 | 照常可用（這個選項只在停用時有作用） |

**藏的時候元素要留著**：各工具的 JS 會 `getElementById` 讀那個勾選框，
整段不輸出的話拿到 null、後面的程式整段停住。真瀏覽器開頁那一關在
`test_llm_hidden_pages_boot.py`。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def llm_state():
    from app.core.llm_settings import llm_settings
    before = llm_settings.get()

    def set_(enabled: bool, hide: bool) -> None:
        llm_settings.update({"enabled": enabled, "hide_when_disabled": hide})

    yield set_
    llm_settings.update({"enabled": before.get("enabled", False),
                         "hide_when_disabled": before.get("hide_when_disabled", False)})


def _registry():
    from app.tool_registry import discover_tools
    return {t.metadata.id: t for t in discover_tools()}


def _tools_whose_page_uses(pattern: str) -> set[str]:
    """哪幾支工具的樣板用了某種 LLM 區塊 —— 從**樣板本身**算，不從我們的標記算
    （判準不可以跟被守的程式共用同一個定義）。"""
    out = set()
    for tid, t in _registry().items():
        tdir = t.templates_dir
        if not tdir or not Path(tdir).is_dir():
            continue
        src = "".join(p.read_text(encoding="utf-8") for p in Path(tdir).glob("*.html"))
        if re.search(pattern, src):
            out.add(tid)
    return out


LLM_ONLY = _tools_whose_page_uses(r"llm_gate\([^)]*mode=['\"]only['\"]")
AUGMENT = _tools_whose_page_uses(r"llm_gate\((?![^)]*mode=['\"]only['\"])")


def _anchor_classes(html: str) -> dict[str, list[str]]:
    """頁面上每一個工具連結（側欄 `sb-item` 與首頁 `tool-card`）的 class。"""
    out: dict[str, list[str]] = {}
    for tag in re.findall(r"<a\s[^>]*data-tool-id=\"[^\"]+\"[^>]*>", html, re.S):
        tid = re.search(r'data-tool-id="([^"]+)"', tag).group(1)
        cls = (re.search(r'class="([^"]*)"', tag) or [None, ""])[1]
        out.setdefault(tid, []).append(cls)
    return out


# ---------------------------------------------------------------- 設定本身

def test_the_default_is_grey_not_hidden():
    """**不可以把預設改成隱藏**（使用者原話：停用不代表要隱藏）。"""
    from app.core.llm_settings import DEFAULT_SETTINGS
    assert DEFAULT_SETTINGS["hide_when_disabled"] is False


def test_the_llm_only_tools_are_the_ones_whose_page_is_llm_only():
    """`requires_setup="llm"` 要剛好標在「整頁都靠 LLM」的那幾支上 ——
    多標一支，會把一支不需要 LLM 也能用的工具藏掉；少標一支，隱藏就漏一支。"""
    assert len(LLM_ONLY) >= 3, f"樣板掃出來的只靠 LLM 的工具太少：{LLM_ONLY}（掃描本身壞了？）"
    marked = {tid for tid, t in _registry().items()
              if getattr(t.metadata, "requires_setup", "") == "llm"}
    assert marked == LLM_ONLY, f"標記 {sorted(marked)} ≠ 樣板 {sorted(LLM_ONLY)}"


def test_saving_the_string_false_does_not_turn_it_on(client, auth_off, llm_state):
    """`"false"` 在 Python 是真值 —— 原樣存進去就等於把隱藏打開。"""
    from app.core.llm_settings import llm_settings
    llm_state(False, False)
    r = client.post("/admin/api/llm/settings", json={"hide_when_disabled": "false"})
    assert r.status_code == 200, r.text
    assert llm_settings.get()["hide_when_disabled"] is False
    client.post("/admin/api/llm/settings", json={"hide_when_disabled": True})
    assert llm_settings.get()["hide_when_disabled"] is True


# ---------------------------------------------------------------- 側欄與首頁

def test_disabled_without_hide_greys_them_on_sidebar_and_home(client, auth_off, llm_state):
    llm_state(False, False)
    html = client.get("/").text
    got = _anchor_classes(html)
    for tid in LLM_ONLY:
        classes = got.get(tid) or []
        assert any("sb-item" in c for c in classes), f"{tid} 不在側欄 —— 沒勾隱藏時要看得到"
        assert any("tool-card" in c for c in classes), f"{tid} 不在首頁 —— 沒勾隱藏時要看得到"
        assert all("is-locked" in c for c in classes), f"{tid} 沒有反灰：{classes}"
    assert "LLM 服務還沒啟用" in html, "反灰的工具要說得出為什麼（滑鼠移上去）"


def test_disabled_with_hide_removes_them_everywhere(client, auth_off, llm_state):
    llm_state(False, True)
    html = client.get("/").text
    got = _anchor_classes(html)
    for tid in LLM_ONLY:
        assert tid not in got, f"勾了「停用時一併隱藏」，{tid} 還在：{got.get(tid)}"
    # 反向對照：其他工具照樣在（不是整片不見）
    others = set(_registry()) - LLM_ONLY
    assert len(others & set(got)) >= len(others) - 8, "隱藏把不相干的工具也拿掉了"


def test_enabled_ignores_the_hide_option(client, auth_off, llm_state):
    """這個選項**只在停用時有作用** —— 啟用時勾著也不影響任何東西。"""
    llm_state(True, True)
    got = _anchor_classes(client.get("/").text)
    for tid in LLM_ONLY:
        classes = got.get(tid) or []
        assert classes, f"LLM 啟用時 {tid} 不見了 —— 隱藏只該在停用時有作用"
        assert not any("is-locked" in c for c in classes), f"LLM 啟用時 {tid} 仍反灰"


# ---------------------------------------------------------------- 各工具頁的 LLM 區塊

def _augment_div(html: str) -> str:
    m = re.search(r"<div class=\"llm-gate-augment[^>]*>", html)
    assert m, "頁面上找不到 LLM 加值區塊"
    return m.group(0)


@pytest.mark.parametrize("tid", sorted(AUGMENT))
def test_augment_block_is_grey_by_default_and_hidden_only_when_asked(client, auth_off,
                                                                    llm_state, tid):
    llm_state(False, False)
    html = client.get(f"/tools/{tid}/").text
    div = _augment_div(html)
    assert " hidden" not in div, f"{tid}：沒勾隱藏時 LLM 加值區塊不可以藏起來"
    assert "is-disabled" in div, f"{tid}：停用時要反灰"

    llm_state(False, True)
    html = client.get(f"/tools/{tid}/").text
    div = _augment_div(html)
    assert " hidden" in div, f"{tid}：勾了隱藏，LLM 加值區塊還看得到"
    assert 'class="llm-gate-aug-cb"' in html, (
        f"{tid}：藏起來時勾選框本身要留著 —— JS 會 getElementById 它，拿到 null 整段停住")

    llm_state(True, True)
    div = _augment_div(client.get(f"/tools/{tid}/").text)
    assert " hidden" not in div and "is-disabled" not in div, f"{tid}：啟用時不該藏也不該反灰"


def test_the_augment_scan_found_the_tools(client):
    """掃到 0 支跟「都合格」在輸出裡長得一樣。"""
    assert len(AUGMENT) >= 5, f"只掃到 {sorted(AUGMENT)}"


@pytest.mark.parametrize("path,marker", [
    ("/tools/pdf-fill/", 'id="useLlmReview"'),
    ("/tools/pdf-extract-text/", 'id="btnLlmReflow"'),
])
def test_tools_that_used_to_drop_the_llm_part_now_grey_it(client, auth_off, llm_state,
                                                         path, marker):
    """這兩支原本 LLM 一停用就整段不輸出 —— 跟「停用＝反灰」不一致。"""
    llm_state(False, False)
    html = client.get(path).text
    assert marker in html, f"{path}：停用、沒勾隱藏時要看得到（反灰）"
    tag = re.search(r"<[^>]*" + re.escape(marker) + r"[^>]*>", html).group(0)
    assert "disabled" in tag, f"{path}：停用時要不能按：{tag}"

    llm_state(False, True)
    assert marker not in client.get(path).text, f"{path}：勾了隱藏還看得到"

    llm_state(True, False)
    tag = re.search(r"<[^>]*" + re.escape(marker) + r"[^>]*>", client.get(path).text).group(0)
    assert "disabled" not in tag, f"{path}：啟用時不該反灰"


def test_einvoice_llm_button(client, auth_off, llm_state):
    def tag():
        html = client.get("/tools/einvoice-scan/").text
        return re.search(r"<button[^>]*id=\"btnLlmClassify\"[^>]*>", html, re.S).group(0)

    llm_state(False, False)
    t = tag()
    assert "disabled" in t and " hidden" not in t, t
    llm_state(False, True)
    assert " hidden" in tag(), "勾了隱藏，按鈕還看得到（元素要留著給 JS 抓，所以驗 hidden 屬性）"
    llm_state(True, True)
    t = tag()
    assert "disabled" not in t and " hidden" not in t, t


def test_ocr_llm_postprocessing_block(client, auth_off, llm_state):
    def tag():
        html = client.get("/tools/pdf-ocr/").text
        return re.search(r"<details class=\"po-llm-details\"[^>]*>", html).group(0)

    llm_state(False, False)
    assert " hidden" not in tag()
    llm_state(False, True)
    assert " hidden" in tag()
    llm_state(True, True)
    assert " hidden" not in tag()


# ---------------------------------------------------------------- 管理頁

def test_settings_page_offers_the_option_and_states_the_truth(client, auth_off, llm_state):
    llm_state(False, False)
    html = client.get("/admin/llm-settings").text
    assert 'id="hide_when_disabled"' in html
    assert "hide_when_disabled:" in html, "存檔的 JS 沒有把這個選項送出去"
    # 原本寫「會自動隱藏」—— 實際一直是反灰（介面承諾了沒做到的事）
    assert "自動隱藏" not in html
    assert "只在停用時有作用" in html, "要講清楚啟用時這個選項沒作用"


def test_settings_page_tool_count_is_computed(client, auth_off):
    """這一頁原本寫 10、側欄寫 12、實際 13 —— 三處三個數字。
    判準**自己算**（註冊過的工具 ∩ LLM 工具清單），不拿產品的算法來驗自己。"""
    from app.core.llm_settings import LLMSettingsManager
    registered = set(_registry())
    want = len([k for k in LLMSettingsManager.KNOWN_LLM_TOOLS if k["id"] in registered])
    html = client.get("/admin/llm-settings").text
    m = re.search(r"共 (\d+) 個工具", html)
    assert m, "找不到工具數那一句"
    assert int(m.group(1)) == want, f"頁面寫 {m.group(1)}，實際 {want}"
    assert want >= 10


def test_no_hard_coded_llm_tool_count_left_in_the_admin_nav():
    src = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    assert not re.search(r"\d+ 個工具的 LLM", src), "側欄管理區的說明又寫死了工具數"
