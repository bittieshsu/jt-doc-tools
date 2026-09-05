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


def _catalog() -> dict:
    return json.loads((REPO / "app" / "i18n" / "en.json").read_text(encoding="utf-8"))


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


@pytest.mark.parametrize("name,getter", [
    ("去識別化樣態", _deident_labels),
    ("設定備份的類別", _settings_export_labels),
    ("相依套件說明", _sys_deps_labels),
    ("辦公文件格式", _office_format_labels),
    ("OCR 語言", _ocr_language_labels),
    ("上傳上限說明", _upload_limit_labels),
    ("公司資料分區", _profile_section_titles),
    ("通知管道", _notify_channel_labels),
    ("字型名稱", _font_labels),
    ("掃描工具欄位", _scan_tool_column_labels),
    ("LLM 工具清單", _llm_tool_labels),
])
def test_dynamic_labels_have_english(name: str, getter):
    cat = _catalog()
    missing = [s for s in getter() if CJK.search(s) and s not in cat]
    assert not missing, f"{name} 有 {len(missing)} 條沒有英文：{missing[:8]}"
