"""LLM 逐欄校驗：連兩輪都指出同一個問題才採納。

## 由來

逐欄校驗的最後一行原本是：

    rr.accepted = list(rr.corrections)   # no consensus — accept all

也就是 **LLM 說什麼就採納什麼**。而這個功能的動作是「把已經填好的表單自動改掉」
（改值、甚至把值搬到別的欄位）—— 沒有任何防線。

更糟的是**畫面上早就寫著「連續 N 輪同錯」**並把採納的列標成綠色，設定檔裡也一直有
`consecutive_required: 2` —— 介面與設定都承諾了後端沒有做到的事。看起來有做、
其實沒做，比明講不支援更危險。

舊的整頁版 `llm_review._consensus_filter` 有這個規則，但那條路徑已經不使用了。

## 做法與成本

第二輪**只重問第一輪標記出來的欄位**。貴的是逐欄查詢，而可疑欄位通常只有個位數，
所以成本增加很少，卻擋掉了「問一次剛好抖一下」的偽陽性。

## 測試怎麼寫

不 mock `per_field_review` 本身，而是**換掉最底層的 `_ollama_chat`**，用一個可以
腳本化回答的假模型。這樣兩輪的流程、key 比對、回報都是真的在跑。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.core import llm_review_per_field as pf
from app.core.llm_review import FilledField


def _fields(n=3):
    out = []
    for i in range(n):
        out.append(FilledField(
            page=0, label_text=f"欄位{i}", profile_key=f"key{i}",
            value=f"值{i}", slot_pt=(50, 50 + i * 40, 300, 80 + i * 40)))
    return out


class _FakeLLM:
    """腳本化的假模型。

    `plan` 是 `{(問題種類, 欄位值): [第一輪答案, 第二輪答案, …]}`。

    問題種類要用**各自獨有**的句子判斷 —— q2 / q3 / q4 的提示裡都有 "red box"，
    第一版用它來分類，結果三種問題全被當成 q2（測試自己先出錯）。
    q4 的提示裡沒有欄位值，所以用「最後一次問到的欄位」來對應。
    """

    def __init__(self, plan):
        self.plan = plan
        self.calls: list[tuple[str, str]] = []
        self._last_val = ""

    def __call__(self, base_url, model, prompt, png, timeout):
        if "exactly this value" in prompt:
            kind = "q2"
        elif "make sense for that label" in prompt:
            kind = "q3"
        elif "ACTUALLY inside" in prompt:
            kind = "q4"
        else:
            kind = "q5"
        val = next((v for v in self._values() if f'"{v}"' in prompt), "")
        if val:
            self._last_val = val
        elif kind == "q4":
            # **只有 q4** 的提示裡沒有欄位值，才沿用上一個。其他種類沿用的話，
            # 沒寫進腳本的欄位會繼承別人的答案（第一版就是這樣，害 值2 也被標記）。
            val = self._last_val
        self.calls.append((kind, val))
        seq = self.plan.get((kind, val))
        if not seq:
            # 沒腳本 → q2 說沒問題、其餘給空答案（等同「問不出來」）
            return ("YES", None) if kind == "q2" else ("", None)
        return (seq.pop(0), None)

    def _values(self):
        return {v for _, v in self.plan}


@pytest.fixture
def enabled(monkeypatch, tmp_path):
    # `llm_settings` 是**實例**不是模組 —— 要 patch 實例上的方法
    monkeypatch.setattr(pf.llm_settings, "get", lambda: {
        "enabled": True, "base_url": "http://fake", "model": "m",
        "consecutive_required": 2})
    monkeypatch.setattr(pf.llm_settings, "get_model_for", lambda tool: "m")
    monkeypatch.setattr(pf, "_render_page", lambda p, i: b"PNG")
    monkeypatch.setattr(pf, "_crop_tile", lambda img, slot: b"TILE")
    return tmp_path / "x.pdf"


def _run(monkeypatch, pdf, plan, fields=None):
    fake = _FakeLLM(plan)
    monkeypatch.setattr(pf, "_ollama_chat", fake)
    res = pf.per_field_review(pdf, fields or _fields(), page_index=0)
    return res, fake


# ------------------------------------------------------------ 核心規則

def test_flagged_twice_is_accepted(enabled, monkeypatch):
    """兩輪都說 NO → 採納。"""
    plan = {("q2", "值1"): ["NO", "NO"], ("q3", "值1"): ["YES", "YES"],
            ("q4", "值1"): ["別的字", "別的字"]}
    res, _ = _run(monkeypatch, enabled, plan)
    accepted = [c for r in res.rounds for c in r.accepted]
    assert len(accepted) == 1
    assert accepted[0].label == "欄位1"


def test_flagged_once_is_not_accepted(enabled, monkeypatch):
    """**這就是原本壞掉的地方** —— 第一輪說 NO、第二輪說 YES，不可以採納。"""
    plan = {("q2", "值1"): ["NO", "YES"], ("q3", "值1"): ["YES"],
            ("q4", "值1"): ["別的字"]}
    res, _ = _run(monkeypatch, enabled, plan)
    accepted = [c for r in res.rounds for c in r.accepted]
    assert accepted == [], "只被指出一次就採納了"


def test_flagged_once_is_still_visible(enabled, monkeypatch):
    """沒被採納的疑慮**仍然要看得到** —— 無聲丟掉會讓人以為 LLM 什麼都沒發現。"""
    plan = {("q2", "值1"): ["NO", "YES"], ("q3", "值1"): ["YES"],
            ("q4", "值1"): ["別的字"]}
    res, _ = _run(monkeypatch, enabled, plan)
    all_c = [c for r in res.rounds for c in r.corrections]
    assert len(all_c) >= 1, "疑慮被整個丟掉了"
    assert any(c.label == "欄位1" for c in all_c)


def test_second_round_only_reprobes_suspects(enabled, monkeypatch):
    """第二輪**只**重問可疑的那幾個 —— 全部重跑一次成本會加倍。"""
    plan = {("q2", "值1"): ["NO", "NO"], ("q3", "值1"): ["YES", "YES"],
            ("q4", "值1"): ["別的字", "別的字"]}
    res, fake = _run(monkeypatch, enabled, plan, _fields(5))
    q2_calls = [v for k, v in fake.calls if k == "q2"]
    # 5 個欄位各問一次（第一輪）+ 唯一那個可疑的再問一次（第二輪）= 6
    assert len(q2_calls) == 6, f"第二輪問的欄位數不對：{q2_calls}"
    assert q2_calls.count("值1") == 2, "可疑欄位沒有被再確認"


def test_no_suspects_means_no_second_round(enabled, monkeypatch):
    """第一輪就全清 → 不必跑第二輪。"""
    res, fake = _run(monkeypatch, enabled, {})
    assert len(res.rounds) == 1
    assert res.rounds[0].verdict == "all_clear"


def test_type_must_match_across_rounds(enabled, monkeypatch):
    """兩輪都說有問題但**說法不同**（一次說值錯、一次說填錯格）→ 不算同錯。

    模型連「哪裡不對」都沒有共識時，自動套用它的建議是危險的。
    """
    plan = {("q2", "值1"): ["NO", "NO"],
            ("q3", "值1"): ["YES", "NO"],      # 第一輪值錯、第二輪填錯格
            ("q4", "值1"): ["別的字", "別的字"]}
    res, _ = _run(monkeypatch, enabled, plan)
    accepted = [c for r in res.rounds for c in r.accepted]
    assert accepted == [], "兩輪對「哪裡不對」沒有共識，卻採納了"


# ------------------------------------------------------------ 設定與相容

def test_setting_one_falls_back_to_single_round(enabled, monkeypatch):
    """管理員把 `consecutive_required` 設成 1 → 維持單輪（原本的行為）。"""
    monkeypatch.setattr(pf.llm_settings, "get", lambda: {
        "enabled": True, "base_url": "http://fake", "model": "m",
        "consecutive_required": 1})
    plan = {("q2", "值1"): ["NO"], ("q3", "值1"): ["YES"], ("q4", "值1"): ["別的字"]}
    res, _ = _run(monkeypatch, enabled, plan)
    assert len(res.rounds) == 1
    assert len(res.rounds[0].accepted) == 1


def test_disabled_by_default():
    """LLM 校驗是附加功能，預設關閉 —— 這一輪不可以把它打開。"""
    from app.core.llm_settings import DEFAULT_SETTINGS
    assert DEFAULT_SETTINGS.get("enabled") in (False, None, 0)


def test_default_consecutive_is_two():
    from app.core.llm_settings import DEFAULT_SETTINGS
    assert int(DEFAULT_SETTINGS.get("consecutive_required", 0)) >= 2


# ------------------------------------------------------------ 不再全收

def test_accept_all_line_is_gone():
    """靜態守門：`accepted = list(rr.corrections)` 不可以再出現在第一輪之後。

    這一行就是原本的問題本身。留下的那一處是 `consecutive_required == 1`
    的退路，必須在明確的分支底下。
    """
    import inspect
    src = inspect.getsource(pf.per_field_review)
    assert "no consensus — accept all" not in src
    i = src.index("rr.accepted = list(rr.corrections)")
    before = src[:i]
    assert "need >= 2" in before, "全收那一行沒有被放在單輪的分支底下"


def test_ui_promise_matches_the_backend():
    """畫面上寫著「連續 N 輪同錯」—— 後端要真的做到，不然是承諾了做不到的事。"""
    tpl = (Path(pf.__file__).resolve().parent.parent / "tools" / "pdf_fill" /
           "templates" / "pdf_fill.html").read_text(encoding="utf-8")
    assert "連續" in tpl and "輪同錯" in tpl
    import inspect
    assert "consecutive_required" in inspect.getsource(pf.per_field_review)


def test_ui_deduplicates_across_rounds():
    """每一輪都會回報它看到的疑慮 —— 連兩輪確認的那些**必然**出現在兩輪裡。

    畫面直接攤平所有輪次的話，同一個問題會在表格上列兩次。加了第二輪之後這是
    必然發生的（實測確認過：一個問題變成兩列），所以畫面要去重。
    """
    tpl = (Path(pf.__file__).resolve().parent.parent / "tools" / "pdf_fill" /
           "templates" / "pdf_fill.html").read_text(encoding="utf-8")
    i = tpl.index("const allCorrections")
    seg = tpl[max(0, i - 600):i + 600]
    assert "_seen" in seg and "filter" in seg, "沒有去重，同一個問題會列兩次"


def test_backend_still_reports_both_rounds():
    """去重是**畫面**的事，後端仍然要如實回報每一輪看到什麼 ——
    兩輪的原始結果對排查很有用（例如判斷模型抖動的程度）。"""
    import inspect
    src = inspect.getsource(pf.per_field_review)
    assert "result.rounds.append(r2)" in src
    assert "r2.corrections.append" in src
