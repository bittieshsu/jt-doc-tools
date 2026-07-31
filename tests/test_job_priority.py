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


def _submit_as(mgr, tool_id: str, owner_id, priority_ids):
    """以某位使用者的身分送出一件作業。

    `priority_ids` 給**有順序的**清單（第一個最優先）；給 set 也可以，
    那代表「都在名單裡但不在意誰前誰後」。

    走的是**真正的路徑**：用 `set_current_actor` 設定「這個請求是誰」（中介層在
    正式環境做的事），再讓 `job_priority.rank_of` 去查名單。這樣測到的才是
    submit 自己的判斷，不是測試自己排好順序再塞進去。
    """
    from app.core import job_manager as jm
    ordered = list(priority_ids)
    orig = job_priority.rank_of
    job_priority.rank_of = (
        lambda oid: ordered.index(oid) if oid in ordered else None)
    try:
        jm.set_current_actor({"user_id": owner_id, "username": f"u{owner_id}"},
                             client_ip="127.0.0.1")
        return mgr.submit(tool_id, lambda j: None)
    finally:
        job_priority.rank_of = orig
        jm.set_current_actor(None)


def _queue(mgr):
    with mgr._lock:
        return list(mgr._pending)


# ------------------------------------------------------------- 佇列順序

def test_priority_job_jumps_ahead_of_normal_jobs(paused_mgr):
    m = paused_mgr
    a = _submit_as(m, "pdf-merge", 1, [])
    b = _submit_as(m, "pdf-merge", 2, [])
    vip = _submit_as(m, "pdf-merge", 9, [9])
    assert _queue(m)[0] == vip.id, "優先作業沒有排到最前面"
    assert _queue(m)[1:] == [a.id, b.id], "一般作業之間的順序被動到了"


def test_same_rank_keeps_fifo(paused_mgr):
    """同一位優先使用者的多件作業之間照先來後到。

    直接插到最前面（appendleft）會讓後送出的那件跑到自己先送出的那件前面 ——
    變成後進先出，那是壞掉不是功能。
    """
    m = paused_mgr
    normal = _submit_as(m, "pdf-merge", 1, [])
    a = _submit_as(m, "pdf-merge", 9, [9])
    b = _submit_as(m, "pdf-merge", 9, [9])
    c = _submit_as(m, "pdf-merge", 9, [9])
    assert _queue(m) == [a.id, b.id, c.id, normal.id]


def test_higher_ranked_user_jumps_ahead_of_lower_ranked(paused_mgr):
    """名單的順序就是優先順序 —— 排前面的使用者要壓過排後面的。

    沒有這一條的話「插隊」只有一級，董事長跟部門主管會互相卡（先送出的先跑），
    管理員排的順序等於沒有作用。
    """
    m = paused_mgr
    ORDER = [9, 10]                      # 9 是第 1 位、10 是第 2 位
    normal = _submit_as(m, "pdf-merge", 1, ORDER)
    low = _submit_as(m, "pdf-merge", 10, ORDER)     # 先送出，但排名較後
    high = _submit_as(m, "pdf-merge", 9, ORDER)     # 後送出，但排名較前
    assert _queue(m) == [high.id, low.id, normal.id]


def test_lower_ranked_never_jumps_over_higher_ranked(paused_mgr):
    m = paused_mgr
    ORDER = [9, 10]
    high = _submit_as(m, "pdf-merge", 9, ORDER)
    low = _submit_as(m, "pdf-merge", 10, ORDER)
    assert _queue(m) == [high.id, low.id]


def test_normal_job_never_jumps(paused_mgr):
    m = paused_mgr
    vip = _submit_as(m, "pdf-merge", 9, [9])
    normal = _submit_as(m, "pdf-merge", 1, [9])
    assert _queue(m) == [vip.id, normal.id]


def test_queue_positions_reflect_the_jump(paused_mgr):
    """使用者看到的號碼要跟真正的派送順序一致。"""
    m = paused_mgr
    a = _submit_as(m, "pdf-merge", 1, [])
    vip = _submit_as(m, "pdf-merge", 9, [9])
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
    orig = jm._priority_rank_of
    jm._priority_rank_of = lambda oid: 0
    try:
        m.submit("pdf-merge", lambda j: None)
    finally:
        jm._priority_rank_of = orig
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
    # 順序要原樣保留 —— 它就是優先順序
    assert job_priority.get_ordered() == [3, 7, 9]
    assert job_priority.rank_of(3) == 0 and job_priority.rank_of(9) == 2
    assert job_priority.rank_of(8) is None
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
    assert "_priority_rank_of(job.owner_id)" in src, \
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


def test_reordering_the_list_does_not_reshuffle_queued_jobs(paused_mgr,
                                                            monkeypatch,
                                                            tmp_path):
    """名單改順序時，**已經在排隊**的作業不該換位置。

    排名是送出當下記在作業上的。若每次派送都重查名單，管理員拖一下順序，
    別人已經排好的號碼就會往後跳 —— 使用者看到的是「我明明排第 2，怎麼變第 5」。
    """
    from app.config import settings
    from app.core import auth_settings
    monkeypatch.setattr(auth_settings, "is_enabled", lambda: True)
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    job_priority.invalidate_cache()
    job_priority.set_user_ids([9, 10])

    m = paused_mgr
    from app.core import job_manager as jm
    jm.set_current_actor({"user_id": 9, "username": "u9"}, client_ip="1.1.1.1")
    first = m.submit("pdf-merge", lambda j: None)
    jm.set_current_actor({"user_id": 10, "username": "u10"}, client_ip="1.1.1.1")
    second = m.submit("pdf-merge", lambda j: None)
    jm.set_current_actor(None)
    assert _queue(m) == [first.id, second.id]

    # 管理員把順序倒過來 —— 已排隊的兩件不受影響
    job_priority.set_user_ids([10, 9])
    assert _queue(m) == [first.id, second.id]
    assert m.get(first.id).priority_rank == 0


def test_admin_api_returns_the_list_in_priority_order(monkeypatch, tmp_path):
    """管理 API 讀回名單時要照**名單順序**，不是照使用者編號。

    原本寫成 `sorted(get_user_ids())` —— 拖好的順序在讀回時被整個洗掉，
    畫面上永遠是按編號排的（端對端測試才抓到，單看程式碼很像沒事）。
    """
    import re
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent
           / "app" / "admin" / "auth_router.py").read_text(encoding="utf-8")
    m = re.search(r"async def jobs_api_priority_users\(.*?\n(.*?)\n    @router",
                  src, re.S)
    assert m, "找不到 priority-users 端點"
    body = m.group(1)
    assert "get_ordered()" in body, "讀回名單沒有照順序"
    assert "sorted(job_priority" not in body, \
        "用 sorted() 讀名單會把管理員拖好的順序洗掉"


def test_picker_shows_which_realm_each_account_belongs_to():
    """挑人時一定要看得出是哪一種認證。

    使用者回報：「本機有 jason，LDAP 也有 jason，不知是哪一個」。同一個
    username 可以同時存在於多個來源（`UNIQUE(username, source)`），只顯示帳號
    就會挑錯人 —— 而挑錯人的後果是「別人的作業一直插到我前面」，還很難查。

    全站的寫法是 `username@來源`（見 `sessions.user_label`），這裡跟著用。
    """
    from pathlib import Path
    tpl = (Path(__file__).resolve().parent.parent
           / "app" / "admin" / "templates" / "admin_jobs.html").read_text(
        encoding="utf-8")
    assert "prioWho" in tpl, "挑人的下拉沒有顯示來源"
    assert "`${u.username}@${u.source}`" in tpl, \
        "來源沒有用全站通用的 username@來源 寫法"
    # 名單列與搜尋結果兩邊都要標
    assert tpl.count("prioWho(u)") >= 2, "名單列或搜尋下拉少了一邊"
    assert "SRC_LABEL" in tpl, "沒有來源的中文標籤"


def test_max_users_is_a_small_number():
    """名單是「少數例外」的機制。

    上限太大就等於沒有優先順序可言（一般使用者永遠排最後），而且順序要一個一個
    拖，名單太長根本排不動。使用者指定 15。
    """
    assert job_priority.MAX_USERS == 15
