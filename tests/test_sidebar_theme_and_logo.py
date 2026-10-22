"""「企業 Logo / 識別」的左側選單配色與內建 Logo（2026-10-11 加入；瀏覽器實量在 `test_sidebar_theme_browser.py`）。

* 12 種配色：6 種漸層（含原本的靛紫）＋ 6 種純色；代號、名稱、說明、深淺在 `branding.SIDEBAR_THEMES`，
  顏色只寫在 `platform.css`，兩邊對得上。
* 預設（靛紫）的顏色跟改版前一模一樣；其他配色的文字對比 4.5:1 以上（這裡先用 CSS 的值算一次，
  不必開瀏覽器）。
* 側欄的樣式裡不再有寫死的白字、白色半透明底（不然淺色配色上會看不到字）。
* 內建 Logo 兩個：Jason Tools Logo（側欄依背景深淺換深色版 / 淺色版）、介紹網站的文件圖示；
  上傳過企業 Logo 一律用那張。通知信夾的 Logo 跟著換，換完不必重啟。
* 端點：送錯代號 400；改了寫稽核記錄；一般使用者不能改；設定備份帶得走，匯入之後馬上生效（不必重啟）。
* 登入頁、初次設定頁（不套側欄的那兩頁）的頁籤圖示與 Logo 也跟著換。
"""
from __future__ import annotations

import io
import json
import re
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
#: 去掉註解再解析（不然註解會黏在下一個選擇器前面，比對不到）
CSS = re.sub(r"/\*.*?\*/", "", (ROOT / "static" / "css" / "platform.css").read_text(encoding="utf-8"), flags=re.S)


def _br():
    from app.core import branding
    return branding


@pytest.fixture()
def br_dir(tmp_path, monkeypatch):
    """把 branding 目錄指到暫存目錄（不動測試共用的資料目錄）。"""
    from app.config import settings
    d = tmp_path / "branding"
    monkeypatch.setattr(type(settings), "branding_dir", property(lambda self: d), raising=False)
    b = _br()
    b._appearance_cache = (None, {})
    yield d
    b._appearance_cache = (None, {})


# ---------------------------------------------------------------- 配色的定義

def _css_theme(tid: str) -> dict:
    out = {}
    for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", CSS):
        sels = [s.strip() for s in m.group(1).split(",")]
        if f'[data-sb-theme="{tid}"]' in sels:
            for k, v in re.findall(r"(--sb-[\w-]+)\s*:\s*([^;]+);", m.group(2)):
                out[k] = v.strip()
    return out


def _selectors_with(var: str) -> set[str]:
    """設了某個變數的那幾個區塊的 data-sb-theme 代號。"""
    ids = set()
    for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", CSS):
        if re.search(rf"{re.escape(var)}\s*:", m.group(2)):
            ids |= set(re.findall(r'\[data-sb-theme="([\w-]+)"\]', m.group(1)))
    return ids


def test_twelve_themes_six_gradient_six_solid():
    b = _br()
    ids = [t[0] for t in b.SIDEBAR_THEMES]
    assert len(ids) == 12 and len(set(ids)) == 12
    assert [t[1] for t in b.SIDEBAR_THEMES].count("gradient") == 6
    assert [t[1] for t in b.SIDEBAR_THEMES].count("solid") == 6
    assert b.DEFAULT_SIDEBAR_THEME == "indigo" and ids[0] == "indigo"
    assert all(t[3] and t[4] for t in b.SIDEBAR_THEMES), "每一種都要有名稱與一句說明"


def test_the_builtin_logo_names():
    """名稱照使用者定的寫（2026-10-11 改：不加「公司」二字）。"""
    names = {x["id"]: x["name"] for x in _br().builtin_logo_list()}
    assert names == {"jt": "Jason Tools Logo", "doc": "文件圖示（介紹網站）"}


def test_every_theme_has_its_colours_in_the_css_and_the_kind_matches():
    for tid, kind, tone, *_ in _br().SIDEBAR_THEMES:
        v = _css_theme(tid)
        assert {"--sb-bg-top", "--sb-bg-bottom", "--sb-fg"} <= set(v), (tid, v)
        if kind == "gradient":
            assert v["--sb-bg-top"] != v["--sb-bg-bottom"], f"{tid} 標成漸層卻是單色"
        else:
            assert v["--sb-bg-top"] == v["--sb-bg-bottom"], f"{tid} 標成純色卻是漸層"


def test_light_themes_get_dark_text_and_the_others_do_not():
    b = _br()
    light = {t[0] for t in b.SIDEBAR_THEMES if t[2] == "light"}
    assert light and _selectors_with("--sb-item-bg") - {"indigo"} >= light
    # 淺色那一組的區塊只列淺色配色（深色配色混進去會變成深底深字）
    for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", CSS):
        if "--sb-pin: #d97706" in m.group(2):
            assert set(re.findall(r'\[data-sb-theme="([\w-]+)"\]', m.group(1))) == light


def test_the_default_colours_are_exactly_the_old_ones():
    """改版前寫死在 `.sidebar` 與各元件上的值（一個都不能變）。"""
    v = _css_theme("indigo")
    assert (v["--sb-bg-top"], v["--sb-bg-bottom"], v["--sb-fg"]) == ("#7c6df0", "#5b4bd9", "#f4f3ff")
    root = {}
    for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", CSS):
        if [s.strip() for s in m.group(1).split(",")] == [":root", "[data-sb-theme]"]:
            root = dict(re.findall(r"(--sb-[\w-]+)\s*:\s*([^;]+);", m.group(2)))
    old = {"--sb-desc": "rgba(255,255,255,0.85)", "--sb-desc-opacity": "0.78", "--sb-soft": "rgba(255,255,255,0.75)",
           "--sb-group-bg": "rgba(15,15,40,0.35)", "--sb-item-bg": "rgba(255,255,255,0.10)",
           "--sb-item-bg-hover": "rgba(255,255,255,0.20)", "--sb-pin": "#fde047",
           "--sb-placeholder": "rgba(255,255,255,0.7)", "--sb-fg-strong": "#fff",
           # v1.16.80 才改成變數的：原本寫死在捲軸、說明文字、登入來源（`@ldap`）上的值
           "--sb-thumb": "rgba(148,163,184,0.55)", "--sb-thumb-hover": "rgba(203,213,225,0.9)",
           "--sb-desc-hover": "var(--sb-desc)", "--sb-user-src-opacity": "0.7"}
    assert {k: root.get(k) for k in old} == old


def _lum(h):
    h = h.lstrip("#")
    f = lambda c: c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4  # noqa: E731
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)


def _ratio(a, b):
    x, y = sorted((_lum(a), _lum(b)), reverse=True)
    return (x + 0.05) / (y + 0.05)


def test_text_contrast_of_the_new_themes():
    bad = []
    for tid, *_ in _br().SIDEBAR_THEMES[1:]:
        v = _css_theme(tid)
        for edge in ("--sb-bg-top", "--sb-bg-bottom"):
            r = _ratio(v["--sb-fg"], v[edge])
            if r < 4.5:
                bad.append(f"{tid} {edge} {r:.2f}")
    assert not bad, bad


#: 側欄的元件（彈出的通知面板 / 帳號選單是白底，圖示方塊是彩色底，那幾個不算）
_SIDEBAR_SEL = re.compile(r"\.sidebar|\.sb-(?!item-icon|inbox|menu\b|menu-item|menu-sep|menu-value|menu-tick|submenu|bell-dot)")
#: 任何寫死的顏色（`rgba(var(--sb-ink), …)` 不算）。原本只找白色，漏掉浮動捲軸的灰色：
#: 淺色配色上幾乎看不到，滑過時還變得更淡。
_HARD = re.compile(r"#[0-9a-fA-F]{3,8}\b|rgba?\(\s*\d")


#: 自己有實色底、不吃側欄背景的元件：沒有 Logo 時的文字標（白底深字）、收起後左邊緣的展開鈕（主色底）、
#: 手機版打開選單時蓋住頁面的半透明黑（在側欄外面）
_OWN_BG = {".sidebar .brand-mark", ".sidebar-toggle", ".sidebar-backdrop"}


def _rules(text):
    for m in re.finditer(r"([^{}@]+)\{([^{}]*)\}", text):
        yield m.group(1).strip(), m.group(2)


def test_no_hard_coded_colours_left_in_the_sidebar_styles():
    base = (ROOT / "app" / "web" / "templates" / "base.html").read_text(encoding="utf-8")
    from tools.source_text import blocks
    style = re.sub(r"/\*.*?\*/", "", "\n".join(blocks(base, "style")), flags=re.S)
    bad = []
    for src, text in (("platform.css", CSS), ("base.html", style)):
        for sel, body in _rules(text):
            if "data-sb-theme" in sel or ":root" in sel or not _SIDEBAR_SEL.search(sel):
                continue
            if sel.startswith(".sb-item.active") or ".sb-item-icon" in sel or sel in _OWN_BG:
                continue
            # 陰影畫在側欄外面（頁面上），不算
            props = [p for p in body.split(";")
                     if re.search(r"(^|\s)(color|background(-color)?|border(-color)?|fill|stroke|outline(-color)?)\s*:", p)]
            if any(_HARD.search(p) for p in props):
                bad.append(f"{src}: {sel}")
    assert not bad, f"這些側欄樣式還寫死顏色（換配色時不會跟著變）：{bad}"


# ---------------------------------------------------------------- 設定與 Logo

def test_appearance_defaults_saves_and_rejects_unknown(br_dir):
    b = _br()
    assert b.get_appearance() == {"sidebar_theme": "indigo", "builtin_logo": "jt", "hero_glow": "indigo", "hero_title": "indigo"}
    assert b.set_appearance(sidebar_theme="mist")["sidebar_theme"] == "mist"
    assert b.set_appearance(builtin_logo="doc") == {"sidebar_theme": "mist", "builtin_logo": "doc", "hero_glow": "indigo", "hero_title": "indigo"}
    with pytest.raises(ValueError):
        b.set_appearance(sidebar_theme="neon")
    with pytest.raises(ValueError):
        b.set_appearance(builtin_logo="../x")
    (br_dir / "appearance.json").write_text('{"sidebar_theme": "evil", "builtin_logo": 3}', encoding="utf-8")
    assert b.get_appearance() == {"sidebar_theme": "indigo", "builtin_logo": "jt", "hero_glow": "indigo", "hero_title": "indigo"}, "壞掉的檔案要回預設"
    (br_dir / "appearance.json").write_text("not json", encoding="utf-8")
    assert b.get_appearance()["sidebar_theme"] == "indigo"


def test_logo_follows_the_choice_and_the_sidebar_tone(br_dir, monkeypatch):
    b = _br()
    assert b.logo_url("sidebar").endswith("logo-on-dark.png")
    assert b.logo_url("icon").endswith("logo-on-light.png")
    b.set_appearance(sidebar_theme="silver")
    assert b.logo_url("sidebar").endswith("logo-on-light.png"), "淺色背景要用白底那一版的 Jason Tools Logo"
    b.set_appearance(builtin_logo="doc")
    assert {b.logo_url(s) for s in ("sidebar", "icon", "page")} == {"/static/images/logo-doc.png"}
    assert b.builtin_logo_file().is_file()
    monkeypatch.setattr(b, "custom_logo_url", lambda: "/branding/logo")
    assert {b.logo_url(s) for s in ("sidebar", "page")} == {"/branding/logo"}, "上傳的企業 Logo 優先"
    # favicon 用企業 Logo 的正方形版本（v1.17.14，見 test_favicon_follows_the_logo.py）


def test_the_document_logo_matches_its_source():
    from PIL import Image, ImageChops
    import importlib.util
    spec = importlib.util.spec_from_file_location("bld", ROOT / "tools" / "build_logo_doc.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    fresh = Image.open(io.BytesIO(mod.render())).convert("RGBA")
    saved = Image.open(ROOT / "static" / "images" / "logo-doc.png").convert("RGBA")
    assert saved.size == fresh.size == (512, 512)
    assert max(b for _lo, b in ImageChops.difference(saved, fresh).getextrema()) <= 3, \
        "logo-doc.png 跟 logo-doc.svg 對不上：改了 SVG 要重跑 tools/build_logo_doc.py"
    # 跟介紹網站左上角同一個樣子：四角透明、中間是漸層底上的白色圖示
    assert saved.getpixel((0, 0))[3] == 0 and saved.getpixel((40, 40))[3] == 255


def test_the_email_logo_follows_the_choice_without_a_restart(br_dir):
    b = _br()
    from app.core import notify_email_assets as nea
    nea.invalidate()
    jt = nea.site_logo_png()
    b.set_appearance(builtin_logo="doc")
    assert nea.site_logo_png() == jt, "還沒通知換 Logo 時用快取（這一條確認快取真的在）"
    nea.invalidate()
    doc = nea.site_logo_png()
    assert doc and doc != jt


# ---------------------------------------------------------------- 端點與頁面

def test_endpoint_saves_audits_and_rejects(client, auth_off, br_dir, monkeypatch):
    from app.core import audit_db, notify_email_assets
    seen, cleared = [], []
    monkeypatch.setattr(audit_db, "log_event", lambda ev, **kw: seen.append((ev, kw)))
    monkeypatch.setattr(notify_email_assets, "invalidate", lambda: cleared.append(1))
    r = client.post("/admin/branding/appearance", json={"sidebar_theme": "wine", "builtin_logo": "doc"})
    assert r.status_code == 200, r.text
    j = r.json()
    assert (j["sidebar_theme"], j["builtin_logo"]) == ("wine", "doc")
    assert j["sidebar_logo"].endswith("logo-doc.png") and j["icon_logo"].endswith("logo-doc.png")
    assert seen and seen[-1][0] == "settings_change" and seen[-1][1]["target"] == "branding"
    assert seen[-1][1]["details"] == {"sidebar_theme": "wine", "builtin_logo": "doc"}
    assert cleared, "改了內建 Logo 要清掉通知信的 Logo 快取（不然要重啟才換）"
    for bad in ({"sidebar_theme": "neon"}, {"builtin_logo": "x"}, {}, []):
        assert client.post("/admin/branding/appearance", json=bad).status_code == 400, bad
    api = client.get("/admin/api/branding").json()
    assert api["sidebar_theme"] == "wine" and len(api["sidebar_themes"]) == 12 and len(api["builtin_logos"]) == 2
    html = client.get("/").text
    assert 'data-sb-theme="wine"' in html[:600]
    assert html.count("/static/images/logo-doc.png") >= 2, "側欄與頁籤圖示都要換"


def test_the_settings_page_has_a_preview_card_per_theme(client, auth_off, br_dir):
    html = client.get("/admin/branding").text
    cards = re.findall(r'<button type="button" class="br-theme[^"]*"\s+data-theme-pick="([\w-]+)".*?</button>', html, re.S)
    assert cards == [t[0] for t in _br().SIDEBAR_THEMES]
    for tid in cards:
        card = re.search(rf'data-theme-pick="{tid}".*?</button>', html, re.S).group(0)
        assert f'class="br-mini" data-sb-theme="{tid}"' in card, f"{tid} 沒有預覽"
        assert 'class="br-theme-desc"' in card
    assert html.count("data-builtin-logo=") == 2


def test_a_regular_user_cannot_change_it(admin_session):
    from tests.test_authz_boundaries import _user_client
    _, c = _user_client("alice_sbtheme")
    r = c.post("/admin/branding/appearance", json={"sidebar_theme": "wine"}, follow_redirects=False)
    assert r.status_code in (401, 403, 302, 303), r.status_code


def test_the_settings_backup_carries_it(tmp_path, monkeypatch):
    from app.config import settings
    from app.core import settings_export
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    b = _br()
    b._appearance_cache = (None, {})
    b.set_appearance(sidebar_theme="teal", builtin_logo="doc", hero_glow="rose", hero_title="slate")
    out = tmp_path / "backup.zip"
    settings_export.export_to_zip(out, ["branding"])
    with zipfile.ZipFile(out) as zf:
        names = zf.namelist()
        got = json.loads(zf.read("data/branding/appearance.json"))
    assert got == {"sidebar_theme": "teal", "builtin_logo": "doc", "hero_glow": "rose", "hero_title": "slate"}, names
    b._appearance_cache = (None, {})


def test_restoring_a_backup_applies_at_once(tmp_path, monkeypatch):
    """匯入是原地覆寫檔案；讀設定的快取要看得出來換過了，不必重啟服務。"""
    from app.config import settings
    from app.core import settings_export
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    b = _br()
    b._appearance_cache = (None, {})
    b.set_appearance(sidebar_theme="teal", builtin_logo="doc")
    out = tmp_path / "backup.zip"
    settings_export.export_to_zip(out, ["branding"])
    b.set_appearance(sidebar_theme="wine", builtin_logo="jt")
    assert b.sidebar_theme() == "wine"          # 這時快取裡是 wine
    settings_export.import_from_zip(out, selected_ids=["branding"])
    assert b.get_appearance() == {"sidebar_theme": "teal", "builtin_logo": "doc", "hero_glow": "indigo", "hero_title": "indigo"}, "匯入之後還是舊的設定"
    assert b.logo_url("sidebar").endswith("logo-doc.png")
    b._appearance_cache = (None, {})


def test_the_cache_notices_a_rewrite_with_the_same_timestamp(br_dir):
    """修改時間解析度粗的檔案系統（同一個時間戳記內寫兩次）：內容換了也要讀到新的。"""
    import os
    b = _br()
    b.set_appearance(sidebar_theme="teal", builtin_logo="jt")
    p = br_dir / "appearance.json"
    st = p.stat()
    assert b.sidebar_theme() == "teal"
    with open(p, "w", encoding="utf-8") as f:   # 原地覆寫，跟匯入一樣
        json.dump({"sidebar_theme": "wine", "builtin_logo": "doc"}, f)
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert b.get_appearance() == {"sidebar_theme": "wine", "builtin_logo": "doc", "hero_glow": "indigo", "hero_title": "indigo"}


def test_the_login_and_setup_pages_follow_the_choice(auth_off, br_dir):
    """這兩頁不套 base.html（沒有側欄），頁籤圖示與頁面上的 Logo 要各自跟著換。"""
    from fastapi.testclient import TestClient
    from app import main as app_main
    from app.core import auth_settings
    b = _br()
    c = TestClient(app_main.app)
    b.set_appearance(builtin_logo="doc")
    page = c.get("/setup-admin").text
    assert 'rel="icon" type="image/png" href="/static/images/logo-doc.png"' in page
    pw = "TestAdmin1234"
    auth_settings.enable_local_with_admin(admin_username="jtdt-admin", admin_display_name="管理員",
                                          admin_password=pw, admin_password_confirm=pw, actor_ip="127.0.0.1")
    page = c.get("/login").text
    assert 'rel="icon" type="image/png" href="/static/images/logo-doc.png"' in page
    assert page.count("/static/images/logo-doc.png") >= 2, "登入頁上的 Logo 沒有換"
    b.set_appearance(builtin_logo="jt")
    page = c.get("/login").text
    assert "logo-doc.png" not in page and "/static/images/logo-on-light.png" in page

