"""文件拉正（v1.15.33，第一期：只有自動模式）。

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
    h, w = gray.shape
    # 箭頭形（第二個點凹進去）—— 這才是 isContourConvex 會拒絕的形狀
    concave = [[10, 10], [w // 2, h // 3], [w - 10, 10], [w // 2, h - 10]]
    out, res = SC.straighten_page(gray, quad=concave)
    assert res.quad_found is False, "凹四邊形被當成有效的了"
    assert abs(res.residual) <= 0.3


def test_shuffled_corner_order_is_fixed_not_rejected():
    """使用者拉四個點時順序一定會亂 —— 那要**修正**不是拒絕。"""
    gray = _skewed_page(2.3)
    h, w = gray.shape
    shuffled = [[w - 20, h - 20], [20, 20], [w - 20, 20], [20, h - 20]]
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
    assert float(r.headers["x-straighten-worst-residual"]) <= 0.3
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


def test_the_quad_is_rotated_together_with_the_page():
    """四個角要跟著整頁轉向一起轉。

    只轉影像不轉座標的話，透視校正會抓到完全不相干的區域。開發時實測：
    轉 90° 之後 `quad_is_sane()` 會把它擋掉（長寬對調了），所以**看起來沒事**
    —— 其實是「使用者拉的四個角被安靜地丟掉」，比算錯更難發現。
    """
    import cv2
    import numpy as np

    from app.tools.doc_straighten.straighten_core import _rotate_quad

    img = np.zeros((100, 60), np.uint8)
    img[5, 10] = 255                       # (x=10, y=5)
    for deg, code in ((90, cv2.ROTATE_90_CLOCKWISE),
                      (180, cv2.ROTATE_180),
                      (270, cv2.ROTATE_90_COUNTERCLOCKWISE)):
        rotated = cv2.rotate(img, code)
        ys, xs = np.where(rotated == 255)
        want = (int(xs[0]), int(ys[0]))
        got = tuple(int(v) for v in _rotate_quad(np.float32([[10, 5]]), deg,
                                                 img.shape)[0])
        assert got == want, f"{deg}°：座標換算 {got} 與影像實際 {want} 對不上"


def test_a_user_quad_survives_a_rotation():
    """**行為層**：轉向 ＋ 自己拉的四角一起用時，四角不可以被丟掉。"""
    from app.tools.doc_straighten import straighten_core as SC

    g = _page_gray()
    quad = np.float32([[30, 40], [400, 30], [410, 560], [40, 570]]) \
        if (np := __import__("numpy")) else None
    _out, res = SC.straighten_page(g, quad=quad, page_no=1, rotate_deg=90)
    assert res.quad_found, "轉向之後使用者拉的四個角被丟掉了"
    assert res.rotate_deg == 90


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
    out, res = SC.straighten_page(g, quad=quad, page_no=1, dpi=200)
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
