"""通知設定頁的兩件事：**存進去的值不可以被自動帶值蓋掉**、欄位要看得到內容。

由來（2026-09-10，客戶回報兩則）：

1. 「連接埠就算改成 25 按儲存，下次再回來看又變 587」。原因是換寄送方式時
   會幫忙帶慣例埠號，而那段程式**在頁面載入時也跑了一次** —— 判斷「使用者
   有沒有改過」用的旗標只在這次載入打字時才會設，所以每次開頁都把存好的值
   覆蓋掉。用真的瀏覽器重現過：存 25 → 重新載入 → 變 587。

2. 「站台網址寬度太小」。那條 CSS 是照**數字欄位**寫的（`width: 110px`
   ＋ 靠右 ＋ 等寬數字），網址欄繼承了同一條規則，只看得到
   `https://doc.exa…`。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

TPL = Path("app/admin/templates/admin_notify.html").read_text(encoding="utf-8")


def test_the_saved_port_is_not_overwritten_on_load():
    """**載入時不可以動已經存好的值。**

    判準是：初始那一次呼叫要能被區分出來，而且帶值那段被它擋著。
    """
    assert "apply(true)" in TPL, "初始呼叫沒有標記成 initial"
    assert re.search(r"addEventListener\('change',\s*\(\)\s*=>\s*apply\(false\)\)", TPL), \
        "換模式時沒有以 initial=false 呼叫"
    assert re.search(r"if \(portEl && !initial\)", TPL), \
        "帶入慣例埠號時沒有排除「載入」那一次 —— 存好的值會被蓋掉"


def test_a_hand_typed_port_is_never_touched():
    """使用者自己填了 2526 這種值，換模式也不可以動它。"""
    assert "CONVENTIONAL" in TPL, "沒有「目前的值是不是慣例值」這個判斷"
    m = re.search(r"CONVENTIONAL = new Set\(\[([^\]]*)\]\)", TPL)
    assert m, "找不到慣例埠號清單"
    assert "'587'" in m.group(1) and "'25'" in m.group(1)


def test_the_url_field_does_not_inherit_the_number_field_width():
    """一條 CSS 同時套在數字欄與網址欄上，就是這個 bug 的來源。"""
    assert ".nt-inline input[type=number]" in TPL, "數字欄沒有自己的規則"
    assert re.search(r"\.nt-inline input\[type=text\]", TPL), "文字欄沒有自己的規則"
    # 共用那條不可以再帶寬度 / 對齊
    m = re.search(r"\.nt-inline input \{([^}]*)\}", TPL)
    assert m, "找不到共用規則"
    shared = m.group(1)
    for bad in ("width:", "text-align:"):
        assert bad not in shared, f"共用規則還帶著 {bad} —— 會壓到網址欄"


@pytest.mark.parametrize("prop", ["width: 110px", "text-align: right"])
def test_the_narrow_style_is_scoped_to_numbers(prop: str):
    m = re.search(r"\.nt-inline input\[type=number\] \{([^}]*)\}", TPL)
    assert m and prop in m.group(1), f"{prop} 應該只套在數字欄上"
