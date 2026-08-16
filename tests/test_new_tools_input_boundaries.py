"""三支新工具（書籤與目錄 / 騎縫章 / 頁面尺寸統一）的輸入邊界。

## 由來

v1.14.31 的對抗式驗證對這三支工具送畸形輸入，抓到四類問題。**共通點是：
壞掉的輸入不會被擋下來，而是一路衝進去變成 500，或者更糟 —— 回 200 但給了
錯的東西**。後者使用者完全看不出來。

1. **毀損 / 加密的 PDF 一路冒成 500**。同類的既有工具（`pdf-border`）早就
   擋得下來，新工具沒跟上 —— 這是一致性退步，不是新問題。
2. **書籤 JSON 的 `int()` 沒防呆**：`page` 的值是使用者可控的，`"abc"` /
   `1e400` / `[1]` / 5000 位數字字串分別丟 ValueError / OverflowError /
   TypeError，5 種 × 3 個端點全部 500。
3. **縮圖端點的頁碼沒有範圍檢查**：呼叫端一律傳 `page_no - 1`，所以 `0` 變
   `-1` —— Python 的負索引讓它**回最後一頁而且是 200 OK**。同一支工具的
   `/preview` 有檢查、`thumb` 沒有，一支工具內兩套標準。
4. **`page=0` 被無聲改成第 1 頁**：`int(it.page or 1)` 把 0 當 falsy 吃掉，
   於是 `page < 1` 永遠看不到它。`-5` 有警告、`0` 沒有。

## 判準

**任何輸入都不可以打出 500**，而且不可以「回 200 但給錯東西」。
"""
from __future__ import annotations

import hashlib
import os
import tempfile

import pytest

os.environ.setdefault("JTDT_CSRF_DISABLE", "1")


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient

    from app.main import app
    # 例外要變成 500 回應才驗得到，不能讓 TestClient 直接往外丟
    return TestClient(app, raise_server_exceptions=False)


def _pdf(pages: int = 5) -> bytes:
    import fitz

    d = fitz.open()
    for i in range(pages):
        d.new_page().insert_text((72, 100), f"Page {i} 測試內容")
    out = d.tobytes()
    d.close()
    return out


def _encrypted_pdf() -> bytes:
    import fitz

    d = fitz.open("pdf", _pdf(3))
    out = d.tobytes(encryption=fitz.PDF_ENCRYPT_AES_256,
                    owner_pw="o", user_pw="u")
    d.close()
    return out


#: 三支工具的 `/load` 與它們吃的欄位名（書籤是多檔上傳）
LOADERS = [("pdf-bookmark", "files"), ("pdf-seam-stamp", "file"),
           ("pdf-page-size", "file")]


@pytest.mark.parametrize("tool,field", LOADERS)
def test_encrypted_pdf_is_rejected_cleanly(client, tool, field):
    """加密的 PDF 要當場回 400。

    騎縫章原本更糟：`/load` 只讀 page_count 就過了（PyMuPDF 對加密檔仍回得出
    頁數）→ **回 200 顯示「3 頁」**，使用者要到後面才撞牆，而且撞得很散
    （thumb 500、assembled 200 給了一張圖、submit 的作業狀態 error）。
    """
    r = client.post(f"/tools/{tool}/load",
                    files={field: ("a.pdf", _encrypted_pdf(), "application/pdf")})
    assert r.status_code == 400, f"{tool} 對加密 PDF 回了 {r.status_code}"
    assert "密碼" in r.text


@pytest.mark.parametrize("tool,field", LOADERS)
@pytest.mark.parametrize("frac", [0.5, 0.3, 0.15, 0.05, 0.01])
def test_truncated_pdf_never_500(client, tool, field, frac):
    """截斷的 PDF 不可以打出 500。

    PyMuPDF 對半截的檔案有容錯、有時真的救得回來（那時回 200 是對的），
    救不回來時丟 `FileDataError` —— 那個例外必須被接住轉成 400。
    """
    good = _pdf(5)
    data = good[:max(1, int(len(good) * frac))]
    r = client.post(f"/tools/{tool}/load",
                    files={field: ("a.pdf", data, "application/pdf")})
    assert r.status_code < 500, f"{tool} 對 {frac:.0%} 截斷檔回了 {r.status_code}"


@pytest.mark.parametrize("tool,field", LOADERS)
def test_header_only_pdf_is_rejected(client, tool, field):
    r = client.post(f"/tools/{tool}/load",
                    files={field: ("a.pdf", b"%PDF-1.7\n", "application/pdf")})
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# 書籤 JSON
# ---------------------------------------------------------------------------

#: 每一種都會讓 `int()` 丟出不同的例外
MALFORMED_BOOKMARKS = [
    pytest.param('[{"title":"x","page":"abc","level":1}]', id="page-不是數字"),
    pytest.param('[{"title":"x","page":1e400,"level":1}]', id="page-無限大"),
    pytest.param('[{"title":"x","page":[1],"level":1}]', id="page-是陣列"),
    pytest.param('[{"title":"x","page":1,"level":"deep"}]', id="level-不是數字"),
    pytest.param('[{"title":"x","page":"' + "9" * 5000 + '"}]',
                 id="page-5000位數字字串"),
]


@pytest.fixture(scope="module")
def bookmark_upload(client):
    r = client.post("/tools/pdf-bookmark/load",
                    files={"files": ("a.pdf", _pdf(5), "application/pdf")})
    assert r.status_code == 200, r.text
    return r.json()["upload_id"]


@pytest.mark.parametrize("payload", MALFORMED_BOOKMARKS)
@pytest.mark.parametrize("endpoint", ["validate", "toc-preview"])
def test_malformed_bookmark_json_never_500(client, bookmark_upload,
                                           endpoint, payload):
    r = client.post(f"/tools/pdf-bookmark/{endpoint}",
                    data={"upload_id": bookmark_upload, "bookmarks": payload})
    assert r.status_code < 500, (
        f"{endpoint} 對畸形書籤回了 {r.status_code}：{r.text[:200]}")


# ---------------------------------------------------------------------------
# 縮圖頁碼
# ---------------------------------------------------------------------------

def test_thumb_rejects_out_of_range_pages(client, bookmark_upload):
    """`0` 與負數不可以回「別頁的圖 + 200 OK」。

    這是最陰的一種：畫面上完全看不出拿錯頁。原本 `page_no=0` 與
    `page_no=<最後一頁>` 回的 PNG 位元組一模一樣。
    """
    ok = {}
    for pn in (1, 5):
        r = client.get(f"/tools/pdf-bookmark/thumb/{bookmark_upload}/{pn}")
        assert r.status_code == 200, f"第 {pn} 頁應該取得到"
        ok[pn] = hashlib.sha1(r.content).hexdigest()
    assert ok[1] != ok[5], "前提壞了：兩頁的縮圖不該一樣"

    for pn in (0, -1, -2, 999):
        r = client.get(f"/tools/pdf-bookmark/thumb/{bookmark_upload}/{pn}")
        assert r.status_code != 200, (
            f"page_no={pn} 回了 200 —— 拿到的是別頁的圖，畫面上看不出來")
        assert r.status_code < 500, f"page_no={pn} 回了 {r.status_code}"


# ---------------------------------------------------------------------------
# 正規化要把修改講出來
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("field,value", [("page", 0), ("level", 0)])
def test_zero_is_not_silently_corrected(field, value):
    """`0` 被改掉時一定要有一句警告。

    `int(it.page or 1)` 把 0 當 falsy 直接變 1，於是下面的 `page < 1` 永遠
    看不到它 —— 使用者打 0 被改成第 1 頁卻一句話都沒有，而打 -5 反而有警告。
    這違反該模組自己 docstring 寫的原則（「不是靜靜修掉就算了」）。
    """
    from app.tools.pdf_bookmark import bookmark_core as BC

    kw = {"title": "甲", "page": 1, "level": 1}
    kw[field] = value
    _, warns = BC.normalize([BC.BookmarkItem(**kw)], 3)
    assert warns, f"{field}={value} 被改掉了卻沒有任何警告"
    assert "甲" in warns[0]


# ---------------------------------------------------------------------------
# 印章圖的處理成本
# ---------------------------------------------------------------------------

def test_stamp_image_is_downscaled_to_a_sane_size():
    """大張印章圖一定要先縮小 —— 用**產出尺寸**判斷，不要用計時。

    計時的判準太鬆：拿掉尺寸上限之後 numpy 版處理 6000×6000 仍然只要 2.2 秒，
    計時測試照樣全綠（實測變異驗證沒抓到）。產出尺寸是確定性的。

    章蓋出來最大就幾公分，2000 px 已經遠超過 300dpi 所需。
    """
    import io

    from PIL import Image

    from app.tools.pdf_seam_stamp import stamp_source as SS

    buf = io.BytesIO()
    Image.new("RGB", (3000, 3000), (255, 255, 255)).save(buf, format="PNG")
    out = Image.open(io.BytesIO(SS.normalize_upload(buf.getvalue())))
    assert max(out.size) <= SS._MAX_STAMP_PX, (
        f"3000×3000 的圖沒有被縮小（產出 {out.size}）—— 伺服器在做白工")


def test_stamp_processing_is_vectorized():
    """去白底不可以用純 Python 逐像素迴圈。

    第一版一張 **116 KB** 的 6000×6000 全白 PNG 要跑 **27 秒**，而端點是
    `async def` —— 那 27 秒卡在事件迴圈上，期間全站每個請求都在等（實測
    `/healthz` 從 74 ms 變成 19.6 秒）。壓縮率讓成本極度不對稱：
    上傳 116 KB，伺服器付 27 秒，反覆送幾個就是零成本的服務阻斷。

    取 2000×2000（剛好等於上限，不會被縮小）當量測點：向量運算 0.37 秒、
    逐像素迴圈 3.0 秒，差距夠大，門檻放 1.5 秒不會在慢一點的機器上誤判。
    """
    import io
    import time

    from PIL import Image

    from app.tools.pdf_seam_stamp import stamp_source as SS

    buf = io.BytesIO()
    Image.new("RGB", (2000, 2000), (255, 255, 255)).save(buf, format="PNG")
    data = buf.getvalue()

    t0 = time.perf_counter()
    SS.normalize_upload(data)
    dt = time.perf_counter() - t0
    assert dt < 1.5, (
        f"處理一張 2000×2000 的圖花了 {dt:.1f} 秒 —— 這是逐像素迴圈的速度")


def test_stamp_whiteout_behaviour_preserved():
    """去白底的行為不可以因為改寫而變 —— 淺灰的印泥不能被吃掉。"""
    import io

    from PIL import Image

    from app.tools.pdf_seam_stamp import stamp_source as SS

    img = Image.new("RGB", (60, 60), (255, 255, 255))
    for x in range(10, 30):
        for y in range(10, 30):
            img.putpixel((x, y), (200, 30, 30))      # 紅章
    for x in range(35, 50):
        for y in range(35, 50):
            img.putpixel((x, y), (235, 235, 235))    # 淺灰印泥
    buf = io.BytesIO()
    img.save(buf, format="PNG")

    out = Image.open(io.BytesIO(SS.normalize_upload(buf.getvalue())))
    assert out.getpixel((2, 2))[3] == 0, "白底應該變透明"
    assert out.getpixel((20, 20))[3] == 255, "章體不可以被吃掉"
    assert out.getpixel((40, 40))[3] == 255, "淺灰的印泥不可以被當成白底"


# ---------------------------------------------------------------------------
# 公開 API 路徑 —— 與網頁的 /load 是**不同的一段程式碼**
# ---------------------------------------------------------------------------

#: 公開 API 的實際路徑是 `/tools/<id>/api/<id>`（不是 `/api/<id>`）
API_LOADERS = [("pdf-bookmark", "files"), ("pdf-seam-stamp", "file"),
               ("pdf-page-size", "file")]


@pytest.mark.parametrize("tool,field", API_LOADERS)
@pytest.mark.parametrize("label", ["加密", "截斷", "只有標頭", "空檔"])
def test_public_api_never_500_on_bad_pdf(client, tool, field, label):
    """公開 API 對畸形 PDF 不可以 500。

    修好網頁的 `/load` **不代表這條也修好了** —— 它是另一段程式碼，
    v1.14.31 對抗式驗證實測：3 支工具 × 3 種輸入 = 9 個組合全部 500。
    這就是為什麼那個檢查被收斂成 `app/core/pdf_guard.ensure_readable_pdf()`，
    兩條路徑呼叫同一支。
    """
    good = _pdf(4)
    data = {"加密": _encrypted_pdf(), "截斷": good[:len(good) // 5],
            "只有標頭": b"%PDF-1.7\n", "空檔": b""}[label]
    r = client.post(f"/tools/{tool}/api/{tool}",
                    files={field: ("a.pdf", data, "application/pdf")})
    assert r.status_code < 500, (
        f"/tools/{tool}/api/{tool} 對「{label}」回了 {r.status_code}：{r.text[:150]}")
