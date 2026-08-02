"""重建資料表的 migration 一律要關掉外鍵，否則升級會**清空子表**。

## 由來

`db.get_conn` 開著 `PRAGMA foreign_keys=ON`，而 `group_members.user_id` 與
`sessions.user_id` 都是 `ON DELETE CASCADE` 指向 `users`。SQLite 沒辦法就地改
UNIQUE 約束，所以這類 migration 走「建新表 → 複製 → `DROP TABLE users` → 改名」
的標準十二步。**`DROP TABLE` 的隱含刪除會觸發串聯** —— 少一行
`PRAGMA foreign_keys=OFF`，升級之後每個人的群組成員關係與所有 session 就沒了，
而且完全無聲：使用者還在、角色還在，只是群組權限突然不生效。

v1.12.0 的 `_m8` 踩過一次（`group_members` 被清空）。當時**只修了 `_m8`**，
`_m2` 一直沒補 —— 這一份就是那次補漏時寫的，並且改成掃**所有**重建型 migration，
而不是只釘住已知的那兩支。

## 兩層驗證

* **靜態**：任何含 `DROP TABLE` 的 migration 都要有 `foreign_keys=OFF` 與 `=ON`。
  新加一支忘了寫，這裡就會紅。
* **行為**：真的建一份含群組成員與 session 的舊版資料庫，跑真的 migration，
  確認資料還在。靜態掃描只能保證「有寫」，保證不了「寫對」。
"""
from __future__ import annotations

import inspect
import re
import sqlite3
import time

import pytest

from app.core import auth_db


def _strip_docstring(src: str) -> str:
    """把說明字串拿掉，只留實際會執行的程式碼。

    `_m8` 的說明裡就寫著「DROP TABLE 會串聯」與「foreign_keys=ON」這些字 ——
    連說明一起掃的話，順序判斷會被那些散文誤導（第一次就是這樣誤報的）。
    """
    import ast
    import textwrap
    try:
        tree = ast.parse(textwrap.dedent(src))
    except SyntaxError:  # pragma: no cover
        return src
    fn = tree.body[0]
    if (isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and fn.body
            and isinstance(fn.body[0], ast.Expr)
            and isinstance(fn.body[0].value, ast.Constant)
            and isinstance(fn.body[0].value.value, str)):
        fn.body = fn.body[1:]
    return ast.unparse(tree)


def _rebuild_migrations():
    """所有會 DROP TABLE 的 migration（(名稱, 去掉說明的原始碼)）。"""
    out = []
    for fn in auth_db.MIGRATIONS:
        try:
            src = _strip_docstring(inspect.getsource(fn))
        except (OSError, TypeError):  # pragma: no cover
            continue
        if re.search(r"\bDROP\s+TABLE\b", src, re.I):
            out.append((fn.__name__, src))
    return out


# ------------------------------------------------------------ 靜態

def test_there_are_rebuild_migrations_to_check():
    """掃描本身要有效 —— 抓到 0 支代表掃描壞了，不是代表沒問題。"""
    assert _rebuild_migrations(), "一支重建型 migration 都沒掃到"


@pytest.mark.parametrize("name,src",
                         _rebuild_migrations(),
                         ids=lambda v: v if isinstance(v, str) and
                         v.startswith("_m") else "")
def test_rebuild_migration_disables_foreign_keys(name, src):
    """DROP TABLE 之前一定要關外鍵，之後要開回來。"""
    assert re.search(r"foreign_keys\s*=\s*OFF", src, re.I), (
        f"{name} 會 DROP TABLE 但沒有先關外鍵 —— 升級會把子表串聯清空")
    assert re.search(r"foreign_keys\s*=\s*ON", src, re.I), (
        f"{name} 關了外鍵卻沒有開回來")


@pytest.mark.parametrize("name,src", _rebuild_migrations(),
                         ids=lambda v: v if isinstance(v, str) and
                         v.startswith("_m") else "")
def test_off_comes_before_the_drop(name, src):
    """順序要對 —— 寫在 DROP 之後等於沒寫。"""
    off = re.search(r"foreign_keys\s*=\s*OFF", src, re.I)
    drop = re.search(r"\bDROP\s+TABLE\b", src, re.I)
    assert off.start() < drop.start(), f"{name} 的 foreign_keys=OFF 寫在 DROP 之後"


# ------------------------------------------------------------ 行為

def _legacy_db() -> sqlite3.Connection:
    """一份 v1 結構、含群組成員與 session 的資料庫（模擬舊版安裝）。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    # 與 db.get_conn 一致 —— 沒有這一行就重現不出問題
    conn.execute("PRAGMA foreign_keys=ON")
    auth_db._m1_initial(conn)
    now = time.time()
    conn.execute(
        "INSERT INTO users(username, display_name, source, enabled, "
        "is_admin_seed, created_at) VALUES ('alice','Alice','ldap',1,0,?)", (now,))
    conn.execute("INSERT INTO groups(name, source, created_at) "
                 "VALUES ('RD','ldap',?)", (now,))
    conn.execute("INSERT INTO group_members(group_id, user_id) VALUES (1,1)")
    conn.execute(
        "INSERT INTO sessions(token_hash, user_id, created_at, expires_at, "
        "remember) VALUES ('hash',1,?,?,0)", (now, now + 9999))
    conn.commit()
    return conn


def _count(conn, table) -> int:
    return conn.execute(f"SELECT COUNT(*) c FROM {table}").fetchone()["c"]


def test_m2_keeps_group_members_and_sessions():
    """**這就是原本壞掉的地方** —— 修之前這兩個數字都會變成 0。"""
    conn = _legacy_db()
    assert (_count(conn, "group_members"), _count(conn, "sessions")) == (1, 1)
    auth_db._m2_username_source_unique(conn)
    conn.commit()
    assert _count(conn, "group_members") == 1, "群組成員關係被串聯刪除了"
    assert _count(conn, "sessions") == 1, "所有 session 被串聯刪除了"
    assert _count(conn, "users") == 1


def test_m2_still_does_its_job():
    """修外鍵不可以把 migration 本來要做的事弄壞：同名不同來源要能並存。"""
    conn = _legacy_db()
    auth_db._m2_username_source_unique(conn)
    conn.commit()
    conn.execute(
        "INSERT INTO users(username, display_name, source, enabled, "
        "is_admin_seed, created_at) VALUES ('alice','Alice','local',1,0,?)",
        (time.time(),))
    conn.commit()
    assert _count(conn, "users") == 2, "UNIQUE(username, source) 沒有生效"


def test_foreign_keys_are_back_on_afterwards():
    """關掉之後沒開回來的話，後面所有 migration 與整個連線都失去 FK 保護。"""
    conn = _legacy_db()
    auth_db._m2_username_source_unique(conn)
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_full_migration_chain_preserves_data():
    """從 v1 一路升到最新版，群組成員關係要全程存活。

    逐支測會漏掉「A 支修好了但 B 支又清掉」的組合 —— 這一條走完整條鏈。
    """
    conn = _legacy_db()
    for fn in auth_db.MIGRATIONS[1:]:      # _m1 已經在 _legacy_db 裡跑過
        fn(conn)
        conn.commit()
        assert _count(conn, "group_members") == 1, \
            f"{fn.__name__} 之後群組成員關係不見了"
    assert _count(conn, "users") == 1
