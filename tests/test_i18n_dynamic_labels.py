"""程式端產生的顯示字串（`tr(變數)`）也必須有英文。

`tests/test_i18n_catalog.py` 掃的是原始碼裡字面寫死的 `tr('…')`。但有一整類
標籤是**從 Python 的資料表來的** —— 去識別化的樣態名稱、紙張名稱、語言名稱 ——
樣板寫的是 `{{ tr(p.label) }}`，靜態掃描看不到那個鍵長什麼樣。

沒有這一支的話，新增一條樣態就會**無聲**地在英文介面上顯示中文：功能正常、
測試全綠、只有把介面切成英文的人看得到。

判準一律是「**從程式實算**出清單，逐條比對語系檔」，不寫死期望值 —— 寫死的話
這支測試自己就是下一個會漂掉的東西。
"""
from __future__ import annotations

import json
import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
#: 本來就沒有中文的標籤（`Email`、`UUID / GUID`）不需要譯文。
CJK = re.compile("[\u3400-\u9fff]")


def _catalog(locale: str = "en") -> dict:
    return json.loads(
        (REPO / "app" / "i18n" / f"{locale}.json").read_text(encoding="utf-8"))


def _locales() -> list[str]:
    """要驗哪些語言 —— **唯一來源是 `ui_locale.SUPPORTED`**。

    寫死 `en` 的話，加了第三種語言之後這整支守門會**安靜地只驗英文**。
    """
    from app.core.ui_locale import DEFAULT_LOCALE, SUPPORTED
    return [c for c in SUPPORTED if c != DEFAULT_LOCALE]


def _deident_labels() -> list[str]:
    from app.tools.doc_deident.patterns import CATALOG
    out: list[str] = []
    for p in CATALOG:
        for x in (p.group, p.label):
            if x and x not in out:
                out.append(x)
    return out


def _paper_labels() -> list[str]:
    from app.tools.pdf_nup.router import PAPERS  # type: ignore[attr-defined]
    return [p["label"] for p in PAPERS]


def _settings_export_labels() -> list[str]:
    from app.core.settings_export import CATEGORIES
    out: list[str] = []
    for c in CATEGORIES:
        for k in ("label", "desc"):
            v = c.get(k)
            if v and v not in out:
                out.append(v)
    return out


def _sys_deps_labels() -> list[str]:
    from app.core.sys_deps import collect_sys_deps
    out: list[str] = []
    for d in collect_sys_deps():
        for k in ("category", "impact"):
            v = d.get(k)
            if v and v not in out:
                out.append(v)
    return out


def _office_format_labels() -> list[str]:
    from app.core.office_formats import catalogue, _CURATED
    out: list[str] = []
    for f in catalogue():
        if f.name and f.name not in out:
            out.append(f.name)
        for t in f.targets:
            for v in (t.label, t.note):
                if v and v not in out:
                    out.append(v)
    # 對照表裡的也要翻 —— 這台機器缺某支濾鏡不代表客戶那台也缺。
    for v in _CURATED.values():
        for x in (v[3], v[4]):
            if x and x not in out:
                out.append(x)
    return out


def _ocr_language_labels() -> list[str]:
    from app.core.tessdata_manager import LANG_CATALOG
    out: list[str] = []
    for it in LANG_CATALOG:
        for k in ("name", "hint"):
            v = it.get(k)
            if v and v not in out:
                out.append(v)
    return out


def _upload_limit_labels() -> list[str]:
    from app.core.upload_limits import app_side_limits
    out: list[str] = []
    for r in app_side_limits():
        for k in ("label", "note"):
            v = r.get(k)
            if v and v not in out:
                out.append(v)
    return out


def _profile_section_titles() -> list[str]:
    from app.core.profile_manager import SECTIONS
    return [t for t, _keys in SECTIONS]


def _profile_field_labels() -> list[str]:
    """公司資料的**欄位標題**（`profile_manager.DEFAULT_FIELDS`）。

    分區標題早就在上面收了，欄位標題卻一直沒有 —— 於是表單自動填寫那一頁的
    公司資料卡在英文 / 日文介面下整片是中文（2026-09-16 用真瀏覽器逐頁掃
    抓到，en 45 條、ja 9 條）。

    **只收出貨的那份預設清單**：使用者可以自己改欄位名稱（改完存在
    `profile.json` 裡），那是**使用者資料**，永遠不會在語系檔裡 ——
    收進來的話「只要有人把欄位改成中文名字就紅」。顯示端是
    `tr(row.label)`，查不到就原樣顯示，所以自訂名稱照樣正確。
    """
    from app.core.profile_manager import DEFAULT_FIELDS
    return [lab for _k, lab, _v in DEFAULT_FIELDS]


def _notify_channel_labels() -> list[str]:
    import app.core.notify_channels as nc
    out: list[str] = []
    for name in dir(nc):
        v = getattr(nc, name)
        if not isinstance(v, dict):
            continue
        for d in v.values():
            if isinstance(d, dict):
                for k in ("label", "needs"):
                    x = d.get(k)
                    if x and x not in out:
                        out.append(x)
    return out


def _font_labels() -> list[str]:
    """只收**我們自己寫的**字型名稱。

    `list_fonts()` 也會列出管理員上傳的自訂字型（例如「業務用楷體」）——
    那是**使用者資料**，不是介面文字，永遠不會在語系檔裡，收進來這條守門
    就變成「只要有人上傳中文名字的字型就紅」。
    """
    from app.core.pdf_text_overlay import AVAILABLE_FONTS
    from app.core.font_catalog import list_fonts
    out = [name for _id, name, _p in AVAILABLE_FONTS]
    for f in list_fonts():
        if f.get("category") == "custom":      # 上傳的，名字由使用者決定
            continue
        v = f.get("label") or ""
        if v and v not in out:
            out.append(v)
    return out


def _scan_tool_column_labels() -> list[str]:
    """乘車證明 / 電子發票的欄位標籤（使用者可自訂顯示與匯出標題）。"""
    import app.tools.transit_proof.settings as tp
    import app.tools.einvoice_scan.settings as ei
    out: list[str] = []
    for mod in (tp, ei):
        for name in dir(mod):
            v = getattr(mod, name)
            if isinstance(v, list) and v and isinstance(v[0], dict):
                for d in v:
                    x = d.get("label")
                    if x and x not in out:
                        out.append(x)
    return out


def _llm_tool_labels() -> list[str]:
    """一定要讀**執行期的值**，不可以用正規式從原始碼抓。

    那幾句說明是用 Python 的隱式字串串接寫成多行的，正規式只會抓到第一段 ——
    於是譯文用「第一段」當鍵存進語系檔，執行期查的是「整句」，永遠對不上，
    而且守門還是綠的（它自己用同一個錯的鍵）。踩過一次。
    """
    from app.core.llm_settings import LLMSettingsManager
    out: list[str] = []
    for t in LLMSettingsManager.KNOWN_LLM_TOOLS:
        for k in ("name", "use"):
            v = t.get(k)
            if v and v not in out:
                out.append(v)
    return out


def _database_labels() -> list[str]:
    """系統狀態頁的資料庫清單（`db_health.DATABASES` 的 `label`）。

    畫面上是 JS 依 API 回傳的資料畫出來的 —— 樣板掃字面 `tr()` 的守門看不到
    （2026-09-14 日文版逐頁掃抓到「稽核記錄」「統編資料庫」兩條）。
    """
    import re as _re
    src = (REPO / "app" / "core" / "db_health.py").read_text(encoding="utf-8")
    return _re.findall(r'"label":\s*"([^"]+)"', src)


def _glossary_language_names() -> list[str]:
    """翻譯對照字典的語言下拉（`translate_doc._LANG_NAMES`）。

    同上：`lang_choices()` 算出來的資料，下拉是 JS 拼的，
    英文介面從這一頁上線起就一直顯示中文。
    """
    from app.tools.translate_doc.router import _LANG_NAMES
    return list(_LANG_NAMES.values())


def _admin_nav_labels() -> list[str]:
    """側欄管理區每一項的名稱與說明（`app/main.py` 的 `_NAV_SETTINGS_ALL`）。

    樣板端已經包了 `tr()`，缺的是**語系檔裡沒有那一條** —— 於是側欄在英文 /
    日文介面下原樣顯示中文，而且**掃字面 `tr('…')` 的守門看不到**
    （翻譯對照字典那一條從 v1.15.19 上線起就一直是中文，2026-09-14 逐頁掃
    才抓到）。
    """
    import app.main as M
    out: list[str] = []
    for it in M._NAV_SETTINGS_ALL:
        for k in ("name", "description"):
            v = it.get(k)
            if v and v not in out:
                out.append(v)
    for g in M._NAV_TOOL_GROUPS_ALL:
        v = g.get("name")
        if v and v not in out:
            out.append(v)
    return out


def _deident_doc_languages() -> list[str]:
    """去識別化的「文件語言」下拉（`patterns.DOC_LANGS`）。"""
    from app.tools.doc_deident.patterns import DOC_LANGS
    return [n for _c, n in DOC_LANGS]


def _straighten_dpi_notes() -> list[str]:
    """掃描修正的解析度說明（`_DPI_NOTES`）。"""
    import re as _re
    src = (REPO / "app" / "tools" / "doc_straighten"
           / "router.py").read_text(encoding="utf-8")
    body = src.split("_DPI_NOTES", 1)[1].split("}", 1)[0]
    return _re.findall(r'"([^"]+)"', body)


def _tool_lock_reasons() -> list[str]:
    """工具反灰時滑鼠移上去看到的那句話。

    **樣板寫的是 `tr(t.lock_reason)`** —— 掃字面 `tr('…')` 的守門看不到。
    原本只有「語言不符」一種原因、那句話直接寫死在兩個樣板裡；
    v1.15.94 加「外部服務還沒設定」這一種時改成由資料帶理由，
    於是它就掉進這一類了。
    """
    import app.main as m
    out = [m._LOCALE_LOCK_REASON]
    out += [reason for (_nr, reason, _u) in m._SETUP_CHECKS.values()]
    return out


@pytest.mark.parametrize("name,getter", [
    ("去識別化樣態", _deident_labels),
    ("設定備份的類別", _settings_export_labels),
    ("相依套件說明", _sys_deps_labels),
    ("辦公文件格式", _office_format_labels),
    ("OCR 語言", _ocr_language_labels),
    ("上傳上限說明", _upload_limit_labels),
    ("公司資料分區", _profile_section_titles),
    ("公司資料欄位", _profile_field_labels),
    ("通知管道", _notify_channel_labels),
    ("字型名稱", _font_labels),
    ("掃描工具欄位", _scan_tool_column_labels),
    ("LLM 工具清單", _llm_tool_labels),
    ("資料庫清單", _database_labels),
    ("對照字典的語言", _glossary_language_names),
    ("側欄管理區", _admin_nav_labels),
    ("去識別化的文件語言", _deident_doc_languages),
    ("掃描修正的解析度說明", _straighten_dpi_notes),
    ("工具反灰的理由", _tool_lock_reasons),
])
@pytest.mark.parametrize("locale", _locales())
def test_dynamic_labels_are_translated(locale: str, name: str, getter):
    cat = _catalog(locale)
    missing = [s for s in getter() if CJK.search(s) and s not in cat]
    assert not missing, f"{name} 有 {len(missing)} 條沒有 {locale}：{missing[:8]}"


# ---------------------------------------------------------------------------
# `<option>{{ 變數 }}</option>` —— 掃字面 `tr('…')` 的守門看不到的那一類
#
# 2026-09-14 加日文時一次抓到四處：去識別化的文件語言（**兩支工具各一份，
# 我只修了其中一支**）、掃描修正的解析度說明、登入頁的認證來源。
# 三處的共同點是「下拉的文字來自伺服器送來的資料」——
# **畫面上就是中文，而且沒有任何測試會紅**。
#
# 判準是「這個運算式有沒有走 `tr()`」，不是「這串字看起來像不像介面文字」。
# 真的是資料的（使用者名稱、工具 id、模型名稱、語言的自稱）列進豁免，
# **而且要寫理由** —— 沒有理由的豁免下一個人不敢動，就變成永久的洞。
# ---------------------------------------------------------------------------

#: `<option>` 裡**刻意不翻**的運算式。key 是樣板路徑尾段 + 運算式。
_OPTION_RAW_OK = {
    # 使用者名稱、工具 id、事件代號、模型名稱：都是資料不是介面文字
    ("admin_uploads.html", "u"): "使用者名稱",
    ("admin_uploads.html", "t"): "工具 id（ASCII）",
    ("admin_history.html", "u"): "使用者名稱",
    ("admin_audit.html", "e"): "事件代號（ASCII）",
    ("llm_settings.html", "settings.model"): "模型名稱",
    ("llm_settings.html", "_v"): "模型名稱",
    # 語言選項的**自稱**：「日本語」在英文介面下也要是「日本語」
    ("login.html", "name"): "語言的自稱，翻掉就選不到自己的語言",
    # 語音服務的「處理設定」代號（`meeting.balanced` 之類）：那是**對方的資料**，
    # 由 `GET /profiles` 給，翻掉就送不出去了
    ("admin_jtlw.html", "s.profile_id"): "對方的處理設定代號（ASCII）",
}

_OPTION_RE = re.compile(r"<option\b[^>]*>\s*\{\{\s*([^}]+?)\s*\}\}\s*</option>")


def test_option_labels_go_through_tr():
    bad: list[str] = []
    for p in sorted(REPO.joinpath("app").rglob("*.html")):
        for m in _OPTION_RE.finditer(p.read_text(encoding="utf-8")):
            expr = m.group(1).strip()
            if expr.startswith("tr(") or "|" in expr:
                continue
            if (p.name, expr) in _OPTION_RAW_OK:
                continue
            bad.append(f"{p.relative_to(REPO).as_posix()}: {{{{ {expr} }}}}")
    assert not bad, (
        "下拉選項的文字沒有走 tr()（英文 / 日文介面下會原樣顯示中文）：\n"
        + "\n".join(bad)
        + "\n真的是資料的話請加進 _OPTION_RAW_OK 並寫下理由。")


def test_the_option_exemptions_have_not_gone_stale():
    """豁免清單裡的每一條都還要真的存在。

    留著沒必要的豁免比沒有豁免更糟 —— 下一個人會以為那裡有一個已知的例外。
    """
    seen = set()
    for p in REPO.joinpath("app").rglob("*.html"):
        for m in _OPTION_RE.finditer(p.read_text(encoding="utf-8")):
            seen.add((p.name, m.group(1).strip()))
    stale = [k for k in _OPTION_RAW_OK if k not in seen]
    assert not stale, f"豁免清單過期了：{stale}"
