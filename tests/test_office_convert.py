"""辦公文件格式互轉（office-convert）。

## 這份測試在守什麼

這個工具的所有風險都集中在一件事上：**soffice 失敗的時候是無聲的**。

* 濾鏡名稱不認得 → 回傳碼 0、不產檔
* 來源與輸出在同一個目錄且副檔名相同 → 回傳碼 0、檔案原封不動
  （而「同副檔名互轉」正是這個工具的用途之一：換版本、修復壞檔）
* 來源檔毀損 → 回傳碼 0，只在輸出印一句 `source file could not be loaded`

所以真正要驗的不是「有沒有回 200」，而是**產出的檔案存在、不是空的、
內容還在**，以及**失敗的時候真的有丟例外**。

## 為什麼有些測試要真的跑 soffice

`convert_with_filter` 的價值幾乎全在「它怎麼處理 soffice 的怪脾氣」。
把 soffice 換成 mock 之後，剩下的只有幾行參數組裝 —— 那種測試不會紅，
也就擋不住任何東西。需要 soffice 的測試會在沒有 soffice 時 skip。
"""
from __future__ import annotations

import re
import zipfile
from pathlib import Path

import pytest

from app.core import office_convert, office_formats

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

_HAS_SOFFICE = office_convert.find_soffice() is not None
needs_soffice = pytest.mark.skipif(not _HAS_SOFFICE,
                                   reason="這台機器沒有 LibreOffice / OxOffice")


# --------------------------------------------------------------------------
# 格式目錄
# --------------------------------------------------------------------------

def test_three_families_present():
    fams = {f.id for f in office_formats.catalogue()}
    assert fams == {"text", "sheet", "slides"}, fams


def test_every_target_has_ext_and_filter():
    for fam in office_formats.catalogue():
        for t in fam.targets:
            assert t.ext and not t.ext.startswith("."), (fam.id, t.id, t.ext)
            assert t.filter, (fam.id, t.id)
            assert t.label, (fam.id, t.id)


def test_target_ids_are_unique_across_families():
    """id 全站唯一 —— `resolve()` 是掃過所有家族找第一個相符的。

    如果兩個家族出現同一個 id，`resolve()` 會回錯的家族，而伺服器端的
    家族檢查就會拿錯的家族去比對，等於那道防線失效。
    """
    seen: dict[str, str] = {}
    for fam in office_formats.catalogue():
        for t in fam.targets:
            assert t.id not in seen, f"{t.id} 同時出現在 {seen[t.id]} 與 {fam.id}"
            seen[t.id] = fam.id


def test_no_pdf_or_image_targets():
    """轉 PDF / 轉圖片各有專門的工具，不在這裡重複做。"""
    bad = [(f.id, t.id, t.ext) for f in office_formats.catalogue()
           for t in f.targets if t.ext in ("pdf", "png", "jpg", "svg")]
    assert not bad, bad


def test_common_targets_exist_in_every_family():
    """每個家族都要有「常用」的目標，不然畫面上第一眼是空的。"""
    for fam in office_formats.catalogue():
        assert any(t.common for t in fam.targets), fam.id


def test_version_variants_share_extension():
    """同副檔名的多個目標 = 版本選擇，這是這個工具的賣點之一。"""
    text = next(f for f in office_formats.catalogue() if f.id == "text")
    docx = [t for t in text.targets if t.ext == "docx"]
    assert len(docx) >= 2, f"預期 .docx 有多個版本可選，實際 {docx}"
    assert len({t.filter for t in docx}) == len(docx), "版本不同但濾鏡一樣"


def test_family_for_ext_is_case_insensitive():
    assert office_formats.family_for_ext(".PPTX").id == "slides"
    assert office_formats.family_for_ext("XLSX").id == "sheet"
    assert office_formats.family_for_ext(".odt").id == "text"
    assert office_formats.family_for_ext(".pdf") is None       # PDF 不在這裡


def test_resolve_unknown_returns_none():
    assert office_formats.resolve("no-such-target") is None


def test_curated_extensions_are_not_derived_from_id():
    """`doc-xml2003` 的副檔名是 `.xml` 不是 `.doc`。

    這條擋的是「用 id 前綴推副檔名」那種寫法 —— 推錯了檔名會不對，
    而且不會有任何錯誤訊息。
    """
    got = office_formats.resolve("doc-xml2003")
    if got is None:                       # 這套 office 沒有這支濾鏡
        pytest.skip("沒有 MS Word 2003 XML 濾鏡")
    assert got[1].ext == "xml", got[1].ext


@needs_soffice
def test_catalogue_matches_installed_registry():
    """對照表寫的副檔名要跟 soffice 自己宣告的一致。

    兩邊漂掉的話，畫面上寫 `.docx`、實際產出別的副檔名，使用者下載回去
    才發現打不開。
    """
    reg = office_formats._scan_registry()
    if not reg:
        pytest.skip("讀不到 registry")
    bad = []
    for fam in office_formats.catalogue():
        for t in fam.targets:
            info = reg.get(t.filter)
            exts = (info or {}).get("exts") or []
            # **比對的是「在不在合法清單內」不是「等不等於第一個」**：
            # 一個 Type 可以有多個副檔名（`generic_Text` 是 `csv txt`），
            # Writer 的純文字匯出用 `.txt` 完全正確，只是排在第二個。
            if exts and t.ext not in exts:
                bad.append(f"{t.id}: 我們寫 .{t.ext}，soffice 只認 {exts}")
    assert not bad, "\n".join(bad)


# --------------------------------------------------------------------------
# 轉換本身 —— 無聲失敗的三種樣態
# --------------------------------------------------------------------------

def _mini_odt(tmp_path: Path) -> Path:
    """做一份最小的 ODF 文字文件（flat XML，不必經過 soffice）。"""
    p = tmp_path / "src.fodt"
    p.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<office:document '
        'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
        'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
        'office:version="1.3" '
        'office:mimetype="application/vnd.oasis.opendocument.text">'
        '<office:body><office:text>'
        '<text:p>格式互轉測試 ABC 123</text:p>'
        '<text:p>第二段落</text:p>'
        '</office:text></office:body></office:document>',
        encoding="utf-8")
    return p


@needs_soffice
def test_conversion_keeps_content(tmp_path):
    """轉出來的檔案要真的有內容 —— 不是只看有沒有產生檔案。"""
    src = _mini_odt(tmp_path)
    dst = tmp_path / "out.odt"
    office_convert.convert_with_filter(src, dst, "odt", "writer8")

    assert dst.exists() and dst.stat().st_size > 0
    with zipfile.ZipFile(dst) as z:
        body = z.read("content.xml").decode("utf-8", "replace")
    text = re.sub(r"<[^>]+>", "", body)
    assert "格式互轉測試" in text
    assert "第二段落" in text


@needs_soffice
def test_same_extension_conversion_actually_runs(tmp_path):
    """同副檔名互轉 —— 這是 soffice 最容易無聲跳過的情境。

    來源與輸出落在同一個目錄且副檔名相同時，soffice 會什麼都不做並回傳
    成功。`convert_with_filter` 一律轉到獨立的暫存目錄就是為了這件事。

    **怎麼證明「真的轉了」**：光是斷言「檔案還在、內容還對」是抓不到的
    —— 什麼都沒做的話檔案本來就還在、內容本來就還對。所以這裡放一份
    **副檔名是 `.odt`、內容其實是未壓縮 flat XML** 的檔案：真的轉過就會
    變成 ZIP 容器，跳過的話還是純文字 XML。差別看得出來，測試才有意義。
    """
    disguised = tmp_path / "same.odt"
    disguised.write_bytes(_mini_odt(tmp_path).read_bytes())
    assert not zipfile.is_zipfile(disguised), "前提不成立：這份應該還不是 ZIP"

    office_convert.convert_with_filter(disguised, disguised, "odt", "writer8")

    assert zipfile.is_zipfile(disguised), (
        "就地同副檔名轉換被無聲跳過了 —— 檔案還是原本的 flat XML")
    with zipfile.ZipFile(disguised) as z:
        text = re.sub(r"<[^>]+>", "",
                      z.read("content.xml").decode("utf-8", "replace"))
    assert "格式互轉測試" in text


@needs_soffice
def test_unknown_filter_raises_not_silently_succeeds(tmp_path):
    """濾鏡不認得時 soffice 回傳 0 且不產檔 —— 一定要變成例外。"""
    src = _mini_odt(tmp_path)
    with pytest.raises(RuntimeError) as exc:
        office_convert.convert_with_filter(src, tmp_path / "x.odt", "odt",
                                           "No Such Filter 9000")
    assert "No Such Filter 9000" in str(exc.value)


@needs_soffice
def test_broken_source_blames_the_source_not_the_filter(tmp_path):
    """來源檔壞掉時的訊息要指向來源，不要怪到目標格式頭上。

    兩種失敗 soffice 都是回傳碼 0，只能靠它印的內容分辨。分錯的話使用者
    會一直去換目標格式，怎麼換都不會成功。
    """
    bad = tmp_path / "broken.docx"
    bad.write_bytes(b"PK\x03\x04 not really a docx")
    with pytest.raises(RuntimeError) as exc:
        office_convert.convert_with_filter(bad, tmp_path / "y.odt", "odt",
                                           "writer8")
    msg = str(exc.value)
    assert "來源檔" in msg, msg
    assert "writer8" not in msg, f"訊息把責任推給濾鏡了：{msg}"


def test_good_output_wins_over_bad_exit_code(tmp_path, monkeypatch):
    """soffice 回傳非 0 但檔案好好的 —— 要當成功，不可以丟掉。

    soffice 的離開碼不可靠：它會一邊印無關的警告（找不到 Java）一邊正常
    轉完，也可能在收尾階段才死掉。先看回傳碼的話，已經轉好的檔案會被
    白白丟掉，使用者看到的是「轉換失敗」配上一段看不懂的 Java 警告。
    """
    import subprocess

    src = tmp_path / "a.odt"
    src.write_bytes(b"dummy")

    class _FakeProc:
        returncode = 1

        def communicate(self, timeout=None):
            # 假裝 soffice 已經把檔案寫進 outdir 了
            outdir = _FakeProc.outdir
            (outdir / "a.doc").write_bytes(b"REAL CONTENT")
            return (b"", b"javaldx: Could not find a Java Runtime Environment!")

    def _fake_popen(cmd, **kw):
        # `--outdir <dir>` 的下一個參數就是輸出目錄
        _FakeProc.outdir = Path(cmd[cmd.index("--outdir") + 1])
        return _FakeProc()

    monkeypatch.setattr(office_convert, "find_soffice", lambda: "/fake/soffice")
    monkeypatch.setattr(office_convert, "_track", lambda p: None)
    monkeypatch.setattr(subprocess, "Popen", _fake_popen)

    dst = tmp_path / "out.doc"
    office_convert.convert_with_filter(src, dst, "doc", "MS Word 97")
    assert dst.read_bytes() == b"REAL CONTENT"


def test_killed_process_says_so_instead_of_blaming_the_format(tmp_path,
                                                              monkeypatch):
    """被訊號中止（實測 137 = SIGKILL）要講「系統中止」。

    講成「這套 office 不支援這個格式」的話，使用者會一直換目標格式重試，
    但問題其實是記憶體或同時轉太多份 —— 換幾次都不會好。
    """
    import subprocess

    src = tmp_path / "a.odt"
    src.write_bytes(b"dummy")

    class _FakeProc:
        returncode = 137          # 128 + SIGKILL

        def communicate(self, timeout=None):
            return (b"", b"javaldx: Could not find a Java Runtime Environment!")

    monkeypatch.setattr(office_convert, "find_soffice", lambda: "/fake/soffice")
    monkeypatch.setattr(office_convert, "_track", lambda p: None)
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **kw: _FakeProc())

    with pytest.raises(RuntimeError) as exc:
        office_convert.convert_with_filter(src, tmp_path / "o.doc", "doc",
                                           "MS Word 97")
    msg = str(exc.value)
    assert "系統中止" in msg, msg
    assert "不支援" not in msg, f"把責任推給目標格式了：{msg}"


def test_missing_soffice_raises_clear_error(tmp_path, monkeypatch):
    """沒裝 office 時要講人話，不要丟 FileNotFoundError。"""
    monkeypatch.setattr(office_convert, "find_soffice", lambda: None)
    with pytest.raises(RuntimeError) as exc:
        office_convert.convert_with_filter(tmp_path / "a.odt",
                                           tmp_path / "b.doc", "doc",
                                           "MS Word 97")
    assert "LibreOffice" in str(exc.value) or "OxOffice" in str(exc.value)


# --------------------------------------------------------------------------
# 端點
# --------------------------------------------------------------------------

@pytest.fixture()
def client():
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app)


def test_formats_endpoint_lists_families(client):
    r = client.get("/tools/office-convert/formats")
    assert r.status_code == 200
    fams = r.json()["families"]
    assert {f["id"] for f in fams} == {"text", "sheet", "slides"}
    for f in fams:
        assert f["targets"] and f["sources"]


def test_page_renders_every_family_group(client):
    """三組選項都要 render 出來 —— 前端只負責顯示哪一組。"""
    r = client.get("/tools/office-convert/")
    assert r.status_code == 200
    for fam in ("text", "sheet", "slides"):
        assert f'name="target-{fam}"' in r.text, fam


def test_cross_family_target_is_rejected(client, tmp_path):
    """試算表配文書檔的目標 —— 前端擋得住，但 API 打得進來。

    不擋的話 soffice 會「成功」產出一份內容全毀的檔案（試算表被當文書檔開），
    使用者拿到的是看起來正常、實際錯誤的結果。
    """
    p = tmp_path / "book.ods"
    p.write_bytes(b"PK\x03\x04dummy")
    r = client.post("/tools/office-convert/submit",
                    data={"target": "docx-2007"},
                    files=[("file", ("book.ods", p.read_bytes(),
                                     "application/octet-stream"))])
    assert r.status_code == 400
    assert "試算表" in r.json()["detail"]


def test_unknown_target_is_rejected(client, tmp_path):
    r = client.post("/tools/office-convert/submit",
                    data={"target": "nope"},
                    files=[("file", ("a.odt", b"PK\x03\x04", "application/octet-stream"))])
    assert r.status_code == 400


def test_unsupported_extension_is_rejected(client):
    r = client.post("/tools/office-convert/submit",
                    data={"target": "odt"},
                    files=[("file", ("photo.png", b"\x89PNG\r\n", "image/png"))])
    assert r.status_code == 400
    assert "png" in r.json()["detail"].lower()


def test_no_extension_is_rejected(client):
    r = client.post("/tools/office-convert/submit",
                    data={"target": "odt"},
                    files=[("file", ("noext", b"data", "application/octet-stream"))])
    assert r.status_code == 400


def test_empty_file_is_rejected(client):
    r = client.post("/tools/office-convert/submit",
                    data={"target": "odt"},
                    files=[("file", ("a.docx", b"", "application/octet-stream"))])
    assert r.status_code == 400


def test_mixed_families_in_one_batch_are_rejected(client):
    """一批裡混了兩類 —— 第一份對、第二份錯，也要擋。"""
    r = client.post("/tools/office-convert/submit",
                    data={"target": "odt"},
                    files=[("file", ("a.docx", b"PK\x03\x04", "application/octet-stream")),
                           ("file", ("b.xlsx", b"PK\x03\x04", "application/octet-stream"))])
    assert r.status_code == 400
    assert "試算表" in r.json()["detail"]
