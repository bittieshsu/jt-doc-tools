"""事務機掃描取件：設備回的檔案 —— 認格式、檢查、合成 PDF（掃描與信箱取件共用）。

* **格式只看檔頭**（PDF / JPEG / TIFF；BigTIFF 也認），不信 Content-Type 與副檔名：設備回了一張
  HTML 登入頁或 XML 錯誤訊息時，狀態碼照樣可能是 200。
* 檢查用 Pillow 開檔頭與頁數（TIFF 的每一頁、JPEG 的尺寸），**像素數有上限**（壞掉或惡意的檔案
  宣稱幾十億像素時直接拒絕，不會吃光記憶體）；PDF 用 PyMuPDF 從磁碟開。
* 合成 PDF：一份 PDF 原樣保留（不重新編碼）；TIFF 交給 PyMuPDF（多頁、CCITT G3/G4、JPEG 壓縮實測都可以，
  頁面大小照 TIFF 記的解析度）；JPEG 照解析度換算成頁面大小。
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Optional

#: 單頁最多幾個像素（約 A3、1200 dpi 彩色）。超過就當成壞檔，不解碼。
MAX_PIXELS = 300_000_000
#: 一份檔案最多幾頁（多頁 TIFF）。
MAX_FRAMES = 1000
#: 沒有解析度可用時，當成幾 dpi（事務機掃描最常見的設定）。
DEFAULT_DPI = 200

EXT = {"pdf": ".pdf", "jpeg": ".jpg", "tiff": ".tif"}


class OutputError(Exception):
    def __init__(self, code: str, detail: Optional[dict] = None):
        super().__init__(code)
        self.code = code
        self.detail = detail or {}


def sniff(path: Path) -> Optional[str]:
    """檔頭 → `pdf` / `jpeg` / `tiff`；都不是回 None。"""
    with open(path, "rb") as f:
        head = f.read(8)
    if head.startswith(b"%PDF-"):
        return "pdf"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if head[:4] in (b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+"):
        return "tiff"
    return None


def inspect(path: Path, kind: str) -> dict:
    """檢查檔案打得開，回傳 `{"pages", "dpi"}`。打不開丟 `OutputError("FILE_INVALID")`。

    不把影像整個解碼（大張彩色掃描一頁就上百 MB）：Pillow 只讀檔頭與每一頁的目錄。"""
    if kind == "pdf":
        import fitz
        try:
            with fitz.open(str(path), filetype="pdf") as d:
                n = d.page_count
        except Exception:  # noqa: BLE001 —— 打不開就是壞檔
            raise OutputError("FILE_INVALID", {"kind": kind}) from None
        if n < 1:
            raise OutputError("FILE_INVALID", {"kind": kind, "reason": "no page"})
        return {"pages": n, "dpi": 0}
    from PIL import Image
    try:
        with Image.open(path) as im:
            n = int(getattr(im, "n_frames", 1) or 1)
            if n > MAX_FRAMES:
                raise OutputError("FILE_INVALID", {"kind": kind, "reason": "too many pages"})
            dpi = 0
            for i in range(n):
                if i:
                    im.seek(i)
                w, h = im.size
                if w < 1 or h < 1 or w * h > MAX_PIXELS:
                    raise OutputError("FILE_INVALID", {"kind": kind, "reason": "image size"})
                if not dpi:
                    try:
                        dpi = int(round(float((im.info.get("dpi") or (0,))[0])))
                    except (TypeError, ValueError):
                        dpi = 0
            if kind == "jpeg":
                im.seek(0)
                im.verify()
    except OutputError:
        raise
    except Exception:  # noqa: BLE001 —— Pillow 的各種例外（截斷、格式錯、不支援的壓縮）
        raise OutputError("FILE_INVALID", {"kind": kind}) from None
    return {"pages": n, "dpi": dpi if 50 <= dpi <= 2400 else 0}


def to_pdf(files: list[tuple[Path, str]], out: Path, resolution: int = 0) -> int:
    """依序合成一份 PDF，回傳頁數。只有一份 PDF 時原樣搬過去（不重新編碼）。

    `resolution`：JPEG 沒有記解析度時用的 dpi（eSCL 掃描時知道自己要的是多少）。
    失敗丟 `OutputError("PDF_FAILED")`（例如 TIFF 用了 PyMuPDF 不支援的壓縮 —— 呼叫端會改存原始檔）。"""
    import fitz
    from PIL import Image
    if len(files) == 1 and files[0][1] == "pdf":
        n = inspect(files[0][0], "pdf")["pages"]
        shutil.move(str(files[0][0]), str(out))
        return n
    doc = fitz.open()
    try:
        for path, kind in files:
            if kind == "pdf":
                with fitz.open(str(path), filetype="pdf") as src:
                    doc.insert_pdf(src)
            elif kind == "tiff":
                with fitz.open(str(path), filetype="tiff") as img:
                    pdf_bytes = img.convert_to_pdf()
                with fitz.open("pdf", pdf_bytes) as src:
                    doc.insert_pdf(src)
            elif kind == "jpeg":
                with Image.open(path) as im:
                    w_px, h_px = im.size
                    try:
                        own = int(round(float((im.info.get("dpi") or (0,))[0])))
                    except (TypeError, ValueError):
                        own = 0
                dpi = resolution or (own if 50 <= own <= 2400 else 0) or DEFAULT_DPI
                page = doc.new_page(width=w_px * 72.0 / dpi, height=h_px * 72.0 / dpi)
                page.insert_image(page.rect, filename=str(path))
            else:
                raise OutputError("PDF_FAILED", {"kind": kind})
        if doc.page_count < 1:
            raise OutputError("PDF_FAILED", {"reason": "no page"})
        doc.save(str(out), garbage=3, deflate=True)
        return doc.page_count
    except OutputError:
        raise
    except Exception as e:  # noqa: BLE001 —— PyMuPDF / Pillow 的各種例外
        raise OutputError("PDF_FAILED", {"error": e.__class__.__name__}) from None
    finally:
        doc.close()
