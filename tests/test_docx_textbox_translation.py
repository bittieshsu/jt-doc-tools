"""含**文字方塊**的 .docx 翻譯 —— 同一段文字會被收好幾次。

由來（2026-09-08，客戶的兩份 PDF 轉檔後拿去翻譯時抓到）：一份 12 頁、
44,900 字的文件，`extract_units` 算出 **180,532 字**（四倍）。三個來源：

1. **外層的容器段落**：`w:p` 裡面放著文字方塊時，`para.iter()` 會把方塊裡的
   文字也收進來 —— 於是「整頁串成一段」。寫回去時那一大段會被塞進**第一個
   文字方塊**（實測：第一個小標題方塊收到整頁 4,882 字的譯文），版面直接毀掉。
2. **`mc:Choice` 與 `mc:Fallback` 各一份**：Word 把同一個方塊存兩種格式
   （新的 DrawingML 與舊的 VML），內容一樣。兩份都翻＝**兩倍的 LLM 費用**，
   而且可能翻得不一樣，不同閱讀器看到不同內容。

這不限於我們轉出來的檔案 —— **任何含文字方塊的 Word 文件都會踩到**。
"""
from __future__ import annotations

import io
import zipfile

import pytest

from app.core import office_text_map as otm

_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_MC = "http://schemas.openxmlformats.org/markup-compatibility/2006"
_WPS = "http://schemas.microsoft.com/office/word/2010/wordprocessingShape"
_V = "urn:schemas-microsoft-com:vml"


def _doc(inner: str) -> bytes:
    xml = (f'<w:document xmlns:w="{_W}" xmlns:mc="{_MC}" xmlns:wps="{_WPS}" '
           f'xmlns:v="{_V}"><w:body>{inner}</w:body></w:document>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", xml)
    return buf.getvalue()


def _textbox(text: str) -> str:
    """一個文字方塊：`mc:Choice`（新）與 `mc:Fallback`（舊）各存一份同樣的字。"""
    body = f'<w:p><w:r><w:t>{text}</w:t></w:r></w:p>'
    return (
        "<w:p><w:r><mc:AlternateContent>"
        f"<mc:Choice Requires='wps'><w:drawing><wps:txbx><w:txbxContent>{body}"
        "</w:txbxContent></wps:txbx></w:drawing></mc:Choice>"
        f"<mc:Fallback><w:pict><v:rect><v:textbox><w:txbxContent>{body}"
        "</w:txbxContent></v:textbox></v:rect></w:pict></mc:Fallback>"
        "</mc:AlternateContent></w:r></w:p>"
    )


def _texts(data: bytes) -> list[str]:
    import re
    x = zipfile.ZipFile(io.BytesIO(data)).read("word/document.xml").decode()
    return [m for m in re.findall(r"<w:t[^>]*>([^<]*)</w:t>", x)]


def test_a_textbox_is_counted_once_not_four_times():
    data = _doc(_textbox("Hello") + _textbox("World"))
    units, _ = otm.extract_units(data, ".docx")
    assert [u.text for u in units] == ["Hello", "World"], \
        "文字方塊被重複收了（外層容器 / Choice / Fallback）"


def test_the_container_paragraph_is_never_a_unit():
    """外層容器的文字是所有子孫串起來的 —— 翻了會被塞進第一個方塊。"""
    units, _ = otm.extract_units(_doc(_textbox("A") + _textbox("B")), ".docx")
    assert not any("AB" in u.text or "AA" in u.text for u in units)


def test_translation_lands_in_its_own_textbox():
    """**這是版面會不會毀掉的關鍵。**

    修正前：第一個方塊收到整頁的譯文（實測 4,882 字塞進一個小標題框）。
    """
    data = _doc(_textbox("Alpha") + _textbox("Beta"))
    units, state = otm.extract_units(data, ".docx")
    out = otm.rebuild(state, {i: f"譯{u.text}" for i, u in enumerate(units)}, units)
    got = [t for t in _texts(out) if t.strip()]
    assert got.count("譯Alpha") == 2, got      # Choice + 鏡射到 Fallback
    assert got.count("譯Beta") == 2, got
    assert not any("譯Alpha譯Beta" in t or "AlphaBeta" in t for t in got)


def test_the_old_format_copy_gets_the_same_translation():
    """只翻 `mc:Choice` 的話，交出去的檔案會**藏著一份完整的原文**。

    新版 Word 與 LibreOffice 看 Choice、顯示譯文；走 VML 那條路的閱讀器
    會看到英文，全文搜尋與字數統計也會把兩份都算進去。
    """
    data = _doc(_textbox("Hello"))
    units, state = otm.extract_units(data, ".docx")
    out = otm.rebuild(state, {0: "哈囉"}, units)
    import re
    x = zipfile.ZipFile(io.BytesIO(out)).read("word/document.xml").decode()
    fb = x.split("Fallback")[1] if "Fallback" in x else ""
    assert "哈囉" in fb, "舊格式那份還是原文"
    assert "Hello" not in x, f"檔案裡還留著原文：{x[:200]}"


def test_mismatched_counts_leave_the_old_copy_alone():
    """**對不上就整組不動** —— 硬對會把 A 方塊的譯文寫進 B 方塊。"""
    body_a = "<w:p><w:r><w:t>One</w:t></w:r><w:r><w:t>Two</w:t></w:r></w:p>"
    body_b = "<w:p><w:r><w:t>OneTwo</w:t></w:r></w:p>"     # 節點數不同
    inner = ("<w:p><w:r><mc:AlternateContent>"
             f"<mc:Choice Requires='wps'><w:drawing><wps:txbx><w:txbxContent>{body_a}"
             "</w:txbxContent></wps:txbx></w:drawing></mc:Choice>"
             f"<mc:Fallback><w:pict><v:textbox><w:txbxContent>{body_b}"
             "</w:txbxContent></v:textbox></w:pict></mc:Fallback>"
             "</mc:AlternateContent></w:r></w:p>")
    data = _doc(inner)
    units, state = otm.extract_units(data, ".docx")
    out = otm.rebuild(state, {0: "一二"}, units)
    assert "OneTwo" in _texts(out), "節點數對不上時不可以動舊格式那一份"


def test_a_plain_paragraph_still_works():
    """一般段落（沒有文字方塊）行為完全不變。"""
    data = _doc("<w:p><w:r><w:t>Plain text</w:t></w:r></w:p>")
    units, state = otm.extract_units(data, ".docx")
    assert [u.text for u in units] == ["Plain text"]
    out = otm.rebuild(state, {0: "純文字"}, units)
    assert "純文字" in _texts(out)


def test_nested_tables_are_not_treated_as_containers():
    """表格儲存格裡的段落是正常段落，不可以被當成容器跳過。"""
    cell = ("<w:tbl><w:tr><w:tc>"
            "<w:p><w:r><w:t>Cell A</w:t></w:r></w:p>"
            "</w:tc></w:tr></w:tbl>")
    units, _ = otm.extract_units(_doc(cell), ".docx")
    assert [u.text for u in units] == ["Cell A"]
