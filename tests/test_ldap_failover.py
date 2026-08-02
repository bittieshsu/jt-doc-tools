"""多台 DC 容錯與連線逾時。

## 由來

企業 AD 標配至少兩台 DC，但 `_build_server` 原本只吃**單一** `server_url`，也沒有
任何逾時設定。後果：

* 那一台 DC 維護或重啟 → **全公司登不進來**。
* DC 網路不通時，登入請求會卡到 socket 的預設逾時（動輒數十秒），使用者體感就是
  「系統掛了」。

## `exhaust` 一定要給秒數，不可以給 `True`

第一版寫 `ServerPool(..., exhaust=True)`，讀 ldap3 的 `pooling.py` 才發現那是
**永久排除**：

    if (isinstance(exhaust, bool) and exhaust) or (now - last_checked).seconds < exhaust:
        continue   # keeps server offline

布林 `True` 那一支永遠成立 —— DC 只要閃一次就再也不會被試，直到行程重啟。
那比沒有容錯更糟（原本至少每次都會重試）。給秒數才是「先跳過、過一陣子再試」。

## 所有連線都要走同一個 builder

漏掉任何一條路徑，那條就會在 DC 掛掉時卡死，而且是最難查的那種
（登入好好的、同步卻整個停住）。
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

from app.core import auth_ldap


# ------------------------------------------------------------ 多台解析

@pytest.mark.parametrize("raw,expect", [
    ("ldaps://dc1", ["ldaps://dc1"]),
    ("ldaps://dc1,ldaps://dc2", ["ldaps://dc1", "ldaps://dc2"]),
    ("ldaps://dc1, ldaps://dc2 ,ldaps://dc3",
     ["ldaps://dc1", "ldaps://dc2", "ldaps://dc3"]),
    ("ldaps://dc1\nldaps://dc2", ["ldaps://dc1", "ldaps://dc2"]),
    # 重複的只留一份；順序即嘗試順序
    ("ldaps://dc1,ldaps://dc1,ldaps://dc2", ["ldaps://dc1", "ldaps://dc2"]),
    ("", []),
    ("   ", []),
])
def test_split_servers(raw, expect):
    assert auth_ldap._split_servers(raw) == expect


def test_single_server_stays_a_plain_server():
    """只有一台時不必包成 pool —— 少一層間接，錯誤訊息也比較直接。"""
    srv = auth_ldap._build_server({"server_url": "ldaps://dc1"})
    assert type(srv).__name__ == "Server"


def test_multiple_servers_become_a_pool():
    pool = auth_ldap._build_server({"server_url": "ldaps://dc1,ldaps://dc2"})
    assert type(pool).__name__ == "ServerPool"
    assert len(pool.servers) == 2


def test_pool_tries_the_first_server_first():
    """FIRST 策略：就近的主要 DC 優先，掛了才換 —— 不是負載平衡。"""
    pool = auth_ldap._build_server({"server_url": "a,b"})
    assert pool.strategy == "FIRST"


def test_exhaust_is_seconds_not_true():
    """**這是第一版的 bug**：`exhaust=True` 會讓掛掉的 DC 永久消失。

    給秒數，DC 修好之後才會自己回到輪替。
    """
    pool = auth_ldap._build_server({"server_url": "a,b"})
    assert pool.exhaust is not True, (
        "exhaust=True 在 ldap3 是永久排除 —— DC 閃一次就再也不會被用到")
    assert isinstance(pool.exhaust, (int, float))
    assert pool.exhaust > 0
    assert auth_ldap._POOL_EXHAUST_SECONDS > 0


def test_empty_url_still_rejected():
    with pytest.raises(auth_ldap.AuthError):
        auth_ldap._build_server({"server_url": "   "})


# ------------------------------------------------------------ 逾時

def test_connect_timeout_is_set_on_every_server():
    """DC 不通時不可以讓使用者枯等 socket 預設逾時。"""
    pool = auth_ldap._build_server({"server_url": "a,b"})
    for srv in pool.servers:
        assert srv.connect_timeout == auth_ldap._CONNECT_TIMEOUT
    assert 0 < auth_ldap._CONNECT_TIMEOUT <= 15


def test_receive_timeout_is_provided():
    kw = auth_ldap._conn_kwargs()
    assert kw.get("receive_timeout") == auth_ldap._RECEIVE_TIMEOUT
    assert auth_ldap._RECEIVE_TIMEOUT >= auth_ldap._CONNECT_TIMEOUT


# ------------------------------------------------------------ 涵蓋率

def _source() -> str:
    return (Path(auth_ldap.__file__)).read_text(encoding="utf-8")


def test_no_connection_bypasses_the_timeout():
    """每一個 `Connection(...)` 都要帶 `_conn_kwargs()`。

    漏掉一條路徑，那條就會在 DC 掛掉時卡死 —— 而且症狀很難聯想
    （登入正常、同步卻整個停住）。
    """
    src = _source()
    bad = [m.group(0)[:70].replace("\n", " ")
           for m in re.finditer(r"Connection\((?:[^()]|\([^()]*\))*\)", src)
           if "auto_bind=True" in m.group(0) and "_conn_kwargs" not in m.group(0)]
    assert not bad, f"這些連線沒有逾時設定：{bad}"


def test_no_inline_server_construction():
    """不可以自己 `Server(...)` —— 那條路徑就繞過了多 DC 與逾時。"""
    src = _source()
    # 只允許 _build_server / _one 裡面那一處
    hits = [m.start() for m in re.finditer(r"(?<!_)\bServer\(", src)]
    builder = src.index("def _build_server")
    builder_end = src.index("def _conn_kwargs")
    outside = [h for h in hits if not (builder <= h <= builder_end)]
    assert not outside, (
        f"有 {len(outside)} 處自己建 Server，會繞過多 DC 容錯與逾時")


def test_ui_field_is_not_type_url():
    """伺服器 URL 欄位不可以是 `type="url"`。

    多台 DC 是逗號分隔的字串，`type="url"` 會被瀏覽器的 HTML5 驗證判成無效網址，
    **整個表單送不出去** —— 而且畫面上只會有一個瀏覽器原生的小提示，很難聯想。
    """
    tpl = (Path(auth_ldap.__file__).resolve().parent.parent / "admin" /
           "templates" / "admin_auth_settings.html").read_text(encoding="utf-8")
    m = re.search(r'<input id="af-server-url"[^>]*>', tpl)
    assert m, "找不到伺服器 URL 欄位"
    assert 'type="url"' not in m.group(0), (
        'type="url" 會擋掉多台 DC 的逗號分隔寫法')
