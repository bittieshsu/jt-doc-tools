"""事務機排隊：同一台實體設備一次只做一件事（使用者 2026-10-14：「要避免同時多人存取事務機
要排隊機制，以免塞爆事務機作業」）。

* 先來先做；輪到才能連設備，同一台不會有兩件同時在做。
* 每台、每人都有上限，超過直接拒絕（不排、不碰設備）。
* 排隊中取消直接移出；正在做的取消後，在做事的那一方停下來之前，下一位不可以開始。
* 連線測試等不到（設備正在幫別人掃描）就說「正忙」，不卡住。
"""
from __future__ import annotations

import threading
import time

import pytest

from app.core import mfp_queue as q


@pytest.fixture(autouse=True)
def _clean():
    q._reset_for_tests()
    yield
    q._reset_for_tests()


def test_first_come_first_served_and_never_two_at_once():
    order, running, peak = [], [0], [0]
    lock = threading.Lock()
    tickets = [q.enqueue("192.168.1.50", f"u{i}", "scan") for i in range(4)]

    def work(t):
        assert q.wait_turn(t, timeout=10)
        with lock:
            running[0] += 1
            peak[0] = max(peak[0], running[0])
            order.append(t.owner)
        time.sleep(0.05)
        with lock:
            running[0] -= 1
        q.done(t)

    ths = [threading.Thread(target=work, args=(t,)) for t in reversed(tickets)]   # 執行緒反著啟動
    for th in ths:
        th.start()
    for th in ths:
        th.join(10)
    assert order == ["u0", "u1", "u2", "u3"], "要照排隊的順序，不是誰的執行緒先跑"
    assert peak[0] == 1, "同一台設備同時有兩件在做"


def test_different_devices_do_not_wait_for_each_other():
    a = q.enqueue("192.168.1.50", "u1", "scan")
    b = q.enqueue("192.168.1.59", "u1", "scan")
    assert q.wait_turn(a, timeout=0.1) and q.wait_turn(b, timeout=0.1)


def test_positions_and_snapshot():
    a = q.enqueue("d", "u1", "scan")
    b = q.enqueue("d", "u2", "scan")
    c = q.enqueue("d", "u3", "test")
    assert [q.position(x) for x in (a, b, c)] == [0, 1, 2]
    assert q.snapshot("d") == {"busy": True, "waiting": 2, "kind": "scan"}
    q.done(a)
    assert q.position(b) == 0 and q.position(c) == 1 and q.position(a) == -1
    assert q.snapshot("nothing") == {"busy": False, "waiting": 0, "kind": None}


def test_limits_refuse_without_queueing(monkeypatch):
    monkeypatch.setattr(q, "MAX_PER_DEVICE", 3)
    monkeypatch.setattr(q, "MAX_PER_OWNER", 2)
    q.enqueue("d", "u1", "scan")
    q.enqueue("d", "u1", "scan")
    with pytest.raises(q.QueueError) as e:
        q.enqueue("d", "u1", "scan")
    assert e.value.code == "OWNER_QUEUE_FULL"
    q.enqueue("d", "u2", "scan")
    with pytest.raises(q.QueueError) as e:
        q.enqueue("d", "u3", "scan")
    assert e.value.code == "DEVICE_QUEUE_FULL" and e.value.waiting == 3
    assert q.snapshot("d")["waiting"] == 2, "被拒絕的不可以留在隊伍裡"


def test_cancel_while_waiting_leaves_the_queue():
    a = q.enqueue("d", "u1", "scan")
    b = q.enqueue("d", "u2", "scan")
    got = []
    th = threading.Thread(target=lambda: got.append(q.wait_turn(b, timeout=5)))
    th.start()
    time.sleep(0.1)
    q.cancel(b)
    th.join(5)
    assert got == [False]
    assert q.position(b) == -1 and q.snapshot("d")["waiting"] == 0
    q.done(a)


def test_a_cancelled_running_job_still_holds_the_device_until_it_stops():
    a = q.enqueue("d", "u1", "scan")
    assert q.wait_turn(a, timeout=1)            # 開始做了
    b = q.enqueue("d", "u2", "scan")
    q.cancel(a)                                  # 使用者按取消，但設備那邊還沒停
    assert not q.wait_turn(b, timeout=0.3), "前一件還沒停下來，下一件不可以開始"
    b = q.enqueue("d", "u2", "scan")
    q.done(a)                                    # 做事的那一方真的停了
    assert q.wait_turn(b, timeout=1)


def test_turn_times_out_with_device_busy():
    a = q.enqueue("d", "u1", "scan")
    assert q.wait_turn(a, timeout=1)
    t0 = time.monotonic()
    with pytest.raises(q.QueueError) as e:
        with q.turn("d", "u2", "test", timeout=0.3):
            pytest.fail("設備在忙還進得來")
    assert e.value.code == "DEVICE_BUSY" and time.monotonic() - t0 < 2
    assert q.position(a) == 0 and q.snapshot("d")["waiting"] == 0, "等不到的那件要離開隊伍"
    q.done(a)
    with q.turn("d", "u2", "test", timeout=1):
        assert q.snapshot("d")["busy"]
    assert not q.snapshot("d")["busy"], "離開 with 要叫下一位"


def test_on_wait_reports_how_many_are_ahead():
    a = q.enqueue("d", "u1", "scan")
    b = q.enqueue("d", "u2", "scan")
    c = q.enqueue("d", "u3", "scan")
    seen = []
    th = threading.Thread(target=lambda: q.wait_turn(c, timeout=5, on_wait=seen.append, poll=0.05))
    th.start()
    time.sleep(0.2)
    q.done(a)
    time.sleep(0.2)
    q.done(b)
    th.join(5)
    assert 2 in seen and 1 in seen and seen == sorted(seen, reverse=True)
    q.done(c)


def test_every_code_has_a_message_and_translations():
    import json
    from pathlib import Path
    en = json.loads(Path("app/i18n/en.json").read_text(encoding="utf-8"))
    ja = json.loads(Path("app/i18n/ja.json").read_text(encoding="utf-8"))
    for code, msg in q.MESSAGES.items():
        assert msg in en and msg in ja, f"{code} 沒有英日文：{msg}"
