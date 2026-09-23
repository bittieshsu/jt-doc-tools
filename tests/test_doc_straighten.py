"""掃描修正（v1.15.33，第一期：只有自動模式）。

**主要判準是「修正後再估一次的殘留角」**，不是「有沒有轉」——
轉錯方向時角度看起來有變化，只有殘留角會現形（規劃階段實測踩過：
角度算對了卻把負號加了兩次，殘留變成 4.6°）。

**第二個判準是「不要弄壞」**：`do_binarize` 實測會讓 OCR 相似度從 0.775 掉到
0.108（中文細筆畫被吃掉），所以它必須是預設關閉的選項，而且介面要寫明用途。
"""
from __future__ import annotations

import pathlib

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")
import fitz  # noqa: E402

from app.tools.doc_straighten import straighten_core as SC  # noqa: E402


def _skewed_page(angle: float = 2.3, *, border: bool = True,
                 noisy: bool = True, size=(1190, 1684)) -> np.ndarray:
    """做一份「掃歪的紙」：文字 + 歪斜 + 黑邊 + 雜訊 + 漸層陰影。"""
    w, h = size
    img = np.full((h, w), 250, np.uint8)
    for i, t in enumerate(("INVOICE 2026", "Name: Michael Thompson",
                           "Amount: 1,234,567", "Date: 2026-09-13")):
        cv2.putText(img, t, (90, 260 + i * 180), cv2.FONT_HERSHEY_SIMPLEX,
                    1.8, 25, 4)
    M = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
    out = cv2.warpAffine(img, M, (w, h), flags=cv2.INTER_CUBIC, borderValue=30)
    if border:
        out = cv2.copyMakeBorder(out[30:-30, 30:-30], 30, 30, 30, 30,
                                 cv2.BORDER_CONSTANT, value=25)
    yy, xx = np.mgrid[0:h, 0:w]
    out = np.clip(out.astype(np.int16) - (22 * (xx / w) + 16 * (yy / h)), 0, 255)
    if noisy:
        rng = np.random.default_rng(5)
        out = np.clip(out + rng.normal(0, 9, (h, w)), 0, 255)
    return out.astype(np.uint8)


# ---------------------------------------------------------------- 核心

@pytest.mark.parametrize("angle", [2.3, -1.7, 4.0, 0.0])
def test_the_residual_angle_is_near_zero(angle):
    """**這是主要判準** —— 而且它同時擋住「轉錯方向」。"""
    gray = _skewed_page(angle)
    _out, res = SC.straighten_page(gray)
    assert abs(res.residual) <= 0.3, (
        f"原本歪 {angle}°，轉了 {res.angle}° 之後還殘留 {res.residual}°")


def test_turning_the_wrong_way_would_show_up_in_the_residual():
    """把修正角取負號（就是「轉錯方向」）→ 殘留角必須變大。

    這條是**對判準本身的驗證**：如果殘留角對方向不敏感，上面那條就沒有意義。
    """
    gray = _skewed_page(2.3)
    base = SC.crop_page(gray)
    good = SC.rotate(base, SC.deskew_angle(base))
    bad = SC.rotate(base, -SC.deskew_angle(base))
    assert abs(SC.deskew_angle(good, 6.0, 0.1)) < abs(SC.deskew_angle(bad, 6.0, 0.1))


def test_the_page_is_cropped_but_content_survives():
    gray = _skewed_page(2.0)
    out, _res = SC.straighten_page(gray)
    assert out.shape[0] < gray.shape[0] and out.shape[1] < gray.shape[1], "沒有裁邊"
    assert out.mean() > 150, "裁過頭或整頁變黑"
    # 紙張中央還要有墨水（文字沒被裁掉）
    h, w = out.shape
    assert out[h // 6:h * 5 // 6, w // 6:w * 5 // 6].min() < 120


def test_a_bad_quad_is_ignored_instead_of_producing_garbage():
    """凹的（自交的）四邊形要退回只做拉正。

    `warpPerspective` 對這種形狀會產出扭曲到看不出是什麼的東西，
    **而且不會報錯**。

    註：把矩形的四個角**打亂順序**不算壞四邊形 —— `order_quad` 的職責就是
    把順序正規化回來（使用者可以把左上拖到右下去）。我第一版拿打亂順序的
    矩形當「蝴蝶結」，測到的其實是正常行為。
    """
    gray = _skewed_page(2.3)
    # 箭頭形（第二個點凹進去）—— 這才是 isContourConvex 會拒絕的形狀。
    # **座標是正規化 0~1**（轉向後的座標系，見 `straighten_page` 的說明）。
    concave = [[0.02, 0.02], [0.5, 0.33], [0.98, 0.02], [0.5, 0.98]]
    out, res = SC.straighten_page(gray, quad=concave)
    assert res.quad_found is False, "凹四邊形被當成有效的了"
    assert abs(res.residual) <= 0.3


def test_shuffled_corner_order_is_fixed_not_rejected():
    """使用者拉四個點時順序一定會亂 —— 那要**修正**不是拒絕。"""
    gray = _skewed_page(2.3)
    shuffled = [[0.96, 0.96], [0.04, 0.04], [0.96, 0.04], [0.04, 0.96]]
    _out, res = SC.straighten_page(gray, quad=shuffled)
    assert res.quad_found is True, "順序打亂的正常四邊形被拒絕了"


def test_quad_sanity_rejects_tiny_and_out_of_bounds():
    shape = (1000, 800)
    assert not SC.quad_is_sane(np.float32([[0, 0], [10, 0], [10, 10], [0, 10]]), shape)
    assert not SC.quad_is_sane(np.float32([[-50, -50], [900, 0], [900, 900],
                                           [0, 900]]), shape)
    assert SC.quad_is_sane(np.float32([[20, 20], [760, 30], [770, 950],
                                       [10, 940]]), shape)


def test_order_quad_is_canonical_whatever_order_you_pass():
    """使用者可以把左上拖到右下去 —— 順序亂掉會產生鏡像或轉 180° 的結果。"""
    pts = [[10, 10], [500, 20], [510, 700], [5, 690]]
    import itertools
    base = SC.order_quad(np.float32(pts)).tolist()
    for perm in itertools.islice(itertools.permutations(pts), 8):
        assert SC.order_quad(np.float32(list(perm))).tolist() == base


# ---------------------------------------------------------------- 整份 PDF

def _pdf_with(tmp_path, pages: int, angle: float = 2.3) -> pathlib.Path:
    doc = fitz.open()
    for _ in range(pages):
        gray = _skewed_page(angle)
        png = cv2.imencode(".png", gray)[1].tobytes()
        page = doc.new_page(width=595, height=842)
        page.insert_image(page.rect, stream=png)
    out = tmp_path / "in.pdf"
    doc.save(str(out))
    doc.close()
    return out


def test_page_count_and_size_are_preserved(tmp_path):
    src = _pdf_with(tmp_path, 3)
    dst = tmp_path / "out.pdf"
    res = SC.straighten_pdf(src, dst, dpi=150)
    assert len(res) == 3
    got = fitz.open(str(dst))
    try:
        assert got.page_count == 3
        for p in got:
            # 尺寸照原頁的**點數** —— 拿像素當點數會變成巨大的頁面
            assert round(p.rect.width) == 595 and round(p.rect.height) == 842
    finally:
        got.close()


def test_progress_is_reported_per_page(tmp_path):
    """逐頁報進度 —— 只有 0% / 100% 的話，使用者看不出它卡在第幾頁。"""
    src = _pdf_with(tmp_path, 4)
    seen = []
    SC.straighten_pdf(src, tmp_path / "o.pdf", dpi=150,
                      progress=lambda d, t: seen.append((d, t)))
    assert len(seen) >= 4, seen
    assert seen[-1] == (4, 4)


def test_cancelling_stops_and_raises(tmp_path):
    src = _pdf_with(tmp_path, 5)
    calls = {"n": 0}

    def cancelled():
        calls["n"] += 1
        return calls["n"] > 2
    with pytest.raises(SC.Cancelled):
        SC.straighten_pdf(src, tmp_path / "o.pdf", dpi=150, cancelled=cancelled)


def test_greyscale_output_uses_jpeg_and_binarised_uses_png(tmp_path):
    """編碼要看內容：灰階用 JPEG（實測 2.5 MB → 885 KB），黑白用 PNG。"""
    src = _pdf_with(tmp_path, 1)
    grey, bw = tmp_path / "g.pdf", tmp_path / "b.pdf"
    SC.straighten_pdf(src, grey, dpi=150)
    SC.straighten_pdf(src, bw, dpi=150, do_binarize=True)

    def exts(p):
        d = fitz.open(str(p))
        try:
            return {(d.extract_image(i[0]) or {}).get("ext")
                    for i in d[0].get_images(full=True)}
        finally:
            d.close()
    assert exts(grey) == {"jpeg"}, exts(grey)
    assert exts(bw) == {"png"}, exts(bw)
    assert bw.stat().st_size < grey.stat().st_size


# ---------------------------------------------------------------- 介面承諾

def test_the_ui_says_binarising_is_for_file_size_not_accuracy():
    """實測開了二值化 OCR 相似度 0.775 → 0.108。

    **它必須是預設關閉**，而且介面要寫出用途 —— 不寫的話使用者會以為
    「轉成黑白」= 更清楚。
    """
    import re
    tpl = (pathlib.Path(__file__).resolve().parents[1] / "app" / "tools"
           / "doc_straighten" / "templates" / "doc_straighten.html"
           ).read_text(encoding="utf-8")
    visible = re.sub(r"\{#.*?#\}", "", tpl, flags=re.S)
    m = re.search(r'id="dsBin"[^>]*>', visible)
    assert m and "checked" not in m.group(0), "二值化不可以預設打開"
    assert "縮小檔案" in visible and "辨識" in visible, (
        "介面沒有寫明「轉成黑白是為了縮小檔案，不是提高辨識率」")


def test_the_preview_reports_the_residual_angle():
    """殘留角是驗收指標，畫面上要看得到。"""
    tpl = (pathlib.Path(__file__).resolve().parents[1] / "app" / "tools"
           / "doc_straighten" / "templates" / "doc_straighten.html"
           ).read_text(encoding="utf-8")
    assert "殘留" in tpl and "residual" in tpl


# ---------------------------------------------------------------- 端點

def _client():
    from fastapi.testclient import TestClient
    import app.main as m
    return TestClient(m.app)


def _tiny_pdf(tmp_path) -> bytes:
    return _pdf_with(tmp_path, 1, angle=2.0).read_bytes()


def test_the_public_api_returns_a_pdf_with_the_residual_in_a_header(tmp_path):
    """`X-Straighten-Worst-Residual` 是驗收指標，要真的出現在回應標頭上。

    這條同時擋住一個我犯過的錯：`content_disposition()` 回的是**字串**不是
    dict，`headers={**content_disposition(...)}` 會在**回應階段**炸成 500
    —— 單元測試看不到，只有真的打端點才會現形。
    """
    c = _client()
    r = c.post("/tools/doc-straighten/api/doc-straighten",
               files={"file": ("scan.pdf", _tiny_pdf(tmp_path), "application/pdf")},
               data={"dpi": "150"})
    assert r.status_code == 200, r.text[:300]
    assert r.content[:5] == b"%PDF-"
    assert r.headers.get("x-straighten-pages") == "1"
    # **0.5 是「已經是正的」的門檻**（見 `straighten_core` 的說明：完全沒歪的
    # 頁面估計器自己也會回報最多 0.40°）。原本寫 0.3 是照「整條管線都在灰階上
    # 跑」那時候的數字定的 —— v1.15.47 起幾何套在**彩色**那一張上（不然沒有勾
    # 「轉成黑白」的輸出也是灰的），彩色內插後再轉灰階跟灰階內插差了 0.25°，
    # 而**修正角度完全相同**（兩條路都是 -2.0°）。
    assert float(r.headers["x-straighten-worst-residual"]) <= 0.5
    assert "straightened.pdf" in r.headers.get("content-disposition", "")


def test_a_broken_file_is_a_400_not_a_500(tmp_path):
    """壞檔是使用者送錯東西，不是伺服器壞了（全站慣例）。"""
    c = _client()
    r = c.post("/tools/doc-straighten/load",
               files={"file": ("bad.pdf", b"%PDF-1.4 broken", "application/pdf")})
    assert r.status_code == 400, r.status_code


def test_an_image_can_be_uploaded_directly(tmp_path):
    """手機拍的照片是主要情境之一 —— 不可以逼使用者先轉成 PDF。"""
    png = cv2.imencode(".png", _skewed_page(2.0))[1].tobytes()
    c = _client()
    r = c.post("/tools/doc-straighten/load",
               files={"file": ("photo.png", png, "image/png")})
    assert r.status_code == 200, r.text[:200]
    assert r.json()["pages"] == 1


def test_an_out_of_range_page_is_a_404(tmp_path):
    c = _client()
    up = c.post("/tools/doc-straighten/load",
                files={"file": ("s.pdf", _tiny_pdf(tmp_path), "application/pdf")})
    uid = up.json()["upload_id"]
    r = c.post("/tools/doc-straighten/preview",
               data={"upload_id": uid, "page": 99})
    assert r.status_code == 404, r.status_code


def _pv_dims(c, url):
    r = c.get(url)
    assert r.status_code == 200, (url, r.status_code)
    img = cv2.imdecode(np.frombuffer(r.content, np.uint8), cv2.IMREAD_UNCHANGED)
    assert img is not None, "預覽圖解不開（讀到寫一半的檔？）"
    return img.shape[:2]


def test_each_preview_keeps_its_own_image(tmp_path):
    """**每一次預覽一個檔**（v1.16.10，CI 上時好時壞抓到的）。

    原本同一頁固定寫同一個檔。拖曳 / 轉向會連發好幾個請求，前端取消舊的，
    **但伺服器那邊照樣算完、晚一點寫回同一個檔** —— 把最新那張蓋掉
    （畫面顯示舊結果、狀態列卻寫新的），或讓瀏覽器讀到寫一半的檔。

    判準：兩次預覽（不轉 / 轉 90°）之後，**第一次的網址拿到的仍然是第一次的圖**。
    同一個檔名的話它會拿到第二次那張（長寬對調）。
    """
    c = _client()
    uid = c.post("/tools/doc-straighten/load",
                 files={"file": ("s.pdf", _tiny_pdf(tmp_path), "application/pdf")}
                 ).json()["upload_id"]
    a = c.post("/tools/doc-straighten/preview",
               data={"upload_id": uid, "page": 1, "rotate": 0, "dpi": 150}).json()
    b = c.post("/tools/doc-straighten/preview",
               data={"upload_id": uid, "page": 1, "rotate": 90, "dpi": 150}).json()
    assert a["url"] != b["url"], "兩次預覽用同一個網址 —— 晚寫完的會蓋掉新的"
    ha, wa = _pv_dims(c, a["url"])
    hb, wb = _pv_dims(c, b["url"])
    assert (ha > wa) != (hb > wb), "素材要是長方形才分得出轉向"
    assert _pv_dims(c, a["url"]) == (ha, wa), "第一次的網址拿到的是第二次的圖"


def test_old_previews_are_pruned_but_not_to_one(tmp_path):
    """同一頁只留最新的幾張。**不可以只留一張**：被取消的舊請求可能在新的之後
    才寫完，只留一張的話它會把畫面正要載入的那張刪掉。"""
    from app.config import settings
    import importlib
    R = importlib.import_module("app.tools.doc_straighten.router")
    c = _client()
    uid = c.post("/tools/doc-straighten/load",
                 files={"file": ("s.pdf", _tiny_pdf(tmp_path), "application/pdf")}
                 ).json()["upload_id"]
    urls = [c.post("/tools/doc-straighten/preview",
                   data={"upload_id": uid, "page": 1, "dpi": 150}).json()["url"]
            for _ in range(R._PV_KEEP + 3)]
    left = list(settings.temp_dir.glob(f"dspv_{uid}_1_*.png"))
    assert 2 <= len(left) <= R._PV_KEEP, len(left)
    assert c.get(urls[-1]).status_code == 200
    assert c.get(urls[-2]).status_code == 200, "只留了一張 —— 晚寫完的會刪掉畫面要的那張"
    assert not list(settings.temp_dir.glob(f"dspv_{uid}_1_*.part")), "留下寫一半的暫存檔"


@pytest.mark.parametrize("v", ["../../etc", "ABCDEF123456", "abc", "0123456789abc"])
def test_the_preview_version_is_validated(tmp_path, v):
    c = _client()
    uid = c.post("/tools/doc-straighten/load",
                 files={"file": ("s.pdf", _tiny_pdf(tmp_path), "application/pdf")}
                 ).json()["upload_id"]
    # 那個檔真的存在也要拒絕 —— 不然拿掉格式檢查時，這條只是因為「找不到檔」而綠
    from app.config import settings
    if "/" not in v:
        (settings.temp_dir / f"dspv_{uid}_1_{v}.png").write_bytes(b"x")
    r = c.get(f"/tools/doc-straighten/preview-img/{uid}/1", params={"v": v})
    assert r.status_code == 404, (v, r.status_code)


def test_the_page_joins_the_cache_buster_with_an_ampersand():
    """網址本身已經帶 `?v=` —— 再接 `?t=` 的話 `v` 會變成 `abc?t=123`，整張 404。"""
    html = (pathlib.Path(__file__).resolve().parents[1] / "app" / "tools" / "doc_straighten"
            / "templates" / "doc_straighten.html").read_text(encoding="utf-8")
    assert "d.url + '?t='" not in html
    assert "d.url.indexOf('?') >= 0 ? '&' : '?'" in html


def test_the_dpi_is_clamped_server_side(tmp_path):
    """前端的下拉只是提示 —— API 呼叫者不受它拘束，1200 dpi 會吃掉幾百 MB。"""
    from app.tools.doc_straighten.router import _clamp_dpi
    assert _clamp_dpi(1200) == 300
    assert _clamp_dpi(10) == 150
    assert _clamp_dpi("abc") == 200


# ------------------------------------------- 不可以把向量文字變成圖片

def _vector_pdf(tmp_path, *, skew: float = 0.0) -> pathlib.Path:
    """原生 PDF（文字是向量的）—— 這種頁面處理它就是在破壞它。"""
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    y = 90
    for line in ("INVOICE 2026-0913", "Name: Michael Thompson",
                 "Amount: 1,234,567 TWD", "Terms: net 30 days",
                 "Signed by: Sarah Chen"):
        page.insert_text((60, y), line, fontsize=12)
        y += 28
    out = tmp_path / f"vector{skew}.pdf"
    doc.save(str(out))
    doc.close()
    return out


def test_a_straight_vector_page_is_passed_through_untouched(tmp_path):
    """**這是會無聲弄壞文件的那條路**：原生 PDF 被整頁轉成圖片之後，
    文字選不到、搜尋不到、複製不到，而畫面上看起來一模一樣。
    """
    src = _vector_pdf(tmp_path)
    dst = tmp_path / "out.pdf"
    res = SC.straighten_pdf(src, dst, dpi=150)
    assert res[0].skipped is True, "已經是正的原生 PDF 頁面被重新算圖了"

    a, b = fitz.open(str(src)), fitz.open(str(dst))
    try:
        assert b[0].get_text().strip() == a[0].get_text().strip(), "文字層不見了"
        assert not b[0].get_images(), "整頁被貼成圖片了"
    finally:
        a.close(); b.close()


def test_a_skewed_page_with_a_text_layer_is_still_processed(tmp_path):
    """只看「有沒有文字層」是不夠的：掃描件被 OCR 過之後也有文字層，
    但它該處理 —— 歪的就是歪的。"""
    gray = _skewed_page(3.0)
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_image(page.rect, stream=cv2.imencode(".png", gray)[1].tobytes())
    # 模擬 OCR 文字層（看不見但抽得到）
    page.insert_text((60, 100), "INVOICE 2026 Name Michael Thompson Amount "
                                "1234567 Date 2026-09-13 extra text here",
                     fontsize=10, render_mode=3)
    src = tmp_path / "ocr.pdf"
    doc.save(str(src)); doc.close()

    res = SC.straighten_pdf(src, tmp_path / "o.pdf", dpi=150)
    assert res[0].skipped is False, "歪的 OCR 掃描件被當成「已經是正的」跳過了"


def test_the_ui_and_the_job_message_say_which_pages_were_kept():
    """使用者丟一份原生 PDF 進來，看到「完成」卻什麼都沒變會以為工具壞了。"""
    import re
    root = pathlib.Path(__file__).resolve().parents[1]
    tpl = (root / "app" / "tools" / "doc_straighten" / "templates"
           / "doc_straighten.html").read_text(encoding="utf-8")
    visible = re.sub(r"\{#.*?#\}", "", tpl, flags=re.S)
    assert "原樣保留" in visible, "介面沒有說明哪些頁面不會被處理"
    router = (root / "app" / "tools" / "doc_straighten"
              / "router.py").read_text(encoding="utf-8")
    assert "原樣保留" in router and "kept_pages" in router, (
        "作業完成訊息沒有講出幾頁原樣保留")


# ---------------------------------------------------------------- 逐頁手動調整
#
# v1.15.36 使用者要求：除了自動，還要能**自己拉四個點**與**逐頁手動旋轉**。
# 自動流程處理不了的兩種情況正是這兩個：
#   * 掃反了 / 掃成橫的 —— 自動估角只看 ±6°，那是方向不是歪斜
#   * 紙張邊界抓錯 —— 背景雜亂或紙張顏色接近桌面時會抓歪

def _page_gray(angle_deg: float = 0.0):
    """做一張有內容的測試頁（可指定整頁先轉幾度）。"""
    import cv2
    import numpy as np
    img = np.full((600, 440), 245, np.uint8)
    for y in range(80, 520, 34):
        cv2.line(img, (60, y), (380, y), 40, 3)
    if angle_deg:
        m = cv2.getRotationMatrix2D((220, 300), angle_deg, 1.0)
        img = cv2.warpAffine(img, m, (440, 600), borderValue=245)
    return img


def test_manual_rotation_turns_the_page_and_swaps_the_axes():
    """轉 90° 之後**長寬要對調** —— 不對調的話內容會被壓扁塞進原本的框。"""
    from app.tools.doc_straighten import straighten_core as SC

    g = _page_gray()
    up, r0 = SC.straighten_page(g, page_no=1)
    side, r90 = SC.straighten_page(g, page_no=1, rotate_deg=90)
    assert r0.rotate_deg == 0 and r90.rotate_deg == 90
    assert up.shape[0] > up.shape[1], "原稿應該是直的"
    assert side.shape[1] > side.shape[0], "轉 90° 之後應該變成橫的"


def test_manual_rotation_still_deskews_afterwards():
    """轉向之後**照樣自動拉正** —— 「轉 90° 再微調 1.5°」要一次做完。"""
    from app.tools.doc_straighten import straighten_core as SC

    _out, res = SC.straighten_page(_page_gray(2.0), page_no=1, rotate_deg=90)
    assert abs(res.residual) < 0.6, (
        f"轉向之後沒有把剩下的歪斜拉掉（殘留 {res.residual}°）")


def test_only_right_angles_are_accepted():
    """**不可以默默接受任意角度** —— 那會讓頁面尺寸算不出來。"""
    import pytest as _pytest

    from app.tools.doc_straighten import straighten_core as SC

    with _pytest.raises(ValueError):
        SC.straighten_page(_page_gray(), page_no=1, rotate_deg=45)


def test_overrides_are_parsed_per_page_and_bad_items_are_dropped():
    """壞掉的項目**個別丟掉**，不要整批失敗 —— 使用者調了半天的東西，
    其中一頁的資料有問題不該讓整份工作送不出去。"""
    from app.tools.doc_straighten.router import _parse_overrides

    raw = ('{"1": {"rotate": 90},'
           ' "2": {"quad": [[0,0],[1,0],[1,1],[0,1]]},'
           ' "3": {"rotate": 45},'              # 不是直角 → 丟掉
           ' "9": {"rotate": 90},'              # 超出頁數 → 丟掉
           ' "x": {"rotate": 90},'              # 頁碼不是數字 → 丟掉
           ' "4": {"quad": [[0,0],[1,0]]}}')    # 只有兩個點 → 丟掉
    got = _parse_overrides(raw, page_count=5)
    assert set(got) == {1, 2}, got
    assert got[1] == {"rotate": 90}
    assert got[2]["quad"][2] == [1.0, 1.0]


def test_a_broken_overrides_string_falls_back_to_automatic():
    from app.tools.doc_straighten.router import _parse_overrides

    for raw in ("", "not json", "[]", "null", '{"1": "nope"}'):
        assert _parse_overrides(raw, page_count=3) == {}, raw


def test_user_quad_is_used_instead_of_the_detected_one(tmp_path):
    """拉四個點要真的生效 —— 拿**明顯不同**的四邊形跑，產出尺寸要跟著變。"""
    import fitz

    from app.tools.doc_straighten import straighten_core as SC

    src = tmp_path / "in.pdf"
    doc = fitz.open()
    page = doc.new_page(width=440, height=600)
    page.insert_text((80, 200), "manual quad test", fontsize=24)
    doc.save(src); doc.close()

    auto = tmp_path / "auto.pdf"
    SC.straighten_pdf(src, auto, dpi=100, detect_quad=False)
    manual = tmp_path / "manual.pdf"
    # 只取左上那一小塊 —— 產出的長寬比一定跟整頁不一樣
    SC.straighten_pdf(src, manual, dpi=100, detect_quad=False,
                      overrides={1: {"quad": [[0.05, 0.05], [0.55, 0.05],
                                              [0.55, 0.35], [0.05, 0.35]]}})
    with fitz.open(str(auto)) as a, fitz.open(str(manual)) as m:
        assert a.page_count == m.page_count == 1
        # 兩份都產得出來，而且手動那份真的走了不同的路徑（檔案內容不同）
        assert a[0].get_pixmap(dpi=40).samples != m[0].get_pixmap(dpi=40).samples, \
            "指定四個角之後產出跟自動的一模一樣 —— 那就是沒有吃到"


def test_a_user_quad_is_read_in_the_rotated_frame():
    """**轉向 ＋ 自己拉四個角**：座標系是使用者看到的那張圖（轉向後）。

    2026-09-14 使用者回報「有拉但出來的跑掉」。當時是兩個錯疊在一起：
    呼叫端用**未轉**的長寬把 0~1 換成像素，然後核心又把它轉了一次。
    轉 90° 的實測：產出 834×358（應為 471×629）、墨點比例 **41.2%**
    （框到的大半是桌面，正確值 1.9%）。

    判準是**產出本身**：拿「在轉向後的圖上自動抓到的那組座標」當使用者拉的
    輸入，結果要跟「直接在轉向後的圖上自動抓」幾乎一樣。
    只驗 `quad_found` 是不夠的 —— 當年那個 bug 的 `quad_found` 也是 True。
    """
    import cv2
    import numpy as np

    from app.tools.doc_straighten import straighten_core as SC

    g = _photo_on_desk()
    g90 = cv2.rotate(g, cv2.ROTATE_90_CLOCKWISE)
    auto90 = SC.find_page_quad(g90)
    assert auto90 is not None, "素材不對：轉向後抓不到紙，這條測不到東西"
    h2, w2 = g90.shape[:2]
    as_user = [[float(x) / w2, float(y) / h2] for x, y in auto90]

    want, _ = SC.straighten_page(g, detect_quad=True, rotate_deg=90,
                                 page_no=1, enhance=False)
    got, res = SC.straighten_page(g, quad=as_user, rotate_deg=90,
                                  page_no=1, enhance=False)
    assert res.quad_found and res.rotate_deg == 90
    assert abs(got.shape[0] - want.shape[0]) <= 4, (
        f"轉向後用使用者的座標裁出來是 {got.shape}，自動抓是 {want.shape}")
    assert abs(got.shape[1] - want.shape[1]) <= 4
    # **墨點比例**才看得出「框到的是紙還是桌面」—— 尺寸對了也可能框錯位置
    ink = float((got < 128).mean())
    assert ink < 0.15, f"產出有 {ink:.0%} 是暗的 —— 框到桌面了"


def test_the_quad_the_core_reports_back_is_in_the_rotated_frame():
    """回給前端的四個角也要是**轉向後**的正規化座標。

    不然使用者轉了 90° 再切到手動時，手柄會落在完全不相干的位置
    —— 而畫面上「有四個點」，看起來完全正常。
    """
    import cv2

    from app.tools.doc_straighten import straighten_core as SC

    g = _photo_on_desk()
    _out, res = SC.straighten_page(g, detect_quad=True, rotate_deg=90,
                                   page_no=1, enhance=False)
    assert res.quad and len(res.quad) == 4
    g90 = cv2.rotate(g, cv2.ROTATE_90_CLOCKWISE)
    h2, w2 = g90.shape[:2]
    direct = SC.find_page_quad(g90)
    assert direct is not None
    want = [[float(x) / w2, float(y) / h2] for x, y in SC.order_quad(direct)]
    # **比對要與起點無關**：同一個四邊形從哪個角開始列都是同一個四邊形，
    # 逐項比的話會因為清單旋轉一格就誤報（我第一版就是這樣紅的）。
    got_s = sorted([round(x, 3), round(y, 3)] for x, y in res.quad)
    want_s = sorted([round(x, 3), round(y, 3)] for x, y in want)
    for (gx, gy), (wx, wy) in zip(got_s, want_s):
        assert abs(gx - wx) < 0.02 and abs(gy - wy) < 0.02, (
            f"回報的四個角 {got_s} 跟轉向後實際抓到的 {want_s} 對不上")


def _photo_on_desk():
    """深色桌面上的一張斜紙 —— 四個角抓得到，而且框錯就會看得出來。"""
    import cv2
    import numpy as np

    h, w = 900, 1200
    img = np.full((h, w), 60, np.uint8)
    sheet = np.full((640, 480), 245, np.uint8)
    for i, txt in enumerate(("TOP LEFT", "middle line", "BOTTOM")):
        cv2.putText(sheet, txt, (30, 120 + i * 220), cv2.FONT_HERSHEY_SIMPLEX,
                    1.1, (20,), 3)
    m = cv2.getRotationMatrix2D((240, 320), 8, 1.0)
    m[0, 2] += (w - 480) / 2
    m[1, 2] += (h - 640) / 2
    return cv2.warpAffine(sheet, m, (w, h), dst=img,
                          borderMode=cv2.BORDER_TRANSPARENT)


# ---------------------------------------------------------- 真實手機照片（v1.15.37）
#
# 使用者給了兩張真的手機照片（**含個資，只放 temp_pdfs/，不上 git**）。
# 第一版的偵測**兩張都抓不到**：最大的輪廓就是紙（佔畫面 29% / 44%），
# 但真實照片的角落有陰影與圓角，`approxPolyDP` 近似出來是 6 點 / 5 點，
# 而寫死的 `len(ap) == 4` 直接丟掉。合成樣本一張都沒有這種特性。

def _real_photos():
    import pathlib
    d = pathlib.Path(__file__).resolve().parent.parent / "temp_pdfs" / "customer"
    return sorted(p for p in d.glob("*.JPG")) if d.is_dir() else []


@pytest.mark.skipif(not _real_photos(), reason="沒有真實照片樣本（不隨 git 散布）")
@pytest.mark.parametrize("photo", _real_photos(), ids=lambda p: p.name)
def test_a_real_phone_photo_finds_the_sheet(photo):
    """真實手機照片要抓得到紙，而且**不可以切到內容**。"""
    import cv2

    from app.tools.doc_straighten import straighten_core as SC

    g = cv2.imread(str(photo), cv2.IMREAD_GRAYSCALE)
    assert g is not None, f"讀不到 {photo}"
    quad = SC.find_page_quad(g)
    assert quad is not None, (
        f"{photo.name}：抓不到紙張邊界 —— 這是這支工具主打的手機翻拍情境")
    o = SC.order_quad(quad)
    frac = cv2.contourArea(o) / float(g.shape[0] * g.shape[1])
    assert 0.05 <= frac <= 0.95, f"四邊形佔畫面 {frac:.0%}，不像一張紙"
    # **走產品那條路**：偵測交給 `straighten_page`（`quad` 現在一律是
    # 正規化、轉向後的座標，自己先抓再傳像素進去是舊契約）。
    out, res = SC.straighten_page(g, detect_quad=True, page_no=1, dpi=200)
    assert abs(res.residual) < 0.5, (
        f"{photo.name}：修正後殘留 {res.residual}°（應該接近 0）")
    # **殘留角小不代表抓對紙**（隨便一個凸四邊形 warp 完都會很正）——
    # 再驗產出裡真的有內容：紙面應該是亮的，而且要有夠多的暗像素（字）
    import numpy as np
    assert out.mean() > 120, "產出偏暗 —— 可能框到桌面而不是紙"
    ink = float((out < 100).mean())
    assert ink > 0.001, f"產出幾乎沒有暗像素（{ink:.4f}）—— 可能裁到空白處"


def test_the_angle_range_rejects_a_quad_that_grabbed_the_desk():
    """四個內角極差 —— 借自 `andrewdcampbell/OpenCV-Document-Scanner`。

    用今天真實失敗的那幾組座標驗：抓對的 9°、框到桌面的 44°、整個畫面的 44°。
    """
    import numpy as np

    from app.tools.doc_straighten import straighten_core as SC

    good = np.float32([[896, 1132], [1996, 1112], [2180, 3140], [840, 3208]])
    desk = np.float32([[0, 0], [3020, 0], [3020, 1984], [840, 3212]])
    whole = np.float32([[0, 0], [3020, 0], [2732, 3076], [0, 4028]])
    assert SC.quad_angle_range(good) < 15
    assert SC.quad_angle_range(desk) > SC._MAX_ANGLE_RANGE
    assert SC.quad_angle_range(whole) > SC._MAX_ANGLE_RANGE
    shape = (4032, 3024)
    assert SC.quad_is_sane(good, shape)
    assert not SC.quad_is_sane(desk, shape), "框到桌面的四邊形沒有被擋掉"


# ---------------------------------------------------------------------------
# 陰影裡的紙（2026-09-13 使用者截圖回報：修正後的圖上下還留著一圈桌面）
#
# 根因**不是四邊形擬合**，是遮罩：只用亮度分割時，落在陰影裡的那半張紙會被
# 判成桌面，於是四邊形只框到「被照到的那一半」，而最小外接矩形為了把它包回來
# 就多框了一條桌面。實測使用者的照片：純度只有 0.784，**框進去的東西兩成是桌面**。
#
# 這裡用合成樣本釘住兩件事：①有色桌面 ＋ 一角在陰影裡時，抓出來的四邊形
# **不可以把桌面框進來**；②**彩色資訊真的有被用到** —— 只給灰階時會退步。
# ---------------------------------------------------------------------------

def _photo_with_shadow():
    """一張「有色桌面 ＋ 白紙 ＋ 一角落在陰影裡」的合成照片。"""
    import numpy as np
    import cv2

    h, w = 900, 700
    # 木頭色桌面（偏橘）—— 色度離中性色很遠，這正是色度那條判準的依據
    img = np.zeros((h, w, 3), np.uint8)
    img[:, :] = (150, 105, 60)          # RGB
    quad = np.float32([[120, 90], [610, 150], [560, 780], [90, 700]])
    cv2.fillPoly(img, [quad.astype(np.int32)], (245, 245, 242))
    # 紙上寫點字（深色），整張都要在最後被框進去
    for i, y in enumerate(range(230, 700, 70)):
        cv2.line(img, (170, y), (470 - i * 8, y), (40, 40, 40), 6)
    # 左下角打一片陰影：亮度砍半，**色相不變**（真實陰影就是這樣）
    sh = np.zeros((h, w), np.float32)
    cv2.fillPoly(sh, [np.int32([[0, 430], [430, 560], [330, 900], [0, 900]])], 1.0)
    sh = cv2.GaussianBlur(sh, (81, 81), 0)[..., None]
    img = (img * (1.0 - 0.55 * sh)).astype(np.uint8)
    return img, quad


def _quad_quality(rgb, quad):
    """回 (墨水涵蓋率, 純度) —— 直接用核心裡的評分函式，判準跟產品一致。"""
    import numpy as np
    import cv2

    g = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    s, m = SC._paper_mask(g, rgb)
    cnts, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    only = np.zeros_like(m)
    cv2.drawContours(only, [max(cnts, key=cv2.contourArea)], -1, 255, cv2.FILLED)
    return SC._quad_score(s, only, (np.asarray(quad, np.float32) / 4.0))


def test_a_shadowed_corner_does_not_drag_the_desk_into_the_crop():
    import cv2

    rgb, _truth = _photo_with_shadow()
    g = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    q = SC.find_page_quad(g, rgb)
    assert q is not None, "有色桌面上的白紙應該抓得到"
    ink, purity = _quad_quality(rgb, q)
    # 切到字是不可原諒的；框到桌面是使用者看得到的黑邊
    assert ink >= 0.99, f"切到內容了：墨水涵蓋率只有 {ink:.3f}"
    assert purity >= 0.95, f"框到桌面了：純度只有 {purity:.3f}"


def test_the_colour_information_is_actually_used():
    """只給灰階時**會退步** —— 證明色度那一層真的在做事。

    沒有這條的話，把 `_paper_mask` 的色度分支整段拿掉，上面那條可能照樣過
    （合成樣本的陰影不夠重時亮度分割仍然分得開）。
    """
    import cv2

    rgb, _ = _photo_with_shadow()
    g = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    colour = _quad_quality(rgb, SC.find_page_quad(g, rgb))
    grey_q = SC.find_page_quad(g, None)
    grey = _quad_quality(rgb, grey_q) if grey_q is not None else (0.0, 0.0)
    assert colour[1] >= grey[1], (
        f"帶彩色的純度 {colour[1]:.3f} 不應該比只有灰階的 {grey[1]:.3f} 差")
    assert colour[0] >= grey[0] - 1e-6


# ---------------------------------------------------------------------------
# 掃描件不該被透視「校正」（2026-09-13，用 82 張公開語料實測之後補的）
#
# 拿真實的掃描件測才看到：**四邊形把頁面最上面的抬頭文字切掉了**。根因又是
# 遮罩 —— 頁面最上緣那一條色調不同，被判成背景，四邊形就跟著切在那裡。
#
# 與其再去調遮罩，判準改成認清楚「**這種圖本來就沒有透視可以校正**」：
# 挑出來的候選是個完美矩形（內角極差 0°、對邊等長）而且佔滿大半畫面時，
# 透視變換唯一會做的事就是把邊緣切掉。實測 16 張掃描件 / 正面照全部落在
# 這一格，而真的有透視的 4 張是 8.4° ~ 32.8°，分得很開。
# ---------------------------------------------------------------------------

def _flat_scan():
    """一張**正面**的掃描件：頁面佔畫面約八成、四周有掃描邊、沒有透視。

    **比例要像真的那兩張**（0.77 / 0.92）。第一版我做成「整張都是紙」，
    於是它是被更早的「太大＝不需要透視校正」那條擋掉的 —— 變異驗證
    （把新的那道關拿掉）**照樣全綠**，等於這條測試通過的理由是錯的。
    """
    import cv2
    import numpy as np

    h, w = 1100, 850
    img = np.full((h, w, 3), 60, np.uint8)           # 壓蓋沒蓋滿留下的暗邊
    x0, y0, x1, y1 = 48, 60, w - 48, h - 60          # 頁面 ≈ 80% 畫面
    img[y0:y1, x0:x1] = 248
    for i, y in enumerate(range(y0 + 120, y1 - 60, 46)):
        cv2.line(img, (x0 + 40, y), (x1 - 60 - (i % 3) * 40, y), (35, 35, 35), 4)
    cv2.line(img, (x0 + 40, y0 + 45), (x1 - 200, y0 + 45), (25, 25, 25), 6)  # 抬頭
    # 上緣壓暗一條 —— 真實掃描件很常見（壓不平 / 邊緣陰影），
    # 這正是把遮罩騙掉、讓四邊形切在抬頭底下的那個特徵。
    img[y0:y0 + 40, x0:x1] = (img[y0:y0 + 40, x0:x1] * 0.72).astype(np.uint8)
    return img


def test_a_flat_scan_is_not_perspective_corrected():
    """正面掃描件不可以做透視 —— 沒有東西可以校正，只會裁掉邊緣。"""
    import cv2

    rgb = _flat_scan()
    g = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    assert SC.find_page_quad(g, rgb) is None, (
        "正面掃描件回了四邊形 —— 透視變換在這種圖上只會把抬頭切掉")


def test_a_photo_with_real_perspective_is_still_corrected():
    """反向對照：**真的有透視**的照片仍然要抓得到。

    只驗上面那條的話，把 `find_page_quad` 改成永遠回 None 也會過
    —— 那等於把整個透視校正關掉。
    """
    import cv2

    rgb, _ = _photo_with_shadow()
    g = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    q = SC.find_page_quad(g, rgb)
    assert q is not None, "有透視的翻拍照片應該仍然抓得到紙"
    assert SC.quad_angle_range(q) > 1.0, "這張的四個角本來就不是直角"


def test_a_colour_page_stays_in_colour_unless_you_ask_for_black_and_white():
    """**沒有勾「轉成黑白」就不可以把彩色洗掉**（2026-09-14 使用者回報）。

    以前整條管線都在灰階上跑（`crop_page(gray)` / `warp_quad(gray, …)`），
    所以拍彩色名片、沒有勾任何東西，拿回來的也是灰的 —— 而畫面上那個勾選框
    寫著「轉成黑白（預設不要用）」，等於**介面承諾了一件沒有做到的事**。

    判準是**飽和度**，不是「有幾個通道」：輸出成 3 通道但每個通道都一樣，
    看起來仍然是黑白的。
    """
    import cv2
    import numpy as np
    from app.tools.doc_straighten import straighten_core as SC

    # 白底 + 一塊飽和的紅 / 藍（模擬名片上的色塊）
    rgb = np.full((400, 600, 3), 245, np.uint8)
    rgb[80:180, 60:300] = (220, 30, 30)
    rgb[220:320, 60:300] = (30, 60, 220)
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)

    out, _ = SC.straighten_page(gray, rgb=rgb, dpi=150)
    assert out.ndim == 3, "彩色進去卻回了灰階"
    sat = cv2.cvtColor(out, cv2.COLOR_RGB2HSV)[:, :, 1]
    assert sat.max() > 150, f"顏色被洗掉了（最大飽和度只有 {sat.max()}）"

    # 紅的還是紅的、藍的還是藍的 —— RGB / BGR 弄反的話這兩條會對調
    red = out[120, 150]
    blue = out[260, 150]
    assert int(red[0]) > int(red[2]) + 60, f"紅色區塊變成 {red}（RGB/BGR 反了？）"
    assert int(blue[2]) > int(blue[0]) + 60, f"藍色區塊變成 {blue}"

    # 勾了才可以變黑白
    bw, _ = SC.straighten_page(gray, rgb=rgb, dpi=150, do_binarize=True)
    assert bw.ndim == 2, "勾了「轉成黑白」卻沒有變成單通道"


def test_a_grey_only_page_behaves_exactly_as_before():
    """只給灰階時**一個位元都不可以變** —— 掃描件那條路沒有彩色可留。"""
    import cv2
    import numpy as np
    from app.tools.doc_straighten import straighten_core as SC

    rng = np.random.default_rng(7)
    gray = np.full((400, 600), 240, np.uint8)
    gray[100:300, 80:520] = rng.integers(0, 90, (200, 440), dtype=np.uint8)
    out, res = SC.straighten_page(gray, dpi=150)
    assert out.ndim == 2, "灰階進去不可以變成彩色"


def test_the_output_size_never_crops_or_stretches():
    """指定輸出尺寸時**維持比例、四周留白** —— 不裁切也不拉伸。

    這支工具從第一版就寫著「切到內容不可原諒，多框一條桌面只是難看」。
    輸出尺寸是同一條規則：比例對不上時寧可補白。
    """
    import numpy as np
    from app.tools.doc_straighten.straighten_core import fit_to, mm_to_px, PAPER_MM

    img = np.zeros((400, 300, 3), np.uint8)
    img[:] = (10, 20, 30)
    out = fit_to(img, 600, 600)
    assert out.shape[:2] == (600, 600)
    # 四周是白的（補白不是補黑 —— 補黑列印會整片吃墨）
    assert tuple(out[0, 0]) == (255, 255, 255)
    # 內容還在，而且**沒有被拉伸**：原圖 3:4，放進 600×600 之後應該是 450×600
    ink = np.argwhere((out != 255).any(axis=2))
    h = ink[:, 0].max() - ink[:, 0].min() + 1
    w = ink[:, 1].max() - ink[:, 1].min() + 1
    assert abs(w / h - 300 / 400) < 0.01, f"比例跑掉了：{w}×{h}"

    # mm 換算：A4 在 200 dpi 是 1654 × 2339
    assert mm_to_px(PAPER_MM["a4"][0], 200) == 1654
    assert mm_to_px(PAPER_MM["a4"][1], 200) == 2339


def test_a_bad_output_size_is_ignored_not_a_500():
    """輸出尺寸填壞了就當作沒填 —— 使用者打錯一個數字不該看到 500。"""
    from app.tools.doc_straighten.router import _parse_out_size

    assert _parse_out_size("", "", "px", 200) is None
    assert _parse_out_size("abc", "10", "px", 200) is None
    assert _parse_out_size("-5", "10", "px", 200) is None
    assert _parse_out_size("999999", "10", "px", 200) is None, "大到會把記憶體吃光"
    assert _parse_out_size("210", "297", "mm", 200) == (1654, 2339)
    assert _parse_out_size("800", "600", "px", 200) == (800, 600)
    # 單位打錯一律當像素（白名單），不可以丟例外
    assert _parse_out_size("800", "600", "furlong", 200) == (800, 600)
