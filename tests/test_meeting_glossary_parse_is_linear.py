"""「專有名詞或會議背景」的解析不可以是平方級（v1.16.59，CodeQL #200～#203、#199）。

## 由來

CodeQL 標了三個「polynomial regular expression」：箭頭的式子寫成 `\\s*(?:→|->|=>|⇒)\\s*`，
一行很長的空白、又沒有箭頭時，每個起點都把後面的空白吃完再失敗 —— 實測 16,000 個空白要 5.4 秒、
長度加倍時間變四倍。**三支呼叫它的端點都是 async、直接在事件迴圈上跑**，所以一個請求就讓整個網站停住。

順著看同一條路，另外三處也是平方級，CodeQL 沒標：
* 箭頭右邊先拆詞（`_strip_brackets` 一次剝一個括號、每次都數一遍）才檢查長度 —— 一長串括號；
* 同一個錯寫法去重時，每加一個就把前面的全部重建成集合 —— 一行寫幾萬個錯寫法；
* 重複的詞每次都回頭掃整份清單 —— 幾萬個重複的詞。

第四個 CodeQL 告警（#203「overly permissive regex range」）是真的：漢字那一類的第三段起點打成
一般的「豈」（U+8C48），不是相容漢字區的 U+F900 —— 那一段實際涵蓋 U+8C48～U+FAFF，
把韓文音節、彝文、私用區都算成漢字。

## 判準

時間的上限給得很寬（3 秒；舊寫法同樣的輸入要幾十秒到幾百秒），分得出平方級，
在忙碌的機器上也不會誤報。另有一條驗式子的形狀（不靠計時）。
"""
from __future__ import annotations

import importlib
import logging
import re
import time
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

mt = importlib.import_module("app.tools.meeting_transcribe.router")

LIMIT_S = 3.0


def _timed(fn, *args):
    t0 = time.perf_counter()
    try:
        out = fn(*args)
        err = None
    except HTTPException as e:
        out, err = None, e
    return out, err, time.perf_counter() - t0


def test_the_arrow_pattern_does_not_carry_surrounding_whitespace():
    """形狀：箭頭兩旁的空白另外 strip，不寫進式子（寫進去就是 CodeQL 標的那種）。"""
    assert r"\s" not in mt._VARIANT_ARROW.pattern
    # 行為不變：兩旁有沒有空白都認得、剛好一個箭頭才算
    assert mt._variant_line("Proksmox → Proxmox") == ("Proxmox", ["Proksmox"])
    assert mt._variant_line("Proksmox->Proxmox") == ("Proxmox", ["Proksmox"])
    assert mt._variant_line("上傳 → 轉檔 → 下載") is None


def test_a_long_blank_line_without_an_arrow_does_not_stall():
    line = "ab" + " " * 200_000 + "cd"
    out, err, dt = _timed(mt.parse_glossary, line)
    assert err is None and dt < LIMIT_S, f"{dt:.1f} 秒"
    assert out[0] == ["ab cd"]


def test_a_long_run_of_brackets_on_the_right_is_refused_before_splitting():
    out, err, dt = _timed(mt.parse_glossary, "Proksmox → " + "(" * 200_000)
    assert dt < LIMIT_S, f"{dt:.1f} 秒"
    assert err is not None and err.status_code == 400 and "太長" in str(err.detail)


def test_many_duplicate_terms_are_not_quadratic():
    lines = [f"Term{i}" for i in range(30_000)] * 2
    out, err, dt = _timed(mt.parse_glossary, "\n".join(lines))
    assert dt < LIMIT_S, f"{dt:.1f} 秒"
    assert err is not None and err.status_code == 400      # 超過 MAX_TERMS，照講出數字


def test_many_wrong_spellings_on_one_line_are_not_quadratic():
    line = "、".join(f"w{i:05d}" for i in range(40_000)) + " → Proxmox"
    out, err, dt = _timed(mt.parse_glossary, line)
    assert dt < LIMIT_S, f"{dt:.1f} 秒"
    assert err is not None and err.status_code == 400      # 超過 MAX_VARIANTS


def test_duplicates_still_keep_the_first_spelling_and_merge_variants():
    """改成字典之後行為不變：重複的詞留第一次的寫法、兩行的錯寫法併在一起、不分大小寫去重。"""
    terms, variants = mt.parse_glossary("Proxmox\nproxmox\nProksmox → PROXMOX\nproksmox、Proxmux → Proxmox")
    assert terms == ["Proxmox"]
    assert variants == {"Proxmox": ["Proksmox", "Proxmux"]}


def test_the_cjk_class_covers_ideographs_only():
    """漢字那一類：中日韓漢字（含擴充 A 與相容漢字），**不含**韓文音節、彝文、私用區。"""
    c = mt._CJK_CHAR
    for ch in "漢字豈㐀䶿一鿿豈﫿":
        assert c.match(ch), f"U+{ord(ch):04X} 應該算漢字"
    for ch in "한국어ꀀab1":
        assert not c.match(ch), f"U+{ord(ch):04X} 不是漢字"
    # 範圍不可以再寫成字面的「豈」（U+8C48）—— 那就是 #203 抓到的錯
    assert "豈" not in c.pattern


def test_a_failed_thinking_probe_logs_the_model_name_on_one_line(client, caplog, monkeypatch):
    """#199：模型名稱是頁面送來的，換行要拿掉，不然可以在記錄裡偽造一行。"""
    from app.core import llm_client as lc
    monkeypatch.setattr(lc.LLMClient, "test_connection",
                        lambda self: SimpleNamespace(ok=True, latency_ms=1, error=None, models=[]))

    def boom(self, model):
        raise RuntimeError("probe failed")
    monkeypatch.setattr(lc.LLMClient, "thinking_probe", boom)
    forged = "gemma\n2026-10-06 12:00:00 INFO 管理員登入成功"
    with caplog.at_level(logging.WARNING):
        r = client.post("/admin/api/llm/test-connection",
                        json={"base_url": "http://127.0.0.1:9", "probe_model": forged})
    assert r.status_code == 200, r.text
    msgs = [rec.getMessage() for rec in caplog.records if "思考檢查失敗" in rec.getMessage()]
    assert msgs, "沒有記到思考檢查失敗"
    assert all("\n" not in m and "\r" not in m for m in msgs), msgs
