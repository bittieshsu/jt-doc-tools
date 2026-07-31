"""優先派送名單 —— 指定的使用者送出的作業會插到佇列最前面。

## 由來

使用者要求：「請加入插隊或稱為優先派送功能，管理員可以指定幾個 user，當他們的
工作要進行背景時，優先插隊在下一個」，並補充「通常用在高階主管或重要工作人士」。

## 這份要守住的四件事

1. **會插隊** —— 名單內的人送出時排到一般作業前面。
2. **不搶跑** —— 已經在跑的作業不受影響（轉檔跑到一半殺掉只會留下半成品，
   原本那個人也白等）。插隊的效果是「下一個換你」。
3. **名單內的人彼此照先來後到** —— 插到最前面時要插在**已經排在前面的其他優先
   作業之後**。直接 appendleft 會讓同一群人之間變成後進先出，那是壞掉不是功能。
4. **身分只由伺服器決定** —— 不看任何請求參數。讓前端傳得動 `priority=1`
   就等於開放所有人插隊。
"""
from __future__ import annotations

import threading
import time

import pytest

from app.core import job_priority
from app.core.job_manager import JobManager


@pytest.fixture
def paused_mgr():
    """一個暫停派送的管理器 —— 這樣才觀察得到佇列順序本身。"""
    m = JobManager(workers=1)
    m.set_paused(True)
    yield m
    m.set_paused(False)


def _submit_as(mgr, tool_id: str, owner_id, priority_ids: set[int]):
    """以某位使用者的身分送出一件作業。

    走的是**真正的路徑**：用 `set_current_actor` 設定「這個請求是誰」（中介層在
    正式環境做的事），再讓 `job_priority.is_priority` 去查名單。這樣測到的才是
    submit 自己的判斷，不是測試自己排好順序再塞進去。
    """
    from app.core import job_manager as jm
    orig = job_priority.is_priority
    job_priority.is_priority = lambda oid: oid in priority_ids
    try:
        jm.set_current_actor({"user_id": owner_id, "username": f"u{owner_id}"},
                             client_ip="127.0.0.1")
        return mgr.submit(tool_id, lambda j: None)
    finally:
        job_priority.is_priority = orig
        jm.set_current_actor(None)


def _queue(mgr):
    with mgr._lock:
        return list(mgr._pending)


# ------------------------------------------------------------- 佇列順序

def test_priority_job_jumps_ahead_of_normal_jobs(paused_mgr):
    m = paused_mgr
    a = _submit_as(m, "pdf-merge", 1, set())
    b = _submit_as(m, "pdf-merge", 2, set())
    vip = _submit_as(m, "pdf-merge", 9, {9})
    assert _queue(m)[0] == vip.id, "優先作業沒有排到最前面"
    assert _queue(m)[1:] == [a.id, b.id], "一般作業之間的順序被動到了"


def test_priority_jobs_keep_fifo_among_themselves(paused_mgr):
    """同一群優先使用者之間仍照先來後到。

    直接插到最前面（appendleft）會讓後送出的主管跑到先送出的主管前面 ——
    同一群人之間變成後進先出，那是壞掉不是功能。
    """
    m = paused_mgr
    normal = _submit_as(m, "pdf-merge", 1, set())
    vip1 = _submit_as(m, "pdf-merge", 9, {9, 10})
    vip2 = _submit_as(m, "pdf-merge", 10, {9, 10})
    vip3 = _submit_as(m, "pdf-merge", 9, {9, 10})
    assert _queue(m) == [vip1.id, vip2.id, vip3.id, normal.id]


def test_normal_job_never_jumps(paused_mgr):
    m = paused_mgr
    vip = _submit_as(m, "pdf-merge", 9, {9})
    normal = _submit_as(m, "pdf-merge", 1, {9})
    assert _queue(m) == [vip.id, normal.id]


def test_queue_positions_reflect_the_jump(paused_mgr):
    """使用者看到的號碼要跟真正的派送順序一致。"""
    m = paused_mgr
    a = _submit_as(m, "pdf-merge", 1, set())
    vip = _submit_as(m, "pdf-merge", 9, {9})
    pos = m.queue_positions()
    assert pos[vip.id] == 1 and pos[a.id] == 2


def test_running_job_is_not_preempted():
    """插隊不會中斷正在跑的作業。"""
    m = JobManager(workers=1)
    started = threading.Event()
    release = threading.Event()

    def slow(job):
        started.set()
        release.wait(timeout=10)

    running = m.submit("pdf-merge", slow)
    assert started.wait(timeout=10), "第一件作業沒有開始"
    from app.core import job_manager as jm
    orig = jm._is_priority_owner
    jm._is_priority_owner = lambda oid: True
    try:
        m.submit("pdf-merge", lambda j: None)
    finally:
        jm._is_priority_owner = orig
    time.sleep(0.2)
    assert m.get(running.id).status == "running", "正在跑的作業被搶掉了"
    release.set()


# ------------------------------------------------------------- 名單本身

def test_list_is_empty_when_auth_is_off(monkeypatch, tmp_path):
    """認證關閉時沒有帳號可指定 —— 名單一律視為空的。"""
    from app.core import auth_settings
    monkeypatch.setattr(auth_settings, "is_enabled", lambda: False)
    job_priority.invalidate_cache()
    assert job_priority.get_user_ids() == set()
    assert job_priority.is_priority(1) is False


def test_set_and_get(monkeypatch, tmp_path):
    from app.core import auth_settings
    from app.config import settings
    monkeypatch.setattr(auth_settings, "is_enabled", lambda: True)
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    job_priority.invalidate_cache()
    job_priority.set_user_ids([3, 7, 3, "9", "x", -1])
    assert job_priority.get_user_ids() == {3, 7, 9}
    assert job_priority.is_priority(7) is True
    assert job_priority.is_priority(8) is False
    assert job_priority.is_priority(None) is False


def test_list_is_capped(monkeypatch, tmp_path):
    """名單一長就等於沒有優先順序 —— 反而讓一般使用者永遠排最後。"""
    from app.core import auth_settings
    from app.config import settings
    monkeypatch.setattr(auth_settings, "is_enabled", lambda: True)
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    job_priority.invalidate_cache()
    saved = job_priority.set_user_ids(list(range(1, 500)))
    assert len(saved) == job_priority.MAX_USERS


def test_broken_list_file_degrades_to_normal(monkeypatch, tmp_path):
    """名單檔壞掉時要降級成一般作業，不可以讓人送不出工作。"""
    from app.core import auth_settings
    from app.config import settings
    monkeypatch.setattr(auth_settings, "is_enabled", lambda: True)
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    (tmp_path / "job_priority.json").write_text("{ 壞掉的", encoding="utf-8")
    job_priority.invalidate_cache()
    assert job_priority.get_user_ids() == set()


def test_priority_never_comes_from_the_request():
    """身分只從伺服器端的擁有者判斷，不吃任何請求參數。

    這是安全界線 —— 只要前端傳得動，就等於開放所有人插隊。
    """
    import inspect

    from app.core import job_manager as jm
    src = inspect.getsource(jm.JobManager.submit)
    assert "_is_priority_owner(job.owner_id)" in src, \
        "submit 判斷優先權的依據不是伺服器端決定的 owner_id"
    # meta 是呼叫端（工具）給的，絕不可以拿它當依據
    assert 'meta.get("priority"' not in src
    assert 'meta["priority"]' not in src


# --------------------------------------------------------- 共用資源標籤

def test_ocr_and_remote_tool_lists_match_the_code():
    """OCR / 外部服務的清單是人工維護的 —— 用實際掃描結果比對。

    使用者問「有用到 office 引擎的有標籤，那用到 OCR / LLM 之類的呢」。加了標籤
    之後，清單漏一個的症狀就是「這個作業明明在等 GPU，畫面上卻看不出來」。
    """
    import re
    from pathlib import Path

    from app.core.concurrency_settings import OCR_TOOL_IDS, REMOTE_TOOL_IDS

    ocr_re = re.compile(r"ocr_engine|recognize_image|pytesseract|easyocr")
    rmt_re = re.compile(r"llm_settings|llm_client|remote_limit")
    root = Path(__file__).resolve().parent.parent / "app" / "tools"

    # 套件目錄名**不等於**工具 id：`pdf_diff` 這個目錄的工具 id 是 `doc-diff`
    # （v1.1.61 改名時目錄沒跟著改）。拿目錄名硬換成 id 會憑空生出一個不存在的
    # 工具，然後說它沒列進清單 —— 要走註冊表拿真正的 id。
    from app.tool_registry import discover_tools
    dir_to_id = {}
    for t in discover_tools():
        td = getattr(t, "templates_dir", None)
        name = Path(td).parent.name if td else t.metadata.id.replace("-", "_")
        dir_to_id[name] = t.metadata.id

    ocr, remote = set(), set()
    for pkg in root.iterdir():
        if not pkg.is_dir():
            continue
        tool_id = dir_to_id.get(pkg.name)
        if not tool_id:
            continue          # 停用或不是工具的目錄
        blob = "\n".join(f.read_text(encoding="utf-8", errors="ignore")
                         for f in pkg.rglob("*.py"))
        if ocr_re.search(blob):
            ocr.add(tool_id)
        if rmt_re.search(blob):
            remote.add(tool_id)
    assert ocr <= set(OCR_TOOL_IDS), \
        f"這些工具會做 OCR 但沒列進 OCR_TOOL_IDS：{sorted(ocr - set(OCR_TOOL_IDS))}"
    assert remote <= set(REMOTE_TOOL_IDS), \
        (f"這些工具會呼叫外部服務但沒列進 REMOTE_TOOL_IDS："
         f"{sorted(remote - set(REMOTE_TOOL_IDS))}")


def test_resource_tags_can_be_multiple():
    """一個工具可以同時吃多種資源（OCR 文字辨識可走本機也可走遠端 GPU）。"""
    from app.core.concurrency_settings import resource_tags
    assert resource_tags("pdf-ocr") == ["ocr", "remote"]
    assert resource_tags("pdf-to-slides") == ["office"]
    assert resource_tags("pdf-merge") == []
