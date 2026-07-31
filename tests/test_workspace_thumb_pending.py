"""縮圖還沒做好時回的那張空白圖，不可以被瀏覽器快取。

## 由來

使用者回報：「你昨天有修我的工作區縮圖問題，但這裡怎麼有一個 pptx 還是沒有？」

那個檔是 33.9 MB 的簡報。查正式機的磁碟：`thumb.png` **其實已經產生了**
（比檔案存入的時間晚了十幾個小時），但畫面上仍是一片空白。所以問題不在產生，
在「產生好之後使用者拿不到」，有兩個原因：

1. **空白佔位圖被瀏覽器快取。** 回應沒有任何快取標頭，瀏覽器就會依啟發式規則
   自己決定要不要快取 —— 一旦快取住，之後每次重試都只是從快取拿回同一張空白
   圖，那個檔就「永遠沒有縮圖」。
2. **前端重試的時間窗太短。** 原本固定 6 次 × 2.5 秒（共 15 秒），但大檔轉一次
   要 48 秒（程式碼裡自己量的），一定等不到。

這份守住第 1 點（第 2 點在模板裡，由 `test_template_js_syntax` 保語法、
下面的常數檢查保時間窗）。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.main as app_main
from app.core import workspace as ws


@pytest.fixture
def c():
    return TestClient(app_main.app)


def test_placeholder_is_never_cached(c, monkeypatch):
    """產生中 / 失敗時回的空白圖一定要 no-store。"""
    monkeypatch.setattr(ws, "get_thumbnail",
                        lambda *a, **k: (_ for _ in ()).throw(
                            ws.WorkspaceError("預覽產生中")))
    monkeypatch.setattr(ws, "is_enabled", lambda: True)
    r = c.get("/workspace/thumb/" + "a" * 32)
    assert r.status_code == 200
    assert r.headers.get("content-type") == "image/png"
    cc = (r.headers.get("cache-control") or "").lower()
    assert "no-store" in cc, (
        "空白佔位圖被允許快取了 —— 縮圖產好之後使用者永遠拿不到")


def test_placeholder_is_the_1x1_blank(c, monkeypatch):
    """前端靠 naturalWidth <= 2 判斷「還沒好」，所以佔位圖必須是 1×1。"""
    monkeypatch.setattr(ws, "get_thumbnail",
                        lambda *a, **k: (_ for _ in ()).throw(
                            ws.WorkspaceError("預覽產生中")))
    monkeypatch.setattr(ws, "is_enabled", lambda: True)
    r = c.get("/workspace/thumb/" + "b" * 32)
    png = r.content
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    import struct
    w, h = struct.unpack(">II", png[16:24])
    assert (w, h) == (1, 1), f"佔位圖不是 1×1（{w}×{h}），前端會誤判成已完成"


def test_retry_window_covers_the_slowest_conversion():
    """前端重試的總時間要蓋得住最慢的檔。

    `workspace.py` 自己量到 37.9 MB 的簡報要 48 秒。重試總時間若小於它，
    大檔就一定看不到縮圖（使用者回報的正是這件事）。
    """
    tpl = (Path(__file__).resolve().parent.parent
           / "app" / "web" / "templates" / "my_workspace.html").read_text(
        encoding="utf-8")
    m = re.search(r"const DELAYS = \[([0-9,\s]+)\]", tpl)
    assert m, "找不到重試間隔的定義（DELAYS）"
    delays = [int(x) for x in m.group(1).replace(" ", "").split(",") if x]
    total = sum(delays) / 1000.0
    assert total >= 90, (
        f"重試總時間只有 {total:.0f} 秒，蓋不住實測 48 秒的大檔（要留餘裕）")
    # 前面幾次要密一點，小檔（幾秒）不該等太久
    assert delays[0] <= 3000, "第一次重試太晚，小檔的縮圖會慢半拍才出現"
