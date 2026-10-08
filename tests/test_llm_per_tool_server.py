"""每支工具可以指定「另一台 LLM 伺服器」（全站通用功能）。

## 為什麼要有

全站原本只有一台 LLM 伺服器，每支工具只能另選**模型**。有些工具的資料只能送到特定的
伺服器（例如公文只能在機關自己的地端伺服器上跑），所以管理員要能另外登錄幾台、
把某一支工具指過去。

## 這份測試守的事（每一條都要兩個方向）

1. 工具 A 指到伺服器 X → A 的請求打到 X、**帶 X 的金鑰**；工具 B 照舊打全站那一台。
   （只驗 A 打到 X 的話，把所有工具都送去 X 也會過。）
2. **X 不見了 / 位址不合格 → A 明確失敗，而且沒有任何請求跑到全站那一台。**
   這是整個功能最重要的性質：管理員把 A 指到 X，多半是因為資料只能送去 X ——
   偷偷退回全站那台，等於把資料送到管理員明確不要的地方，而畫面上看不出來。
3. 金鑰不外洩：`get()`、設定頁 HTML、設定 API 都只有替身字串；設定檔裡是密文。
4. 「測試連線」帶替身字串時，**存著的金鑰只送給那一台存檔時的位址**（全站那一把的
   既有規則，每一台都要照）。
5. 位址走同一道 SSRF 檢查（雲端中繼資料位址、非 http(s) 一律擋）。
6. 存檔留稽核紀錄（`settings_change`、目標 `llm`），紀錄裡沒有金鑰。
7. 設定匯出 / 匯入（換機器）時每一台的金鑰都重新加密。
8. 有工具指到不在清單裡的伺服器 → 整筆不存（管理頁的刪除擋下之外的第二道）。
"""
from __future__ import annotations

import json
import socket
import threading
import time

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.core import llm_client as lc
from app.core import llm_settings as ls_mod
from app.core.llm_settings import LLMServerUnavailable, llm_settings

SID_X = "a1b2c3d4e5f6"
SID_Y = "0f1e2d3c4b5a"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeServer:
    """最小的 OpenAI 相容服務：記下每一個請求打了哪裡、帶了什麼金鑰。"""

    def __init__(self, answer: str = "OK"):
        self.answer = answer
        self.chat_auth: list[str] = []
        self.models_auth: list[str] = []
        self.port = _free_port()
        self.app = self._build()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    @property
    def hits(self) -> int:
        return len(self.chat_auth) + len(self.models_auth)

    def _build(self) -> FastAPI:
        app = FastAPI()
        me = self

        @app.get("/api/version")
        async def version():
            return JSONResponse({"error": "not found"}, status_code=404)

        @app.get("/v1/models")
        async def models(request: Request):
            me.models_auth.append(request.headers.get("authorization", ""))
            return {"data": [{"id": f"model-on-{me.port}", "owned_by": "x"}]}

        @app.post("/v1/chat/completions")
        async def chat(request: Request):
            body = await request.json()
            me.chat_auth.append(request.headers.get("authorization", ""))
            if not body.get("stream"):
                return {"choices": [{"message": {"content": me.answer}}]}

            def gen():
                yield "data: " + json.dumps(
                    {"choices": [{"delta": {"content": me.answer}}]}) + "\n\n"
                yield "data: [DONE]\n\n"
            return StreamingResponse(gen(), media_type="text/event-stream")

        return app

    def __enter__(self):
        import uvicorn
        cfg = uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_level="error")
        self._server = uvicorn.Server(cfg)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        for _ in range(100):
            if getattr(self._server, "started", False):
                break
            time.sleep(0.05)
        lc._BACKEND_CACHE.clear()
        lc._REJECTED_PARAMS.clear()
        return self

    def __exit__(self, *exc):
        self._server.should_exit = True
        self._thread.join(timeout=5)
        lc._BACKEND_CACHE.clear()
        lc._REJECTED_PARAMS.clear()


@pytest.fixture
def saved_settings():
    """這些測試會寫 LLM 設定檔 —— 跑完原樣還回去。"""
    p = llm_settings._path
    before = p.read_bytes() if p.exists() else None
    yield
    if before is None:
        p.unlink(missing_ok=True)
    else:
        p.write_bytes(before)
    lc._BACKEND_CACHE.clear()
    lc._REJECTED_PARAMS.clear()


def _setup(g: FakeServer, x: FakeServer, *, tool="official-doc", key_x="sk-x-secret"):
    llm_settings.update({
        "enabled": True, "base_url": g.base, "api_key": "sk-global-secret",
        "servers": [{"id": SID_X, "name": "機關地端", "base_url": x.base,
                     "api_key": key_x, "timeout_seconds": 120}],
        "server_per_tool": {tool: SID_X},
    })


def _drop_server_from_file(new_url: str | None = None):
    """模擬「伺服器被拿掉，但工具還指著它」—— 設定匯入、手動改檔都會造成這個狀態
    （管理頁存檔會擋，所以這裡直接改檔）。給 `new_url` 就是改成那個位址而不是刪掉。"""
    data = json.loads(llm_settings._path.read_text(encoding="utf-8"))
    if new_url is None:
        data["servers"] = []
    else:
        data["servers"][0]["base_url"] = new_url
    llm_settings._path.write_text(json.dumps(data), encoding="utf-8")


# ------------------------------------------------------------ 1. 請求打到對的那一台

def test_a_tool_on_another_server_goes_there_and_others_stay(saved_settings):
    with FakeServer("from-global") as g, FakeServer("from-x") as x:
        _setup(g, x)
        assert llm_settings.make_client("official-doc").text_query("hi", "m") == "from-x"
        assert llm_settings.make_client("translate-doc").text_query("hi", "m") == "from-global"
        assert llm_settings.make_client().text_query("hi", "m") == "from-global"
    assert x.chat_auth and all(a == "Bearer sk-x-secret" for a in x.chat_auth), x.chat_auth
    assert g.chat_auth and all(a == "Bearer sk-global-secret" for a in g.chat_auth), g.chat_auth
    # 反向：各自只收到自己那幾支工具的請求
    assert len(x.chat_auth) == 1 and len(g.chat_auth) == 2


def test_the_server_timeout_and_an_override_are_used(saved_settings):
    with FakeServer() as g, FakeServer() as x:
        _setup(g, x)
        assert llm_settings.make_client("official-doc").timeout == 120
        assert llm_settings.make_client("official-doc", timeout=30).timeout == 30
        assert llm_settings.make_client("translate-doc").timeout == float(
            llm_settings.get()["timeout_seconds"])


def test_changing_one_tool_does_not_touch_the_others_or_the_global(saved_settings):
    with FakeServer() as g, FakeServer() as x:
        _setup(g, x)
        before = llm_settings.get()
        llm_settings.update({"server_per_tool": {"official-doc": SID_X, "doc-translate": SID_X},
                             "model_per_tool": {"doc-translate": "qwen3.8:27b"}})
        after = llm_settings.get()
    assert after["base_url"] == before["base_url"], "專屬設定不可以寫回全站設定"
    assert after["model"] == before["model"]
    assert after["server_per_tool"] == {"official-doc": SID_X, "doc-translate": SID_X}
    assert llm_settings.server_for("translate-doc") == ""
    assert llm_settings.get_model_for("translate-doc") == before["model"]


def test_disabled_llm_still_returns_none(saved_settings):
    with FakeServer() as g, FakeServer() as x:
        _setup(g, x)
        llm_settings.update({"enabled": False})
        assert llm_settings.make_client("official-doc") is None
    assert x.hits == 0 and g.hits == 0


# ------------------------------------------------------------ 2. 失敗就失敗，不退回全站

def test_a_missing_server_fails_and_nothing_reaches_the_global_one(saved_settings):
    with FakeServer() as g, FakeServer() as x:
        _setup(g, x)
        _drop_server_from_file()
        with pytest.raises(LLMServerUnavailable):
            llm_settings.make_client("official-doc")
        assert llm_settings.server_problem("official-doc") == LLMServerUnavailable.MESSAGE
        assert llm_settings.base_url_for("official-doc") == "", \
            "畫面上不可以說它送去全站那一台"
        # 別的工具不受影響
        assert llm_settings.make_client("translate-doc").text_query("hi", "m")
    assert len(g.chat_auth) == 1, "只有 translate-doc 那一次可以到全站那一台"
    assert x.hits == 0


def test_a_server_with_a_blocked_address_fails_without_falling_back(saved_settings):
    with FakeServer() as g, FakeServer() as x:
        _setup(g, x)
        _drop_server_from_file(new_url="http://169.254.169.254/v1")
        with pytest.raises(LLMServerUnavailable):
            llm_settings.make_client("official-doc")
        assert llm_settings.server_problem("official-doc")
    assert g.hits == 0 and x.hits == 0


def test_a_tool_helper_fails_loudly_instead_of_using_the_global_server(saved_settings):
    """真的走一支工具的程式：文件去識別化的 LLM 補偵測。"""
    import importlib
    # 套件的 __init__ 把 `router` 這個名字蓋成 APIRouter 物件，要照模組路徑載入
    dd = importlib.import_module("app.tools.doc_deident.router")
    with FakeServer() as g, FakeServer() as x:
        _setup(g, x, tool="doc-deident")
        _drop_server_from_file()
        with pytest.raises(LLMServerUnavailable):
            dd._llm_extra_findings("王小明的電話是 0912-345-678", [])
    assert g.hits == 0


def test_an_unguarded_endpoint_answers_503_not_500(client, saved_settings):
    """沒有自己接住例外的端點（逐句翻譯的同步 API）：全域處理器回 503 ＋ 那句固定訊息。"""
    with FakeServer() as g, FakeServer() as x:
        _setup(g, x, tool="translate-doc")
        _drop_server_from_file()
        r = client.post("/tools/translate-doc/api/translate-doc", json={"text": "Hello world."})
    assert r.status_code == 503, r.text
    assert r.json()["detail"] == LLMServerUnavailable.MESSAGE
    assert g.hits == 0, "失敗時不可以有任何請求跑到全站那一台"


def test_field_review_reports_the_server_problem(saved_settings, tmp_path):
    """表單填寫的逐欄校驗：把例外收成一句錯誤訊息（不是「全部填對了」，也不打全站）。"""
    from app.core import llm_review_per_field as pf
    from app.core.llm_review import FilledField
    with FakeServer() as g, FakeServer() as x:
        _setup(g, x, tool="pdf-fill")
        _drop_server_from_file()
        res = pf.per_field_review(
            tmp_path / "x.pdf",
            [FilledField(page=0, label_text="欄位", profile_key="k", value="v",
                         slot_pt=(50, 50, 300, 80))], page_index=0)
    assert res.errors == [LLMServerUnavailable.MESSAGE], res.errors
    assert g.hits == 0


# ------------------------------------------------------------ 3. 金鑰不外洩

def test_server_keys_are_encrypted_and_never_shown(client, saved_settings):
    with FakeServer() as g, FakeServer() as x:
        _setup(g, x, key_x="sk-x-visible?")
    raw = llm_settings._path.read_text(encoding="utf-8")
    assert "sk-x-visible?" not in raw
    stored = json.loads(raw)["servers"][0]
    assert stored["api_key_enc"] and "api_key" not in stored
    pub = llm_settings.get()
    assert pub["servers"][0]["api_key"] == ls_mod.SECRET_KEPT
    assert all("api_key_enc" not in srv for srv in pub["servers"])
    assert "sk-x-visible?" not in json.dumps(pub, ensure_ascii=False)
    assert "sk-x-visible?" not in json.dumps(llm_settings.servers(), ensure_ascii=False)
    assert "sk-x-visible?" not in client.get("/admin/llm-settings").text
    assert "sk-x-visible?" not in client.get("/admin/api/llm/settings").text
    assert llm_settings.server_api_key(SID_X) == "sk-x-visible?"


def test_kept_keeps_the_server_key_and_empty_clears_it(saved_settings):
    with FakeServer() as g, FakeServer() as x:
        _setup(g, x)
        srv = {"id": SID_X, "name": "機關地端", "base_url": x.base,
               "api_key": ls_mod.SECRET_KEPT, "timeout_seconds": 90}
        llm_settings.update({"servers": [srv]})
        assert llm_settings.server_api_key(SID_X) == "sk-x-secret"
        assert llm_settings.server(SID_X)["timeout_seconds"] == 90
        llm_settings.update({"servers": [dict(srv, api_key="")]})
        assert llm_settings.server_api_key(SID_X) is None


def test_a_new_server_cannot_borrow_a_kept_key(saved_settings):
    """新的一台（編號不在清單裡）送替身字串 → 沒有金鑰，不會拿到別人的。"""
    with FakeServer() as g, FakeServer() as x:
        _setup(g, x)
        llm_settings.update({"servers": [
            {"id": SID_X, "name": "a", "base_url": x.base, "api_key": ls_mod.SECRET_KEPT},
            {"id": SID_Y, "name": "b", "base_url": x.base, "api_key": ls_mod.SECRET_KEPT}]})
    assert llm_settings.server_api_key(SID_Y) is None
    assert llm_settings.server_api_key(SID_X) == "sk-x-secret"


# ------------------------------------------------------------ 4. 測試連線的金鑰規則

def test_a_saved_server_key_only_goes_to_that_servers_saved_address(client, saved_settings):
    with FakeServer() as g, FakeServer() as x, FakeServer() as other:
        _setup(g, x)
        # 位址改成別台（還沒存）→ 不帶 X 的金鑰
        client.post("/admin/api/llm/test-connection",
                    json={"server_id": SID_X, "base_url": other.base,
                          "api_key": ls_mod.SECRET_KEPT})
        # X 的金鑰也不可以送到全站那一台的位址
        client.post("/admin/api/llm/test-connection",
                    json={"server_id": SID_X, "base_url": g.base,
                          "api_key": ls_mod.SECRET_KEPT})
        # 不認得的伺服器編號 → 沒有金鑰
        client.post("/admin/api/llm/test-connection",
                    json={"server_id": SID_Y, "base_url": x.base,
                          "api_key": ls_mod.SECRET_KEPT})
        n_before = len(x.models_auth)
        # 位址跟 X 存檔時一樣 → 帶 X 的金鑰
        r = client.post("/admin/api/llm/test-connection",
                        json={"server_id": SID_X, "base_url": x.base,
                              "api_key": ls_mod.SECRET_KEPT})
        assert r.json()["ok"], r.text
    assert other.models_auth and all(a == "" for a in other.models_auth), other.models_auth
    assert g.models_auth and all(a == "" for a in g.models_auth), g.models_auth
    assert all(a == "" for a in x.models_auth[:n_before]), x.models_auth
    assert x.models_auth[n_before:] == ["Bearer sk-x-secret"], x.models_auth


def test_the_global_key_does_not_go_to_another_servers_address(client, saved_settings):
    with FakeServer() as g, FakeServer() as x:
        _setup(g, x)
        client.post("/admin/api/llm/test-connection",
                    json={"base_url": x.base, "api_key": ls_mod.SECRET_KEPT})
    assert x.models_auth == [""], x.models_auth


def test_models_of_a_saved_server_are_listed_with_its_key(client, saved_settings):
    with FakeServer() as g, FakeServer() as x:
        _setup(g, x)
        j = client.get(f"/admin/api/llm/models?server={SID_X}").json()
        bad = client.get("/admin/api/llm/models?server=../../etc").json()
    assert j["ok"] and j["models"][0]["id"] == f"model-on-{x.port}", j
    assert x.models_auth == ["Bearer sk-x-secret"]
    assert not bad["ok"]
    assert g.hits == 0


# ------------------------------------------------------------ 5. SSRF / 不合格的清單

@pytest.mark.parametrize("url", ["http://169.254.169.254/v1", "file:///etc/passwd",
                                 "ftp://10.0.0.5/v1", "http:///v1", ""])
def test_a_bad_server_address_is_rejected_and_nothing_is_saved(client, saved_settings, url):
    before = llm_settings._path.read_bytes()
    r = client.post("/admin/api/llm/settings", json={
        "servers": [{"id": SID_X, "name": "x", "base_url": url}]})
    assert r.status_code == 400, r.text
    assert r.json()["code"] == "server_url"
    assert r.json()["error"] == ls_mod.SERVER_ERRORS["server_url"]
    assert llm_settings._path.read_bytes() == before, "不合格就整筆不存"


def test_test_connection_blocks_a_metadata_address_for_a_server(client):
    r = client.post("/admin/api/llm/test-connection",
                    json={"server_id": SID_X, "base_url": "http://169.254.169.254/v1"})
    assert r.json()["ok"] is False
    assert "黑名單" in r.json()["error"]


def test_a_tool_pointing_at_a_missing_server_blocks_the_save(client, saved_settings):
    before = llm_settings._path.read_bytes()
    r = client.post("/admin/api/llm/settings", json={
        "servers": [], "server_per_tool": {"official-doc": SID_Y}})
    assert r.status_code == 400, r.text
    j = r.json()
    assert j["code"] == "server_in_use" and j["tools"] == ["official-doc"], j
    assert llm_settings._path.read_bytes() == before


def test_names_must_be_present_and_unique(client, saved_settings):
    r = client.post("/admin/api/llm/settings", json={
        "servers": [{"id": SID_X, "name": " ", "base_url": "http://127.0.0.1:1/v1"}]})
    assert r.json()["code"] == "server_name"
    r = client.post("/admin/api/llm/settings", json={
        "servers": [{"id": SID_X, "name": "A", "base_url": "http://127.0.0.1:1/v1"},
                    {"id": SID_Y, "name": "a", "base_url": "http://127.0.0.1:2/v1"}]})
    assert r.json()["code"] == "server_dup_name"


def test_unknown_tools_and_bad_ids_are_dropped(saved_settings):
    llm_settings.update({
        "servers": [{"id": "not-a-hex-id", "name": "n", "base_url": "http://127.0.0.1:1/v1"}],
        "server_per_tool": {}})
    sid = llm_settings.servers()[0]["id"]
    assert sid != "not-a-hex-id" and len(sid) == 12
    llm_settings.update({"server_per_tool": {"no-such-tool": sid, "official-doc": "x" * 12,
                                             "translate-doc": sid}})
    assert llm_settings.get()["server_per_tool"] == {"translate-doc": sid}


# ------------------------------------------------------------ 6. 稽核紀錄

def _max_audit_id() -> int:
    from app.core import audit_db
    r = audit_db.conn().execute("SELECT COALESCE(MAX(id), 0) AS m FROM audit_events").fetchone()
    return int(r["m"])


def _llm_audit_rows(after_id: int) -> list:
    from app.core import audit_db
    deadline = time.time() + 5
    rows = []
    while time.time() < deadline:
        rows = audit_db.conn().execute(
            "SELECT id, username, details_json FROM audit_events "
            "WHERE event_type='settings_change' AND target='llm' AND id > ? "
            "ORDER BY id DESC", (after_id,)).fetchall()
        if rows:
            return rows
        time.sleep(0.2)
    return rows


def test_saving_writes_an_audit_event_without_any_key(admin_session, saved_settings):
    c, admin, _ = admin_session
    start = _max_audit_id()
    r = c.post("/admin/api/llm/settings", json={
        "api_key": "sk-global-NOLOG-1",
        "servers": [{"id": SID_X, "name": "機關地端", "base_url": "http://127.0.0.1:9/v1",
                     "api_key": "sk-server-NOLOG-2"}],
        "server_per_tool": {"official-doc": SID_X}})
    assert r.status_code == 200, r.text
    rows = _llm_audit_rows(start)
    assert rows, "存了 LLM 設定卻沒有 settings_change 稽核紀錄"
    assert rows[0]["username"] == admin
    raw = rows[0]["details_json"]
    assert "NOLOG" not in raw, "金鑰寫進稽核紀錄了"
    d = json.loads(raw)
    assert d["api_key_status"] == "已更新"
    assert d["server_per_tool"] == {"official-doc": SID_X}
    assert d["servers"][0]["api_key_status"] == "已更新"
    assert d["servers"][0]["base_url"] == "http://127.0.0.1:9/v1"
    # 再存一次、金鑰不動 → 「不變」；把伺服器刪掉 → 記下刪了哪一台
    start = _max_audit_id()
    c.post("/admin/api/llm/settings", json={
        "servers": [{"id": SID_X, "name": "機關地端", "base_url": "http://127.0.0.1:9/v1",
                     "api_key": ls_mod.SECRET_KEPT}]})
    d = json.loads(_llm_audit_rows(start)[0]["details_json"])
    assert d["servers"][0]["api_key_status"] == "不變"
    start = _max_audit_id()
    c.post("/admin/api/llm/settings", json={"servers": [], "server_per_tool": {}})
    d = json.loads(_llm_audit_rows(start)[0]["details_json"])
    assert d["servers_removed"] == [{"id": SID_X, "name": "機關地端"}]


# ------------------------------------------------------------ 7. 設定匯出 / 匯入

def test_export_and_import_rekey_every_server_key(saved_settings, tmp_path):
    from app.core import settings_export as se
    llm_settings.update({
        "api_key": "sk-global-exp",
        "servers": [{"id": SID_X, "name": "x", "base_url": "http://127.0.0.1:1/v1",
                     "api_key": "sk-x-exp"},
                    {"id": SID_Y, "name": "y", "base_url": "http://127.0.0.1:2/v1",
                     "api_key": ""}]})
    blob = se._rekey_export_blob("llm_settings.json")
    # 備份檔裡是明文（換一台機器才解得開；該分類標記為含祕密，同全站那一把）
    assert blob and "sk-x-exp" in blob
    target = tmp_path / "llm_settings.json"
    target.write_text(blob, encoding="utf-8")
    se._rekey_after_import(target)
    again = target.read_text(encoding="utf-8")
    assert "sk-x-exp" not in again and "sk-global-exp" not in again
    data = json.loads(again)
    assert ls_mod.decrypt_secret(data["servers"][0]["api_key_enc"]) == "sk-x-exp"
    assert data["servers"][1]["api_key_enc"] == ""
    assert ls_mod.decrypt_secret(data["api_key_enc"]) == "sk-global-exp"
