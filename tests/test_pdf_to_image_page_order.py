"""辦公文件轉圖片：ZIP 內檔名頁碼必須對應 PDF 實際頁數。

回歸 bug（v1.11.71）：download 端點用字串排序 `sorted(glob('_p*.png'))` →
_p1, _p10, _p11 … _p2 … 再 enumerate 重新編號，導致 ≥10 頁的 PDF 第 10 頁
的圖被命名成 _p2.png。改為依檔名數字排序 + 用原頁碼當 arcname。
"""
from __future__ import annotations

import io
import re
import zipfile


def _make_pdf_varied_widths(n: int) -> bytes:
    """產生 n 頁 PDF，第 i 頁（0-based）寬度 = 300 + i*20 pt（高度固定）。
    每頁寬度唯一 → 當作頁碼指紋，驗證 ZIP 內檔名對應正確頁。"""
    import fitz
    doc = fitz.open()
    for i in range(n):
        doc.new_page(width=300 + i * 20, height=400)
    out = doc.tobytes()
    doc.close()
    return out


def test_zip_filenames_match_pdf_pages(client):
    n = 12  # ≥10 才會觸發舊的字串排序 bug
    pdf = _make_pdf_varied_widths(n)
    files = {"file": ("doc.pdf", io.BytesIO(pdf), "application/pdf")}
    r = client.post("/tools/pdf-to-image/convert", files=files, data={"dpi": "72"})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["page_count"] == n
    # /convert 回傳的 pages 是正確頁序（page index 0..n-1）
    width_by_page = {i + 1: j["pages"][i]["width_px"] for i in range(n)}
    # 每頁寬度應遞增且唯一（指紋有效）
    assert len(set(width_by_page.values())) == n, "頁寬指紋不唯一"

    upload_id = j["upload_id"]
    rd = client.get(f"/tools/pdf-to-image/download/{upload_id}")
    assert rd.status_code == 200, rd.text
    assert rd.headers["content-type"] == "application/zip"

    from PIL import Image
    with zipfile.ZipFile(io.BytesIO(rd.content)) as z:
        names = z.namelist()
        assert len(names) == n, names
        # 每個 _p{k}.png 的實際圖寬，必須等於 /convert 回報的第 k 頁寬
        for name in names:
            m = re.search(r"_p(\d+)\.png$", name)
            assert m, f"unexpected entry {name}"
            k = int(m.group(1))
            with z.open(name) as fp:
                w = Image.open(io.BytesIO(fp.read())).width
            assert w == width_by_page[k], (
                f"{name}: ZIP 圖寬 {w} != PDF 第 {k} 頁寬 {width_by_page[k]} "
                f"(檔名頁碼與實際頁不一致)")
        # 頁碼集合應為 1..n 完整無缺
        nums = sorted(int(re.search(r'_p(\d+)\.png$', x).group(1)) for x in names)
        assert nums == list(range(1, n + 1)), nums
