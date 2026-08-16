"""PDF 編輯器寫進去的中文：字形要看得見、檔案不可以是十幾 MB。

## 由來

v1.14.19 的正式機故障是「字型子集化把中文變成看不見」—— 而**文字層完全正常**
（抽得到、搜尋得到、複製得到），檔案還變小了，所以看起來一切都對。
教訓是：**「檔案變小」「文字抽得到」都不是視覺正確的證據，一定要算圖數墨水。**

v1.14.31 的對抗式驗證發現編輯器的四個字型註冊點都沒有傳 `text=`，等於完全
沒有子集化 —— 使用者每存一次檔就嵌一支 16 MB 的字型（實測單一個文字物件的
產出 16,301 KB）。

## 這份測試擋什麼

補上子集化之後，最危險的新失效模式是**傳漏字**：子集化只會嵌進 `text` 裡有
的字，漏掉的字在產出的 PDF 裡**完全看不見**，而文字層照樣抽得到 —— 跟當初
那個慘案一模一樣的無聲失敗。

所以這裡不只測「有沒有變小」，而是：

1. 逐字算圖確認**紙上真的有墨水**
2. 專門測「同一頁、同一個字型、兩個文字物件用不同的字」—— 如果實作只拿
   第一個物件的文字去子集化，第二個物件的字就會消失
"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("JTDT_CSRF_DISABLE", "1")


def _ink(page, dpi: int = 100) -> int:
    """紙上有多少深色像素。

    `pix.samples` 是 property，**每次存取都重建整份緩衝** —— 一定要先綁成
    區域變數，放在迴圈條件裡會慢到像當掉。
    """
    pix = page.get_pixmap(dpi=dpi, alpha=False)
    data = pix.samples
    return sum(1 for i in range(0, len(data), 3) if data[i] < 200)


def _chars_not_embedded(pdf_bytes: bytes, text: str) -> set[str]:
    """這份 PDF 沒有自帶字形的那些字。

    把 PDF 內嵌的每一支字型抽出來看 cmap；文字裡有、而所有內嵌字型都沒有的
    字，就是「要靠開檔那台機器剛好有字型」才看得到的字。
    """
    import io

    import fitz
    from fontTools.ttLib import TTFont

    covered: set[int] = set()
    with fitz.open("pdf", pdf_bytes) as d:
        for pno in range(d.page_count):
            for f in d[pno].get_fonts(full=True):
                try:
                    _, _, _, buf = d.extract_font(f[0])
                except Exception:  # noqa: BLE001
                    continue
                if not buf or len(buf) < 1000:
                    continue
                try:
                    covered |= set(TTFont(io.BytesIO(buf), lazy=True,
                                          fontNumber=0).getBestCmap())
                except Exception:  # noqa: BLE001
                    continue
    return {c for c in text if ord(c) > 0x2E80 and ord(c) not in covered}


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient

    from app.main import app
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def blank_pdf() -> bytes:
    import fitz

    d = fitz.open()
    d.new_page(width=595, height=842)
    out = d.tobytes()
    d.close()
    return out


def _upload(client, data: bytes) -> str:
    r = client.post("/tools/pdf-editor/load",
                    files={"file": ("a.pdf", data, "application/pdf")})
    assert r.status_code == 200, r.text
    return r.json()["upload_id"]


def _text_obj(oid: str, text: str, y: float, font: str) -> dict:
    return {"id": oid, "type": "text", "x": 60, "y": y, "w": 400, "h": 40,
            "text": text, "font": font, "font_size": 24, "color": "#000000"}


def _cjk_font_id() -> str:
    """從字型目錄挑一支真的 CJK 字型（`system:<path>` 形式）。"""
    from app.core import font_catalog

    best = font_catalog.best_cjk_path("sans", "traditional")
    if not best:
        pytest.skip("這台機器沒有安裝 CJK 字型")
    for f in font_catalog.list_fonts():
        if str(f.get("path") or "") == str(best[0]):
            return str(f.get("id"))
    pytest.skip("字型目錄裡找不到對應的項目")


def test_editor_writes_visible_chinese_and_subsets(client, blank_pdf):
    """存檔後中文要看得見，而且不可以嵌整支字型。"""
    import fitz

    uid = _upload(client, blank_pdf)
    fid = _cjk_font_id()
    body = {"upload_id": uid, "pages": [
        {"page": 0, "objects": [_text_obj("t1", "測試中文字", 120, fid)]}]}
    r = client.post("/tools/pdf-editor/save", json=body)
    assert r.status_code == 200, r.text[:300]

    out = client.get(f"/tools/pdf-editor/download/{uid}")
    assert out.status_code == 200, out.text[:200]
    data = out.content

    with fitz.open("pdf", data) as d:
        page = d[0]
        assert _ink(page) > 200, (
            "存檔後紙上沒有墨水 —— 中文被寫成看不見的字（v1.14.19 的形態）")
        assert "測試中文字" in page.get_text(), "文字層也要正常"

    # 一頁五個中文字不該讓檔案變成十幾 MB
    assert len(data) < 4_000_000, (
        f"產出 {len(data) / 1024:,.0f} KB —— 整支字型被嵌進去了，沒有子集化")


def test_two_objects_sharing_a_font_are_both_visible(client, blank_pdf):
    """**同一頁、同一個字型、不同的字** —— 兩個都要看得見。

    這是子集化最危險的失效模式：如果實作只拿第一個物件的文字去子集化，
    第二個物件的字就完全不在字型裡 → 畫面上那一行是空的，而 `get_text()`
    照樣抽得到，使用者不會發現，收件方才會。
    """
    import fitz

    uid = _upload(client, blank_pdf)
    fid = _cjk_font_id()
    body = {"upload_id": uid, "pages": [{"page": 0, "objects": [
        _text_obj("t1", "第一段文字", 120, fid),
        _text_obj("t2", "另外完全不同的漢字", 260, fid),
    ]}]}
    r = client.post("/tools/pdf-editor/save", json=body)
    assert r.status_code == 200, r.text[:300]

    data = client.get(f"/tools/pdf-editor/download/{uid}").content

    # **算圖數墨水在這裡會給出假的安心**：子集真的漏了字時，MuPDF 會退回
    # **這台機器上的系統字型**把它畫出來 —— 在裝了中文字型的開發機上看起來
    # 完全正常。換一台沒裝 CJK 字型的機器、或換一個閱讀器，那一行就是空的。
    # 實測確認過：漏字版的產出照樣有 3,553 個墨水像素。
    #
    # 所以判準是**產出的 PDF 必須自帶所有用到的字形**，不是「這台機器畫得出來」。
    missing = _chars_not_embedded(data, "第一段文字另外完全不同的漢字")
    assert not missing, (
        f"這些字沒有被嵌進 PDF：{''.join(sorted(missing))} —— "
        "在沒有安裝中文字型的機器上會整段消失")

    with fitz.open("pdf", data) as d:
        page = d[0]
        for label, y0, y1 in (("第一段", 90, 170), ("第二段", 230, 310)):
            clip = fitz.Rect(0, y0, page.rect.width, y1)
            pix = page.get_pixmap(dpi=100, clip=clip, alpha=False)
            buf = pix.samples
            ink = sum(1 for i in range(0, len(buf), 3) if buf[i] < 200)
            assert ink > 100, f"{label}沒有墨水（{ink}）"


# ---------------------------------------------------------------------------
# 逐句翻譯的對照 PDF
# ---------------------------------------------------------------------------

def test_translate_doc_pdf_uses_traditional_face_and_subsets():
    """逐句翻譯的對照 PDF：字形要是**繁中**、檔案不可以是十幾 MB。

    原本這裡寫的是 `fontfile=str(path)` 加 `set_simple=False`，註解還寫著
    「PyMuPDF insert_font 支援 TTC 指定 face index」—— **那句話是錯的**：
    PyMuPDF 的公開 API 完全沒有 ttc 索引參數，`fontfile` 一律用第 0 套。
    而 Linux 的 `NotoSansCJK-Regular.ttc` 第 0 套是**日文**（繁中在第 3 套），
    所以整份對照 PDF 的中文都是日文字形（每/毎、過、船 寫法不同）。
    同時因為沒給 `text=`，整支 16 MB 字型原封不動嵌進去（實測 13,406 KB）。

    CLAUDE.md 為這個雷記過兩次（v1.11.40、v1.14.19），這一處是漏網的。
    """
    import io

    from fontTools.pens.recordingPen import RecordingPen
    from fontTools.ttLib import TTCollection, TTFont

    from app.core.font_catalog import best_cjk_path
    from app.tools.translate_doc.router import _build_pdf

    best = best_cjk_path("sans", "traditional")
    if not best or str(best[0]).lower().endswith((".ttf", ".otf")):
        pytest.skip("這台機器的 CJK 字型不是 .ttc，測不到子字型挑選")
    path, tc_idx = best
    if not tc_idx:
        pytest.skip("這台機器的繁中就是第 0 套，測不到挑錯的情況")

    text = "每天海運直達過船"
    data = _build_pdf([{"source": "s", "target": text}], {"filename": "t.pdf"})

    assert len(data) < 4_000_000, (
        f"產出 {len(data) / 1024:,.0f} KB —— 整支字型被嵌進去了")

    missing = _chars_not_embedded(data, text)
    assert not missing, f"這些字沒有被嵌進 PDF：{''.join(sorted(missing))}"

    # 字形輪廓要跟**繁中**那一套一致，不是日文那套
    import fitz
    embedded = None
    with fitz.open("pdf", data) as d:
        for f in d[0].get_fonts(full=True):
            _, _, _, buf = d.extract_font(f[0])
            if buf and len(buf) > 1000:
                embedded = buf
    assert embedded, "PDF 裡沒有內嵌字型"

    coll = TTCollection(str(path))
    emb = TTFont(io.BytesIO(embedded), lazy=True, fontNumber=0)

    def outline(font, ch):
        gname = font.getBestCmap().get(ord(ch))
        if not gname:
            return None
        pen = RecordingPen()
        font.getGlyphSet()[gname].draw(pen)
        return str(pen.value)

    checked = 0
    for ch in text:
        e = outline(emb, ch)
        tc = outline(coll.fonts[tc_idx], ch)
        jp = outline(coll.fonts[0], ch)
        if e is None or tc is None or jp is None or tc == jp:
            continue          # 這個字兩套長得一樣，分辨不出來
        checked += 1
        assert e == tc, f"「{ch}」用的是日文字形，不是繁中"
    assert checked >= 2, "沒有找到足以分辨的字，這個測試沒測到東西"
