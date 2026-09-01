"""去識別化類工具**必須**驗到「產出本身」（使用者要求，2026-09-01）。

由來是 issue #51：地址式子把 `[縣市]` 寫成字面 `<縣市>`，大部分縣市的地址
**從上線起就沒抓到過**，而且完全無聲。那個 bug 在單元層級一眼可見卻活了很多版，
因為沒有任何測試是「拿一份真的有地址的檔案跑一次，看它最後有沒有被遮掉」。

這裡只釘死能客觀判定的那條線：**這兩支工具的端到端測試要在，而且最後一步
必須是「產出裡抽不到那段個資」**。其餘工具的輸出層驗收是人的判斷
（有些走內部函式驗得更嚴），寫在 `TEST_PLAN.md` §0.5 的清單裡，不在這裡用
啟發式猜 —— 猜太鬆會變成一支永遠綠的假測試，猜太緊會把驗得更嚴的工具誤報。
"""
from __future__ import annotations

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

# (端到端測試檔, 必須呼叫的端點, 產出取回的方式)
E2E = {
    "doc-deident": ("tests/test_doc_deident_e2e.py",
                    "/tools/doc-deident/process", "fitz.open(stream="),
    "text-deident": ("tests/test_text_deident_e2e.py",
                     "/tools/text-deident/process", 'json()["text"]'),
}


@pytest.mark.parametrize("tool", sorted(E2E))
def test_deident_tool_has_an_end_to_end_test(tool: str):
    path, endpoint, _ = E2E[tool]
    f = REPO / path
    assert f.is_file(), f"{tool} 少了端到端測試 {path}"
    src = f.read_text(encoding="utf-8")
    assert endpoint in src, f"{path} 沒有真的呼叫 {endpoint} —— 只驗偵測不算端到端"


@pytest.mark.parametrize("tool", sorted(E2E))
def test_end_to_end_test_asserts_on_the_output(tool: str):
    """最後一步必須是「打開產出、確認個資不在裡面」。

    只驗「偵測到幾筆」是不夠的 —— 偵測到卻沒真的處理掉，是最危險的失敗方式：
    使用者會拿著一份標示「已處理」的檔案送出去。
    """
    path, _, how = E2E[tool]
    src = (REPO / path).read_text(encoding="utf-8")
    assert how in src, f"{path} 沒有把產出取回來重新檢查（缺 {how}）"
    assert "not in" in src, f"{path} 沒有『產出裡不可以再出現那段個資』的斷言"
