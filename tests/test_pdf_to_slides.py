"""pdf-to-slides（PDF 轉簡報）測試。

重點在「產出必須是**合法的 Impress 檔**」——這條路最容易壞的地方不是版面，而是
ODF 封裝細節（mimetype / manifest / master page），而 soffice 對這類問題只回一句
「source file could not be loaded」，無從得知哪裡錯。
"""
from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from app.core.office_convert import find_soffice
from app.tools.pdf_to_slides.engines import slides_engine as se

PRES_MIME = "application/vnd.oasis.opendocument.presentation"
_MNS = "urn:oasis:names:tc:opendocument:xmlns:manifest:1.0"


def _make_pdf(path: Path, n: int = 3, landscape: bool = True) -> None:
    import fitz

    d = fitz.open()
    w, h = (720, 405) if landscape else (595, 842)
    for i in range(n):
        pg = d.new_page(width=w, height=h)
        pg.insert_text((60, 100), f"SLIDE{i + 1}", fontsize=28)
        pg.insert_text((60, 160), f"content line for slide {i + 1}", fontsize=13)
    d.save(str(path))
    d.close()


def _soffice_or_skip():
    if not find_soffice():
        pytest.skip("需要 LibreOffice / OxOffice")


# ── 封裝正確性（不需 soffice 也能驗大部分）────────────────────────────────
def test_output_is_presentation_not_drawing(tmp_path):
    """mimetype / manifest / body 都必須是 presentation，否則 Impress 拒載。"""
    _soffice_or_skip()
    src = tmp_path / "in.pdf"
    _make_pdf(src, 2)
    out = tmp_path / "out.odp"
    r = se.convert_pdf_to_slides(src, out, "odp", timeout=300)
    assert r.get("ok"), r
    z = zipfile.ZipFile(out)
    assert z.read("mimetype").decode() == PRES_MIME
    content = z.read("content.xml").decode()
    assert "<office:presentation" in content
    assert "<office:drawing" not in content, "body 仍是 Draw → Impress 會拒載"
    manifest = z.read("META-INF/manifest.xml").decode()
    assert PRES_MIME in manifest
    assert "opendocument.graphics" not in manifest


def test_mimetype_is_first_and_stored(tmp_path):
    """ODF 規定 mimetype 必須是 zip 第一個項目且不壓縮。"""
    _soffice_or_skip()
    src = tmp_path / "in.pdf"
    _make_pdf(src, 1)
    out = tmp_path / "out.odp"
    assert se.convert_pdf_to_slides(src, out, "odp", timeout=300).get("ok")
    z = zipfile.ZipFile(out)
    first = z.infolist()[0]
    assert first.filename == "mimetype"
    assert first.compress_type == zipfile.ZIP_STORED


def test_every_zip_entry_is_listed_in_manifest(tmp_path):
    """**漏列 manifest 會讓整份文件被拒載**（raster fallback 加圖時踩過）。"""
    _soffice_or_skip()
    import re

    src = tmp_path / "in.pdf"
    _make_pdf(src, 2)
    out = tmp_path / "out.odp"
    assert se.convert_pdf_to_slides(src, out, "odp", timeout=300).get("ok")
    z = zipfile.ZipFile(out)
    listed = set(re.findall(r'manifest:full-path="([^"]+)"',
                            z.read("META-INF/manifest.xml").decode()))
    in_zip = {n for n in z.namelist()
              if not n.endswith("/") and n not in ("mimetype",
                                                   "META-INF/manifest.xml")}
    assert not (in_zip - listed), f"未列進 manifest：{sorted(in_zip - listed)}"


def test_slide_size_follows_source_pdf(tmp_path):
    """投影片尺寸要沿用原 PDF 每頁尺寸（不可硬塞成 16:9）。"""
    _soffice_or_skip()
    import re

    src = tmp_path / "portrait.pdf"
    _make_pdf(src, 1, landscape=False)          # A4 直向
    out = tmp_path / "out.odp"
    assert se.convert_pdf_to_slides(src, out, "odp", timeout=300).get("ok")
    styles = zipfile.ZipFile(out).read("styles.xml").decode()
    m = re.search(r'fo:page-width="([\d.]+)cm"\s+fo:page-height="([\d.]+)cm"', styles)
    assert m, "找不到 page-layout 尺寸"
    w, h = float(m.group(1)), float(m.group(2))
    assert h > w, f"直向 PDF 應產生直向投影片，實得 {w}x{h}"
    assert 20.5 < w < 21.5, w                    # 595pt ≈ 21cm


def test_one_master_page_per_slide(tmp_path):
    """每張投影片各自的 master page（尺寸可能逐頁不同）。"""
    _soffice_or_skip()
    src = tmp_path / "in.pdf"
    _make_pdf(src, 3)
    out = tmp_path / "out.odp"
    r = se.convert_pdf_to_slides(src, out, "odp", timeout=300)
    assert r["pages"] == 3, r
    styles = zipfile.ZipFile(out).read("styles.xml").decode()
    assert styles.count("JtSMP") >= 3


# ── 端到端：產出必須真的載得進 Impress ───────────────────────────────────
def _render(doc: Path, outdir: Path) -> Path:
    import subprocess
    import time

    outdir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["pkill", "-9", "soffice.bin"], capture_output=True)
    time.sleep(1)
    subprocess.run([find_soffice(), "--headless",
                    f"-env:UserInstallation=file://{outdir}/cfg",
                    "--convert-to", "pdf", "--outdir", str(outdir), str(doc)],
                   capture_output=True, timeout=300)
    return outdir / (doc.stem + ".pdf")


@pytest.mark.parametrize("fmt", ["odp", "pptx"])
def test_output_loads_and_keeps_pages_and_text(tmp_path, fmt):
    """**核心驗證**：產出載得進 Impress、張數正確、每張文字都在。"""
    _soffice_or_skip()
    import fitz

    src = tmp_path / "in.pdf"
    _make_pdf(src, 3)
    out = tmp_path / f"out.{fmt}"
    r = se.convert_pdf_to_slides(src, out, fmt, timeout=300)
    assert r.get("ok"), r
    assert r["pages"] == 3

    rendered = _render(out, tmp_path / "r")
    assert rendered.exists(), "產出載不進 Impress（多半是 ODF 封裝有誤）"
    d = fitz.open(str(rendered))
    try:
        assert d.page_count == 3, f"應為 3 張，實得 {d.page_count}"
        for i in range(3):
            assert f"SLIDE{i + 1}" in d[i].get_text(), d[i].get_text()[:60]
    finally:
        d.close()


def test_bad_pdf_reports_error_not_crash(tmp_path):
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"not a pdf at all")
    r = se.convert_pdf_to_slides(bad, tmp_path / "o.odp", "odp", timeout=60)
    assert r["ok"] is False
    assert r["error"]


# ── API / 權限 ──────────────────────────────────────────────────────────
def test_api_rejects_bad_format():
    from fastapi.testclient import TestClient

    from app.main import app

    c = TestClient(app)
    r = c.post("/tools/pdf-to-slides/convert",
               files={"file": ("a.pdf", b"%PDF-1.4 x", "application/pdf")},
               data={"output_format": "docx"})
    assert r.status_code == 400
    assert "odp" in r.text or "pptx" in r.text


def test_api_rejects_non_pdf():
    from fastapi.testclient import TestClient

    from app.main import app

    c = TestClient(app)
    r = c.post("/tools/pdf-to-slides/convert",
               files={"file": ("a.txt", b"hello", "text/plain")},
               data={"output_format": "pptx"})
    assert r.status_code == 400


def test_tool_registered_with_search_keywords():
    """新工具必須有中英文搜尋關鍵字（否則使用者搜不到）。"""
    from app.main import _TOOL_ALIASES

    kw = _TOOL_ALIASES.get("pdf-to-slides", "")
    assert kw, "缺搜尋關鍵字"
    for term in ("pptx", "odp", "簡報", "投影片", "presentation"):
        assert term in kw, f"關鍵字缺 {term}"


def test_tool_in_default_role():
    """要在預設角色內，既有客戶升級後才用得到。"""
    from app.core.roles import SEED_ROLES

    default = next((r for r in SEED_ROLES if r["id"] == "default-user"), None)
    assert default and "pdf-to-slides" in default["tools"]
