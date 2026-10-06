"""通知裡的「我的作業」要是連結 —— 管理員沒填「站台網址」也一樣（v1.16.57）。

## 由來（2026-10-06 使用者看到通知信）

信裡寫「可到「我的作業」頁下載結果。」，**不能點**。程式其實會放連結，但前提是管理員在
通知設定填了「站台網址」—— 伺服器不知道對外網址（可能經反向代理、也可能是內網 IP），
沒填就退回純文字。正式機沒填，所以從來沒有連結。

## 做法

送出作業的那個請求，瀏覽器會在 `Origin`（沒有就 `Referer`）寫上它當時用的網址 ——
那正是這位使用者連得回來的地方，而通知只寄給他本人。送出時記在 `job.meta["origin"]`
（跟著作業存進資料庫），寄通知時：站台網址優先，沒有就用它，兩個都沒有才不放連結。

## 判準

* 只收 http / https、有主機名、不帶帳密，路徑丟掉；`javascript:`、`null` 這些一律不收。
* 從資料庫讀回來的值要再驗一次（不信任存著的字串）。
* 純文字版（Slack / Zulip / Teams 收到的就是它）也要寫出網址。
"""
from __future__ import annotations

import ast
import pathlib
from types import SimpleNamespace

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent

ORIGIN = "https://doc.example.test"


@pytest.mark.parametrize("headers,expected", [
    ({"origin": ORIGIN}, ORIGIN),
    ({"origin": "http://192.0.2.10:8765"}, "http://192.0.2.10:8765"),
    ({"referer": "http://192.0.2.10:8765/tools/pdf-merge/?a=1#x"}, "http://192.0.2.10:8765"),
    ({"origin": "null", "referer": ORIGIN + "/my-jobs"}, ORIGIN),
    ({"origin": "HTTPS://Doc.Example.Test"}, "https://doc.example.test"),
    ({"origin": "http://[2001:db8::1]:8765"}, "http://[2001:db8::1]:8765"),
])
def test_the_browser_origin_is_read_from_the_request(headers, expected):
    from app.core.http_utils import browser_origin
    assert browser_origin(headers) == expected


@pytest.mark.parametrize("headers", [
    {},
    {"origin": "null"},
    {"origin": "javascript:alert(1)"},
    {"origin": "ftp://doc.example.test"},
    {"origin": "https://user:pw@evil.example.test"},
    {"origin": "https://doc.example.test:notaport"},
    {"origin": "https://doc example.test"},
    {"origin": "https://"},
])
def test_anything_that_is_not_a_plain_web_address_is_ignored(headers):
    from app.core.http_utils import browser_origin
    assert browser_origin(headers) == ""


def test_a_submitted_job_remembers_where_it_was_submitted_from():
    from app.core import job_manager as jm
    jm.set_current_actor(None, "192.0.2.10", ORIGIN)
    job = jm.Job(id="x" * 32, tool_id="pdf-merge", meta={})
    jm.job_manager._attach_actor(job, None)
    assert job.meta.get("origin") == ORIGIN
    # 呼叫端自己帶了 origin 的話不蓋掉
    job2 = jm.Job(id="y" * 32, tool_id="pdf-merge", meta={"origin": "https://other.example.test"})
    jm.job_manager._attach_actor(job2, None)
    assert job2.meta["origin"] == "https://other.example.test"
    jm.set_current_actor(None, "", "")
    job3 = jm.Job(id="z" * 32, tool_id="pdf-merge", meta={})
    jm.job_manager._attach_actor(job3, None)
    assert "origin" not in job3.meta


def test_both_request_paths_record_the_origin():
    """認證關閉與開啟兩條路都要把瀏覽器的網址交給作業 —— 只接一條的話，另一種部署的信永遠沒有連結。"""
    tree = ast.parse((ROOT / "app" / "main.py").read_text(encoding="utf-8"))
    gate = next(n for n in ast.walk(tree)
                if isinstance(n, ast.AsyncFunctionDef) and n.name == "_auth_gate")
    calls = [c for c in ast.walk(gate)
             if isinstance(c, ast.Call) and getattr(c.func, "id", "") == "set_current_actor"]
    assert len(calls) >= 2, "找不到兩條路的 set_current_actor"
    for c in calls:
        assert len(c.args) >= 3 and "browser_origin" in ast.unparse(c.args[2]), (
            f"第 {c.lineno} 行沒有把瀏覽器的網址傳進去")


def _job(origin: str | None, saved: bool = False):
    meta = {"filename": "a.pdf"}
    if origin is not None:
        meta["origin"] = origin
    if saved:
        meta["workspace"] = {"saved": True}
    return SimpleNamespace(tool_id="pdf-merge", status="done", meta=meta, error=None,
                           result_filename="a.pdf", elapsed=lambda: 90, owner_id=None)


@pytest.fixture
def site_url(monkeypatch):
    from app.core import notify_settings
    box = {"site_url": ""}
    real = notify_settings.get
    monkeypatch.setattr(notify_settings, "get", lambda: {**real(), "site_url": box["site_url"]})
    return box


def test_the_configured_site_url_wins(site_url):
    from app.core.job_notify import _site_url
    site_url["site_url"] = "https://public.example.test/"
    assert _site_url("/my-jobs", _job(ORIGIN)) == "https://public.example.test/my-jobs"


def test_without_a_site_url_the_link_uses_the_origin(site_url):
    from app.core.job_notify import _site_url
    assert _site_url("/my-jobs", _job(ORIGIN)) == ORIGIN + "/my-jobs"
    assert _site_url("/my-jobs", _job(None)) == "", "兩個都沒有就不放連結"
    # 從資料庫讀回來的值要再驗一次
    assert _site_url("/my-jobs", _job("javascript:alert(1)")) == ""
    assert _site_url("/my-jobs", _job("https://user:pw@evil.example.test")) == ""


def test_the_email_and_the_chat_text_both_carry_the_link(site_url):
    from app.core import job_notify
    _subject, text = job_notify.build_message(_job(ORIGIN))
    assert f"可到「我的作業」頁下載結果：{ORIGIN}/my-jobs" in text
    _subject, text = job_notify.build_message(_job(ORIGIN, saved=True))
    assert f"結果已自動存入「我的工作區」：{ORIGIN}/workspace" in text
    html = job_notify.build_html(_job(ORIGIN))
    assert f'href="{ORIGIN}/my-jobs"' in html and "我的作業</a>" in html


def test_without_any_address_the_text_stays_as_before(site_url):
    from app.core import job_notify
    _subject, text = job_notify.build_message(_job(None))
    assert "可到「我的作業」頁下載結果。" in text
    html = job_notify.build_html(_job(None))
    assert "「我的作業」" in html and "我的作業</a>" not in html
