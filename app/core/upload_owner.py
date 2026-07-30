"""Track upload_id ownership for ACL on /preview, /download, /file endpoints.

Without this, two users with auth ON could fetch each other's uploaded
PDFs by guessing or observing the upload_id (in browser history, server
logs, screenshare, etc.). UUID4 is unguessable but it leaks via URLs.

Design:
- Each new upload writes a sidecar JSON `<temp>/.owners/<upload_id>.json`
  with the user_id of the request that created it. Sidecar (not in-memory)
  so ACL survives service restarts.
- On read endpoints, we compare current user_id vs the recorded owner.
  Admin (effective_tools == ALL) is always allowed.
- When auth is OFF: ACL is a no-op (single-user mode, no isolation needed).
- When the owner record is missing: deny non-admins (could be a legacy
  upload from before this fix, or a tampered URL). Admins can still access
  for support / debugging.
- Sidecar files are auto-cleaned by the temp_dir sweeper since they live
  inside `<temp>/.owners/` (same TTL as the actual uploads).
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

from fastapi import HTTPException, Request

from .safe_paths import is_uuid_hex


def _owners_dir() -> Path:
    from ..config import settings
    d = settings.temp_dir / ".owners"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _user_id(request: Request) -> Optional[int]:
    user = getattr(getattr(request, "state", None), "user", None)
    if not user:
        return None
    if isinstance(user, dict):
        v = user.get("user_id")
    else:
        v = getattr(user, "user_id", None)
    try:
        return int(v) if v is not None else None
    except Exception:
        return None


def _is_admin(uid: int) -> bool:
    try:
        from . import permissions as _perm
        return _perm.effective_tools(uid) == "ALL"
    except Exception:
        return False


def _auth_enabled() -> bool:
    try:
        from . import auth_settings as _as
        return _as.is_enabled()
    except Exception:
        return False


def record(upload_id: str, request: Request) -> None:
    """Record that this upload_id belongs to the request's user. Best-effort
    (errors swallowed). Skipped when auth is off (single-user mode)."""
    if not is_uuid_hex(upload_id):
        return
    if not _auth_enabled():
        return
    uid = _user_id(request)
    if uid is None:
        return
    try:
        f = _owners_dir() / f"{upload_id}.json"
        f.write_text(json.dumps({"user_id": uid, "ts": time.time()}),
                     encoding="utf-8")
    except Exception:
        pass


def check(upload_id: str, request: Request) -> bool:
    """Return True if the request's user is allowed to access this upload's
    files. Allow-all when auth is off. Admin override always wins."""
    if not _auth_enabled():
        return True
    if not is_uuid_hex(upload_id):
        return False
    cur_uid = _user_id(request)
    if cur_uid is None:
        return False
    if _is_admin(cur_uid):
        return True
    f = _owners_dir() / f"{upload_id}.json"
    if not f.exists():
        # No record — be safe and deny non-admins. Could be: legacy upload
        # from before this fix, sweeper cleaned it, or someone guessed an id.
        return False
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return False
    return int(data.get("user_id") or 0) == cur_uid


def require(upload_id: str, request: Request) -> None:
    """Hard ACL check — raise 403 if not allowed. Convenience for
    endpoints; equivalent to `if not check(...): raise HTTPException(403)`."""
    if not check(upload_id, request):
        raise HTTPException(403, "access denied")


def extract_upload_id(filename: str) -> str:
    """Pull the upload_id (32-hex) out of a temp filename.

    Scans **every** `_`-separated segment, not just the first one. Temp names
    are not all `<uuid>_...` — pdf-watermark uses `wm_<uuid>_p1.png`, the API
    path uses `wm_api_<uuid>_out.pdf`. Looking only at the first segment made
    `extract_upload_id("wm_<uuid>_p1.png")` return `""`, and callers that wrote
    `if uid: require(...)` then skipped the ACL entirely.

    The old behaviour was patched at one call site by stripping `"wm_"` there —
    per-call-site workarounds don't survive the next tool that picks a new
    prefix. Do the scan here, once.
    """
    if not filename:
        return ""
    for seg in filename.replace(".", "_").split("_"):
        if is_uuid_hex(seg):
            return seg
    return ""


def record_uid(upload_id: str, user_id: int) -> None:
    """Record ownership for a known user_id (no Request needed).

    For background jobs and tests, where the owning user is known but there's
    no live request to read it from.
    """
    if not is_uuid_hex(upload_id) or not user_id:
        return
    try:
        f = _owners_dir() / f"{upload_id}.json"
        f.write_text(json.dumps({"user_id": int(user_id), "ts": time.time()}),
                     encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def require_by_filename(filename: str, request: Request) -> None:
    """ACL for endpoints that serve a temp file **by name**. Fail-closed.

    `if uid: require(uid, request)` is the wrong shape: an unrecognised name
    silently means "no ACL at all". Temp dir really does hold files that don't
    start with a uuid (`wm_temp_<uuid>.png` — a user-uploaded watermark image),
    so this isn't hypothetical.

    Deny when no upload_id can be found — admins excepted (support / triage),
    and a no-op when auth is off (single-user mode).

    Raises 404, not 403: 403 confirms "this file exists, you just can't have
    it", which is itself information the caller shouldn't get.
    """
    if not _auth_enabled():
        return
    uid = extract_upload_id(filename)
    if uid:
        if not check(uid, request):
            raise HTTPException(404)
        return
    cur = _user_id(request)
    if cur is not None and _is_admin(cur):
        return
    raise HTTPException(404)
