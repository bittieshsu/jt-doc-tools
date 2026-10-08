"""政府公開資料「要匯入的項目」的位階篩選：國發會的行政規則寫的是代碼
（`05:行政規則§159,II,1`），原樣列成篩選按鈕看不懂（2026-10-08 使用者截圖：「這顯示怪怪的」）。

要守住的事：

* 篩選、清單每一項底下那一行，用的都是整理過的名稱（`機關內部規定`、`解釋性規定、裁量基準`）；
  依據（行政程序法第 159 條第 2 項第 N 款）放在 `level_notes` 給滑鼠提示。
* 篩選照整理過的名稱比對 —— 畫面送回來的就是那個名稱。
* 法律、命令、憲法這種本來就看得懂的照原樣。
* 寫進知識庫內文的那一行要完整（名稱 ＋ 依據），模型讀得懂。
* 已經下載過的清單不必重新下載（整理在讀的時候做，不改存著的檔）。
"""
from __future__ import annotations

import json

from app.core.kb import gov, law_text
from tests._kb_support import kb_isolated  # noqa: F401
from tests.test_kb_gov_import import (  # noqa: F401
    NDC_XML, _download, _point, allow_fake, g, srv)


def test_level_label_reads_the_code():
    assert law_text.level_label("05:行政規則§159,II,1") == "機關內部規定"
    assert law_text.level_label("06:行政規則§159,II,2") == "解釋性規定、裁量基準"
    assert law_text.level_basis("06:行政規則§159,II,2") == "行政程序法第 159 條第 2 項第 2 款"
    assert law_text.level_label("07:行政規則§159,IV,3") == "行政規則（第 159 條第 4 項第 3 款）", \
        "沒列在對照表的款次照代碼換成看得懂的寫法（羅馬數字要換成項次）"
    for plain in ("法律", "命令", "憲法", ""):
        assert law_text.level_label(plain) == plain and law_text.level_basis(plain) == ""
    assert law_text.level_text("05:行政規則§159,II,1") == \
        "行政規則（行政程序法第 159 條第 2 項第 1 款：機關內部規定）"


def _serve_ndc(srv) -> None:
    srv.routes["/r1.xml"] = (200, NDC_XML.encode("utf-8"))
    srv.routes["/r2.xml"] = (200, NDC_XML.replace("GL900001", "GL900002")
                             .replace("範例文書作業規範", "範例格式參考規範")
                             .replace("05:行政規則§159,II,1", "06:行政規則§159,II,2").encode("utf-8"))
    _point("ndc-rules-1", srv.url("/r1.xml"))
    _point("ndc-rules-2", srv.url("/r2.xml"))
    _download("ndc")


def test_filter_and_rows_use_the_readable_names(g, allow_fake, srv):
    _serve_ndc(srv)
    full = gov.search("ndc", "", browse=True)
    assert full["levels"] == {"機關內部規定": 1, "解釋性規定、裁量基準": 1}
    assert full["level_notes"]["機關內部規定"] == "行政程序法第 159 條第 2 項第 1 款"
    assert not any("§" in i["level"] or "05:" in i["level"] for i in full["items"])
    one = gov.search("ndc", "", browse=True, level="解釋性規定、裁量基準")
    assert [i["key"] for i in one["items"]] == ["GL900002"]
    assert gov.search("ndc", "", keys_only=True, level="機關內部規定")["keys"] == ["GL900001"]
    raw = json.loads((gov._pkg_dir("ndc-rules-1") / "current" / "index.json").read_text(encoding="utf-8"))
    assert raw["items"][0]["level"] == "05:行政規則§159,II,1", "存著的清單不改（舊的下載照樣能用）"


def test_imported_text_spells_out_the_level(g, allow_fake, srv):
    _serve_ndc(srv)
    gov.set_selection("ndc", ["GL900001"])
    gov.sync("ndc")
    from app.core.kb import store
    from tests.test_kb_gov_import import _versions
    v = _versions("範例文書作業規範")[0]
    text = store.stored_path(v).read_text(encoding="utf-8")
    assert "- 法規位階：行政規則（行政程序法第 159 條第 2 項第 1 款：機關內部規定）" in text
    assert "05:" not in text


def test_the_new_names_are_translated():
    from pathlib import Path
    for lang in ("en", "ja"):
        d = json.loads(Path(f"app/i18n/{lang}.json").read_text(encoding="utf-8"))
        for k in ("機關內部規定", "解釋性規定、裁量基準", "行政程序法第 159 條第 2 項第 1 款",
                  "行政程序法第 159 條第 2 項第 2 款", "法律", "命令", "憲法"):
            assert d.get(k), f"{lang}.json 少了「{k}」"
