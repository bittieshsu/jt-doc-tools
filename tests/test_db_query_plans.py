"""熱路徑的 SQL 不可以整表掃描。

2026-08-27 客戶回報：「刪 user 會卡住，多刪幾個系統就像掛掉」。根因之一是
`group_members` 的主鍵是 `(group_id, user_id)` —— **用 user_id 單獨查用不到
那個索引**（不是前綴），而刪除 `users` 會觸發它的 ON DELETE CASCADE，於是
每刪一個帳號就整表掃描一次。目錄同步把整個網域鏡射進來之後（客戶那邊
18,611 位），那張表幾十萬列，一次掃描好幾百毫秒。

**這種缺陷從功能測試看不出來** —— 功能完全正確，只是慢，而且要資料量夠大
才看得出來。所以改用 SQLite 自己的查詢計畫來驗：熱路徑上不可以出現 `SCAN`。

實測（18,000 使用者 / 90,000 成員列）：刪 20 個帳號 0.42 秒 → 0.042 秒。
"""
from __future__ import annotations

import pytest

from app.core import auth_db


#: (說明, SQL, 這句一定要用到索引的資料表)
HOT_QUERIES = [
    ("刪帳號時 group_members 的 CASCADE",
     "SELECT * FROM group_members WHERE user_id=1", "group_members"),
    ("刪帳號時 sessions 的 CASCADE",
     "SELECT * FROM sessions WHERE user_id=1", "sessions"),
    ("查某人的角色",
     "SELECT * FROM subject_roles WHERE subject_type='user' AND subject_key='1'",
     "subject_roles"),
    ("查某人的直接授權",
     "SELECT * FROM subject_perms WHERE subject_type='user' AND subject_key='1'",
     "subject_perms"),
    ("用帳號名找人（每次登入都會做）",
     "SELECT * FROM users WHERE username='someone'", "users"),
]


@pytest.mark.parametrize("label,sql,table", HOT_QUERIES,
                         ids=[q[0] for q in HOT_QUERIES])
def test_hot_query_uses_an_index(auth_off, label, sql, table):
    auth_db.init()
    conn = auth_db.conn()
    plan = [row[-1] for row in conn.execute("EXPLAIN QUERY PLAN " + sql)]
    scans = [p for p in plan if p.startswith(f"SCAN {table}")]
    assert not scans, (
        f"{label}：`{table}` 走整表掃描而不是索引。\n"
        f"  查詢計畫：{plan}\n"
        "  資料量小的時候看不出來，鏡射整個目錄之後就是好幾百毫秒一次。")


def test_group_members_has_a_user_index(auth_off):
    """釘住那個具體的索引 —— 上面那條驗行為，這條驗它為什麼成立。"""
    auth_db.init()
    names = [r[0] for r in auth_db.conn().execute(
        "SELECT name FROM sqlite_master WHERE type='index' "
        "AND tbl_name='group_members'")]
    assert any("user" in n for n in names), (
        "group_members 少了 user_id 的索引 —— 主鍵 (group_id, user_id) 對 "
        "user_id 單獨查是用不到的")
