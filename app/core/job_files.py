"""作業「擁有」的暫存檔：「我的作業」的開啟鈕、管理頁的作業結果檔用量。

認人的規則**只有一份**，在 `job_store`（`file_keys` / `owns_file` /
`name_tokens`）—— 清理程式（`retention._sweep_temp_dir`）用同一套決定
「這個檔還不能刪」。這裡拿同一套回答另外兩個問題：

* **「開啟」按下去還打得開嗎？**（`view_ok_map`）
  過了保留期，資料被清掉了，「我的作業」那一列卻還掛著「開啟」，按下去是
  410（v1.16.6 查保留期時記下的待辦）。**不可以拿 `has_result` 判斷** ——
  逐句翻譯的結果是 `trd_<作業編號>.json`、沒有設 `result_path`，它的
  `has_result` 永遠是 false，照那個條件改的話逐句翻譯的「開啟」會整個消失。

* **作業的檔案實際佔多少空間？**（`usage`）
  管理頁的「作業結果檔」原本量的是 `data/jobs/` —— 全站沒有任何一支工具把
  結果放在那裡，所以那一列永遠是 0 MB。真正的作業檔案在 `data/temp/`，
  照作業的保留期留著（見 `job_store.keep_alive_keys`）。

**不靠每支工具自己登記**：下一支新工具一定會漏。工具只要照慣例把作業編號
或 `upload_id` 嵌進暫存檔名、把 `upload_id` 記進 `job.meta`，這裡就認得。
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from . import job_store

#: 歸屬紀錄的子目錄（`upload_owner` 寫的 `<temp>/.owners/<upload_id>.json`）
_OWNERS = ".owners"


@dataclass
class TempIndex:
    """暫存區此刻有哪些東西（只列一次，給整份清單共用）。"""
    #: 頂層的檔名（含子目錄名，`.owners` 除外）
    names: set[str] = field(default_factory=set)
    #: 頂層檔名裡出現過的 32 碼識別碼
    tokens: set[str] = field(default_factory=set)
    #: `.owners/` 裡有歸屬紀錄的 upload_id
    owners: set[str] = field(default_factory=set)

    def has_data(self, ids: set[str], names: set[str]) -> bool:
        """`(ids, names)` 這件作業在暫存區還有沒有東西。

        跟 `job_store.owns_file` 是同一條規則（檔名相同，或檔名裡帶著其中
        一個識別碼），只是倒過來查：先把整個目錄的識別碼收起來再比對。
        """
        return bool(names & self.names) or bool(ids & self.tokens)


def _temp_dir() -> Path:
    from ..config import settings
    return settings.temp_dir


def temp_index() -> TempIndex:
    """把暫存區列一次。讀不到（還沒建、權限）就當成空的 —— 不可以讓清單壞掉。"""
    idx = TempIndex()
    d = _temp_dir()
    try:
        with os.scandir(d) as it:
            for e in it:
                if e.name == _OWNERS:
                    continue
                idx.names.add(e.name)
                idx.tokens.update(job_store.name_tokens(e.name))
    except OSError:
        return idx
    try:
        with os.scandir(d / _OWNERS) as it:
            for e in it:
                stem = e.name[:-5] if e.name.endswith(".json") else ""
                if stem and job_store.name_tokens(stem) == [stem]:
                    idx.owners.add(stem)
    except OSError:
        pass
    return idx


def _auth_enabled() -> bool:
    try:
        from . import auth_settings as _as
        return bool(_as.is_enabled())
    except Exception:  # noqa: BLE001 — 讀不到設定時照「沒啟用」處理（不擋按鈕）
        return False


def view_ok(row: dict, idx: TempIndex, *, status: Optional[str] = None,
            auth_on: bool = False) -> bool:
    """這一列的「開啟」按下去還打得開嗎？

    `row` 是 `job_store.list_jobs()` 的一列（`meta` 已經解開）。

    * 沒有 `view_url` → 沒有「開啟」可言。
    * 還在排隊 / 執行中 → 打得開（頁面是去看進度）。
    * **「開啟」指的頁面不靠這件作業的暫存檔** → 打得開。判準是網址裡有沒有
      這件作業的識別碼（作業編號或 `upload_id`）：`?job=<作業編號>` 那種頁面
      要從暫存區把結果讀回來；送件前檢核的案件頁、知識庫管理頁是按自己的
      資料定址的，資料有自己的保存方式，不跟著暫存區的保留期走。
    * 其餘：結果檔還在，或暫存區還有帶著它識別碼的檔案 → 打得開。
      頁面是用 `upload_id` 讀資料的話（會經過 `upload_owner` 的歸屬檢查），
      啟用認證時**歸屬紀錄也要還在** —— 資料留著、紀錄沒了，一般使用者
      按下去是 403，一樣是一顆打不開的鈕。
    """
    meta = row.get("meta") or {}
    url = meta.get("view_url")
    if not isinstance(url, str) or not url:
        return False
    st = status or row.get("status")
    if st not in job_store.TERMINAL:
        return True
    jid = str(row.get("id") or "")
    ids, names = job_store.file_keys(jid, meta, row.get("result_path"))
    if not (set(job_store.name_tokens(url)) & ids):
        return True
    rp = row.get("result_path")
    have = idx.has_data(ids, names)
    if not have and rp:
        try:
            have = Path(str(rp)).is_file()
        except OSError:
            have = False
    if not have:
        return False
    uid = str(meta.get("upload_id") or "")
    if auth_on and uid in ids and uid not in idx.owners:
        return False
    return True


def view_ok_map(rows: Iterable[dict],
                statuses: Optional[dict[str, str]] = None) -> dict[str, bool]:
    """整份清單的 `{作業編號: 開啟打得開嗎}`。

    暫存區只列**一次**（而且只在真的有一列需要查時才列）——「我的作業」有
    作業在跑時每 2 秒輪詢一次，每一列各列一次目錄的話，暫存檔一多就慢。
    `statuses` 是記憶體裡的即時狀態（資料庫的狀態會晚一步）。
    """
    out: dict[str, bool] = {}
    idx: Optional[TempIndex] = None
    auth_on: Optional[bool] = None
    for r in rows:
        jid = str(r.get("id") or "")
        st = (statuses or {}).get(jid) or r.get("status")
        meta = r.get("meta") or {}
        if not meta.get("view_url") or st not in job_store.TERMINAL:
            out[jid] = view_ok(r, TempIndex(), status=st)
            continue
        if idx is None:
            idx = temp_index()
        if auth_on is None:
            auth_on = _auth_enabled()
        out[jid] = view_ok(r, idx, status=st, auth_on=auth_on)
    return out


# ---------- 管理頁的用量 ----------

@dataclass
class _Acc:
    bytes: int = 0
    files: int = 0
    oldest: Optional[float] = None

    def add(self, size: int, mtime: float) -> None:
        self.bytes += size
        self.files += 1
        if self.oldest is None or mtime < self.oldest:
            self.oldest = mtime

    def as_dict(self) -> dict[str, Any]:
        hours = None if self.oldest is None else \
            max(0.0, (time.time() - self.oldest) / 3600.0)
        return {"size_mb": self.bytes / 1024 / 1024, "files": self.files,
                "oldest_hours": hours,
                "oldest_days": None if hours is None else hours / 24.0}


def _walk(p: Path, acc: _Acc) -> None:
    """一個暫存項目（檔案，或整個子目錄）算進 `acc`。"""
    try:
        if p.is_dir() and not p.is_symlink():
            for root, _dirs, files in os.walk(p):
                for f in files:
                    try:
                        st = (Path(root) / f).stat()
                        acc.add(st.st_size, st.st_mtime)
                    except OSError:
                        pass
            return
        st = p.stat()
        acc.add(st.st_size, st.st_mtime)
    except OSError:
        pass


def usage(jobs_seconds: int) -> dict[str, dict[str, Any]]:
    """作業擁有的檔案 vs. 其他暫存檔，各佔多少。

    回 `{"jobs": …, "temp": …}`，各自有 `size_mb` / `files` / `oldest_hours`。

    * `jobs` —— 保留期內（或還沒結束）的作業擁有的檔案：結果檔、「開啟」要讀
      的資料、歸屬紀錄（**跟清理程式留下來的是同一批**，見 `keep_alive_keys`），
      再加上 `data/jobs/` 裡的東西（那個目錄就是作業結果檔的位置）。
    * `temp` —— 暫存區其餘的檔案（上傳的原檔、預覽圖…），照暫存的保留期清。

    兩者加起來就是暫存區＋`data/jobs/` 的總量，**不會重複計算**：
    原本「暫存」量整個 `data/temp/`、「作業結果檔」量 `data/jobs/`，作業的檔案
    明明照作業的保留期走，卻被算在暫存那一列。
    """
    from ..config import settings
    since = time.time() - jobs_seconds if jobs_seconds and jobs_seconds > 0 else 0.0
    ids, names = job_store.keep_alive_keys(since)
    jobs, other = _Acc(), _Acc()
    t = settings.temp_dir
    try:
        entries = list(t.iterdir()) if t.is_dir() else []
    except OSError:
        entries = []
    for child in entries:
        if child.name == _OWNERS and child.is_dir():
            try:
                owners = list(child.iterdir())
            except OSError:
                owners = []
            for f in owners:
                _walk(f, jobs if job_store.owns_file(f.name, ids, names) else other)
            continue
        _walk(child, jobs if job_store.owns_file(child.name, ids, names) else other)
    jd = settings.jobs_dir
    try:
        if jd.is_dir():
            for child in jd.iterdir():
                _walk(child, jobs)
    except OSError:
        pass
    return {"jobs": jobs.as_dict(), "temp": other.as_dict()}
