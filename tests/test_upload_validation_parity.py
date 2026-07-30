"""上傳的檔案不是 PDF 時要回 400，不是 500。

## 這一輪怎麼發現的

在測「檔名含 HTML 會不會變成 XSS」時順手送了一個內容不是 PDF 的檔案，四個工具
全部回 **500 Internal Server Error** —— PyMuPDF 開檔失敗的例外沒有人接。

看程式碼才發現同一支工具的**兩個入口只有一個有驗**：

    router.py:576   @router.post("/api/pdf-compress")   → `if data[:4] != b"%PDF": 400`
    router.py:403   @router.post("/analyze")            → 直接開檔

對外的 API 有驗、網頁介面沒有。跟先前抓到的「預覽端點有驗、報告端點沒驗」是同一
個形狀：**同一支 router 裡相鄰的入口，只改了其中一個**。

## 為什麼 500 不能算「反正也擋下來了」

* 使用者看到的是「Internal Server Error」，不知道該怎麼辦（正確訊息是「這不是
  PDF」）。前端把回應內容直接顯示出來，所以畫面上就是那句英文。
* 500 會進錯誤記錄並觸發告警，把真正的問題淹掉。
* 未攔截的例外走的是框架的預設路徑 —— 那條路徑的行為（會不會帶出堆疊、路徑）
  取決於部署時的設定，不該由運氣決定。
"""
from __future__ import annotations

import io

import fitz
import pytest
from fastapi.testclient import TestClient

import app.main as app_main


#: (工具, 網頁介面端點, 對外 API 端點)
CASES = (
    ("pdf-compress", "/tools/pdf-compress/analyze", "/tools/pdf-compress/api/pdf-compress"),
    ("pdf-attachments", "/tools/pdf-attachments/scan", "/tools/pdf-attachments/api/pdf-attachments"),
    ("pdf-metadata", "/tools/pdf-metadata/analyze", "/tools/pdf-metadata/api/pdf-metadata"),
    ("pdf-hidden-scan", "/tools/pdf-hidden-scan/scan", "/tools/pdf-hidden-scan/api/pdf-hidden-scan"),
)

NOT_A_PDF = b"this is definitely not a pdf"


@pytest.fixture
def c():
    return TestClient(app_main.app, raise_server_exceptions=False)


def _pdf() -> bytes:
    d = fitz.open()
    d.new_page()
    buf = io.BytesIO()
    d.save(buf)
    d.close()
    return buf.getvalue()


@pytest.mark.parametrize("tool,web_ep,_api", CASES)
def test_web_endpoint_rejects_non_pdf_with_400(c, tool, web_ep, _api):
    r = c.post(web_ep, files={"file": ("x.pdf", NOT_A_PDF, "application/pdf")})
    assert r.status_code == 400, (
        f"{tool} 的網頁介面回了 {r.status_code}（應為 400）：{r.text[:120]}")


@pytest.mark.parametrize("tool,web_ep,_api", CASES)
def test_error_message_is_actionable(c, tool, web_ep, _api):
    """訊息要讓使用者知道該怎麼辦 —— 不可以只是 Internal Server Error。"""
    r = c.post(web_ep, files={"file": ("x.pdf", NOT_A_PDF, "application/pdf")})
    assert "PDF" in r.text, f"{tool} 的訊息看不出問題在哪：{r.text[:120]}"
    assert "Internal Server Error" not in r.text


@pytest.mark.parametrize("tool,web_ep,_api", CASES)
def test_empty_upload_also_rejected(c, tool, web_ep, _api):
    r = c.post(web_ep, files={"file": ("x.pdf", b"", "application/pdf")})
    assert r.status_code == 400, f"{tool} 空檔回了 {r.status_code}"


@pytest.mark.parametrize("tool,web_ep,_api", CASES)
def test_valid_pdf_still_works(c, tool, web_ep, _api):
    """加了檢查不可以擋掉正常的檔案。"""
    r = c.post(web_ep, files={"file": ("ok.pdf", _pdf(), "application/pdf")})
    assert r.status_code == 200, f"{tool} 正常 PDF 被擋：{r.status_code} {r.text[:120]}"


@pytest.mark.parametrize("tool,web_ep,api_ep", CASES)
def test_both_entry_points_agree(c, tool, web_ep, api_ep):
    """網頁介面與對外 API 對同一份壞檔的判定要一致。

    這正是問題的根源：只改了其中一個入口。
    """
    a = c.post(web_ep, files={"file": ("x.pdf", NOT_A_PDF, "application/pdf")})
    b = c.post(api_ep, files={"file": ("x.pdf", NOT_A_PDF, "application/pdf")})
    if b.status_code in (401, 403, 404):
        pytest.skip("對外 API 需要 token / 路徑不同")
    assert a.status_code == b.status_code, (
        f"{tool} 兩個入口不一致：網頁 {a.status_code} vs API {b.status_code}")


# ---------- 伺服器回應不可以被當成 HTML 塞進畫面 ----------

def test_no_template_injects_raw_server_text_into_innerhtml():
    """`innerHTML = '...' + await r.text()` 這種寫法一律不准。

    伺服器的回應裡有使用者取的檔名（實測：四個工具的分析結果都會回檔名）。
    今天那些錯誤路徑回的是固定字串，所以還不能利用 —— 但只要哪天有人在錯誤訊息
    裡帶上檔名，這一行就直接變成 XSS。用 `textContent` 就沒有這個問題，顯示效果
    也一樣。

    專案本來就有這條規則（CodeQL「DOM text reinterpreted as HTML」，v1.12.52
    踩過一次），這裡把它變成會擋下來的測試。
    """
    import pathlib
    import re
    bad = []
    pat = re.compile(r"innerHTML\s*=[^;\n]*(await\s+\w+\.text\(\)|\.message\b|\bmsg\b)")
    safe = re.compile(r"escapeHtml|escapeHTML|\besc\(")
    #: 例外：這個 helper 內部已經對 title / detail 做 escapeHTML，
    #: 只有寫死的 hint 是刻意保留 HTML（裡面是我們自己的 <a> 連結）。
    exempt = re.compile(r"llmErrorCard\(")
    for f in list(pathlib.Path("app").rglob("*.html")) + list(
            pathlib.Path("static").rglob("*.js")):
        for i, line in enumerate(f.read_text(encoding="utf-8").split("\n"), 1):
            if pat.search(line) and not safe.search(line) \
                    and not exempt.search(line):
                bad.append(f"{f}:{i}  {line.strip()[:80]}")
    assert not bad, (
        "這些地方把伺服器回應 / 例外訊息當成 HTML 塞進 DOM：\n  "
        + "\n  ".join(bad) + "\n（改用 textContent，或先過 escapeHtml）")
