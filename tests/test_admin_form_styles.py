"""管理區的設定頁要用同一套表單樣式。

## 由來

使用者回報「SSO 單一登入設定頁應該樣式沒套」。用真實瀏覽器截圖比對才看出差在哪：
輸入框本身其實有套到全域的 `.field`，**差的是版面結構** ——

* 認證設定頁：欄位分組在有邊框的區塊卡片裡，每組上緣有一個標籤（連線 / 搜尋 /
  屬性對應…），每個欄位有標題與說明，整體限寬。
* SSO 頁：一整片兩欄 grid，標籤是純文字，欄位橫跨整個面板寬度，沒有任何分組。

兩頁擺在一起看像不同產品。原因是那套樣式當初只寫在認證設定頁的 `<style>` 裡，
別的頁面拿不到，只好各自寫一套陽春的。

修法是把它抽到 `platform.css`，SSO 頁掛上同樣的 class。這份測試守住兩件事：
樣式留在共用位置（不要有人又搬回單一頁面），以及設定頁確實用了它。
"""
from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CSS = ROOT / "static" / "css" / "platform.css"
TPL = ROOT / "app" / "admin" / "templates"

#: 用這套表單樣式的設定頁。新的設定頁請一併加進來。
FORM_PAGES = ["admin_auth_settings.html", "admin_sso.html"]


def test_shared_form_styles_live_in_platform_css():
    """樣式要在共用的 CSS 裡，不可以只寫在某一頁的 <style> 內。

    只寫在單頁的後果就是這次踩到的：下一頁拿不到，只好自己寫一套，
    兩頁長得不一樣。
    """
    css = CSS.read_text(encoding="utf-8")
    for sel in (".auth-section", "> legend", ".af-label", ".af-hint",
                ".af-attrs", ".af-checks"):
        assert sel in css, f"共用表單樣式缺了 {sel}"


@pytest.mark.parametrize("name", FORM_PAGES)
def test_settings_pages_use_the_shared_form_classes(name):
    s = (TPL / name).read_text(encoding="utf-8")
    assert 'class="panel auth-form"' in s or 'class="auth-form"' in s, \
        f"{name} 沒有掛 .auth-form（欄位不會限寬、標籤樣式也吃不到）"
    assert 'class="auth-section"' in s, f"{name} 沒有把欄位分組成區塊卡片"
    assert 'class="af-label"' in s, f"{name} 的欄位標題沒有用 .af-label"


def test_sso_page_no_longer_uses_its_own_ad_hoc_grid():
    """SSO 頁自己那組 `.grid2` 已經被取代 —— 留著會讓人以為還有兩套。"""
    s = (TPL / "admin_sso.html").read_text(encoding="utf-8")
    assert "grid2" not in s, "SSO 頁還留著舊的 .grid2 版面"
