"""「檔案太大」要說得出是哪一段擋的（v1.16.34）。

使用者 2026-10-02 傳一個 300 MB 的錄音，畫面只寫「檔案太大（413）」——
實際上是**網站前面的反向代理**擋的（它的上限 300 MB），本系統自己的上限是 500 MB。
看不出是哪一段，就不知道該去哪裡改。

本系統回的 413 一律帶 `x-jtdt-limit`：`site`＝全站上限（`app/main.py` 的中介層）、
`tool`＝個別功能自己的上限。**沒有這個標頭、也沒有我們的 JSON 說明的 413，就是前面的代理擋的**
—— 那種回應根本沒到我們，只能由前端從「沒有標記」反推（`static/js/friendly_error.js`）。
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------- 伺服器端：每個 413 都帶標記

def test_the_site_wide_limit_marks_itself_with_its_size(client, monkeypatch):
    from app.core import upload_settings
    monkeypatch.setattr(upload_settings, "max_upload_bytes", lambda: 1024 * 1024)
    r = client.post("/tools/text-diff/compare", content=b"x" * (2 * 1024 * 1024),
                    headers={"content-type": "application/json"})
    assert r.status_code == 413
    assert r.headers.get("x-jtdt-limit") == "site"
    assert r.headers.get("x-jtdt-limit-mb") == "1"
    assert "系統狀態" in r.json()["detail"], "說明要講得出管理員去哪裡改"


def test_a_tools_own_limit_is_marked_as_the_tool(client):
    big = "字" * (400 * 1024)                  # 單側超過 1 MiB（UTF-8 三位元組）
    r = client.post("/tools/text-diff/compare", json={"text_a": big, "text_b": "x"})
    assert r.status_code == 413
    assert r.headers.get("x-jtdt-limit") == "tool"


def test_other_responses_are_not_marked(client):
    """反向對照：只給 413 加標記。全部都加的話「沒有標記＝代理」這個判斷就失去意義。"""
    r = client.post("/tools/text-diff/compare", content=b"not json",
                    headers={"content-type": "application/json"})
    assert r.status_code == 400
    assert "x-jtdt-limit" not in r.headers
    assert "x-jtdt-limit" not in client.get("/healthz").headers


# ---------------------------------------------------------------- 前端：訊息說得出是哪一段

_NODE = shutil.which("node")

_HARNESS = r"""
const fs = require('fs');
global.window = {};
global.tr = (s) => s;
global.console = console;
eval(fs.readFileSync(process.argv[2], 'utf8'));
function resp(status, ct, body, headers) {
  const h = Object.assign({'content-type': ct}, headers || {});
  return {
    status, headers: {get: (k) => h[k.toLowerCase()] || null},
    json: () => Promise.resolve(JSON.parse(body)), text: () => Promise.resolve(body),
  };
}
(async () => {
  const out = {};
  out.proxy = await window.friendlyServerError(
    resp(413, 'text/html', '<html><head><title>413 Request Entity Too Large</title></head></html>'));
  out.site = await window.friendlyServerError(
    resp(413, 'application/json', JSON.stringify({detail: '檔案太大 —— 本系統的單次上傳上限是 500 MB。'}),
         {'x-jtdt-limit': 'site', 'x-jtdt-limit-mb': '500'}));
  out.tool = await window.friendlyServerError(
    resp(413, 'application/json', JSON.stringify({detail: '單側文字超過 1024 KiB 上限'}),
         {'x-jtdt-limit': 'tool'}));
  out.legacy = await window.friendlyServerError(
    resp(413, 'application/json', JSON.stringify({detail: '檔案過大（單檔上限 50 MB）'})));
  out.other = await window.friendlyServerError(
    resp(400, 'application/json', JSON.stringify({detail: '參數不對'})));
  process.stdout.write(JSON.stringify(out));
})();
"""


@pytest.fixture(scope="module")
def messages(tmp_path_factory):
    if not _NODE:
        pytest.skip("沒有 node")
    h = tmp_path_factory.mktemp("fe") / "h.js"
    h.write_text(_HARNESS, encoding="utf-8")
    out = subprocess.run([_NODE, str(h), str(ROOT / "static/js/friendly_error.js")],
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_a_proxy_rejection_says_it_was_the_proxy(messages):
    m = messages["proxy"]
    assert "反向代理" in m and "client_max_body_size" in m, m
    assert "不是本系統的上限" in m, m


def test_the_site_limit_says_its_size_and_where_to_change_it(messages):
    m = messages["site"]
    assert "500 MB" in m and "系統狀態" in m, m
    assert "反向代理" not in m, m


def test_a_tool_limit_names_the_feature_and_keeps_its_detail(messages):
    m = messages["tool"]
    assert "這項功能" in m and "1024 KiB" in m, m
    assert "反向代理" not in m, m


def test_our_own_older_413_without_the_header_is_not_blamed_on_the_proxy(messages):
    """還沒更新的伺服器回的 413 沒有標記，但帶著我們的 JSON 說明 —— 不可以說成是代理擋的。"""
    m = messages["legacy"]
    assert "反向代理" not in m and "50 MB" in m, m


def test_other_errors_are_unchanged(messages):
    assert messages["other"] == "請求格式錯誤（400）：參數不對"
