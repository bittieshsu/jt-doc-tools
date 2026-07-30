"""升級到 v1.14.6：既有客戶的資料目錄要能無痛接上。

**重大原則：客戶升級版本，原有設定必需留存**（feedback_data_files_must_be_
service_user_owned）。這一版新增了工作紀錄資料庫、併行度設定、資料庫備份目錄，
以及 retention 的新欄位 —— 每一項都必須在「舊資料目錄 + 新程式」的組合下自動
補齊，而不是要求客戶手動處理或砍掉重來。

模擬方式是建一個**只有舊檔案**的資料目錄（沒有 jobs.sqlite、沒有
concurrency.json、retention.json 缺新欄位），再跑新程式。
"""
from __future__ import annotations

import json

import pytest


@pytest.fixture
def legacy_data_dir(tmp_path, monkeypatch):
    """一個 v1.14.5 時代的資料目錄。"""
    d = tmp_path / "data"
    d.mkdir()
    # 客戶自己調過的保留設定（沒有 job_records_days —— 那是新欄位）
    (d / "retention.json").write_text(json.dumps({
        "fill_history_days": 180,
        "stamp_history_days": 200,
        "watermark_history_days": 365,
        "temp_hours": 6,
        "jobs_hours": 48,
        "audit_days": 120,
        "updated_at": 1700000000.0,
    }), encoding="utf-8")
    (d / "llm_settings.json").write_text(
        json.dumps({"base_url": "http://llm.example.internal:11434"}),
        encoding="utf-8")
    monkeypatch.setattr("app.config.settings.data_dir", d)
    for mod in ("app.core.retention", "app.core.concurrency_settings"):
        __import__(mod)
    import app.core.concurrency_settings as _cs
    import app.core.retention as _ret
    _cs.invalidate_cache()
    _ret._CACHE = None
    yield d
    _cs.invalidate_cache()
    _ret._CACHE = None


def test_existing_retention_settings_are_preserved(legacy_data_dir):
    """客戶調過的保留天數不可被新版重設回預設值。"""
    from app.core import retention
    s = retention.get()
    assert s["fill_history_days"] == 180
    assert s["stamp_history_days"] == 200
    assert s["temp_hours"] == 6
    assert s["audit_days"] == 120


def test_new_retention_field_gets_a_default(legacy_data_dir):
    """舊設定檔沒有 job_records_days —— 要自動補預設，不可 KeyError。"""
    from app.core import retention
    assert retention.get()["job_records_days"] == 30


def test_sweep_works_on_legacy_settings(legacy_data_dir):
    """升級後第一次清理排程跑起來不可炸（新欄位 + 新資料庫都還不存在）。"""
    from app.core import audit_db, auth_db, retention
    auth_db.init()
    audit_db.init()
    report = retention.sweep_all()
    assert "job_records" in report


def test_job_db_is_created_on_first_start(legacy_data_dir):
    """舊資料目錄沒有 jobs.sqlite —— 啟動時要自己建，不需客戶做任何事。"""
    from app.core import job_store
    assert not (legacy_data_dir / "jobs.sqlite").exists()
    job_store.init()
    assert (legacy_data_dir / "jobs.sqlite").exists()
    assert job_store.count_jobs() == 0


def test_concurrency_defaults_without_config_file(legacy_data_dir):
    """沒有 concurrency.json 就用預設值 —— 而且預設要維持**舊行為**
    （同時 2 個工作、Office 轉檔 1 個），升級不可默默改變併行度。"""
    from app.core import concurrency_settings as cs
    assert not (legacy_data_dir / "concurrency.json").exists()
    cfg = cs.get()
    assert cfg["max_concurrent_jobs"] == 2
    assert cfg["max_office_concurrent"] == 1


def test_db_health_tolerates_missing_databases(legacy_data_dir):
    """全新 / 舊資料目錄裡多數資料庫還不存在 —— 不可被當成毀損而發警報。"""
    from app.core import db_health
    rows = db_health.startup_check()
    assert all(r["ok"] for r in rows)
    assert any(not r["exists"] for r in rows)


def test_backup_dir_created_on_demand(legacy_data_dir):
    """備份目錄不需要安裝程式先建 —— 第一次備份時自己建。"""
    from app.core import audit_db, auth_db, db_health
    auth_db.init()
    audit_db.init()
    assert not db_health.backup_dir().exists()
    db_health.backup_all()
    assert db_health.backup_dir().is_dir()


def test_no_new_third_party_dependency():
    """這一版的新模組只用標準函式庫。

    加新相依要同步改五個地方（pyproject / uv.lock / requirements /
    install.sh 與 setup-python.cmd 的 import 煙霧測試 / cli.py 的 svc_update
    驗證）—— 漏一個就會像 v1.1.68 那樣，客戶裝完卻起不來。這裡直接釘住
    「沒有新相依」這個前提，日後若真的加了，這個測試會提醒要走那套流程。
    """
    import ast
    from pathlib import Path

    stdlib_ok = {
        "json", "logging", "os", "platform", "shutil", "sqlite3", "threading",
        "time", "pathlib", "typing", "contextvars", "collections", "uuid",
        "dataclasses", "concurrent", "re", "sys", "base64", "hashlib",
        "__future__",
    }
    root = Path(__file__).resolve().parent.parent
    for name in ("job_store.py", "db_health.py", "concurrency_settings.py"):
        src = (root / "app" / "core" / name).read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Import):
                for a in node.names:
                    top = a.name.split(".")[0]
                    assert top in stdlib_ok, f"{name} 匯入了非標準函式庫 {top}"
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                top = (node.module or "").split(".")[0]
                assert top in stdlib_ok, f"{name} 匯入了非標準函式庫 {top}"


def test_cli_db_commands_go_through_the_chown_helper():
    """CLI 用 sudo 跑時是 root —— 寫出來的檔案會變成 root 所有，服務讀不到
    （v1.4.2 慘案）。新的資料庫指令必須沿用會 chown 回去的 helper。"""
    import inspect

    from app import cli
    for fn in (cli.svc_db_check, cli.svc_db_backup, cli.svc_db_restore,
               cli.svc_db_backups):
        src = inspect.getsource(fn)
        assert "_run_auth_helper" in src, f"{fn.__name__} 沒走 chown helper"


def test_interrupted_status_is_handled_in_frontend():
    """升級（`jtdt update`）會重啟服務 —— 當下正在轉檔的使用者，頁面必須告訴
    他工作被中斷，而不是進度條一直轉下去。"""
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    files = [
        root / "static" / "js" / "job_progress.js",
        root / "app" / "tools" / "pdf_ocr" / "templates" / "pdf_ocr.html",
        root / "app" / "tools" / "submission_check" / "templates"
        / "sc_upload.html",
    ]
    for f in files:
        assert "interrupted" in f.read_text(encoding="utf-8"), \
            f"{f.name} 沒有處理 interrupted 狀態，升級時進度條會卡住"
