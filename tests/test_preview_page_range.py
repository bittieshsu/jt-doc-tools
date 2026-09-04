"""縮圖 / 預覽的頁碼超出範圍要回 4xx，**不可以 500**。

## 由來

2026-09-05 用瀏覽器實跑英文介面時順手發現：`/tools/pdf-rotate/thumb/<id>/0`
與 `/99` 都回 **500**。頁碼在**路徑上**，所以那是「使用者送錯網址」，不是
伺服器壞掉 —— 500 會讓人以為服務掛了而一直重試，監控端也全是假警報
（跟 v1.14.37「毀損檔案一律 400 不可 500」同一條原則）。

`render_page_png` 其實早就擋住範圍了（v1.14.31 修的「`page_no=0` 走負索引
回最後一頁而且是 200 OK」），但它丟的 `ValueError` 沒有人接，一路冒成 500。
修法是給它一個專屬例外 `PageOutOfRange` + **一個全域處理器**：全站三十幾支
縮圖 / 預覽端點形狀相同，逐支改的話下一支新工具又會漏。

**判準是「不是 5xx」**，不是「一定要 404」—— 有些工具自己先擋下來回 400，
那也是對的。
"""
from __future__ import annotations

import io

import fitz
import pytest


@pytest.fixture(scope="module")
def three_page_pdf() -> bytes:
    doc = fitz.open()
    for i in range(3):
        doc.new_page().insert_text((72, 100), f"page {i + 1}")
    data = doc.tobytes()
    doc.close()
    return data


#: (工具 id, 上傳端點, 縮圖端點樣板)。頁碼從 1 起算。
_TOOLS = [
    ("pdf-rotate", "/tools/pdf-rotate/load", "/tools/pdf-rotate/thumb/{uid}/{page}"),
    ("pdf-pages", "/tools/pdf-pages/load", "/tools/pdf-pages/thumb/{uid}/{page}"),
    ("pdf-pageno", "/tools/pdf-pageno/load", "/tools/pdf-pageno/thumb/{uid}/{page}"),
    ("pdf-border", "/tools/pdf-border/load", "/tools/pdf-border/thumb/{uid}/{page}"),
    ("pdf-page-size", "/tools/pdf-page-size/load",
     "/tools/pdf-page-size/thumb/{uid}/{page}"),
    ("pdf-seam-stamp", "/tools/pdf-seam-stamp/load",
     "/tools/pdf-seam-stamp/thumb/{uid}/{page}"),
    ("pdf-extract-images", "/tools/pdf-extract-images/load",
     "/tools/pdf-extract-images/page-thumb/{uid}/{page}"),
    ("pdf-annotations", "/tools/pdf-annotations/analyze",
     "/tools/pdf-annotations/preview/{uid}/{page}"),
    ("pdf-annotations-strip", "/tools/pdf-annotations-strip/analyze",
     "/tools/pdf-annotations-strip/preview/{uid}/{page}"),
]


@pytest.mark.parametrize("tool,upload,thumb", _TOOLS)
@pytest.mark.parametrize("page", [0, 4, 99])
def test_out_of_range_page_is_not_a_server_error(client, three_page_pdf, tool, upload, thumb, page):
    r = client.post(upload, files={"file": ("t.pdf", io.BytesIO(three_page_pdf), "application/pdf")})
    assert r.status_code == 200, f"{tool} 上傳失敗：{r.status_code}"
    uid = r.json()["upload_id"]
    got = client.get(thumb.format(uid=uid, page=page))
    assert got.status_code < 500, (
        f"{tool} 的第 {page} 頁縮圖回了 {got.status_code} —— "
        "頁碼超出範圍是使用者送錯網址，不可以是 5xx")


@pytest.mark.parametrize("tool,upload,thumb", _TOOLS)
def test_valid_page_still_renders(client, three_page_pdf, tool, upload, thumb):
    """**反向對照**：擋掉超範圍之後，正常的頁碼還是要畫得出圖。

    只驗「超範圍會被擋」的話，把整支端點改成永遠回 404 也會過。
    """
    r = client.post(upload, files={"file": ("t.pdf", io.BytesIO(three_page_pdf), "application/pdf")})
    uid = r.json()["upload_id"]
    got = client.get(thumb.format(uid=uid, page=1))
    assert got.status_code == 200, f"{tool} 的第 1 頁縮圖回了 {got.status_code}"
    assert len(got.content) > 200, f"{tool} 的第 1 頁縮圖是空的"
