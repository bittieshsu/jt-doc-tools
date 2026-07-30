"""逐句翻譯改成背景作業（離開頁面也會繼續跑）。

## 這支的由來

使用者回報：「為何逐句翻譯一離開該頁就沒繼續了？作業裡也沒有？」

原因是這個工具**從來沒有走過作業系統** —— 翻譯是瀏覽器自己驅動的：前端開 4 個
worker 逐句打 `/translate-one`，結果存在那個分頁的記憶體裡。所以：

* 關掉分頁 = 翻譯停止（伺服器端沒有任何東西在跑）
* 「我的作業」看不到（根本沒有建立作業）

幾萬句的文件要人一直開著頁面等，不合理。改成伺服器端背景作業之後，送出即與分頁
無關，回到頁面（或從「我的作業」點進來）能看到進度與已完成的句子。

## 這份要守住的事

1. 送出後**伺服器端**真的有作業在跑，而且出現在作業清單裡。
2. 結果要能**中途查得到**（不是只有全部做完才給），否則「回來看進度」沒有意義。
3. 結果不可以塞進 `job.meta` —— 那有 64KB 上限，幾萬句的譯文會爆掉。
4. 進度查詢要有歸屬檢查（譯文就是文件內容）。
5. 可以中途取消。
"""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

import app.main as app_main


@pytest.fixture(autouse=True)
def _fake_llm(monkeypatch):
    """把 LLM 換掉 —— 這份測的是作業流程，不是翻譯品質。"""
    # `from ... import router` 會拿到那個 APIRouter 物件（套件 __init__ 匯入了
    # 它），不是模組 —— 用 import_module 明確取模組才 patch 得到。
    import importlib
    trd = importlib.import_module("app.tools.translate_doc.router")

    monkeypatch.setattr(trd.llm_settings, "is_enabled", lambda: True)
    monkeypatch.setattr(trd.llm_settings, "make_client", lambda: object())
    monkeypatch.setattr(trd.llm_settings, "get_model_for", lambda _t: "fake-model")
    monkeypatch.setattr(trd.llm_settings, "get", lambda: {"translate_concurrency": 2})
    monkeypatch.setattr(trd, "_warmup_llm", lambda *a, **k: None)
    monkeypatch.setattr(trd, "_detect_language", lambda _t: "en")

    def fake_one(client, model, src, sl, tl, domain=""):
        time.sleep(0.02)              # 讓「中途查得到」有機會被觀察到
        return {"src": src, "translated": f"[{tl}] {src}"}

    monkeypatch.setattr(trd, "_translate_one", fake_one)
    # 作業紀錄表由 app 啟動時建立；這裡有些測試在發第一個請求前就會用到，
    # 而 `/api/jobs` 是讀資料庫不是讀記憶體，沒有表就會查不到任何東西。
    from app.core import job_store
    job_store.init()
    yield


@pytest.fixture
def c():
    return TestClient(app_main.app)


def _done_count(results) -> int:
    """已完成的句數。

    進度檔一開始就寫入**全部原文**（讓使用者一回到頁面就看得到對照表），
    所以「有這一列」不等於「翻好了」—— 要看有沒有 translated / error。
    前端用的是同一個判斷。
    """
    return sum(1 for r in results
               if r and (r.get("translated") is not None or r.get("error")))


def _wait_done(c, jid, timeout=20):
    for _ in range(int(timeout / 0.2)):
        r = c.get(f"/tools/translate-doc/job/{jid}")
        assert r.status_code == 200, r.text
        j = r.json()
        if j["status"] in ("done", "error", "cancelled", "interrupted"):
            return j
        time.sleep(0.2)
    raise AssertionError("作業沒有在時限內完成")


def test_start_returns_job_id_and_runs_server_side(c):
    r = c.post("/tools/translate-doc/start",
               json={"sentences": ["hello", "world"], "target_lang": "zh-TW"})
    assert r.status_code == 200, r.text
    jid = r.json()["job_id"]
    assert r.json()["total"] == 2
    j = _wait_done(c, jid)
    assert j["status"] == "done"
    assert [x["translated"] for x in j["results"]] == ["[zh-TW] hello",
                                                       "[zh-TW] world"]


def test_job_appears_in_job_list(c):
    """「作業裡也沒有」是使用者回報的一半 —— 必須看得到。"""
    r = c.post("/tools/translate-doc/start", json={"sentences": ["a"]})
    jid = r.json()["job_id"]
    _wait_done(c, jid)
    jobs = c.get("/api/jobs").json().get("jobs", [])
    assert any(x["id"] == jid for x in jobs), "作業清單裡找不到這筆"
    row = [x for x in jobs if x["id"] == jid][0]
    assert row["tool_id"] == "translate-doc"


def test_partial_results_are_visible_while_running(c):
    """跑到一半就要查得到已完成的句子，否則「回來看進度」沒有意義。"""
    r = c.post("/tools/translate-doc/start",
               json={"sentences": [f"s{i}" for i in range(120)]})
    jid = r.json()["job_id"]
    seen_partial = False
    for _ in range(100):
        j = c.get(f"/tools/translate-doc/job/{jid}").json()
        got = _done_count(j["results"])
        if j["status"] in ("running", "pending") and 0 < got < 120:
            seen_partial = True
            break
        if j["status"] == "done":
            break
        time.sleep(0.05)
    _wait_done(c, jid)
    assert seen_partial, "整批做完才看得到結果 —— 中途查不到進度"


def test_source_text_is_available_immediately(c):
    """一送出就要看得到原文（右邊還空著）。

    不然在第一次落檔之前回到頁面的人會看到一片空白，以為作業掉了 ——
    CDP 端對端測試就是這樣抓到的（4 秒後回來顯示「完成 0 句」且沒有任何原文）。
    """
    r = c.post("/tools/translate-doc/start",
               json={"sentences": [f"sentence {i}" for i in range(60)]})
    jid = r.json()["job_id"]
    j = c.get(f"/tools/translate-doc/job/{jid}").json()
    assert len(j["results"]) == 60, "原文沒有立刻寫進進度檔"
    assert j["results"][0]["src"] == "sentence 0"
    _wait_done(c, jid)


def test_start_parameter_only_returns_new_rows(c):
    """`?start=` 讓前端只取新的部分（幾萬句每次全量回傳太浪費）。"""
    r = c.post("/tools/translate-doc/start",
               json={"sentences": ["a", "b", "c", "d"]})
    jid = r.json()["job_id"]
    _wait_done(c, jid)
    j = c.get(f"/tools/translate-doc/job/{jid}?start=2").json()
    assert j["start"] == 2
    assert len(j["results"]) == 2
    assert j["results"][0]["src"] == "c"


def test_results_are_not_stored_in_job_meta(c):
    """meta 有 64KB 上限 —— 譯文必須放在別的地方，不然大檔會爆掉。"""
    from app.core.job_manager import job_manager
    r = c.post("/tools/translate-doc/start",
               json={"sentences": [f"sentence number {i}" for i in range(50)]})
    jid = r.json()["job_id"]
    _wait_done(c, jid)
    meta = job_manager.get(jid).meta or {}
    blob = str(meta)
    assert "translated" not in blob, "譯文被塞進 job.meta 了"
    assert len(blob) < 4096


def test_progress_and_message_are_reported(c):
    r = c.post("/tools/translate-doc/start",
               json={"sentences": [f"s{i}" for i in range(30)]})
    jid = r.json()["job_id"]
    j = _wait_done(c, jid)
    assert j["progress"] == pytest.approx(1.0)
    assert "30" in (j["message"] or "")


def test_cancel_stops_the_job(c):
    r = c.post("/tools/translate-doc/start",
               json={"sentences": [f"s{i}" for i in range(400)]})
    jid = r.json()["job_id"]
    time.sleep(0.2)
    c.post(f"/api/jobs/{jid}/cancel")
    j = _wait_done(c, jid, timeout=30)
    assert j["status"] == "cancelled"


def test_view_url_points_back_to_the_page(c):
    """「我的作業」要能點回這一頁看進度 —— 這個工具的產出不是一個檔案。"""
    from app.core.job_manager import job_manager
    r = c.post("/tools/translate-doc/start", json={"sentences": ["a"]})
    jid = r.json()["job_id"]
    _wait_done(c, jid)
    meta = job_manager.get(jid).meta or {}
    assert meta.get("view_url") == f"/tools/translate-doc/?job={jid}"


def test_empty_sentences_rejected(c):
    assert c.post("/tools/translate-doc/start", json={"sentences": []}
                  ).status_code == 400


def test_malformed_job_id_rejected(c):
    r = c.get("/tools/translate-doc/job/..%2F..%2Fetc%2Fpasswd")
    assert r.status_code in (400, 404)


def test_unknown_job_is_404(c):
    # 用隨機 id，不要寫死 "f"*32 之類的值 —— 別的測試檔會種同樣的固定 id 進
    # 作業紀錄，整套一起跑時就會撞到（單獨跑會過、整套跑會紅）。
    import uuid
    assert c.get(f"/tools/translate-doc/job/{uuid.uuid4().hex}"
                 ).status_code == 404


def test_other_user_cannot_read_progress(admin_session):
    """譯文就是文件內容 —— 進度查詢要有歸屬檢查。"""
    from app.core import sessions, user_manager
    from app.core.job_manager import job_manager
    from app.core import job_store

    uid_a = user_manager.create_local("trd-a", "A", "UserPass1234")
    uid_b = user_manager.create_local("trd-b", "B", "UserPass1234")
    tok_a, _ = sessions.issue(uid_a, remember=False, ip="127.0.0.1", ua="pytest")
    tok_b, _ = sessions.issue(uid_b, remember=False, ip="127.0.0.1", ua="pytest")
    ca = TestClient(app_main.app); ca.cookies.set(sessions.COOKIE_NAME, tok_a)
    cb = TestClient(app_main.app); cb.cookies.set(sessions.COOKIE_NAME, tok_b)

    r = ca.post("/tools/translate-doc/start", json={"sentences": ["secret"]})
    if r.status_code == 403:
        pytest.skip("此帳號沒有 translate-doc 權限")
    jid = r.json()["job_id"]
    _wait_done(ca, jid)
    assert cb.get(f"/tools/translate-doc/job/{jid}").status_code == 404
    assert ca.get(f"/tools/translate-doc/job/{jid}").status_code == 200


def test_source_is_written_before_the_job_starts(c, monkeypatch):
    """佇列忙的時候也要看得到原文 —— 不能等作業真的開始跑才寫。

    先把派工暫停，讓作業一定停在 pending，再查進度：原文要已經在了。
    這條就是原本那個競賽的根因（作業還沒輪到 → 進度檔是空的 → 使用者回到
    頁面看到一片空白，以為送丟了）。
    """
    from app.core.job_manager import job_manager
    job_manager.set_paused(True)
    try:
        r = c.post("/tools/translate-doc/start",
                   json={"sentences": ["queued one", "queued two"]})
        jid = r.json()["job_id"]
        j = c.get(f"/tools/translate-doc/job/{jid}").json()
        assert j["status"] == "pending", j["status"]
        assert [x["src"] for x in j["results"]] == ["queued one", "queued two"]
    finally:
        job_manager.set_paused(False)
    _wait_done(c, jid)
