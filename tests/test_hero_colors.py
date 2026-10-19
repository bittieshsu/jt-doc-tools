"""首頁 Logo 光暈與標題文字可以各自換顏色（v1.17.0）。

* 兩組色票各自選：光暈 8 種（含「不要光暈」）、標題 7 種；預設都是原本的靛紫，跟改版前**一模一樣**。
* 顏色只寫在 `branding.HERO_GLOWS` / `HERO_TITLES`，首頁與設定頁的 CSS 都由那份產生（值是固定常數）。
* 新的標題配色最淺那一點對首頁底色也要有 3:1（38px 粗體是大字）。
* 端點：送錯代號 400 而且一項都不改；改了寫稽核記錄；一般使用者不能改。
* 設定頁每個選項都有色塊，預覽跟著換。
"""
from __future__ import annotations

import re

import pytest

from tests.test_sidebar_theme_and_logo import br_dir  # noqa: F401  （同一個暫存 branding 目錄）

PAGE_BG = "#f5f6f8"      # platform.css 的 --bg


def _br():
    from app.core import branding
    return branding


def _lum(hexc: str) -> float:
    h = hexc.lstrip("#")
    out = []
    for i in (0, 2, 4):
        c = int(h[i:i + 2], 16) / 255
        out.append(c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4)
    return 0.2126 * out[0] + 0.7152 * out[1] + 0.0722 * out[2]


def _contrast(a: str, b: str) -> float:
    la, lb = sorted((_lum(a), _lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def test_the_palettes():
    b = _br()
    glows = [g[0] for g in b.HERO_GLOWS]
    titles = [t[0] for t in b.HERO_TITLES]
    assert glows[0] == titles[0] == "indigo" == b.DEFAULT_HERO_GLOW == b.DEFAULT_HERO_TITLE
    assert "none" in glows and "none" not in titles, "光暈可以不要；標題一定要有顏色"
    assert len(set(glows)) == len(glows) == 8 and len(set(titles)) == len(titles) == 7
    assert all(n for _i, n, _c in b.HERO_GLOWS + b.HERO_TITLES)


def test_the_default_is_exactly_the_old_look():
    b = _br()
    assert dict((g[0], g[2]) for g in b.HERO_GLOWS)["indigo"] == (99, 102, 241)
    assert dict((t[0], t[2]) for t in b.HERO_TITLES)["indigo"] == ("#7c6df0", "#5b4bd9", "#4c3ad0")


@pytest.mark.parametrize("tid", [t[0] for t in __import__("app.core.branding", fromlist=["x"]).HERO_TITLES
                                 if t[0] != "indigo"])
def test_new_title_colours_are_readable_on_the_page(tid):
    stops = dict((t[0], t[2]) for t in _br().HERO_TITLES)[tid]
    worst = min(_contrast(s, PAGE_BG) for s in stops)
    assert worst >= 3.0, f"{tid} 最淺那一點對首頁底色只有 {worst:.2f}:1"


def test_saves_and_rejects_without_partial_changes(br_dir):  # noqa: F811
    b = _br()
    a = b.get_appearance()
    assert (a["hero_glow"], a["hero_title"]) == ("indigo", "indigo")
    out = b.set_appearance(hero_glow="none", hero_title="teal")
    assert (out["hero_glow"], out["hero_title"]) == ("none", "teal")
    assert b.hero_style() == {"glow": "", "title": ["#0d9488", "#0f766e", "#115e59"]}
    with pytest.raises(ValueError):
        b.set_appearance(sidebar_theme="wine", hero_glow="neon")
    assert b.get_appearance()["sidebar_theme"] == "indigo", "有一項不認得：一項都不改"
    with pytest.raises(ValueError):
        b.set_appearance(hero_title="none")


def test_the_home_page_follows_the_choice(client, auth_off, br_dir):  # noqa: F811
    html = client.get("/").text
    assert "rgba(99,102,241,0.45)" in html and "#7c6df0 0%, #5b4bd9 60%, #4c3ad0 100%" in html, "預設跟原本一樣"
    _br().set_appearance(hero_glow="amber", hero_title="rose")
    html = client.get("/").text
    css = re.search(r"\.hero-logo img \{(.*?)\}", html, re.S).group(1)
    assert "rgba(245,158,11,0.45)" in css and "rgba(99,102,241" not in css
    assert "#e11d48 0%, #be123c 60%, #9f1239 100%" in html
    _br().set_appearance(hero_glow="none")
    css = re.search(r"\.hero-logo img \{(.*?)\}", client.get("/").text, re.S).group(1)
    assert "filter: none" in css and "drop-shadow" not in css


def test_endpoint_saves_audits_and_rejects(client, auth_off, br_dir, monkeypatch):  # noqa: F811
    from app.core import audit_db
    seen = []
    monkeypatch.setattr(audit_db, "log_event", lambda ev, **kw: seen.append((ev, kw)))
    r = client.post("/admin/branding/appearance", json={"hero_glow": "blue", "hero_title": "green"})
    assert r.status_code == 200, r.text
    assert (r.json()["hero_glow"], r.json()["hero_title"]) == ("blue", "green")
    assert seen[-1][1]["details"] == {"hero_glow": "blue", "hero_title": "green"}
    for bad in ({"hero_glow": "neon"}, {"hero_title": "none"}, {"hero_glow": "url(x)"}):
        assert client.post("/admin/branding/appearance", json=bad).status_code == 400, bad
    assert _br().get_appearance()["hero_glow"] == "blue"
    api = client.get("/admin/api/branding").json()
    assert len(api["hero_glows"]) == 8 and len(api["hero_titles"]) == 7


def test_the_settings_page_has_a_chip_and_css_for_every_colour(client, auth_off, br_dir):  # noqa: F811
    html = client.get("/admin/branding").text
    b = _br()
    assert re.findall(r'data-hero-glow="([\w-]+)"', html) == [g[0] for g in b.HERO_GLOWS]
    assert re.findall(r'data-hero-title="([\w-]+)"', html) == [t[0] for t in b.HERO_TITLES]
    for gid, _n, rgb in b.HERO_GLOWS:
        assert f'[data-glow-sw="{gid}"]' in html and f'.br-hero-prev[data-glow="{gid}"]' in html
        if rgb:
            assert "rgba({},0.45)".format(",".join(map(str, rgb))) in html
    for tid, _n, stops in b.HERO_TITLES:
        assert f'.br-hero-prev[data-title="{tid}"] .br-hero-title' in html and stops[0] in html
    assert 'id="heroPrev" data-glow="indigo" data-title="indigo"' in html


def test_a_regular_user_cannot_change_it(admin_session):
    from tests.test_authz_boundaries import _user_client
    _, c = _user_client("alice_herocolor")
    r = c.post("/admin/branding/appearance", json={"hero_glow": "rose"}, follow_redirects=False)
    assert r.status_code in (401, 403, 302, 303), r.status_code
