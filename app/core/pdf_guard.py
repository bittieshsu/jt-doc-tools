"""收到的 PDF 到底打不打得開 —— 打不開要當場回 400，不是讓例外冒成 500。

## 為什麼要一支共用函式

每一支工具都要做同一件事，而且**要在兩條路徑上各做一次**（網頁的 `/load`
與公開 API）。各自寫一份 try/except 的結果是：

* v1.14.31 的對抗式驗證發現三支新工具的 `/load` 對毀損 / 加密的 PDF 一律 500，
  而同類的既有工具（`pdf-border`）早就擋得下來 —— 是一致性退步不是新問題。
* 修完 `/load` 之後，**公開 API 那條路仍然全數 500**（3 支工具 × 3 種輸入 = 9
  個組合），因為那是另一段程式碼。

所以收斂成一處。新工具只要記得呼叫它，兩條路徑都有保障。

## 為什麼加密的檔要顯式擋

PyMuPDF 對加密檔仍然回得出 `page_count`，所以「只讀頁數」的檢查會過。
騎縫章因此在載入時回 200 並顯示頁數，使用者要到後面才撞牆，而且撞得很散
（縮圖 500、拼接預覽給了一張圖、送出後作業狀態才變 error）。
"""
from __future__ import annotations

from pathlib import Path

from fastapi import HTTPException


def ensure_readable_pdf(path: Path, *, min_pages: int = 1,
                        unlink_on_error: bool = True) -> int:
    """確認這份 PDF 開得起來、沒有密碼、而且有足夠的頁數。回傳頁數。

    開不起來 / 有密碼 / 頁數不足一律丟 `HTTPException(400)`；
    `unlink_on_error` 會順手把那個暫存檔刪掉（留著也沒用，只是佔空間）。
    """
    import fitz

    try:
        with fitz.open(str(path)) as doc:
            if doc.needs_pass:
                _fail(path, unlink_on_error, "這份 PDF 有密碼保護，請先解除密碼")
            n = doc.page_count
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001 — 任何開檔失敗都當成「檔案有問題」
        _fail(path, unlink_on_error, "檔案讀取失敗，可能已毀損")
    if n < min_pages:
        _fail(path, unlink_on_error,
              "這份文件沒有任何頁面" if min_pages <= 1
              else f"這份文件只有 {n} 頁，至少需要 {min_pages} 頁")
    return n


def _fail(path: Path, unlink: bool, msg: str) -> None:
    if unlink:
        try:
            path.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass
    raise HTTPException(400, msg)
