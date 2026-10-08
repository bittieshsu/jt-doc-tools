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
escape the directory. Accepted types (PDF / PNG / Office / plain text /
recordings) are validated by content, never by the file name.

錄音 / 錄影檔（給「會議錄音轉逐字稿」從工作區載入用）有三件跟其他型別不一樣的事：
①格式判定只讀檔頭（`audio_formats.sniff_stream`），**不整份讀進記憶體**；
②單檔上限另外一個（`max_audio_mb`）—— 三小時會議的 AAC 約 90 MB，一般型別的
50 MB 會直接擋掉；③沒有預覽圖，畫面上顯示圖示（`has_preview()`）。
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

from . import atomic_json
from . import audio_formats
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
    # 純文字沒有魔術位元組，判準見 `_looks_like_text()`。
    "text/plain": ".txt",
    "text/markdown": ".md",
    # JSON 也是純文字，**多一道「整份讀得進 JSON」才收成 `.json`**（2026-10-03 使用者回報：
    # 從工作區下載轉逐字稿送來的檔，內容是 JSON、副檔名卻是 `.txt`）。原本工作區只認
    # `.txt` / `.md`，JSON 一存進來就被改名 —— 會議摘要載回來還被當成逐字稿（v1.16.40 / v1.16.42）。
    "application/json": ".json",
}

_SINGLE_KEY = "__single__"  # auth-OFF shared workspace


# --------------------------------------------------------------------------- #
# Settings (data/workspace.json)
# --------------------------------------------------------------------------- #

_DEFAULTS: dict[str, Any] = {
    "enabled": True,           # admin master switch — off hides everything
    "per_user_quota_mb": 500,  # 0/-1 = unlimited
    "max_file_mb": 50,         # 0/-1 = unlimited
    # 錄音 / 錄影檔另一個上限：三小時會議的 AAC 約 90 MB、錄影更大，套一般型別
    # 的 50 MB 會直接擋掉。預設跟全站上傳上限（`upload_settings`）一樣。
    # 錄音檔只讀檔頭判斷格式、串流寫入，不會整份進記憶體，所以可以比一般型別大。
    "max_audio_mb": 500,       # 0/-1 = unlimited
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
        atomic_json.write_json(_settings_path(), merged, mode=0o600)
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


#: OOXML 主檔的**內容型別**（權威來源）—— 主檔的路徑不保證叫什麼名字。
_OOXML_MAIN_TYPES = (
    ("wordprocessingml.document.main+xml", _DOCX_MIME, ".docx"),
    ("spreadsheetml.sheet.main+xml", _XLSX_MIME, ".xlsx"),
    ("presentationml.presentation.main+xml", _PPTX_MIME, ".pptx"),
    # 範本（.dotx / .xltx / .potx）內容也一樣，當成對應的文件型別收
    ("wordprocessingml.template.main+xml", _DOCX_MIME, ".docx"),
    ("spreadsheetml.template.main+xml", _XLSX_MIME, ".xlsx"),
    ("presentationml.template.main+xml", _PPTX_MIME, ".pptx"),
    ("presentationml.slideshow.main+xml", _PPTX_MIME, ".pptx"),
)


# 把 Office 型別併進 ALLOWED，維持單一事實來源 —— 兩邊各寫一份遲早會不一致
ALLOWED.update({m: e for m, e in _ODF_KINDS.items()})
ALLOWED.update({m: e for _p, m, e in _OOXML_KINDS})
# 錄音 / 錄影：清單**只有一份**，在 `audio_formats`（「會議錄音轉逐字稿」也從那裡拿）。
# 這裡再寫一份的話，症狀是工作區存得進去、轉逐字稿卻挑不到（或反過來），沒有錯誤訊息。
ALLOWED.update({audio_formats.MIME_BY_EXT[e]: e for e in audio_formats.AUDIO_EXTS})

#: 錄音 / 錄影檔的副檔名（來自 `audio_formats`）。
AUDIO_EXTS = frozenset(audio_formats.AUDIO_EXTS)


def is_audio_ext(ext: str) -> bool:
    return (ext or "").lower() in AUDIO_EXTS


def kind_of(ext: str) -> str:
    """畫面上用來挑圖示的大類：pdf / image / office / text / audio / video。"""
    ext = (ext or "").lower()
    if ext in AUDIO_EXTS:
        return "video" if ext in audio_formats.VIDEO_EXTS else "audio"
    if ext in _TEXT_EXTS:
        return "text"
    if ext == ".png":
        return "image"
    if ext == ".pdf":
        return "pdf"
    return "office"


def has_preview(ext: str) -> bool:
    """這種檔案畫不畫得出縮圖。

    **由伺服器端說**，前端不自己判斷：純文字與錄音檔沒有「第一頁」可以畫，
    前端照這個旗標直接顯示圖示，不去要一張註定是空白的縮圖（原本純文字拿到的是
    一張 1×1 的透明圖，畫面上是一塊什麼都沒有的空白）。
    """
    ext = (ext or "").lower()
    return not (ext in _TEXT_EXTS or ext in AUDIO_EXTS)


def accepted_type_groups() -> list[tuple[str, list[str]]]:
    """工作區收哪些格式，**依類別分組、從 `ALLOWED` 實算** —— 給說明文字用。

    設定頁原本寫死「只接受 PDF 與 PNG 檔」，工作區早就收辦公文件與純文字，
    那句話一直沒人改（清單寫兩份一定會漂）。這裡算出來的分組，每一個副檔名都
    來自 `ALLOWED`，而且全部都會出現在某一組裡（測試釘住）。
    """
    exts = sorted(set(ALLOWED.values()))
    av = [e for e in audio_formats.AUDIO_EXTS if e in exts]
    groups = [
        ("PDF 文件", [e for e in exts if e == ".pdf"]),
        ("圖片", [e for e in exts if e == ".png"]),
        ("辦公文件", [e for e in exts if kind_of(e) == "office"]),
        ("純文字", [e for e in exts if kind_of(e) == "text"]),
        ("錄音 / 錄影", av),
    ]
    return [(label, items) for label, items in groups if items]


#: 純文字裡**允許**出現的控制字元。其餘 C0 與 DEL 一律視為「不是文字」。
#: 這一條是純文字唯一的防線 —— 它沒有魔術位元組，只看副檔名的話，
#: 任何東西改名成 `.txt` 都進得來。
_TEXT_OK_CONTROLS = frozenset("\t\n\r")


def _looks_like_text(data: bytes) -> bool:
    """整份解得開 UTF-8，而且沒有奇怪的控制字元，才算純文字。

    **判準不可以是副檔名。** 工作區其餘型別都靠魔術位元組或 zip 內部結構認，
    純文字沒有那種東西，所以改用「內容本身說得通」當判準：

    * 整份 UTF-8 解得開（含 BOM）—— 多數二進位檔在這一關就過不了。
    * 沒有 `\t\n\r` 以外的控制字元 —— 擋住「全部是 ASCII 位元組但夾著
      NUL」那種會通過解碼的二進位檔。

    **整份都要看，不可以只抽前面幾 KB**：前 4 KB 乾淨、後面是二進位的檔案
    是真的做得出來的，而抽樣會讓它整個進來。單檔上限預設 50 MB，解一次的
    成本可以接受。
    """
    if not data:
        return False
    try:
        s = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return False
    return not any(
        (ch < " " or ch == "\x7f") and ch not in _TEXT_OK_CONTROLS for ch in s
    )


def detect_kind(data: bytes, name: str = "") -> Optional[tuple[str, str]]:
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
                # zip 炸彈：判斷集中在 `zip_guard`，全站一份
                from .zip_guard import check as _zip_check
                _zip_check(z)
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
                    # **主檔不一定叫 `word/document.xml`。** Word 自己在某些
                    # 編輯之後會寫成 `word/document2.xml`，那種檔案照樣是合法
                    # 的 .docx，但上面那組路徑比對會認不得 → 使用者拖進工作區
                    # 得到「不支援的檔案類型」，而錯誤訊息還寫著「接受 .docx」。
                    # 真正權威的來源是 `[Content_Types].xml` 裡的內容型別。
                    try:
                        ct = z.read("[Content_Types].xml").decode("utf-8", "replace")
                    except Exception:  # noqa: BLE001
                        ct = ""
                    for main_ct, mime, ext in _OOXML_MAIN_TYPES:
                        if main_ct in ct:
                            return mime, ext
        except Exception:  # noqa: BLE001 — malformed zip → unsupported
            return None
    # 錄音 / 錄影：只看檔頭（判準與理由在 `audio_formats`）。放在純文字前面 ——
    # 文字的判斷要解整份 UTF-8，錄音檔不必白跑那一趟。
    av = audio_formats.sniff(data, name)
    if av is not None:
        return av
    # 純文字放最後：上面每一種都有明確的訊號，文字是「都不像，但內容說得通」。
    # **`name` 只在兩種文字型別之間二選一**，不可以讓不是文字的東西過關 ——
    # 先過 `_looks_like_text()` 才輪得到它。
    if _looks_like_text(data):
        low = (name or "").lower()
        if low.endswith(".md") or low.endswith(".markdown"):
            return "text/markdown", ".md"
        # **檔名說是 `.json`、內容也真的讀得進 JSON 才算** —— 讀不進的照舊是 `.txt`
        #（寧可多一個 `.txt`，也不要讓下載的人拿到一個打不開的 `.json`）。
        # 檔名不是 `.json` 的不改：使用者存成 `.txt` 就是要 `.txt`。
        if low.endswith(".json") and _parses_as_json(data):
            return "application/json", ".json"
        return "text/plain", ".txt"
    return None


def _parses_as_json(data: bytes) -> bool:
    try:
        json.loads(data.decode("utf-8-sig"))
        return True
    except (ValueError, UnicodeDecodeError):
        return False


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
    kind = detect_kind(data, display_name)
    if kind is None:
        raise UnsupportedType(UNSUPPORTED_MESSAGE)
    mime, ext = kind
    _check_room(key, ext, len(data))
    d = _new_entry_dir(key)
    (d / f"file{ext}").write_bytes(data)
    return _write_meta(d, display_name, ext, mime, len(data), source_tool, user_label)


#: 不支援的型別 —— 列出來的格式由 `ALLOWED` 實算（見 `accepted_type_groups`）。
#: 原本這句是寫死的，工作區收了新型別之後它就說謊了。
UNSUPPORTED_MESSAGE = "工作區接受的格式：PDF / PNG、辦公文件、純文字、錄音 / 錄影檔"

#: 被大小上限擋下時**一定要講出是上限、不是格式**（錄音檔最容易踩到：
#: 三小時的會議錄音被擋下，使用者會以為是格式不支援而去轉檔）。
TOO_BIG_MESSAGE = "單檔超過上限 {0} MB（這是大小上限，不是格式問題）"
TOO_BIG_AUDIO_MESSAGE = "錄音檔超過單檔上限 {0} MB（這是大小上限，不是格式問題；可以請管理員到「工作區設定」調整）"
QUOTA_FULL_MESSAGE = "工作區容量已滿（額度 {0} MB），請先刪除舊檔"


def max_bytes_for(ext: str) -> int:
    """這種型別的單檔上限（bytes），0 = 不限。錄音 / 錄影檔用 `max_audio_mb`。"""
    s = get_settings()
    key = "max_audio_mb" if is_audio_ext(ext) else "max_file_mb"
    mb = int(s.get(key) or 0)
    return mb * 1024 * 1024 if mb > 0 else 0


def _check_room(key: str, ext: str, size: int) -> None:
    """單檔上限與每人額度。**寫入之前**就要擋，不要寫到一半才發現。"""
    lim = max_bytes_for(ext)
    if lim and size > lim:
        tpl = TOO_BIG_AUDIO_MESSAGE if is_audio_ext(ext) else TOO_BIG_MESSAGE
        raise QuotaExceeded(tpl.replace("{0}", str(lim // 1024 // 1024)))
    u = usage_for_key(key)
    if u["quota_bytes"] and u["used_bytes"] + size > u["quota_bytes"]:
        raise QuotaExceeded(QUOTA_FULL_MESSAGE.replace(
            "{0}", str(u["quota_bytes"] // 1024 // 1024)))


def _new_entry_dir(key: str) -> Path:
    file_id = uuid.uuid4().hex
    d = _root() / key / file_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_meta(d: Path, display_name: str, ext: str, mime: str, size: int,
                source_tool: str, user_label: str) -> dict[str, Any]:
    meta = {
        "file_id": d.name,
        "name": _clean_display_name(display_name, ext),
        "ext": ext,
        "mime": mime,
        "size": size,
        "source_tool": (source_tool or "")[:64],
        "saved_at": time.time(),
        "user_label": user_label,
    }
    atomic_json.write_json(_meta_path(d), meta)
    return meta


def _head_is_in_memory_kind(head: bytes) -> bool:
    """檔頭看起來是不是 PDF / PNG / zip / 純文字 —— 只用來決定「太大」時講哪一句。"""
    if head[:4] in (b"%PDF", b"PK\x03\x04") or head[:8] == b"\x89PNG\r\n\x1a\n":
        return True
    # 檔頭可能剛好切在一個中文字的中間 —— 退幾個位元組再判斷
    for cut in range(4):
        part = head[:len(head) - cut] if cut else head
        if part and _looks_like_text(part):
            return True
    return False


def save_stream_for_key(key: str, fobj, display_name: str, source_tool: str = "",
                        user_label: str = "") -> dict[str, Any]:
    """從一個**可 seek 的檔案物件**存進工作區（網頁上傳走這條）。

    跟 `save_bytes_for_key` 的差別只在**錄音檔不整份讀進記憶體**：
    格式只看檔頭、大小用 seek 量、內容串流寫進去。其他型別（PDF / Office /
    純文字）判斷時本來就要看整份（開 zip、解 UTF-8），所以**先擋大小**、
    再讀進來交給 `save_bytes_for_key` —— 驗證邏輯仍只有那一份。
    """
    if not is_enabled():
        raise WorkspaceDisabled("工作區功能未啟用")
    fobj.seek(0, os.SEEK_END)
    size = fobj.tell()
    fobj.seek(0)
    if size == 0:
        raise WorkspaceError("檔案為空")
    av = audio_formats.sniff_stream(fobj, display_name)
    if av is None:
        lim = max_bytes_for(".pdf")
        if lim and size > lim:
            head = fobj.read(audio_formats.HEAD_BYTES)
            fobj.seek(0)
            if not _head_is_in_memory_kind(head):
                raise UnsupportedType(UNSUPPORTED_MESSAGE)
            raise QuotaExceeded(TOO_BIG_MESSAGE.replace("{0}", str(lim // 1024 // 1024)))
        return save_bytes_for_key(key, fobj.read(), display_name, source_tool, user_label)

    mime, ext = av
    _check_room(key, ext, size)
    d = _new_entry_dir(key)
    part = d / f"file{ext}.part"
    try:
        fobj.seek(0)
        with part.open("wb") as out:
            shutil.copyfileobj(fobj, out, 1 << 20)
        os.replace(part, d / f"file{ext}")
    except BaseException:
        shutil.rmtree(d, ignore_errors=True)
        raise
    return _write_meta(d, display_name, ext, mime, size, source_tool, user_label)


def save_stream(request: Request, fobj, display_name: str,
                source_tool: str = "") -> dict[str, Any]:
    return save_stream_for_key(user_key(request), fobj, display_name, source_tool,
                               user_label=_user_label(request))


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
            # 畫面要知道畫不畫得出縮圖、該用哪個圖示 —— 由這裡說，前端不自己判斷。
            # （算出來的，不寫回 meta.json。）
            ext = meta.get("ext", "")
            meta["preview"] = has_preview(ext)
            meta["kind"] = kind_of(ext)
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
#: 純文字：沒有可以畫的「第一頁」，縮圖一律回空白佔位圖。
_TEXT_EXTS = (".txt", ".md", ".json")

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
    if ext in _TEXT_EXTS:
        # 純文字沒有「第一頁」可以畫。**要明講**，不要讓它掉到下面那條
        # 「當成 PDF」的路 —— 那會去找一個不存在的 `file.pdf`，
        # 錯誤訊息變成「檔案不存在」，看起來像檔案掉了。
        raise WorkspaceError("純文字沒有預覽圖")
    if ext in AUDIO_EXTS:
        # 同上：錄音 / 錄影沒有預覽圖（前端照 `has_preview()` 直接顯示圖示）。
        # 不可以掉到下面「當成 PDF」那條路 —— 那會說「檔案不存在」。
        raise WorkspaceError("錄音檔沒有預覽圖")
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
    atomic_json.write_json(_meta_path(d), meta)
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
    total_audio = 0
    total_audio_bytes = 0
    if root.exists():
        for udir in sorted(root.iterdir()):
            if not udir.is_dir():
                continue
            cnt = 0
            label = ""
            audio = 0
            audio_bytes = 0
            for d in udir.iterdir():
                if d.is_dir() and _meta_path(d).exists():
                    cnt += 1
                    meta = _read_meta(d) or {}
                    if not label:
                        label = meta.get("user_label", "")
                    # **錄音是敏感資料**（會議內容、人聲）—— 管理員看不到檔案內容，
                    # 但要看得到「誰的工作區裡放著錄音、佔多少」，才知道要不要清。
                    if is_audio_ext(meta.get("ext", "")):
                        audio += 1
                        audio_bytes += int(meta.get("size") or 0)
            size = _dir_size(udir)
            total_bytes += size
            total_count += cnt
            total_audio += audio
            total_audio_bytes += audio_bytes
            users.append({
                "key": udir.name,
                "label": label or (udir.name if udir.name != _SINGLE_KEY else "（單機模式）"),
                "count": cnt,
                "bytes": size,
                "audio_count": audio,
                "audio_bytes": audio_bytes,
            })
    users.sort(key=lambda u: u["bytes"], reverse=True)
    return {"users": users, "total_bytes": total_bytes, "total_count": total_count,
            "audio_count": total_audio, "audio_bytes": total_audio_bytes}
