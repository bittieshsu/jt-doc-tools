"""語系檔與樣板的一致性守門。

**最高原則：加 i18n 不可以改壞現有功能。** 這支測試的第一條就是
「繁體中文底下 `t()` 原樣回傳」—— 也就是**中文使用者永遠不受語系檔影響**，
就算語系檔缺檔、壞檔、寫錯，中文畫面也不會變。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from app.core.i18n import CATALOG_DIR, catalog, translate
from app.core.ui_locale import DEFAULT_LOCALE, SUPPORTED

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = list((ROOT / "app").rglob("*.html"))
_T_CALL = re.compile(r"\{\{\s*tr\('([^']+)'\)\s*\}\}")


def _keys_in_templates() -> set[str]:
    out: set[str] = set()
    for p in TEMPLATES:
        out |= set(_T_CALL.findall(p.read_text(encoding="utf-8")))
    return out


def test_chinese_is_never_affected_by_the_catalog():
    """繁中一律原樣回傳 —— 語系檔壞掉也不可以影響中文畫面。"""
    for text in ("我的作業", "設定", "任何沒收錄的字"):
        assert translate(text, DEFAULT_LOCALE) == text
        assert translate(text, None) == text


def test_missing_translation_falls_back_to_chinese():
    """查不到就回退中文 —— 不可以出現空白按鈕或把 key 露在畫面上。"""
    assert translate("這句故意沒有翻譯", "en") == "這句故意沒有翻譯"


def test_every_template_key_is_translated():
    keys = _keys_in_templates()
    assert keys, "樣板裡應該至少有一個 tr('…')"
    missing = sorted(k for k in keys if k not in catalog("en"))
    assert missing == [], f"這些字串還沒有英文：{missing}"


def test_catalog_entries_are_all_traditional_chinese_keys():
    """key 必須是**繁體中文原文**（gettext 的 msgid 做法）。

    用符號 key（`nav.jobs`）的話，`test_taiwan_terminology.py` 那類
    「掃描使用者看得到的文字」的守門會變成永遠綠燈的假測試。
    """
    # 中文標點也算 —— `<b>A</b>，<b>B</b>` 中間那個逗號本身就是要翻的片段
    cjk = re.compile(r"[㐀-鿿、。，：；！？（）「」《》…—]")
    for k, v in catalog("en").items():
        assert cjk.search(k), f"key 不是中文原文：{k!r}"
        # 譯文只擋**漢字**：`…` 這類標點在英文裡也用得到（"Search tools…"），
        # 用同一個寬鬆的字元集去擋會誤報。
        assert not re.search(r"[㐀-鿿]", v), f"英文譯文裡不該有中文：{k!r} -> {v!r}"


def test_domain_data_modules_never_use_the_translation_helper():
    """**表單標籤 / 會計科目 / 去識別化式子這些中文是資料，翻掉會壞功能。**

    翻掉「統一編號」表單自動填寫就抓不到欄位，而且完全無聲。

    守的是「**這些模組不可以碰翻譯**」，不是「語系檔裡不可以出現某些字」——
    後者我先寫過，是錯的判準：
      * 用關鍵字擋 → 去識別化的工具說明裡本來就會提到「統編」「身分證」，誤報
      * 用「字串是否來自資料模組」擋 → 「帳號」「其他」同時是登入頁的欄位標籤
        與表單欄位關鍵字，照樣誤報

    真正的風險是**有人把資料的用法包進 `tr()`**。語系檔裡剛好有同樣的字不會
    造成任何影響 —— 那些模組根本不會去查表。
    """
    modules = (
        "app/core/pdf_form_detect.py",
        "app/core/pdf_layout.py",
        "app/core/same_as_ref.py",
        "app/tools/einvoice_scan/accounting_classifier.py",
        "app/tools/doc_deident/patterns.py",
    )
    bad = []
    for rel in modules:
        p = ROOT / rel
        if not p.exists():
            continue
        src = p.read_text(encoding="utf-8")
        # `s.translate(_CJK_FOLD)` 是 Python 內建的 str.translate，不是我們的
        # 取字函式 —— 前面有點就不算（第一版沒排除，誤報 pdf_form_detect）。
        if re.search(r"\bfrom .*\bi18n\b|\bimport i18n\b|(?<![.\w])translate\(", src):
            bad.append(rel)
    assert bad == [], f"這些模組的中文是資料，不可以走翻譯：{bad}"


@pytest.mark.parametrize("locale", [loc for loc in SUPPORTED if loc != DEFAULT_LOCALE])
def test_catalog_file_is_valid_json(locale: str):
    path = CATALOG_DIR / f"{locale}.json"
    assert path.exists(), f"缺語系檔：{path}"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict) and data


def test_the_helper_name_is_not_shadowed_by_loop_variables():
    """取字函式**不可以叫 `t`**。

    側欄那些 `{% for t in g.tools %}` 會把同名的全域函式蓋掉，迴圈裡一呼叫就
    「'dict' object is not callable」—— 實際踩到，整頁 500。這裡釘住兩件事：
    註冊的名字是 `tr`，而且沒有樣板拿 `tr` 當迴圈變數。
    """
    from app.main import templates

    assert "tr" in templates.env.globals
    assert "t" not in templates.env.globals, "叫 t 會被 {% for t in ... %} 蓋掉"
    bad = []
    for p in TEMPLATES:
        s = p.read_text(encoding="utf-8")
        # 只看樣板語法，JS 裡的 `const tr = ...` 是另一個命名空間，不衝突
        if re.search(r"\{%\s*(for|set)\s+tr\b", s):
            bad.append(str(p.relative_to(ROOT)))
    assert bad == [], f"這些樣板拿 tr 當變數名，會蓋掉取字函式：{bad}"


def test_translation_keeps_the_trailing_colon_or_ellipsis():
    """原文以「：」或「…」結尾，譯文也要 —— 那是「後面還要再接東西」的訊號。

    程式碼常寫成 `tr('分析失敗：') + err`，譯文如果沒有那個冒號，畫面就變成
    「Analysis failedsomething went wrong」黏成一團。這條也順便擋掉**整批譯文
    對錯鍵**（實際踩過兩次：合併譯文時用「索引」對，清單順序一變就整段錯位）。
    """
    cat = catalog("en")
    # **只看短字串**。長句子以「：」結尾時，後面接的是另一個元素，英文很自然
    # 會以 "from" / "Download" 這種詞收尾而不需要冒號 —— 對那些誤報的話，
    # 這條守門就會被當成雜訊忽略（本專案的老問題）。
    bad = [(k, v) for k, v in cat.items()
           if k and len(k) <= 20 and k[-1] in "：…" and v and v[-1] not in ": ….'"]
    assert not bad, ("原文以冒號 / 刪節號結尾但譯文沒有：\n  "
                     + "\n  ".join(f"{k[:34]!r} -> {v[:40]!r}" for k, v in bad[:6]))


_JS_BLOCK = re.compile(r"<script\b[^>]*>(.*?)</script>", re.S | re.I)
_JS_CALL = re.compile(r"(?<![\w.])tr\((['\"])((?:(?!\1)[^\\])*)\1\)")


def _keys_in_scripts() -> set[str]:
    """樣板 `<script>` 裡的 `tr('…')`。

    這些是**執行期**才求值的（按鈕文字、錯誤訊息），走 `static/js/i18n.js`
    的 `window.tr`，跟樣板端的 `{{ tr() }}` 是兩條路，要分開收。
    """
    out: set[str] = set()
    for p in TEMPLATES:
        for m in _JS_BLOCK.finditer(p.read_text(encoding="utf-8")):
            out |= {k.group(2) for k in _JS_CALL.finditer(m.group(1))}
    return out


def test_every_js_key_is_translated():
    cat = catalog("en")
    missing = sorted(k for k in _keys_in_scripts() if k not in cat)
    assert not missing, f"JS 裡有 {len(missing)} 條沒翻：{missing[:6]}"


def test_js_keys_never_contain_template_syntax():
    """`tr('{{ icon(...) }} 重新偵測')` 這種是錯的。

    Jinja 在**伺服器端**先渲染，執行期 `tr()` 拿到的是渲染後的 HTML，
    永遠查不到翻譯 —— 而且完全無聲（原樣回傳中文）。圖示要留在字串外面：
    `'{{ icon(...) }} ' + tr('重新偵測')`。
    """
    bad = [k for k in _keys_in_scripts() if "{{" in k or "{%" in k]
    assert not bad, f"JS 的 tr() 鍵裡有樣板語法：{bad}"
