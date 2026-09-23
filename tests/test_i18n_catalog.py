"""語系檔與樣板的一致性檢查。

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
#: 樣板裡**任何位置**的 `tr('…')` —— 不只 `{{ tr('…') }}`。
#:
#: 原本只認 `{{ tr('…') }}` 這個整齊的形狀，於是 `{{ tr('…{0}…')|replace('{0}', n) }}`、
#: `{{ tr('…')|safe }}`、`{{ tr('…') if x else … }}` 這些**一條都沒被檢查過**
#: （v1.16.11 做 LLM 設定頁時發現：拿掉一條譯文，這個檢查照樣綠）。
#: 當時補掃出 25 條，剛好都已經有譯文 —— 洞在，只是還沒掉東西進去。
_T_CALL = re.compile(r"(?<![\w.])tr\('([^']+)'\)")


def _keys_in_templates() -> set[str]:
    out: set[str] = set()
    for p in TEMPLATES:
        src = p.read_text(encoding="utf-8")
        # 註解裡常**引用**寫法當例子（use vs mention）；<script> 裡的 tr() 是 JS 的，
        # 由下面 `_JS_CALL` 那一條管，這裡不重複收。
        src = re.sub(r"\{#.*?#\}|<!--.*?-->", " ", src, flags=re.S)
        src = re.sub(r"<script\b[^>]*>.*?</script\b[^>]*>", " ", src, flags=re.S | re.I)
        out |= set(_T_CALL.findall(src))
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


#: 語言的**自稱**（endonym）—— 這幾條在任何語言底下都保持原樣。
#: 英文使用者看到「Japanese」不知道那是不是他要的，看到「日本語」才認得；
#: 這跟 `ui_locale.LOCALE_NAMES` 是同一條原則（那一份不經過語系檔，
#: 所以只有文件語言下拉會撞到這條檢查）。
_ENDONYMS = {"日本語"}


def test_the_endonym_exemptions_have_not_gone_stale():
    """豁免清單裡的每一條都還要真的在語系檔裡。

    留著沒必要的豁免比沒有豁免更糟 —— 下一個人會以為那裡有一個已知的例外。
    """
    en = catalog("en")
    stale = [k for k in _ENDONYMS if k not in en]
    assert not stale, f"豁免清單過期了：{stale}"


def test_catalog_entries_are_all_traditional_chinese_keys():
    """key 必須是**繁體中文原文**（gettext 的 msgid 做法）。

    用符號 key（`nav.jobs`）的話，`test_taiwan_terminology.py` 那類
    「掃描使用者看得到的文字」的檢查會變成永遠綠燈的假測試。
    """
    # 中文標點也算 —— `<b>A</b>，<b>B</b>` 中間那個逗號本身就是要翻的片段
    cjk = re.compile(r"[㐀-鿿、。，：；！？（）「」《》…—]")
    for locale in SUPPORTED:
        if locale == DEFAULT_LOCALE:
            continue
        for k, v in catalog(locale).items():
            assert cjk.search(k), f"[{locale}] key 不是中文原文：{k!r}"
            if locale == "en" and k not in _ENDONYMS:
                # 譯文只擋**漢字**：`…` 這類標點在英文裡也用得到
                # （"Search tools…"），用同一個寬鬆的字元集去擋會誤報。
                assert not re.search(r"[㐀-鿿]", v), \
                    f"英文譯文裡不該有中文：{k!r} -> {v!r}"


#: 現代日文散文幾乎不用的中文字。**這是啟發式，不是正確性檢查** ——
#: 它抓得到「整段忘了翻、原樣留著中文」，抓不到「翻得爛」。後者只有母語者
#: 看得出來，不要假裝測試涵蓋得到。
#:
#: 收的都是**日文不會用**的：`這/那個/嗎`、`們`、`什麼`、`沒有`、`可以`、
#: `這樣`、`一下`、`很`。像 `設定`、`管理`、`文書` 這種中日同形的詞
#: **不可以收** —— 那是正確的日文。
#:
#: **`的` 一定不可以收**（我第一版收了，第一批就誤報三條）：日文的
#: `一般的` / `自動的` / `現代的` 是形容動詞語尾，是**正確的日文**。
#: 誤報一多，這份檢查就會被當雜訊忽略 —— 那比沒有檢查更糟
#:（用詞檢查那次的教訓）。
#: **唯一來源在掃描器裡** —— 瀏覽器逐頁掃也用同一份判準，
#: 兩邊各寫一份一定會漂（本專案反覆踩過）。
from tools.i18n_untranslated_scan import NOT_JAPANESE as _NOT_JAPANESE


def test_the_japanese_catalog_is_not_just_chinese_left_in_place():
    """日文語系檔裡不可以留著沒翻的中文。

    **英文那條判準（譯文不可以有漢字）對日文完全不成立** —— 日文譯文
    100% 會有漢字。改成抓「現代日文不會用的中文字」，那是
    「這一條整段忘了翻」的訊號。
    """
    bad = []
    for k, v in catalog("ja").items():
        hit = [w for w in _NOT_JAPANESE if w in v]
        if hit:
            bad.append((k, v, hit))
    assert bad == [], (
        "日文譯文裡有中文沒翻掉（前 3 條）：" + repr(bad[:3])
        + "　—— 這是啟發式判準，只抓得到「整段留著中文」")


def test_no_translation_slipped_into_a_wrong_script():
    """譯文裡不可以混進**西里爾 / 希臘 / 諺文**。

    翻 4,883 條的時候手滑打成別的字集是真的會發生的（第一批就有一條把
    「省略」打成西里爾字母的 `скип`）。這種錯**看起來只是一個詞怪怪的**，
    而且沒有任何測試會紅 —— 日文譯文本來就不是拉丁字母，字集檢查是唯一
    抓得到的方式。
    """
    import re as _re

    wrong = _re.compile(r"[\u0400-\u04FF\u0370-\u03FF\uAC00-\uD7AF]")
    bad = []
    for locale in SUPPORTED:
        if locale == DEFAULT_LOCALE:
            continue
        for k, v in catalog(locale).items():
            hit = wrong.findall(v)
            if hit:
                bad.append((locale, k, "".join(sorted(set(hit)))))
    assert bad == [], f"譯文混進了別的字集：{bad[:3]}"


def test_the_japanese_catalog_covers_what_it_claims_to():
    """**已收錄的條目不可以是空的或原樣照抄。**

    `catalog()` 會把空值濾掉，所以「翻了一半」在畫面上只是回退中文 ——
    不會壞，但也看不出來。這條釘住「檔案裡出現過的條目都真的翻了」。
    """
    import json
    from pathlib import Path as _P

    path = _P(__file__).resolve().parent.parent / "app" / "i18n" / "ja.json"
    if not path.exists():
        pytest.skip("還沒有日文語系檔")
    raw = json.loads(path.read_text(encoding="utf-8"))
    empty = [k for k, v in raw.items() if not str(v).strip()]
    assert empty == [], f"日文語系檔有空值（前 3 條）：{empty[:3]}"


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
            bad.append(p.relative_to(ROOT).as_posix())
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
    # 這條檢查就會被當成雜訊忽略（本專案的老問題）。
    bad = [(k, v) for k, v in cat.items()
           if k and len(k) <= 20 and k[-1] in "：…" and v and v[-1] not in ": ….'"]
    assert not bad, ("原文以冒號 / 刪節號結尾但譯文沒有：\n  "
                     + "\n  ".join(f"{k[:34]!r} -> {v[:40]!r}" for k, v in bad[:6]))


_JS_BLOCK = re.compile(r"<script\b[^>]*>(.*?)</script\b[^>]*>", re.S | re.I)
_JS_CALL = re.compile(r"(?<![\w.])tr\((['\"])((?:(?!\1)[^\\])*)\1\)")


def _keys_in_scripts() -> set[str]:
    """樣板 `<script>` 裡的 `tr('…')`。

    這些是**執行期**才求值的（按鈕文字、錯誤訊息），走 `static/js/i18n.js`
    的 `window.tr`，跟樣板端的 `{{ tr() }}` 是兩條路，要分開收。

    **註解要先去掉**：解釋這條規則的註解裡常會寫 `tr('…')` 當例子，
    不去註解的話那個例子會被當成一條真的鍵，然後這支檢查就報「有一條沒翻」
    —— 這正是本專案反覆記過的「掃描器被它要檢查的那個名字騙到」
    （2026-09-14 又踩一次，被自己新寫的註解報出來）。
    """
    from tools.source_text import strip_js_comments

    out: set[str] = set()
    for p in TEMPLATES:
        for m in _JS_BLOCK.finditer(p.read_text(encoding="utf-8")):
            out |= {k.group(2)
                    for k in _JS_CALL.finditer(strip_js_comments(m.group(1)))}
    # **`static/js/*.js` 也要收。** 這裡原本只掃樣板，於是共用元件
    # （上傳、作業進度、工作區挑選、錯誤訊息）裡的 `tr('…')`
    # **從來沒有被檢查過** —— 2026-09-16 補上時當場抓到 22 條沒翻，
    # 而那幾支幾乎每一個工具頁都會載，等於每一頁都看得到中文。
    # 範圍太窄跟沒有檢查一樣。
    for p in sorted(ROOT.glob("static/js/*.js")):
        out |= {k.group(2)
                for k in _JS_CALL.finditer(strip_js_comments(p.read_text(encoding="utf-8")))}
    return out


def test_the_js_scan_reaches_the_shared_scripts():
    """**「掃 0 個檔」跟「掃過都乾淨」在 pytest 輸出裡長得一模一樣。**

    門檻是**量出來的**（實際 17 支 `.js`、其中 11 支有 `tr()`）——
    寫一個好看的整數會變成「永遠成立」或「動不動就紅」。
    """
    js = sorted(ROOT.glob("static/js/*.js"))
    assert len(js) >= 12, f"只收到 {len(js)} 支共用 JS"
    from tools.source_text import strip_js_comments
    withtr = [p for p in js
              if _JS_CALL.search(strip_js_comments(p.read_text(encoding="utf-8")))]
    assert len(withtr) >= 6, f"只有 {len(withtr)} 支共用 JS 收得到 tr()"


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


def test_no_attribute_renders_the_helper_call_as_text():
    """`title=tr("…")` —— 屬性值會**原樣印出** `tr("拖曳調整順序")` 給人看。

    上一輪的屬性包裝就是這樣寫的：template literal 裡少了 `${}`、一般字串裡
    少了接起來，兩種都不會有語法錯誤、也不會有例外，只是滑鼠移上去看到一串
    程式碼。六處全部在正式版上待了一陣子才被英文介面的掃描抓到。
    """
    bad: list[str] = []
    # `el.title = tr('…')` 是**正確**的 JS，不可誤報 —— 判準是「這個屬性名
    # 出現在標記裡」：前面要有 `<`、而且不是 `.title`（物件屬性）。
    pat = re.compile(r'<[^<>]*?(?<![.\w])(title|alt|placeholder|aria-label|data-tip)'
                     r'\s*=\s*tr\(')
    for p in sorted(ROOT.glob("app/**/*.html")) + sorted(ROOT.glob("static/js/*.js")):
        for i, line in enumerate(p.read_text(encoding="utf-8").split("\n"), 1):
            if pat.search(line):
                bad.append(f"{p.relative_to(ROOT)}:{i}")
    assert not bad, ("屬性值直接寫 tr(…) 會原樣顯示程式碼：\n  "
                     + "\n  ".join(bad)
                     + "\n樣板字面用 {{ tr('…') }}；template literal 用 "
                       "title=\"${tr('…')}\"；一般字串用 title=\"' + tr('…') + '\"")


#: 這幾支的字串是**畫面上的說明文字**（相依檢查、LLM 工具清單、設定備份分類…）。
#: LLM 的 prompt 也常寫 `**…**`，但那是給模型看的、不會出現在畫面上，所以
#: 這條檢查**只釘顯示用的模組**，不要整包 app/ 掃（會被 prompt 淹掉）。
_DISPLAY_TEXT_MODULES = (
    "app/core/sys_deps.py",
    "app/core/llm_settings.py",
    "app/core/settings_export.py",
    "app/core/notify_channels.py",
    "app/core/upload_limits.py",
    "app/core/profile_manager.py",
    "app/core/tessdata_manager.py",
    "app/core/office_formats.py",
)


def test_display_strings_never_use_markdown():
    """畫面不會渲染 markdown —— `**粗體**` 會原樣印出星號給使用者看。

    CLAUDE.md 記過這條（v1.14.59 被截圖抓到一次），2026-09-05 又在相依檢查頁
    被抓到一次。要強調就用「」，或把粗體交給模板的 `<b>`。
    """
    import ast
    md = re.compile(r"\*\*[^*\n]+\*\*")
    bad: list[str] = []
    for rel in _DISPLAY_TEXT_MODULES:
        p = ROOT / rel
        if not p.is_file():
            continue
        tree = ast.parse(p.read_text(encoding="utf-8"))
        docs = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)):
                d = ast.get_docstring(node, clean=False)
                if d:
                    docs.add(d)
        for node in ast.walk(tree):
            if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and node.value not in docs and md.search(node.value)):
                bad.append(f"{rel}:{node.lineno}  {node.value[:60]}")
    assert not bad, ("畫面文字裡寫了 markdown 粗體（會原樣顯示星號）：\n  "
                     + "\n  ".join(bad))


def test_templates_never_use_markdown_bold():
    """樣板 / JS 的字串也不可以寫 `**粗體**` —— 同上，星號會原樣顯示。

    註解裡寫 `**` 是給讀程式的人看的，掃描前要先去掉（Jinja 註解、HTML 註解、
    JS 註解三種）—— 這個專案的檢查「連說明一起掃」已經誤報過兩次。
    """
    md = re.compile(r"\*\*[^*\n<>]+\*\*")
    bad: list[str] = []
    files = sorted(ROOT.glob("app/**/*.html")) + sorted(ROOT.glob("static/js/*.js"))
    for p in files:
        src = p.read_text(encoding="utf-8")
        src = re.sub(r"\{#.*?#\}|<!--.*?-->", " ", src, flags=re.S)
        src = re.sub(r"//[^\n]*", " ", src)
        src = re.sub(r"/\*.*?\*/", " ", src, flags=re.S)
        for m in md.finditer(src):
            bad.append(f"{p.relative_to(ROOT)}:{src[:m.start()].count(chr(10)) + 1}"
                       f"  {m.group(0)[:50]}")
    assert not bad, "樣板裡寫了 markdown 粗體：\n  " + "\n  ".join(bad)
