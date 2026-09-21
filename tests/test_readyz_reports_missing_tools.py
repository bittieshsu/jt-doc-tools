"""工具載入失敗要有地方看得到 —— `/healthz` 說正常不代表東西都在。

**外部稽核 2026-09-18 指出的洞**：`discover_tools()` 載入失敗只留一行 ERROR
然後 `continue`，而 `/healthz` 固定回 `{"ok": true}`。少一個直接 import 的
套件（`defusedxml` 那次）就會讓幾支工具安靜消失：服務照常啟動、healthz 照樣
200、側欄少了幾項，使用者只發現「工具不見了」，管理員沒有任何地方看得到。

**兩層，不是一個布林**：
* 資料目錄寫不進去 / 資料庫開不起來 → 真的不能工作 → `/readyz` 回 503。
* 工具少了幾支 → 其餘照常可用 → 回 200 ＋ `degraded: true`。
  這一層回 503 是有害的：本專案是**單一 web 行程**，把唯一的實例判成不健康，
  使用者看到的是整站錯誤頁。
"""
from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient

from app import tool_registry


@pytest.fixture
def clean_failures():
    """每條測試自己收乾淨 —— `_LOAD_FAILURES` 是模組層級的共用狀態。"""
    saved = dict(tool_registry._LOAD_FAILURES)
    tool_registry._LOAD_FAILURES.clear()
    tool_registry._LOAD_FAILURES.update(saved)
    yield
    tool_registry._LOAD_FAILURES.clear()
    tool_registry._LOAD_FAILURES.update(saved)


def test_a_tool_that_fails_to_import_is_recorded(monkeypatch, clean_failures):
    """判準落在「真的走了那條 except」上，不是直接塞進字典。"""
    tool_registry._LOAD_FAILURES.clear()
    real = importlib.import_module
    victim = "pdf_merge"

    def fake(name, *a, **kw):
        if name == f"app.tools.{victim}":
            raise ModuleNotFoundError("No module named 'somedep'")
        return real(name, *a, **kw)

    monkeypatch.setattr(tool_registry.importlib, "import_module", fake)
    tools = tool_registry.discover_tools()

    failures = tool_registry.load_failures()
    assert victim in failures, "載入失敗沒有被記下來"
    assert "somedep" in failures[victim], "原因要說得出缺什麼"
    assert all(t.metadata.id != "pdf-merge" for t in tools), "壞掉的工具不該出現在清單裡"


def test_healthz_stays_ok_even_when_tools_are_missing(client, clean_failures):
    """存活探測**刻意**不看工具 —— 服務管理員拿它決定要不要重啟，
    因為少一支工具而一直重啟比問題本身更糟。"""
    tool_registry._LOAD_FAILURES["some_tool"] = "ModuleNotFoundError: nope"
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_readyz_says_degraded_when_a_tool_is_missing(client, clean_failures):
    tool_registry._LOAD_FAILURES.clear()
    before = client.get("/readyz").json()
    assert before["degraded"] is False
    assert before["tools"]["failed"] == 0

    tool_registry._LOAD_FAILURES["some_tool"] = "ModuleNotFoundError: nope"
    r = client.get("/readyz")
    body = r.json()
    # 少一支工具不是致命的 —— 其餘 47 支照常可用
    assert r.status_code == 200, "工具少幾支不可以把唯一的實例判成不健康"
    assert body["ok"] is True
    assert body["degraded"] is True
    assert body["tools"]["failed"] == 1
    assert body["tools"]["loaded"] == before["tools"]["loaded"]


def test_readyz_does_not_leak_module_names_or_exception_text(client, clean_failures):
    """這支跟 `/healthz` 一樣是公開的 —— 不可以吐內部細節。"""
    marker = "zzz_secret_module_path_zzz"
    tool_registry._LOAD_FAILURES[marker] = f"ImportError: /opt/internal/{marker}.py"
    raw = client.get("/readyz").text
    assert marker not in raw, "公開端點吐出了模組名稱"
    assert "ImportError" not in raw, "公開端點吐出了例外訊息"
    assert "/opt/" not in raw, "公開端點吐出了檔案路徑"


def test_readyz_is_public_like_healthz():
    from app.main import _PUBLIC_EXACT
    assert "/readyz" in _PUBLIC_EXACT, "監控探測不可以被認證擋住"


def test_readyz_checks_the_things_that_are_actually_fatal(client):
    body = client.get("/readyz").json()
    assert set(body["checks"]) == {"data_dir_writable", "database"}
    assert body["checks"]["data_dir_writable"] is True
    assert body["checks"]["database"] is True


def test_readyz_returns_503_when_the_database_is_unreachable(client, monkeypatch):
    """反向對照：只驗「好的時候回 200」的話，把檢查整段拿掉也會全綠。"""
    from app.core import auth_db

    def boom():
        raise RuntimeError("database is locked")

    monkeypatch.setattr(auth_db, "conn", boom)
    r = client.get("/readyz")
    assert r.status_code == 503, "資料庫開不起來就是真的不能工作"
    body = r.json()
    assert body["ok"] is False
    assert body["checks"]["database"] is False


def test_the_admin_page_shows_which_tools_failed(admin_session, clean_failures):
    admin, _, _ = admin_session
    """細節只在管理區（需要管理員）。"""
    tool_registry._LOAD_FAILURES["pdf_merge"] = "ModuleNotFoundError: No module named 'defusedxml'"
    r = admin.get("/admin/system-status")
    assert r.status_code == 200
    assert "pdf_merge" in r.text
    assert "defusedxml" in r.text


def test_the_admin_page_stays_quiet_when_nothing_failed(admin_session, clean_failures):
    admin, _, _ = admin_session
    """沒事的時候不占畫面 —— 常態出現的警告會被忽略。"""
    tool_registry._LOAD_FAILURES.clear()
    r = admin.get("/admin/system-status")
    assert r.status_code == 200
    assert "class=\"tlf\"" not in r.text
