"""Admin endpoints for authentication, users, groups, roles, permissions.

All endpoints inherit `require_admin` from the parent admin router (added
via router-level dependency), so they're locked behind the admin role
when auth is on, and freely accessible when auth is off (existing
behaviour).
"""
from __future__ import annotations

import logging
import time

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from ..core import (audit_db, audit_forward, auth_db, auth_settings,
                    group_manager, permissions, roles, sso_settings,
                    user_manager)
from ..core import db
from ..core import sessions as _ss


def _all_tool_ids() -> list[str]:
    from ..tool_registry import discover_tools
    return [t.metadata.id for t in discover_tools()]

logger = logging.getLogger(__name__)


def _client_ip(r: Request) -> str:
    from ..core import client_ip as _cip
    return _cip.real_client_ip(r)


def _logged_in_identity_sets():
    """(dns, logins) of directory users who have **actually logged in**
    (`last_login_at>0`) — used to mark 目錄成員「已登入過本系統」。

    Mere existence in the local `users` table no longer counts: directory sync
    now mirrors ALL directory users as a catalog (enabled=0, never logged in),
    so "in the table" ≠ "has used this system". Targeted query (only the
    logged-in subset) so it stays cheap even with thousands of mirrored rows."""
    from ..core import auth_db
    dns, logins = set(), set()
    rows = auth_db.conn().execute(
        "SELECT external_dn, username FROM users "
        "WHERE source IN ('ldap','ad') AND COALESCE(last_login_at,0)>0"
    ).fetchall()
    for r in rows:
        if r["external_dn"]:
            dns.add(str(r["external_dn"]).strip().lower())
        if r["username"]:
            un = str(r["username"]).strip().lower()
            logins.add(un)
            logins.add(un.split("@", 1)[0])
    return dns, logins


def _actor(r: Request) -> str:
    user = getattr(r.state, "user", None)
    return user["username"] if user else ""


def build_auth_router(templates) -> APIRouter:
    router = APIRouter()

    # ---------- /admin/sso (OIDC + SAML，附加登入) ----------

    @router.get("/sso", response_class=HTMLResponse)
    async def sso_page(request: Request):
        from ..core import sso_provision  # noqa: F401 (ensure importable)
        _s = auth_settings.get()
        return templates.TemplateResponse(request, "admin_sso.html", {
            "request": request,
            "sso": sso_settings.get(),          # secrets masked
            "auth_enabled": auth_settings.is_enabled(),
            "proxy_sso": _s.get("proxy_sso", {}),
            "ldap_configured": bool((_s.get("ldap") or {}).get("server_url")
                                    and (_s.get("ldap") or {}).get("service_dn")),
            # Direct TCP peer as this server sees it — for the proxy-SSO panel
            # to hint which IP to trust (behind a reverse proxy this IS the
            # proxy's IP, i.e. the value to put in trusted_proxies).
            "peer_ip": (request.client.host if request.client else ""),
        })

    @router.post("/sso/proxy-save")
    async def sso_proxy_save(request: Request):
        """Save the reverse-proxy (Kerberos/SPNEGO) SSO settings. Additive
        login path — does not touch the primary backend. See proxy_sso.py."""
        body = await request.json()
        raw_proxies = body.get("trusted_proxies")
        if isinstance(raw_proxies, str):
            # Accept newline- or comma-separated text from a textarea.
            raw_proxies = [p.strip() for p in raw_proxies.replace(",", "\n").splitlines()]
        proxies = [p.strip() for p in (raw_proxies or []) if p and p.strip()]
        enabled = bool(body.get("enabled"))
        # Guard 1: enabling proxy SSO without auth on = no break-glass admin.
        if enabled and not auth_settings.is_enabled():
            raise HTTPException(409, "請先於「認證設定」啟用認證並建立管理員，再開啟 Reverse Proxy SSO（保留 break-glass 帳號）")
        # Guard 2: it resolves users via LDAP/AD, so that must be configured.
        _s = auth_settings.get()
        ldap_ok = bool((_s.get("ldap") or {}).get("server_url")
                       and (_s.get("ldap") or {}).get("service_dn"))
        if enabled and not ldap_ok:
            raise HTTPException(409, "Reverse Proxy SSO 需要先填妥 LDAP/AD 連線設定（用來查詢並同步網域使用者）")
        # Guard 3: refuse to enable with an empty trusted-proxy list — that
        # would trust the header from ANY source (spoofable).
        if enabled and not proxies:
            raise HTTPException(400, "啟用時「信任的反向代理 IP」不可空白，否則任何來源都能偽造帳號")
        # Guard 4: every entry must be a valid IP or CIDR, and reject
        # all-encompassing networks (0.0.0.0/0, ::/0, wildcards) — those trust
        # EVERY source, making the header-spoofing defence meaningless.
        import ipaddress as _ipa
        for p in proxies:
            if p in ("*", "0.0.0.0", "::", "0.0.0.0/0", "::/0"):
                raise HTTPException(400, f"「{p}」會信任所有來源，等於關閉防偽造保護，不允許")
            try:
                if "/" in p:
                    net = _ipa.ip_network(p, strict=False)
                    if net.prefixlen == 0:
                        raise HTTPException(400, f"「{p}」涵蓋所有位址，不允許（請填實際反向代理的 IP）")
                else:
                    _ipa.ip_address(p)
            except HTTPException:
                raise
            except ValueError:
                raise HTTPException(400, f"「{p}」不是合法的 IP 或 CIDR")
        _s["proxy_sso"] = {
            "enabled": enabled,
            "header": (body.get("header") or "X-Remote-User").strip() or "X-Remote-User",
            "fallback_login": bool(body.get("fallback_login", True)),
            "trusted_proxies": proxies or ["127.0.0.1", "::1"],
        }
        auth_settings.save(_s)
        audit_db.log_event("settings_change", username=_actor(request),
                           ip=_client_ip(request), target="proxy_sso",
                           details={"enabled": enabled, "header": _s["proxy_sso"]["header"],
                                    "fallback_login": _s["proxy_sso"]["fallback_login"],
                                    "trusted_proxies": proxies})
        return JSONResponse({"ok": True})

    @router.post("/sso/save")
    async def sso_save(request: Request):
        body = await request.json()
        oidc_in = body.get("oidc") or {}
        saml_in = body.get("saml") or {}
        new = {
            "base_url": (body.get("base_url") or "").strip().rstrip("/"),
            "oidc": {
                "enabled": bool(oidc_in.get("enabled")),
                "display_name": (oidc_in.get("display_name") or "").strip()[:64],
                "issuer": (oidc_in.get("issuer") or "").strip().rstrip("/"),
                "client_id": (oidc_in.get("client_id") or "").strip(),
                "client_secret_enc": oidc_in.get("client_secret_enc", sso_settings.SECRET_KEPT),
                "require_https": bool(oidc_in.get("require_https", True)),
                "scopes": (oidc_in.get("scopes") or "openid email profile").strip(),
                "username_claim": (oidc_in.get("username_claim") or "preferred_username").strip(),
                "email_claim": (oidc_in.get("email_claim") or "email").strip(),
                "name_claim": (oidc_in.get("name_claim") or "name").strip(),
                "groups_claim": (oidc_in.get("groups_claim") or "groups").strip(),
                "admin_group": (oidc_in.get("admin_group") or "").strip(),
            },
            "saml": {
                "enabled": bool(saml_in.get("enabled")),
                "display_name": (saml_in.get("display_name") or "").strip()[:64],
                "idp_entity_id": (saml_in.get("idp_entity_id") or "").strip(),
                "idp_sso_url": (saml_in.get("idp_sso_url") or "").strip(),
                "idp_slo_url": (saml_in.get("idp_slo_url") or "").strip(),
                "idp_x509cert": (saml_in.get("idp_x509cert") or "").strip(),
                "sp_entity_id": (saml_in.get("sp_entity_id") or "").strip(),
                "want_assertions_signed": bool(saml_in.get("want_assertions_signed", True)),
                "username_attr": (saml_in.get("username_attr") or "").strip(),
                "email_attr": (saml_in.get("email_attr") or "").strip(),
                "name_attr": (saml_in.get("name_attr") or "").strip(),
                "groups_attr": (saml_in.get("groups_attr") or "").strip(),
                "admin_group": (saml_in.get("admin_group") or "").strip(),
                "sp_private_key_enc": saml_in.get("sp_private_key_enc", sso_settings.SECRET_KEPT),
                "sp_x509cert": (saml_in.get("sp_x509cert") or "").strip(),
            },
        }
        # Guard: enabling SSO without a primary auth backend would let anyone the
        # IdP authenticates in, but there'd be no break-glass admin. Require auth on.
        if (new["oidc"]["enabled"] or new["saml"]["enabled"]) and not auth_settings.is_enabled():
            raise HTTPException(409, "請先於「認證設定」啟用認證並建立管理員，再開啟 SSO（保留 break-glass 帳號）")
        sso_settings.save(new)
        sso_settings._invalidate_cache()
        audit_db.log_event("settings_change", username=_actor(request),
                           ip=_client_ip(request), target="sso",
                           details={"oidc": new["oidc"]["enabled"],
                                    "saml": new["saml"]["enabled"]})
        return JSONResponse({"ok": True})

    @router.post("/sso/test")
    async def sso_test(request: Request):
        """Validate OIDC discovery + SAML SP metadata against current saved
        config (does not require enabling). Returns per-provider result."""
        body = await request.json()
        which = body.get("provider")
        out: dict = {}
        if which in (None, "oidc"):
            try:
                from ..core import oidc as _oidc
                cfg = sso_settings.get_oidc(reveal=True)
                doc = _oidc.discover(cfg)
                out["oidc"] = {"ok": True,
                               "authorization_endpoint": doc.get("authorization_endpoint"),
                               "token_endpoint": doc.get("token_endpoint")}
            except Exception as e:
                logger.warning("SSO OIDC 連線測試失敗: %s", e)
                out["oidc"] = {"ok": False, "error": f"測試失敗（{type(e).__name__}），詳見伺服器日誌"}
        if which in (None, "saml"):
            try:
                from ..core import saml as _saml
                cfg = sso_settings.get_saml(reveal=True)
                base = sso_settings.base_url()
                _saml.sp_metadata(cfg, base)
                out["saml"] = {"ok": True, "metadata_url": (base + "/auth/saml/metadata") if base else ""}
            except Exception as e:
                logger.warning("SSO SAML 連線測試失敗: %s", e)
                out["saml"] = {"ok": False, "error": f"測試失敗（{type(e).__name__}），詳見伺服器日誌"}
        return JSONResponse({"ok": True, "result": out})

    # ---------- /admin/auth-settings ----------

    @router.get("/auth-settings", response_class=HTMLResponse)
    async def auth_settings_page(request: Request):
        s = auth_settings.get()
        # 目前仍在鎖定中的帳號 / IP（給「解鎖清單」用）。
        import time as _t
        now = _t.time()
        rows = auth_db.conn().execute(
            "SELECT key, locked_until FROM lockouts WHERE locked_until > ? "
            "ORDER BY locked_until DESC", (now,)).fetchall()
        locked_accounts, locked_ips = [], []
        for r in rows:
            key = r["key"] or ""
            mins = max(1, int((r["locked_until"] - now + 59) // 60))
            if key.startswith("user:"):
                locked_accounts.append({"name": key[5:], "mins": mins})
            elif key.startswith("ip:"):
                locked_ips.append({"ip": key[3:], "mins": mins})
        return templates.TemplateResponse(request, "admin_auth_settings.html", {
            "request": request,
            "settings": s,
            "is_enabled": auth_settings.is_enabled(),
            "locked_accounts": locked_accounts,
            "locked_ips": locked_ips,
        })

    @router.post("/auth-settings/policy-save")
    async def auth_settings_policy_save(request: Request):
        """儲存鎖定政策（啟用 / 視窗 / 帳號門檻 / IP 門檻 / 鎖定時間）。"""
        body = await request.json()
        def _int(name, default, lo, hi):
            try:
                v = int(body.get(name, default))
            except (TypeError, ValueError):
                raise HTTPException(400, f"{name} 必須是整數")
            if v < lo or v > hi:
                raise HTTPException(400, f"{name} 需在 {lo}–{hi} 之間")
            return v
        s = auth_settings.get()
        s["lockout_enabled"] = bool(body.get("lockout_enabled", True))
        s["lockout_window_minutes"] = _int("lockout_window_minutes", 10, 1, 1440)
        s["lockout_threshold"] = _int("lockout_threshold", 5, 1, 1000)
        s["lockout_ip_threshold"] = _int("lockout_ip_threshold", 20, 1, 10000)
        s["lockout_minutes"] = _int("lockout_minutes", 15, 1, 10080)
        auth_settings.save(s)
        audit_db.log_event(
            "settings_change", username=_actor(request), ip=_client_ip(request),
            target="lockout_policy",
            details={k: s[k] for k in ("lockout_enabled", "lockout_window_minutes",
                                       "lockout_threshold", "lockout_ip_threshold",
                                       "lockout_minutes")})
        return JSONResponse({"ok": True})

    @router.post("/auth-settings/unlock-key")
    async def auth_settings_unlock_key(request: Request):
        """解鎖單一鎖定項目（帳號 `user:<帳號>` 或 IP `ip:<addr>`），給「目前
        鎖定中」清單的每列解鎖鈕用。只接受 user:/ip: 前綴，避免亂刪。"""
        body = await request.json()
        key = (body.get("key") or "").strip()
        if not (key.startswith("user:") or key.startswith("ip:")):
            raise HTTPException(400, "無效的鎖定鍵")
        from ..core import db
        conn = auth_db.conn()
        with db.tx(conn):
            n = conn.execute("DELETE FROM lockouts WHERE key=?", (key,)).rowcount
        audit_db.log_event(
            "lockout_unlock", username=_actor(request), ip=_client_ip(request),
            target=key, details={"cleared": n})
        return JSONResponse({"ok": True, "cleared": n})

    @router.post("/auth-settings/disable")
    async def auth_settings_disable(request: Request):
        if not auth_settings.is_enabled():
            return JSONResponse({"ok": True, "noop": True})
        auth_settings.disable_auth(actor=_actor(request), ip=_client_ip(request))
        return JSONResponse({"ok": True})

    @router.post("/auth-settings/ldap-save")
    async def auth_settings_ldap_save(request: Request):
        """Configure LDAP/AD settings. To switch the backend itself (off →
        ldap), set body['backend'] = 'ldap' or 'ad'; otherwise we just
        update the LDAP block.

        Switching from 'local' → 'ldap' will leave existing local users
        intact (they just won't be able to log in until you switch back).
        """
        # Defence in depth: refuse if auth is not enabled. The UI also locks
        # this form, but a curl/script could still hit the endpoint and lock
        # the admin out (no jtdt-admin exists yet to log back in with).
        if not auth_settings.is_enabled():
            raise HTTPException(
                409,
                "Cannot configure LDAP/AD backend before authentication is enabled. "
                "Visit /setup-admin to enable auth and create the first admin first.",
            )
        body = await request.json()
        target_backend = (body.get("backend") or "").lower()
        ldap_cfg = body.get("ldap") or {}
        if target_backend not in ("", "off", "local", "ldap", "ad"):
            raise HTTPException(400, "invalid backend")
        s = auth_settings.get()
        if target_backend:
            s["backend"] = target_backend
        # Merge new LDAP fields into the block (don't blow away service_password
        # if caller didn't provide one — admin is just editing other fields).
        for k in ("server_url", "use_tls", "verify_cert", "service_dn",
                  "user_search_base", "user_search_filter", "group_attr",
                  "username_attr", "displayname_attr", "email_attr"):
            if k in ldap_cfg:
                s["ldap"][k] = ldap_cfg[k]
        if ldap_cfg.get("service_password"):
            # Note: storing in plain JSON for v1.1.0 (file is mode 600).
            # M3+ enhancement: encrypt with Fernet keyed off session secret.
            s["ldap"]["service_password"] = ldap_cfg["service_password"]
        auth_settings.save(s)
        audit_db.log_event(
            "settings_change", username=_actor(request), ip=_client_ip(request),
            target="ldap", details={k: v for k, v in ldap_cfg.items()
                                    if k != "service_password"},
        )
        return {"ok": True, "backend": s["backend"]}

    def _build_ldap_cfg_from_request(body: dict) -> dict:
        """Compose an LDAP cfg dict from request body, falling back to the
        saved value for any field the user left blank — so the admin can
        test their just-edited form without re-entering the saved password."""
        saved = auth_settings.get().get("ldap", {}) or {}
        ldap_in = body.get("ldap") or {}
        merged = {}
        for k in ("server_url", "service_dn", "user_search_base",
                  "user_search_filter", "username_attr", "displayname_attr",
                  "group_attr", "email_attr"):
            v = ldap_in.get(k)
            merged[k] = (v if v not in (None, "") else saved.get(k, ""))
        # bools — accept explicit False from the form
        for k in ("use_tls", "verify_cert"):
            if k in ldap_in:
                merged[k] = bool(ldap_in[k])
            else:
                merged[k] = bool(saved.get(k, False))
        # password: if user typed a new one use it, else use saved.
        merged["service_password"] = (
            ldap_in.get("service_password") or saved.get("service_password", "")
        )
        return merged

    @router.post("/auth-settings/ldap-test-connection")
    async def auth_settings_ldap_test_connection(request: Request):
        from ..core import auth_ldap
        body = await request.json()
        cfg = _build_ldap_cfg_from_request(body)
        try:
            res = auth_ldap.test_connection(cfg)
        except auth_ldap.AuthError as exc:
            audit_db.log_event(
                "settings_change", username=_actor(request),
                ip=_client_ip(request), target="ldap_test_connection",
                details={"ok": False, "error": str(exc)[:200]},
            )
            raise HTTPException(400, str(exc))
        audit_db.log_event(
            "settings_change", username=_actor(request), ip=_client_ip(request),
            target="ldap_test_connection",
            details={"ok": True, "elapsed_ms": res.get("elapsed_ms")},
        )
        return res

    @router.post("/auth-settings/ldap-test-login")
    async def auth_settings_ldap_test_login(request: Request):
        from ..core import auth_ldap
        body = await request.json()
        cfg = _build_ldap_cfg_from_request(body)
        username = (body.get("username") or "").strip()
        password = body.get("password") or ""
        try:
            res = auth_ldap.test_user_login(cfg, username, password)
        except auth_ldap.AuthError as exc:
            audit_db.log_event(
                "settings_change", username=_actor(request),
                ip=_client_ip(request), target="ldap_test_login",
                details={"ok": False, "tested_user": username,
                         "error": str(exc)[:200]},
            )
            raise HTTPException(400, str(exc))
        audit_db.log_event(
            "settings_change", username=_actor(request), ip=_client_ip(request),
            target="ldap_test_login",
            details={"ok": True, "tested_user": username,
                     "elapsed_ms": res.get("elapsed_ms")},
        )
        # Truncate group list before returning — we don't need them all on UI.
        res["groups"] = res.get("groups", [])[:20]
        return res

    # ---------- /admin/users ----------

    @router.get("/users", response_class=HTMLResponse)
    async def users_page(request: Request):
        # Default view = 'active': excludes the (potentially thousands of)
        # directory users mirrored as a catalog (enabled=0, never logged in) —
        # those live in the directory browser. This is what stops the browser
        # OOM the customer hit after bulk AD user sync.
        qp = request.query_params
        view = (qp.get("view") or "active").lower()
        if view not in ("active", "directory", "all", "missing",
                        "dir_disabled", "pwd_expiring"):
            view = "active"
        q = (qp.get("q") or "").strip()
        src = (qp.get("src") or "").strip().lower()
        if src not in ("", "local", "ldap", "ad", "oidc", "saml"):
            src = ""
        st = (qp.get("state") or "").strip().lower()   # on / off / never
        try:
            page = max(1, int(qp.get("page") or 1))
        except ValueError:
            page = 1
        size = user_manager.PAGE_SIZE
        # **篩選一律在伺服器端做**。前端過濾只過濾得到「已經 render 出來的那一頁」，
        # 使用者以為篩了全部其實只篩了眼前 100 筆 —— 那會讓人以為某個帳號不存在。
        data = user_manager.list_users_page(
            view=view, q=q, offset=(page - 1) * size, limit=size,
            source=src,
            enabled=(True if st == "on" else (False if st == "off" else None)),
            never_logged_in=(st == "never"))
        users = data["rows"]
        page_total = data["total"]
        pages = max(1, (page_total + size - 1) // size)
        dir_count = user_manager.count_users(view="directory")
        missing_count = user_manager.count_users(view="missing")
        # 目錄端停用 / 密碼快到期的人數。0 的時候頁籤不顯示 ——
        # 不是 AD 環境的話這兩個永遠是 0，沒必要占版面。
        dir_disabled_count = user_manager.count_users(view="dir_disabled")
        pwd_expiring_count = user_manager.count_users(view="pwd_expiring")
        # 「全部停用」按鈕上的數字要是**實際會動到的人數**（還啟用中的），
        # 不是這個檢視的總筆數 —— 否則停用完之後按鈕還寫著「共 3 人」，
        # 再按一次卻什麼都沒發生。
        from ..core import directory_cleanup as _dcl
        actionable = {v: len(_dcl.candidates(v))
                      for v in ("missing", "dir_disabled")}
        all_roles = roles.list_roles()
        # Lightweight group list for the edit-modal picker: id/name/source only,
        # NOT list_groups() (which carries per-group member_ids arrays — those
        # bloated the page to the point of OOM and the picker never used them).
        all_groups = group_manager.list_group_names()
        # Enrich each user with role display names so the table can show
        # human labels ("管理員") not just slugs ("admin"). Keep `roles` as
        # the slug list (backend contract) and add `roles_display`.
        role_name_by_id = {r["id"]: r["display_name"] for r in all_roles}
        # Pull current lockout state so UI can flag locked users + offer
        # "解鎖" button.
        from ..core import auth_db
        import time as _t
        now = _t.time()
        lock_rows = auth_db.conn().execute(
            "SELECT key, locked_until FROM lockouts WHERE locked_until > ?",
            (now,),
        ).fetchall()
        locked_by_uid: dict[int, float] = {}
        locked_by_username: dict[str, float] = {}
        for r in lock_rows:
            key = r["key"] or ""
            until = r["locked_until"]
            if key.startswith("user:"):
                rest = key.split(":", 2)[1]
                if rest.isdigit():
                    locked_by_uid[int(rest)] = until
                else:
                    locked_by_username[rest] = until
        for u in users:
            u["roles_display"] = [
                {"id": rid, "display_name": role_name_by_id.get(rid, rid)}
                for rid in (u.get("roles") or [])
            ]
            until = (locked_by_uid.get(u["id"])
                     or locked_by_username.get(u["username"]) or 0)
            u["locked"] = bool(until and until > now)
            u["locked_until"] = until or None
        # 「在線」只在啟用認證時才有意義 —— 單機模式沒有帳號概念，
        # 顯示一個永遠是 1 的人數只會誤導。
        auth_on = auth_settings.is_enabled()
        online_ids = _ss.online_user_ids() if auth_on else set()
        for u in users:
            u["online"] = auth_on and u["id"] in online_ids
        return templates.TemplateResponse(request, "admin_users.html", {
            "request": request,
            "users": users,
            "all_roles": all_roles,
            "all_groups": all_groups,
            "auth_on": auth_on,
            "online_count": len(online_ids),
            "online_window_min": _ss.ONLINE_WINDOW_SECONDS // 60,
            "view": view,
            "dir_count": dir_count,
            "missing_count": missing_count,
            "dir_disabled_count": dir_disabled_count,
            "pwd_expiring_count": pwd_expiring_count,
            "pwd_warn_days": user_manager.PWD_WARN_DAYS,
            "actionable": actionable,
            "q": q, "src": src, "state": st,
            "page": page, "pages": pages, "page_total": page_total,
            "page_size": size,
            # 沒做過完整目錄掃描時「目錄已無」判定不成立 —— 畫面要說明原因，
            # 不然管理員只會看到 0 筆而不知道是還沒掃還是真的沒有。
            "has_full_scan": user_manager.last_full_directory_scan_at() > 0,
        })

    @router.post("/users/create")
    async def users_create(request: Request):
        body = await request.json()
        try:
            new_id = user_manager.create_local(
                username=body.get("username", ""),
                display_name=body.get("display_name", ""),
                password=body.get("password", ""),
                enabled=bool(body.get("enabled", True)),
                roles=body.get("roles") or [roles.get_default_role_id()],
            )
        except ValueError as e:
            raise HTTPException(400, str(e))
        audit_db.log_event(
            "user_create", username=_actor(request), ip=_client_ip(request),
            target=body.get("username", ""),
            details={"new_user_id": new_id, "roles": body.get("roles")},
        )
        return {"ok": True, "id": new_id}

    @router.post("/users/{uid}/update")
    async def users_update(uid: int, request: Request):
        body = await request.json()
        try:
            user_manager.update(
                uid,
                display_name=body.get("display_name"),
                enabled=body.get("enabled"),
                roles=body.get("roles"),
                groups=body.get("groups"),
                email=body.get("email"),
            )
        except ValueError as e:
            raise HTTPException(400, str(e))
        audit_db.log_event(
            "user_update", username=_actor(request), ip=_client_ip(request),
            target=str(uid), details={k: v for k, v in body.items() if k != "password"},
        )
        return {"ok": True}

    #: 一次最多處理幾個帳號。批次是為了省事，不是為了讓人一鍵改動整個組織 ——
    #: 上限逼使用者先篩選再操作，也讓稽核記錄看得懂。
    _BULK_MAX = 500

    def _bulk_ids(body: dict) -> list[int]:
        raw = body.get("user_ids")
        if not isinstance(raw, list) or not raw:
            raise HTTPException(400, "沒有選擇任何帳號")
        if len(raw) > _BULK_MAX:
            raise HTTPException(400, f"一次最多 {_BULK_MAX} 個帳號，請先篩選")
        out = []
        for v in raw:
            try:
                out.append(int(v))
            except (TypeError, ValueError):
                raise HTTPException(400, "帳號編號格式錯誤")
        return out

    def _protected_reason(uid: int, me_id, *, disabling: bool) -> str:
        """這個帳號能不能被批次動到。回空字串代表可以。

        三條防線，少一條就可能把自己鎖在門外：
        1. 不能停用自己 —— 按下去當場失去管理權。
        2. 不能動 seed 管理員 —— 那是 break-glass 帳號。
        3. 停用前確認還留得下至少一個啟用中的管理員。
        """
        u = user_manager.get_by_id(uid)
        if not u:
            return "帳號不存在"
        if u.get("is_admin_seed"):
            return "內建管理員帳號不可批次異動"
        if disabling and me_id is not None and int(uid) == int(me_id):
            return "不可停用自己"
        return ""

    def _remaining_admin_count(excluding: set[int]) -> int:
        """扣掉這批之後還剩幾個「啟用中且有 admin 角色」的帳號。"""
        n = 0
        for u in user_manager.list_users("all"):
            if u["id"] in excluding or not u["enabled"]:
                continue
            if "admin" in (u.get("roles") or []) or u.get("is_admin_seed"):
                n += 1
        return n

    @router.post("/users/bulk/enabled")
    async def users_bulk_enabled(request: Request):
        """批次啟用 / 停用。"""
        body = await request.json()
        ids = _bulk_ids(body)
        enabled = bool(body.get("enabled"))
        me = (getattr(request.state, "user", None) or {}).get("user_id")
        done, skipped = [], []
        for uid in ids:
            why = _protected_reason(uid, me, disabling=not enabled)
            if why:
                skipped.append({"id": uid, "reason": why})
                continue
            done.append(uid)
        if not enabled and done:
            # **停用前確認不會把管理員清光**。少了這一條，一次全選停用之後
            # 就沒有人能進管理區了，只能用 CLI 救。
            if _remaining_admin_count(set(done)) == 0:
                raise HTTPException(
                    400, "這樣會停用最後一個管理員 —— 請至少保留一個可登入的管理員")
        for uid in done:
            try:
                user_manager.update(uid, enabled=enabled)
            except ValueError as e:
                skipped.append({"id": uid, "reason": str(e)})
        done = [u for u in done if u not in {s["id"] for s in skipped}]
        audit_db.log_event(
            "user_bulk_update", username=_actor(request), ip=_client_ip(request),
            target=f"{len(done)} users",
            details={"action": "enable" if enabled else "disable",
                     "ids": done[:200], "skipped": skipped[:50]})
        return {"ok": True, "changed": len(done), "skipped": skipped}

    @router.post("/users/bulk/disable-view")
    async def users_bulk_disable_view(request: Request):
        """把某個檢視底下**全部**的帳號停用（不只當前這一頁）。

        「目錄已無」可能有好幾百人、橫跨好幾頁 —— 只能勾當前頁的話，管理員要翻
        十幾次才做得完，實務上就是不會去做。

        安全閥、管理員保護、稽核都在 `directory_cleanup` 裡，與排程自動停用共用
        同一份邏輯（兩邊各寫一份遲早會不一致）。
        """
        from ..core import directory_cleanup
        body = await request.json()
        view = (body.get("view") or "").strip()
        res = directory_cleanup.disable_view(
            view, actor=_actor(request), ip=_client_ip(request),
            force=bool(body.get("force")), dry_run=bool(body.get("dry_run")),
            trigger="manual")
        if res.get("aborted") and not res.get("ok"):
            # 200 + aborted：前端要拿到 reason 顯示給人看（400 只會被當成壞掉）
            return JSONResponse(res, status_code=200)
        return res

    @router.post("/users/bulk/roles")
    async def users_bulk_roles(request: Request):
        """批次指派 / 移除角色。

        `mode`：`add` 疊加、`remove` 移除、`set` 整組取代。預設 `add` ——
        整組取代最危險（會洗掉別人原本的角色），要明確指定才做。
        """
        body = await request.json()
        ids = _bulk_ids(body)
        mode = (body.get("mode") or "add").lower()
        if mode not in ("add", "remove", "set"):
            raise HTTPException(400, "mode 只能是 add / remove / set")
        want = [str(r) for r in (body.get("roles") or [])]
        known = {r["id"] for r in roles.list_roles()}
        bad = [r for r in want if r not in known]
        if bad:
            raise HTTPException(400, f"不存在的角色：{bad}")
        if not want and mode != "set":
            raise HTTPException(400, "沒有選擇角色")
        me = (getattr(request.state, "user", None) or {}).get("user_id")
        changed, skipped = 0, []
        for uid in ids:
            why = _protected_reason(uid, me, disabling=False)
            if why:
                skipped.append({"id": uid, "reason": why})
                continue
            cur = set(permissions.list_roles_for_subject("user", str(uid)))
            if mode == "add":
                new = cur | set(want)
            elif mode == "remove":
                new = cur - set(want)
            else:
                new = set(want)
            if new == cur:
                continue
            permissions.set_subject_roles("user", str(uid), sorted(new))
            changed += 1
        audit_db.log_event(
            "user_bulk_roles", username=_actor(request), ip=_client_ip(request),
            target=f"{changed} users",
            details={"mode": mode, "roles": want, "ids": ids[:200],
                     "skipped": skipped[:50]})
        return {"ok": True, "changed": changed, "skipped": skipped}

    @router.get("/users/{uid}/effective")
    async def users_effective(uid: int, request: Request):
        """這個帳號**最終**能用哪些工具，每一項是從哪一條規則來的。

        權限來源散在四處（直接角色 / 群組含巢狀 / OU / 直接授權），出事時管理員
        原本無法自證也無法排查 —— 只看得到「這個 subject 有哪些角色」，看不到
        加總後的結果。稽核回應與交接文件都要用這個。
        """
        u = user_manager.get_by_id(uid)
        if not u:
            raise HTTPException(404, "帳號不存在")
        exp = permissions.explain_effective_tools(uid)
        # 工具 id → 顯示名稱，讓畫面不用再自己對照一次
        names = {}
        try:
            from ..tool_registry import discover_tools
            names = {t.metadata.id: t.metadata.name for t in discover_tools()}
        except Exception:  # noqa: BLE001 — 取不到名稱不該擋住這個查詢
            pass
        exp["tool_names"] = names
        exp["username"] = u["username"]
        exp["enabled"] = u["enabled"]
        return exp

    @router.get("/users/{uid}/sessions")
    async def users_sessions(uid: int, request: Request):
        """某個帳號目前的登入 session（裝置 / IP / 最後活動）。"""
        from ..core import sessions as _ss
        if not user_manager.get_by_id(uid):
            raise HTTPException(404, "帳號不存在")
        return {"ok": True, "sessions": _ss.list_for_user(uid),
                "window_minutes": _ss.ONLINE_WINDOW_SECONDS // 60}

    @router.post("/users/{uid}/sessions/revoke")
    async def users_sessions_revoke(uid: int, request: Request):
        """踢掉某個帳號的單一或全部 session。

        `sid` 給就踢那一個，不給就全部踢掉。踢自己是允許的（等同登出所有裝置），
        但要記進稽核 —— 強制登出是會被問「誰做的」的動作。
        """
        from ..core import sessions as _ss
        if not user_manager.get_by_id(uid):
            raise HTTPException(404, "帳號不存在")
        body = await request.json()
        sid = (body.get("sid") or "").strip()
        if sid:
            ok = _ss.revoke_one(uid, sid)
            n = 1 if ok else 0
        else:
            n = _ss.revoke_all_for_user(uid)
        audit_db.log_event(
            "session_revoke", username=_actor(request), ip=_client_ip(request),
            target=str(uid), details={"count": n, "sid": sid or "all"})
        return {"ok": True, "revoked": n}

    @router.post("/users/{uid}/reset-password")
    async def users_reset_password(uid: int, request: Request):
        body = await request.json()
        try:
            user_manager.reset_password(uid, body.get("password", ""))
        except ValueError as e:
            raise HTTPException(400, str(e))
        audit_db.log_event(
            "user_pwd_reset", username=_actor(request), ip=_client_ip(request),
            target=str(uid),
        )
        return {"ok": True}

    @router.post("/users/{uid}/delete")
    async def users_delete(uid: int, request: Request):
        try:
            user_manager.delete(uid)
        except ValueError as e:
            raise HTTPException(400, str(e))
        audit_db.log_event(
            "user_delete", username=_actor(request), ip=_client_ip(request),
            target=str(uid),
        )
        return {"ok": True}

    @router.post("/users/{uid}/reset-totp")
    async def users_reset_totp(uid: int, request: Request):
        """Admin 重設使用者的 2FA — 清掉 secret + enabled。下次登入會被導
        去 /2fa-verify 強制 setup（重新顯示 QR）。用情境：使用者手機遺失
        無法產生 6 碼、或 admin 想強制重設。"""
        from ..core import auth_db, db, totp as _totp
        conn = auth_db.conn()
        row = conn.execute("SELECT username FROM users WHERE id=?",
                           (uid,)).fetchone()
        if not row:
            raise HTTPException(404, "使用者不存在")
        _totp.disable(uid)
        # Also revoke active sessions so any cookie they have stops working
        with db.tx(conn):
            conn.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
        audit_db.log_event(
            "user_2fa_reset", username=_actor(request),
            ip=_client_ip(request), target=row["username"],
        )
        return {"ok": True}

    @router.post("/users/{uid}/unlock")
    async def users_unlock(uid: int, request: Request):
        """清除這個 user 的密碼錯誤次數鎖定。Lockouts 表用 user_id 跟 IP
        當 key — 這裡只清 user 的，IP 鎖另外有「清所有 IP 鎖」按鈕。"""
        from ..core import auth_db, db
        conn = auth_db.conn()
        row = conn.execute("SELECT username FROM users WHERE id=?",
                           (uid,)).fetchone()
        if not row:
            raise HTTPException(404, "使用者不存在")
        uname = (row["username"] or "").strip()
        with db.tx(conn):
            # auth_local 實際寫的 key 格式是 `user:<小寫帳號>`（見 auth_local
            # _record_fail / user_key）——先精準清這個，否則解鎖等於沒作用。
            n_user = conn.execute(
                "DELETE FROM lockouts WHERE key=?",
                (f"user:{uname.lower()}",),
            ).rowcount
            # 防禦：清掉可能存在的舊 / 其他格式（uid-based 或帶尾冒號）。
            n_user += conn.execute(
                "DELETE FROM lockouts WHERE key LIKE ? OR key LIKE ? OR key=?",
                (f"user:{uid}:%", f"user:{uname}:%", f"user:{uname}"),
            ).rowcount
        audit_db.log_event(
            "user_unlock", username=_actor(request), ip=_client_ip(request),
            target=str(uid), details={"cleared": n_user},
        )
        return {"ok": True, "cleared": n_user}

    @router.post("/auth-settings/unlock-all")
    async def auth_unlock_all(request: Request):
        """清除所有鎖定（含 IP-based）。緊急用 — 例如多人同時撞密碼鎖死全
        辦公室 IP。"""
        from ..core import auth_db, db
        conn = auth_db.conn()
        with db.tx(conn):
            cur = conn.execute("DELETE FROM lockouts")
            n = cur.rowcount
        audit_db.log_event(
            "lockouts_clear_all", username=_actor(request),
            ip=_client_ip(request), details={"cleared": n},
        )
        return {"ok": True, "cleared": n}

    # ---------- /admin/groups ----------

    _GROUP_TREE_MAX = 300      # above this, switch to flat paginated + search

    @router.get("/groups", response_class=HTMLResponse)
    async def groups_page(request: Request):
        from ..core import auth_settings
        total = group_manager.count_groups()
        q = (request.query_params.get("q") or "").strip()
        size = 100
        try:
            page = max(1, int(request.query_params.get("page") or 1))
        except ValueError:
            page = 1
        # Small directory → full tree (nice). Many groups (or searching) →
        # flat, server-paginated, searchable list — otherwise rendering
        # thousands of group rows OOMs the browser.
        if total <= _GROUP_TREE_MAX and not q:
            groups = group_manager.order_groups_as_tree(group_manager.list_groups())
            paged = False
            page_total = total
            pages = 1
        else:
            data = group_manager.list_groups_page(
                offset=(page - 1) * size, limit=size, q=q)
            groups = data["rows"]
            page_total = data["total"]
            pages = max(1, (page_total + size - 1) // size)
            paged = True
        # Member picker only needs pickable users; use the 'active' view so we
        # don't embed thousands of never-logged-in mirrored directory users.
        # 只送挑選器真正用到的四個欄位。整包 user 記錄會把 created_at /
        # last_login_at / external_dn / password_set 一起序列化進頁面 ——
        # 原始 Unix 時間戳會被掃描器判為資訊洩漏，其餘欄位這個畫面也用不到。
        # 「送出去的資料越少越好」在這裡剛好也讓頁面變小。
        all_users = [
            {"id": u["id"], "username": u["username"],
             "display_name": u["display_name"], "source": u["source"]}
            for u in user_manager.list_users(view="active")
        ]
        all_roles = roles.list_roles()
        backend = (auth_settings.get() or {}).get("backend", "off")
        return templates.TemplateResponse(request, "admin_groups.html", {
            "request": request,
            "groups": groups,
            "all_users": all_users,
            "all_roles": all_roles,
            "auth_backend": backend,
            "is_directory_backend": backend in ("ldap", "ad"),
            "total_groups": total,
            "paged": paged,
            "page": page,
            "pages": pages,
            "page_total": page_total,
            "q": q,
        })

    @router.post("/groups/create")
    async def groups_create(request: Request):
        body = await request.json()
        try:
            gid = group_manager.create_local(
                name=body.get("name", ""),
                description=body.get("description", ""),
            )
        except ValueError as e:
            raise HTTPException(400, str(e))
        audit_db.log_event(
            "group_create", username=_actor(request), ip=_client_ip(request),
            target=body.get("name", ""), details={"new_group_id": gid},
        )
        return {"ok": True, "id": gid}

    @router.post("/groups/sync-ldap")
    async def groups_sync_ldap(request: Request):
        """從 AD / LDAP 目錄列舉**所有**群組,鏡射進本地群組清單（不動成員）。
        解決預設「只看得到曾登入使用者所屬群組」的 JIT 限制 → admin 可預先把
        權限指派給任何目錄群組。"""
        from ..core import auth_settings, auth_ldap
        backend = (auth_settings.get() or {}).get("backend", "off")
        if backend not in ("ldap", "ad"):
            raise HTTPException(400, "目前認證後端不是 LDAP / AD，無法同步目錄群組。")
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        name_contains = str((body or {}).get("name_contains") or "").strip()
        try:
            result = auth_ldap.sync_all_groups(name_contains=name_contains)
        except auth_ldap.AuthError as e:
            raise HTTPException(400, str(e))
        except Exception as e:  # noqa: BLE001
            raise HTTPException(500, f"同步失敗：{type(e).__name__}: {e}")
        audit_db.log_event(
            "group_sync_ldap", username=_actor(request), ip=_client_ip(request),
            details=result,
        )
        return {"ok": True, **result}

    @router.get("/groups/directory-sync/status")
    async def groups_dirsync_status(request: Request):
        """排程目錄同步的目前設定 + 上次結果（給群組管理頁顯示）。"""
        from ..core import directory_sync
        s = directory_sync.get_settings()
        return {
            "enabled": bool(s.get("enabled")),
            "interval_hours": int(s.get("interval_hours", 6)),
            "name_contains": s.get("name_contains", "") or "",
            "sync_users": bool(s.get("sync_users", True)),
            "auto_disable": s.get("auto_disable", "off") or "off",
            "last_auto_disable": s.get("last_auto_disable"),
            "last_run_at": s.get("last_run_at"),
            "last_result": s.get("last_result"),
            "last_error": s.get("last_error"),
            "running": directory_sync.is_running(),
            "is_directory_backend": directory_sync.is_directory_backend(),
        }

    @router.post("/groups/directory-sync/settings")
    async def groups_dirsync_settings(request: Request):
        """存排程目錄同步設定（啟用 / 間隔小時 / 名稱過濾）。"""
        from ..core import directory_sync
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        s = directory_sync.save_settings(
            enabled=bool(body.get("enabled", True)),
            interval_hours=int(body.get("interval_hours", 6) or 6),
            name_contains=str(body.get("name_contains", "") or ""),
            sync_users=bool(body.get("sync_users", True)),
            auto_disable=str(body.get("auto_disable", "off") or "off"),
        )
        audit_db.log_event(
            "settings_change", username=_actor(request), ip=_client_ip(request),
            target="directory_sync",
            details={"enabled": s["enabled"], "interval_hours": s["interval_hours"],
                     "sync_users": s["sync_users"],
                     "auto_disable": s.get("auto_disable", "off")},
        )
        return {"ok": True, "enabled": s["enabled"],
                "interval_hours": s["interval_hours"],
                "name_contains": s["name_contains"],
                "sync_users": s["sync_users"],
                "auto_disable": s.get("auto_disable", "off")}

    @router.post("/groups/directory-sync/run")
    async def groups_dirsync_run(request: Request):
        """手動「立即同步」：在背景執行一次目錄群組同步 + 成員數快取更新，立刻回
        傳（頁面顯示同步中，重新整理即看到新結果）。"""
        import threading
        from ..core import auth_settings, directory_sync
        backend = (auth_settings.get() or {}).get("backend", "off")
        if backend not in ("ldap", "ad"):
            raise HTTPException(400, "目前認證後端不是 LDAP / AD，無法同步目錄。")
        if directory_sync.is_running():
            return {"ok": True, "started": False, "running": True}
        threading.Thread(
            target=directory_sync.run_sync, name="directory-sync-manual",
            daemon=True).start()
        audit_db.log_event(
            "group_sync_ldap", username=_actor(request), ip=_client_ip(request),
            target="directory_sync_manual", details={"trigger": "manual"},
        )
        return {"ok": True, "started": True, "running": True}

    @router.get("/groups/{gid}/members-ldap")
    async def groups_members_ldap(gid: int, request: Request):
        """查某 AD/LDAP 群組在**目錄**裡的直接成員（含尚未登入過本系統的人）。"""
        from ..core import auth_settings, auth_ldap
        backend = (auth_settings.get() or {}).get("backend", "off")
        if backend not in ("ldap", "ad"):
            raise HTTPException(400, "此功能僅適用 LDAP / AD 群組。")
        g = group_manager.get(gid)
        if not g:
            raise HTTPException(404, "群組不存在")
        dn = (g.get("external_dn") or "").strip()
        if not dn:
            raise HTTPException(400, "此群組沒有目錄 DN（可能是本機群組）。")
        try:
            members = auth_ldap.get_group_members(dn)
        except auth_ldap.AuthError as e:
            raise HTTPException(400, str(e))
        except Exception as e:  # noqa: BLE001
            raise HTTPException(500, f"查詢失敗：{type(e).__name__}: {e}")
        # 標註哪些目錄成員「已登入過本系統」= 真正登入過（last_login>0），不是
        # 「本地表有列」（目錄同步會鏡射全部使用者，存在≠登入過）。
        local_dns, local_logins = _logged_in_identity_sets()
        local_count = 0
        for m in members:
            is_local = (
                (m.get("dn", "").strip().lower() in local_dns)
                or (m.get("login", "").strip().lower() in local_logins))
            m["local"] = is_local
            if is_local:
                local_count += 1
        return {"ok": True, "group": g.get("name"), "count": len(members),
                "local_count": local_count,
                "not_local_count": len(members) - local_count,
                "members": members}

    @router.get("/groups/{gid}/member-count")
    async def groups_member_count(gid: int, request: Request):
        """回某 AD/LDAP 群組的目錄成員數（快取 5 分鐘）。群組清單頁載入時非同步
        呼叫,把「成員數」欄從本地登入數更新成目錄實際數。"""
        from ..core import auth_settings, auth_ldap
        if (auth_settings.get() or {}).get("backend", "off") not in ("ldap", "ad"):
            raise HTTPException(400, "僅 LDAP / AD 群組")
        g = group_manager.get(gid)
        if not g or not (g.get("external_dn") or "").strip():
            raise HTTPException(404, "群組不存在或無目錄 DN")
        try:
            n = auth_ldap.count_group_members(g["external_dn"])
        except auth_ldap.AuthError as e:
            raise HTTPException(400, str(e))
        except Exception as e:  # noqa: BLE001
            raise HTTPException(500, f"查詢失敗：{type(e).__name__}: {e}")
        return {"ok": True, "count": n}

    @router.post("/groups/{gid}/update")
    async def groups_update(gid: int, request: Request):
        body = await request.json()
        try:
            group_manager.update(
                gid,
                name=body.get("name"),
                description=body.get("description"),
                roles=body.get("roles"),
            )
            if "members" in body:
                group_manager.set_members(gid, [int(m) for m in body["members"]])
        except ValueError as e:
            raise HTTPException(400, str(e))
        audit_db.log_event(
            "group_update", username=_actor(request), ip=_client_ip(request),
            target=str(gid),
        )
        return {"ok": True}

    @router.post("/groups/{gid}/delete")
    async def groups_delete(gid: int, request: Request):
        try:
            group_manager.delete(gid)
        except ValueError as e:
            raise HTTPException(400, str(e))
        audit_db.log_event(
            "group_delete", username=_actor(request), ip=_client_ip(request),
            target=str(gid),
        )
        return {"ok": True}

    # ---------- /admin/directory (AD/LDAP OU treeview → 指派權限) ----------

    @router.get("/directory", response_class=HTMLResponse)
    async def directory_page(request: Request):
        from ..core import auth_settings, roles as _roles, dir_filter
        backend = (auth_settings.get() or {}).get("backend", "off")
        flt = dir_filter.get_settings()
        return templates.TemplateResponse(request, "admin_directory.html", {
            "request": request,
            "auth_backend": backend,
            "is_directory_backend": backend in ("ldap", "ad"),
            "all_roles": _roles.list_roles(),
            "dir_default_mode": flt["default_mode"],
            "dir_rules": flt["rules"],
        })

    def _require_dir_backend():
        from ..core import auth_settings
        if (auth_settings.get() or {}).get("backend", "off") not in ("ldap", "ad"):
            raise HTTPException(400, "此功能僅適用 LDAP / AD 後端。")

    @router.get("/directory/tree")
    async def directory_tree(request: Request, dn: str = ""):
        """列某節點的直接子 OU / 容器（treeview 逐層展開）。dn 空 = 根。"""
        from ..core import auth_ldap
        _require_dir_backend()
        try:
            nodes = auth_ldap.list_ou_children(dn)
        except auth_ldap.AuthError as e:
            raise HTTPException(400, str(e))
        except Exception as e:  # noqa: BLE001
            raise HTTPException(500, f"查詢失敗：{type(e).__name__}: {e}")
        # 附上每個 OU 目前指派的角色（讓樹上可看到哪些 OU 有權限）
        for n in nodes:
            n["roles"] = permissions.list_roles_for_subject("ou", n["dn"])
        return {"ok": True, "root": (dn or auth_ldap._dir_root_base()), "nodes": nodes}

    @router.get("/directory/users")
    async def directory_users(request: Request, dn: str, recursive: int = 0):
        """列某 OU 下的使用者（標註已登入過本系統者）+ 該 OU 目前的角色。"""
        from ..core import auth_ldap
        _require_dir_backend()
        if not dn:
            raise HTTPException(400, "缺少 OU DN")
        try:
            users = auth_ldap.list_ou_users(dn, recursive=bool(recursive))
        except auth_ldap.AuthError as e:
            raise HTTPException(400, str(e))
        except Exception as e:  # noqa: BLE001
            raise HTTPException(500, f"查詢失敗：{type(e).__name__}: {e}")
        local_dns, local_logins = _logged_in_identity_sets()
        lc = 0
        for m in users:
            m["local"] = (m.get("dn", "").strip().lower() in local_dns
                          or m.get("login", "").strip().lower() in local_logins)
            lc += 1 if m["local"] else 0
        return {"ok": True, "ou": dn, "count": len(users),
                "local_count": lc, "not_local_count": len(users) - lc,
                "ou_roles": permissions.list_roles_for_subject("ou", dn),
                "users": users}

    @router.get("/directory/user")
    async def directory_user_detail(request: Request, dn: str):
        """查單一目錄使用者的完整屬性（點使用者看細節用）+ 是否已登入過本系統。"""
        from ..core import auth_ldap
        _require_dir_backend()
        if not dn:
            raise HTTPException(400, "缺少使用者 DN")
        try:
            detail = auth_ldap.get_user_detail(dn)
        except auth_ldap.AuthError as e:
            raise HTTPException(400, str(e))
        except Exception as e:  # noqa: BLE001
            raise HTTPException(500, f"查詢失敗：{type(e).__name__}: {e}")
        # 是否已登入過本系統 = 真正登入過（last_login>0），非「本地表有列」。
        local_dns, _ = _logged_in_identity_sets()
        detail["local"] = dn.strip().lower() in local_dns
        return {"ok": True, **detail}

    @router.post("/directory/ou-roles")
    async def directory_ou_roles(request: Request):
        """指派角色給某 OU（subject_type=ou）。該 OU 下所有使用者登入時即生效。"""
        _require_dir_backend()
        body = await request.json()
        dn = str((body or {}).get("dn") or "").strip()
        role_ids = list((body or {}).get("roles") or [])
        if not dn:
            raise HTTPException(400, "缺少 OU DN")
        permissions.set_subject_roles("ou", dn, role_ids)
        audit_db.log_event("perm_ou_set", username=_actor(request),
                           ip=_client_ip(request), target=dn,
                           details={"roles": role_ids})
        return {"ok": True, "dn": dn, "roles": role_ids}

    def _mirror_row_for_dn(dn: str, username: str, display_name: str) -> int:
        """找出（必要時建立）這個目錄 DN 對應的本機使用者列，回 users.id。

        建立的是**鏡射列**：`enabled=0`、不給任何角色、不設密碼 —— 與排程同步
        建出來的完全一樣。本人真的登入時 JIT 會把它啟用，而且**已經有角色的人
        不會被塞預設角色**（見 `auth_ldap._sync_user`），所以管理員在這裡先指派
        的東西不會被蓋掉。

        這一段就是「指派權限不必等對方先登入一次」的關鍵。
        """
        from ..core import auth_settings as _as
        backend = (_as.get() or {}).get("backend", "ldap")
        conn = auth_db.conn()
        row = conn.execute(
            "SELECT id FROM users WHERE source IN ('ldap','ad') AND external_dn=?",
            (dn,)).fetchone()
        if row:
            return int(row["id"])
        if not username:
            raise HTTPException(400, "這個目錄物件沒有帳號名稱，無法建立對應帳號")
        # 同名不同 DN：拒絕，不要無聲接管別人的身分（與同步的規則一致）
        clash = conn.execute(
            "SELECT external_dn FROM users WHERE username=? AND source=?",
            (username, backend)).fetchone()
        if clash:
            raise HTTPException(
                409, f"已有另一個 DN 使用帳號名「{username}」（{clash['external_dn']}）"
                     "—— 請先處理同名衝突")
        with db.tx(conn):
            cur = conn.execute(
                "INSERT INTO users(username, display_name, source, external_dn, "
                "enabled, is_admin_seed, created_at) VALUES (?,?,?,?,0,0,?)",
                (username, display_name or username, backend, dn, time.time()))
        return int(cur.lastrowid)

    @router.get("/directory/user-roles")
    async def directory_user_roles_get(request: Request, dn: str = ""):
        """這個目錄使用者目前有哪些角色（還沒鏡射過就是空的）。"""
        _require_dir_backend()
        dn = (dn or "").strip()
        if not dn:
            raise HTTPException(400, "缺少 DN")
        row = auth_db.conn().execute(
            "SELECT id, username, enabled, last_login_at FROM users "
            "WHERE source IN ('ldap','ad') AND external_dn=?", (dn,)).fetchone()
        if not row:
            return {"ok": True, "dn": dn, "mirrored": False, "roles": [],
                    "enabled": False, "logged_in": False}
        return {"ok": True, "dn": dn, "mirrored": True,
                "user_id": row["id"],
                "roles": permissions.list_roles_for_subject("user",
                                                           str(row["id"])),
                "enabled": bool(row["enabled"]),
                "logged_in": bool(row["last_login_at"])}

    @router.post("/directory/user-roles")
    async def directory_user_roles_set(request: Request):
        """指派角色給單一目錄使用者（不必等他先登入過一次）。

        原本只能指派給 OU —— 但「整個 OU 都給財務權限」跟「只有這兩個人是財務」
        是完全不同的事，後者才是實務上最常見的需求，而它以前只能等對方登入之後
        再去使用者管理找人。
        """
        _require_dir_backend()
        body = await request.json()
        dn = str((body or {}).get("dn") or "").strip()
        if not dn:
            raise HTTPException(400, "缺少 DN")
        role_ids = list((body or {}).get("roles") or [])
        uid = _mirror_row_for_dn(dn, str((body or {}).get("username") or "").strip(),
                                 str((body or {}).get("display_name") or "").strip())
        permissions.set_subject_roles("user", str(uid), role_ids)
        audit_db.log_event("perm_user_set", username=_actor(request),
                           ip=_client_ip(request), target=dn,
                           details={"roles": role_ids, "user_id": uid,
                                    "via": "directory"})
        return {"ok": True, "dn": dn, "user_id": uid, "roles": role_ids,
                "mirrored": True}

    @router.get("/directory/group-roles")
    async def directory_group_roles_get(request: Request, dn: str = ""):
        """這個目錄群組目前有哪些角色。"""
        _require_dir_backend()
        dn = (dn or "").strip()
        if not dn:
            raise HTTPException(400, "缺少 DN")
        row = auth_db.conn().execute(
            "SELECT id, name FROM groups WHERE source IN ('ldap','ad') "
            "AND external_dn=?", (dn,)).fetchone()
        if not row:
            return {"ok": True, "dn": dn, "mirrored": False, "roles": []}
        return {"ok": True, "dn": dn, "mirrored": True, "group_id": row["id"],
                "name": row["name"],
                "roles": permissions.list_roles_for_subject("group",
                                                            str(row["id"]))}

    @router.post("/directory/group-roles")
    async def directory_group_roles_set(request: Request):
        """指派角色給目錄群組（在目錄瀏覽裡看到誰屬於哪個群組時直接就能設）。"""
        _require_dir_backend()
        body = await request.json()
        dn = str((body or {}).get("dn") or "").strip()
        if not dn:
            raise HTTPException(400, "缺少 DN")
        role_ids = list((body or {}).get("roles") or [])
        conn = auth_db.conn()
        row = conn.execute(
            "SELECT id FROM groups WHERE source IN ('ldap','ad') AND external_dn=?",
            (dn,)).fetchone()
        if row:
            gid = int(row["id"])
        else:
            # 還沒同步到的群組：用 DN 的第一段當名稱先鏡射一列。
            from ..core import auth_settings as _as
            backend = (_as.get() or {}).get("backend", "ldap")
            name = str((body or {}).get("name") or "").strip()
            if not name:
                head = dn.split(",")[0]
                name = head.split("=", 1)[1] if "=" in head else head
            with db.tx(conn):
                cur = conn.execute(
                    "INSERT INTO groups(name, source, external_dn, created_at) "
                    "VALUES (?,?,?,?)", (name, backend, dn, time.time()))
            gid = int(cur.lastrowid)
        permissions.set_subject_roles("group", str(gid), role_ids)
        audit_db.log_event("perm_group_set", username=_actor(request),
                           ip=_client_ip(request), target=dn,
                           details={"roles": role_ids, "group_id": gid,
                                    "via": "directory"})
        return {"ok": True, "dn": dn, "group_id": gid, "roles": role_ids,
                "mirrored": True}

    # ----- 已選定 filter（全域一份，admin 共用）+ 剪枝樹 -----

    @router.get("/directory/filter")
    async def directory_filter_get(request: Request):
        from ..core import dir_filter
        return {"ok": True, **dir_filter.get_settings()}

    @router.post("/directory/filter")
    async def directory_filter_save(request: Request):
        from ..core import dir_filter
        body = await request.json()
        data = dir_filter.save_settings(
            default_mode=(body or {}).get("default_mode"),
            rules=(body or {}).get("rules"),
        )
        audit_db.log_event("dir_filter_set", username=_actor(request),
                           ip=_client_ip(request), target="directory",
                           details={"default_mode": data["default_mode"],
                                    "rule_count": len(data["rules"])})
        return {"ok": True, **data}

    @router.get("/directory/selected")
    async def directory_selected(request: Request):
        """依 filter 規則回「剪枝樹」（只留通往符合物件的分支）+ 統計。"""
        from ..core import auth_ldap, dir_filter
        _require_dir_backend()
        settings = dir_filter.get_settings()
        rules = settings.get("rules") or []
        if not rules:
            return {"ok": True, "tree": [], "count": 0, "capped": False,
                    "matched_dns": [], "empty_rules": True}
        try:
            res = auth_ldap.search_selected_objects(rules)
        except auth_ldap.AuthError as e:
            raise HTTPException(400, str(e))
        except Exception as e:  # noqa: BLE001
            raise HTTPException(500, f"查詢失敗：{type(e).__name__}: {e}")
        tree = dir_filter.prune_tree(res["objects"], auth_ldap._dir_root_base())
        # 附上符合的 OU 目前角色（樹上可看到哪些選定 OU 有權限）
        matched = [o for o in res["objects"] if o["type"] == "ou"]

        def _annotate(node):
            if node.get("matched") and node.get("type") == "ou":
                node["roles"] = permissions.list_roles_for_subject("ou", node["dn"])
            for c in node.get("children", []):
                _annotate(c)
        for n in tree:
            _annotate(n)
        return {"ok": True, "tree": tree, "count": res["count"],
                "capped": res["capped"],
                "matched_dns": [o["dn"] for o in res["objects"]],
                "matched_ou_count": len(matched)}

    # ---------- /admin/roles ----------

    @router.get("/roles", response_class=HTMLResponse)
    async def roles_page(request: Request):
        all_roles = roles.list_roles()
        # tool registry: id + display name
        tools_meta = [{"id": tid, "name": _tool_name(tid)} for tid in _all_tool_ids()]
        return templates.TemplateResponse(request, "admin_roles.html", {
            "request": request,
            "roles": all_roles,
            "tools": tools_meta,
        })

    @router.post("/roles/create")
    async def roles_create(request: Request):
        body = await request.json()
        try:
            roles.create(
                role_id=body.get("id", ""),
                display_name=body.get("display_name", ""),
                description=body.get("description", ""),
                tools=body.get("tools") or [],
            )
        except ValueError as e:
            raise HTTPException(400, str(e))
        audit_db.log_event(
            "role_create", username=_actor(request), ip=_client_ip(request),
            target=body.get("id", ""),
        )
        return {"ok": True}

    @router.post("/roles/{role_id}/update")
    async def roles_update(role_id: str, request: Request):
        body = await request.json()
        try:
            roles.update(
                role_id,
                display_name=body.get("display_name"),
                description=body.get("description"),
                tools=body.get("tools"),
            )
        except ValueError as e:
            raise HTTPException(400, str(e))
        permissions.invalidate_cache()
        audit_db.log_event(
            "role_update", username=_actor(request), ip=_client_ip(request),
            target=role_id,
        )
        return {"ok": True}

    @router.post("/roles/{role_id}/delete")
    async def roles_delete(role_id: str, request: Request):
        try:
            roles.delete(role_id)
        except ValueError as e:
            raise HTTPException(400, str(e))
        permissions.invalidate_cache()
        audit_db.log_event(
            "role_delete", username=_actor(request), ip=_client_ip(request),
            target=role_id,
        )
        return {"ok": True}

    @router.post("/roles/{role_id}/set-default")
    async def roles_set_default(role_id: str, request: Request):
        """Mark this role as the default assigned to brand-new users who
        aren't given an explicit role (LDAP/AD/SSO JIT provisioning, admin
        create-user without a role selection). Rejects admin / auditor."""
        try:
            roles.set_default_role_id(role_id)
        except ValueError as e:
            raise HTTPException(400, str(e))
        audit_db.log_event(
            "role_set_default_for_new", username=_actor(request),
            ip=_client_ip(request), target=role_id,
        )
        return {"ok": True}

    # ---------- /admin/permissions (matrix) ----------

    @router.get("/permissions", response_class=HTMLResponse)
    async def permissions_page(request: Request):
        users = user_manager.list_users()
        groups = group_manager.list_groups()
        all_roles = roles.list_roles()
        # Subjects shown in matrix: users + groups (OUs only when LDAP/AD active
        # and admin has set per-OU rules — TBD via M3).
        subjects = []
        for u in users:
            subjects.append({
                "type": "user", "key": str(u["id"]),
                "label": f"{u['display_name']} ({u['username']})",
                "name": u["display_name"], "username": u["username"],
                "source": u["source"],
                "is_admin_seed": u.get("is_admin_seed", False),
                "is_audit_seed": u.get("is_audit_seed", False),
                "roles": permissions.list_roles_for_subject("user", str(u["id"])),
                "direct_tools": permissions.list_direct_tools_for_subject("user", str(u["id"])),
            })
        for g in groups:
            subjects.append({
                "type": "group", "key": str(g["id"]),
                "label": f"群組：{g['name']}",
                "name": g["name"], "username": "",
                "source": g["source"], "is_admin_seed": False,
                "is_audit_seed": False,
                "roles": permissions.list_roles_for_subject("group", str(g["id"])),
                "direct_tools": permissions.list_direct_tools_for_subject("group", str(g["id"])),
            })
        tools_meta = [{"id": tid, "name": _tool_name(tid)} for tid in _all_tool_ids()]
        return templates.TemplateResponse(request, "admin_permissions.html", {
            "request": request,
            "subjects": subjects,
            "all_roles": all_roles,
            "tools": tools_meta,
        })

    @router.post("/permissions/set")
    async def permissions_set(request: Request):
        body = await request.json()
        st = body.get("subject_type")
        sk = body.get("subject_key")
        if st not in ("user", "group", "ou") or not sk:
            raise HTTPException(400, "subject_type / subject_key required")
        # Built-in seed users (jtdt-admin / jtdt-auditor) 角色與工具是固定的，
        # 不可改 — 拒絕。
        if st == "user":
            from ..core import auth_db
            row = auth_db.conn().execute(
                "SELECT username, is_admin_seed, is_audit_seed FROM users WHERE id=?",
                (int(sk),),
            ).fetchone()
            if row and (row["is_admin_seed"] or row["is_audit_seed"]):
                raise HTTPException(
                    400,
                    f"內建帳號（{row['username']}）的角色與工具權限固定，"
                    "不可從權限矩陣修改。")
        try:
            if "roles" in body:
                permissions.set_subject_roles(st, str(sk), body["roles"])
            if "direct_tools" in body:
                # Replace direct grants
                from ..core import auth_db, db as _db
                conn = auth_db.conn()
                with _db.tx(conn):
                    conn.execute(
                        "DELETE FROM subject_perms WHERE subject_type=? AND subject_key=?",
                        (st, str(sk)),
                    )
                    for t in body["direct_tools"]:
                        conn.execute(
                            "INSERT OR IGNORE INTO subject_perms(subject_type, subject_key, tool_id) "
                            "VALUES (?,?,?)", (st, str(sk), t),
                        )
                permissions.invalidate_cache()
        except ValueError as e:
            raise HTTPException(400, str(e))
        audit_db.log_event(
            "perm_change", username=_actor(request), ip=_client_ip(request),
            target=f"{st}:{sk}",
            details={k: body.get(k) for k in ("roles", "direct_tools") if k in body},
        )
        return {"ok": True}

    # ---------- /admin/audit ----------

    @router.get("/audit", response_class=HTMLResponse)
    async def audit_page(request: Request,
                         q_user: str = "", q_event: str = "",
                         q_from: str = "", q_to: str = "",
                         page: int = 1, page_size: int = 100):
        page = max(1, page)
        page_size = min(500, max(10, page_size))
        offset = (page - 1) * page_size
        # Build SQL conditions
        conds, params = [], []
        if q_user:
            conds.append("username LIKE ?")
            params.append(f"%{q_user}%")
        if q_event:
            conds.append("event_type = ?")
            params.append(q_event)
        if q_from:
            try:
                import datetime as _dt
                ts_from = _dt.datetime.fromisoformat(q_from).timestamp()
                conds.append("ts >= ?"); params.append(ts_from)
            except ValueError:
                pass
        if q_to:
            try:
                import datetime as _dt
                ts_to = _dt.datetime.fromisoformat(q_to).timestamp()
                conds.append("ts <= ?"); params.append(ts_to)
            except ValueError:
                pass
        where = " WHERE " + " AND ".join(conds) if conds else ""

        c = audit_db.conn()
        total = c.execute(f"SELECT count(*) FROM audit_events{where}",
                          tuple(params)).fetchone()[0]
        rows = c.execute(
            f"SELECT id, ts, username, ip, event_type, target, details_json "
            f"FROM audit_events{where} ORDER BY id DESC LIMIT ? OFFSET ?",
            tuple(params) + (page_size, offset),
        ).fetchall()
        events = [dict(r) for r in rows]
        # Distinct values for filter dropdowns
        distinct_events = [r[0] for r in c.execute(
            "SELECT DISTINCT event_type FROM audit_events ORDER BY event_type"
        ).fetchall()]
        distinct_users = [r[0] for r in c.execute(
            "SELECT DISTINCT username FROM audit_events WHERE username != '' "
            "ORDER BY username"
        ).fetchall()]
        # File size for the warning banner.
        from . import router as _admin_router_mod  # noqa
        from ..core import db as _db
        size_bytes = _db.db_size_bytes(audit_db.audit_db_path())
        return templates.TemplateResponse(request, "admin_audit.html", {
            "request": request,
            "events": events,
            "total": total,
            "page": page, "page_size": page_size,
            "pages": (total + page_size - 1) // page_size,
            "q_user": q_user, "q_event": q_event,
            "q_from": q_from, "q_to": q_to,
            "distinct_events": distinct_events,
            "distinct_users": distinct_users,
            "size_mb": size_bytes / 1024 / 1024,
            "size_warn": size_bytes > 5 * 1024 * 1024 * 1024,
        })

    @router.get("/audit/export.csv")
    async def audit_export_csv(request: Request,
                               q_user: str = "", q_event: str = "",
                               q_from: str = "", q_to: str = ""):
        import csv as _csv
        import io as _io
        from datetime import datetime as _dt
        from fastapi.responses import StreamingResponse
        conds, params = [], []
        if q_user:
            conds.append("username = ?"); params.append(q_user)
        if q_event:
            conds.append("event_type = ?"); params.append(q_event)
        if q_from:
            try:
                conds.append("ts >= ?"); params.append(_dt.fromisoformat(q_from).timestamp())
            except ValueError:
                pass
        if q_to:
            try:
                conds.append("ts <= ?"); params.append(_dt.fromisoformat(q_to).timestamp())
            except ValueError:
                pass
        where = " WHERE " + " AND ".join(conds) if conds else ""
        rows = audit_db.conn().execute(
            f"SELECT id, ts, username, ip, event_type, target, details_json "
            f"FROM audit_events{where} ORDER BY id DESC", tuple(params)
        ).fetchall()

        from ..core import csv_safe as _csv_safe
        buf = _io.StringIO()
        # UTF-8 BOM so Excel opens it as UTF-8 by default
        buf.write("﻿")
        w = _csv.writer(buf)
        w.writerow(["id", "time", "user", "ip", "event_type", "target", "details"])
        for r in rows:
            t = _dt.fromtimestamp(r["ts"]).isoformat(sep=" ", timespec="seconds")
            # target / details 裡有使用者取的檔名 —— 而這份 CSV 的開檔者是
            # 管理員。不中和等於把公式送進管理員的 Excel（見 core/csv_safe）。
            w.writerow(_csv_safe.row(
                [r["id"], t, r["username"], r["ip"],
                 r["event_type"], r["target"], r["details_json"]]))
        from ..core.http_utils import content_disposition
        return StreamingResponse(
            iter([buf.getvalue()]),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition":
                     content_disposition(f"audit_{_dt.now():%Y%m%d_%H%M%S}.csv")},
        )

    # ---------- /admin/uploads (file-upload activity) ----------

    @router.get("/uploads", response_class=HTMLResponse)
    async def uploads_page(request: Request,
                           q_user: str = "", q_tool: str = "",
                           q_filename: str = "",
                           q_from: str = "", q_to: str = "",
                           page: int = 1, page_size: int = 50):
        """Uploads activity log — derived from `audit_events` rows where
        event_type='tool_invoke' AND details_json contains a `filename`
        (filled in by the upload-filename middleware in main.py).
        """
        import json as _json
        page = max(1, page)
        page_size = min(500, max(10, page_size))
        offset = (page - 1) * page_size
        conds = ["event_type = 'tool_invoke'", "details_json LIKE '%\"filename\"%'"]
        params: list = []
        if q_user:
            conds.append("username LIKE ?")
            params.append(f"%{q_user}%")
        if q_tool:
            conds.append("target = ?")
            params.append(q_tool)
        if q_filename:
            # Crude substring match on the JSON blob — fine since filename
            # appears as `"filename": "X"` within details_json.
            conds.append("details_json LIKE ?")
            params.append(f"%{q_filename}%")
        if q_from:
            try:
                import datetime as _dt
                conds.append("ts >= ?"); params.append(_dt.datetime.fromisoformat(q_from).timestamp())
            except ValueError:
                pass
        if q_to:
            try:
                import datetime as _dt
                conds.append("ts <= ?"); params.append(_dt.datetime.fromisoformat(q_to).timestamp())
            except ValueError:
                pass
        where = " WHERE " + " AND ".join(conds)

        c = audit_db.conn()
        total = c.execute(f"SELECT count(*) FROM audit_events{where}",
                          tuple(params)).fetchone()[0]
        rows = c.execute(
            f"SELECT id, ts, username, ip, target, details_json "
            f"FROM audit_events{where} ORDER BY id DESC LIMIT ? OFFSET ?",
            tuple(params) + (page_size, offset),
        ).fetchall()
        uploads = []
        total_bytes = 0
        for r in rows:
            try:
                d = _json.loads(r["details_json"] or "{}")
            except Exception:
                d = {}
            sz = int(d.get("size_bytes") or 0)
            total_bytes += sz
            uploads.append({
                "id": r["id"],
                "ts": r["ts"],
                "username": r["username"] or "(匿名)",
                "ip": r["ip"],
                "tool_id": r["target"],
                "filename": d.get("filename", ""),
                "filenames": d.get("filenames"),
                "file_count": d.get("count") or (1 if d.get("filename") else 0),
                "action": d.get("action", ""),
                "size_bytes": sz,
                "status": d.get("status", 0),
            })
        # Distinct dropdowns
        distinct_users = [r[0] for r in c.execute(
            "SELECT DISTINCT username FROM audit_events "
            "WHERE event_type='tool_invoke' AND username != '' "
            "AND details_json LIKE '%\"filename\"%' ORDER BY username"
        ).fetchall()]
        distinct_tools = [r[0] for r in c.execute(
            "SELECT DISTINCT target FROM audit_events "
            "WHERE event_type='tool_invoke' AND target != '' "
            "AND details_json LIKE '%\"filename\"%' ORDER BY target"
        ).fetchall()]
        return templates.TemplateResponse(request, "admin_uploads.html", {
            "request": request,
            "uploads": uploads,
            "total": total,
            "total_bytes": total_bytes,
            "page": page, "page_size": page_size,
            "pages": (total + page_size - 1) // page_size,
            "q_user": q_user, "q_tool": q_tool, "q_filename": q_filename,
            "q_from": q_from, "q_to": q_to,
            "distinct_users": distinct_users,
            "distinct_tools": distinct_tools,
        })

    # ---------- /admin/log-forward ----------

    @router.get("/log-forward", response_class=HTMLResponse)
    async def log_forward_page(request: Request):
        cfg = audit_forward.get()
        return templates.TemplateResponse(request, "admin_log_forward.html", {
            "request": request,
            "destinations": cfg.get("destinations", []),
        })

    @router.post("/log-forward/save")
    async def log_forward_save(request: Request):
        body = await request.json()
        dests = body.get("destinations") or []
        # Validate
        cleaned = []
        import uuid as _uu
        for d in dests:
            if not isinstance(d, dict):
                continue
            fmt = d.get("format")
            if fmt not in ("syslog", "cef", "gelf"):
                raise HTTPException(400, f"unsupported format: {fmt}")
            transport = d.get("transport", "udp")
            if transport not in ("udp", "tcp"):
                raise HTTPException(400, f"unsupported transport: {transport}")
            host = (d.get("host") or "").strip()
            if not host:
                raise HTTPException(400, "host required")
            try:
                port = int(d.get("port", 514))
            except ValueError:
                raise HTTPException(400, "port must be int")
            if port < 1 or port > 65535:
                raise HTTPException(400, "port out of range")
            cleaned.append({
                "id": d.get("id") or _uu.uuid4().hex[:12],
                "name": (d.get("name") or "")[:80] or f"{fmt}://{host}:{port}",
                "format": fmt,
                "transport": transport,
                "host": host,
                "port": port,
                "enabled": bool(d.get("enabled", True)),
            })
        audit_forward.save({"destinations": cleaned})
        audit_db.log_event(
            "settings_change", username=_actor(request), ip=_client_ip(request),
            target="log_forward", details={"destination_count": len(cleaned)},
        )
        # Make sure worker is running
        audit_forward.start_worker()
        return {"ok": True, "count": len(cleaned)}

    # ---------- /admin/history (fill / stamp / watermark) ----------

    @router.get("/history", response_class=HTMLResponse)
    async def history_redirect(request: Request):
        return RedirectResponse("/admin/history/fill", status_code=302)

    @router.get("/history/{kind}", response_class=HTMLResponse)
    async def history_page(kind: str, request: Request,
                           q_user: str = ""):
        from ..core.history_manager import (history_manager, stamp_history,
                                              watermark_history)
        managers = {"fill": (history_manager, "表單填寫", "/tools/pdf-fill"),
                    "stamp": (stamp_history, "用印與簽名", "/tools/pdf-stamp"),
                    "watermark": (watermark_history, "浮水印", "/tools/pdf-watermark")}
        if kind not in managers:
            raise HTTPException(404)
        mgr, title, tool_url = managers[kind]
        entries = mgr.list_all()
        if q_user:
            entries = [e for e in entries if (e.get("username") or "") == q_user]
        users = sorted({e.get("username") or "(匿名)" for e in mgr.list_all()})
        return templates.TemplateResponse(request, "admin_history.html", {
            "request": request,
            "kind": kind, "title": title, "tool_url": tool_url,
            "entries": entries, "users": users, "q_user": q_user,
        })

    @router.get("/history/{kind}/{hid}/file/{which}")
    async def history_file(kind: str, hid: str, which: str):
        from ..core.history_manager import (history_manager, stamp_history,
                                              watermark_history)
        from fastapi.responses import FileResponse
        mgr_map = {"fill": history_manager, "stamp": stamp_history,
                   "watermark": watermark_history}
        mgr = mgr_map.get(kind)
        if not mgr:
            raise HTTPException(404)
        p = mgr.file(hid, which)
        if not p:
            raise HTTPException(404)
        media = "image/png" if which == "preview" else "application/pdf"
        return FileResponse(str(p), media_type=media, filename=p.name)

    @router.post("/history/{kind}/{hid}/delete")
    async def history_delete(kind: str, hid: str, request: Request):
        from ..core.history_manager import (history_manager, stamp_history,
                                              watermark_history)
        mgr_map = {"fill": history_manager, "stamp": stamp_history,
                   "watermark": watermark_history}
        mgr = mgr_map.get(kind)
        if not mgr:
            raise HTTPException(404)
        ok = mgr.delete(hid)
        if not ok:
            raise HTTPException(404)
        audit_db.log_event(
            "history_delete", username=_actor(request), ip=_client_ip(request),
            target=f"{kind}:{hid}",
        )
        return {"ok": True}

    # ---------- /admin/retention ----------

    @router.get("/retention", response_class=HTMLResponse)
    async def retention_page(request: Request):
        from ..core import retention as _ret
        return templates.TemplateResponse(request, "admin_retention.html", {
            "request": request,
            "settings": _ret.get(),
            "stats": _ret.collect_stats(),
        })

    @router.post("/retention/save")
    async def retention_save(request: Request):
        from ..core import retention as _ret
        body = await request.json()
        try:
            _ret.save(body)
        except ValueError as e:
            raise HTTPException(400, str(e))
        audit_db.log_event(
            "settings_change", username=_actor(request), ip=_client_ip(request),
            target="retention", details=body,
        )
        return {"ok": True}

    @router.post("/retention/sweep-now")
    async def retention_sweep_now(request: Request):
        from ..core import retention as _ret
        report = _ret.sweep_all()
        audit_db.log_event(
            "retention_sweep", username=_actor(request), ip=_client_ip(request),
            details=report,
        )
        return {"ok": True, "report": report}

    # ---------- /admin/notify（作業完成通知）----------

    @router.get("/notify", response_class=HTMLResponse)
    async def notify_page(request: Request):
        from ..core import notify_channels as _nc, notify_settings as _ns
        return templates.TemplateResponse(request, "admin_notify.html", {
            "request": request,
            "cfg": _ns.get(),                       # 祕密已遮罩
            "info": _nc.CHANNEL_INFO,
            "channels": _nc.ALL_CHANNELS,
            "personal": _ns.PERSONAL_CHANNELS,
            "dual": _ns.DUAL_CHANNELS,
            # 哪些管道還沒有對真實服務驗證過（見 notify_channels.DEV_CHANNELS）
            "dev_channels": _nc.DEV_CHANNELS,
            "secret_kept": _ns.SECRET_KEPT,
        })

    @router.post("/notify/save")
    async def notify_save(request: Request):
        from ..core import notify_settings as _ns
        body = await request.json()
        cfg = _ns.save(body)
        # 稽核只記「改了哪些管道」，**不記憑證內容**
        audit_db.log_event(
            "settings_change", username=_actor(request), ip=_client_ip(request),
            target="notify",
            details={"enabled": cfg.get("enabled"),
                     "min_seconds": cfg.get("min_seconds"),
                     "channels": sorted(
                         k for k, v in (cfg.get("channels") or {}).items()
                         if v.get("enabled"))},
        )
        return {"ok": True, "cfg": cfg}

    @router.post("/notify/test/{channel}")
    async def notify_test(request: Request, channel: str):
        """送一則測試訊息。失敗要把原因回給管理員 —— 「失敗」兩個字沒有用。"""
        import asyncio as _asyncio

        from ..core import notify_channels as _nc, notify_settings as _ns
        if channel not in _nc.ALL_CHANNELS:
            raise HTTPException(400, "未知的通知管道")
        body = await request.json() if await request.body() else {}
        cfg = _ns.get(reveal=True)["channels"].get(channel) or {}
        # 個人管道要有目的地才測得動 —— 用管理員自己填的測試位址
        for f in ("email_to", "telegram_chat_id", "line_to",
                  "zulip_to", "nextcloud_to"):
            if body.get(f):
                cfg[f] = body[f]
        try:
            await _asyncio.to_thread(
                _nc.send_one, cfg, channel,
                "[測試] Jason Tools 文件工具箱",
                "這是一則測試通知，看到就代表這個管道設定正確。")
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"{type(e).__name__}: {e}"[:300]}
        audit_db.log_event(
            "notify_test", username=_actor(request), ip=_client_ip(request),
            target=channel,
        )
        return {"ok": True}

    # ---------- /admin/jobs（工作監控 + 併行度）----------

    @router.get("/jobs", response_class=HTMLResponse)
    async def jobs_page(request: Request):
        from ..core import concurrency_settings as _cs
        return templates.TemplateResponse(request, "admin_jobs.html", {
            "request": request,
            "conc": _cs.describe(),
        })

    @router.get("/jobs/api/list")
    async def jobs_api_list(request: Request, active: bool = False,
                            limit: int = 100, offset: int = 0):
        """所有使用者的工作（管理視角）。"""
        from ..core import concurrency_settings as _cs, job_store, office_convert
        from ..core.job_manager import job_manager
        from ..tool_registry import discover_tools
        names = {t.metadata.id: t.metadata.name for t in discover_tools()}
        rows = job_store.list_jobs(active_only=bool(active), limit=limit,
                                   offset=offset)
        qpos = job_manager.queue_positions()
        usage = job_manager.resource_usage()
        # 疊上即時狀態（進度不寫 DB，只讀 DB 的話進度條永遠是 0）
        live = job_manager.live_snapshot()
        # 送出者目前**是不是**優先派送名單裡的人（以及排第幾）。
        #
        # 這跟作業自己的 `priority` 不一樣：作業上那個是**送出當下**的狀態，
        # 用來決定它在佇列裡的位置；這裡是**現在**的名單，用來在清單上標出
        # 「這個人是優先使用者」。兩者刻意分開 —— 名單改了不該讓歷史作業
        # 的排隊結果看起來變了。
        from ..core import job_priority as _jp
        prio_rank = {uid: i for i, uid in enumerate(_jp.get_ordered())}
        out = []
        for r in rows:
            meta = r.get("meta") or {}
            lv = live.get(r["id"]) or {}
            out.append({
                "id": r["id"],
                "tool_id": r["tool_id"],
                "tool_name": names.get(r["tool_id"], r["tool_id"]),
                "status": lv.get("status", r["status"]),
                "progress": lv.get("progress", r["progress"]),
                "message": lv.get("message") or r["message"],
                "error": r["error"],
                "filename": (meta.get("filename") or r["result_filename"]
                             or (f"{meta['count']} 個檔案"
                                 if isinstance(meta.get("count"), int) else "")),
                "owner": r["owner_label"] or "", "client_ip": r["client_ip"] or "",
                # 送出者目前在優先派送名單裡的排名（0 起算），不在名單裡為 None
                "owner_priority": prio_rank.get(r.get("owner_id")),
                "created_at": r["created_at"], "updated_at": r["updated_at"],
                "elapsed": round(max(0.0, (r["finished_at"] or time.time())
                                     - r["created_at"]), 1),
                "is_office": r["tool_id"] in _cs.OFFICE_TOOL_IDS,
                # 這個作業會跟別人搶哪些共用資源（Office / OCR / 外部服務）——
                # 「為什麼排這麼久」的答案通常就在這裡
                "resources": _cs.resource_tags(r["tool_id"]),
                # 優先派送（管理員指定的名單）
                "priority": bool(live.get(r["id"], {}).get("priority")),
                # 排隊順序取自實際的派送佇列，不是拿時間去猜
                "queue_pos": qpos.get(r["id"]),
                # 實測的子行程用量（soffice 才是真正吃記憶體的那個）；
                # 量不到就給 None，前端顯示估計值並標示為估計，不混為一談
                "usage": usage.get(r["id"]),
                "est_mb": _cs.estimated_job_mb(r["tool_id"]),
            })
        # 排隊中的排最前面且**照派送順序**（其餘維持新到舊）—— 管理員最關心的
        # 是「接下來會跑誰」，用建立時間倒序會把佇列頭尾顛倒過來。
        out.sort(key=lambda j: (
            0 if j["queue_pos"] else (1 if j["status"] == "running" else 2),
            j["queue_pos"] or 0,
            -j["created_at"]))
        return {
            "jobs": out,
            "total": job_store.count_jobs(),
            "runtime": job_manager.stats(),
            "office": office_convert.office_concurrency(),
            "memory": {"total_mb": _cs.total_mb(),
                       "available_mb": _cs.available_mb(),
                       "reserve_mb": _cs.reserve_mb()},
            "cpu": _cs.cpu_snapshot(),
        }

    @router.get("/jobs/api/history")
    async def jobs_api_history(request: Request, hours: int = 24,
                               buckets: int = 96):
        """作業量與資源使用率的歷史 —— 給「目前狀態」那幾張卡片點開看圖表用。

        作業量從 jobs 表的起訖時間推導（精確、不需取樣）；CPU / 記憶體來自每分鐘
        的取樣。
        """
        import asyncio as _asyncio

        from ..core import job_store
        jobs, res = await _asyncio.gather(
            _asyncio.to_thread(job_store.history, hours, buckets),
            _asyncio.to_thread(job_store.metrics_history, hours, buckets),
        )
        return {"hours": hours, "jobs": jobs, "resources": res}

    @router.post("/jobs/api/cancel/{job_id}")
    async def jobs_api_cancel(request: Request, job_id: str):
        from ..core.job_manager import job_manager
        from ..core.safe_paths import require_uuid_hex
        require_uuid_hex(job_id, "job_id")
        job = job_manager.get(job_id)
        ok = job_manager.cancel(job_id) if job else False
        audit_db.log_event(
            "job_cancel", username=_actor(request), ip=_client_ip(request),
            target=job_id, details={"tool_id": getattr(job, "tool_id", ""),
                                    "owner": getattr(job, "owner_label", ""),
                                    "ok": ok},
        )
        return {"ok": ok}

    @router.post("/jobs/api/pause")
    async def jobs_api_pause(request: Request):
        """暫停 / 恢復派送。只影響尚未開始的工作 —— 執行中的 soffice 是獨立子
        行程，凍結不了（UI 已照實說明）。"""
        from ..core.job_manager import job_manager
        body = await request.json()
        paused = job_manager.set_paused(bool(body.get("paused")))
        audit_db.log_event(
            "settings_change", username=_actor(request), ip=_client_ip(request),
            target="job_dispatch", details={"paused": paused},
        )
        return {"ok": True, "paused": paused}

    @router.post("/jobs/api/concurrency")
    async def jobs_api_concurrency(request: Request):
        from ..core import concurrency_settings as _cs
        body = await request.json()
        cfg = _cs.save(body)          # save() 內部已把數值夾在安全範圍
        audit_db.log_event(
            "settings_change", username=_actor(request), ip=_client_ip(request),
            target="concurrency", details=cfg,
        )
        return {"ok": True, "conc": _cs.describe()}

    @router.get("/jobs/api/priority-users")
    async def jobs_api_priority_users(request: Request):
        """優先派送名單（含顯示用的帳號資訊）。

        回傳時把已經不存在的使用者濾掉 —— 帳號刪掉之後名單裡留著一個孤兒編號，
        畫面上會是一列空白，管理員也認不出那是誰。
        """
        from ..core import job_priority, user_manager
        # **照名單本身的順序**，不要 sorted() —— 順序就是優先順序，
        # 依編號排會把管理員拖好的順序整個洗掉（實測踩到）。
        users = []
        for uid in job_priority.get_ordered():
            u = user_manager.get_by_id(uid)
            if not u:
                continue
            users.append({"id": uid, "username": u.get("username", ""),
                          "display_name": u.get("display_name", ""),
                          "source": u.get("source", "")})
        return {"ok": True, "users": users, "max": job_priority.MAX_USERS,
                "auth_enabled": auth_settings.is_enabled()}

    @router.get("/jobs/api/user-search")
    async def jobs_api_user_search(request: Request, q: str = ""):
        """給優先派送名單挑人用的帳號搜尋。

        只回顯示需要的欄位（編號 / 帳號 / 姓名 / 來源），**不回信箱或任何憑證**
        —— 這個端點的用途只是挑人。空字串也回前幾筆，管理員不必先知道要打什麼。
        """
        from ..core import user_manager
        needle = (q or "").strip().lower()
        out = []
        for u in user_manager.list_users("all"):
            hay = f"{u.get('username', '')} {u.get('display_name', '')}".lower()
            if needle and needle not in hay:
                continue
            out.append({"id": u.get("id"), "username": u.get("username", ""),
                        "display_name": u.get("display_name", ""),
                        "source": u.get("source", "")})
            if len(out) >= 20:
                break
        return {"ok": True, "users": out}

    @router.post("/jobs/api/priority-users")
    async def jobs_api_priority_users_save(request: Request):
        """覆寫優先派送名單。

        只收使用者編號，而且**逐一確認帳號存在**才存 —— 前端送一串亂數進來時
        不該讓它們落進設定檔（之後查不到人，也看不出是誰）。
        """
        from ..core import job_priority, user_manager
        body = await request.json()
        raw = body.get("user_ids")
        if not isinstance(raw, list):
            raise HTTPException(400, "user_ids 必須是陣列")
        if len(raw) > job_priority.MAX_USERS:
            raise HTTPException(400, f"最多 {job_priority.MAX_USERS} 位")
        valid: list[int] = []
        for v in raw:
            try:
                uid = int(v)
            except (TypeError, ValueError):
                raise HTTPException(400, "user_ids 只能是使用者編號")
            if user_manager.get_by_id(uid):
                valid.append(uid)
        saved = job_priority.set_user_ids(valid)
        audit_db.log_event(
            "settings_change", username=_actor(request), ip=_client_ip(request),
            target="job_priority", details={"user_ids": sorted(saved)},
        )
        return {"ok": True, "count": len(saved)}

    # ---------- /admin/workspace ----------

    @router.get("/workspace", response_class=HTMLResponse)
    async def workspace_admin_page(request: Request):
        from ..core import workspace as _ws
        return templates.TemplateResponse(request, "admin_workspace.html", {
            "request": request,
            "settings": _ws.get_settings(),
            "stats": _ws.collect_stats(),
        })

    @router.post("/workspace/save")
    async def workspace_admin_save(request: Request):
        from ..core import workspace as _ws
        body = await request.json()
        try:
            saved = _ws.save_settings(body)
        except ValueError as e:
            raise HTTPException(400, str(e))
        audit_db.log_event(
            "settings_change", username=_actor(request), ip=_client_ip(request),
            target="workspace", details=body,
        )
        return {"ok": True, "settings": saved}

    @router.post("/workspace/clear-user")
    async def workspace_admin_clear_user(request: Request, user_key: str = Form(...)):
        from ..core import workspace as _ws
        n = _ws.admin_clear_user(user_key)
        audit_db.log_event(
            "settings_change", username=_actor(request), ip=_client_ip(request),
            target="workspace", details={"action": "clear_user", "user_key": user_key, "removed": n},
        )
        return {"ok": True, "removed": n}

    @router.post("/workspace/clear-all")
    async def workspace_admin_clear_all(request: Request):
        from ..core import workspace as _ws
        n = _ws.admin_clear_all()
        audit_db.log_event(
            "settings_change", username=_actor(request), ip=_client_ip(request),
            target="workspace", details={"action": "clear_all", "removed": n},
        )
        return {"ok": True, "removed": n}

    # ---------- /admin/system-status ----------

    @router.get("/system-status", response_class=HTMLResponse)
    async def system_status_page(request: Request):
        return templates.TemplateResponse(request, 
            "admin_system_status.html", {"request": request},
        )

    @router.get("/system-status/host")
    async def system_status_host():
        """Fast — only psutil snapshot. Called every 5s by the auto-refresh."""
        from ..core import host_stats as _hs
        return _hs.get_host_stats()

    @router.get("/system-status/users")
    async def system_status_users(force: bool = False):
        """Slow — walks filesystem to compute per-user file counts + bytes.
        Cached 60s; pass `?force=1` to bypass cache (button on the page).
        Heavy IO offloaded to thread pool to keep the event loop free."""
        from ..core import host_stats as _hs
        import asyncio as _asyncio
        return await _asyncio.to_thread(_hs.get_user_file_stats, force)

    @router.get("/system-status/databases")
    async def system_status_databases(thorough: bool = False):
        """資料庫完整性 + 備份狀況。

        `thorough=1` 走完整的 integrity_check（大檔要幾秒）→ 丟到執行緒避免卡住
        事件迴圈。
        """
        import asyncio as _asyncio

        from ..core import db_health as _dh
        # thorough=1（管理員按「完整檢查」）才連大檔一起掃；平時跳過 ——
        # 統編資料庫 1.4 GB，掃一次實測 58 秒，頁面每次載入都等於整頁卡住。
        rows = await _asyncio.to_thread(
            _dh.check_all, bool(thorough), None if thorough else _dh._STARTUP_MAX_BYTES)
        newest = None
        for m in _dh.MANAGED:
            for b in _dh.list_backups(m["file"])[:1]:
                try:
                    ts = b.stat().st_mtime
                    newest = ts if newest is None else max(newest, ts)
                except OSError:
                    pass
        return {"databases": rows, "newest_backup": newest,
                "backup_dir": str(_dh.backup_dir())}

    @router.post("/system-status/databases/backup")
    async def system_status_backup_now(request: Request):
        import asyncio as _asyncio

        from ..core import db_health as _dh
        rep = await _asyncio.to_thread(_dh.backup_all)
        audit_db.log_event(
            "db_backup", username=_actor(request), ip=_client_ip(request),
            details=rep,
        )
        return {"ok": not rep.get("skipped"), "report": rep}

    return router


def _tool_name(tool_id: str) -> str:
    """Look up the friendly name for a tool id from the registry."""
    from ..tool_registry import discover_tools
    for t in discover_tools():
        if t.metadata.id == tool_id:
            return t.metadata.name
    return tool_id
