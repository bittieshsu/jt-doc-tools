"""認證設定讀不到的時候，**不可以無聲地把認證關掉**。

## 由來

v1.14.31 的對抗式驗證發現：`auth_settings.json` 毀損或遺失時，`get()` 直接
退回 `_DEFAULTS`，而 `_DEFAULTS["backend"]` 是 `"off"` —— 於是 `is_enabled()`
回 False、`_auth_gate` 第一個判斷就放行、`require_admin` 對所有人回 True。

**使用者、角色、權限資料表完全沒動**，只是那個檔壞了。實測四種情況
（非法 JSON / 內容是 `{}` / 0 bytes / 檔案被刪）都讓未登入者拿到
`/admin/users`、`/admin/audit` 與受限工具的 200。

## 這不是假設性的

* `save()` 原本沒有 fsync —— ext4 的延遲配置讓「rename 完成、內容沒落地」
  成為可能，斷電或 VM 硬重置後那個檔就是 **0 bytes**。
* 設定匯入是 `copyfileobj` 原樣寫入，不驗 JSON。
* 備份還原漏檔、組態管理樣板出錯、手動搬 `data/`。

## 判準

程式**分辨得出來**「全新安裝」與「設定檔壞了」—— 看資料庫裡有沒有使用者
（`list_existing_users()` 本來就是為此存在的）。有使用者就 fail-secure。
"""
from __future__ import annotations

import json
import os

import pytest

os.environ.setdefault("JTDT_CSRF_DISABLE", "1")


@pytest.fixture
def env(tmp_path, monkeypatch):
    """獨立的資料目錄 + 一個已建立使用者的認證資料庫。

    **`auth_settings._CACHE` 是模組層全域，一定要還原**。這份測試會把它
    設成「認證開啟」，不還原的話**後面所有測試都以為認證是開著的** ——
    實測：`/admin/settings-export` 因此被導向登入頁，回傳 HTML 而不是 zip，
    三個不相干的測試跟著紅（而且只在「這幾個檔案一起跑」時才會發生，
    完整套件因為順序不同剛好躲過）。

    `monkeypatch` 只還原得了 `settings.data_dir`，還原不了別的模組的全域。
    """
    from app.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    from app.core import auth_db, auth_settings, user_manager

    saved_cache = auth_settings._CACHE
    auth_settings._CACHE = None
    auth_db.init()
    # 角色要先種好 ——  預設會指派 default-user，
    # 沒有那一列會撞外鍵。
    from app.core import roles as _roles
    _roles.seed_builtin_roles()
    user_manager.create_local("jtdt-admin", "管理員", "Aa!23456789")
    s = auth_settings.get()
    s["backend"] = "local"
    auth_settings.save(s)
    assert auth_settings.is_enabled(), "前提：認證是開著的"
    yield tmp_path, auth_settings
    auth_settings._CACHE = saved_cache


#: 四種「設定檔讀不到」的樣子
CORRUPTIONS = {
    "非法 JSON": lambda p: p.write_text('{"backend": "local"', encoding="utf-8"),
    "內容是空物件": lambda p: p.write_text("{}", encoding="utf-8"),
    "0 bytes": lambda p: p.write_text("", encoding="utf-8"),
    "檔案被刪除": lambda p: p.unlink(missing_ok=True),
    "內容是陣列": lambda p: p.write_text("[]", encoding="utf-8"),
    "缺 backend 鍵": lambda p: p.write_text('{"session_days": 7}', encoding="utf-8"),
}


@pytest.mark.parametrize("label", list(CORRUPTIONS))
def test_broken_settings_never_disables_auth(env, label):
    """有使用者存在時，設定檔壞掉不可以讓認證關閉。"""
    data_dir, auth_settings = env
    CORRUPTIONS[label](data_dir / "auth_settings.json")
    auth_settings._CACHE = None          # 模擬行程重啟

    assert auth_settings.is_enabled(), (
        f"「{label}」讓認證被無聲關閉 —— 未登入者會拿到管理區與所有工具")
    assert auth_settings.get()["backend"] == "local", (
        "fail-secure 要退到本機認證，內建管理員才做得了 break-glass")


def test_fresh_install_still_starts_with_auth_off(tmp_path, monkeypatch):
    """**全新安裝要照舊** —— 沒有使用者時認證本來就不啟用。

    這一條跟上面一樣重要：判斷寫得太寬就會把全新安裝的人鎖在門外。
    """
    from app.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    from app.core import auth_db, auth_settings

    saved = auth_settings._CACHE
    auth_settings._CACHE = None
    try:
        auth_db.init()
        assert not auth_settings.list_existing_users(), "前提：資料庫沒有使用者"
        assert not auth_settings.is_enabled(), (
            "全新安裝不該啟用認證 —— 那會讓第一次安裝的人進不去")
    finally:
        auth_settings._CACHE = saved


def test_settings_are_flushed_to_disk(env):
    """`save()` 要 fsync —— 斷電後不可以留下 0 bytes 的認證設定。

    直接驗行為：存完之後檔案要有內容且解析得出 `backend`。
    （fsync 本身在測試裡驗不到掉電，這裡守的是「不可以改回沒有落地保證的
    `write_text`」—— 那個寫法會讓內容與 rename 的順序沒有保證。）
    """
    data_dir, auth_settings = env
    p = data_dir / "auth_settings.json"
    assert p.stat().st_size > 0, "存完之後檔案是空的"
    assert json.loads(p.read_text(encoding="utf-8"))["backend"] == "local"

    # **要比對真的呼叫，不是「原始碼裡有沒有 fsync 這個字」** ——
    # `save()` 的註解裡就寫著「一定要 fsync 再 rename」，用字串比對的話
    # 把程式碼改回去、只留註解也照樣全綠（實測變異驗證沒抓到）。
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(auth_settings.save).lstrip())
    # **要認出「fsync 的是剛寫的那個檔」**。只比對有沒有 `os.fsync` 不夠 ——
    # 同一個函式裡還有一個給**目錄**用的 `os.fsync(dfd)`，把檔案那個拿掉、
    # 只留目錄那個，寬鬆的比對照樣全綠（實測變異驗證沒抓到）。
    # 判準：有一個 `os.fsync(...)`，而它的引數是某個東西的 `.fileno()`。
    fsync_on_file = any(
        isinstance(n, ast.Call)
        and ast.unparse(n.func) == "os.fsync"
        and n.args
        and isinstance(n.args[0], ast.Call)
        and ast.unparse(n.args[0].func).endswith(".fileno")
        for n in ast.walk(tree))
    assert fsync_on_file, (
        "`save()` 沒有對寫出去的檔案本身呼叫 os.fsync —— ext4 延遲配置下，"
        "斷電後可能留下 0 bytes 的認證設定檔，而那曾經等於「認證關閉」")
