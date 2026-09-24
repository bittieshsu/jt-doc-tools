"""EasyOCR 的 Reader 一次只建一個 —— 第一次建的時候它會下載模型。

EasyOCR 固定把模型壓縮檔下載到 `<模型目錄>/temp.zip`。兩件 OCR 作業同時第一次跑
（作業併行上限預設就是 2），兩邊一起寫同一個檔 → 解壓時 `Bad CRC-32`，
約 300 MB 白下載，那一件還安靜地退回 Tesseract。Win10 實機在慢網路上重現過：
第一件下載 15 分鐘還沒完，使用者（這裡是測試腳本）又送了第二件。

另外一半：建不起來的時候要記住一段時間。一份 20 頁的 PDF 每一頁都會來要 Reader，
不記的話下載失敗時**每一頁都重新下載一次**。
"""
from __future__ import annotations

import sys
import threading
import time
import types

import pytest

from app.core import ocr_engine as oe

LANGS = ("ch_tra", "en")


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    monkeypatch.setattr(oe, "_easyocr_readers", {})
    monkeypatch.setattr(oe, "_easyocr_failed_at", {})
    yield


def _fake_easyocr(monkeypatch, *, fail=False, delay=0.3):
    state = {"built": 0, "active": 0, "max_active": 0}
    lock = threading.Lock()

    class Reader:
        def __init__(self, langs, **kw):
            with lock:
                state["built"] += 1
                state["active"] += 1
                state["max_active"] = max(state["max_active"], state["active"])
            try:
                time.sleep(delay)          # 模擬下載 / 載入模型
                if fail:
                    raise RuntimeError("Bad CRC-32 for file 'craft_mlt_25k.pth'")
            finally:
                with lock:
                    state["active"] -= 1

    mod = types.ModuleType("easyocr")
    mod.Reader = Reader
    monkeypatch.setitem(sys.modules, "easyocr", mod)
    return state


def _hammer(n=4):
    got = []
    threads = [threading.Thread(target=lambda: got.append(oe._get_easyocr_reader(LANGS)))
               for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not any(t.is_alive() for t in threads), "有人卡住沒回來"
    return got


def test_concurrent_first_use_builds_the_reader_once(monkeypatch):
    state = _fake_easyocr(monkeypatch)
    got = _hammer()
    assert state["max_active"] == 1, (
        f"同時有 {state['max_active']} 個在建 Reader —— 它們會一起寫 temp.zip，"
        "解壓時 Bad CRC-32")
    assert state["built"] == 1, f"模型下載 / 載入了 {state['built']} 次，應該只有一次"
    assert len(got) == 4 and all(r is got[0] for r in got) and got[0] is not None


def test_a_failed_build_is_not_retried_on_every_page(monkeypatch):
    state = _fake_easyocr(monkeypatch, fail=True, delay=0.05)
    for _ in range(5):                      # 一份 5 頁的 PDF
        assert oe._get_easyocr_reader(LANGS) is None
    assert state["built"] == 1, (
        f"下載失敗之後每一頁都重試（{state['built']} 次）—— 慢網路上一頁就是十幾分鐘")


def test_concurrent_callers_during_a_failure_do_not_each_retry(monkeypatch):
    state = _fake_easyocr(monkeypatch, fail=True, delay=0.3)
    got = _hammer()
    assert got == [None] * 4
    assert state["built"] == 1


def test_it_tries_again_after_the_backoff(monkeypatch):
    state = _fake_easyocr(monkeypatch, fail=True, delay=0.0)
    now = [1000.0]
    monkeypatch.setattr(oe.time, "monotonic", lambda: now[0])
    assert oe._get_easyocr_reader(LANGS) is None
    now[0] += oe._EASYOCR_RETRY_AFTER_S + 1
    assert oe._get_easyocr_reader(LANGS) is None
    assert state["built"] == 2, "網路恢復之後要能自己再試，不必重開服務"


def test_a_later_success_clears_the_failure(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(oe.time, "monotonic", lambda: now[0])
    _fake_easyocr(monkeypatch, fail=True, delay=0.0)
    assert oe._get_easyocr_reader(LANGS) is None
    now[0] += oe._EASYOCR_RETRY_AFTER_S + 1
    _fake_easyocr(monkeypatch, fail=False, delay=0.0)
    r = oe._get_easyocr_reader(LANGS)
    assert r is not None and LANGS not in oe._easyocr_failed_at


def test_other_language_sets_are_not_blocked_by_a_failure(monkeypatch):
    state = _fake_easyocr(monkeypatch, fail=True, delay=0.0)
    assert oe._get_easyocr_reader(LANGS) is None
    assert oe._get_easyocr_reader(("en",)) is None
    assert state["built"] == 2, "一組語言失敗不可以連帶讓別組語言也不試"
