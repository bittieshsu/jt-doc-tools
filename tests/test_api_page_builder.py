"""`github/build-api-page.py` 產出的 api.html 不可以毀損。

## 由來（v1.14.20）

生成器用 `\\x00<n>\\x00` 當佔位符把行內程式碼 / 連結 / 粗體先抽出來保護，最後再還原。
**但佔位符會巢狀**：`**依 `x` 而定**` 會先把 `` `x` `` 抽成佔位符，接著 bold 再把
「含佔位符的整段」抽成第二層。原本只 `re.sub` 一次，還原出外層之後內層那個佔位符
就永遠留在輸出裡（`re.sub` 不會回頭重掃自己的替換結果）。

症狀非常隱蔽：HTML 裡多出 NUL 位元組，而且**那段程式碼字面直接從畫面上消失**，
沒有任何錯誤訊息。`grep` 還會因此把整個 api.html 當成二進位檔，於是連「這頁有沒有
某個端點」都查不出來。第一次踩到是新增「PDF 轉 Markdown」時寫了
`**回應型別依 `include_images` 而不同**`。
"""
from __future__ import annotations

import importlib.util
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
BUILDER = ROOT / "github" / "build-api-page.py"


@pytest.fixture(scope="module")
def builder():
    spec = importlib.util.spec_from_file_location("build_api_page", BUILDER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ------------------------------------------------------------ 巢狀行內標記

def test_code_inside_bold_survives(builder):
    """踩過的那一個 —— 參數名整個從畫面上消失。"""
    out = builder.render_inline("**回應型別依 `include_images` 而不同**")
    assert "\x00" not in out, "佔位符沒還原（NUL 會留在 HTML 裡）"
    assert "include_images" in out, "行內程式碼的字面不見了"
    assert "<code>" in out and "<strong>" in out


@pytest.mark.parametrize("src,must_contain", [
    ("**粗體含 `code` 字**", "code"),
    ("**[連結](https://example.com) 在粗體內**", "example.com"),
    ("`code` 與 **bold** 並列", "code"),
    ("**a `x` b `y` c**", "y"),
])
def test_nested_inline_markup_is_fully_restored(builder, src, must_contain):
    out = builder.render_inline(src)
    assert "\x00" not in out, f"{src!r} → 佔位符殘留"
    assert must_contain in out


def test_plain_text_is_escaped(builder):
    """保護機制不可以順手把跳脫弄丟。"""
    out = builder.render_inline("a < b & c > d")
    assert "&lt;" in out and "&amp;" in out


# ------------------------------------------------------------ 產出檔本身

def test_generated_page_has_no_nul_bytes():
    """NUL 會讓 grep 把整頁當二進位 —— 連查端點在不在都查不出來。"""
    data = (ROOT / "github" / "docs" / "api.html").read_bytes()
    assert data.count(b"\x00") == 0, (
        "api.html 有 NUL 位元組，代表行內標記還原失敗；"
        "重跑 python3 github/build-api-page.py")


def test_generated_page_is_in_sync_with_api_md(tmp_path, builder, monkeypatch):
    """改了 API.md 沒重跑生成器 → 網頁版 stale，只有點進去的人看得到。"""
    current = (ROOT / "github" / "docs" / "api.html").read_text(encoding="utf-8")
    out = tmp_path / "api.html"
    monkeypatch.setattr(builder, "OUT", out)
    builder.main()
    assert out.read_text(encoding="utf-8") == current, (
        "api.html 與 API.md 不同步 —— 請重跑 python3 github/build-api-page.py")
