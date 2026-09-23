"""示範資料的欄位標題必須取自出貨的那份預設清單。

`tools/seed_demo_data.py` 產生的是**要拿去拍截圖、放到介紹站公開**的畫面。
它原本自己抄了一份 `{欄位代號: 標題}`，跟
`profile_manager.DEFAULT_FIELDS` 漂了 **6 個欄位** —— 其中兩個漂成大陸用語
「郵箱」（台灣要寫「信箱」），而那正是我們自己禁用詞表上的詞。

**兩邊並排才看得出來**：畫面完全正常、測試全綠、截圖也拍得出來，
只是公開出去的那張圖上寫著大陸用語，而且欄位名稱跟客戶實際會看到的不一樣。

判準是「**那份標題不可以是字面寫死的字典**」，不是比對內容 ——
比對內容的話，有人在兩邊同時改成一樣的錯字照樣會過。
"""
from __future__ import annotations

import ast
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

SEED = REPO / "tools" / "seed_demo_data.py"


def _assign(name: str) -> ast.Assign:
    tree = ast.parse(SEED.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return node
    raise AssertionError(
        f"`{SEED.name}` 裡找不到 `{name}` 的指派 —— 改名的話這支檢查會安靜失效，"
        "所以這裡要直接紅")


def test_the_demo_labels_are_not_a_second_hard_coded_copy():
    """`LABELS` 必須是**算出來的**，不可以是字面字典。"""
    node = _assign("LABELS")
    assert not isinstance(node.value, ast.Dict), (
        "`LABELS` 又被寫成字面字典了 —— 同一份清單放兩個地方一定會漂。"
        "請從 `profile_manager.DEFAULT_FIELDS` 取。")


def test_every_demo_field_has_a_shipped_label():
    """示範資料填的每一個欄位，出貨的預設清單裡都要有。

    沒有的話上面那個推導會 `KeyError` —— 與其等到有人跑種子腳本才炸，
    不如在這裡先講清楚是哪一個欄位。
    """
    from app.core.profile_manager import DEFAULT_FIELDS
    demo = ast.literal_eval(_assign("DEMO_COMPANY").value)
    shipped = {k for k, _lab, _v in DEFAULT_FIELDS}
    missing = sorted(set(demo) - shipped)
    assert not missing, f"示範資料有這些欄位但出貨的預設清單沒有：{missing}"


def test_the_demo_labels_really_match_the_shipped_ones():
    """真的載進來比一次 —— 上面兩條都只看原始碼。"""
    import importlib

    from app.core.profile_manager import DEFAULT_FIELDS
    mod = importlib.import_module("tools.seed_demo_data")
    shipped = {k: lab for k, lab, _v in DEFAULT_FIELDS}
    drifted = {k: (v, shipped.get(k)) for k, v in mod.LABELS.items()
               if shipped.get(k) != v}
    assert not drifted, f"示範資料的標題跟出貨的不一樣：{drifted}"
