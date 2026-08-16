"""錯誤訊息不可以把使用者送的字串原樣吐回去。

## 由來

外部資安掃描（2026-08-13）把 FastAPI 預設 422 的 `"input": "<使用者原文>"`
判成反射型 XSS（High）。v1.14.29 拿掉了那個欄位 —— **但只修了一半**。

v1.14.31 的對抗式驗證用同一個 payload 重打一次，發現 `HTTPException.detail`
這條路原封不動：全站有 38 處把使用者送的檔名回填進錯誤訊息
（`f"只支援 PDF：{f.filename}"` 這種形狀），散在 20 個檔案。實測 7 個上傳端點
裡 6 個把 `<script>alert(1)</script>.txt` 一字不差地吐回來，載體、`nosniff`、
CSP 全部跟被判 High 的那個 422 一模一樣。**反射面沒有消失，只是換了一個
JSON 鍵名。**

## 為什麼不逐一改呼叫端

38 處分散在 20 個檔案，逐一改一定會漏，而且下次有人新寫一支工具又會長回來。
改在出口（`app/main.py` 的 HTTPException handler）一處處理，現有與未來的
回填一起擋掉。

## 判準

**不可利用不等於應該回填**。使用者需要知道的是「哪個檔案不合格」，
不需要把他送的位元組原樣拿回去。檔名的可辨識性要保留（上傳多個檔時要分得
出是哪一個），只把 HTML 有意義的字元與控制字元換掉。
"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("JTDT_CSRF_DISABLE", "1")

#: 掃描器會盯的形狀，外加控制字元 / 換行（標頭注入與日誌污染的來源）
PAYLOADS = [
    pytest.param('<script>alert(1)</script>.txt', id="script標籤"),
    pytest.param('"><img src=x onerror=alert(1)>.pdf', id="屬性跳脫"),
    pytest.param("<svg onload=alert(1)>.pdf", id="svg"),
    pytest.param("a'b\"c`d.pdf", id="引號與反引號"),
    pytest.param("a\r\nSet-Cookie: x=1.pdf", id="換行"),
]

#: 會走「檔案不合格」那條路的端點（送非 PDF 內容就會被擋）
UPLOAD_ENDPOINTS = [
    ("/tools/pdf-pages/submit", {"mode": "keep", "spec": "1"}),
    ("/tools/pdf-encrypt/submit", {"user_pw": "x"}),
    ("/tools/pdf-split/submit", {"mode": "every", "n": "1"}),
    ("/tools/pdf-pageno/submit", {}),
    ("/tools/pdf-extract-images/submit", {}),
    ("/tools/office-to-pdf/submit", {}),
]


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient

    from app.main import app
    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize("path,data", UPLOAD_ENDPOINTS)
@pytest.mark.parametrize("payload", PAYLOADS)
def test_filename_is_not_reflected_verbatim(client, auth_off, path, data, payload):
    r = client.post(path, files={"file": (payload, b"", "text/plain")}, data=data)
    # **沒打到端點就通過 = 假的通過**。認證開著時會被導到登入頁，那份 HTML
    # 自己就含 `<img` 之類的字串，寬鬆的斷言會在那裡誤報，嚴格的斷言則會
    # 在「其實根本沒測到」的情況下變綠。兩種都不行，所以先確認真的走到了。
    assert r.status_code == 400, (
        f"{path} 回了 {r.status_code} —— 沒走到「檔案不合格」那條路，這個測試沒測到東西")

    # **要看 `detail` 的值，不是整份原始文字**。JSON 本身就長成
    # `{"detail":"..."}`，拿整份去找 `"` 一定找得到 —— 那是語法不是反射。
    detail = str(r.json().get("detail", ""))

    # **只對危險字元下判準**。`alert(1)` 留在訊息裡是無害的純文字 ——
    # 讓它變成可執行程式碼的是 `<` `>` `"` `'` 這些字元，被換掉之後
    # 那串東西在任何脈絡下都只是字。把無害的片段也列進來只會製造誤報。
    for bad in '<>"\'&`\r\n':
        if bad in payload:
            assert bad not in detail, (
                f"{path} 把 {bad!r} 原樣吐回來了：{detail[:200]}")
    assert payload not in detail, f"{path} 把整個檔名原樣吐回來了"


def test_scrub_keeps_the_message_readable():
    """洗掉危險字元之後，訊息仍然要看得出是在講哪個檔案。

    使用者一次上傳多個檔時，「哪一個不合格」是他唯一需要的資訊。
    洗成一片空白等於把錯誤訊息廢掉。
    """
    from app.main import scrub_error_detail

    got = scrub_error_detail("只支援 PDF：報價單<script>.txt")
    assert "只支援 PDF" in got
    assert "報價單" in got and ".txt" in got, "檔名的可辨識部分不可以一起洗掉"
    assert "<script" not in got


def test_scrub_handles_nested_details():
    """`detail` 不一定是字串 —— 有些端點回 dict / list。"""
    from app.main import scrub_error_detail

    got = scrub_error_detail({"errors": [{"file": "a<script>.pdf", "why": "空檔"}]})
    assert "<script" not in str(got)
    assert "空檔" in str(got)


def test_validation_errors_do_not_echo_input(client):
    """422 那一半（v1.14.29 修的）不可以退化回去。"""
    r = client.get("/api/jobs/1?limit=<script>alert(1)</script>")
    assert "<script" not in r.text
    assert '"input"' not in r.text


#: 全站合法訊息裡本來就有這些字元 —— 清洗不可以把它們毀掉。
LEGITIMATE_MESSAGES = [
    "image > 50MB",
    "import file too large (>200 MB)",
    "engine must be 'easyocr' or 'tesseract'",
    "quality must be 'fast' or 'best'",
    "cols / rows 不合理（1 <= cols*rows <= 64）",
    "JSON 缺少 'fields' 欄位或格式錯誤",
]


@pytest.mark.parametrize("msg", LEGITIMATE_MESSAGES)
def test_scrub_keeps_legitimate_messages_readable(msg):
    """清洗**不可以把正常訊息毀掉**。

    第一版把危險字元全部換成底線，結果「image > 50MB」變成「image ＿ 50MB」、
    「1 <= cols*rows <= 64」變成「1 ＿= cols*rows ＿= 64」—— 為了防禦把訊息
    廢掉。全站有 14 句訊息本身就含這些字元。

    改成全形對應字元後同時滿足兩件事：讀起來一模一樣，而且在 HTML 裡完全
    無害（`＜` 不會開一個標籤、`＂` 不會跳脫屬性）。
    """
    from app.main import scrub_error_detail

    got = scrub_error_detail(msg)
    # 訊息長度不可以縮水（底線那版會，全形版不會）
    assert len(got) == len(msg), f"訊息被改變了長度：{got!r}"
    # 語意上的字元要還在（用全形對應，不是被抹掉）
    for ch, full in (("<", "＜"), (">", "＞"), ("'", "＇"), ('"', "＂")):
        assert got.count(full) == msg.count(ch), (
            f"{ch!r} 沒有被換成全形對應字元：{got!r}")
    # 危險的原字元不可以留下
    for ch in "<>\"'&`":
        assert ch not in got
