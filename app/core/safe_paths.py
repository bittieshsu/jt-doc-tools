"""Path-safety helpers — reject path traversal in user-supplied filenames.

Used by the dozens of `/preview/{name}` / `/download/{filename}` endpoints
across tool routers. Centralised so the rule is uniform and reviewable.

Rule: filenames must be plain ASCII (alnum + `._-`), no slash / backslash /
NUL / dotdot. Anything else is rejected with HTTP 400.

Why not just `Path(name).name`? That strips path components but allows
unicode normalization tricks, percent-encoding from path params (FastAPI
already decodes), and on Windows things like `CON`/`NUL` reserved names.
A strict allowlist is simpler and we control all the producers — they
all generate names from `uuid4().hex` + a fixed suffix anyway.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from fastapi import HTTPException

# 32-hex uuid + optional suffix(_p1, _filled, etc.) + extension.
# Allows our internal naming convention; rejects everything else.
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9._\-]{1,255}$")
# Strict UUID4-hex check (32 lowercase hex). Use to bound upload_id path params.
UUID_HEX_RE = re.compile(r"^[a-f0-9]{32}$")


def is_safe_name(name: str) -> bool:
    """Pure boolean — does name pass our strict allowlist?"""
    if not name or len(name) > 255:
        return False
    if name in (".", ".."):
        return False
    if "/" in name or "\\" in name or "\x00" in name:
        return False
    if ".." in name:
        return False
    return bool(_SAFE_NAME_RE.match(name))


def sanitize_filename(name: str) -> str:
    """Return name unchanged if safe, else raise HTTP 400."""
    if not is_safe_name(name):
        raise HTTPException(400, "invalid filename")
    return name


def safe_join(base: Path, name: str) -> Path:
    """Resolve `name` under `base`. Reject if result escapes base or fails
    the strict filename rule. Returns a fully-resolved Path."""
    safe = sanitize_filename(name)
    p = (base / safe).resolve()
    base_resolved = base.resolve()
    # Containment check — covers symlink/escape edge cases too
    try:
        p.relative_to(base_resolved)
    except ValueError:
        raise HTTPException(400, "path escape blocked")
    return p


def is_uuid_hex(s: str) -> bool:
    """Check string is 32-char lowercase hex (our standard upload_id form)."""
    return bool(UUID_HEX_RE.match(s or ""))


def require_uuid_hex(s: str, field: str = "id") -> str:
    """Validate or raise HTTP 400. Returns the validated string."""
    if not is_uuid_hex(s):
        raise HTTPException(400, f"invalid {field}")
    return s


# ── 管理者可設定的輸出目錄 ──────────────────────────────────────────────────
# 設定匯出 / 排程備份的目標目錄由管理員自行填寫，把備份放到 /mnt/backup 這類外部
# 路徑是**合理且常見**的用法，所以不能硬性限制在資料目錄底下（會讓既有客戶的設定
# 失效）。但仍要擋住明顯危險或明顯是誤填的目標：寫進系統目錄可能覆蓋掉作業系統
# 檔案，而相對路徑會隨行程工作目錄漂移、寫到預期外的地方。
_FORBIDDEN_ROOTS_POSIX = (
    "/etc", "/proc", "/sys", "/dev", "/boot", "/bin", "/sbin",
    "/usr/bin", "/usr/sbin", "/lib", "/lib64", "/run",
)
_FORBIDDEN_PARTS_WINDOWS = ("windows", "system32", "syswow64", "program files",
                            "program files (x86)")


class UnsafeOutputDir(ValueError):
    """管理員設定的輸出目錄不可接受（相對路徑 / 系統目錄 / 目標是檔案）。"""


def safe_output_dir(raw: str | Path) -> Path:
    """驗證並正規化「管理員設定的輸出目錄」。

    通過條件：絕對路徑、正規化後不落在系統目錄、目標不是既有檔案。
    不限制必須在資料目錄底下（外部備份路徑是合理用法）。

    Raises:
        UnsafeOutputDir: 不可接受時，訊息可直接顯示給管理員。
    """
    p = Path(raw)
    if not p.is_absolute():
        raise UnsafeOutputDir("匯出目錄必須是絕對路徑（避免隨執行位置漂移）")
    resolved = p.resolve()
    text = str(resolved)
    parts_lower = [seg.lower() for seg in resolved.parts]
    if os.name == "nt":
        for bad in _FORBIDDEN_PARTS_WINDOWS:
            if bad in parts_lower:
                raise UnsafeOutputDir("不可匯出到系統目錄（%s）" % bad)
    else:
        for bad in _FORBIDDEN_ROOTS_POSIX:
            if text == bad or text.startswith(bad + "/"):
                raise UnsafeOutputDir("不可匯出到系統目錄（%s）" % bad)
        if resolved == Path("/"):
            raise UnsafeOutputDir("不可匯出到根目錄")
    if resolved.exists() and not resolved.is_dir():
        raise UnsafeOutputDir("匯出目錄路徑已存在同名檔案，請換一個位置")
    return resolved
