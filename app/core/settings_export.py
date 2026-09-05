"""全站設定匯出 / 匯入 — 以「類別」為單位，匯出與匯入都可逐項勾選。

每個設定類別（category）對應 data/ 內的一組檔案 / 目錄，或（RBAC）auth.sqlite
內的角色/權限資料。匯出時 admin 可勾選要包含哪些類別（預設全含，歷史記錄預設
不含）；匯入時先讀備份檔的 manifest 看裡面有哪些類別，再逐項勾選只還原哪些。

**RBAC 類別**（角色與權限）匯出：roles（含 is_default_for_new）、role_perms、
role_seed_snapshot、以及 OU 層級的權限規則（subject_type='ou'，key 是 DN 字串，
可跨機）。**不含**使用者帳號 / 密碼 hash、也不含綁 user/group id 的個別指派
（那些換機器 id 對不上，搬過去沒意義）。

**永遠不含**：temp/ jobs/ audit.sqlite、auth.sqlite 的 users/密碼、以及
`.session_secret`（那是 session 簽章金鑰，外流等於可偽造登入）。

**加新設定檔就要加分類** —— 漏加不會有任何錯誤訊息，只會在客戶搬機還原後才發現
設定不見了。`tools/check_settings_export_coverage.py` 會掃描程式碼中所有 data_dir
下的設定檔引用並比對本清單，發版前必跑。

匯入是「合併」非「整清」：覆寫前先把現有對應檔備份成 `<name>.bak.<ts>`。
匯出檔名：`jtdt-settings-YYYYMMDD-HHMMSS-vX.Y.Z.zip`
"""
from __future__ import annotations

import json
import shutil
import time
import zipfile
from pathlib import Path
from typing import Optional

from . import safe_paths
from ..config import settings

MANIFEST_NAME = "manifest.json"
RBAC_NAME = "rbac.json"


# ---- Category registry --------------------------------------------------
# kind:
#   "files" — items are single files relative to data_dir
#   "dirs"  — items are directories relative to data_dir (packed recursively)
#   "rbac"  — special: dumped from / merged into auth.sqlite (see _rbac_*)
# default: pre-checked in the export UI (history is opt-in).
CATEGORIES: list[dict] = [
    {"id": "auth", "label": "認證設定", "kind": "files",
     "items": ["auth_settings.json"],
     "desc": "認證後端 / LDAP / AD 連線與對應設定（SSO 另見「SSO 單一登入」）",
     "default": True},
    {"id": "sso", "label": "SSO 單一登入", "kind": "files",
     "items": ["sso_settings.json"], "rekey": "sso",
     "desc": "OIDC / SAML 設定（含用戶端密鑰、SP 私鑰 — 敏感）",
     "default": True, "sensitive": True},
    {"id": "notify", "label": "通知設定", "kind": "files",
     "items": ["notify_settings.json"], "rekey": "notify",
     "desc": "作業完成通知的管道憑證（SMTP 帳密、bot token、webhook URL — 敏感）",
     "default": True, "sensitive": True},
    {"id": "notify_prefs", "label": "個人收訊偏好", "kind": "dirs",
     "items": ["notify_prefs"],
     "desc": "各使用者的通知開關、信箱、Telegram / LINE 收訊者",
     "default": True},
    {"id": "directory", "label": "目錄同步 / 過濾", "kind": "files",
     "items": ["directory_sync.json", "dir_filter.json"],
     "desc": "AD / LDAP 目錄同步排程與瀏覽過濾條件", "default": True},
    {"id": "log_forward", "label": "記錄轉送", "kind": "files",
     "items": ["log_forwarders.json"],
     "desc": "稽核記錄轉送目的地（syslog / CEF / GELF）", "default": True},
    {"id": "concurrency", "label": "併行度設定", "kind": "files",
     "items": ["concurrency.json", "job_priority.json"],
     "desc": "同時可處理的工作數、Office 轉檔同時數、外部服務同時呼叫數、轉檔 CPU 上限、記憶體保留量、優先派送名單",
     "default": True},
    {"id": "retention", "label": "檔案保留 / 清理", "kind": "files",
     "items": ["retention.json"],
     "desc": "各類資料的保留天數與自動清理排程", "default": True},
    {"id": "scheduled_export", "label": "排程備份設定", "kind": "files",
     "items": ["scheduled_export.json"],
     "desc": "設定備份的排程與匯出目錄", "default": True},
    {"id": "rbac", "label": "角色與權限", "kind": "rbac", "items": [],
     "desc": "角色定義、工具權限、新使用者預設角色、OU 權限規則（不含使用者 / 密碼）",
     "default": True},
    {"id": "profile", "label": "公司資料", "kind": "files",
     "items": ["profile.json"], "desc": "office_profile 公司資料 profile", "default": True},
    {"id": "office_profile", "label": "公司資料（附件）", "kind": "dirs",
     "items": ["office_profile"], "desc": "profile 附帶檔案", "default": True},
    {"id": "synonyms", "label": "同義詞對照", "kind": "files",
     "items": ["label_synonyms.json"], "desc": "欄位標籤同義詞", "default": True},
    {"id": "form_templates", "label": "表單範本", "kind": "files",
     "items": ["form_templates.json"], "desc": "pdf-fill 表單範本", "default": True},
    {"id": "api_tokens", "label": "API Token", "kind": "files",
     "items": ["api_tokens.json"], "desc": "對外 API 存取權杖（敏感）",
     "default": True, "sensitive": True},
    {"id": "llm", "label": "LLM 設定", "kind": "files",
     "items": ["llm_settings.json"], "desc": "LLM server / 模型 / 參數", "default": True},
    {"id": "ocr", "label": "OCR 設定", "kind": "files",
     "items": ["ocr_settings.json", "ocr_remote.json"],
     "desc": "預設 OCR 引擎；遠端 GPU OCR 伺服器位址與存取權杖（敏感）",
     "default": True, "sensitive": True},
    {"id": "office_paths", "label": "Office 路徑", "kind": "files",
     "items": ["office_paths.json"], "desc": "soffice / OxOffice 執行檔路徑", "default": True},
    {"id": "fonts", "label": "自訂字型", "kind": "files_and_dirs",
     "items": ["font_settings.json"], "dirs": ["fonts"],
     "desc": "上傳的 TTF / OTF 字型 + 設定", "default": True},
    {"id": "assets", "label": "資產（印章 / 簽名 / Logo）", "kind": "dirs",
     "items": ["assets"], "desc": "印章 / 簽名 / logo 圖與 metadata", "default": True},
    {"id": "branding", "label": "品牌 Logo", "kind": "dirs",
     "items": ["branding"], "desc": "企業 logo", "default": True},
    {"id": "scan_prefs", "label": "掃描工具欄位偏好", "kind": "dirs",
     "items": ["transit_proof_settings", "einvoice_settings"],
     "desc": "乘車證明 / 電子發票的欄位顯示、排序、匯出標籤（各使用者一份）",
     "default": True},
    {"id": "scan_buffers", "label": "掃描暫存資料", "kind": "dirs",
     "items": ["transit_proof_buffer", "einvoice_buffer"],
     "desc": "使用者掃到一半、尚未匯出的乘車證明 / 發票資料（量大，搬機通常不需要）",
     "default": False},
    {"id": "submission_check", "label": "送件檢查（自家實體）", "kind": "dirs",
     "items": ["submission_check/self_entities"],
     "desc": "自家公司實體清單（個別 case 屬工作資料，不隨設定備份）",
     "default": True},
    {"id": "workspace", "label": "使用者工作區", "kind": "files_and_dirs",
     "items": ["workspace.json"], "dirs": ["workspace"],
     "desc": "使用者存放的檔案與 metadata（量大，搬機通常不需要）",
     "default": False},
    {"id": "history_fill", "label": "表單填寫歷史", "kind": "dirs",
     "items": ["fill_history"], "desc": "使用者填單歷史（量大，搬機通常不需要）",
     "default": False},
    {"id": "history_stamp", "label": "用印簽名歷史", "kind": "dirs",
     "items": ["stamp_history"], "desc": "用印歷史", "default": False},
    {"id": "history_watermark", "label": "浮水印歷史", "kind": "dirs",
     "items": ["watermark_history"], "desc": "浮水印歷史", "default": False},
]

_CAT_BY_ID = {c["id"]: c for c in CATEGORIES}


def _cat_files(cat: dict) -> list[str]:
    if cat["kind"] in ("files", "files_and_dirs"):
        return list(cat.get("items", []))
    return []


def _cat_dirs(cat: dict) -> list[str]:
    if cat["kind"] == "dirs":
        return list(cat.get("items", []))
    if cat["kind"] == "files_and_dirs":
        return list(cat.get("dirs", []))
    return []


# ---- SSO 祕密重新加密 ----------------------------------------------------
# sso_settings.json 內的 client secret / SP 私鑰是用**本機** data/.session_secret
# 當金鑰做 Fernet 加密的。直接把檔案複製到另一台機器，密文解不開 —— 設定看起來
# 都還在，SSO 卻會在還原後無聲失效。而 .session_secret 同時是 session 簽章金鑰，
# 放進備份檔等於把登入偽造能力一起送出去 → 只能「匯出時解密、匯入時用目標機器的
# 金鑰重新加密」。備份檔內因此是明文，與既有的 LDAP 服務帳號密碼 / API token
# 同級（該分類標記 sensitive）。
_SSO_PLAINTEXT_KEY = "_jtdt_secrets_plaintext"


def _rekey_specs() -> dict:
    """需要「匯出解密、匯入重新加密」的檔案 → (模組, 取得該檔祕密欄位的函式)。

    兩個檔案都用同一把 Fernet 金鑰（`data/.session_secret`），所以跨機都會遇到
    一樣的問題。共用同一套處理，新增這類檔案時只要在這裡加一列。
    """
    from . import notify_settings as _ns, sso_settings as _sso
    return {
        # sso_settings.json：祕密在 data["oidc"]["client_secret_enc"] 這種兩層結構
        "sso_settings.json": (_sso, lambda d: [
            (d.get(sec), fld) for sec, fld in _sso.SECRET_FIELDS
            if isinstance(d.get(sec), dict)]),
        # notify_settings.json：祕密在 data["channels"][管道][欄位]
        "notify_settings.json": (_ns, lambda d: [
            ((d.get("channels") or {}).get(ch), fld)
            for ch, fld in _ns.SECRET_FIELDS
            if isinstance((d.get("channels") or {}).get(ch), dict)]),
    }


def _rekey_export_blob(name: str) -> Optional[str]:
    """回傳「祕密已解密」的設定檔內容；檔案不存在回 None。"""
    p = settings.data_dir / name
    if not p.is_file():
        return None
    mod, locate = _rekey_specs()[name]
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    for holder, field in locate(data):
        if holder and holder.get(field):
            holder[field] = mod.decrypt_secret(holder[field])
    data[_SSO_PLAINTEXT_KEY] = True
    return json.dumps(data, ensure_ascii=False, indent=2)


def _rekey_after_import(target: Path) -> None:
    """把剛還原的設定檔內明文祕密改用**本機**金鑰加密。

    沒有標記的（v1.14.5 以前匯出的備份）代表裡面是**別台機器的密文**，本機解不開
    → 原樣留著，由管理員重新輸入祕密即可，不要試圖解密（會變成空字串，設定看似
    存在卻是壞的）。
    """
    spec = _rekey_specs().get(target.name)
    if not spec:
        return
    mod, locate = spec
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not isinstance(data, dict) or not data.pop(_SSO_PLAINTEXT_KEY, False):
        return
    for holder, field in locate(data):
        if holder and holder.get(field):
            holder[field] = mod.encrypt_secret(holder[field])
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                      encoding="utf-8")
    try:
        target.chmod(0o600)
    except OSError:      # Windows / 不支援 chmod 的檔案系統
        pass


# 舊名保留，既有測試與呼叫端仍可用
def _sso_export_blob() -> Optional[str]:
    return _rekey_export_blob("sso_settings.json")


def _sso_rekey_after_import(target: Path) -> None:
    _rekey_after_import(target)


def _dir_stats(p: Path) -> tuple[int, int]:
    total = count = 0
    if p.is_dir():
        for f in p.rglob("*"):
            if f.is_file():
                total += f.stat().st_size
                count += 1
    return count, total


# ---- RBAC dump / merge (auth.sqlite) ------------------------------------

def _rbac_dump() -> dict:
    """Serialise portable RBAC config from auth.sqlite. No users/passwords."""
    from . import auth_db
    conn = auth_db.conn()
    roles = [dict(r) for r in conn.execute(
        "SELECT id, display_name, description, is_builtin, is_protected, "
        "is_default_for_new FROM roles").fetchall()]
    role_perms = [[r["role_id"], r["tool_id"]] for r in conn.execute(
        "SELECT role_id, tool_id FROM role_perms").fetchall()]
    snapshot = [[r["role_id"], r["tool_id"]] for r in conn.execute(
        "SELECT role_id, tool_id FROM role_seed_snapshot").fetchall()]
    # OU-level assignments only (subject_key is a DN string → portable).
    ou_roles = [[r["subject_key"], r["role_id"]] for r in conn.execute(
        "SELECT subject_key, role_id FROM subject_roles WHERE subject_type='ou'"
    ).fetchall()]
    ou_perms = [[r["subject_key"], r["tool_id"]] for r in conn.execute(
        "SELECT subject_key, tool_id FROM subject_perms WHERE subject_type='ou'"
    ).fetchall()]
    return {"roles": roles, "role_perms": role_perms,
            "role_seed_snapshot": snapshot,
            "ou_subject_roles": ou_roles, "ou_subject_perms": ou_perms}


def _rbac_summary() -> dict:
    try:
        d = _rbac_dump()
        return {"roles": len(d["roles"]), "role_perms": len(d["role_perms"]),
                "ou_rules": len(d["ou_subject_roles"]) + len(d["ou_subject_perms"])}
    except Exception:
        return {"roles": 0, "role_perms": 0, "ou_rules": 0}


def _rbac_merge(data: dict) -> dict:
    """Merge an RBAC dump into auth.sqlite. Upserts role definitions + perms +
    snapshot + OU rules. Custom roles are created; built-in role metadata is
    updated but is_builtin/is_protected are preserved from the imported flag.
    Enforces the single is_default_for_new invariant. Returns a small summary.
    """
    from . import auth_db, db
    # Roles that a crafted import backup must NEVER be able to hand out — a
    # confused-deputy admin importing an attacker's "backup" could otherwise
    # escalate: make `admin` the new-user default, or grant admin to a whole
    # OU. Mirror roles._INELIGIBLE_DEFAULT_ROLES.
    _INELIGIBLE_ROLES = {"admin", "auditor"}
    conn = auth_db.conn()
    roles = data.get("roles") or []
    role_perms = data.get("role_perms") or []
    snapshot = data.get("role_seed_snapshot") or []
    ou_roles = data.get("ou_subject_roles") or []
    ou_perms = data.get("ou_subject_perms") or []
    now = time.time()
    imported_default = None
    with db.tx(conn):
        for r in roles:
            rid = r.get("id")
            if not rid:
                continue
            exists = conn.execute("SELECT 1 FROM roles WHERE id=?", (rid,)).fetchone()
            if exists:
                conn.execute(
                    "UPDATE roles SET display_name=?, description=? WHERE id=?",
                    (r.get("display_name") or rid, r.get("description") or "", rid))
            else:
                # An imported (new) role is always a CUSTOM role: force
                # is_builtin=0 / is_protected=0 so a backup can't plant an
                # undeletable "protected/builtin" fake role.
                conn.execute(
                    "INSERT INTO roles(id, display_name, description, is_builtin, "
                    "is_protected, is_default_for_new, created_at) "
                    "VALUES (?,?,?,0,0,0,?)",
                    (rid, r.get("display_name") or rid, r.get("description") or "", now))
            # Never let an import set admin/auditor as the new-user default
            # (that would bypass roles.set_default_role_id's guard and make
            # every JIT-provisioned user an admin).
            if r.get("is_default_for_new") and rid not in _INELIGIBLE_ROLES:
                imported_default = rid
        # Replace role_perms for the imported roles only.
        imported_role_ids = {r.get("id") for r in roles if r.get("id")}
        for rid in imported_role_ids:
            conn.execute("DELETE FROM role_perms WHERE role_id=?", (rid,))
            conn.execute("DELETE FROM role_seed_snapshot WHERE role_id=?", (rid,))
        for rid, tool in role_perms:
            if rid in imported_role_ids:
                conn.execute("INSERT OR IGNORE INTO role_perms(role_id, tool_id) "
                             "VALUES (?,?)", (rid, tool))
        for rid, tool in snapshot:
            if rid in imported_role_ids:
                conn.execute("INSERT OR IGNORE INTO role_seed_snapshot(role_id, "
                             "tool_id) VALUES (?,?)", (rid, tool))
        for key, rid in ou_roles:
            # Block importing an OU→admin/auditor grant (e.g. granting admin to
            # the domain root DN = escalate the whole directory).
            if rid in _INELIGIBLE_ROLES:
                continue
            conn.execute("INSERT OR IGNORE INTO subject_roles(subject_type, "
                         "subject_key, role_id) VALUES ('ou', ?, ?)", (key, rid))
        for key, tool in ou_perms:
            conn.execute("INSERT OR IGNORE INTO subject_perms(subject_type, "
                         "subject_key, tool_id) VALUES ('ou', ?, ?)", (key, tool))
        if imported_default:
            conn.execute("UPDATE roles SET is_default_for_new=0 "
                         "WHERE is_default_for_new=1")
            conn.execute("UPDATE roles SET is_default_for_new=1 WHERE id=?",
                         (imported_default,))
    try:
        from . import permissions as _perm
        _perm.invalidate_cache()
    except Exception:
        pass
    return {"roles": len(roles), "role_perms": len(role_perms),
            "ou_rules": len(ou_roles) + len(ou_perms)}


# ---- public API ---------------------------------------------------------

def list_categories() -> list[dict]:
    """For the export UI: every category with presence + size, so admin can
    tick which to include (default-checked flag included)."""
    out = []
    for c in CATEGORIES:
        present = False
        size = 0
        count = 0
        if c["kind"] == "rbac":
            s = _rbac_summary()
            present = s["roles"] > 0
            count = s["roles"]
            detail = f"{s['roles']} 個角色 / {s['role_perms']} 項權限 / {s['ou_rules']} 條 OU 規則"
        else:
            for fn in _cat_files(c):
                p = settings.data_dir / fn
                if p.is_file():
                    present = True
                    size += p.stat().st_size
                    count += 1
            for dn in _cat_dirs(c):
                p = settings.data_dir / dn
                if p.is_dir():
                    fc, fs = _dir_stats(p)
                    if fc:
                        present = True
                    size += fs
                    count += fc
            detail = f"{count} 個檔案・{size/1024:.1f} KB" if present else "（無）"
        # `detail` 是**組好的中文字串**，沒辦法當翻譯的鍵（數字每次都不一樣）。
        # 另外附上組成的零件，樣板才有辦法用 `tr()` 各自翻再拼起來。
        parts = ({"kind": "rbac", "roles": s["roles"], "perms": s["role_perms"],
                  "ou": s["ou_rules"]} if c["kind"] == "rbac"
                 else {"kind": "files" if present else "none",
                       "count": count, "kb": round(size / 1024, 1)})
        out.append({
            "detail_parts": parts,
            "id": c["id"], "label": c["label"], "desc": c["desc"],
            "kind": c["kind"], "default": c.get("default", True),
            "sensitive": c.get("sensitive", False),
            "present": present, "count": count, "size": size, "detail": detail,
        })
    return out


def export_to_zip(out_path: Path, selected_ids: Optional[list[str]] = None,
                  app_version: str = "") -> dict:
    """Pack the SELECTED categories into out_path. If selected_ids is None,
    include every default-on category (back-compat convenience)."""
    if selected_ids is None:
        selected_ids = [c["id"] for c in CATEGORIES if c.get("default", True)]
    selected = [c for c in CATEGORIES if c["id"] in set(selected_ids)]
    entries_by_cat: dict[str, list[str]] = {}
    files_added = 0
    total_bytes = 0
    # out_path 來自 admin 設定的匯出目錄。目錄本身經 safe_output_dir 驗證（絕對路徑、
    # 非系統目錄），檔名則是本程式產生的固定樣板（jtdt-settings-<時間>-v<版本>.zip），
    # 不含使用者輸入。
    out_dir = safe_paths.safe_output_dir(Path(out_path).parent)
    out_path = out_dir / safe_paths.sanitize_filename(Path(out_path).name)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for c in selected:
            names: list[str] = []
            if c["kind"] == "rbac":
                blob = json.dumps(_rbac_dump(), ensure_ascii=False, indent=2)
                zf.writestr(RBAC_NAME, blob)
                names.append(RBAC_NAME)
                files_added += 1
                total_bytes += len(blob.encode("utf-8"))
            else:
                for fn in _cat_files(c):
                    p = settings.data_dir / fn
                    if not p.is_file():
                        continue
                    arc = f"data/{fn}"
                    if c.get("rekey"):
                        # 密文換明文後才寫進備份（見 _rekey_export_blob）
                        blob = _rekey_export_blob(fn)
                        if blob is None:
                            continue
                        zf.writestr(arc, blob)
                        total_bytes += len(blob.encode("utf-8"))
                    else:
                        zf.write(p, arcname=arc)
                        total_bytes += p.stat().st_size
                    names.append(arc)
                    files_added += 1
                for dn in _cat_dirs(c):
                    p = settings.data_dir / dn
                    if not p.is_dir():
                        continue
                    for f in p.rglob("*"):
                        if not f.is_file():
                            continue
                        rel = f.relative_to(settings.data_dir)
                        arc = f"data/{rel.as_posix()}"
                        zf.write(f, arcname=arc)
                        names.append(arc)
                        files_added += 1
                        total_bytes += f.stat().st_size
            if names:
                entries_by_cat[c["id"]] = names
        manifest = {
            "kind": "jtdt-settings-export",
            "schema_version": 2,
            "app_version": app_version or "unknown",
            "exported_at": time.time(),
            "exported_at_iso": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "file_count": files_added,
            "total_bytes": total_bytes,
            "categories": [
                {"id": c["id"], "label": c["label"]}
                for c in selected if c["id"] in entries_by_cat
            ],
            "entries_by_category": entries_by_cat,
        }
        zf.writestr(MANIFEST_NAME, json.dumps(manifest, indent=2, ensure_ascii=False))
    return {"out_path": str(out_path), "file_count": files_added,
            "total_bytes": total_bytes, "manifest": manifest}


def read_manifest(zip_path: Path) -> dict:
    """Read + validate a backup zip's manifest so the import UI can show which
    categories are inside. Raises ValueError if not a valid jtdt export."""
    if not zip_path.exists():
        raise FileNotFoundError(f"zip not found: {zip_path}")
    with zipfile.ZipFile(zip_path, "r") as zf:
        names = zf.namelist()
        if MANIFEST_NAME not in names:
            raise ValueError("missing manifest — not a jtdt settings export?")
        manifest = json.loads(zf.read(MANIFEST_NAME).decode("utf-8"))
        if manifest.get("kind") != "jtdt-settings-export":
            raise ValueError(f"unknown export kind: {manifest.get('kind')!r}")
    # Present categories (schema v2). Fall back for v1 (no per-category map).
    cats = manifest.get("categories")
    if not cats:
        cats = [{"id": "legacy", "label": "（舊版備份，整包匯入）"}]
    # decorate with label from registry if available
    for c in cats:
        reg = _CAT_BY_ID.get(c["id"])
        if reg:
            c["desc"] = reg["desc"]
    manifest["categories"] = cats
    return manifest


def import_from_zip(zip_path: Path, selected_ids: Optional[list[str]] = None,
                    app_version: str = "") -> dict:
    """Restore only the SELECTED categories from a backup zip. Backs up any
    existing target file/dir to `<name>.bak.<ts>` first. selected_ids=None
    restores everything in the zip (back-compat)."""
    if not zip_path.exists():
        raise FileNotFoundError(f"zip not found: {zip_path}")
    with zipfile.ZipFile(zip_path, "r") as zf:
        names = zf.namelist()
        if MANIFEST_NAME not in names:
            raise ValueError("missing manifest — not a jtdt settings export?")
        manifest = json.loads(zf.read(MANIFEST_NAME).decode("utf-8"))
        if manifest.get("kind") != "jtdt-settings-export":
            raise ValueError(f"unknown export kind: {manifest.get('kind')!r}")
        # zip-slip defence + decompression-bomb cap: only manifest, rbac.json,
        # and data/ entries allowed; reject archives that expand beyond sane
        # limits (a small crafted zip can inflate to GBs → OOM/disk fill).
        _MAX_TOTAL = 2 * 1024 * 1024 * 1024   # 2 GiB total uncompressed
        _MAX_FILE = 512 * 1024 * 1024         # 512 MiB per member
        total_uncompressed = 0
        for info in zf.infolist():
            name = info.filename
            if name in (MANIFEST_NAME, RBAC_NAME):
                continue
            if not name.startswith("data/") or ".." in Path(name).parts:
                raise ValueError(f"unsafe path in zip: {name!r}")
            if info.file_size > _MAX_FILE:
                raise ValueError("備份檔內有過大的檔案（疑似解壓縮炸彈），已中止")
            total_uncompressed += info.file_size
            if total_uncompressed > _MAX_TOTAL:
                raise ValueError("備份檔解壓後過大（疑似解壓縮炸彈），已中止")

        entries_by_cat = manifest.get("entries_by_category") or {}
        is_legacy = not entries_by_cat
        if selected_ids is None:
            # **`rbac.json` 不可以混進來**。它在 zip 的根目錄（不是 `data/`
            # 底下），而下面解壓時每個項目都會做 `relative_to("data")` ——
            # 混進來就是 `ValueError`，於是「全部還原」這條路**一定失敗**
            # （v1.14.31 對抗式驗證：自己匯出的檔自己匯不回去）。
            # 另外兩條分支都正確排除了它，只有這條漏掉；RBAC 由 `do_rbac`
            # 那段單獨處理。
            wanted_entries = set(n for n in names
                                 if n not in (MANIFEST_NAME, RBAC_NAME))
            do_rbac = RBAC_NAME in names
        elif is_legacy:
            # v1 backup has no category map — restore all its data/ entries.
            wanted_entries = set(n for n in names if n.startswith("data/"))
            do_rbac = RBAC_NAME in names and "rbac" in selected_ids
        else:
            wanted_entries = set()
            for cid in selected_ids:
                for n in (entries_by_cat.get(cid) or []):
                    if n != RBAC_NAME:
                        wanted_entries.add(n)
            do_rbac = ("rbac" in selected_ids) and (RBAC_NAME in names)

        ts = time.strftime("%Y%m%d_%H%M%S")
        backup_paths: list[str] = []
        imported_files = 0
        restored_cats: list[str] = []

        # Back up + extract file/dir entries.
        # Back up each distinct top-level target once.
        backed_up: set[str] = set()
        for name in sorted(wanted_entries):
            rel = Path(name).relative_to("data")
            target = settings.data_dir / rel
            top = rel.parts[0] if rel.parts else ""
            if top and top not in backed_up:
                backed_up.add(top)
                src_top = settings.data_dir / top
                if src_top.exists():
                    bak = settings.data_dir / f"{top}.bak.{ts}"
                    if src_top.is_dir():
                        shutil.copytree(src_top, bak, dirs_exist_ok=True)
                    else:
                        shutil.copy2(src_top, bak)
                    backup_paths.append(str(bak))
            target.parent.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(zip_path, "r") as zf2, zf2.open(name) as src, \
                    open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
            # **`.json` 要先確認解析得過再收下**。原本是原樣寫入，於是一份
            # 不完整的備份可以把 `auth_settings.json` 換成壞掉的內容 ——
            # 而壞掉的認證設定曾經等於「全站認證關閉」（v1.14.31 對抗式驗證）。
            # 讀取端現在會 fail-secure，但讓壞資料落地本身就不該發生：
            # 寫壞了就還原剛剛備份的那一份，並讓整批匯入失敗。
            if target.suffix.lower() == ".json":
                try:
                    json.loads(target.read_text(encoding="utf-8"))
                except Exception as exc:  # noqa: BLE001
                    target.unlink(missing_ok=True)
                    raise ValueError(
                        f"備份檔內的 {name} 不是合法的 JSON，已中止匯入：{exc}")
            if Path(name).name in _rekey_specs():
                _rekey_after_import(target)
            imported_files += 1

        rbac_result = None
        if do_rbac:
            with zipfile.ZipFile(zip_path, "r") as zf2:
                blob = zf2.read(RBAC_NAME).decode("utf-8")
            rbac_result = _rbac_merge(json.loads(blob))
            restored_cats.append("rbac")

    # Which categories were actually restored (for the response).
    if not is_legacy:
        for cid in (selected_ids if selected_ids is not None
                    else list(entries_by_cat.keys())):
            if cid != "rbac" and entries_by_cat.get(cid):
                restored_cats.append(cid)

    return {"imported_files": imported_files, "manifest": manifest,
            "backup_paths": backup_paths, "restored_categories": restored_cats,
            "rbac": rbac_result,
            "imported_at_iso": time.strftime("%Y-%m-%dT%H:%M:%S")}
