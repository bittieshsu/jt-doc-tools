from __future__ import annotations

from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse

from ..config import settings

from ..core import auth_settings as _as, permissions as _perm

router = APIRouter()


def build_router(templates, tools, app_name: str, version: str) -> APIRouter:
    @router.get("/", response_class=HTMLResponse)
    async def home(request: Request):
        # **跟側欄用同一份清單**（權限、語言不符、外部服務沒設定、LLM 停用時的
        # 反灰或隱藏全在 `nav_tool_groups` 一個地方算）。原本首頁自己再算一份，
        # 只算了「語言不符」—— 語音服務沒設定好時側欄反灰、首頁卻照常可以點，
        # 而且反灰的卡片拿不到 `lock_reason`（滑鼠移上去是空的）。
        groups = [
            {"title": g["title"], "tools": list(g["tools"])}
            for g in templates.env.globals["nav_tool_groups"](request)
        ]
        tools_ctx = [t for g in groups for t in g["tools"]]
        return templates.TemplateResponse(request, 
            "home.html",
            {
                "request": request,
                "tools": tools_ctx,
                # **整個註冊表的工具數**（不是這位使用者看得到的幾支）——
                # 首頁那句是在講產品的規模。
                "tool_count": len(list(tools)),
                "groups": groups,
                # 不傳 app_name — Jinja global 已是動態 (branding.get_site_name())，
                # 這裡傳會 override 成 boot-time cached 值，導致改完站台名稱後首頁不變
                "version": version,
            },
        )

    @router.get("/healthz")
    async def healthz():
        """存活探測：這個行程還在服務就回 200。

        **刻意不看工具載入狀況** —— 服務管理員拿這支決定要不要重啟，
        因為少一支工具而讓它一直重啟比問題本身更糟。
        「少了什麼」看 `/readyz`。
        """
        return {"ok": True}

    @router.get("/readyz")
    async def readyz(response: Response):
        """這個行程實際上有沒有少東西。

        **為什麼要有這支**（外部稽核 2026-09-18 指出）：工具載入失敗原本只留
        一行 ERROR 然後跳過 —— 服務照常啟動、`/healthz` 照樣 200，
        使用者只發現「工具不見了」。少一個直接 import 的套件就會這樣
        （`defusedxml` 那次），而管理員沒有任何地方看得到。

        **兩層，不是一個布林**：
        * 資料目錄寫不進去 / 資料庫開不起來 → **真的不能工作**，回 **503**。
        * 工具少了幾支 → 其餘的照常可用，回 **200 ＋ `degraded: true`**。
          這一層回 503 是有害的：我們是**單一 web 行程**（見 OPS.md），
          把唯一的實例判成不健康，使用者看到的是整站錯誤頁。

        **不吐模組名稱與例外訊息** —— 這支跟 `/healthz` 一樣是公開的。
        細節在管理區的系統狀態頁（需要管理員）。
        """
        from ..tool_registry import load_failures

        failures = load_failures()

        data_dir_ok = False
        try:
            # **用臨時檔名、而且交給 `tempfile` 收尾** —— 固定檔名有兩個問題：
            # ①例外時會留下殘骸 ②那個字面會被「設定備份涵蓋」的檢查掃成
            # 一個沒人備份的設定檔（它掃的是原始碼裡的 `data_dir / "…"`）。
            import tempfile
            with tempfile.NamedTemporaryFile(dir=settings.data_dir,
                                             prefix=".probe-", delete=True):
                pass
            data_dir_ok = True
        except Exception:  # noqa: BLE001
            pass

        db_ok = False
        try:
            from ..core import auth_db
            # `auth_db.conn()` 是 thread-local 連線、**不是 context manager**
            # （寫成 `with` 會把連線關掉，而那條連線是整個執行緒共用的）。
            auth_db.conn().execute("SELECT 1").fetchone()
            db_ok = True
        except Exception:  # noqa: BLE001
            pass

        fatal = not (data_dir_ok and db_ok)
        if fatal:
            response.status_code = 503
        return {
            "ok": not fatal,
            "degraded": bool(failures),
            "tools": {"loaded": len(tools), "failed": len(failures)},
            "checks": {"data_dir_writable": data_dir_ok, "database": db_ok},
        }

    @router.get("/whoami")
    async def whoami(request: Request):
        """Return the current viewer's identity + roles + effective tools.
        Used by the sidebar's account-detail modal."""
        from ..core import auth_db
        nav_lookup = {
            n["id"]: n.get("name", n["id"])
            for n in (templates.env.globals.get("nav_tools") or [])
        }
        if not _as.is_enabled():
            return {
                "auth_enabled": False,
                "username": "(anonymous)",
                "display_name": "單機模式",
                "source": "off",
                "is_admin": True,
                "roles": [],
                "tools": [],
                "tools_all": True,
            }
        user = getattr(request.state, "user", None)
        if not user:
            return {"auth_enabled": True, "anonymous": True}
        uid = user.get("user_id", 0)
        et = _perm.effective_tools(uid)
        # Roles assigned directly to the user subject (groups/OUs aren't shown
        # explicitly here — keep the modal lean; the effective tools list
        # already reflects the union).
        role_ids = _perm.list_roles_for_subject("user", str(uid))
        roles_out = []
        if role_ids:
            conn = auth_db.conn()
            placeholders = ",".join("?" * len(role_ids))
            rows = conn.execute(
                f"SELECT id, display_name FROM roles WHERE id IN ({placeholders}) ORDER BY display_name",
                tuple(role_ids),
            ).fetchall()
            roles_out = [{"id": r["id"], "display_name": r["display_name"]} for r in rows]
        if et == "ALL":
            tools_out = []
            tools_all = True
        else:
            tools_all = False
            tools_out = sorted(
                [{"id": tid, "name": nav_lookup.get(tid, tid)} for tid in et],
                key=lambda x: x["name"],
            )
        # 2FA / TOTP self-service status — show in the account modal so
        # any user can enable it (LDAP/AD users included; TOTP is local-app
        # state, independent of password backend).
        try:
            from ..core import totp as _totp
            tstate = _totp.get_user_totp_state(uid)
        except Exception:
            tstate = {"enabled": False, "required": False, "has_secret": False}
        # Auditor users: required is hard-coded True even if DB column is 0
        try:
            forced_by_role = _perm.is_auditor(uid)
        except Exception:
            forced_by_role = False
        # 信箱：作業完成通知寄到這裡。
        #
        # **能不能自己改，取決於帳號從哪裡來** —— 與同一張卡片上的密碼是同一套
        # 邏輯：本機帳號自己管，目錄帳號由來源管（改了也會在下次登入被覆蓋，
        # 那種「改得動但沒有用」的欄位比唯讀更糟）。
        src = user.get("source", "local")
        return {
            "auth_enabled": True,
            "username": user.get("username"),
            "display_name": user.get("display_name") or user.get("username"),
            "source": src,
            "email": user.get("email") or "",
            "email_editable": src == "local",
            "is_admin": (et == "ALL"),
            "roles": roles_out,
            "tools": tools_out,
            "tools_all": tools_all,
            "totp": {
                "enabled": tstate["enabled"],
                "required": tstate["required"] or forced_by_role,
            },
        }

    return router
