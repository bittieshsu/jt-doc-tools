"""SSO (OIDC + SAML) configuration store.

SSO is an **additional** login method that coexists with the primary auth
backend (off / local / ldap / ad). Enabling SSO never disables local login, so
the built-in ``jtdt-admin`` always remains as a break-glass account.

Config lives at ``data/sso_settings.json`` (mode 600). Client secrets / SP
private keys are encrypted at rest with Fernet, keyed off the same 32-byte
``.session_secret`` used for session signing (so there is one secret to protect,
not two). ``get()`` returns secrets MASKED for admin display; the service layer
calls ``get_oidc(reveal=True)`` / ``get_saml(reveal=True)`` to obtain decrypted
values at request time.

Group→role mapping: IdP group claims/attributes are synced as local ``groups``
rows (source ``oidc`` / ``saml``), exactly like LDAP/AD — the admin then maps
those groups to roles in the permission matrix. An optional ``admin_group`` is a
convenience shortcut that grants the built-in admin role to its members.
"""
from __future__ import annotations

import base64
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from ..config import settings
from ..logging_setup import get_logger

logger = get_logger(__name__)

_LOCK = threading.RLock()
_CACHE: dict[str, Any] | None = None

# Sentinel returned/accepted by the admin UI so an unchanged secret field
# (shown masked) does not overwrite the stored ciphertext on save.
SECRET_KEPT = "__JTDT_SECRET_KEPT__"

_DEFAULTS: dict[str, Any] = {
    # Public HTTPS base URL the IdP redirects back to (behind a reverse proxy
    # this MUST be the external URL, e.g. https://docs.example.com). Empty =
    # derive from the incoming request (works for direct / dev access).
    "base_url": "",
    "oidc": {
        "enabled": False,
        "display_name": "OIDC 登入",      # login-button label
        "issuer": "",                      # e.g. https://login.microsoftonline.com/<tenant>/v2.0
        "client_id": "",
        "client_secret_enc": "",           # Fernet ciphertext
        "scopes": "openid email profile",
        "username_claim": "preferred_username",
        "email_claim": "email",
        "name_claim": "name",
        "groups_claim": "groups",
        "admin_group": "",                 # optional: members → admin role
    },
    "saml": {
        "enabled": False,
        "display_name": "SAML 登入",
        "idp_entity_id": "",
        "idp_sso_url": "",                  # IdP SingleSignOnService (HTTP-Redirect)
        "idp_x509cert": "",                # IdP signing cert (PEM body, no headers)
        "sp_entity_id": "",                # our SP EntityID; blank → <base>/auth/saml/metadata
        "want_assertions_signed": True,
        "username_attr": "",               # SAML attribute name → username (blank → NameID)
        "email_attr": "",
        "name_attr": "",
        "groups_attr": "",
        "admin_group": "",
        "sp_private_key_enc": "",          # optional, to sign AuthnRequests (Fernet ciphertext)
        "sp_x509cert": "",
    },
    "updated_at": 0.0,
}

_SECRET_FIELDS = {
    ("oidc", "client_secret_enc"),
    ("saml", "sp_private_key_enc"),
}


def _path() -> Path:
    return settings.data_dir / "sso_settings.json"


# ---------- encryption (Fernet keyed off the session secret) ----------

def _fernet():
    from cryptography.fernet import Fernet
    from . import auth_settings
    key = base64.urlsafe_b64encode(auth_settings._ensure_secret())  # 32 bytes → valid key
    return Fernet(key)


def encrypt_secret(plaintext: str) -> str:
    if not plaintext:
        return ""
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_secret(ciphertext: str) -> str:
    if not ciphertext:
        return ""
    try:
        return _fernet().decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except Exception:
        logger.warning("sso_settings: secret decrypt failed (key rotated?)")
        return ""


# ---------- persistence ----------

def _deep_merge(target: dict, src: dict) -> None:
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(target.get(k), dict):
            _deep_merge(target[k], v)
        else:
            target[k] = v


def _load() -> dict[str, Any]:
    global _CACHE
    if _CACHE is None:
        merged = json.loads(json.dumps(_DEFAULTS))
        p = _path()
        if p.exists():
            try:
                _deep_merge(merged, json.loads(p.read_text(encoding="utf-8")))
            except Exception as exc:
                logger.error("sso_settings parse failed (%s); using defaults", exc)
        _CACHE = merged
    return _CACHE


def get(*, mask_secrets: bool = True) -> dict[str, Any]:
    """Return a deep copy of the settings. Secret fields are replaced with
    SECRET_KEPT (if set) when mask_secrets, so they never reach the admin UI."""
    with _LOCK:
        data = json.loads(json.dumps(_load()))
    if mask_secrets:
        for section, field in _SECRET_FIELDS:
            if data.get(section, {}).get(field):
                data[section][field] = SECRET_KEPT
    return data


def save(new_settings: dict[str, Any]) -> None:
    """Persist settings. Secret fields equal to SECRET_KEPT preserve the stored
    ciphertext; any other (non-empty) value is treated as a new PLAINTEXT secret
    and encrypted; empty string clears it."""
    global _CACHE
    with _LOCK:
        current = json.loads(json.dumps(_load()))
        incoming = json.loads(json.dumps(new_settings))
        # Handle secret fields before the generic merge so we control encryption.
        for section, field in _SECRET_FIELDS:
            if section not in incoming:
                continue
            val = incoming[section].get(field, SECRET_KEPT)
            if val == SECRET_KEPT:
                incoming[section][field] = current.get(section, {}).get(field, "")
            elif val:
                incoming[section][field] = encrypt_secret(val)
            else:
                incoming[section][field] = ""
        merged = json.loads(json.dumps(_DEFAULTS))
        _deep_merge(merged, current)
        _deep_merge(merged, incoming)
        merged["updated_at"] = time.time()
        p = _path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except Exception:
            pass
        tmp.replace(p)
        _CACHE = merged


def _invalidate_cache() -> None:
    global _CACHE
    with _LOCK:
        _CACHE = None


# ---------- accessors for the service layer ----------

def get_oidc(*, reveal: bool = False) -> dict[str, Any]:
    d = json.loads(json.dumps(_load()["oidc"]))
    if reveal:
        d["client_secret"] = decrypt_secret(d.get("client_secret_enc", ""))
    return d


def get_saml(*, reveal: bool = False) -> dict[str, Any]:
    d = json.loads(json.dumps(_load()["saml"]))
    if reveal:
        d["sp_private_key"] = decrypt_secret(d.get("sp_private_key_enc", ""))
    return d


def oidc_enabled() -> bool:
    return bool(_load()["oidc"].get("enabled"))


def saml_enabled() -> bool:
    return bool(_load()["saml"].get("enabled"))


def any_enabled() -> bool:
    return oidc_enabled() or saml_enabled()


def base_url() -> str:
    return (_load().get("base_url") or "").rstrip("/")


def login_buttons() -> list[dict[str, str]]:
    """Providers to render on the login page (enabled + minimally configured)."""
    out: list[dict[str, str]] = []
    o = _load()["oidc"]
    if o.get("enabled") and o.get("issuer") and o.get("client_id"):
        out.append({"provider": "oidc", "label": o.get("display_name") or "OIDC 登入",
                    "url": "/auth/oidc/login"})
    sm = _load()["saml"]
    if sm.get("enabled") and sm.get("idp_sso_url") and sm.get("idp_x509cert"):
        out.append({"provider": "saml", "label": sm.get("display_name") or "SAML 登入",
                    "url": "/auth/saml/login"})
    return out
