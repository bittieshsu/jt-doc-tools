"""User workspace — per-account server-side storage for tool outputs.

Users click 「存至工作區」 on a tool's PDF/PNG output to keep the file on the
server under their own account; 「從工作區載入」 feeds a saved file back into
any tool's upload box. The admin enables / disables the whole feature and sets
a single uniform per-user quota + retention (no per-user overrides). When the
feature is disabled, no UI nor endpoint is exposed.

Storage layout::

    data/workspace/<user_key>/<file_id>/
        ├─ file.pdf | file.png      ← the stored artefact (fixed safe name)
        └─ meta.json                ← {file_id, name, ext, mime, size,
                                        source_tool, saved_at, user_label}

``user_key`` is ``u<user_id>`` when auth is ON, or ``__single__`` when auth is
OFF (a single shared workspace for the one local operator). Cross-user access
is structurally prevented: every read/write resolves under the *requesting*
user's own directory, and ``file_id`` is validated as 32-hex so it can never
escape the directory. Only PDF + PNG are accepted (validated by magic bytes).
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from fastapi import Request

logger = logging.getLogger(__name__)

# mime -> extension。涵蓋本站各工具會產出的格式：PDF / PNG + 文書 / 試算表 /
# 簡報（OOXML 與 ODF 兩系）。實際的型別判定一律走 `detect_kind()` 開 zip 驗
# 內部結構，這份表只是對照用 —— 只看副檔名的話，改名的 zip 就能冒充。
ALLOWED: dict[str, str] = {
    "application/pdf": ".pdf",
    "image/png": ".png",
}

_SINGLE_KEY = "__single__"  # auth-OFF shared workspace


# --------------------------------------------------------------------------- #
# Settings (data/workspace.json)
# --------------------------------------------------------------------------- #

_DEFAULTS: dict[str, Any] = {
    "enabled": True,           # admin master switch — off hides everything
    "per_user_quota_mb": 500,  # 0/-1 = unlimited
    "max_file_mb": 50,         # 0/-1 = unlimited
    "retention_hours": 24,     # -1 = keep forever
    "updated_at": 0.0,
}

_LOCK = threading.Lock()
_CACHE: dict[str, Any] | None = None


def _settings_path() -> Path:
    from ..config import settings
    return settings.data_dir / "workspace.json"


def get_settings() -> dict[str, Any]:
    global _CACHE
    with _LOCK:
        if _CACHE is None:
            p = _settings_path()
            merged = json.loads(json.dumps(_DEFAULTS))
            if p.exists():
                try:
                    raw = json.loads(p.read_text(encoding="utf-8"))
                    merged.update({k: v for k, v in raw.items() if k in _DEFAULTS})
                except Exception:
                    pass
            _CACHE = merged
        return json.loads(json.dumps(_CACHE))


def save_settings(new: dict[str, Any]) -> dict[str, Any]:
    """Merge + persist workspace settings (atomic write, 0600)."""
    global _CACHE
    with _LOCK:
        merged = json.loads(json.dumps(_DEFAULTS))
        cur = _CACHE if _CACHE is not None else None
        if cur:
            merged.update({k: cur[k] for k in _DEFAULTS if k in cur})
        for k in _DEFAULTS:
            if k == "updated_at" or k not in new:
                continue
            v = new[k]
            if k == "enabled":
                merged[k] = bool(v)
            else:
                if not isinstance(v, (int, float)) or isinstance(v, bool):
                    raise ValueError(f"{k} 必須是數字")
                merged[k] = int(v)
        merged["updated_at"] = time.time()
        p = _settings_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(merged, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except Exception:
            pass
        tmp.replace(p)
        _CACHE = merged
        return json.loads(json.dumps(merged))


def is_enabled() -> bool:
    return bool(get_settings().get("enabled"))


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #

class WorkspaceError(Exception):
    """Base for user-facing workspace failures (caller maps to HTTP 4xx)."""


class WorkspaceDisabled(WorkspaceError):
    pass


class QuotaExceeded(WorkspaceError):
    pass


class UnsupportedType(WorkspaceError):
    pass


class NotFound(WorkspaceError):
    pass


# --------------------------------------------------------------------------- #
# User identity → storage key
# --------------------------------------------------------------------------- #

def _auth_enabled() -> bool:
    try:
        from . import auth_settings as _as
        return _as.is_enabled()
    except Exception:
        return False


def _user_id(request: Request) -> Optional[int]:
    user = getattr(getattr(request, "state", None), "user", None)
    if not user:
        return None
    v = user.get("user_id") if isinstance(user, dict) else getattr(user, "user_id", None)
    try:
        return int(v) if v is not None else None
    except Exception:
        return None


def _user_label(request: Request) -> str:
    from . import sessions
    user = getattr(getattr(request, "state", None), "user", None)
    return sessions.user_label(user) if user else ""


def user_key(request: Request) -> str:
    """Storage key for the requesting user. Raises WorkspaceError when auth is
    ON but no user is bound to the request (anonymous → no workspace)."""
    if not _auth_enabled():
        return _SINGLE_KEY
    uid = _user_id(request)
    if uid is None:
        raise WorkspaceError("尚未登入")
    return f"u{uid}"


def _root() -> Path:
    from ..config import settings
    return settings.data_dir / "workspace"


def purge_user(user_id: int) -> int:
    """刪掉某位使用者工作區裡的全部檔案，回傳刪了幾個。

    帳號被刪除時要一起清 —— 第一版的 `user_manager.delete()` 只清資料庫，
    `data/workspace/u<id>/` 原封不動留在磁碟上（v1.14.31 對抗式驗證實測：
    刪掉帳號後檔案還在，管理區的用量統計也照樣列出那個人）。保留期設成
    「永久保留」時就是**永久**留著離職者的檔案。

    這件事跟交接直接相關：交接每按一次就自動存一份到工作區，使用者不見得
    知道有留底。
    """
    d = _root() / f"u{int(user_id)}"
    if not d.exists():
        return 0
    n = sum(1 for x in d.iterdir() if x.is_dir())
    import shutil
    shutil.rmtree(d, ignore_errors=True)
    return n


def _user_dir(request: Request, create: bool = False) -> Path:
    d = _root() / user_key(request)
    if create:
        d.mkdir(parents=True, exist_ok=True)
    return d


# --------------------------------------------------------------------------- #
# Type detection
# --------------------------------------------------------------------------- #

_OOX = "application/vnd.openxmlformats-officedocument"
_DOCX_MIME = f"{_OOX}.wordprocessingml.document"
_XLSX_MIME = f"{_OOX}.spreadsheetml.sheet"
_PPTX_MIME = f"{_OOX}.presentationml.presentation"
_ODF = "application/vnd.oasis.opendocument"
_ODT_MIME = f"{_ODF}.text"
_ODS_MIME = f"{_ODF}.spreadsheet"
_ODP_MIME = f"{_ODF}.presentation"
_ODG_MIME = f"{_ODF}.graphics"

#: ODF：檔頭那個未壓縮的 `mimetype` 成員直接寫明型別 → 一對一對照即可
_ODF_KINDS = {
    _ODT_MIME: ".odt", _ODS_MIME: ".ods",
    _ODP_MIME: ".odp", _ODG_MIME: ".odg",
}
#: OOXML：沒有 mimetype 成員，靠「主要內容部件」的路徑判別
_OOXML_KINDS = (
    ("word/document.xml", _DOCX_MIME, ".docx"),
    ("xl/workbook.xml", _XLSX_MIME, ".xlsx"),
    ("ppt/presentation.xml", _PPTX_MIME, ".pptx"),
)


# 把 Office 型別併進 ALLOWED，維持單一事實來源 —— 兩邊各寫一份遲早會不一致
ALLOWED.update({m: e for m, e in _ODF_KINDS.items()})
ALLOWED.update({m: e for _p, m, e in _OOXML_KINDS})


def detect_kind(data: bytes) -> Optional[tuple[str, str]]:
    """Return (mime, ext) for a supported file by magic bytes, else None.

    PDF / PNG are matched by their leading signature. Office documents are all
    ZIP containers (PK\\x03\\x04) — we open the archive and inspect its
    internal structure to tell them apart (and reject arbitrary zips), so a
    renamed .zip can't slip in claiming to be a document.

    簡報（.pptx / .odp）與試算表（.xlsx / .ods）是 v1.14.6 補上的：本站的
    「PDF 轉簡報」產出的就是這些格式，原本工作區收不下 —— 使用者按「存至
    工作區」只會拿到「不支援的檔案類型」，而那正是他最需要留存的產出。
    """
    if data[:4] == b"%PDF":
        return "application/pdf", ".pdf"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png", ".png"
    if data[:4] == b"PK\x03\x04":  # ZIP-based Office document
        try:
            import io
            import zipfile
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                names = set(z.namelist())
                # ODF: a leading uncompressed "mimetype" member states the type.
                if "mimetype" in names:
                    mt = z.read("mimetype")[:96].decode("ascii", "replace")
                    for mime, ext in _ODF_KINDS.items():
                        if mt.startswith(mime):
                            return mime, ext
                # OOXML: content-types map + the main content part.
                if "[Content_Types].xml" in names:
                    for part, mime, ext in _OOXML_KINDS:
                        if part in names:
                            return mime, ext
        except Exception:  # noqa: BLE001 — malformed zip → unsupported
            return None
    return None


def _clean_display_name(name: str, ext: str) -> str:
    """A safe, friendly display filename. Stored only in meta.json (never used
    as a filesystem path), so we just strip control chars + cap length and
    ensure the right extension."""
    name = (name or "").strip().replace("\r", " ").replace("\n", " ")
    name = "".join(ch for ch in name if ch.isprintable())
    # drop any directory components a caller might have sent
    name = name.replace("\\", "/").split("/")[-1]
    if not name:
        name = "file" + ext
    if not name.lower().endswith(ext):
        # replace a wrong/absent extension
        stem = name.rsplit(".", 1)[0] if "." in name else name
        name = stem + ext
    return name[:200]


# --------------------------------------------------------------------------- #
# Usage / quota
# --------------------------------------------------------------------------- #

def _dir_size(p: Path) -> int:
    if not p.exists():
        return 0
    total = 0
    for root, _, files in os.walk(p):
        for f in files:
            try:
                total += (Path(root) / f).stat().st_size
            except OSError:
                pass
    return total


def usage_for_key(key: str) -> dict[str, Any]:
    """以儲存鍵（而非 request）查用量 —— 背景執行緒沒有 request 可用。"""
    s = get_settings()
    used = _dir_size(_root() / key)
    quota_mb = int(s.get("per_user_quota_mb") or 0)
    quota_bytes = quota_mb * 1024 * 1024 if quota_mb > 0 else 0  # 0 = unlimited
    return {
        "used_bytes": used,
        "quota_bytes": quota_bytes,
        "max_file_bytes": (int(s.get("max_file_mb") or 0) * 1024 * 1024
                           if int(s.get("max_file_mb") or 0) > 0 else 0),
    }


def key_for_user_id(user_id: Optional[int]) -> str:
    """由使用者 id 組出儲存鍵。認證關閉時是共用的單一工作區。"""
    if not _auth_enabled():
        return _SINGLE_KEY
    if user_id is None:
        raise WorkspaceError("尚未登入")
    return f"u{int(user_id)}"


def usage(request: Request) -> dict[str, Any]:
    s = get_settings()
    used = _dir_size(_user_dir(request))
    quota_mb = int(s.get("per_user_quota_mb") or 0)
    quota_bytes = quota_mb * 1024 * 1024 if quota_mb > 0 else 0  # 0 = unlimited
    return {
        "used_bytes": used,
        "quota_bytes": quota_bytes,          # 0 = unlimited
        "max_file_bytes": (int(s.get("max_file_mb") or 0) * 1024 * 1024
                           if int(s.get("max_file_mb") or 0) > 0 else 0),
    }


# --------------------------------------------------------------------------- #
# CRUD
# --------------------------------------------------------------------------- #

def _meta_path(d: Path) -> Path:
    return d / "meta.json"


def _read_meta(d: Path) -> Optional[dict[str, Any]]:
    mf = _meta_path(d)
    if not mf.exists():
        return None
    try:
        return json.loads(mf.read_text(encoding="utf-8"))
    except Exception:
        return None


def save_bytes(request: Request, data: bytes, display_name: str,
               source_tool: str = "") -> dict[str, Any]:
    """Persist bytes into the requesting user's workspace. Validates the master
    switch, file type, single-file cap and per-user quota. Returns the meta."""
    return save_bytes_for_key(user_key(request), data, display_name,
                              source_tool, user_label=_user_label(request))


def save_bytes_for_key(key: str, data: bytes, display_name: str,
                       source_tool: str = "",
                       user_label: str = "") -> dict[str, Any]:
    """同 `save_bytes`，但以儲存鍵指定對象。

    背景作業完成後要自動存進送出者的工作區，那時已經沒有 request 可用（原本的
    寫法只接受 request）—— 把驗證與寫入的邏輯留在同一處，避免自動存入這條路
    繞過額度 / 型別檢查。
    """
    if not is_enabled():
        raise WorkspaceDisabled("工作區功能未啟用")
    if not data:
        raise WorkspaceError("檔案為空")
    kind = detect_kind(data)
    if kind is None:
        raise UnsupportedType(
            "工作區接受 PDF / PNG、Word (.docx) / Excel (.xlsx) / "
            "PowerPoint (.pptx)、OpenDocument (.odt / .ods / .odp / .odg)")
    mime, ext = kind
    s = get_settings()
    max_file_mb = int(s.get("max_file_mb") or 0)
    if max_file_mb > 0 and len(data) > max_file_mb * 1024 * 1024:
        raise QuotaExceeded(f"單檔超過上限 {max_file_mb} MB")
    u = usage_for_key(key)
    if u["quota_bytes"] and u["used_bytes"] + len(data) > u["quota_bytes"]:
        quota_mb = u["quota_bytes"] // 1024 // 1024
        raise QuotaExceeded(f"工作區容量已滿（額度 {quota_mb} MB），請先刪除舊檔")

    file_id = uuid.uuid4().hex
    base = _root() / key
    base.mkdir(parents=True, exist_ok=True)
    d = base / file_id
    d.mkdir(parents=True, exist_ok=True)
    (d / f"file{ext}").write_bytes(data)
    meta = {
        "file_id": file_id,
        "name": _clean_display_name(display_name, ext),
        "ext": ext,
        "mime": mime,
        "size": len(data),
        "source_tool": (source_tool or "")[:64],
        "saved_at": time.time(),
        "user_label": user_label,
    }
    _meta_path(d).write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                             encoding="utf-8")
    return meta


def list_files(request: Request) -> list[dict[str, Any]]:
    if not is_enabled():
        return []
    base = _user_dir(request)
    if not base.exists():
        return []
    out: list[dict[str, Any]] = []
    for d in base.iterdir():
        if not d.is_dir():
            continue
        meta = _read_meta(d)
        if meta:
            out.append(meta)
    out.sort(key=lambda m: m.get("saved_at", 0), reverse=True)
    return out


def count_files(request: Request) -> int:
    """Lightweight count of the user's workspace entries (no meta reads)."""
    if not is_enabled():
        return 0
    base = _user_dir(request)
    if not base.exists():
        return 0
    n = 0
    for d in base.iterdir():
        if d.is_dir() and _meta_path(d).exists():
            n += 1
    return n


def _entry_dir(request: Request, file_id: str) -> Path:
    from .safe_paths import is_uuid_hex
    if not is_uuid_hex(file_id):
        raise NotFound("檔案不存在")
    d = _user_dir(request) / file_id
    if not d.is_dir() or _read_meta(d) is None:
        raise NotFound("檔案不存在")
    return d


def get_file(request: Request, file_id: str) -> tuple[Path, dict[str, Any]]:
    """Return (file_path, meta) for one of the requesting user's files. Raises
    NotFound if it doesn't exist / isn't theirs (resolved under their dir)."""
    if not is_enabled():
        raise WorkspaceDisabled("工作區功能未啟用")
    d = _entry_dir(request, file_id)
    meta = _read_meta(d) or {}
    fp = d / f"file{meta.get('ext', '')}"
    if not fp.exists():
        raise NotFound("檔案不存在")
    return fp, meta


#: 需要先轉成 PDF 才畫得出第一頁的格式。
_OFFICE_THUMB_EXTS = (".docx", ".odt", ".xlsx", ".ods", ".pptx", ".odp", ".odg")

#: 超過這個大小就不做縮圖。
#:
#: 實測（正式機）：4.7 MB 的簡報 6 秒、37.9 MB 的年報簡報 **48 秒**。
#: 因為是背景做、而且只做一次（結果與失敗記號都會快取），48 秒可以接受 ——
#: 使用者原本看到的是永遠空白。上限拉到 80 MB 讓真實的大檔也有縮圖；再大的
#: 就不划算了：那段時間 Office 引擎的名額被佔住，真正在等轉檔的人得排隊。
_THUMB_MAX_BYTES = 80 * 1024 * 1024

#: 縮圖產不出來時留一個記號，下次直接跳過。
#: 沒有這個的話，每次開工作區頁面都會對同一個檔重跑一次 soffice ——
#: 失敗的檔案通常每次都會失敗，等於固定的浪費。
_THUMB_FAIL_MARK = "thumb.failed"


#: 正在背景產生縮圖的項目（避免同一個檔被排好幾次）。
_thumb_building: set[str] = set()
_thumb_lock = threading.Lock()


def _office_thumbnail(d: Path, ext: str, *, blocking: bool = False):
    """Office / ODF 檔的第一頁縮圖：先轉 PDF，再畫第一頁，結果快取起來。

    使用者問「為何非 PDF 都沒有縮圖」—— 原本這裡直接放棄，畫面上就是一片空白。
    真正的原因是這些格式沒有便宜的「畫第一頁」方法，一定要經過 Office 引擎。

    所以：**做，但只做一次**。縮圖與失敗記號都存在該檔案自己的目錄裡，跟著檔案
    一起被清掉；轉檔本身走既有的 Office 名額控管（不會因為有人開工作區頁面就把
    引擎佔滿）。
    """
    thumb = d / "thumb.png"
    if thumb.exists():
        return thumb, "image/png"
    if (d / _THUMB_FAIL_MARK).exists():
        raise WorkspaceError("此檔案無法產生預覽")
    if not blocking:
        # **不要在 HTTP 請求裡等轉檔**。轉一份文件要幾秒，而 Office 引擎同時只
        # 跑得了少數幾個 —— 一頁 17 個檔就是 17 個請求排隊等同一顆引擎，最後
        # 一個要等上一分鐘，期間還佔著 worker。
        # 改成：排到背景去做，這次先回空白圖，做好之後下次（或前端稍後重試）
        # 就看得到。
        _schedule_thumbnail(d, ext)
        raise WorkspaceError("預覽產生中")
    src = d / f"file{ext}"
    if not src.exists():
        raise NotFound("檔案不存在")
    try:
        if src.stat().st_size > _THUMB_MAX_BYTES:
            raise WorkspaceError("檔案過大，略過預覽")
    except OSError:
        raise NotFound("檔案不存在")

    import tempfile
    try:
        from . import office_convert
        with tempfile.TemporaryDirectory(prefix="wsthumb_") as tmp:
            # convert_to_pdf 是「寫到指定路徑」不是「回傳路徑」——
            # 傳目錄進去會拿到 None，然後在下一行才炸（訊息還看不出原因）。
            pdf = Path(tmp) / "preview.pdf"
            office_convert.convert_to_pdf(src, pdf)
            import fitz
            with fitz.open(str(pdf)) as doc:
                if not doc.page_count:
                    raise WorkspaceError("文件沒有任何頁面")
                pix = doc[0].get_pixmap(matrix=fitz.Matrix(1.3, 1.3), alpha=False)
                pix.save(str(thumb))
    except Exception as e:  # noqa: BLE001 — 縮圖失敗只影響好看，不影響檔案本身
        try:
            (d / _THUMB_FAIL_MARK).write_text(
                f"{e.__class__.__name__}", encoding="utf-8")
        except OSError:
            pass
        logger.info("工作區縮圖產生失敗（%s）：%s", src.name, e.__class__.__name__)
        raise WorkspaceError("無法產生預覽")
    return thumb, "image/png"


def _schedule_thumbnail(d: Path, ext: str) -> None:
    """把縮圖產生排到背景。同一個項目只會排一次。"""
    key = str(d)
    with _thumb_lock:
        if key in _thumb_building:
            return
        _thumb_building.add(key)

    def work():
        try:
            _office_thumbnail(d, ext, blocking=True)
        except Exception:  # noqa: BLE001 — 失敗已經寫進記號檔
            pass
        finally:
            with _thumb_lock:
                _thumb_building.discard(key)

    t = threading.Thread(target=work, name="ws-thumb", daemon=True)
    t.start()


def get_thumbnail(request: Request, file_id: str) -> tuple[Path, str]:
    """Return (path, mime) for a preview thumbnail of one of the user's files.
    PNG → the image itself; PDF → first page rendered to a cached thumb.png
    (cached in the entry dir, so it's cleaned with the file). Raises NotFound /
    WorkspaceError on failure (caller serves a placeholder)."""
    if not is_enabled():
        raise WorkspaceDisabled("工作區功能未啟用")
    d = _entry_dir(request, file_id)
    meta = _read_meta(d) or {}
    ext = meta.get("ext", "")
    if ext == ".png":
        fp = d / "file.png"
        if not fp.exists():
            raise NotFound("檔案不存在")
        return fp, "image/png"
    if ext in _OFFICE_THUMB_EXTS:
        return _office_thumbnail(d, ext)
    # PDF → render first page (cache thumb.png).
    thumb = d / "thumb.png"
    if thumb.exists():
        return thumb, "image/png"
    src = d / "file.pdf"
    if not src.exists():
        raise NotFound("檔案不存在")
    try:
        import fitz
        with fitz.open(str(src)) as doc:
            page = doc[0]
            pix = page.get_pixmap(matrix=fitz.Matrix(1.3, 1.3), alpha=False)
            pix.save(str(thumb))
    except Exception as e:  # noqa: BLE001
        raise WorkspaceError(f"無法產生預覽：{e.__class__.__name__}")
    return thumb, "image/png"


def delete_file(request: Request, file_id: str) -> bool:
    if not is_enabled():
        raise WorkspaceDisabled("工作區功能未啟用")
    d = _entry_dir(request, file_id)
    shutil.rmtree(d, ignore_errors=True)
    return True


def rename_file(request: Request, file_id: str, new_name: str) -> dict[str, Any]:
    if not is_enabled():
        raise WorkspaceDisabled("工作區功能未啟用")
    d = _entry_dir(request, file_id)
    meta = _read_meta(d) or {}
    meta["name"] = _clean_display_name(new_name, meta.get("ext", ""))
    _meta_path(d).write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                             encoding="utf-8")
    return meta


# --------------------------------------------------------------------------- #
# Retention sweep + admin-wide stats
# --------------------------------------------------------------------------- #

def sweep_older_than(seconds: int) -> int:
    """Delete workspace entries whose saved_at is older than `seconds`.
    seconds <= 0 → no-op (keep forever). Returns count removed."""
    if seconds <= 0:
        return 0
    root = _root()
    if not root.exists():
        return 0
    cutoff = time.time() - seconds
    removed = 0
    for udir in root.iterdir():
        if not udir.is_dir():
            continue
        for d in udir.iterdir():
            if not d.is_dir():
                continue
            meta = _read_meta(d)
            ts = (meta or {}).get("saved_at")
            if ts is None:
                # No meta → fall back to mtime so orphans still expire.
                try:
                    ts = d.stat().st_mtime
                except OSError:
                    continue
            if ts < cutoff:
                shutil.rmtree(d, ignore_errors=True)
                removed += 1
    return removed


_USER_KEY_RE = re.compile(r"^(u\d+|__single__)$")


def admin_clear_user(user_key: str) -> int:
    """Admin housekeeping: delete ALL of one user's workspace files. Returns
    the number of entries removed. (Admin manages capacity but does not browse
    individual file contents — consistent with the app's no-snoop model.)"""
    if not _USER_KEY_RE.match(user_key or ""):
        return 0
    d = _root() / user_key
    if not d.is_dir():
        return 0
    n = sum(1 for x in d.iterdir() if x.is_dir() and (x / "meta.json").exists())
    shutil.rmtree(d, ignore_errors=True)
    return n


def admin_clear_all() -> int:
    """Admin: clear every user's workspace. Returns total entries removed."""
    root = _root()
    if not root.exists():
        return 0
    total = 0
    for udir in list(root.iterdir()):
        if udir.is_dir():
            total += admin_clear_user(udir.name)
    return total


def collect_stats() -> dict[str, Any]:
    """Admin view: per-user usage + totals."""
    root = _root()
    users: list[dict[str, Any]] = []
    total_bytes = 0
    total_count = 0
    if root.exists():
        for udir in sorted(root.iterdir()):
            if not udir.is_dir():
                continue
            cnt = 0
            label = ""
            for d in udir.iterdir():
                if d.is_dir() and _meta_path(d).exists():
                    cnt += 1
                    if not label:
                        label = (_read_meta(d) or {}).get("user_label", "")
            size = _dir_size(udir)
            total_bytes += size
            total_count += cnt
            users.append({
                "key": udir.name,
                "label": label or (udir.name if udir.name != _SINGLE_KEY else "（單機模式）"),
                "count": cnt,
                "bytes": size,
            })
    users.sort(key=lambda u: u["bytes"], reverse=True)
    return {"users": users, "total_bytes": total_bytes, "total_count": total_count}
