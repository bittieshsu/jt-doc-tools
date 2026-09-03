"""文件翻譯：產出**同格式、同版面**的檔案。

這支工具的賣點就是「排版不會跑掉」，所以驗收一路走到產出檔本身：
翻完之後**用同一支抽取器再讀一次**，確認每一段都換成了譯文，而且檔案
還打得開（能轉成 PDF）。只驗端點回 200 沒有意義。
"""
from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from app.core import office_text_map as M


# ---------- 核心：抽出 / 寫回 ----------

def _minimal_docx() -> bytes:
    """手工做一份最小的 .docx（不依賴 soffice，測試才能離線跑）。"""
    doc = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body>"
        "<w:p><w:r><w:t>Quarterly </w:t></w:r><w:r><w:t>Report</w:t></w:r></w:p>"
        "<w:p><w:r><w:t>Revenue grew 12%.</w:t></w:r></w:p>"
        "<w:p><w:r><w:t>2026-09-02</w:t></w:r></w:p>"
        "</w:body></w:document>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", doc)
    return buf.getvalue()


def test_paragraph_is_one_unit_not_one_per_run():
    """一段話被切成好幾個 run 時要**合起來**當一段翻。

    逐個 run 翻等於把半句話丟給模型（「Quarterly 」+「Report」），
    譯文一定走樣。
    """
    units, _ = M.extract_units(_minimal_docx(), ".docx")
    texts = [u.text for u in units]
    assert "Quarterly Report" in texts, f"run 沒有合併：{texts}"


def test_pure_number_or_date_is_skipped():
    units, _ = M.extract_units(_minimal_docx(), ".docx")
    assert all("2026-09-02" != u.text for u in units), "純日期不該送去翻"


def test_rebuild_replaces_text_and_keeps_the_package():
    data = _minimal_docx()
    units, state = M.extract_units(data, ".docx")
    out = M.rebuild(state, {i: f"[{u.text}]" for i, u in enumerate(units)}, units)

    again, _ = M.extract_units(out, ".docx")
    assert [u.text for u in again] == [f"[{u.text}]" for u in units]

    # zip 的其他成員要原封不動 —— 少一個檔案 Word 就開不起來
    with zipfile.ZipFile(io.BytesIO(data)) as a, zipfile.ZipFile(io.BytesIO(out)) as b:
        assert set(a.namelist()) == set(b.namelist())


@pytest.mark.parametrize("text,want", [
    ("Hello world", True),
    ("報告內容", True),
    ("12,345.67", False),
    ("2026-09-02", False),
    ("https://example.com", False),
    ("<number>", False),        # 頁碼欄位的佔位字
    ("   ", False),
    ("——", False),
])
def test_should_translate(text: str, want: bool):
    assert M.should_translate(text) is want


def test_supported_extensions_cover_the_three_families():
    for ext in (".doc", ".docx", ".odt", ".xls", ".xlsx", ".ods",
                ".ppt", ".pptx", ".odp"):
        assert M.is_supported("x" + ext), ext
    assert not M.is_supported("x.pdf"), "PDF 不該被收 —— 版面一定跑掉"


# ---------- 端點 ----------

def test_pdf_is_rejected_with_the_reason(client, sample_pdf):
    r = client.post("/tools/doc-translate/upload",
                    files={"file": ("a.pdf", sample_pdf, "application/pdf")})
    assert r.status_code == 400
    assert "PDF" in r.text and "逐句翻譯" in r.text, \
        f"擋下來了但沒說原因與替代方案：{r.text[:200]}"


def test_upload_reports_units(client):
    r = client.post("/tools/doc-translate/upload",
                    files={"file": ("a.docx", _minimal_docx(),
                                    "application/vnd.openxmlformats-officedocument"
                                    ".wordprocessingml.document")})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["units"] == 2 and d["upload_id"]


def test_start_without_llm_is_rejected(client):
    up = client.post("/tools/doc-translate/upload",
                     files={"file": ("a.docx", _minimal_docx(), "application/octet-stream")})
    assert up.status_code == 200
    r = client.post("/tools/doc-translate/start",
                    json={"upload_id": up.json()["upload_id"], "target_lang": "en"})
    # 測試環境沒有 LLM → 要明確回 503，不可以 500
    assert r.status_code == 503, r.status_code


def test_tool_page_loads(client):
    r = client.get("/tools/doc-translate/")
    assert r.status_code == 200
    assert "文件翻譯" in r.text
    assert "不支援 PDF" in r.text, "頁面要講清楚為什麼不收 PDF"


# ---------- 端到端：整個作業真的跑一次 ----------

def test_job_produces_a_translated_file(tmp_path, monkeypatch):
    """**走完整條路徑**：假的 LLM → 產出檔裡每一段都換成了譯文。

    這條測試的由來：第一版把 `_translate_one` 的回傳當成字串用（它其實回
    dict），結果作業跑到最後才炸 `'dict' object has no attribute 'strip'`，
    而使用者只看到「失敗」兩個字。單元層級的測試看不出來 ——
    只有真的把作業跑完、打開產出檔才會發現。
    """
    import importlib
    R = importlib.import_module("app.tools.doc_translate.router")

    class _FakeClient:
        def text_query(self, prompt: str, **kw) -> str:
            # prompt 最後一行是「原文：…」
            src = prompt.rsplit("原文：", 1)[-1].strip()
            return f"<{src}>"

    monkeypatch.setattr(R.llm_settings, "is_enabled", lambda: True)
    monkeypatch.setattr(R.llm_settings, "make_client", lambda: _FakeClient())
    monkeypatch.setattr(R.llm_settings, "get_model_for", lambda _t: "fake-model")
    monkeypatch.setattr(R.llm_settings, "get", lambda: {"translate_concurrency": 2})
    monkeypatch.setattr(R, "_warmup_llm", lambda *a, **k: None)
    # 預覽要起 soffice，這裡不是重點（另有 §0.5 的實檔驗收）
    monkeypatch.setattr(R, "_make_preview", lambda *a, **k: 0)

    upload_id = "b" * 32
    R._src_path(upload_id).write_bytes(_minimal_docx())
    R._meta_path(upload_id).write_text(
        '{"filename": "a.docx", "ext": ".docx", "work_ext": ".docx",'
        ' "units": 2, "chars": 30}', encoding="utf-8")

    class _Job:
        progress = 0.0
        message = ""
        cancelled = False
        meta: dict = {}

    job = _Job()
    job.meta = {}
    R._run_job(job, upload_id, {"filename": "a.docx", "ext": ".docx",
                                "work_ext": ".docx"},
               "en", "zh-TW", "")

    out = R._out_path(upload_id, ".docx")
    assert out.exists(), "作業跑完卻沒有產出檔"
    units, _ = M.extract_units(out.read_bytes(), ".docx")
    texts = [u.text for u in units]
    assert texts == ["<Quarterly Report>", "<Revenue grew 12%.>"], texts
    assert job.meta["download_url"].endswith(upload_id)
    assert job.meta["translated"] == 2
    # **「我的作業」的下載鈕看的是 `result_path`**，不是 meta 裡的網址。
    # 少了它，那一列會顯示「已完成」卻沒有任何東西可以下載（實際踩過）。
    assert job.result_path == out, "沒有把產出設成作業結果"
    assert job.result_filename.endswith(".docx")
