"""工作區的型別判斷要以**內容型別**為準，不是主檔的路徑名。"""
import io
import zipfile

from app.core import workspace as W


def _ooxml(main_part: str, content_type: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml",
                   '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/'
                   'package/2006/content-types"><Override PartName="/' + main_part +
                   '" ContentType="' + content_type + '"/></Types>')
        z.writestr(main_part, "<x/>")
    return buf.getvalue()


def test_docx_with_numbered_main_part_is_accepted():
    """Word 在某些編輯後會把主檔寫成 `word/document2.xml`。

    照路徑名比對的話，這種檔案拖進工作區會被拒 —— 而錯誤訊息還寫著
    「接受 Word (.docx)」，使用者只會覺得工具壞了（使用者實際踩到）。
    """
    data = _ooxml("word/document2.xml",
                  "application/vnd.openxmlformats-officedocument."
                  "wordprocessingml.document.main+xml")
    got = W.detect_kind(data)
    assert got is not None and got[1] == ".docx", got


def test_conventional_layout_still_works():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", "<x/>")
    assert W.detect_kind(buf.getvalue())[1] == ".docx"


def test_plain_zip_is_still_rejected():
    """放寬判斷不可以連一般的 zip 都收 —— 那是刻意擋掉的。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("a.txt", "hi")
    assert W.detect_kind(buf.getvalue()) is None
