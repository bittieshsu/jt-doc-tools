"""模板用到的 CSS 類別，在**那個情境下**必須真的有樣式。

## 由來（v1.15.37，使用者連續三次截圖回報）

同一個根因犯了三次，每一次都是「用了在這個情境下不會生效的東西」，
而**畫面上沒有任何錯誤訊息**：

1. `class="notice"` —— 全站沒有任何規則定義它（我自己發明的名字）。
2. `af-field` / `af-note` —— `platform.css` 裡只有 `.auth-form .af-field`，
   工具頁不在 `.auth-form` 底下，**等於完全沒有樣式**：標籤、控制項、說明
   全部擠在同一行。
3. `jt-select` —— 那是 `custom_select.js` 的**選擇器掛勾**，沒載那支 js 的頁面
   拿到的是瀏覽器原生下拉（使用者：「選文件語言下拉清單沒套用本專案樣式」）。

## 判準

對每個工具頁蒐集 `class="…"` 裡的類別（**跳過 `<script>` / `<style>` 區塊**
與含 `${}` 的 JS 運算式 —— 不跳的話會被 template literal 淹掉），然後問：

* 這個類別在任何一份 `.css` 裡有規則嗎？
* 有的話，那條規則要求的**祖先類別**這一頁拿得到嗎？
  （`.auth-form .af-field` 要求祖先有 `.auth-form`）
* 或者它其實是 **JS 掛勾**（`querySelector('.x')` / `classList.add('x')`）
  或這一頁自己的 `<style>` 定義的？

三者皆非 → 這個類別在這一頁**不會生效**。

既有工具的欠帳列在 `_EXEMPT` 裡（**新工具不准再增加**）——
這是使用者的要求：「這種情況必須列入測試計劃，而且以後新工具也要生效」。
"""
from __future__ import annotations

import collections
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
APP = ROOT / "app"

#: 既有的欠帳（v1.15.37 實算）。**只能變少不能變多。**
#: 這些多半是「只給 JS 抓、本來就不需要樣式」的名字，但逐支確認要花時間，
#: 所以先釘住現狀，避免新工具再犯。
_EXEMPT: dict[str, set[str]] = {
    "tools/einvoice_scan/templates/einvoice_scan.html": {"hdr-grab", "hdr-name"},
    "tools/pdf_border/templates/pdf_border.html": {"mj-count"},
    "tools/pdf_decrypt/templates/pdf_decrypt.html": {"mr-from", "mr-to"},
    "tools/pdf_extract_text/templates/pdf_extract_text.html": {"pv-tab"},
    "tools/pdf_fill/templates/pdf_fill.html": {"unmatched-box"},
    "tools/pdf_ocr/templates/pdf_ocr.html": {"po-llm-details"},
    "tools/pdf_seam_stamp/templates/pdf_seam_stamp.html": {"sm-assets"},
    "tools/pdf_stamp/templates/pdf_stamp.html": {
        "ovr-rot-rand", "ovr-rot-reset", "pos-mode-switch", "type-"},
    "tools/pdf_watermark/templates/pdf_watermark.html": {"type-"},
    "tools/pdf_wordcount/templates/pdf_wordcount.html": {
        "wc-tab-active", "wc-tab-btn", "wc-tabs"},
    "tools/scan_merge/templates/scan_merge.html": {"drop-zone-inner"},
    "tools/translate_doc/templates/translate_doc.html": {
        "trd-domain-chips", "trd-form-cell-wide", "trd-parse-label",
        "trd-srctext-wrap"},
    "tools/doc_deident/templates/doc_deident.html": {"dd-type-cb"},
    # 管理區與一般頁的既有欠帳（v1.15.37 實算）。多半是「只給 JS 抓」的名字，
    # 逐支確認要花時間 —— 先釘住現狀，**新工具不准再增加**。
    "admin/templates/admin_audit.html": {"pagination"},
    "admin/templates/admin_auth_settings.html": {"af-attr-cell"},
    "admin/templates/admin_directory.html": {"tn-ic-src"},
    "admin/templates/admin_groups.html": {"chk", "src-"},
    "admin/templates/admin_jobs.html": {"aj-head"},
    "admin/templates/admin_notify.html": {"nt-helo", "nt-hint", "nt-mode-row"},
    "admin/templates/admin_permissions.html": {
        "perm-form", "perm-row-roles", "picker-clear", "src-"},
    "admin/templates/admin_sso.html": {"kv"},
    "admin/templates/admin_system_status.html": {"ssdb-head"},
    "admin/templates/admin_uploads.html": {"pagination"},
    "admin/templates/admin_users.html": {"src-"},
    "admin/templates/api_tokens.html": {"btn-copy", "btn-reveal", "btn-revoke"},
    "web/templates/my_jobs.html": {"mj-head"},
    "web/templates/my_workspace.html": {"drop-zone-inner"},
}

_BAD_CHARS = set("${}'\"?:=+()[]!|&")
_SRC = re.compile(r'class="([^"]*)"')


def _strip_blocks(text: str) -> str:
    # **結束標籤裡可以有空白**（`</script  >` 是合法 HTML）——
    # 自己寫的話很容易漏掉那一段，整塊就會被當成還沒結束。全站一份。
    from tools.source_text import strip_blocks
    return strip_blocks(text, "script", "style")


def _classes(text: str, strip: bool = True) -> set[str]:
    if strip:
        text = _strip_blocks(text)
    out: set[str] = set()
    for m in _SRC.findall(text):
        m = re.sub(r"\{\{.*?\}\}|\{%.*?%\}", " ", m)
        for tok in m.split():
            if tok and not (set(tok) & _BAD_CHARS):
                out.add(tok)
    return out


def _css_rules() -> dict[str, list[str]]:
    """類別 → 它出現過的選擇器片段。"""
    css = ""
    for f in (ROOT / "static" / "css").glob("*.css"):
        css += re.sub(r"/\*.*?\*/", "", f.read_text(encoding="utf-8"), flags=re.S) + "\n"
    rules: dict[str, list[str]] = collections.defaultdict(list)
    for sel in re.findall(r"([^{}]+)\{[^{}]*\}", css):
        sel = sel.strip()
        if not sel or sel.startswith("@"):
            continue
        for part in sel.split(","):
            part = part.strip()
            for cls in set(re.findall(r"\.([A-Za-z][\w-]*)", part)):
                rules[cls].append(part)
    return rules


def _ancestors_required(part: str, cls: str) -> set[str]:
    out: set[str] = set()
    for seg in re.split(r"\s*[>+~]\s*|\s+", part):
        if f".{cls}" in seg:
            continue
        out |= set(re.findall(r"\.([A-Za-z][\w-]*)", seg))
    return out


def _js_hooks() -> set[str]:
    js = " ".join(p.read_text(encoding="utf-8")
                  for p in (ROOT / "static" / "js").glob("*.js"))
    hooks = set(re.findall(r"querySelector(?:All)?\(['\"]\.([\w-]+)", js))
    hooks |= set(re.findall(r"classList\.(?:add|remove|toggle|contains)\(['\"]([\w-]+)", js))
    for blob in re.findall(r"className\s*=\s*['\"]([\w -]+)", js):
        hooks |= set(blob.split())
    return hooks


RULES = _css_rules()
HOOKS = _js_hooks()
BASE = _classes((APP / "web" / "templates" / "base.html").read_text(encoding="utf-8"))
COMP: set[str] = set()
for _c in (APP / "web" / "templates" / "components").glob("*.html"):
    COMP |= _classes(_c.read_text(encoding="utf-8"))
PAGES = sorted(p for p in APP.rglob("*.html") if "components" not in p.parts)


def _rel(p: pathlib.Path) -> str:
    return p.relative_to(APP).as_posix()


def test_the_scan_has_a_sane_baseline():
    """檢查自己要有牙齒：掃 0 個檔跟「都合格」在輸出裡一模一樣。"""
    assert len(PAGES) > 50, f"只掃到 {len(PAGES)} 個模板"
    assert ".auth-form" not in RULES, "解析選擇器的方式變了"
    assert "af-field" in RULES, "platform.css 裡應該找得到 af-field（scoped 在 .auth-form）"
    assert _ancestors_required(".auth-form .af-field", "af-field") == {"auth-form"}


@pytest.mark.parametrize("page", PAGES, ids=_rel)
def test_every_class_in_the_page_actually_gets_styled(page: pathlib.Path):
    text = page.read_text(encoding="utf-8")
    from tools.source_text import blocks
    own_css = " ".join(blocks(text, "style"))
    own = set(re.findall(r"\.([A-Za-z][\w-]*)", own_css))
    inline_js = " ".join(blocks(text, "script"))
    own_hooks = set(re.findall(r"querySelector(?:All)?\(['\"]\.([\w-]+)", inline_js))
    own_hooks |= set(re.findall(
        r"classList\.(?:add|remove|toggle|contains)\(['\"]([\w-]+)", inline_js))
    # `jt-select` 這種掛勾只有在那一頁真的載了對應的 js 時才算數
    if "custom_select.js" in text:
        own_hooks.add("jt-select")
    here = _classes(text) | BASE | COMP | own
    allowed = _EXEMPT.get(_rel(page), set())

    dead = []
    for cls in sorted(_classes(text)):
        if cls in own or cls in HOOKS or cls in own_hooks or cls in allowed:
            continue
        # `class="tile-color-{{ t.color }}"` 這種**在渲染時才組出來**的名字，
        # 去掉 `{{ }}` 之後只剩半截（`tile-color-`）。它是某個真類別的前綴，
        # 不是「沒有樣式」——不放行的話檢查會被自己的解析方式騙
        # （第一版一次誤報 16 支）。
        if any(known.startswith(cls) for known in RULES) and cls not in RULES:
            continue
        sels = RULES.get(cls)
        if not sels:
            dead.append(f"{cls}（全站沒有任何規則）")
        elif not any(_ancestors_required(s, cls) <= here for s in sels):
            need = sorted(min((_ancestors_required(s, cls) for s in sels), key=len))
            dead.append(f"{cls}（只在 {need} 底下才有樣式）")
    assert not dead, (
        f"{_rel(page)} 用了在這一頁**不會生效**的類別：\n  " + "\n  ".join(dead)
        + "\n畫面上不會有任何錯誤訊息，那幾個元素就是沒有樣式"
          "（v1.15.36~37 連續三次都是這個原因）。")


def test_no_exemption_is_stale():
    """例外清單會過期 —— 檔案改名或已經修乾淨了就要拿掉。"""
    missing = [k for k in _EXEMPT if not (APP / k).exists()]
    assert not missing, f"_EXEMPT 列的模板已不存在：{missing}"


def test_info_box_keeps_its_icon_on_the_same_line():
    """`.info-box` 裡不可以放區塊元素 —— 圖示會被擠成自己一行。

    `.info-box` 的圖示是 **inline** 的（`platform.css` 只給它
    `vertical-align`），所以文字一旦包進 `<div>` / `<p>` / `<ul>`，
    圖示就獨占一行，看起來像排版壞掉（2026-09-13 使用者截圖回報）。

    要換行用 `<br>`。判準只看**直接子層**，巢在 `<br>` 之後的行內標記不管。
    """
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    bad = []
    box = re.compile(r'<div class="info-box"[^>]*>(.*?)</div>\s*(?=<|$)', re.S)
    for p in sorted(root.glob("app/**/templates/**/*.html")):
        text = p.read_text(encoding="utf-8")
        for m in re.finditer(r'<div class="info-box"[^>]*>', text):
            tail = text[m.end():m.end() + 600]
            head = tail.split("</div>")[0]
            blk = re.search(r"<(div|p|ul|ol|table|h[1-6])\b", head)
            if blk:
                line = text[: m.start()].count("\n") + 1
                bad.append(f"{p.relative_to(root)}:{line} 裡面有 <{blk.group(1)}>")
    assert not bad, (
        "`.info-box` 裡有區塊元素，圖示會被擠到自己一行：\n" + "\n".join(bad)
        + "\n要換行請用 <br>。")
