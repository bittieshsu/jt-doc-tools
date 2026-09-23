"""語音服務失敗時，畫面上那句話要**指向對的方向**。

## 由來（2026-09-23，語音服務 v2.9 核對我們的疑難排解頁時指出）

* **401 與 403 是兩件事**：401 是金鑰錯或被撤銷；403 是金鑰**對**、但沒有這個動作的
  權限。原本合成一句「確認金鑰是不是被撤銷或打錯了」—— 遇到 403 的人照著查會查不出問題。
* **網址過期不是排隊造成的**：對方收到送件就立刻拉檔，之後排多久都不會再拉。原本寫
  「多半是排隊太久；請管理員延長有效期」—— 前半句讓人去懷疑對方的佇列，後半句叫管理員
  去改一個**根本不存在的設定**。
* **0 段的「成功」**：對方的 GPU 伺服器在傳結果途中重啟，可能把 0 段當成功回來
  （對方已修，`.223` 升級前都可能發生）。我們本來就判失敗，但要讓使用者知道重送通常就好。

這幾句原本**一條測試都沒有**，改壞了（或改回錯的說法）不會有任何東西紅。
"""
from __future__ import annotations

import importlib
import logging

import httpx
import pytest

from app.core import jtlw_client as C


def _raise(status: int, body: dict | None = None) -> C.JtlwError:
    resp = httpx.Response(status, json=body or {},
                          request=httpx.Request("GET", "https://x.invalid/api/v1/jobs"))
    with pytest.raises(C.JtlwError) as ei:
        C._raise_for(resp)
    return ei.value


def test_401_and_403_say_different_things():
    e401 = _raise(401)
    e403 = _raise(403, {"error": {"code": "forbidden",
                                  "details": {"required_scope": "jobs:write"}}})
    assert str(e401) == C.MESSAGES["key_rejected"]
    assert str(e403) == C.MESSAGES["key_no_permission"]
    assert str(e401) != str(e403), "401 與 403 又合成一句了"
    assert "打錯" in str(e401) and "權限" not in str(e401)
    assert "權限" in str(e403) and "打錯" not in str(e403), (
        "403 的金鑰是對的 —— 叫人去查有沒有打錯會查不出問題")


def test_403_writes_the_missing_permission_to_the_log(caplog):
    """缺哪一個權限寫進記錄（不接在訊息尾巴 —— 那樣英 / 日介面查不到翻譯）。"""
    with caplog.at_level(logging.WARNING):
        e = _raise(403, {"error": {"code": "forbidden",
                                   "details": {"required_scope": "jobs:write"}}})
    assert "jobs:write" in caplog.text
    assert "jobs:write" not in str(e)


def test_an_expired_link_does_not_blame_the_queue():
    msg = C.describe_error("source_unreachable", http_status="404")
    assert msg == C.MESSAGES["url_expired"]
    assert "排隊" not in msg, "對方收到送件就拉檔 —— 排隊不會讓網址過期"
    assert "延長" not in msg, "錄音檔網址的有效期沒有設定可以改，不可以叫管理員去改"
    assert "重新送" in msg


def test_other_source_failures_still_point_at_our_address():
    msg = C.describe_error("source_unreachable", http_status="503")
    assert msg == C.MESSAGES["source_unreachable"]


def test_no_message_has_a_variable_tail():
    """`MESSAGES` 裡不可以有格式化欄位 —— 接上變數的整句在語系檔裡查不到。"""
    for k, v in C.MESSAGES.items():
        assert "{" not in v and "%" not in v, f"{k} 帶了變數：{v}"


def test_an_empty_success_fails_with_a_hint_to_resend(monkeypatch, tmp_path):
    """對方回報成功但 0 段 → 我們判失敗，訊息要說得出「重送通常就好」。"""
    R = importlib.import_module("app.tools.meeting_transcribe.router")

    class _Job:
        cancelled = False
        meta: dict = {}
        progress = 0.0
        message = ""

    class _Done:
        def submit(self, body, *, idempotency_key):
            return {"job_id": "remote-1"}

        def job(self, remote_id):
            return {"status": "succeeded"}

    monkeypatch.setattr(R.jtlw_client, "JtlwClient", lambda *a, **k: _Done())
    monkeypatch.setattr(R, "_build_body", lambda *a, **k: {})
    monkeypatch.setattr(R, "_assemble", lambda *a, **k: [])
    monkeypatch.setattr(R.time, "sleep", lambda s: None)
    monkeypatch.setattr(R, "_meta_path", lambda _u: tmp_path / "meta.json")
    (tmp_path / "meta.json").write_text("{}", encoding="utf-8")
    with pytest.raises(RuntimeError) as ei:
        R._run_job(_Job(), "0" * 32, "auto", None)
    assert str(ei.value) == C.MESSAGES["empty_result"]
    assert "重新送" in str(ei.value)
