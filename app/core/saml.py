"""SAML 2.0 Service Provider (SP) via python3-saml (OneLogin).

Mirrors the jt-ipam approach. The SP ACS / EntityID / metadata URLs are derived
from the admin-configured public base URL (so it works behind a reverse proxy).
Assertion signature verification is required by default. xmlsec (the native dep)
ships prebuilt wheels for all three platforms, so no system library is needed.
"""
from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from ..logging_setup import get_logger

logger = get_logger(__name__)


class SAMLError(Exception):
    pass


def _acs_url(base_url: str) -> str:
    return base_url.rstrip("/") + "/auth/saml/acs"


def _sls_url(base_url: str) -> str:
    return base_url.rstrip("/") + "/auth/saml/sls"


def _metadata_url(base_url: str) -> str:
    return base_url.rstrip("/") + "/auth/saml/metadata"


def _settings_dict(cfg: dict[str, Any], base_url: str) -> dict[str, Any]:
    if not base_url:
        raise SAMLError("SAML 需要設定對外 base URL（reverse proxy 的公開網址）")
    sp_entity = cfg.get("sp_entity_id") or _metadata_url(base_url)
    s: dict[str, Any] = {
        "strict": True,
        "debug": False,
        "sp": {
            "entityId": sp_entity,
            "assertionConsumerService": {
                "url": _acs_url(base_url),
                "binding": "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST",
            },
            "singleLogoutService": {
                "url": _sls_url(base_url),
                "binding": "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect",
            },
            "NameIDFormat": "urn:oasis:names:tc:SAML:1.1:nameid-format:unspecified",
            "x509cert": cfg.get("sp_x509cert") or "",
            "privateKey": cfg.get("sp_private_key") or "",
        },
        "idp": {
            "entityId": cfg.get("idp_entity_id") or "",
            "singleSignOnService": {
                "url": cfg.get("idp_sso_url") or "",
                "binding": "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect",
            },
            "x509cert": cfg.get("idp_x509cert") or "",
        },
        "security": {
            "wantAssertionsSigned": bool(cfg.get("want_assertions_signed", True)),
            "wantMessagesSigned": False,
            "requestedAuthnContext": False,
            "authnRequestsSigned": bool(cfg.get("sp_private_key")),
            "logoutRequestSigned": bool(cfg.get("sp_private_key")),
            "logoutResponseSigned": bool(cfg.get("sp_private_key")),
        },
    }
    if cfg.get("idp_slo_url"):
        s["idp"]["singleLogoutService"] = {
            "url": cfg["idp_slo_url"],
            "binding": "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect",
        }
    return s


def _request_dict(request: Any, base_url: str, post_data: dict | None = None) -> dict[str, Any]:
    parsed = urlparse(base_url)
    https = "on" if parsed.scheme == "https" else "off"
    host = parsed.hostname or (request.url.hostname if request else "localhost")
    port = parsed.port or (443 if https == "on" else 80)
    return {
        "https": https,
        "http_host": host,
        "server_port": str(port),
        "script_name": request.scope.get("path") if request else "/auth/saml/acs",
        "get_data": dict(request.query_params) if request else {},
        "post_data": post_data or {},
    }


def _auth(request: Any, cfg: dict[str, Any], base_url: str,
          post_data: dict | None = None):
    from onelogin.saml2.auth import OneLogin_Saml2_Auth
    return OneLogin_Saml2_Auth(_request_dict(request, base_url, post_data),
                               _settings_dict(cfg, base_url))


def build_auth_url(request: Any, cfg: dict[str, Any], base_url: str, *,
                   relay_state: str = "/") -> str:
    try:
        return _auth(request, cfg, base_url).login(return_to=relay_state)
    except SAMLError:
        raise
    except Exception as e:
        raise SAMLError(f"建立 SAML 登入請求失敗：{e.__class__.__name__}") from e


def process_acs(request: Any, cfg: dict[str, Any], base_url: str,
                post_data: dict) -> dict[str, Any]:
    """Validate the SAML Response (signature etc.) and return mapped identity."""
    try:
        auth = _auth(request, cfg, base_url, post_data)
        auth.process_response()
    except Exception as e:
        # **完整堆疊一定要進伺服器日誌**。這條路徑走到的是「XML 層就炸掉」
        # 的失敗（最常見：IdP 啟用了斷言加密但 SP 沒設定憑證 / 私鑰、
        # 憑證格式錯、xmlsec 演算法不支援）—— 原本只留類別名稱，客戶問
        # 「有沒有 log 可查」時什麼都查不到（2026-08-19 ADFS 導入案）。
        # 畫面維持通用訊息不變；細節進日誌與稽核（管理員才看得到）。
        logger.exception("SAML 回應解析失敗（XML / 簽章 / 解密層）")
        brief = " ".join(str(e).split())[:120]
        raise SAMLError(
            f"SAML 回應解析失敗：{e.__class__.__name__}"
            + (f"（{brief}）" if brief else "")) from e
    errors = auth.get_errors()
    if errors:
        reason = auth.get_last_error_reason() or ",".join(errors)
        logger.warning("SAML ACS errors: %s (%s)", errors, reason)
        raise SAMLError(f"SAML 驗證失敗：{reason[:160]}")
    if not auth.is_authenticated():
        logger.warning("SAML 回應驗證通過但 is_authenticated 為 False")
        raise SAMLError("SAML 未通過驗證")
    # Replay protection: reject an assertion ID we've already consumed (signature
    # + NotOnOrAfter are checked by OneLogin above, but not cross-request replay).
    from . import sso_store
    try:
        aid = auth.get_last_assertion_id()
        naa = auth.get_last_assertion_not_on_or_after()
    except Exception:
        aid, naa = "", None
    if sso_store.assertion_is_replay(aid, float(naa) if naa else None):
        raise SAMLError("SAML assertion 已被使用過（疑似重放攻擊）")
    attrs = auth.get_attributes() or {}
    name_id = auth.get_nameid() or ""

    def _attr(name: str) -> str:
        if not name:
            return ""
        v = attrs.get(name)
        if isinstance(v, (list, tuple)):
            return str(v[0]) if v else ""
        return str(v) if v is not None else ""

    username = _attr(cfg.get("username_attr") or "") or name_id
    email = _attr(cfg.get("email_attr") or "")
    name = _attr(cfg.get("name_attr") or "") or username
    groups: list[str] = []
    gattr = cfg.get("groups_attr") or ""
    if gattr and gattr in attrs:
        gv = attrs.get(gattr) or []
        groups = [str(g) for g in gv] if isinstance(gv, (list, tuple)) else [str(gv)]
    try:
        session_index = auth.get_session_index() or ""
    except Exception:
        session_index = ""
    return {"nameid": name_id, "username": username, "email": email,
            "name": name, "groups": groups, "session_index": session_index,
            "relay_state": (post_data.get("RelayState") or "/")}


def logout_url(request: Any, cfg: dict[str, Any], base_url: str, *,
               name_id: str = "", session_index: str = "",
               return_to: str = "/") -> str | None:
    """SP-initiated Single-Logout: build a LogoutRequest to the IdP's SLS and
    return the redirect URL. Returns None when the IdP has no SLO endpoint
    configured (caller then just does local logout)."""
    if not cfg.get("idp_slo_url"):
        return None
    try:
        auth = _auth(request, cfg, base_url)
        return auth.logout(return_to=return_to, name_id=name_id or None,
                           session_index=session_index or None)
    except Exception as e:
        logger.warning("SAML logout_url build failed: %s", e.__class__.__name__)
        return None


def process_sls(request: Any, cfg: dict[str, Any], base_url: str,
                get_data: dict) -> str:
    """Handle the IdP's LogoutResponse / LogoutRequest at our SLS endpoint.
    Returns a redirect URL (the IdP-provided RelayState or '/login'). Local
    session teardown is the route's responsibility; this only validates SLO."""
    try:
        auth = _auth(request, cfg, base_url)
        # keep_local_session=True: we clear our own session in the route.
        url = auth.process_slo(keep_local_session=True, delete_session_cb=None)
        errors = auth.get_errors()
        if errors:
            logger.warning("SAML SLS errors: %s", errors)
        return url or "/login"
    except Exception as e:
        logger.warning("SAML process_slo failed: %s", e.__class__.__name__)
        return "/login"


def sp_metadata(cfg: dict[str, Any], base_url: str) -> str:
    from onelogin.saml2.settings import OneLogin_Saml2_Settings
    try:
        s = OneLogin_Saml2_Settings(_settings_dict(cfg, base_url), sp_validation_only=True)
        meta = s.get_sp_metadata()
        errors = s.validate_metadata(meta)
        if errors:
            raise SAMLError(f"SP metadata 無效：{errors}")
        return meta.decode("utf-8") if isinstance(meta, bytes) else meta
    except SAMLError:
        raise
    except Exception as e:
        raise SAMLError(f"產生 SP metadata 失敗：{e.__class__.__name__}") from e
