"""拿**真實的**樣本檔掃過所有吃單一 PDF 的工具。

`temp_pdfs/` 有 29 份真實廠商表單 + 6 份 Office 檔，但一直只有「表單自動填寫
回歸」在用。其餘工具的測試全部用測試裡臨時產生的合成 PDF —— 而真正的意外
都在真實檔案裡：壞掉的文字對應表、Wingdings 核取方塊、直書、掃描件、
奇怪的表格版型、旋轉頁、缺字型…（2026-08-26～27 這兩天連續踩到三種）。

這支不驗「處理得對不對」（那要逐工具的功能驗收），只驗**不可以炸掉**：
每一支工具對每一份真實樣本都不能回 5xx、不能拋例外。這是最便宜也最有效的
一層網 —— 實測 29 份 × 22 支不到 20 秒。

> **樣本含客戶資料**：這支測試只看狀態碼與結構，**不印內容也不寫出任何檔案**。
> 失敗訊息只會出現檔名（檔名本身已經被 `test_no_sample_names_in_public.py`
> 擋在公開檔案之外，不會外流）。
"""
from __future__ import annotations

import glob
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent

#: 只丟一份 PDF 就跑得動的工具（其餘需要額外參數，交給各自的功能測試）。
#: 清單是實測出來的，不是猜的 —— 少列一支只是少驗一支，不會誤判。
SINGLE_PDF_TOOLS = [
    "doc-deident", "pdf-annotations", "pdf-annotations-flatten",
    "pdf-annotations-strip", "pdf-border", "pdf-compress", "pdf-decrypt",
    "pdf-extract-text", "pdf-fill", "pdf-hidden-scan", "pdf-metadata",
    "pdf-nup", "pdf-page-size", "pdf-pageno", "pdf-pages", "pdf-rotate",
    "pdf-split", "pdf-to-markdown", "pdf-wordcount",
]


def _samples() -> list[pathlib.Path]:
    return [pathlib.Path(p) for p in sorted(glob.glob(str(ROOT / "temp_pdfs" / "*.pdf")))]


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    import os
    os.environ["JTDT_DATA_DIR"] = str(tmp_path_factory.mktemp("smoke"))
    os.environ["JTDT_CSRF_DISABLE"] = "1"
    from fastapi.testclient import TestClient
    import app.main as app_main
    return TestClient(app_main.app)


def test_samples_exist():
    """樣本不在就要說出來，不可以安靜跳過整份測試。

    這台機器沒有樣本（例如 CI）時 skip 是合理的，但要看得到 —— 少跑一項
    比跑出紅字危險，因為報告看起來仍然是綠的。
    """
    samples = _samples()
    if not samples:
        pytest.skip("這台機器沒有 temp_pdfs 樣本 —— 這一層網等於沒跑")
    assert len(samples) >= 10, f"樣本只剩 {len(samples)} 份，是不是被誤刪了？"


@pytest.mark.parametrize("tool_id", SINGLE_PDF_TOOLS)
def test_tool_survives_every_real_sample(client, tool_id):
    samples = _samples()
    if not samples:
        pytest.skip("這台機器沒有 temp_pdfs 樣本")
    url = f"/tools/{tool_id}/api/{tool_id}"
    failures = []
    for path in samples:
        try:
            r = client.post(url, files={"file": (path.name, path.read_bytes(),
                                                "application/pdf")})
        except Exception as exc:                      # noqa: BLE001
            failures.append(f"{path.name} → 例外 {type(exc).__name__}: {str(exc)[:80]}")
            continue
        if r.status_code >= 500:
            failures.append(f"{path.name} → HTTP {r.status_code}")
    assert not failures, (
        f"{tool_id} 對真實樣本壞掉了（{len(failures)}/{len(samples)} 份）：\n  "
        + "\n  ".join(failures[:6]))


def test_extraction_tools_actually_get_text(client):
    """抽文字類的工具對真實樣本要真的抽到東西。

    只驗「不回 5xx」不夠 —— 回 200 但內容全空、或是一整片圓點，使用者看到的
    仍然是壞的（2026-08-26 客戶回報的就是這種：狀態碼一路 200）。
    """
    samples = _samples()
    if not samples:
        pytest.skip("這台機器沒有 temp_pdfs 樣本")
    empty = []
    dots = []
    for path in samples:
        r = client.post("/tools/pdf-extract-text/api/pdf-extract-text",
                        files={"file": (path.name, path.read_bytes(), "application/pdf")})
        if r.status_code != 200:
            continue
        text = "".join(p.get("text", "") for p in r.json().get("pages", []))
        stripped = text.strip()
        if not stripped:
            empty.append(path.name)          # 掃描件沒有文字層屬正常，見下
        elif all(ch in "•·*?_ \n\t" for ch in stripped):
            dots.append(path.name)

    # 整份都是佔位字元 = 文字對應表壞掉卻沒還原成功，一份都不該有
    assert not dots, f"這些樣本抽出來整片是佔位字元：{dots}"
    # 掃描件本來就沒有文字層，但**大多數**樣本應該抽得到字
    assert len(empty) <= len(samples) * 0.3, (
        f"抽不到文字的樣本太多（{len(empty)}/{len(samples)}）："
        f"{empty[:5]} —— 抽取路徑可能整條壞了")
