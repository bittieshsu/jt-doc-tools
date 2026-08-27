"""自訂上傳字型的顯示名稱。

使用者要求：「自訂上傳字型也要可以自訂字型顯示出來的名稱，尤其是 pdf editor
會用到」，以及「可以讓系統上傳時自動抓字型檔裡面的名稱當名稱嗎？但也保留可
自訂名稱」。

原本顯示的是**檔名** —— 而檔名常常是 `NotoSansTC-Regular.ttf` 或一串亂碼。
這個名稱會出現在 PDF 編輯器、用印、浮水印的字型下拉裡，是使用者每天看到的
東西。

**優先序：管理員自訂 > 字型檔內建的名稱 > 檔名。** 自訂留空會自動退回內建
名稱，管理員不必記得檔名長什麼樣。
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from app.core import font_catalog as fc


@pytest.fixture
def custom_font(tmp_path, monkeypatch):
    """在自訂字型資料夾放一份真的字型檔（檔名故意取得很難看）。"""
    monkeypatch.setenv("JTDT_DATA_DIR", str(tmp_path))
    src = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
    if not src.exists():
        pytest.skip("這台機器沒有 DejaVuSans 可以拿來當樣本")
    cdir = fc.custom_fonts_dir()
    cdir.mkdir(parents=True, exist_ok=True)
    dst = cdir / "aBcD-1234_upload.ttf"
    shutil.copyfile(src, dst)
    fc.refresh_cache()
    yield dst
    fc.refresh_cache()


def _find(fonts, filename):
    return next((f for f in fonts if f.get("filename") == filename), None)


def test_internal_name_is_read_from_the_font_file():
    src = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    if not Path(src).exists():
        pytest.skip("沒有樣本字型")
    assert fc.font_internal_name(src) == "DejaVu Sans"


def test_unreadable_file_returns_empty_not_an_exception(tmp_path):
    """壞檔案要安靜回空字串 —— 一個壞字型不該讓整份清單掛掉。"""
    bad = tmp_path / "broken.ttf"
    bad.write_bytes(b"not a font at all")
    assert fc.font_internal_name(str(bad)) == ""


def test_upload_shows_the_font_internal_name_not_the_filename(custom_font):
    """檔名叫 `aBcD-1234_upload.ttf`，清單上要顯示 `DejaVu Sans`。"""
    row = _find(fc.list_fonts(include_hidden=True), custom_font.name)
    assert row is not None, "自訂字型沒出現在清單裡"
    assert row["family"] == "DejaVu Sans", f"顯示的是 {row['family']!r}"
    assert row["auto_name"] == "DejaVu Sans"
    assert row["custom_name"] == "", "還沒設自訂名稱就不該有值"


def test_admin_can_override_the_display_name(custom_font):
    fc.set_custom_name(custom_font.name, "公司標準黑體")
    row = _find(fc.list_fonts(include_hidden=True), custom_font.name)
    assert row["family"] == "公司標準黑體"
    assert row["custom_name"] == "公司標準黑體"
    assert row["auto_name"] == "DejaVu Sans", "自動抓到的名稱要留著給介面顯示"


def test_clearing_the_override_falls_back_to_the_internal_name(custom_font):
    """留空 = 回到自動抓的名稱，**不是**回到檔名。"""
    fc.set_custom_name(custom_font.name, "暫時的名字")
    fc.set_custom_name(custom_font.name, "")
    row = _find(fc.list_fonts(include_hidden=True), custom_font.name)
    assert row["family"] == "DejaVu Sans"
    assert row["custom_name"] == ""


def test_name_is_trimmed_and_length_capped(custom_font):
    fc.set_custom_name(custom_font.name, "  太多   空白   的  名字  ")
    assert fc.get_custom_names()[custom_font.name] == "太多 空白 的 名字"
    fc.set_custom_name(custom_font.name, "長" * 200)
    assert len(fc.get_custom_names()[custom_font.name]) == fc.CUSTOM_NAME_MAX


def test_rename_endpoint_only_accepts_custom_fonts(admin_session):
    """系統字型不可以改名 —— 那些是掃出來的，改了下次掃描就沒了。"""
    client, _, _ = admin_session
    r = client.post("/admin/fonts/rename",
                    json={"id": "system:/usr/share/fonts/x.ttf", "name": "x"})
    assert r.status_code == 400
    assert "自訂" in r.text


def test_rename_endpoint_rejects_a_path_outside_the_font_dir(admin_session):
    """不可以往設定檔裡塞任意鍵。"""
    client, _, _ = admin_session
    r = client.post("/admin/fonts/rename",
                    json={"id": "custom:../../etc/passwd", "name": "x"})
    assert r.status_code in (400, 404)


def test_rename_endpoint_works_end_to_end(admin_session, custom_font):
    client, _, _ = admin_session
    r = client.post("/admin/fonts/rename",
                    json={"id": f"custom:{custom_font.name}", "name": "業務用楷體"})
    assert r.status_code == 200, r.text
    row = _find(fc.list_fonts(include_hidden=True), custom_font.name)
    assert row["family"] == "業務用楷體"


def test_the_admin_page_offers_a_rename_button():
    html = Path("app/admin/templates/fonts.html").read_text(encoding="utf-8")
    assert "rename-custom-font" in html
    assert "/admin/fonts/rename" in html
