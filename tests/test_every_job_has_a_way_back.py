"""每一件背景作業都要有回到結果的路徑（下載，或「開啟」）。

## 由來（使用者 2026-09-19：「從我的作業 點了開啟 但是進去後 下面沒東西」
## ＋「其它工具可能也有類似問題 明天安排時間查一下」）

「我的作業」那一列只有兩個出口：**下載**（靠 `job.result_path`）與
**開啟**（靠 `job.meta["view_url"]`）。兩個都沒有的話，那一列會顯示「完成」
卻什麼都按不了 —— 使用者得自己想到要回哪支工具、怎麼找回那筆結果。

實算抓到一支：**送件前檢核**。它的產出不是一個檔案而是案件底下的一份報告，
所以本來就沒有 `result_path`，而當時也沒有 `view_url`。

> **判準走 AST 不走字串比對**：第一版我用 `"view_url" in src` 掃，結果
> **`preview_url` 被當成 `view_url`**（子字串），七支工具被誤判成「有開啟」。
> 本專案在前綴 / 子字串比對上已經吃過好幾次虧。
"""
from __future__ import annotations

import ast
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
TOOLS = ROOT / "app" / "tools"


def _routers() -> list[pathlib.Path]:
    return sorted(p for p in TOOLS.glob("*/router.py") if p.is_file())


def _submits_jobs(tree: ast.AST) -> bool:
    """`job_manager.submit(...)` —— 兩種 import 寫法都要認得。

    第一版只認 `_jm.job_manager.submit`（`Attribute`），而多數工具是
    `from ... import job_manager` 之後直接 `job_manager.submit`（`Name`）
    —— 29 支只掃到 3 支。**「先證明掃得到東西」那條當場抓到**；
    沒有它的話這份守門會靜靜地只檢查三支工具，而且全綠。
    """
    for n in ast.walk(tree):
        if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "submit"):
            continue
        recv = n.func.value
        if isinstance(recv, ast.Name) and recv.id == "job_manager":
            return True
        if isinstance(recv, ast.Attribute) and recv.attr == "job_manager":
            return True
    return False


def _ways_back(tree: ast.AST) -> set[str]:
    """找「指派給 result_path」與「塞進 meta 的 view_url」—— 節點，不是字串。"""
    found: set[str] = set()
    for n in ast.walk(tree):
        if not isinstance(n, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            continue
        targets = n.targets if isinstance(n, ast.Assign) else [n.target]
        for t in targets:
            if isinstance(t, ast.Attribute) and t.attr == "result_path":
                found.add("result_path")
            if (isinstance(t, ast.Subscript)
                    and isinstance(t.slice, ast.Constant)
                    and t.slice.value == "view_url"):
                found.add("view_url")
    # 有些工具是在 submit 時就把 view_url 放進 meta= 字典裡
    for n in ast.walk(tree):
        if isinstance(n, ast.Dict):
            for k in n.keys:
                if isinstance(k, ast.Constant) and k.value == "view_url":
                    found.add("view_url")
    return found


def _tool_trees():
    for p in _routers():
        tree = ast.parse(p.read_text(encoding="utf-8"))
        if _submits_jobs(tree):
            yield p, tree


def test_the_scan_actually_reaches_the_job_submitting_tools():
    """**先證明掃得到東西。** 掃 0 支跟「全部合格」在 pytest 輸出裡一模一樣。"""
    names = [p.parent.name for p, _ in _tool_trees()]
    assert len(names) >= 25, f"只掃到 {len(names)} 支走背景作業的工具：{names}"


@pytest.mark.parametrize("path", [p for p, _ in _tool_trees()],
                         ids=lambda p: p.parent.name)
def test_every_background_job_offers_a_way_back(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    ways = _ways_back(tree)
    assert ways, (
        f"{path.parent.name} 送出背景作業，但既沒有 `result_path` 也沒有 "
        f"`meta[\"view_url\"]` —— 「我的作業」那一列會顯示「完成」卻什麼都按不了。"
        f"產出是檔案就設 `result_path`；產出是一個畫面就設 `view_url`。"
    )


def test_preview_url_is_not_mistaken_for_view_url():
    """判準不可以退回子字串比對。

    `preview_url` 含有 `view_url` —— 用 `in` 掃的話，**七支只有預覽圖的工具
    會被判成「有開啟」**，這條守門就整個失去意義。這裡直接拿一段程式碼試。
    """
    tree = ast.parse('x = {}\nx["preview_url"] = "/p.png"\n')
    assert _ways_back(tree) == set(), "preview_url 被誤判成 view_url 了"
    tree = ast.parse('job.meta["view_url"] = "/tools/x/"\n')
    assert _ways_back(tree) == {"view_url"}


def test_view_url_set_after_submit_actually_reaches_the_jobs_list(tmp_path, monkeypatch):
    """**判準要落在「我的作業」真的拿得到的資料上。**

    `job.meta["view_url"] = …` 是在 `submit()` **之後**才設的，而清單是從
    資料庫的列組出來的 —— 兩者之間隔著一次 upsert。只驗「程式碼裡有那一行」
    的話，萬一哪天 meta 的持久化時機改了，這條照樣全綠而按鈕已經不見了。
    """
    import time
    from app.config import settings
    monkeypatch.setattr(settings, "data_dir", tmp_path, raising=False)

    from app.core import job_store
    from app.core.job_manager import job_manager

    job_store.init()
    done = []

    def _fn(job):
        done.append(1)

    job = job_manager.submit("submission-check", _fn, meta={"case_id": "c1"})
    job.meta["view_url"] = "/tools/submission-check/case/c1"

    for _ in range(200):
        rows = [r for r in job_store.list_jobs(limit=50) if r["id"] == job.id]
        if rows and (rows[0].get("meta") or {}).get("view_url"):
            break
        time.sleep(0.05)
    else:
        pytest.fail("view_url 沒有進到作業清單的資料裡 —— 「開啟」不會出現")

    from app.main import _safe_view_url
    assert _safe_view_url("/tools/submission-check/case/c1") == \
        "/tools/submission-check/case/c1", "被安全檢查擋掉了"
