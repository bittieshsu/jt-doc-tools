from __future__ import annotations

from pathlib import Path

import fitz


# v1.7.36：高解析 PDF (海報 / 工程圖 / 大尺寸投影片) 在 120 DPI 下渲染 PNG
# 像素量爆炸 → 重繪 / 自動儲存後 BG 重載要好幾秒。透過 pixel cap 限制
# 預覽 PNG 最長邊不超過此值，視內容大小自動降 DPI。
#
# 注意：這只影響「編輯器預覽 PNG」，下載的 PDF 仍是原始解析度（PDF document
# 直接修改 + save，從來沒走 PNG render path）。所以 user-visible 損失只在
# 編輯器 canvas 上看到的清晰度，下載出來的最終 PDF 完全保留原始品質。
_PREVIEW_MAX_PIXEL = 1800


def compute_preview_dpi(
    page_w_pt: float,
    page_h_pt: float,
    target_dpi: int = 120,
    max_pixel: int = _PREVIEW_MAX_PIXEL,
) -> int:
    """根據頁面尺寸算出實際渲染 DPI。
    一般 letter / A4 用 target_dpi（120）；超大頁面降到剛好讓最長邊 = max_pixel。
    """
    max_dim_pt = max(page_w_pt, page_h_pt, 1.0)
    px_at_target = max_dim_pt * target_dpi / 72.0
    if px_at_target <= max_pixel:
        return target_dpi
    return max(36, int(max_pixel * 72.0 / max_dim_pt))


async def render_page_png_async(*args, **kwargs) -> tuple[int, int]:
    """`render_page_png` 的非同步版本 —— **端點一律用這支**。

    算縮圖是純 CPU 的重活。直接在 async 端點裡呼叫同步版會把事件迴圈卡住，
    那段時間**全站對所有人都不回應**（2026-08-26 正式機實測：文件去識別化
    一份檔卡住 116 秒，而作業佇列是空的）。

    非端點的呼叫者（背景作業、CLI、已經在執行緒裡的程式）繼續用同步版就好，
    再包一層 `to_thread` 只是多一次跳轉。
    """
    import asyncio
    return await asyncio.to_thread(render_page_png, *args, **kwargs)


def render_page_png(
    pdf_path: Path,
    out_png: Path,
    page_index: int = 0,
    dpi: int = 110,
    adaptive: bool = True,
) -> tuple[int, int]:
    """Render a single PDF page to PNG. Returns (width_px, height_px).

    adaptive=True：若 page 尺寸超過 _PREVIEW_MAX_PIXEL 換算後，自動降 DPI 避免
    PNG 過大（高解析 PDF 重繪 / autosave 變慢的主因）。
    """
    with fitz.open(str(pdf_path)) as doc:
        # **頁碼一定要在這裡驗**。呼叫端一律傳 `page_no - 1`，所以使用者送
        # `page_no=0` 會變成 `-1` —— Python 的負索引讓它**回最後一頁而且是
        # 200 OK**，畫面上完全看不出拿錯頁（v1.14.31 對抗式驗證：`page_no=0`
        # 與 `page_no=5` 回的 PNG 位元組一模一樣）。超出範圍則是 IndexError
        # 冒上去變成 500。四支工具的縮圖端點形狀相同，擋在這一處才不會再長回來。
        if not (0 <= page_index < doc.page_count):
            raise ValueError(
                f"頁碼超出範圍（第 {page_index + 1} 頁，文件共 {doc.page_count} 頁）")
        page = doc[page_index]
        actual_dpi = dpi
        if adaptive:
            actual_dpi = compute_preview_dpi(page.rect.width, page.rect.height, target_dpi=dpi)
        mat = fitz.Matrix(actual_dpi / 72, actual_dpi / 72)
        pix = page.get_pixmap(matrix=mat, alpha=False)
        pix.save(str(out_png))
        return pix.width, pix.height
