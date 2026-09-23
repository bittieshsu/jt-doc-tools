"""照 `github/API.md` 的 curl 範例實際呼叫 —— 抓「照文件呼叫卻壞」。

## 為什麼要有這一份

既有兩支檢查管的是**對照層**：
- `test_api_doc_coverage.py`：程式有的端點文件有沒有寫（雙向）。
- `test_api_doc_contract.py`：幾支挑出來的工具，參數名 / 值對不對。

它們都看不到「文件寫的範例整條送出去會怎樣」。v1.15.34 把 API.md 裡
**82 條 curl 全部解析出來實際打一遍**，抓到的就是這一類：

- `/admin/api/llm/test-connection` 的範例**沒有帶 body**（文件也沒有參數表），
  而端點 `await request.json()` 對空 body 丟例外 → 全域處理器回
  `400 Invalid JSON body`。照文件做的人看到的是「你送錯東西」，
  其實是我們沒寫參數。

完整的 82 條逐條實跑放在 `temp/api-audit/`（要 soffice / OCR，跑一次約
40 秒，不適合每次 pytest 都跑）。這裡把**已經抓到的那幾條**釘死，
避免修好又壞掉。
"""
from __future__ import annotations


def test_llm_test_connection_accepts_an_empty_body(admin_session):
    """文件範例只有 `-X POST`，沒有 body —— 不可以回 400。

    判準是「**不是 400/500**，而且回的是一個判斷結果」。不驗 `ok` 為真：
    這台機器不一定連得到 LLM，那是環境不是契約。
    """
    c, _u, _p = admin_session
    r = c.post("/admin/api/llm/test-connection")
    assert r.status_code == 200, (
        f"照文件呼叫（不帶 body）回了 {r.status_code}：{r.text[:200]}")
    body = r.json()
    assert "ok" in body, f"回的不是連線判斷結果：{body}"


def test_llm_test_connection_still_tests_unsaved_settings(admin_session):
    """管理頁那顆按鈕送的是**還沒存檔**的設定 —— 退回存檔值不可以把它蓋掉。"""
    c, _u, _p = admin_session
    r = c.post("/admin/api/llm/test-connection",
               json={"base_url": "not-a-url"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("ok") is False
    assert "Base URL" in (body.get("error") or ""), body


def test_llm_test_connection_reports_missing_base_url(admin_session, monkeypatch):
    """存檔值也是空的時候要說「Base URL 未填」，不是丟例外。"""
    from app.core.llm_settings import llm_settings
    monkeypatch.setattr(llm_settings, "get", lambda: {})
    c, _u, _p = admin_session
    r = c.post("/admin/api/llm/test-connection")
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": False, "error": "Base URL 未填"}


def test_a_documented_example_still_calls_it_without_a_body():
    """**檢查要跟著文件走。**

    上面那幾條守的是「文件裡那個不帶 body 的範例要能用」。如果哪天文件只剩
    帶 body 的版本，那幾條就是在守一個沒有人照著做的形狀 —— 所以這裡確認
    文件裡**至少還有一條**不帶 body 的呼叫。
    """
    import pathlib
    import re
    import sys

    root = pathlib.Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(root))
    from tools.repo_paths import public_root

    md = (public_root(root) / "API.md").read_text(encoding="utf-8")
    # 把續行接起來之後，逐條看打到這個端點的 curl
    joined, cur = [], ""
    for line in md.splitlines():
        line = line.rstrip()
        if line.endswith("\\"):
            cur += line[:-1] + " "
            continue
        cur += line
        joined.append(cur)
        cur = ""
    calls = [c for c in joined
             if "curl" in c and "/admin/api/llm/test-connection" in c]
    assert calls, "API.md 裡找不到打 test-connection 的 curl 範例"
    bodyless = [c for c in calls
                if not re.search(r"\s(-d|--data|--data-raw|--data-binary)\s", c)]
    assert bodyless, (
        "API.md 已經沒有「不帶 body」的範例了 —— 那上面幾條測試守的形狀"
        "就該跟著改（或確認那個用法還要不要支援）。實際找到的呼叫："
        + " | ".join(c[:100] for c in calls))
