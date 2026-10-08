"""知識庫的存取範圍：誰看得到哪些資料集。

規則只有一份，**檢索、取段落、下載原檔、列資料集都走這裡**：

* 認證關閉（單機模式）→ 全部看得到。
* 管理員 → 全部看得到（知識庫是管理員在維護的，本來就看得到每一份）。
* 其他人 → 「全站」的資料集 ＋ 「指定群組」裡自己所屬群組（**含巢狀的上層群組**，
  跟權限矩陣同一套 `permissions._user_groups_local`）的資料集。
* 認證開著卻沒有使用者（理論上不會發生）→ 只有「全站」的。

**權限不足與不存在回一樣的結果**（呼叫端回 404）—— 不透露那份資料存在。
每一次都重新判斷，不快取：群組成員一改、權限一撤，下一次查詢就生效（規格 S02）。
"""
from __future__ import annotations

import json
from typing import Optional

from ...logging_setup import get_logger

logger = get_logger(__name__)


def _auth_enabled() -> bool:
    try:
        from .. import auth_settings
        return auth_settings.is_enabled()
    except Exception:
        # 讀不到認證設定時**當成開著**（fail-secure）—— 當成關著就等於全部公開
        logger.warning("知識庫：讀不到認證設定，存取範圍以最嚴格的方式判斷", exc_info=True)
        return True


def _is_admin(user_id: int) -> bool:
    try:
        from .. import permissions
        return permissions.is_admin(int(user_id))
    except Exception:
        return False


def user_group_ids(user_id: int) -> set[int]:
    try:
        from .. import auth_db, permissions
        return {int(g) for g in permissions._user_groups_local(auth_db.conn(), int(user_id))}
    except Exception:
        logger.warning("知識庫：讀不到使用者 %s 的群組", user_id, exc_info=True)
        return set()


def sees_everything(user_id: Optional[int]) -> bool:
    if not _auth_enabled():
        return True
    if user_id is None:
        return False
    return _is_admin(user_id)


def visible_dataset_ids(user_id: Optional[int]) -> Optional[set[str]]:
    """這個人看得到的資料集 id。`None` ＝ 全部（不用過濾）。"""
    if sees_everything(user_id):
        return None
    from . import store
    c = store.conn()
    out = {r["id"] for r in c.execute(
        "SELECT id FROM kb_datasets WHERE access='all'").fetchall()}
    if user_id is not None:
        groups = user_group_ids(user_id)
        if groups:
            # 查詢字串保持常數（清單走 json_each 參數），不拼接 —— 同統編查詢的做法
            out |= {r["dataset_id"] for r in c.execute(
                "SELECT DISTINCT g.dataset_id FROM kb_dataset_groups g "
                "JOIN kb_datasets d ON d.id = g.dataset_id "
                "WHERE d.access='groups' AND g.group_id IN (SELECT value FROM json_each(?))",
                (json.dumps(sorted(groups)),)).fetchall()}
    return out


def can_see_dataset(user_id: Optional[int], dataset_id: str) -> bool:
    vis = visible_dataset_ids(user_id)
    return vis is None or dataset_id in vis
