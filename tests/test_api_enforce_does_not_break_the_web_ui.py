"""「API token 強制檢查」不可以把網頁自己的 `/api/` 擋掉（GitHub issue #52）。

**這是「介面承諾了後端沒做到的事」那一類。** 設定頁自己寫著：

> 開啟：沒帶有效 token 的 `/api/*` 呼叫一律拒絕 401；
> **web UI 本身仍照常運作（不走 token）**。

而程式做不到：`_api_token_gate` 原本只放行一份**手列的**「雙重存取路徑」
（`/admin/**` 與 pdf-to-office 那兩支預覽），於是強制檢查一開，
網頁自己打的其他 `/api/` 全部 401 —— 進度輪詢、取消作業、通知、收件匣、
工作區清單。頁面本身還開得起來、**作業也真的在背景跑完了**，
所以症狀是「畫面卡住」，看起來像作業系統壞了，不像一個設定問題。

**四條判準（缺一條這支守門就沒有牙齒）**：
①帶 session → 網頁的 `/api/` 要通
②什麼都沒帶 → 照樣 401（不然等於把強制檢查關掉）
③帶錯的 token → 照樣 401
④**逐一走過網頁真的會打的那幾支** —— 手列清單漏掉的正是這些
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import main as app_main

#: 網頁自己會打的 `/api/`。**這份清單不是拿來當白名單的**（修法正是
#: 「不要維護白名單」），是拿來證明「這幾支在強制檢查開啟時仍然通得過」。
#: 少列幾支不會讓守門變鬆，但列進來的每一支都真的被網頁用著。
WEB_UI_API_PATHS = [
    "/api/my/notify",                       # 通知輪詢（每一頁都會打）
    "/api/my/inbox",                        # 收件匣
    "/workspace/api/list",                  # 工作區清單
    "/workspace/api/count",                 # 側欄的工作區計數
    "/admin/jobs/api/list",                 # 管理區（原本就在手列清單裡）
    "/tools/einvoice-scan/api/backend-status",
]


@pytest.fixture
def enforced(admin_session):
    """強制檢查開啟 ＋ 一個已登入的瀏覽器 session。"""
    from app.core.api_tokens import api_tokens
    client, _, _ = admin_session
    before = api_tokens.is_enforced()
    api_tokens.set_enforce(True)
    try:
        yield client
    finally:
        api_tokens.set_enforce(before)


@pytest.mark.parametrize("path", WEB_UI_API_PATHS)
def test_the_web_ui_still_works_when_enforcement_is_on(enforced, path):
    r = enforced.get(path)
    assert r.status_code != 401, (
        f"{path} 在強制檢查開啟時被擋掉了 —— 而設定頁寫著「web UI 本身仍照常運作」。"
        f"回應：{r.text[:200]}")


def test_a_caller_with_no_credentials_is_still_rejected(enforced):
    """反向對照：只驗「網頁通得過」的話，把整個 gate 拿掉也會全綠。"""
    bare = TestClient(app_main.app)          # 沒有 cookie、沒有 token，就是 curl
    for path in ("/api/my/notify", "/workspace/api/list"):
        assert bare.get(path).status_code == 401, f"{path} 應該擋下沒有憑證的呼叫"


def test_a_wrong_token_is_still_rejected(enforced):
    bare = TestClient(app_main.app)
    r = bare.get("/api/my/notify", headers={"Authorization": "Bearer not-a-real-token"})
    assert r.status_code == 401


def test_pages_were_never_affected(enforced):
    """頁面本來就是好的 —— 釘住它，免得修這個 bug 時把頁面弄壞。"""
    for path in ("/", "/my-jobs"):
        assert enforced.get(path).status_code == 200


def test_the_fix_is_not_a_hand_maintained_list():
    """**判準是「有沒有 session」，不是「路徑在不在清單裡」。**

    手列清單一定會漏下一支 —— 這個 bug 就是這樣來的
    （當初為了 `/admin/**` 加了清單，其餘 `/api/` 就全壞了）。
    """
    import inspect
    src = inspect.getsource(app_main._api_token_gate)
    body = src[src.index("has_bearer_attempt = bool(presented)"):]
    assert "sessions" in body and "lookup" in body, \
        "沒帶 token 時要去查 session，不是查一份路徑清單"
