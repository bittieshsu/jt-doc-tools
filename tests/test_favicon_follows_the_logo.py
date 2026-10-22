"""換 Logo 時 favicon 要一起變（使用者 2026-10-22）。

原本 `<link rel="icon">` 雖然跟著 Logo，但：
* `/favicon.ico` 是 404 —— PDF 預覽、下載、API、書籤這類沒有 `<link rel="icon">` 的地方，瀏覽器會自己要這個路徑；
* 企業 Logo 的網址固定是 `/branding/logo`，換了新圖網址不變，favicon 的快取不會重抓；
* 企業 Logo 多半是橫的，直接當 favicon 會被壓扁。
"""
from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image


@pytest.fixture()
def br_dir(tmp_path, monkeypatch):
    from app.config import settings
    d = tmp_path / "branding"
    d.mkdir()
    monkeypatch.setattr(type(settings), "branding_dir", property(lambda self: d), raising=False)
    from app.core import branding
    branding._ICO_CACHE.clear()
    monkeypatch.setattr(branding, "_appearance_cache", (None, {}), raising=False)
    return d


def _png(color, size=(240, 60), margin=10) -> bytes:
    """橫的企業 Logo：四周留透明邊，中間一塊實心色。"""
    im = Image.new("RGBA", size, (0, 0, 0, 0))
    for x in range(margin, size[0] - margin):
        for y in range(margin, size[1] - margin):
            im.putpixel((x, y), color)
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def _client():
    from app.main import app
    return TestClient(app)


def _ico_sizes(data: bytes) -> set:
    with Image.open(io.BytesIO(data)) as im:
        return set(im.info.get("sizes", set()))


def _ico_center(data: bytes):
    with Image.open(io.BytesIO(data)) as im:
        im.size = (48, 48)
        rgba = im.convert("RGBA")
        return rgba.getpixel((24, 24))


def test_favicon_ico_exists_and_follows_the_builtin_choice(br_dir):
    from app.core import branding
    c = _client()
    r = c.get("/favicon.ico")
    assert r.status_code == 200 and r.headers["content-type"] == "image/x-icon"
    assert {(16, 16), (32, 32), (48, 48)} <= _ico_sizes(r.content)
    jt = r.content
    branding.set_appearance(builtin_logo="doc")
    assert c.get("/favicon.ico").content != jt, "選了另一個內建 Logo，favicon 要跟著換"


def test_a_custom_logo_becomes_a_square_favicon_and_the_url_changes_with_it(br_dir):
    from app.core import branding
    c = _client()
    builtin = c.get("/favicon.ico").content
    branding.save_logo(_png((220, 30, 30, 255)), "logo.png")
    url1 = branding.logo_url("icon")
    assert url1.startswith("/branding/icon.png?v="), url1
    r = c.get(url1)
    assert r.status_code == 200
    with Image.open(io.BytesIO(r.content)) as im:
        assert im.size == (branding.ICON_SIZE, branding.ICON_SIZE), "favicon 要是正方形"
        # 透明邊裁掉之後置中：正中央是 Logo 的顏色
        assert im.convert("RGBA").getpixel((90, 90))[:3] == (220, 30, 30)
    fav = c.get("/favicon.ico").content
    assert fav != builtin and _ico_center(fav)[0] > 180, "`/favicon.ico` 也換成企業 Logo"
    page = c.get("/login").text
    assert f'rel="icon" type="image/png" href="{url1}"' in page

    import time
    time.sleep(0.01)
    branding.save_logo(_png((30, 30, 220, 255)), "logo2.png")
    url2 = branding.logo_url("icon")
    assert url2 != url1, "換了 Logo，favicon 的網址要變（瀏覽器的 favicon 快取才會重抓）"
    assert _ico_center(c.get("/favicon.ico").content)[2] > 180, "favicon 跟著新 Logo"
    assert branding.custom_logo_url().startswith("/branding/logo?v=")


def test_removing_the_custom_logo_brings_back_the_builtin_favicon(br_dir):
    from app.core import branding
    c = _client()
    builtin = c.get("/favicon.ico").content
    branding.save_logo(_png((220, 30, 30, 255)), "logo.png")
    assert c.get("/favicon.ico").content != builtin
    branding.reset_logo()
    assert c.get("/branding/icon.png").status_code == 404
    assert not (br_dir / "icon.png").exists(), "移除 Logo 時一起清掉正方形快取"
    assert c.get("/favicon.ico").content == builtin
    assert branding.logo_url("icon").startswith("/static/images/")


def test_favicon_is_reachable_without_signing_in(admin_session, br_dir):
    """登入頁、還沒登入的瀏覽器分頁也要有 favicon。"""
    from app.core import branding
    branding.save_logo(_png((220, 30, 30, 255)), "logo.png")
    anon = _client()
    assert anon.get("/favicon.ico").status_code == 200
    assert anon.get(branding.logo_url("icon")).status_code == 200
