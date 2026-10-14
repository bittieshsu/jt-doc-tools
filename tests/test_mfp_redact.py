"""事務機取件：診斷資訊的遮蔽規則（畫面、下載報告、服務紀錄檔共用這一份）。

遮漏一次就是把設備的 Cookie / 密碼交給看報告的人；遮過頭則讓診斷報告沒有用
（例如把 `Content-Type` 或頁面標題也遮掉）。兩個方向都要測。
"""
from __future__ import annotations

import json

import pytest

from app.core import mfp_redact as r


@pytest.mark.parametrize("name", ["Authorization", "Proxy-Authorization", "Cookie",
                                  "X-CSRF-Token", "x-xsrf-token", "X-Auth-Token"])
def test_secret_headers_are_masked_whole(name):
    out = dict(r.redact_headers([(name, "Basic dXNlcjpwYXNz")]))
    assert "dXNlcjpwYXNz" not in out[name]
    assert "已遮蔽" in out[name] and "長度 18" in out[name], "要看得出有帶值、多長"


def test_set_cookie_keeps_name_and_flags_but_not_value():
    out = dict(r.redact_headers([("Set-Cookie", "SESSIONID=abc123secret; Path=/; HttpOnly; Secure")]))
    v = out["Set-Cookie"]
    assert "abc123secret" not in v
    assert v.startswith("SESSIONID=") and "HttpOnly" in v and "Secure" in v and "Path=/" in v


def test_ordinary_headers_are_left_alone():
    hs = [("Server", "Apache"), ("Content-Type", "text/html; charset=UTF-8"),
          ("Content-Length", "1234"), ("X-Frame-Options", "SAMEORIGIN")]
    assert r.redact_headers(hs) == hs, "一般標頭被遮掉，診斷報告就沒用了"


def test_location_header_query_secrets_are_masked():
    out = dict(r.redact_headers([("Location", "/home/index.html?session=XYZ987&lang=ja")]))
    assert "XYZ987" not in out["Location"] and "lang=ja" in out["Location"]


@pytest.mark.parametrize("url,secret", [
    ("https://10.0.0.5/box?passwd=1234&box=001", "1234"),
    ("http://10.0.0.5/a?TOKEN=deadbeef", "deadbeef"),
    ("http://10.0.0.5/a?csrf_token=cafe", "cafe"),
    ("http://10.0.0.5/a?JSESSIONID=s1s2s3", "s1s2s3"),
    ("http://admin:hunter2@10.0.0.5/", "hunter2"),
])
def test_url_secrets_are_masked(url, secret):
    out = r.redact_url(url)
    assert secret not in out, out


def test_url_ordinary_params_survive():
    out = r.redact_url("http://10.0.0.5/list?box=001&page=2&sort=date")
    assert "box=001" in out and "page=2" in out and "sort=date" in out


@pytest.mark.parametrize("text,secret", [
    ('password=hunter2', "hunter2"),
    ('{"csrfToken": "a1b2c3d4"}', "a1b2c3d4"),
    ("sessionid: QWERTY12", "QWERTY12"),
    ("go to /login?sid=777888 now", "777888"),
])
def test_free_text_secrets_are_masked(text, secret):
    assert secret not in r.redact_text(text)


def test_free_text_ordinary_words_survive():
    t = "Apeos C5571 Internet Services ログイン 機密ボックス 001 Firmware 1.2.3"
    assert r.redact_text(t) == t


def test_redact_obj_masks_by_key_and_walks_everything():
    data = {"host": "10.0.0.5", "password": "hunter2", "steps": [
        {"headers": [["Server", "x"]], "note": "token=abc999"},
        {"form": {"inputs": [{"name": "passwd", "type": "password"}]}},
    ]}
    out = r.redact_obj(data)
    s = json.dumps(out, ensure_ascii=False)
    assert "hunter2" not in s and "abc999" not in s
    assert out["host"] == "10.0.0.5"
    # 表單結構（欄位名稱與型別）要留著 —— 之後寫登入流程就靠它；值本來就不記
    assert out["steps"][1]["form"]["inputs"][0] == {"name": "passwd", "type": "password"}


def test_mask_never_echoes_the_value():
    assert "s3cr3t" not in r.mask("s3cr3t")


@pytest.mark.parametrize("fn,arg", [
    (r.redact_url, "http://10.0.0.5/home/index.html?lang=ja&session=abc999"),
    (r.redact_text, "go to /login?sid=777888 and password=hunter2"),
    (r.redact_set_cookie, "JSID=s3cret; Path=/; HttpOnly"),
])
def test_redacting_twice_changes_nothing(fn, arg):
    """報告先在各步驟遮一次、最後整份再遮一次 —— 第二次不可以把遮蔽標記再遮一層。"""
    once = fn(arg)
    assert fn(once) == once
    assert once.count("已遮蔽") == fn(once).count("已遮蔽")


def test_redacting_a_whole_report_twice_changes_nothing():
    rep = {"headers": [list(h) for h in r.redact_headers([("Location", "/a?session=abc999"),
                                                          ("Set-Cookie", "JSID=s3cret; Path=/")])],
           "text": "token=abc", "url": "/x?passwd=1"}
    once = r.redact_obj(rep)
    assert r.redact_obj(once) == once
    assert "長度 6］ " not in json.dumps(once, ensure_ascii=False)
