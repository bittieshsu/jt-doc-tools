"""「事件與影響」必須自己走一輪 —— **零退步是由構造保證的，不是調出來的**。

## 由來（v1.15.92，外部評語：摘要沒講出事故本身的事實與影響）

摘要**只從已驗證的項目寫**，而「發生了什麼事」不屬於原本那四類，
所以結構上不可能出現。補法是加一類 —— 但**怎麼加，量出來差很多**：

| 做法 | 四類抓到率 | 四類捏造率 |
|---|---|---|
| 基準（四類） | 100% | **5%** |
| 五類擠同一次呼叫 | 88~100% | **16~19%** |
| 五類＋補明確的分流規則 | 100% | **20%**（更糟） |
| **`impacts` 自己一輪** | **100%** | **5~6%** |

**加規則 → 產出更多 → 噪音更多。** 看條目才知道原因：模型確實開始注意到
事實了，但**把事實歸進決議**（「rate limit 目前是每分鐘六十次」
「維護費對方開一年三十二萬」）。問題不是界線講不清楚，是**一次要判斷五類**本身。

所以這一份守的是**那個構造**：主路徑的四類完全不知道有第五類存在。
少了這道守門，有人把 `impacts` 併回主提示時**不會有任何測試變紅**，
而捏造率會靜靜地變成三倍。
"""
from __future__ import annotations

import inspect

from app.core import meeting_insight as mi


def test_the_main_path_does_not_know_about_impacts():
    """主路徑的四類是固定的 —— `impacts` 不可以混進去。"""
    assert mi.MAIN_KINDS == ("decisions", "actions", "risks", "questions")
    assert "impacts" not in mi.MAIN_KINDS
    assert mi.KINDS == ("impacts",) + mi.MAIN_KINDS, "五類的顯示順序變了"


def test_the_four_kind_prompt_never_mentions_impacts():
    """**四類的提示一個字都不可以提到 `impacts`。**

    提了就代表模型在那一次呼叫裡要顧五類 —— 那正是量到捏造率三倍的那個做法。
    """
    for name in ("_RULES", "_REVIEW_RULES"):
        body = getattr(mi, name)
        assert "impacts" not in body, f"{name} 提到了 impacts"
        assert "事件與影響" not in body, f"{name} 提到了事件與影響"


def test_the_impacts_prompt_only_asks_for_impacts():
    """反過來也一樣：impacts 的提示不可以去要其餘四類。"""
    body = mi._IMPACT_RULES
    assert '"impacts"' in body, "impacts 的提示沒有要求那個鍵"
    for k in mi.MAIN_KINDS:
        assert f'"{k}"' not in body, f"impacts 的提示去要了 {k}"


def test_analyse_runs_impacts_in_its_own_loop():
    """判準走原始碼：主迴圈用 `MAIN_KINDS`，impacts 另外一輪。

    **不可以只驗 `MAIN_KINDS` 這個常數存在** —— 常數在、迴圈卻還在用 `KINDS`
    的話，構造保證一樣不成立，而且測試照樣全綠。
    """
    src = inspect.getsource(mi.analyse)
    # **判準是「一處都不可以用 KINDS」，不是「有出現 MAIN_KINDS」。**
    # `analyse` 裡有兩處逐類迴圈（抽取、合併）——
    # 只驗「有出現 MAIN_KINDS」的話，把其中一處改回 `KINDS` 照樣全綠
    # （變異驗證當場抓到）。
    assert "for kind in KINDS:" not in src, (
        "`analyse` 裡還有迴圈在跑五類 —— 那就是捏造率變三倍的那個做法"
    )
    assert src.count("for kind in MAIN_KINDS:") >= 2, "逐類迴圈沒有全部改用 MAIN_KINDS"
    assert "build_impact_prompt" in src, "impacts 沒有自己的抽取"
    assert "kinds=MAIN_KINDS" in src, "複審沒有限縮在四類"
    assert "_IMPACT_REVIEW_RULES" in src, "impacts 沒有自己的複審規則"


def test_impacts_can_be_turned_off_without_touching_the_four():
    """`with_impacts=False` 要完全回到基準的行為（呼叫數也要回去）。"""
    sig = inspect.signature(mi.analyse)
    assert "with_impacts" in sig.parameters
    assert sig.parameters["with_impacts"].default is True

    calls: list[str] = []

    def ask(prompt: str) -> str:
        calls.append(prompt)
        return '{"decisions":[],"actions":[],"risks":[],"questions":[]}'

    segs = [{"seq": i, "text": f"第 {i} 句話，內容普通。", "speaker": "A"}
            for i in range(1, 12)]
    mi.analyse(segs, ask, second_pass=False, with_impacts=False)
    assert calls, "一次都沒問模型 —— 這條測試自己壞了"
    # 判準用 `"impacts"` —— 四類的提示裡沒有這個字（上面那條守著），
    # 所以它是兩條路徑之間真正能區別的標記。
    assert not any('"impacts"' in c for c in calls), \
        "關掉之後仍然送了 impacts 的提示"

    calls.clear()
    mi.analyse(segs, ask, second_pass=False, with_impacts=True)
    assert any('"impacts"' in c for c in calls), "打開卻沒送 impacts 的提示"


def test_every_kind_has_a_label_and_a_chart_style():
    """加第六類時會先紅 —— 少了標籤，匯出的檔案只會少一段（看不出是寫錯）。"""
    from app.core.meeting_charts import KIND_STYLE
    assert set(mi.KIND_LABELS) == set(mi.KINDS)
    for k in mi.KINDS:
        sing = k[:-1] if k.endswith("s") else k
        assert sing in KIND_STYLE, f"{k} 在圖表裡沒有顏色與標籤"
