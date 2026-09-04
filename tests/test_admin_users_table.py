"""使用者清單的欄位索引與排序型別要對得起來。

## 由來

客戶反映「AD 帳號管理還有精進空間」，查下去發現這一頁有兩個**現在就是壞的**功能：

1. **來源篩選點下去整份清單消失**：JS 寫死 `tr.cells[2]` 取來源，但後來在中間插了
   「信箱」欄，來源變成索引 3。`<th>` 的 `data-col` 有跟著改，JS 沒有 —— 於是
   `src` 永遠是空字串，一點 `ldap` 就 `'' === 'ldap'` 為 false，**每一列都被隱藏**。
2. **「最後登入」排序等於沒作用**：欄位宣告 `data-sort="num"`，但 sort key 是
   ISO 8601 字串，`parseFloat("2026-08-01T09:00:00")` = `2026` —— 同一年的人全部
   同分。

兩者都不會有錯誤訊息，只會安靜地不動作。

## 這份守住什麼

* 表頭的 `data-col` 必須真的等於該欄在 `<tr>` 裡的位置（加欄位時漏改一處就會紅）。
* JS 不可以再寫死來源欄的索引。
* ISO 時間欄不可以用數值排序。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

TPL = (Path(__file__).resolve().parent.parent / "app" / "admin" /
       "templates" / "admin_users.html")


def _src() -> str:
    return TPL.read_text(encoding="utf-8")


#: 標題現在包在 `{{ tr('…') }}` 裡（i18n）。這支測試在乎的是**欄位順序**，
#: 不是字面文字，所以把包裝拆掉再比 —— 不拆的話它會在加 i18n 的那一刻紅，
#: 而那跟它要守的東西無關。
_TR = re.compile(r"\{\{\s*tr\(\s*'([^']*)'\s*\)\s*\}\}")


def _label(raw: str) -> str:
    m = _TR.search(raw)
    return (m.group(1) if m else raw).strip()


def _headers(src: str) -> list[tuple[int, str, str]]:
    """[(data-col, data-sort, 標題文字)]。"""
    out = []
    for m in re.finditer(
            r'<th class="sortable"\s+data-sort="([a-z]+)"\s+data-col="(\d+)">(.*?)</th>',
            src, re.S):
        out.append((int(m.group(2)), m.group(1), _label(m.group(3))))
    return sorted(out)


def test_headers_are_declared():
    """`data-col` 要連續。

    起點不一定是 0 —— 前面可能有不可排序的欄（例如批次操作的勾選框），那一欄
    沒有 `data-col`。真正重要的是「連續」以及「對得上實際位置」（下一個測試）。
    """
    hs = _headers(_src())
    assert hs, "抓不到表頭，掃描邏輯可能失效了"
    cols = [c for c, _, _ in hs]
    assert cols == list(range(cols[0], cols[0] + len(cols))), \
        f"data-col 不連續：{hs}"


def test_data_col_matches_actual_cell_order():
    """`data-col` 要對得上 `<tr>` 裡 `<td>` 的實際順序。

    這是那個 bug 的根源：中間插了一欄，`<th>` 改了、`<td>` 順序變了，
    但引用索引的 JS 沒改。
    """
    src = _src()
    body = src[src.index("<tr data-uid="):]
    body = body[:body.index("</tr>")]
    # 逐一取出 <td ...> 的開頭，順序即欄位順序
    tds = re.findall(r"<td\b([^>]*)>", body)
    hs = _headers(src)
    assert len(tds) >= len(hs), f"欄位數（{len(tds)}）少於表頭數（{len(hs)}）"

    # 來源欄：<th> 說在第幾欄，那一格就該帶 data-sort-key="{{ u.source }}"
    src_col = next(c for c, _, t in hs if t == "來源")
    assert 'data-sort-key="{{ u.source }}"' in tds[src_col], (
        f"表頭說來源在第 {src_col} 欄，但那一格不是來源"
        f"（實際是 `{tds[src_col].strip()[:60]}`）—— 索引錯位就是篩選失效的原因")

    last_col = next(c for c, _, t in hs if t == "最後登入")
    assert "last-login" in tds[last_col], \
        f"表頭說最後登入在第 {last_col} 欄，但那一格不是"


def test_filtering_is_server_side():
    """搜尋 / 來源 / 狀態篩選一律走伺服器端。

    前端過濾只過濾得到「已經 render 出來的那一頁」。使用者以為篩了全部、其實只
    篩了眼前一頁 —— 會讓人以為某個帳號不存在。這比沒有篩選更危險。

    （原本的前端篩選還有個 bug：欄位索引寫死 `cells[2]`，中間插了一欄之後
    永遠比不中，一點來源就整份清單消失。改成伺服器端之後那個雷也一起消失。）
    """
    src = _src()
    assert 'method="get" action="/admin/users"' in src, "篩選不是伺服器端表單"
    for name in ('name="q"', 'name="src"', 'name="state"'):
        assert name in src, f"篩選表單少了 {name}"


def test_js_never_hardcodes_a_column_index():
    """JS 裡不可以出現寫死的欄位索引 —— 加欄位時一定會漏改。

    不比對某個特定壞字串（第一版就是這樣寫，換個寫法就溜過去了），
    而是掃整段 JS。
    """
    src = _src()
    i = src.index("{% block scripts %}") if "{% block scripts %}" in src else 0
    js = src[i:]
    hard = re.findall(r"\.cells\[\s*\d+\s*\]", js)
    assert not hard, f"JS 裡把欄位索引寫死了：{hard}"


def test_iso_column_is_not_sorted_as_number():
    """ISO 時間字串不可以宣告成數值排序。

    `parseFloat("2026-08-01T…")` 只會拿到年份，同年份全部同分。
    """
    src = _src()
    hs = _headers(src)
    for col, kind, title in hs:
        if title == "最後登入":
            assert kind == "date", (
                "最後登入是 ISO 字串，宣告成 num 會被 parseFloat 只取年份")
    assert 'if (type === \'date\')' in src, "排序沒有處理 date 型別"


def test_never_logged_in_sorts_last():
    """從未登入的人（sort key 是空字串）要排到最後，不是排到最前。"""
    src = _src()
    i = src.index("if (type === 'date')")
    block = src[i:i + 700]
    assert "if (!va) return 1;" in block and "if (!vb) return -1;" in block, \
        "空值（從未登入）沒有被推到最後"


@pytest.mark.parametrize("bad", [
    'tr.cells[2].dataset.sortKey',      # 寫死索引導致來源篩選整份清單消失
    'data-sort="num"  data-col="6"',    # ISO 字串用數值排序
])
def test_known_broken_shapes_are_gone(bad):
    assert bad not in _src(), f"這個已知壞掉的寫法又回來了：{bad}"
