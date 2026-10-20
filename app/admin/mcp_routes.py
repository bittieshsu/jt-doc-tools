"""設定 → MCP（v1.17.1）：總開關、開放的工具、限流、傳輸票證、哪幾張 Token 能連、客戶端設定範例。

掛在 `/admin` 底下（繼承 require_admin）。改設定寫稽核記錄（`settings_change`，對象 `mcp`），不記 Token。
"""
from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from ..core import audit_db


def _actor(r: Request) -> str:
    user = getattr(r.state, "user", None)
    return (user or {}).get("username", "") if isinstance(user, dict) else ""


def _ip(r: Request) -> str:
    from ..core.client_ip import real_client_ip
    return real_client_ip(r)


def build_mcp_router(templates) -> APIRouter:
    router = APIRouter()

    @router.get("/mcp", response_class=HTMLResponse)
    async def mcp_page(request: Request):
        from ..core import api_tokens as at, auth_db, auth_settings, notify_settings
        from ..mcp import registry, server, settings as ms
        names = {}
        if auth_settings.is_enabled():
            for r in auth_db.conn().execute("SELECT id, username FROM users").fetchall():
                names[r["id"]] = r["username"]
        tokens = [dict(t, owner=names.get(t.get("owner_user_id"), "")) for t in at.api_tokens.list_tokens()]
        size = len(json.dumps({"tools": list(server.FIXED_TOOLS)}, ensure_ascii=False))
        # 開放的工具：照分類分組，每支用它在側欄的圖示、顏色與中文說明
        from .. import main as _main
        meta = {t.metadata.id: t.metadata for t in _main.tools}
        enabled = set(ms.enabled_tools())
        groups = []
        # 點工具卡片看的說明：跟 AI 呼叫 jtdt_describe 拿到的是同一份（registry.describe），再加輸出種類
        tool_docs = {t.name: dict(registry.describe(t), output_kind=t.output) for t in registry.TOOLS}
        for cid, label in registry.CATEGORIES.items():
            items = []
            for t in registry.TOOLS:
                if t.category != cid:
                    continue
                m = meta.get(t.tool_id)
                items.append({"name": t.name, "title": t.title, "on": t.name in enabled,
                              "icon": (m.icon if m else "tool"), "color": _main._tool_color_map.get(t.tool_id, 0),
                              "desc": (m.description if m else ""), "loaded": m is not None})
            if items:
                groups.append({"id": cid, "items": items})
        return templates.TemplateResponse(request, "admin_mcp.html", {
            "request": request,
            "cfg": ms.get(),
            "enabled_tools": ms.enabled_tools(),
            "tools": registry.TOOLS,
            "categories": registry.CATEGORIES,
            "tokens": tokens,
            "auth_enabled": auth_settings.is_enabled(),
            "site_url": (notify_settings.get().get("site_url") or "").strip(),
            "fixed_count": len(server.FIXED_TOOLS),
            "list_chars": size,
            "list_tokens_est": size // 4,
            "groups": groups,
            "tool_docs": tool_docs,
            "mcp_tokens": sum(1 for t in tokens if t.get("mcp")),
        })

    @router.post("/mcp/save")
    async def mcp_save(request: Request):
        from ..core import notify_settings
        from ..mcp import settings as ms
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = None
        if not isinstance(body, dict):
            raise HTTPException(400, "需要 JSON 物件")
        if "site_url" in body:
            url = str(body.get("site_url") or "").strip()
            if url and not url.startswith(("http://", "https://")):
                raise HTTPException(400, "站台網址要以 http:// 或 https:// 開頭")
            notify_settings.save({"site_url": url})
        cfg = ms.save({k: body[k] for k in ("enabled", "tools", "rate_per_minute", "max_concurrent",
                                            "ticket_bind_ip", "ticket_allow_http") if k in body})
        audit_db.log_event("settings_change", username=_actor(request), ip=_ip(request), target="mcp",
                           details={"enabled": cfg["enabled"], "tools": cfg["tools"],
                                    "rate_per_minute": cfg["rate_per_minute"],
                                    "max_concurrent": cfg["max_concurrent"],
                                    "ticket_bind_ip": cfg["ticket_bind_ip"],
                                    "ticket_allow_http": cfg["ticket_allow_http"],
                                    "site_url_changed": "site_url" in body})
        return {"ok": True, "cfg": cfg, "site_url": (notify_settings.get().get("site_url") or "")}

    @router.post("/mcp/token")
    async def mcp_token(request: Request):
        """打開 / 關閉某一張 Token 連 MCP（用畫面上的遮罩辨認，不傳 Token 本身）。"""
        from ..core import api_tokens as at
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = None
        if not isinstance(body, dict) or not isinstance(body.get("preview"), str):
            raise HTTPException(400, "需要 preview")
        allowed = body.get("allowed") is True
        if not at.api_tokens.set_mcp(body["preview"], allowed):
            raise HTTPException(404, "找不到這張 Token")
        label = next((t.get("label") for t in at.api_tokens.list_tokens()
                      if t.get("token_preview") == body["preview"]), "")
        audit_db.log_event("settings_change", username=_actor(request), ip=_ip(request), target="mcp",
                           details={"token": str(label)[:64], "mcp": allowed})
        return {"ok": True, "mcp": allowed}

    return router
