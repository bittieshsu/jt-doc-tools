"""任何工具端點收到壞輸入都不可以回 500。

## 由來（2026-08-16 全端點壞輸入掃描）

拿一份毀損的 PDF（`%PDF-1.4 broken`）打全部工具的 POST 端點，**28 個端點
回 500**。根因都一樣：`fitz.open()` 炸出 `FileDataError` 沒人接，一路冒成
「Internal Server Error」。

500 的問題不只是難看：使用者會以為**服務掛了**而一直重試（其實是他的檔案
壞了，重試一萬次都一樣），管理員則會在監控上看到一堆假警報。毀損的檔案是
客戶端錯誤，該回 400 與可行動的訊息。

修法是 `app/main.py` 的全域 `fitz.FileDataError` 處理器 —— 跟既有的
`JSONDecodeError` 處理器同一個做法，一個涵蓋全部呼叫點，不必逐一改 router。

## 為什麼掃全部端點而不是挑幾個

原本就是「每支工具自己處理」的世界觀才會漏掉 28 個。逐支列舉的測試，
下一支新工具又會漏 —— 這裡直接從路由表撈，新工具自動被涵蓋。
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


def _tool_post_paths():
    from app.main import app
    return sorted({r.path for r in app.routes
                   if r.path.startswith("/tools/")
                   and "POST" in getattr(r, "methods", set())
                   and "{" not in r.path})


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient
    from app.main import app
    # raise_server_exceptions=False：要看到「回應是什麼」，不是讓例外直接炸測試
    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize("path", _tool_post_paths())
def test_broken_pdf_never_500(client, path):
    """毀損的 PDF → 4xx（或 LLM 未啟用的 503），絕不可 500。"""
    r = client.post(path, files=[("file", ("a.pdf", b"%PDF-1.4 broken",
                                           "application/pdf"))])
    assert r.status_code < 500 or r.status_code == 503, (
        f"{path} 對毀損 PDF 回 {r.status_code} —— 使用者會以為服務掛了而"
        f"一直重試；該回 400。回應開頭：{r.text[:120]}")


@pytest.mark.parametrize("path", _tool_post_paths())
def test_empty_body_never_500(client, path):
    """完全沒帶東西 → 422 / 400，絕不可 500。"""
    r = client.post(path)
    assert r.status_code < 500 or r.status_code == 503, (
        f"{path} 對空請求回 {r.status_code}：{r.text[:120]}")


def test_broken_pdf_message_is_actionable(client):
    """400 的訊息要講「檔案壞了、請確認後重傳」，不是丟英文類別名。"""
    r = client.post("/tools/pdf-pages/load",
                    files=[("file", ("a.pdf", b"%PDF-1.4 broken",
                                     "application/pdf"))])
    assert r.status_code == 400, r.status_code
    detail = r.json().get("detail", "")
    assert "毀損" in detail or "打不開" in detail, detail
