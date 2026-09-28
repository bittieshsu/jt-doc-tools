"""辦公文件轉圖片：WebP / JPEG 輸出、指定寬度，以及選的 DPI 真的有效。

## 由來（issue #53，2026-09-28）

使用者把 60～70 頁的簡報逐頁轉成圖放上網站，要兩件事：直接轉出 WebP，
以及直接指定寬度（大圖 1920、縮圖 480）—— 原本只能用 DPI，每份都要自己算。

**查的時候發現 DPI 選項本身就是假的**：這支借用了預覽用的算圖函式，
最長邊被壓在 1800 像素。A4 選 200 / 300 / 400 DPI 產出**一模一樣**
（1265×1790），回應卻寫著選的 DPI；16:9 的簡報頁不管選多少 DPI 都到不了
1920 寬 —— 回報的人「每份都要先算幾 DPI」其實是算不出來的。

判準一律落在**產出的圖片本身**（用 PIL 打開量寬度、看格式），不看回應裡的數字 ——
那個數字原本就在說謊。
"""
from __future__ import annotations

import io
import re
import zipfile

import pytest

fitz = pytest.importorskip("fitz")
from PIL import Image  # noqa: E402

import importlib  # noqa: E402

# `from app.tools.pdf_to_image import router` 拿到的是 APIRouter 物件（套件的
# `__init__` 把同名子模組遮住了），要模組得用 importlib。
R = importlib.import_module("app.tools.pdf_to_image.router")


def _pdf(*sizes, rotate_last=False, crop_last=False) -> bytes:
    doc = fitz.open()
    for w, h in sizes:
        pg = doc.new_page(width=w, height=h)
        pg.insert_text((20, 40), "Page", fontsize=18)
    if rotate_last:
        doc[-1].set_rotation(90)
    if crop_last:
        doc[-1].set_cropbox(fitz.Rect(50, 60, 400, 700))
    out = doc.tobytes()
    doc.close()
    return out


def _convert(client, pdf: bytes, **data):
    files = {"file": ("deck.pdf", io.BytesIO(pdf), "application/pdf")}
    return client.post("/tools/pdf-to-image/convert", files=files,
                       data={k: str(v) for k, v in data.items()})


def _open(client, page: dict) -> Image.Image:
    r = client.get(page["preview_url"])
    assert r.status_code == 200, r.text
    return Image.open(io.BytesIO(r.content)), r


A4 = (595, 842)


# ---------- DPI 真的有效（原本最長邊被壓在 1800） ----------

def test_the_dpi_choices_actually_produce_different_sizes(client):
    widths = {}
    for dpi in (200, 300, 400):
        r = _convert(client, _pdf(A4), dpi=dpi)
        assert r.status_code == 200, r.text
        img, _ = _open(client, r.json()["pages"][0])
        widths[dpi] = img.width
    assert len(set(widths.values())) == 3, (
        f"A4 三種 DPI 產出的寬度是 {widths} —— 選 DPI 沒有作用"
        "（最長邊又被預覽用的上限壓住了？）")
    assert widths[300] == pytest.approx(595 * 300 / 72, abs=1)
    assert widths[400] == pytest.approx(595 * 400 / 72, abs=1)


def test_a_16_by_9_slide_can_reach_1920_wide_by_dpi_alone(client):
    """960×540 pt 的簡報頁、200 DPI ＝ 2667 px。原本被壓到 1800。"""
    r = _convert(client, _pdf((960, 540)), dpi=200)
    img, _ = _open(client, r.json()["pages"][0])
    assert img.width > 1920, img.size


# ---------- 格式 ----------

@pytest.mark.parametrize("fmt, pil_format, media, ext", [
    ("png", "PNG", "image/png", "png"),
    ("webp", "WEBP", "image/webp", "webp"),
    ("jpeg", "JPEG", "image/jpeg", "jpg"),
    ("jpg", "JPEG", "image/jpeg", "jpg"),          # 別名
])
def test_the_output_really_is_the_chosen_format(client, fmt, pil_format, media, ext):
    r = _convert(client, _pdf(A4), dpi=72, format=fmt)
    assert r.status_code == 200, r.text
    j = r.json()
    page = j["pages"][0]
    assert page["preview_url"].endswith("." + ext)
    img, resp = _open(client, page)
    assert img.format == pil_format
    assert resp.headers["content-type"] == media
    # 單頁下載：副檔名與內容一致
    d = client.get(f"/tools/pdf-to-image/download/{j['upload_id']}")
    assert d.status_code == 200
    assert d.headers["content-type"] == media
    assert f'.{ext}' in d.headers["content-disposition"]
    assert Image.open(io.BytesIO(d.content)).format == pil_format


def test_png_stays_the_default(client):
    r = _convert(client, _pdf(A4), dpi=72)
    j = r.json()
    assert j["format"] == "png" and j["quality"] is None
    img, _ = _open(client, j["pages"][0])
    assert img.format == "PNG"


def test_an_unknown_format_is_refused(client):
    r = _convert(client, _pdf(A4), format="gif")
    assert r.status_code == 400
    assert "gif" in r.text


def test_quality_changes_the_file_size(client):
    pdf = _pdf((960, 540))
    sizes = {}
    for q in (30, 95):
        r = _convert(client, pdf, format="webp", width=1280, quality=q)
        sizes[q] = r.json()["pages"][0]["size_bytes"]
    assert sizes[30] < sizes[95], sizes


# ---------- 指定寬度 ----------

SHAPES = [A4, (842, 595), (960, 540), (612, 792), (300.3, 400.7), (1191, 842)]


@pytest.mark.parametrize("fmt", ["png", "webp", "jpeg"])
@pytest.mark.parametrize("width", [480, 1920, 1921])
def test_every_page_comes_out_exactly_the_requested_width(client, fmt, width):
    """包含轉向 90° 與設了裁切框的頁面 —— `page.rect` 是轉向、裁切之後的尺寸。"""
    pdf = _pdf(*SHAPES, (595, 842), rotate_last=True)
    pdf2 = _pdf(A4, crop_last=True)
    for data in (pdf, pdf2):
        r = _convert(client, data, format=fmt, width=width, dpi=400)
        assert r.status_code == 200, r.text
        j = r.json()
        assert j["size_mode"] == "width" and j["width"] == width and j["dpi"] is None
        with fitz.open(stream=data, filetype="pdf") as doc:
            for page, pg in zip(j["pages"], doc):
                img, _ = _open(client, page)
                assert img.width == width, (
                    f"第 {page['index'] + 1} 頁 {pg.rect} 要 {width} px，實際 {img.width}")
                want_h = pg.rect.height * width / pg.rect.width
                assert abs(img.height - want_h) <= 1.5, (img.size, want_h)
                assert page["width_px"] == img.width and page["height_px"] == img.height
                assert page["reduced"] is False


def test_width_wins_over_dpi(client):
    r = _convert(client, _pdf(A4), width=480, dpi=400)
    img, _ = _open(client, r.json()["pages"][0])
    assert img.width == 480


@pytest.mark.parametrize("bad", ["15", "10001", "abc", "-5", "1920.5"])
def test_an_out_of_range_width_is_refused_not_quietly_clamped(client, bad):
    """使用者要的是那個寬度 —— 給他別的寬度就是另一張圖。"""
    r = _convert(client, _pdf(A4), width=bad)
    assert r.status_code == 400, r.text


@pytest.mark.parametrize("empty", ["", "0"])
def test_an_empty_width_means_use_the_dpi(client, empty):
    r = _convert(client, _pdf(A4), width=empty, dpi=100)
    j = r.json()
    assert j["size_mode"] == "dpi" and j["dpi"] == 100
    img, _ = _open(client, j["pages"][0])
    assert img.width == pytest.approx(595 * 100 / 72, abs=1)


# ---------- 上限：縮了要講出來 ----------

def test_page_zoom_caps_the_pixel_count():
    # A3 在 600 DPI ＝ 7016×9921 ≈ 7000 萬像素，超過上限
    zoom, reduced = R.page_zoom(842, 1191, dpi=600, width=None, fmt="png")
    assert reduced is True
    assert (842 * zoom) * (1191 * zoom) <= R.MAX_PIXELS * 1.0001
    # A4 在 400 DPI 在上限內，不可以被縮
    zoom, reduced = R.page_zoom(595, 842, dpi=400, width=None, fmt="png")
    assert reduced is False and zoom == pytest.approx(400 / 72)


def test_page_zoom_respects_the_webp_side_limit():
    # 窄長的頁（100×3000 pt）指定寬 1000 → 高 30000，WebP 單邊最多 16383
    zoom, reduced = R.page_zoom(100, 3000, dpi=200, width=1000, fmt="webp")
    assert reduced is True and 3000 * zoom <= R.WEBP_MAX_SIDE + 0.5
    # PNG 沒有這個限制（3000 萬像素在上限內）
    zoom, reduced = R.page_zoom(100, 3000, dpi=200, width=1000, fmt="png")
    assert reduced is False


def test_a_reduced_page_is_reported_and_really_smaller(client):
    r = _convert(client, _pdf(A4, (100, 3000)), format="webp", width=1000)
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["reduced_pages"] == [2]
    first, second = j["pages"]
    assert first["reduced"] is False
    assert second["reduced"] is True
    img, _ = _open(client, second)
    assert img.height <= R.WEBP_MAX_SIDE
    assert img.width < 1000, "標成縮小了，圖卻沒有變小"


# ---------- 多頁 ZIP ----------

@pytest.mark.parametrize("fmt, ext", [("webp", "webp"), ("jpeg", "jpg")])
def test_zip_entries_carry_the_format_and_the_real_page_number(client, fmt, ext):
    """≥10 頁（字串排序會把 _p10 排到 _p2 前面，v1.11.71 修過）。"""
    n = 12
    pdf = _pdf(*[(300 + i * 20, 400) for i in range(n)])
    r = _convert(client, pdf, format=fmt, dpi=72)
    j = r.json()
    width_by_page = {p["index"] + 1: p["width_px"] for p in j["pages"]}
    d = client.get(f"/tools/pdf-to-image/download/{j['upload_id']}")
    assert d.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(d.content)) as z:
        names = z.namelist()
        assert len(names) == n
        for name in names:
            m = re.search(rf"_p(\d+)\.{ext}$", name)
            assert m, name
            with z.open(name) as fp:
                im = Image.open(io.BytesIO(fp.read()))
            assert im.width == width_by_page[int(m.group(1))], name


def test_the_page_order_regex_does_not_pick_up_other_files(client):
    """同一個上傳編號底下還有 `_in.pdf`、`_name.txt`、上一次的 `.zip` —— 不可以被當成頁面。"""
    r = _convert(client, _pdf(A4, A4), format="webp", dpi=72)
    uid = r.json()["upload_id"]
    first = client.get(f"/tools/pdf-to-image/download/{uid}")
    again = client.get(f"/tools/pdf-to-image/download/{uid}")   # 這時 .zip 已經在了
    with zipfile.ZipFile(io.BytesIO(again.content)) as z:
        assert sorted(z.namelist()) == sorted(
            zipfile.ZipFile(io.BytesIO(first.content)).namelist())
        assert all(n.endswith(".webp") for n in z.namelist())
