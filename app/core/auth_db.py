"""Auth database schema + migrations + low-level access.

Stored at ``data/auth.sqlite``. Contains:

- users / groups / group_members (local mode)
- roles / role_perms (which tools each role can use)
- subject_roles, subject_perms (assign roles or direct tool grants to users
  / groups / OUs)
- sessions (cookie tokens), lockouts (failed login throttle)

External (LDAP/AD) users and groups also live in these tables so the
permission resolver can treat all subjects uniformly. Their `source` field
distinguishes them; `external_dn` carries the AD/LDAP DN.

Higher-level CRUD lives in `auth_manager.py`; this file only owns schema +
helpers shared by every layer.
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from . import db as _db

logger = logging.getLogger(__name__)


# ---------- schema migrations ----------

def _m1_initial(conn: sqlite3.Connection) -> None:
    """v1: full v1.1.0 schema in one migration. Future schema changes get
    their own _m2, _m3, ..."""
    conn.executescript("""
    -- ---------- users ----------
    CREATE TABLE users (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        username        TEXT NOT NULL UNIQUE,
        display_name    TEXT NOT NULL DEFAULT '',
        password_hash   TEXT,                  -- NULL for ldap/ad users
        source          TEXT NOT NULL DEFAULT 'local'
                            CHECK (source IN ('local','ldap','ad')),
        external_dn     TEXT,                  -- LDAP/AD DN, NULL for local
        enabled         INTEGER NOT NULL DEFAULT 1,
        is_admin_seed   INTEGER NOT NULL DEFAULT 0,  -- jtdt-admin protection flag
        created_at      REAL NOT NULL,
        last_login_at   REAL DEFAULT 0
    );
    CREATE INDEX idx_users_username ON users(username);

    -- ---------- groups ----------
    CREATE TABLE groups (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        name            TEXT NOT NULL,
        source          TEXT NOT NULL DEFAULT 'local'
                            CHECK (source IN ('local','ldap','ad')),
        external_dn     TEXT,                  -- AD CN=...,OU=... DN
        description     TEXT NOT NULL DEFAULT '',
        created_at      REAL NOT NULL,
        UNIQUE(source, name)
    );

    -- local groups: explicit members.
    -- ldap/ad groups: members come from AD memberOf at login time, NOT here.
    CREATE TABLE group_members (
        group_id        INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
        user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        PRIMARY KEY (group_id, user_id)
    );

    -- ---------- roles ----------
    -- id is a stable text key (e.g. 'admin', 'clerk') so seed roles survive
    -- import/export. is_builtin=1 protects 'admin' and 'default-user' from
    -- destructive UI operations.
    CREATE TABLE roles (
        id              TEXT PRIMARY KEY,
        display_name    TEXT NOT NULL,
        description     TEXT NOT NULL DEFAULT '',
        is_builtin      INTEGER NOT NULL DEFAULT 0,
        is_protected    INTEGER NOT NULL DEFAULT 0,  -- can edit perms but not rename/delete
        created_at      REAL NOT NULL
    );

    -- which tool ids a role grants. tool_id is the registry id like 'pdf-fill'.
    CREATE TABLE role_perms (
        role_id         TEXT NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
        tool_id         TEXT NOT NULL,
        PRIMARY KEY (role_id, tool_id)
    );

    -- ---------- subject → role assignments ----------
    -- subject_type is 'user' | 'group' | 'ou'.
    -- subject_key is:
    --   user  -> users.id (as text)
    --   group -> groups.id (as text)
    --   ou    -> the OU DN string (e.g. 'OU=Sales,OU=TW,DC=example,DC=com')
    -- Storing as text makes the schema uniform; we don't FK these (OU has
    -- no row anywhere; group/user FKs are still enforced via app-level checks
    -- on delete).
    CREATE TABLE subject_roles (
        subject_type    TEXT NOT NULL CHECK (subject_type IN ('user','group','ou')),
        subject_key     TEXT NOT NULL,
        role_id         TEXT NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
        PRIMARY KEY (subject_type, subject_key, role_id)
    );
    CREATE INDEX idx_subject_roles_subj ON subject_roles(subject_type, subject_key);

    -- direct (subject → tool) grants for special cases. UI hides these
    -- behind an "advanced" toggle.
    CREATE TABLE subject_perms (
        subject_type    TEXT NOT NULL CHECK (subject_type IN ('user','group','ou')),
        subject_key     TEXT NOT NULL,
        tool_id         TEXT NOT NULL,
        PRIMARY KEY (subject_type, subject_key, tool_id)
    );
    CREATE INDEX idx_subject_perms_subj ON subject_perms(subject_type, subject_key);

    -- ---------- sessions ----------
    -- We store sha256(cookie) NOT the raw cookie value: a DB breach then
    -- can't directly resume sessions (attacker still needs the cookie that
    -- only ever lived on the user's browser + briefly in a Set-Cookie).
    -- expires_at is absolute epoch seconds; remember=1 for 30d, 0 for 7d.
    CREATE TABLE sessions (
        token_hash      TEXT PRIMARY KEY,    -- sha256 hex of the cookie value
        user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        created_at      REAL NOT NULL,
        expires_at      REAL NOT NULL,
        remember        INTEGER NOT NULL DEFAULT 0,
        ip              TEXT NOT NULL DEFAULT '',
        user_agent      TEXT NOT NULL DEFAULT ''
    );
    CREATE INDEX idx_sessions_user ON sessions(user_id);
    CREATE INDEX idx_sessions_exp ON sessions(expires_at);

    -- ---------- lockouts ----------
    -- key is 'user:<username>' or 'ip:<addr>'. Failed login increments
    -- failed_count; reaching threshold (5) sets locked_until = now + 15min.
    -- Successful login clears the row.
    CREATE TABLE lockouts (
        key             TEXT PRIMARY KEY,
        failed_count    INTEGER NOT NULL DEFAULT 0,
        locked_until    REAL NOT NULL DEFAULT 0,
        last_failed_at  REAL NOT NULL DEFAULT 0
    );
    """)


def _m2_username_source_unique(conn: sqlite3.Connection) -> None:
    """v2: drop UNIQUE(username), add UNIQUE(username, source).

    Rationale: PVE-style multi-realm — same name `jason` may legitimately
    exist as both a `local` account and an `ldap` account; the realm
    dropdown on /login disambiguates at auth time. SQLite can't drop a
    column-level UNIQUE in place, so rebuild the table the standard way.

    **PRAGMA foreign_keys=OFF 是必要的**：`db.get_conn` 開著 FK，而
    `group_members.user_id` 與 `sessions.user_id` 都是 `ON DELETE CASCADE`
    指向 `users`。少了這一行，`DROP TABLE users` 的隱含刪除會**把所有群組成員
    關係與所有 session 一併清掉** —— 升級之後每個人的群組權限無聲消失。
    這正是 `_m8` 的說明裡記錄的那次事故：當時只修了 `_m8`，這一支一直沒補。
    """
    conn.executescript("""
    PRAGMA foreign_keys=OFF;
    -- Lifted from _m1 with one change: UNIQUE moved off `username` onto
    -- (username, source). Everything else stays bit-for-bit identical so
    -- existing data copies over with INSERT INTO ... SELECT *.
    CREATE TABLE users_new (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        username        TEXT NOT NULL,
        display_name    TEXT NOT NULL DEFAULT '',
        password_hash   TEXT,
        source          TEXT NOT NULL DEFAULT 'local'
                            CHECK (source IN ('local','ldap','ad')),
        external_dn     TEXT,
        enabled         INTEGER NOT NULL DEFAULT 1,
        is_admin_seed   INTEGER NOT NULL DEFAULT 0,
        created_at      REAL NOT NULL,
        last_login_at   REAL DEFAULT 0,
        UNIQUE(username, source)
    );
    INSERT INTO users_new
        (id, username, display_name, password_hash, source, external_dn,
         enabled, is_admin_seed, created_at, last_login_at)
    SELECT id, username, display_name, password_hash, source, external_dn,
           enabled, is_admin_seed, created_at, last_login_at
    FROM users;
    DROP TABLE users;
    ALTER TABLE users_new RENAME TO users;
    CREATE INDEX idx_users_username ON users(username);
    PRAGMA foreign_keys=ON;
    """)


def _m3_rename_pdf_diff_to_doc_diff(conn: sqlite3.Connection) -> None:
    """v3: rename `pdf-diff` → `doc-diff` in role_perms and subject_perms.

    The tool was renamed (and gained Office / ODF support) in v1.1.61.
    Without this migration, existing installs would keep granting access to
    the now-non-existent `pdf-diff` tool id and would NOT grant access to
    the new `doc-diff` — meaning users would silently lose the tool after
    upgrade.

    `INSERT OR IGNORE` shape avoids dupe-key errors if (somehow) both rows
    already exist for the same role/subject (e.g. admin manually granted
    `doc-diff` first); the old row is then dropped by the DELETE.
    """
    conn.executescript("""
    INSERT OR IGNORE INTO role_perms(role_id, tool_id)
        SELECT role_id, 'doc-diff' FROM role_perms WHERE tool_id = 'pdf-diff';
    DELETE FROM role_perms WHERE tool_id = 'pdf-diff';

    INSERT OR IGNORE INTO subject_perms(subject_type, subject_key, tool_id)
        SELECT subject_type, subject_key, 'doc-diff'
        FROM subject_perms WHERE tool_id = 'pdf-diff';
    DELETE FROM subject_perms WHERE tool_id = 'pdf-diff';
    """)


def _m4_grant_image_to_pdf(conn: sqlite3.Connection) -> None:
    """v4: grant the new `image-to-pdf` tool to anyone who already has
    `pdf-to-image`. Without this, existing customers' default-user / clerk
    roles would be missing the new tool after upgrade — they'd see it in the
    sidebar but get 403 when clicking. Pair-tool grant is the simplest
    backfill heuristic that doesn't risk over-granting.
    """
    conn.executescript("""
    INSERT OR IGNORE INTO role_perms(role_id, tool_id)
        SELECT role_id, 'image-to-pdf' FROM role_perms WHERE tool_id = 'pdf-to-image';
    INSERT OR IGNORE INTO subject_perms(subject_type, subject_key, tool_id)
        SELECT subject_type, subject_key, 'image-to-pdf'
        FROM subject_perms WHERE tool_id = 'pdf-to-image';
    """)


def _m5_grant_translate_doc(conn: sqlite3.Connection) -> None:
    """v5: grant the new `translate-doc` tool to anyone who already has
    `text-diff` (both are LLM-light text utilities, similar audience).
    Same backfill heuristic as m4. Without this, existing customers'
    default-user / clerk roles miss translate-doc after upgrade."""
    conn.executescript("""
    INSERT OR IGNORE INTO role_perms(role_id, tool_id)
        SELECT role_id, 'translate-doc' FROM role_perms WHERE tool_id = 'text-diff';
    INSERT OR IGNORE INTO subject_perms(subject_type, subject_key, tool_id)
        SELECT subject_type, subject_key, 'translate-doc'
        FROM subject_perms WHERE tool_id = 'text-diff';
    """)


def _m6_totp_columns(conn: sqlite3.Connection) -> None:
    """v6: 2FA / TOTP support (v1.4.99 起).

    新增三欄供 TOTP 流程使用：
      - totp_secret: 32-char base32 secret（pyotp.random_base32()）。NULL 表示
        從未 setup 過。儲存明文（DB 加密本身就是放心的；secret 一旦外洩
        TOTP 失效，靠 DB 整體權限管理）。
      - totp_enabled: 0/1，使用者是否完成 TOTP 啟用（生成 secret 後還沒
        驗證第一個 6 碼前不算啟用）。
      - totp_required: 0/1，是否強制使用 TOTP（auditor 角色 user 為 1，
        其他角色預設 0；admin 也可用 UI 開）。required + 未 enabled 的
        user 第一次登入會被導去 setup page。"""
    conn.executescript("""
    ALTER TABLE users ADD COLUMN totp_secret TEXT;
    ALTER TABLE users ADD COLUMN totp_enabled INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE users ADD COLUMN totp_required INTEGER NOT NULL DEFAULT 0;
    """)


def _m7_audit_seed_column(conn: sqlite3.Connection) -> None:
    """v7: 加 `users.is_audit_seed` 欄位（v1.5.0 起）。

    內建 `jtdt-auditor` 帳號在啟動時自動建立（`seed_default_auditor_user()`）；
    is_audit_seed=1 表示該 row 是內建稽核員，UI / CLI 拒絕刪除（同 admin
    用 is_admin_seed 的保護模式）。"""
    conn.executescript("""
    ALTER TABLE users ADD COLUMN is_audit_seed INTEGER NOT NULL DEFAULT 0;
    """)


def _m8_sso_sources(conn: sqlite3.Connection) -> None:
    """v8: allow `source` = 'oidc' / 'saml' on users + groups (SSO, v1.12 起).

    SQLite can't alter a column-level CHECK in place, so rebuild both tables the
    standard way. All current columns (incl. m6 totp + m7 is_audit_seed) are
    preserved via INSERT ... SELECT.

    CRITICAL: `DROP TABLE users/groups` with foreign_keys=ON performs an implicit
    DELETE that fires `group_members`' ON DELETE CASCADE — wiping every group
    membership. We therefore disable foreign_keys for the rebuild (the migrate
    connection is autocommit, so the PRAGMA takes effect) and re-enable after.
    The SQLite-recommended 12-step table-rebuild does exactly this."""
    conn.executescript("""
    PRAGMA foreign_keys=OFF;
    CREATE TABLE users_new (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        username        TEXT NOT NULL,
        display_name    TEXT NOT NULL DEFAULT '',
        password_hash   TEXT,
        source          TEXT NOT NULL DEFAULT 'local'
                            CHECK (source IN ('local','ldap','ad','oidc','saml')),
        external_dn     TEXT,
        enabled         INTEGER NOT NULL DEFAULT 1,
        is_admin_seed   INTEGER NOT NULL DEFAULT 0,
        created_at      REAL NOT NULL,
        last_login_at   REAL DEFAULT 0,
        totp_secret     TEXT,
        totp_enabled    INTEGER NOT NULL DEFAULT 0,
        totp_required   INTEGER NOT NULL DEFAULT 0,
        is_audit_seed   INTEGER NOT NULL DEFAULT 0,
        UNIQUE(username, source)
    );
    INSERT INTO users_new
        (id, username, display_name, password_hash, source, external_dn,
         enabled, is_admin_seed, created_at, last_login_at,
         totp_secret, totp_enabled, totp_required, is_audit_seed)
    SELECT id, username, display_name, password_hash, source, external_dn,
           enabled, is_admin_seed, created_at, last_login_at,
           totp_secret, totp_enabled, totp_required, is_audit_seed
    FROM users;
    DROP TABLE users;
    ALTER TABLE users_new RENAME TO users;
    CREATE INDEX idx_users_username ON users(username);

    CREATE TABLE groups_new (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        name            TEXT NOT NULL,
        source          TEXT NOT NULL DEFAULT 'local'
                            CHECK (source IN ('local','ldap','ad','oidc','saml')),
        external_dn     TEXT,
        description     TEXT NOT NULL DEFAULT '',
        created_at      REAL NOT NULL,
        UNIQUE(source, name)
    );
    INSERT INTO groups_new (id, name, source, external_dn, description, created_at)
    SELECT id, name, source, external_dn, description, created_at FROM groups;
    DROP TABLE groups;
    ALTER TABLE groups_new RENAME TO groups;
    PRAGMA foreign_keys=ON;
    """)


def _m9_role_seed_snapshot(conn: sqlite3.Connection) -> None:
    """v9: 記錄「上次 seed 給過某內建角色哪些工具」的快照表（v1.12.53 起）。

    修正「admin 手動移除內建角色某工具 → 升級又被 seed top-up 補回來」的問題。
    `seed_builtin_roles()` 改成只補「這一版 seed 新增的」工具（seed − snapshot），
    不再補 admin 刻意移除的工具。詳見 roles.py:seed_builtin_roles docstring。

    這裡只建表，不寫入資料 —— 避免 migration import roles（會與 auth_db 形成
    循環）。首次 bootstrap（快照為空）由 seed_builtin_roles() 於啟動時處理：
    把當前這版的 seed 定義寫成基準線、本次不補任何工具（保守，保住既有 admin
    的移除設定）。"""
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS role_seed_snapshot (
        role_id     TEXT NOT NULL,
        tool_id     TEXT NOT NULL,
        PRIMARY KEY (role_id, tool_id)
    );
    """)


def _m10_role_default_for_new(conn: sqlite3.Connection) -> None:
    """v10: roles 加 `is_default_for_new` 欄位（v1.12.53 起）。

    新使用者（LDAP/AD/SSO/proxy JIT 開通、admin 建帳號沒指定角色）要對應到
    哪個角色，改由 admin 可設定 —— 可以是內建的 `default-user`，也可以是 admin
    複製 / 自建的自訂角色。恰有一個角色的此旗標為 1。

    ALTER ADD COLUMN 有預設值，不需重建表（避開 m8 那種 FK cascade 風險）。
    實際把 default-user 設為初始預設，交給 roles.seed_builtin_roles() 於啟動時
    ensure（migration 當下 roles 資料列可能還沒 seed）。"""
    conn.executescript("""
    ALTER TABLE roles ADD COLUMN is_default_for_new INTEGER NOT NULL DEFAULT 0;
    """)


def _m11_group_sync_cache(conn: sqlite3.Connection) -> None:
    """v11: groups 加 `member_count` / `member_count_synced_at` / `parent_dn`
    快取欄位（v1.12.67 起）。

    目錄（LDAP/AD）群組的「成員數」原本由群組管理頁**每一列**即時打一次 LDAP
    查詢取得（幾千個群組 = 幾千次連線，頁面等很久）。改由背景排程同步一次寫進
    本機快取，頁面直接讀欄位（毫秒）。`parent_dn` 存巢狀群組的上層群組 DN，供
    呈現群組的上下從屬關係（v1.12.68 用）。

    ALTER ADD COLUMN 皆帶預設值,不需重建表（避開 m8 那種 FK cascade 風險）。"""
    conn.executescript("""
    ALTER TABLE groups ADD COLUMN member_count INTEGER DEFAULT NULL;
    ALTER TABLE groups ADD COLUMN member_count_synced_at REAL DEFAULT NULL;
    ALTER TABLE groups ADD COLUMN parent_dn TEXT NOT NULL DEFAULT '';
    """)


def _m12_unprovision_mirrored_users(conn: sqlite3.Connection) -> None:
    """v12: 還原 v1.12.69 大量鏡射 AD/LDAP 使用者時的「誤開通」（v1.12.70 起）。

    v1.12.69 的 `sync_all_users` 把目錄所有使用者以 `enabled=1` + **自動給預設角色**
    灌進本機 `users` 表 —— 但「鏡射過來（可見、可指派）」不等於「已啟用（可登入
    使用）」。此 migration 把**從未真正登入過**的鏡射帳號還原成「僅目錄可見、未
    啟用」：

      - 目標 = `source IN ('ldap','ad') AND COALESCE(last_login_at,0)=0`
        （只有大量鏡射會產生 last_login=0 的目錄使用者；JIT / proxy 登入一定
         會寫 last_login > 0，所以這條精準命中被灌入且從未登入者）。
      - 移除這些人身上「當前預設角色」的指派（保留 admin 另外指派的其他角色）。
      - 設 `enabled=0`（去啟用）。已驗證 LDAP 登入先 AD 綁定再同步、不 pre-check
        enabled，所以這不擋他們日後真正登入（登入時 JIT 會重新設 enabled=1 並補
        預設角色）。

    **真正登入過（last_login>0）的使用者與所有本機帳號完全不動。** idempotent。"""
    row = conn.execute(
        "SELECT id FROM roles WHERE is_default_for_new=1 LIMIT 1").fetchone()
    default_role = row[0] if row else "default-user"
    conn.execute(
        "DELETE FROM subject_roles "
        "WHERE subject_type='user' AND role_id=? AND subject_key IN ("
        "  SELECT CAST(id AS TEXT) FROM users "
        "  WHERE source IN ('ldap','ad') AND COALESCE(last_login_at,0)=0)",
        (default_role,))
    conn.execute(
        "UPDATE users SET enabled=0 "
        "WHERE source IN ('ldap','ad') AND COALESCE(last_login_at,0)=0")


def _m13_grant_pdf_to_slides(conn: sqlite3.Connection) -> None:
    """v13：把 `pdf-to-slides`（PDF 轉簡報，v1.14.0 新增）補給既有角色。

    為什麼需要這條 migration —— `seed_builtin_roles()` 平常會自動補「這一版新增
    的工具」，但那個機制要有 `role_seed_snapshot` 當基準線才會動。**從
    v1.12.52 或更早直接升到現在的安裝，快照是空的 → 走保守的 bootstrap 路徑
    → 這一輪什麼都不補**（那條路徑是為了保住「admin 刻意移除某工具」的設定）。
    結果就是新工具對那些客戶永遠不出現，而且畫面上沒有任何線索說明原因。

    backfill 的判斷沿用 m4 / m5 的做法：**誰已經有 `pdf-to-office`，就給誰**
    —— 兩者是同一條轉檔路徑（同一顆引擎、同一批使用者），拿它當「這個角色本來
    就該看到轉檔工具」的訊號最準。不用 `pdf-merge` 之類的通用工具當訊號，那會
    連刻意收窄過的角色也一起放寬。

    `INSERT OR IGNORE` → 可重複執行；已經有的不動。
    """
    conn.executescript("""
    INSERT OR IGNORE INTO role_perms(role_id, tool_id)
        SELECT role_id, 'pdf-to-slides' FROM role_perms WHERE tool_id = 'pdf-to-office';
    INSERT OR IGNORE INTO subject_perms(subject_type, subject_key, tool_id)
        SELECT subject_type, subject_key, 'pdf-to-slides'
        FROM subject_perms WHERE tool_id = 'pdf-to-office';
    """)


def _m14_user_email(conn: sqlite3.Connection) -> None:
    """v14：`users` 加 `email` 欄位（v1.14.6 起）。

    為什麼需要 —— 作業完成通知要寄給送出的人，但系統裡**沒有任何地方存過使用者
    的信箱**。原本每個人都得自己到「我的作業 → 通知設定」手動填一次；接了
    AD / LDAP / SSO 的環境更不合理：來源系統早就有 `mail` 屬性 / `email` claim，
    卻在登入時被丟掉。

    這一欄由三個來源填：
      * AD / LDAP：登入與目錄同步時讀 `mail`（屬性名可在認證設定調整）
      * SSO：OIDC 的 email claim / SAML 的 email 屬性
      * 本機帳號：管理員在使用者管理填，或使用者自己在「我的帳號」改

    使用者在通知設定裡另外填的 `email_to` **優先**於這一欄 —— 那是他自己指定的
    收件位置，不可以被下一次目錄同步蓋掉。
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
    if "email" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN email TEXT NOT NULL DEFAULT ''")


def _m15_directory_presence(conn: sqlite3.Connection) -> None:
    """v15：`users` 加 `directory_seen_at`（目錄同步最後一次看到這個帳號的時間）。

    為什麼需要 —— 原本目錄同步**只增不減**：AD 那邊把人停用或刪掉之後，本站的
    帳號列、角色指派、群組成員關係全部還在。那個人登不進來（LDAP bind 會失敗），
    但「這個系統裡還有誰」這個問題答不出來，內控盤點與離職交接都對不起來。

    有了這一欄就能回答「上一次完整同步時，這個帳號還在不在目錄裡」：
      * 有值且等於最後一次同步時間 → 還在
      * 有值但比最後一次同步舊 → **目錄裡已經找不到了**（離職 / 停用 / 移出範圍）
      * NULL → 從來沒被同步涵蓋過（例如只登入過、或同步有用名稱過濾）

    **只有「完整同步」才能拿來判定消失**：帶了名稱過濾的同步只看得到一部分目錄，
    拿它去推論「其他人都不見了」會把整個組織誤標成離職。所以判定要同時看
    `directory_sync` 記錄的最後一次完整同步時間。
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
    if "directory_seen_at" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN directory_seen_at REAL")



def _m16_session_last_seen(conn: sqlite3.Connection) -> None:
    """v16：`sessions` 加 `last_seen_at`（這個 session 最後一次活動的時間）。

    為什麼需要 —— 管理員原本無法回答「現在有誰登入著」。`sessions` 只有
    `created_at` / `expires_at`：一個七天沒回來的人，他的 session 還是「有效」的，
    看起來跟正在使用的人沒有差別。

    有了這一欄就能算出「最近 N 分鐘有活動的人」= 在線人數，也能在踢人之前看出
    哪一個 session 是他現在用的、哪些是忘了登出的舊裝置。

    **更新要節流**：每個請求都寫一次 DB 會變成每請求一次寫入競爭（WAL 下仍是單一
    writer）。實務上只要 >60 秒才寫，對「在線」的判斷完全足夠。
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(sessions)").fetchall()}
    if "last_seen_at" not in cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN last_seen_at REAL")
        # 既有 session 沒有這個資訊 —— 用建立時間當起點，比 NULL 好推理
        conn.execute("UPDATE sessions SET last_seen_at = created_at "
                     "WHERE last_seen_at IS NULL")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_seen "
                     "ON sessions(last_seen_at)")


def _m17_directory_account_state(conn: sqlite3.Connection) -> None:
    """v17：`users` 加「目錄端的帳號狀態」兩欄。

    * `dir_disabled` —— AD 的 `userAccountControl` 有 ACCOUNTDISABLE 位元。
      目錄端停用之後，本站的帳號、角色指派、群組成員關係全部還在，管理員在
      使用者清單上完全看不出這個人已經被停用了（他登不進來是因為 bind 會失敗，
      不是因為我們知道）。
    * `pwd_expires_at` —— AD 的 `msDS-UserPasswordExpiryTimeComputed`。

      **不可以用網域的 `maxPwdAge` 自己算**：那個值忽略細緻密碼原則（PSO），也
      忽略「密碼永久有效」旗標，算出來的日期對套了 PSO 的人是錯的。這個構造屬性
      是 AD 自己算好的，兩者都吃得到。

    兩欄都可以是 NULL —— OpenLDAP 沒有這些概念，本機 / SSO 帳號更沒有。
    NULL 一律當「不知道」，畫面上什麼都不顯示（**不可以**當成「正常」或「已停用」）。
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
    if "dir_disabled" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN dir_disabled INTEGER")
    if "pwd_expires_at" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN pwd_expires_at REAL")


def _m18_grant_transit_proof_and_border(conn: sqlite3.Connection) -> None:
    """v18：把 `transit-proof`（v1.12.74）與 `pdf-border`（v1.14.11）補給既有角色。

    同 `_m13` 的理由 —— `seed_builtin_roles()` 的 top-up 要有
    `role_seed_snapshot` 當基準線才會動；從 v1.12.52 或更早直接升上來的安裝，
    快照是空的，會走保守的 bootstrap 路徑（**這一輪什麼都不補**，為了保住
    「admin 刻意移除某工具」的設定）。那條路徑之後新增的工具，如果沒有像這樣
    明確補一條 migration，對那些客戶就**永遠不會出現**，而且畫面上沒有任何線索。

    `pdf-to-slides` 當時補了（`_m13`），這兩支漏了 —— 盤點時才發現。

    補的對象沿用 m4 / m5 / m13 的做法：拿一個「本來就該看到同類工具」的既有
    授權當訊號，而不是無條件補給所有角色（那會把刻意收窄過的角色一起放寬）。
    * `transit-proof`（乘車證明整理）→ 誰有 `einvoice-scan`（同屬報帳流程）。
    * `pdf-border`（頁面加框）→ 誰有 `pdf-nup`（同屬版面處理，角色範圍一致）。

    `INSERT OR IGNORE` → 可重複執行；已經有的不動。
    """
    conn.executescript("""
    INSERT OR IGNORE INTO role_perms(role_id, tool_id)
        SELECT role_id, 'transit-proof' FROM role_perms WHERE tool_id = 'einvoice-scan';
    INSERT OR IGNORE INTO subject_perms(subject_type, subject_key, tool_id)
        SELECT subject_type, subject_key, 'transit-proof'
        FROM subject_perms WHERE tool_id = 'einvoice-scan';
    INSERT OR IGNORE INTO role_perms(role_id, tool_id)
        SELECT role_id, 'pdf-border' FROM role_perms WHERE tool_id = 'pdf-nup';
    INSERT OR IGNORE INTO subject_perms(subject_type, subject_key, tool_id)
        SELECT subject_type, subject_key, 'pdf-border'
        FROM subject_perms WHERE tool_id = 'pdf-nup';
    """)


def _m19_grant_pdf_bookmark(conn: sqlite3.Connection) -> None:
    """v19：把 `pdf-bookmark`（書籤與目錄，v1.14.20 新增）補給既有角色。

    理由同 `_m18` —— `seed_builtin_roles()` 的 top-up 要有 `role_seed_snapshot`
    當基準線才會動；從 v1.12.52 或更早直接升上來的安裝，快照是空的會走保守的
    bootstrap 路徑（這一輪什麼都不補），新工具對那些客戶就**永遠不會出現**。

    補的對象拿 `pdf-merge` 當訊號 —— 書籤與目錄最常見的用法就是「合併之後讓它
    有導覽」，誰看得到合併就該看得到這個。
    """
    conn.executescript("""
    INSERT OR IGNORE INTO role_perms(role_id, tool_id)
        SELECT role_id, 'pdf-bookmark' FROM role_perms WHERE tool_id = 'pdf-merge';
    INSERT OR IGNORE INTO subject_perms(subject_type, subject_key, tool_id)
        SELECT subject_type, subject_key, 'pdf-bookmark'
        FROM subject_perms WHERE tool_id = 'pdf-merge';
    """)


def _m20_grant_seam_stamp(conn: sqlite3.Connection) -> None:
    """v20：把 `pdf-seam-stamp`（騎縫章，v1.14.20 新增）補給既有角色。

    理由同 `_m18` / `_m19`（seed 快照的 bootstrap 缺口）。補的對象拿
    `pdf-stamp` 當訊號 —— 騎縫章就是用印的一種，誰能用印就該能蓋騎縫章。
    """
    conn.executescript("""
    INSERT OR IGNORE INTO role_perms(role_id, tool_id)
        SELECT role_id, 'pdf-seam-stamp' FROM role_perms WHERE tool_id = 'pdf-stamp';
    INSERT OR IGNORE INTO subject_perms(subject_type, subject_key, tool_id)
        SELECT subject_type, subject_key, 'pdf-seam-stamp'
        FROM subject_perms WHERE tool_id = 'pdf-stamp';
    """)


def _m21_grant_page_size(conn: sqlite3.Connection) -> None:
    """v21：把 `pdf-page-size`（頁面尺寸統一，v1.14.20 新增）補給既有角色。

    理由同 `_m18`~`_m20`（seed 快照的 bootstrap 缺口）。拿 `pdf-pages`
    （頁面整理）當訊號 —— 兩者同屬頁面層級的整理工作。
    """
    conn.executescript("""
    INSERT OR IGNORE INTO role_perms(role_id, tool_id)
        SELECT role_id, 'pdf-page-size' FROM role_perms WHERE tool_id = 'pdf-pages';
    INSERT OR IGNORE INTO subject_perms(subject_type, subject_key, tool_id)
        SELECT subject_type, subject_key, 'pdf-page-size'
        FROM subject_perms WHERE tool_id = 'pdf-pages';
    """)


def _m22_grant_office_convert(conn: sqlite3.Connection) -> None:
    """v22：把 `office-convert`（辦公文件格式互轉，v1.14.34 新增）補給既有角色。

    理由同 `_m18`~`_m21`（seed 快照的 bootstrap 缺口）—— 從舊版升級上來的
    安裝，角色早就存在，`seed_builtin_roles()` 的差集 top-up 以快照為基準，
    新工具不會自己長出來。

    拿 `office-to-pdf`（辦公文件轉 PDF）當訊號：兩者收的檔案完全一樣、
    都要 office 引擎，能轉 PDF 的人轉別的辦公格式沒有額外風險。
    **不可以無條件補給所有角色** —— 那會把刻意收窄過的角色（legal-sec 等）
    一起放寬。
    """
    conn.executescript("""
    INSERT OR IGNORE INTO role_perms(role_id, tool_id)
        SELECT role_id, 'office-convert' FROM role_perms
        WHERE tool_id = 'office-to-pdf';
    INSERT OR IGNORE INTO subject_perms(subject_type, subject_key, tool_id)
        SELECT subject_type, subject_key, 'office-convert'
        FROM subject_perms WHERE tool_id = 'office-to-pdf';
    """)


def _canon_ou_key(subject_key: str) -> str:
    """與 `permissions.canon_subject_key("ou", …)` **完全相同**的規則。

    刻意在這裡複寫一份而不 import：migration 要能獨立跑（升級流程、CLI 修復
    都可能在 permissions 還沒 import 前就跑到），而且規則一旦定版就不該再變 ——
    變了會讓不同版本升上來的 DB 正規化成不同結果。`tests/test_ou_key_canon.py`
    會斷言兩份實作對同一組輸入給出相同答案，漂掉就紅。
    """
    parts = []
    for rdn in str(subject_key or "").split(","):
        rdn = rdn.strip()
        if "=" in rdn:
            attr, _, val = rdn.partition("=")
            parts.append(f"{attr.strip().lower()}={val.strip()}")
        elif rdn:
            parts.append(rdn)
    return ",".join(parts)


def _m23_canon_ou_subject_keys(conn: sqlite3.Connection) -> None:
    """v23：把 subject_type='ou' 的 subject_key 正規化（屬性型別轉小寫、去空白）。

    OU 授權的比對是精確字串相等，但 DN 的屬性型別不分大小寫。管理介面正常操作
    寫進來的 key 與目錄樹同源、沒問題；但手動輸入 / 匯入改過的設定 / 換目錄來源
    可能寫成 `OU=Sales,DC=x`，於是登入時算出的 `ou=Sales,dc=x` 對不上 ——
    指派看起來成功，底下的人卻**無聲地**拿不到權限（2026-08-24 實測）。

    這支把既有資料一次校準。**逐筆記到 log**（升級時 admin 看得到到底動了誰），
    合併衝突（正規化後撞到已存在的列）時保留、跳過重複。
    只動 `ou`，`user` / `group` 的 key 是 id 不碰。
    """
    for table, keycol_extra in (("subject_roles", "role_id"),
                                ("subject_perms", "tool_id")):
        rows = conn.execute(
            f"SELECT rowid, subject_key, {keycol_extra} AS extra "
            f"FROM {table} WHERE subject_type='ou'"
        ).fetchall()
        for r in rows:
            # 位置索引 —— migration 不假設連線設了 row_factory（升級 / CLI 修復
            # 都可能拿到裸連線）。SELECT 順序：rowid, subject_key, extra。
            rowid, old_key, extra = r[0], r[1], r[2]
            new_key = _canon_ou_key(old_key)
            if new_key == old_key:
                continue
            # 正規化後是否已存在等價列？（同 new_key + 同 role/tool）
            dup = conn.execute(
                f"SELECT 1 FROM {table} WHERE subject_type='ou' "
                f"AND subject_key=? AND {keycol_extra}=?",
                (new_key, extra),
            ).fetchone()
            if dup:
                conn.execute(f"DELETE FROM {table} WHERE rowid=?", (rowid,))
                logger.info("OU key 正規化（%s）：'%s' → '%s'（已存在等價列，"
                            "移除重複，%s=%s）", table, old_key, new_key,
                            keycol_extra, extra)
            else:
                conn.execute(
                    f"UPDATE {table} SET subject_key=? WHERE rowid=?",
                    (new_key, rowid))
                logger.info("OU key 正規化（%s）：'%s' → '%s'（%s=%s）",
                            table, old_key, new_key, keycol_extra, extra)


def _m24_index_group_members_user(conn: sqlite3.Connection) -> None:
    """`group_members` 補一個 `user_id` 的索引。

    客戶回報「刪使用者會卡住，多刪幾個系統就像掛掉」。根因之一在這裡：
    這張表的主鍵是 `(group_id, user_id)`，**用 user_id 單獨查用不到那個索引**
    （不是前綴）。而 `users` 的刪除會觸發 `group_members` 的 ON DELETE
    CASCADE —— 於是每刪一個帳號就整表掃描一次。

    目錄同步會把整個網域的帳號鏡射進來（客戶那邊 18,611 位），群組成員
    表跟著長到幾十萬列，一次掃描就是好幾百毫秒，連刪十幾個就是好幾秒，
    而且那段時間**整站都不會回應**（同步的資料庫操作跑在事件迴圈上）。

    實測（18,000 使用者 / 90,000 成員列）：刪 20 個帳號 0.42 秒 → 0.042 秒。
    """
    conn.executescript(
        "CREATE INDEX IF NOT EXISTS idx_group_members_user "
        "ON group_members(user_id);")


def _m25_grant_doc_translate(conn: sqlite3.Connection) -> None:
    """v25：把 `doc-translate`（文件翻譯，v1.14.67 新增）補給既有角色。

    理由同 `_m18`~`_m22`（seed 快照的 bootstrap 缺口）—— 從舊版升上來的安裝，
    角色早就存在，`seed_builtin_roles()` 的差集 top-up 以快照為基準，新工具
    不會自己長出來。

    拿 `translate-doc`（逐句翻譯）當訊號：同一件事（把文件內容送去翻譯），
    只是產出從對照表變成同格式的檔案 —— 能用逐句翻譯的人用這支沒有額外風險。
    **不可以無條件補給所有角色**，那會把刻意收窄過的角色一起放寬。
    """
    conn.executescript("""
    INSERT OR IGNORE INTO role_perms(role_id, tool_id)
        SELECT role_id, 'doc-translate' FROM role_perms
        WHERE tool_id = 'translate-doc';
    INSERT OR IGNORE INTO subject_perms(subject_type, subject_key, tool_id)
        SELECT subject_type, subject_key, 'doc-translate'
        FROM subject_perms WHERE tool_id = 'translate-doc';
    """)


MIGRATIONS = [_m1_initial, _m2_username_source_unique,
              _m3_rename_pdf_diff_to_doc_diff,
              _m4_grant_image_to_pdf,
              _m5_grant_translate_doc,
              _m6_totp_columns,
              _m7_audit_seed_column,
              _m8_sso_sources,
              _m9_role_seed_snapshot,
              _m10_role_default_for_new,
              _m11_group_sync_cache,
              _m12_unprovision_mirrored_users,
              _m13_grant_pdf_to_slides,
              _m14_user_email,
              _m15_directory_presence,
              _m16_session_last_seen, _m17_directory_account_state,
              _m18_grant_transit_proof_and_border,
              _m19_grant_pdf_bookmark, _m20_grant_seam_stamp,
              _m21_grant_page_size,
              _m22_grant_office_convert,
              _m23_canon_ou_subject_keys,
              _m24_index_group_members_user,
              _m25_grant_doc_translate]


def auth_db_path() -> Path:
    from ..config import settings
    return settings.data_dir / "auth.sqlite"


def init() -> None:
    """Apply all pending migrations. Idempotent — safe to call on every boot."""
    path = auth_db_path()
    final = _db.migrate(path, MIGRATIONS)
    logger.info("auth DB ready at %s (schema v%d)", path, final)


def conn():
    """Shortcut: thread-local connection to the auth DB."""
    return _db.get_conn(auth_db_path())
