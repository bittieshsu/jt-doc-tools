"""工作區收 JPEG 與 TIFF（使用者 2026-10-14：「工作區允許 tiff」）。

事務機掃描可以存 JPEG（每頁一個檔），機內信箱取件可以存設備給的原始 TIFF。驗：

* 三條存檔路（位元組、上傳串流、伺服器上的檔案）都認得 JPEG / TIFF（含 BigTIFF 的檔頭），只看檔頭，串流寫入。
* `verify=True` 時圖片要打得開：截斷的 JPEG、宣稱幾十億像素的 TIFF 都拒絕（不解碼整張）。
* 歸在「圖片」一類；格式說明由清單實算，裡面有 JPEG / TIFF。
* 縮圖：JPEG 直接給本身，TIFF 畫第一頁成 PNG（瀏覽器畫不出 TIFF）、快取起來。
* 「從工作區載入」：收 `image/*` 的工具挑得到 JPEG / TIFF；寫成 MIME 或 `.jpeg` / `.tiff` 也對得上；
  只收 PNG 的工具挑不到 JPEG。
"""
from __future__ import annotations

import io
import shutil
import subprocess
from pathlib import Path

import pytest

from app.core import workspace as ws
from tests.test_workspace_large_files import _Req, wsenv  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]


def _jpeg(w=400, h=300) -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (w, h), "white").save(buf, "JPEG", quality=50)
    return buf.getvalue()


def _tiff(pages=2) -> bytes:
    from PIL import Image
    fr = [Image.new("1", (600, 800), 1) for _ in range(pages)]
    buf = io.BytesIO()
    fr[0].save(buf, "TIFF", compression="group4", save_all=True, append_images=fr[1:], dpi=(200, 200))
    return buf.getvalue()


@pytest.mark.parametrize("data,ext,mime", [(_jpeg(), ".jpg", "image/jpeg"), (_tiff(), ".tif", "image/tiff")])
def test_all_three_save_paths_take_jpeg_and_tiff(wsenv, tmp_path, data, ext, mime):
    key = "u1"
    m1 = ws.save_bytes_for_key(key, data, "a" + ext)
    m2 = ws.save_stream_for_key(key, io.BytesIO(data), "b" + ext)
    p = tmp_path / ("c" + ext)
    p.write_bytes(data)
    m3 = ws.save_file_for_key(key, p, "c" + ext, verify=True, expect={ext})
    for m in (m1, m2, m3):
        assert m["ext"] == ext and m["mime"] == mime, m
    assert ws.kind_of(ext) == "image" and ws.has_preview(ext)


@pytest.mark.parametrize("head,ext", [(b"II*\x00", ".tif"), (b"MM\x00*", ".tif"), (b"II+\x00", ".tif"),
                                      (b"\xff\xd8\xff\xe0", ".jpg")])
def test_kinds_come_from_the_header_not_the_name(head, ext):
    assert ws.detect_kind(head + b"\x00" * 32, "whatever.pdf")[1] == ext


def test_a_broken_image_is_refused_when_verifying(wsenv, tmp_path, monkeypatch):
    p = tmp_path / "x.jpg"
    p.write_bytes(_jpeg()[:300])
    with pytest.raises(ws.UnsupportedType) as e:
        ws.save_file_for_key("u1", p, "x.jpg", verify=True)
    assert str(e.value) == ws.IMAGE_BROKEN_MESSAGE
    monkeypatch.setattr(ws, "_IMAGE_MAX_PIXELS", 1000)
    q = tmp_path / "y.tif"
    q.write_bytes(_tiff(1))
    with pytest.raises(ws.UnsupportedType):
        ws.save_file_for_key("u1", q, "y.tif", verify=True)
    assert not ws.list_files(_Req(1)), "驗證失敗不可以留下半個項目"


def test_jpeg_and_tiff_are_listed_as_pictures():
    groups = dict(ws.accepted_type_groups())
    assert {".png", ".jpg", ".tif"} <= set(groups["圖片"])
    assert "JPEG" in ws.UNSUPPORTED_MESSAGE and "TIFF" in ws.UNSUPPORTED_MESSAGE


def test_thumbnails(wsenv):
    j = ws.save_bytes_for_key("u1", _jpeg(), "a.jpg")
    t = ws.save_bytes_for_key("u1", _tiff(3), "b.tif")
    path, mime = ws.get_thumbnail(_Req(1), j["file_id"])
    assert mime == "image/jpeg" and path.name == "file.jpg"
    path, mime = ws.get_thumbnail(_Req(1), t["file_id"])
    assert mime == "image/png" and path.name == "thumb.png" and path.read_bytes().startswith(b"\x89PNG")
    from PIL import Image
    with Image.open(path) as im:
        assert max(im.size) <= 1200


def test_a_broken_tiff_thumbnail_is_marked_and_not_retried(wsenv):
    t = ws.save_bytes_for_key("u1", b"II*\x00" + b"\x00" * 64, "b.tif")
    for _ in range(2):
        with pytest.raises(ws.WorkspaceError):
            ws.get_thumbnail(_Req(1), t["file_id"])
    d = ws._root() / "u1" / t["file_id"]
    assert (d / "thumb.failed").exists()


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
@pytest.mark.parametrize("accept,want", [
    ("image/*", ["png", "jpg", "tif"]),
    (".pdf,image/*,.tif,.tiff", ["pdf", "png", "jpg", "tif"]),
    ("image/png,image/jpeg", ["png", "jpg"]),
    ("image/png", ["png"]),
    (".png,.jpeg", ["png", "jpg"]),
    (".tiff", ["tif"]),
    ("application/pdf,.pdf", ["pdf"]),
])
def test_the_picker_matches_image_types(accept, want):
    js = (ROOT / "static" / "js" / "workspace_picker.js").read_text(encoding="utf-8")
    prog = ("const window = {}; const document = {addEventListener(){}, querySelector(){return null}, "
            "querySelectorAll(){return []}, readyState: 'complete'};\n" + js +
            f"\nconsole.log(JSON.stringify(window.workspaceAcceptExts({accept!r}, 'pdf png jpg tif docx')));")
    out = subprocess.run(["node", "-e", prog], capture_output=True, text=True, timeout=20)
    assert out.returncode == 0, out.stderr
    assert sorted(__import__("json").loads(out.stdout.strip().splitlines()[-1])) == sorted(want)
