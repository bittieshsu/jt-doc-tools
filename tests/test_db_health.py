"""SQLite 完整性檢查、熱備份與復原。

在這之前五個資料庫都**沒有任何**毀損防護：沒有完整性檢查、沒有備份、沒有復原
管道。`auth.sqlite` 壞掉的後果是所有人都無法登入，而密碼無法從別處重建 ——
這是全專案資料遺失風險最高的一塊。

測試的重點不在「快樂路徑能備份」，而在**兩個會把救命索剪斷的情境**：

* `test_corrupt_db_is_not_backed_up` —— 壞掉的資料庫不可以覆蓋既有備份，
  否則輪替幾輪之後好的備份會被壞的擠掉。
* `test_failed_backup_does_not_delete_existing` —— 備份失敗時只能清自己的暫存
  檔。第一版直接刪目標檔，撞名失敗時把上一份好的備份也刪了（實測踩到）。
"""
from __future__ import annotations

import sqlite3
import time

import pytest

from app.core import db_health


@pytest.fixture(autouse=True)
def _data_dir(tmp_path, monkeypatch):
    d = tmp_path / "data"
    d.mkdir()
    monkeypatch.setattr("app.config.settings.data_dir", d)
    return d


def _make_db(path, rows=4000):
    """建一個**主檔夠大**的資料庫。

    WAL 模式下資料先進 `-wal`，主檔可能只有 4 KB —— 不做 checkpoint 就去「打壞
    中段」其實是寫在空白處，測不到東西（寫這個測試時先踩了一次，毀損沒被抓到
    還以為是檢查失效）。
    """
    c = sqlite3.connect(str(path))
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, name TEXT, pad TEXT)")
    c.executemany("INSERT INTO t(name, pad) VALUES(?,?)",
                  [(f"user{i}", "x" * 300) for i in range(rows)])
    c.commit()
    c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    c.close()
    return path


def _corrupt(path, where=None):
    size = path.stat().st_size
    with open(path, "r+b") as f:
        f.seek(where if where is not None else size // 2)
        f.write(b"\xff" * 4096)


# ---------------- 完整性檢查 ----------------

def test_healthy_db_passes(_data_dir):
    p = _make_db(_data_dir / "auth.sqlite")
    r = db_health.check_one(p)
    assert r["ok"] is True and r["detail"] == "正常"


def test_missing_db_is_not_an_error(_data_dir):
    """尚未建立 ≠ 毀損（全新安裝時大部分資料庫都還不存在）。"""
    r = db_health.check_one(_data_dir / "nope.sqlite")
    assert r["ok"] is True and r["exists"] is False


def test_corruption_is_detected(_data_dir):
    p = _make_db(_data_dir / "auth.sqlite")
    _corrupt(p)
    r = db_health.check_one(p)
    assert r["ok"] is False
    assert r["detail"] and "正常" not in r["detail"]


def test_truncated_db_is_detected(_data_dir):
    p = _make_db(_data_dir / "auth.sqlite")
    with open(p, "r+b") as f:
        f.truncate(p.stat().st_size // 2)
    assert db_health.check_one(p)["ok"] is False


def test_garbage_file_is_detected(_data_dir):
    p = _data_dir / "auth.sqlite"
    p.write_bytes(b"this is definitely not a database" * 50)
    assert db_health.check_one(p)["ok"] is False


def test_detail_has_no_newlines(_data_dir):
    """`integrity_check` 的訊息帶換行，直接印會把 CLI 表格與網頁排版打散。"""
    p = _make_db(_data_dir / "auth.sqlite")
    _corrupt(p, where=4096 + 50)
    r = db_health.check_one(p, thorough=True)
    assert "\n" not in r["detail"] and "\r" not in r["detail"]


def test_check_does_not_modify_the_file(_data_dir):
    """檢查要唯讀 —— 不可在壞檔上觸發回復動作而讓情況更糟。"""
    p = _make_db(_data_dir / "auth.sqlite")
    before = (p.stat().st_size, p.stat().st_mtime_ns)
    db_health.check_one(p, thorough=True)
    assert (p.stat().st_size, p.stat().st_mtime_ns) == before


def test_startup_check_never_raises(_data_dir):
    """一個壞掉的稽核 DB 不該讓整個服務起不來。"""
    _corrupt(_make_db(_data_dir / "audit.sqlite"))
    rows = db_health.startup_check()          # 不可丟例外
    audit = next(r for r in rows if r["file"] == "audit.sqlite")
    assert audit["ok"] is False


# ---------------- 備份 ----------------

def test_backup_creates_usable_copy(_data_dir):
    _make_db(_data_dir / "auth.sqlite")
    rep = db_health.backup_all()
    assert rep["created"]
    b = db_health.list_backups("auth.sqlite")[0]
    assert db_health.check_one(b, thorough=True)["ok"]
    n = sqlite3.connect(str(b)).execute("SELECT COUNT(*) FROM t").fetchone()[0]
    assert n == 4000, "備份內容不完整"


def test_backup_captures_uncheckpointed_wal(_data_dir):
    """直接複製 `.sqlite` 會漏掉還在 `-wal` 裡的最新交易；`VACUUM INTO` 不會。"""
    p = _make_db(_data_dir / "auth.sqlite")
    c = sqlite3.connect(str(p))
    c.execute("INSERT INTO t(name, pad) VALUES('剛剛才寫入的', 'y')")
    c.commit()                      # 不做 checkpoint，資料還在 -wal
    c.close()
    db_health.backup_all()
    b = db_health.list_backups("auth.sqlite")[0]
    got = sqlite3.connect(str(b)).execute(
        "SELECT COUNT(*) FROM t WHERE name='剛剛才寫入的'").fetchone()[0]
    assert got == 1, "備份漏掉了尚未 checkpoint 的交易"


def test_same_second_backups_do_not_collide(_data_dir):
    """時間戳是秒級 —— 同一秒備份兩次不可撞名失敗，更不可互相覆蓋。"""
    _make_db(_data_dir / "auth.sqlite", rows=200)
    db_health.backup_all()
    db_health.backup_all()
    assert len(db_health.list_backups("auth.sqlite")) == 2


def test_corrupt_db_is_not_backed_up(_data_dir):
    """**壞掉的不可備份** —— 否則輪替幾輪後好的備份會被壞的擠掉。"""
    p = _make_db(_data_dir / "auth.sqlite")
    db_health.backup_all()
    good = db_health.list_backups("auth.sqlite")[0]
    _corrupt(p)
    rep = db_health.backup_all()
    assert [s["file"] for s in rep["skipped"]] == ["auth.sqlite"]
    assert good.exists(), "好的備份被刪掉了"
    assert len(db_health.list_backups("auth.sqlite")) == 1


def test_failed_backup_does_not_delete_existing(_data_dir, monkeypatch):
    """備份失敗只能清自己的暫存檔。

    第一版失敗時直接 `dest.unlink()`，撞名時把上一份好的備份也刪了 —— 等於把
    唯一的救命索自己剪斷。
    """
    _make_db(_data_dir / "auth.sqlite", rows=100)
    db_health.backup_all()
    existing = db_health.list_backups("auth.sqlite")
    assert len(existing) == 1

    real = sqlite3.connect

    def boom(*a, **k):
        raise sqlite3.OperationalError("disk I/O error")
    monkeypatch.setattr(sqlite3, "connect", boom)
    assert db_health.backup_one(_data_dir / "auth.sqlite") is None
    monkeypatch.setattr(sqlite3, "connect", real)

    assert existing[0].exists(), "備份失敗時刪掉了既有的備份"


def test_rotation_keeps_newest(_data_dir):
    _make_db(_data_dir / "auth.sqlite", rows=100)
    for _ in range(5):
        db_health.backup_all(keep=3)
    kept = db_health.list_backups("auth.sqlite")
    assert len(kept) == 3
    assert kept == sorted(kept, reverse=True), "保留的應該是最新的幾份"


def test_no_temp_files_left_behind(_data_dir):
    _make_db(_data_dir / "auth.sqlite", rows=100)
    db_health.backup_all()
    assert not list(db_health.backup_dir().glob("*.tmp"))


# ---------------- 復原 ----------------

def test_restore_recovers_data(_data_dir):
    p = _make_db(_data_dir / "auth.sqlite")
    db_health.backup_all()
    _corrupt(p)
    assert db_health.check_one(p)["ok"] is False

    res = db_health.restore("auth.sqlite")
    assert res["ok"] is True, res
    assert db_health.check_one(p, thorough=True)["ok"] is True
    n = sqlite3.connect(str(p)).execute("SELECT COUNT(*) FROM t").fetchone()[0]
    assert n == 4000


def test_restore_keeps_the_damaged_file(_data_dir):
    """壞檔別直接丟掉 —— 有時還能用 sqlite3 `.recover` 撈出部分資料。"""
    p = _make_db(_data_dir / "auth.sqlite")
    db_health.backup_all()
    _corrupt(p)
    res = db_health.restore("auth.sqlite")
    from pathlib import Path
    assert res["previous_saved_as"] and Path(res["previous_saved_as"]).exists()


def test_restore_refuses_a_corrupt_backup(_data_dir):
    """備份本身也壞了就要中止，**不可**拿壞檔去蓋掉正式檔。"""
    p = _make_db(_data_dir / "auth.sqlite")
    db_health.backup_all()
    b = db_health.list_backups("auth.sqlite")[0]
    _corrupt(b)
    res = db_health.restore("auth.sqlite", b)
    assert res["ok"] is False and "毀損" in res["error"]
    assert db_health.check_one(p)["ok"] is True, "正式檔被壞備份蓋掉了"


def test_restore_without_backup_reports_clearly(_data_dir):
    _make_db(_data_dir / "auth.sqlite", rows=50)
    res = db_health.restore("auth.sqlite")
    assert res["ok"] is False and "找不到" in res["error"]


def test_restore_clears_stale_wal(_data_dir):
    """舊的 `-wal` 留著會和還原後的主檔對不起來。"""
    p = _make_db(_data_dir / "auth.sqlite", rows=100)
    db_health.backup_all()
    c = sqlite3.connect(str(p))
    c.execute("INSERT INTO t(name, pad) VALUES('後來寫的','z')")
    c.commit()
    c.close()
    wal = p.with_name(p.name + "-wal")
    if wal.exists():
        db_health.restore("auth.sqlite")
        assert not wal.exists(), "還原後仍留著舊的 WAL"


# ---------------- 排程 / 分級 ----------------

def test_large_rebuildable_db_is_not_backed_up():
    """統編資料庫動輒 GB 級且可重新下載 —— 備份它只會塞爆磁碟。"""
    m = {x["file"]: x for x in db_health.MANAGED}
    assert m["vat_db.sqlite"]["backup"] is False
    assert m["auth.sqlite"]["backup"] is True


def test_every_managed_db_has_both_languages():
    """CLI 一律英文（純文字終端 / 精簡容器 / Windows 主控台不一定有中文字型），
    網頁用中文 —— 兩份都要有，不可只寫一種。"""
    for m in db_health.MANAGED:
        assert m["impact"] and m["impact_en"]
        assert m["impact_en"].isascii(), m["file"]


def test_scheduled_backup_throttles_to_daily(_data_dir):
    from app.core import retention
    _make_db(_data_dir / "auth.sqlite", rows=100)
    first = retention._maybe_backup_dbs()
    assert first.get("created")
    second = retention._maybe_backup_dbs()
    assert second.get("skipped"), "6 小時排程每次都備份會塞爆磁碟"


def test_scheduled_backup_runs_again_after_a_day(_data_dir, monkeypatch):
    from app.core import retention
    _make_db(_data_dir / "auth.sqlite", rows=100)
    retention._maybe_backup_dbs()
    # 讓既有備份看起來是兩天前做的
    for b in db_health.list_backups("auth.sqlite"):
        old = time.time() - 2 * 86400
        import os
        os.utime(b, (old, old))
    assert retention._maybe_backup_dbs().get("created")


# ---------------- 啟動效能（部署到正式機才發現的回歸）----------------

def test_startup_skips_large_databases(_data_dir, monkeypatch):
    """大檔不可在啟動時同步檢查。

    正式機的統編資料庫有 1.4 GB —— 冷快取下 `quick_check` 要把整個檔案讀過一遍，
    實測讓啟動從 7 秒變成 61 秒，等於每次 `jtdt update` 多一分鐘連不上。
    """
    p = _make_db(_data_dir / "vat_db.sqlite", rows=2000)
    rows = db_health.startup_check(max_bytes=1024)      # 門檻壓到 1 KB
    vat = next(r for r in rows if r["file"] == "vat_db.sqlite")
    assert vat["skipped"] is True
    assert "未自動檢查" in vat["detail"]
    assert p.stat().st_size > 1024


def test_skipped_is_not_reported_as_verified(_data_dir):
    """跳過檢查的要標示出來，不可讓管理員以為「正常」＝已經驗過。"""
    _make_db(_data_dir / "vat_db.sqlite", rows=2000)
    rows = db_health.startup_check(max_bytes=1024)
    vat = next(r for r in rows if r["file"] == "vat_db.sqlite")
    assert vat["detail"] != "正常"


def test_small_databases_are_still_checked(_data_dir):
    p = _make_db(_data_dir / "auth.sqlite", rows=500)
    _corrupt(p)
    rows = db_health.startup_check()
    auth = next(r for r in rows if r["file"] == "auth.sqlite")
    assert auth["ok"] is False, "小檔仍必須在啟動時檢查"


def test_startup_check_async_does_not_block(_data_dir):
    """服務要立刻可用 —— 檢查在背景跑完再回報。"""
    import threading
    import time as _t

    _make_db(_data_dir / "auth.sqlite", rows=500)
    got = {}
    done = threading.Event()

    def cb(rows):
        got["rows"] = rows
        done.set()

    t0 = _t.perf_counter()
    db_health.startup_check_async(cb)
    elapsed = _t.perf_counter() - t0
    assert elapsed < 0.05, f"startup_check_async 擋住了主執行緒 {elapsed:.3f}s"
    assert done.wait(timeout=10), "背景檢查沒有完成"
    assert any(r["file"] == "auth.sqlite" for r in got["rows"])


def test_startup_check_async_survives_errors(_data_dir, monkeypatch):
    """背景執行緒丟例外不可讓服務掛掉（也不該無聲無息）。"""
    def boom(*a, **k):
        raise RuntimeError("讀不到磁碟")
    monkeypatch.setattr(db_health, "startup_check", boom)
    db_health.startup_check_async(lambda rows: None)   # 不可丟例外
    time.sleep(0.3)


def test_admin_check_also_skips_large_by_default(_data_dir):
    """**這條是使用者實際踩到的**：管理頁的「資料庫健康狀態」每次載入都會呼叫
    `check_all`。原本它不跳過大檔 —— 正式機的統編資料庫 1.4 GB，掃一次實測
    58 秒，結果點「系統狀態」整頁像卡住（瀏覽器對同一站台的連線數有限，其他
    請求全排在後面）。預設必須跳過，明確要求時才全掃。
    """
    _make_db(_data_dir / "vat_db.sqlite", rows=2000)
    rows = db_health.check_all(max_bytes=1024)
    vat = next(r for r in rows if r["file"] == "vat_db.sqlite")
    assert vat["skipped"] is True

    # 明確要求（管理員按「完整檢查」）時才真的掃
    rows2 = db_health.check_all(max_bytes=None)
    vat2 = next(r for r in rows2 if r["file"] == "vat_db.sqlite")
    assert vat2["skipped"] is False
    assert vat2["ok"] is True


def test_check_all_reports_skipped_field_always(_data_dir):
    """每一列都要有 `skipped` 欄位 —— 前端靠它區分「正常」與「沒檢查」，
    少了它會把未檢查的顯示成已驗過。"""
    _make_db(_data_dir / "auth.sqlite", rows=100)
    for r in db_health.check_all():
        assert "skipped" in r
