"""辦公文件的**來源檔**壞掉時，要在送進 soffice 之前就擋下來。

## 由來

2026-09-06 使用者把一份 `.docx` 拉進逐句翻譯，畫面跳出：

    Parsing failed: {"detail":"office 檔解析失敗：轉檔成功但找不到輸出 .txt"}

那份檔案**被截斷了** —— 36,864 bytes（剛好 36 KiB，區塊邊界），有 11 個局部
檔頭但**沒有中央目錄**（`PK\\x05\\x06` 與 `PK\\x01\\x02` 都不存在）。zip 少了
中央目錄就像被撕掉目錄的書：soffice 打得開檔案卻讀不出內容，然後
**回傳碼 0 卻不產出任何檔案** —— 這正是 CLAUDE.md 記過的 soffice 三種無聲
失敗之一。

我們原本只能丟一句「轉檔成功但找不到輸出 .txt」：自相矛盾，而且對使用者
毫無幫助（他不知道是檔案壞了，只會以為工具壞了）。

## 判準

**只驗容器、不解析內容** —— 毫秒級而且判準明確。壞的要擋、好的一個都不能誤擋。
"""
from __future__ import annotations

import io
import zipfile

import pytest

from app.core import office_convert as oc


def _make_docx(tmp_path, name="ok.docx"):
    """最小但**結構完整**的 docx（有 [Content_Types].xml）。"""
    p = tmp_path / name
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", "<document/>")
    return p


def test_a_healthy_docx_passes(tmp_path):
    """好的檔案一個都不能誤擋 —— 誤擋比漏擋更糟（使用者會以為自己的檔壞了）。"""
    oc.ensure_readable(_make_docx(tmp_path))


def test_truncated_docx_is_rejected_before_soffice(tmp_path):
    """**這就是使用者踩到的那一種**：檔案被截斷，中央目錄不見了。"""
    good = _make_docx(tmp_path, "full.docx")
    data = good.read_bytes()
    cut = tmp_path / "cut.docx"
    cut.write_bytes(data[: len(data) // 2])      # 攔腰截斷
    assert b"PK\x05\x06" not in cut.read_bytes(), "測資本身要真的沒有中央目錄"
    with pytest.raises(oc.OfficeSourceError) as ei:
        oc.ensure_readable(cut)
    msg = str(ei.value)
    assert "不完整" in msg or "毀損" in msg
    # 訊息要**可行動** —— 只說「失敗」等於沒說
    assert "另存" in msg or "重新取得" in msg


def test_a_renamed_file_is_rejected(tmp_path):
    """副檔名改成 .docx 的假檔（zip 結構完整但缺 Office 的必要部件）。"""
    p = tmp_path / "fake.docx"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("hello.txt", "not an office document")
    with pytest.raises(oc.OfficeSourceError):
        oc.ensure_readable(p)


def test_legacy_doc_must_be_an_ole2_container(tmp_path):
    p = tmp_path / "fake.doc"
    p.write_bytes(b"this is just text, not OLE2")
    with pytest.raises(oc.OfficeSourceError):
        oc.ensure_readable(p)


def test_a_zip_bomb_is_rejected(tmp_path):
    """解開後大得離譜的檔案要擋 —— 否則 soffice 會替攻擊者吃光記憶體。"""
    p = tmp_path / "bomb.docx"
    with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", b"\0" * (200 * 1024 * 1024))
    with pytest.raises(oc.OfficeSourceError) as ei:
        oc.ensure_readable(p)
    msg = str(ei.value)
    # 釘**意義**不釘特定用字 —— 判斷集中到 `zip_guard` 之後，
    # 「總量過大」與「壓縮比異常」是兩句不同的訊息，兩種都算擋下來了。
    assert ("龐大" in msg or "壓縮比" in msg), msg
    assert "伺服器資源" in msg, "訊息要說得出為什麼拒絕"


def test_flat_xml_odf_is_not_a_zip_but_is_still_valid(tmp_path):
    """**ODF 允許未壓縮的 flat XML** —— 「不是 zip」不等於壞檔。

    副檔名寫 `.odt` 而內容是 flat XML 的檔案，LibreOffice 讀得進去。
    第一版的驗證把這種**合法檔案擋掉**了，是既有的
    `test_office_convert.py::test_same_extension_conversion_actually_runs`
    抓到的 —— 我自己才寫過「好檔案一個都不能誤擋」，卻沒想到這一種。
    """
    p = tmp_path / "flat.odt"
    p.write_bytes(b'<?xml version="1.0" encoding="UTF-8"?>\n'
                  b'<office:document xmlns:office="urn:oasis:names:tc:opendocument'
                  b':xmlns:office:1.0"/>')
    import zipfile
    assert not zipfile.is_zipfile(p), "前提不成立：這份不該是 ZIP"
    oc.ensure_readable(p)          # 不可以丟例外


def test_validation_runs_after_the_environment_check(tmp_path, monkeypatch):
    """**先講環境問題，再講檔案問題。**

    沒裝 office 是部署層面的問題，跟這份檔案無關 —— 先報「找不到
    LibreOffice」才對。第一版把驗證放在 `find_soffice()` 之前，
    連「檔案不存在」都會蓋掉那個更根本的訊息。
    """
    monkeypatch.setattr(oc, "find_soffice", lambda: None)
    with pytest.raises(RuntimeError) as ei:
        oc.convert_to_text(tmp_path / "nope.docx")
    msg = str(ei.value)
    assert "LibreOffice" in msg or "OxOffice" in msg, msg


def test_unknown_extensions_are_left_alone(tmp_path):
    """沒有容器可驗的（txt / csv / rtf）一律放行，不要自作聰明擋掉。"""
    p = tmp_path / "plain.txt"
    p.write_text("hello", encoding="utf-8")
    oc.ensure_readable(p)


def test_every_conversion_entry_point_validates_first():
    """**每一個轉檔入口都要先驗** —— 漏掉一個，那條路徑就還是丟舊的爛訊息。

    這條守的是「下一支新工具會不會漏」：判準是原始碼裡
    `ensure_readable(src)` 的出現次數不少於 `find_soffice()` 的呼叫點。
    """
    import inspect
    src = inspect.getsource(oc)
    entries = src.count("soffice = find_soffice()")
    guarded = src.count("ensure_readable(src)")
    assert entries > 0, "找不到任何轉檔入口，掃描方式八成壞了"
    assert guarded >= entries, (
        f"{entries} 個轉檔入口，只有 {guarded} 個先驗了來源檔")
