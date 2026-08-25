"""HEIC / HEIF（iPhone 照片）要真的解得開（GitHub issue #49）。

## 由來

客戶在 Windows 11 上傳 iPhone 照片，畫面回：

    20260825_134332.heic：請求格式錯誤（400）：無法解析圖片：
    cannot identify image file <_io.BytesIO object at 0x000001EB43FB9350>

查下來是**我們承諾了沒做到**：工具頁說明、後端副檔名白名單、前端檔案過濾、
搜尋關鍵字、README —— 五個地方都宣稱支援 HEIC，**但相依裡從來沒有
`pillow-heif`**，而 Pillow 本身不認這個格式。四道關卡都放行，只有真的要解碼
時才爆，而且吐的是 Pillow 的原文（對使用者毫無意義）。

## 這份測試的重點

**不是「有沒有裝套件」，而是「宣稱支援的每一種格式都真的解得開」** ——
副檔名白名單是自動列舉的，日後有人再加一種格式卻沒接上解碼器，這裡就會紅。
"""
from __future__ import annotations

import io

import pytest
from PIL import Image, ImageDraw

from app.core import image_utils
# **`import app.tools.image_to_pdf.router as itp` 會拿到 APIRouter 物件，不是
# 模組** —— 套件的 `__init__.py` 裡有 `from .router import router`，那個屬性把
# 同名的子模組蓋掉了（`import ... as` 綁的是屬性）。所以直接 from 模組取名字。
from app.tools.image_to_pdf.router import _ALLOWED_EXT, _decode_error


def _sample(fmt: str) -> bytes:
    """產生一張該格式的真圖（不是改副檔名的假檔）。"""
    im = Image.new("RGB", (240, 180), (245, 248, 255))
    ImageDraw.Draw(im).rectangle((10, 10, 230, 170), outline=(200, 60, 60), width=4)
    buf = io.BytesIO()
    if fmt in ("heic", "heif"):
        import pillow_heif
        pillow_heif.from_pillow(im).save(buf, format="HEIF", quality=70)
    else:
        im.save(buf, format={"jpg": "JPEG", "jpeg": "JPEG", "tif": "TIFF"}
                .get(fmt, fmt.upper()))
    return buf.getvalue()


# ------------------------------------------------------------------ 1
def test_heif_decoder_is_registered():
    """解碼器要在 `image_utils` 被 import 時就註冊給 Pillow —— 靠每支工具
    各自記得註冊，遲早會有一支忘記（issue #49 就是沒有任何一處註冊）。"""
    assert image_utils.heif_available(), "pillow-heif 沒有裝或註冊失敗"
    assert ".heic" in Image.registered_extensions()


# ------------------------------------------------------------------ 2
@pytest.mark.parametrize("ext", sorted(_ALLOWED_EXT))
def test_every_advertised_format_actually_opens(ext):
    """**白名單自動列舉**：放行的每一種副檔名都要真的解得開。

    這條就是 issue #49 的守門 —— 當時四個地方都放行 .heic，卻沒有任何一處
    接上解碼器。
    """
    fmt = ext.lstrip(".")
    try:
        raw = _sample(fmt)
    except Exception as e:  # 產不出樣本就不是這條測試要管的
        pytest.skip(f"無法產生 {fmt} 樣本：{e}")
    im = Image.open(io.BytesIO(raw))
    im.load()
    assert im.size == (240, 180)


# ------------------------------------------------------------------ 3
def test_heic_goes_all_the_way_into_a_pdf(client):
    """端到端：HEIC → 對外 API → PDF，而且**紙上真的有東西**。

    只驗 HTTP 200 是不夠的 —— 產出一張空白頁也會是 200。
    """
    import fitz
    raw = _sample("heic")
    r = client.post("/tools/image-to-pdf/api/image-to-pdf",
                    files=[("files", ("20260825_134332.heic", raw, "image/heic"))],
                    data={"page_size": "A4"})
    assert r.status_code == 200, r.text[:200]
    doc = fitz.open(stream=r.content, filetype="pdf")
    assert doc.page_count == 1
    assert len(doc[0].get_images()) == 1, "PDF 裡沒有嵌入影像（空白頁）"


# ------------------------------------------------------------------ 4
def test_upload_endpoint_accepts_heic(client):
    raw = _sample("heic")
    r = client.post("/tools/image-to-pdf/upload",
                    files={"file": ("photo.heic", raw, "image/heic")})
    assert r.status_code == 200, r.text[:200]


# ------------------------------------------------------------------ 5
def test_decode_error_is_human_readable():
    """壞檔要給看得懂的訊息，**不可回吐 Pillow 的原文**。

    「cannot identify image file <_io.BytesIO object at 0x...>」正是客戶
    截圖裡那句 —— 看不懂，也不知道下一步該做什麼。
    """
    exc = _decode_error(OSError("cannot identify image file <_io.BytesIO object>"),
                            "photo.jpg")
    assert "cannot identify" not in exc.detail
    assert "_io.BytesIO" not in exc.detail
    assert "photo.jpg" in exc.detail


def test_missing_decoder_tells_the_user_what_to_do(monkeypatch):
    """套件缺席（舊安裝只更新程式碼沒同步相依）時，訊息要說得出解法。"""
    monkeypatch.setattr(image_utils, "heif_available", lambda: False)
    exc = _decode_error(OSError("cannot identify image file"), "IMG_0001.HEIC")
    assert "pillow-heif" in exc.detail
    assert "jtdt update" in exc.detail


# ------------------------------------------------------------------ 6
def test_dependency_is_declared_in_all_the_usual_places():
    """新相依的五處 SOP —— 漏一處就會在某條安裝路徑上缺套件。"""
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    checks = {
        "pyproject.toml": "pillow-heif",
        "requirements.txt": "pillow-heif",
        "github/install.sh": "pillow_heif",
        "github/setup-python.cmd": "pillow_heif",
        "app/cli.py": "pillow_heif",
        "uv.lock": "pillow-heif",
    }
    missing = [f for f, needle in checks.items()
               if needle not in (root / f).read_text(encoding="utf-8")]
    assert not missing, f"這些地方沒有宣告 pillow-heif：{missing}"


def test_dependency_shows_up_in_the_admin_dependency_page():
    """相依套件檢查頁要看得到它 —— 缺套件時管理員得查得出來是哪一個。"""
    from app.core import sys_deps
    keys = {d["key"] for d in sys_deps.collect_sys_deps()}
    assert "pillow-heif" in keys
