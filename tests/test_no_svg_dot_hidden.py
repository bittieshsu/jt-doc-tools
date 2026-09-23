"""SVG 元素不可以用 `.hidden` 開關顯示。

2026-09-14 使用者回報「掃描修正的四個角拉起來還是沒有連線」。根因：

* `SVGElement` **沒有 `hidden` 這個 IDL 屬性**（HTML 規格把它定義在
  `HTMLElement` 上）。`svg.hidden = false` 只是在物件上掛一個沒人看的
  expando，**`hidden` 那個標記一個字都沒動**。
* 而 `static/css/platform.css` 的 `[hidden] { display: none !important; }`
  是**作者樣式**、沒有命名空間限定 —— 瀏覽器內建的 `html.css` 有
  `@namespace`（所以只管 HTML 元素），我們這條沒有，**SVG 照樣被它蓋掉**。

兩件事湊起來 = 那個 `<svg>` 從寫出來的第一版起就永遠 `display:none`，
而且**沒有任何 JS 例外**。旁邊的手柄是 `<div>`，`.hidden` 正常，
所以畫面上是「四個點在、線永遠不出現」。

判準：`<svg>` 的顯示開關一律走 `toggleAttribute('hidden', …)` /
`setAttribute` / `removeAttribute`（兩種元素都成立）。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.source_text import strip_js_comments, strip_markup_comments  # noqa: E402
_DIRS = ("app", "static")


def _sources() -> list[Path]:
    out: list[Path] = []
    for d in _DIRS:
        for p in sorted((ROOT / d).rglob("*")):
            if p.suffix in (".html", ".js") and p.is_file():
                out.append(p)
    return out


def _svg_ids(text: str) -> set[str]:
    ids: set[str] = set()
    for m in re.finditer(r"<svg\b[^>]*>", text):
        mid = re.search(r"""\bid=["']([^"']+)""", m.group(0))
        if mid:
            ids.add(mid.group(1))
    return ids


def _offenders(raw: str) -> list[str]:
    # **先去註解**：說明裡一定會引用「那個不可以用的寫法」當反例
    #（我自己第一版就被旁邊那段解釋這條規則的註解判成違規）。
    ids = _svg_ids(raw)
    text = strip_js_comments(strip_markup_comments(raw))
    bad: list[str] = []
    for sid in sorted(ids):
        q = re.escape(sid)
        # 直接寫：$('id').hidden = …  /  getElementById('id').hidden = …
        if re.search(rf"""(?:\$\(|getElementById\()\s*["']{q}["']\s*\)\s*\.hidden\s*=""", text):
            bad.append(sid)
            continue
        # 先存進變數再開關：const svg = $('id'); … svg.hidden = …
        for m in re.finditer(rf"""(\w+)\s*=\s*\$\(\s*["']{q}["']\s*\)""", text):
            var = re.escape(m.group(1))
            if re.search(rf"\b{var}\.hidden\s*=", text[m.end():]):
                bad.append(f"{sid}（經變數 {m.group(1)}）")
                break
    # 在 JS 裡直接造出來的 svg
    for m in re.finditer(r"""(\w+)\s*=\s*document\.createElementNS\(\s*["'][^"']*svg["']""", text):
        var = re.escape(m.group(1))
        if re.search(rf"\b{var}\.hidden\s*=", text[m.end():]):
            bad.append(f"createElementNS → {m.group(1)}")
    return bad


@pytest.mark.parametrize("path", _sources(), ids=lambda p: str(p.relative_to(ROOT)))
def test_svg_visibility_is_not_toggled_with_the_hidden_property(path: Path) -> None:
    bad = _offenders(path.read_text(encoding="utf-8"))
    assert not bad, (
        f"{path.relative_to(ROOT)} 用 `.hidden` 開關 SVG：{bad}。"
        " SVGElement 沒有 hidden 這個屬性，標記不會被拿掉，"
        " 而 platform.css 的 [hidden] 規則對 SVG 也生效 → 永遠看不到。"
        " 請改用 toggleAttribute('hidden', …)。"
    )


def test_the_author_stylesheet_really_does_hide_svg_too() -> None:
    """這條規則存在，上面那條檢查才有意義 —— 兩者要一起看。

    如果哪天 `platform.css` 的 `[hidden]` 規則被限定成只管 HTML
    （例如改寫成 `:where(html|*)[hidden]`），上面的檢查就可以放寬。
    在那之前它必須是無命名空間的全域規則。
    """
    css = (ROOT / "static" / "css" / "platform.css").read_text(encoding="utf-8")
    rule = re.search(r"^\[hidden\]\s*\{[^}]*display:\s*none", css, re.M)
    assert rule, "platform.css 少了全域的 [hidden] 規則？那上面那條檢查的理由要重寫"


def test_the_scan_actually_reaches_a_template_that_has_an_svg_with_an_id() -> None:
    """空迴圈的 assert 永遠成立 —— 要確認掃描真的看得到目標形狀。"""
    seen = [p for p in _sources() if _svg_ids(p.read_text(encoding="utf-8"))]
    assert seen, "一個帶 id 的 <svg> 都沒掃到，這份檢查等於沒有在檢查"
