"""樣板裡不可以用瀏覽器原生的 alert / confirm / prompt（使用者要求）。

原生對話框會頂著網域名（「doc.jason.tools 顯示」），樣式也跟全站不一致 ——
使用者在字型管理頁的「變更名稱」看到的就是那個。本站有自己的對話框
（`static/js/modal.js` 的 `showAlert` / `showConfirm` / `showPrompt`）。

`window.alert` 只允許當**退路**用（`(window.showAlert || window.alert)(...)`），
那是 modal.js 還沒載入時的保險，不是主要路徑。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
TEMPLATES = sorted(REPO.glob("app/**/templates/**/*.html"))
IDS = [str(p.relative_to(REPO)) for p in TEMPLATES]

# 主要路徑的呼叫。**退路寫法不算** —— modal.js 還沒載入時的保險是合理的：
#   `(window.showAlert || window.alert)(...)`
#   `window.showConfirm ? window.showConfirm(...) : Promise.resolve(confirm(...))`
# 只擋 prompt / confirm：它們會擋住畫面等使用者回應，而且頂著網域名。
# `alert` 全站已經統一走 `showAlert`，保留 `window.alert` 當退路。
_NATIVE = re.compile(
    r"\bwindow\.(?:prompt|confirm)\s*\("      # window.prompt(...)
    r"|(?<![.\w])(?:prompt|confirm)\s*\("      # 裸寫 prompt(...)
)
_FALLBACK = re.compile(
    r"window\.show(Alert|Confirm|Prompt)\s*(\|\||\?)"      # 兩種退路寫法的前半
)


def _is_fallback(code: str, at: int) -> bool:
    """這個原生呼叫是不是「showXxx 不在時」的退路？"""
    head = code[max(0, at - 260):at]
    return bool(_FALLBACK.search(head))


def _strip_comments(js: str) -> str:
    js = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$", "", js)


@pytest.mark.parametrize("path", TEMPLATES, ids=IDS)
def test_no_native_prompt_or_confirm(path: Path):
    src = path.read_text(encoding="utf-8")
    for block in re.findall(r"<script\b[^>]*>(.*?)</script>", src, re.S | re.I):
        code = _strip_comments(block)
        hit = next((m for m in _NATIVE.finditer(code)
                    if not _is_fallback(code, m.start())), None)
        assert not hit, (
            f"{path.name} 用了瀏覽器原生對話框（{hit.group(0)!r}）—— "
            "改用本站的 showPrompt / showConfirm（static/js/modal.js）。"
            "原生的會頂著網域名，樣式也跟全站不一致。"
        )
