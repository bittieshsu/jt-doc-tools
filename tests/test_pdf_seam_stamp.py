"""騎縫章。

## 由來

合約、標案的實務作法：整疊文件蓋一個跨頁的章，**任何一頁被抽換或掉頁都看得出來**
（那一片對不起來）。紙本世界防抽換最直接的手段，數位化之後仍然被要求。

## 這一份守的重點

1. **切片要能拼回去**。這是整支工具唯一真正的成敗判準 —— 拼不回去就毫無意義。
   寬度用累進取整，`w // n` 會在右邊留最多 n-1 px 的殘缺，肉眼看得出接縫。
2. **旋轉必須在切片之前**。先切再各自旋轉的話，每片繞自己的中心轉，接縫立刻
   對不起來 —— 那是這種工具最明顯的破綻。
3. **最後一組不足時按實際頁數切**，不可以補空白片（會在最後一頁留半個章）。
4. **亂數要能重現**：種子要回報，填回去要得到一模一樣的結果。
"""
from __future__ import annotations

import io

import fitz
import pytest
from PIL import Image, ImageDraw

from app.tools.pdf_seam_stamp import seam_core as SC
from app.tools.pdf_seam_stamp import stamp_source as SS


def _stamp(size: int = 400) -> bytes:
    """一個「有沒有對齊」一眼看得出來的章：雙圓框 + 十字。"""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((6, 6, size - 6, size - 6), outline=(200, 20, 20, 255), width=12)
    d.line((60, size // 2, size - 60, size // 2), fill=(200, 20, 20, 255), width=10)
    d.line((size // 2, 60, size // 2, size - 60), fill=(200, 20, 20, 255), width=10)
    b = io.BytesIO()
    img.save(b, format="PNG")
    return b.getvalue()


def _doc(n: int) -> fitz.Document:
    d = fitz.open()
    for i in range(n):
        pg = d.new_page(width=595, height=842)
        pg.insert_text((72, 80), f"page {i + 1}", fontsize=14)
    return d


# ------------------------------------------------------------ 分組

@pytest.mark.parametrize("pages,group,expect", [
    (6, 3, [[0, 1, 2], [3, 4, 5]]),
    (7, 3, [[0, 1, 2], [3, 4, 5], [6]]),     # 最後一組不足
    (4, 4, [[0, 1, 2, 3]]),
    (2, 2, [[0, 1]]),
])
def test_grouping(pages, group, expect):
    assert SC.make_groups(pages, group) == expect


def test_group_zero_means_whole_document():
    spec = SC.SeamSpec(group=0).normalized(9)
    assert spec.group == 9


def test_group_larger_than_document_is_clamped():
    spec = SC.SeamSpec(group=50).normalized(6)
    assert spec.group == 6


def test_last_group_is_not_padded():
    """不足的那組按實際頁數切 —— 補空白片會在最後一頁留半個章，像印壞了。"""
    groups = SC.make_groups(7, 3)
    assert len(groups[-1]) == 1


# ------------------------------------------------------------ 切片

@pytest.mark.parametrize("n", [2, 3, 5, 7, 13])
def test_slice_widths_sum_to_the_original(n):
    """**這是拼得回去的前提** —— `w // n` 會在右邊留最多 n-1 px 的殘缺。"""
    img = Image.open(io.BytesIO(_stamp(400))).convert("RGBA")
    parts = SC.slice_stamp(img, n)
    assert len(parts) == n
    assert sum(p.size[0] for p in parts) == img.size[0]


def test_slices_are_never_zero_width():
    img = Image.open(io.BytesIO(_stamp(20))).convert("RGBA")
    parts = SC.slice_stamp(img, 30)          # 片比像素還多
    assert all(p.size[0] >= 1 for p in parts)


def test_slices_reassemble_pixel_perfect():
    """把片依序貼回去，要跟原圖一模一樣。"""
    img = Image.open(io.BytesIO(_stamp(400))).convert("RGBA")
    parts = SC.slice_stamp(img, 7)
    out = Image.new("RGBA", img.size)
    x = 0
    for p in parts:
        out.paste(p, (x, 0))
        x += p.size[0]
    assert list(out.getdata()) == list(img.getdata()), "拼回去跟原圖不一樣"


# ------------------------------------------------------------ 旋轉順序

def test_rotation_happens_before_slicing():
    """**先切再各自旋轉的話接縫會對不起來** —— 這是這種工具最明顯的破綻。

    驗法：旋轉後再切，拼回去要等於「先旋轉的整張圖」。
    """
    img = Image.open(io.BytesIO(_stamp(400))).convert("RGBA")
    rot = SC._rotated_stamp(img, 8)
    parts = SC.slice_stamp(rot, 4)
    out = Image.new("RGBA", rot.size)
    x = 0
    for p in parts:
        out.paste(p, (x, 0))
        x += p.size[0]
    assert list(out.getdata()) == list(rot.getdata())


def test_rotation_expands_the_canvas():
    """`expand=True` 才不會把轉出去的角切掉。"""
    img = Image.open(io.BytesIO(_stamp(400))).convert("RGBA")
    rot = SC._rotated_stamp(img, 20)
    assert rot.size[0] > img.size[0] and rot.size[1] > img.size[1]


def test_zero_angle_is_a_noop():
    img = Image.open(io.BytesIO(_stamp(100))).convert("RGBA")
    assert SC._rotated_stamp(img, 0) is img


# ------------------------------------------------------------ 位置

def test_side_mode_puts_every_slice_on_the_same_edge():
    """側邊騎縫：印出來疊好扇開才拼得起來 —— 每片一律貼齊同一邊。"""
    d = _doc(3)
    spec = SC.SeamSpec(mode="side", edge="right", size_mm=45).normalized(3)
    xs = [SC.slice_rect(d[i], spec, i, 3, 0.0, 1.0).x0 for i in range(3)]
    assert len(set(round(x, 2) for x in xs)) == 1, f"位置不一致：{xs}"
    d.close()


def test_side_mode_left_edge():
    d = _doc(2)
    spec = SC.SeamSpec(mode="side", edge="left", offset_mm=5).normalized(2)
    r = SC.slice_rect(d[0], spec, 0, 2, 0.0, 1.0)
    assert r.x0 == pytest.approx(SC._mm(5), abs=0.01)
    d.close()


def test_spread_mode_first_page_right_last_page_left():
    """對開跨頁：並排攤開時要接得上。"""
    d = _doc(2)
    spec = SC.SeamSpec(mode="spread", size_mm=40, offset_mm=0).normalized(2)
    r0 = SC.slice_rect(d[0], spec, 0, 2, 0.0, 1.0)
    r1 = SC.slice_rect(d[1], spec, 1, 2, 0.0, 1.0)
    assert r0.x1 == pytest.approx(d[0].rect.width, abs=0.01), "第一頁沒貼右緣"
    assert r1.x0 == pytest.approx(0.0, abs=0.01), "最後一頁沒貼左緣"
    d.close()


def test_vertical_position_is_centred_by_default():
    d = _doc(2)
    spec = SC.SeamSpec(size_mm=40).normalized(2)
    r = SC.slice_rect(d[0], spec, 0, 2, 0.0, 1.0)
    assert (r.y0 + r.y1) / 2 == pytest.approx(d[0].rect.height / 2, abs=0.01)
    d.close()


def test_slice_height_follows_the_rotated_ratio():
    """旋轉後章會變高變寬，片的比例要跟著走，否則會被壓扁。"""
    d = _doc(2)
    spec = SC.SeamSpec(size_mm=40).normalized(2)
    a = SC.slice_rect(d[0], spec, 0, 2, 0.0, 1.0)
    b = SC.slice_rect(d[0], spec, 0, 2, 0.0, 1.4)
    assert b.height > a.height
    d.close()


# ------------------------------------------------------------ 亂數

def test_same_seed_gives_the_same_result():
    """種子要能重現 —— 事後追查靠它。"""
    d = _doc(6)
    spec = SC.SeamSpec(group=2, jitter_pos=True, jitter_angle=True, seed=12345)
    a = SC.plan(d, spec)
    b = SC.plan(d, spec)
    assert [(p["angle"], p["dy_mm"]) for p in a.placements] == \
           [(p["angle"], p["dy_mm"]) for p in b.placements]
    d.close()


def test_seed_zero_is_reported_so_it_can_be_reproduced():
    d = _doc(4)
    p = SC.plan(d, SC.SeamSpec(group=2, jitter_pos=True, seed=0))
    assert p.seed > 0, "沒有回報用了哪個種子，事後就重現不了"
    d.close()


def test_jitter_varies_between_groups_but_not_within():
    """**同一組的片必須完全一致** —— 組內各自亂數的話就拼不回去了。"""
    d = _doc(6)
    p = SC.plan(d, SC.SeamSpec(group=3, jitter_pos=True, jitter_angle=True,
                               seed=99))
    g1 = [x for x in p.placements if x["page"] in (0, 1, 2)]
    g2 = [x for x in p.placements if x["page"] in (3, 4, 5)]
    assert len({(x["angle"], x["dy_mm"]) for x in g1}) == 1, "同一組內不一致"
    assert (g1[0]["angle"], g1[0]["dy_mm"]) != (g2[0]["angle"], g2[0]["dy_mm"]), \
        "不同組完全一樣 —— 亂數沒作用"
    d.close()


def test_jitter_angle_is_capped():
    """歪太多會蓋到內文，而且看起來不像蓋章像貼紙。"""
    spec = SC.SeamSpec(jitter_angle_deg=90).normalized(4)
    assert spec.jitter_angle_deg <= SC.MAX_JITTER_ANGLE


# ------------------------------------------------------------ 實際輸出

def test_apply_puts_an_image_on_every_page():
    d = _doc(6)
    SC.apply_seam(d, _stamp(), SC.SeamSpec(group=3, size_mm=40))
    for i in range(6):
        assert d[i].get_images(), f"第 {i + 1} 頁沒有蓋到"
    d.close()


def test_thin_slice_is_warned_not_silently_accepted():
    """一片太細印出來看不出是什麼 —— 要講，但不擅自改設定（那是使用者的選擇）。"""
    d = _doc(20)
    p = SC.plan(d, SC.SeamSpec(group=20, size_mm=20))
    assert p.warnings and "mm" in p.warnings[0]
    d.close()


def test_reconstruct_produces_an_image():
    png = SC.reconstruct(_stamp(), SC.SeamSpec(angle_deg=5), 4)
    img = Image.open(io.BytesIO(png))
    assert img.size[0] > 0 and img.size[1] > 0


# ------------------------------------------------------------ 印章來源

@pytest.mark.parametrize("shape", ["circle", "square", "rect"])
def test_generate_produces_a_png(shape):
    png = SS.generate("騎縫章", shape=shape)
    img = Image.open(io.BytesIO(png))
    assert img.mode == "RGBA" and img.size[0] > 100


def test_long_text_widens_the_rect_stamp_instead_of_shrinking_the_glyphs():
    """**字多要加寬，不是把字縮小** —— 縮到看不清楚的章等於沒蓋。

    實務上很多人蓋的是公司全名，硬塞進固定畫布只能一直縮字。
    """
    short = Image.open(io.BytesIO(SS.generate("騎縫章", shape="rect")))
    long_ = Image.open(io.BytesIO(SS.generate("節省工具箱股份有限公司", shape="rect")))
    assert long_.size[0] > short.size[0] * 2, (
        f"字多了寬度沒跟著長：{short.size} → {long_.size}")
    # 高度固定 = 字級沒被縮小（章的高度就是字的高度加內距）
    assert abs(long_.size[1] - short.size[1]) <= 2, (
        f"高度變了代表字級被動過：{short.size[1]} → {long_.size[1]}")


def test_long_text_in_a_round_stamp_wraps_into_more_columns():
    """圓 / 方章外形是等比的 → 字多時**分行**，不是縮字。"""
    small = Image.open(io.BytesIO(SS.generate("騎縫章", shape="circle")))
    big = Image.open(io.BytesIO(SS.generate("節省工具箱股份有限公司", shape="circle")))
    assert big.size[0] > small.size[0], "字多了圓章沒變大，代表是把字縮小塞進去"
    assert big.size[0] == big.size[1], "圓章必須維持正圓"


def test_company_full_name_is_not_truncated():
    """公司全名不可以被截掉 —— 截一半的章比沒蓋更糟。"""
    name = "節省工具箱資訊科技股份有限公司"
    assert len(name) <= SS._MAX_CHARS
    img = Image.open(io.BytesIO(SS.generate(name, shape="rect")))
    assert img.size[0] > 1500


def test_generated_stamp_has_ink():
    """畫出來要真的有東西 —— 全透明的圖蓋上去等於沒蓋。"""
    img = Image.open(io.BytesIO(SS.generate("騎縫章"))).convert("RGBA")
    alpha = img.getchannel("A")
    assert sum(1 for v in alpha.getdata() if v > 0) > 500


def test_generate_survives_empty_text():
    assert SS.generate("") and SS.generate("   ")


def test_upload_white_background_becomes_transparent():
    """使用者常給拍照 / 掃描的章，帶白底貼上去會蓋掉內文。"""
    img = Image.new("RGB", (60, 60), (255, 255, 255))
    ImageDraw.Draw(img).ellipse((10, 10, 50, 50), outline=(200, 20, 20), width=5)
    b = io.BytesIO()
    img.save(b, format="PNG")
    out = Image.open(io.BytesIO(SS.normalize_upload(b.getvalue()))).convert("RGBA")
    assert out.getpixel((1, 1))[3] == 0, "白底沒有被去掉"
    # 印泥本身不可以被去掉
    assert any(p[3] > 0 for p in out.getdata())


def test_upload_keeps_light_grey_ink():
    """只吃「很白」的 —— 淺灰的印泥不能碰。"""
    img = Image.new("RGB", (10, 10), (200, 200, 200))
    b = io.BytesIO()
    img.save(b, format="PNG")
    out = Image.open(io.BytesIO(SS.normalize_upload(b.getvalue()))).convert("RGBA")
    assert out.getpixel((5, 5))[3] == 255


def test_opacity_is_applied():
    full = SS.generate("章")
    half = SS.apply_opacity(full, 0.4)
    a = Image.open(io.BytesIO(full)).convert("RGBA").getchannel("A")
    b = Image.open(io.BytesIO(half)).convert("RGBA").getchannel("A")
    assert max(b.getdata()) < max(a.getdata())


# ------------------------------------------------------------ 工具註冊

def test_tool_registered_and_permissions_match_stamping():
    """騎縫章就是用印的一種 —— 權限要跟 `pdf-stamp` 走，不可以給一般使用者。"""
    from app.core.roles import SEED_ROLES
    from app.tool_registry import discover_tools
    assert "pdf-seam-stamp" in {t.metadata.id for t in discover_tools()}
    for r in SEED_ROLES:
        tools = r.get("tools") or []
        if r["id"] == "admin":
            continue
        assert ("pdf-seam-stamp" in tools) == ("pdf-stamp" in tools), \
            f"{r['id']} 的騎縫章與用印權限不一致"


def test_counts_as_an_office_tool():
    """收 Office 檔 = 會起 soffice —— 沒列進去併行度會低估記憶體。"""
    from app.core.concurrency_settings import OFFICE_TOOL_IDS
    assert "pdf-seam-stamp" in OFFICE_TOOL_IDS


def test_backfill_migration_exists():
    from app.core import auth_db
    assert any("seam" in f.__name__ for f in auth_db.MIGRATIONS)
