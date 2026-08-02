"""頁面尺寸統一。

## 由來

標案、工程文件常是 A3 圖說混 A4 內文。送印時印表機每換一種尺寸就停一次，
裝訂完也會有幾頁凸出來。

## 這一份守的重點

1. **內容不可以被光柵化** —— 文字要仍然選得到。轉成圖片是最省事但最糟的作法
   （檔案暴增、字不能選、放大就糊）。
2. **原本就是目標尺寸的頁面不要動** —— 重放一次會多一層物件、細線可能被重新取樣。
3. **直橫混排的政策要明確**：橫的圖說塞進直的 A4 會縮到看不清楚，所以預設
   跟著原頁的方向。
4. **會裁掉內容時要講** —— 「置中不縮放」遇到比紙張大的頁面會裁掉，那是使用者
   選的，但不能無聲發生。
"""
from __future__ import annotations

import fitz
import pytest

from app.tools.pdf_page_size import resize_core as RC


def _doc(sizes: list[tuple[float, float]]) -> fitz.Document:
    d = fitz.open()
    for i, (w, h) in enumerate(sizes):
        pg = d.new_page(width=w, height=h)
        pg.insert_text((50, 60), f"PAGE{i + 1}", fontsize=20)
        pg.draw_rect(fitz.Rect(20, 20, w - 20, h - 20), color=(0, 0, 1), width=2)
    return d


A4 = (595.0, 842.0)
A4L = (842.0, 595.0)
A3L = (1191.0, 842.0)


# ------------------------------------------------------------ 分析

def test_analyze_reports_mixed_sizes(tmp_path):
    p = tmp_path / "m.pdf"
    d = _doc([A4, A4, A3L])
    d.save(str(p))
    d.close()
    info = RC.analyze(p)
    assert info["mixed"] is True
    assert info["total"] == 3
    assert {s["pages"] for s in info["sizes"]} == {2, 1}


def test_analyze_recognises_common_papers(tmp_path):
    p = tmp_path / "a.pdf"
    d = _doc([A4])
    d.save(str(p))
    d.close()
    assert RC.analyze(p)["sizes"][0]["label"] == "A4 直向"


def test_analyze_uniform_is_not_mixed(tmp_path):
    p = tmp_path / "u.pdf"
    d = _doc([A4, A4])
    d.save(str(p))
    d.close()
    assert RC.analyze(p)["mixed"] is False


# ------------------------------------------------------------ 目標尺寸

def test_auto_orientation_follows_the_source():
    """橫的圖說塞進直的 A4 會縮到看不清楚 —— 預設跟著原頁。"""
    spec = RC.ResizeSpec(paper="a4", orientation="auto")
    w, h = RC.target_size(spec, fitz.Rect(0, 0, *A3L))
    assert w > h, "橫向來源沒有得到橫向紙張"
    w, h = RC.target_size(spec, fitz.Rect(0, 0, *A4))
    assert h > w


@pytest.mark.parametrize("orient,expect_landscape", [
    ("portrait", False), ("landscape", True)])
def test_forced_orientation(orient, expect_landscape):
    spec = RC.ResizeSpec(paper="a4", orientation=orient)
    w, h = RC.target_size(spec, fitz.Rect(0, 0, *A3L))
    assert (w > h) is expect_landscape


def test_custom_size():
    spec = RC.ResizeSpec(paper="custom", custom_w_mm=100, custom_h_mm=150,
                         orientation="portrait")
    w, h = RC.target_size(spec, fitz.Rect(0, 0, *A4))
    assert w == pytest.approx(100 * 72 / 25.4, abs=0.1)
    assert h == pytest.approx(150 * 72 / 25.4, abs=0.1)


# ------------------------------------------------------------ 內容擺放

def test_scale_fits_inside_without_losing_anything():
    """縮放留白是最安全的 —— 內容一定塞得進去。"""
    r = RC.content_rect(fitz.Rect(0, 0, *A3L), *A4, RC.ResizeSpec(fit="scale"))
    assert r.width <= A4[0] + 0.01 and r.height <= A4[1] + 0.01


def test_center_keeps_original_scale():
    src = fitz.Rect(0, 0, 300, 400)
    r = RC.content_rect(src, *A4, RC.ResizeSpec(fit="center"))
    assert r.width == pytest.approx(300) and r.height == pytest.approx(400)


def test_crop_fills_the_paper():
    r = RC.content_rect(fitz.Rect(0, 0, *A3L), *A4, RC.ResizeSpec(fit="crop"))
    assert r.width >= A4[0] - 0.01 or r.height >= A4[1] - 0.01


def test_align_top_left():
    r = RC.content_rect(fitz.Rect(0, 0, 300, 400), *A4,
                        RC.ResizeSpec(fit="center", align="top-left"))
    assert r.x0 == 0 and r.y0 == 0


# ------------------------------------------------------------ 實際處理

def test_all_pages_end_up_the_same_size(tmp_path):
    src, dst = tmp_path / "s.pdf", tmp_path / "o.pdf"
    d = _doc([A4, A3L, (400, 400)])
    d.save(str(src))
    d.close()
    RC.resize(src, dst, RC.ResizeSpec(paper="a4", orientation="portrait"))
    out = fitz.open(str(dst))
    sizes = {(round(out[i].rect.width), round(out[i].rect.height))
             for i in range(out.page_count)}
    assert len(sizes) == 1, f"尺寸還是不一致：{sizes}"


def test_text_is_still_selectable(tmp_path):
    """**內容不可以被光柵化** —— 轉成圖片是最省事但最糟的作法。"""
    src, dst = tmp_path / "s.pdf", tmp_path / "o.pdf"
    d = _doc([A3L])
    d.save(str(src))
    d.close()
    RC.resize(src, dst, RC.ResizeSpec(paper="a4"))
    assert "PAGE1" in fitz.open(str(dst))[0].get_text()


def test_pages_already_correct_are_untouched(tmp_path):
    """重放一次會多一層物件、細線可能被重新取樣。"""
    src, dst = tmp_path / "s.pdf", tmp_path / "o.pdf"
    d = _doc([A4, A4, A3L])
    d.save(str(src))
    d.close()
    rep = RC.resize(src, dst, RC.ResizeSpec(paper="a4", orientation="auto"))
    assert rep.skipped_same == 2
    assert rep.changed == 1


def test_keep_same_can_be_turned_off(tmp_path):
    src, dst = tmp_path / "s.pdf", tmp_path / "o.pdf"
    d = _doc([A4, A4])
    d.save(str(src))
    d.close()
    rep = RC.resize(src, dst, RC.ResizeSpec(paper="a4", keep_same=False))
    assert rep.skipped_same == 0 and rep.changed == 2


def test_page_count_is_preserved(tmp_path):
    src, dst = tmp_path / "s.pdf", tmp_path / "o.pdf"
    d = _doc([A4, A3L, A4L, (300, 300)])
    d.save(str(src))
    d.close()
    RC.resize(src, dst, RC.ResizeSpec(paper="a4"))
    assert fitz.open(str(dst)).page_count == 4


def test_crop_warning_when_content_would_be_cut(tmp_path):
    """會裁掉內容時要講 —— 那是使用者選的，但不能無聲發生。"""
    src, dst = tmp_path / "s.pdf", tmp_path / "o.pdf"
    d = _doc([A3L])
    d.save(str(src))
    d.close()
    rep = RC.resize(src, dst, RC.ResizeSpec(paper="a4", orientation="portrait",
                                            fit="center"))
    assert rep.warnings and "裁掉" in rep.warnings[0]


def test_rotated_source_page_is_handled(tmp_path):
    """`/Rotate` 的頁面 —— `page.rect` 已經是視覺尺寸，不可以再算一次。

    這個專案在用印那邊踩過同一個雷（PyMuPDF 忽略頁面旋轉，v1.12.4）。
    """
    src, dst = tmp_path / "s.pdf", tmp_path / "o.pdf"
    d = fitz.open()
    pg = d.new_page(width=595, height=842)
    pg.insert_text((50, 60), "ROT", fontsize=20)
    pg.set_rotation(90)
    d.save(str(src))
    d.close()
    rep = RC.resize(src, dst, RC.ResizeSpec(paper="a4", orientation="auto"))
    out = fitz.open(str(dst))
    assert out.page_count == 1
    # 旋轉 90° 之後視覺上是橫的 → auto 應給橫向 A4
    assert out[0].rect.width > out[0].rect.height
    assert rep.total == 1


def test_single_size_document_is_a_noop(tmp_path):
    src, dst = tmp_path / "s.pdf", tmp_path / "o.pdf"
    d = _doc([A4, A4, A4])
    d.save(str(src))
    d.close()
    rep = RC.resize(src, dst, RC.ResizeSpec(paper="a4"))
    assert rep.changed == 0 and rep.skipped_same == 3


# ------------------------------------------------------------ 工具註冊

def test_tool_registered_and_granted():
    from app.core.roles import SEED_ROLES
    from app.tool_registry import discover_tools
    assert "pdf-page-size" in {t.metadata.id for t in discover_tools()}
    granted = {r["id"] for r in SEED_ROLES
               if "pdf-page-size" in (r.get("tools") or [])}
    assert "default-user" in granted and "clerk" in granted


def test_counts_as_an_office_tool():
    from app.core.concurrency_settings import OFFICE_TOOL_IDS
    assert "pdf-page-size" in OFFICE_TOOL_IDS


def test_backfill_migration_exists():
    from app.core import auth_db
    assert any("page_size" in f.__name__ for f in auth_db.MIGRATIONS)
