"""作業完成通知：把 jt-doc-tools 帳號綁到 Zulip 帳號（私訊 Bot 綁定，v1.17.0）。

為什麼要綁：Zulip 的 email 隱私設定會讓 Bot 只看到 `user9@<站台>` 這種代用地址，用帳號信箱私訊會被拒
（`Invalid email`）；一般使用者也不知道自己的 Zulip 使用者編號。Zulip 維護人員建議（2026-10-18）：

1. 使用者在本站按「綁定 Zulip」，取得**一次性代碼**（綁這個帳號、10 分鐘有效）。
2. 使用者在 Zulip 把完整代碼**私訊給通知 Bot**。
3. 後端只認「使用者與 Bot 的一對一新私訊」：排除群組、Bot 自己與其他 Bot、編輯過的訊息、代碼產生前的訊息，
   過期或用過的代碼不認。`is:dm` 篩過之後**還要檢查收件人集合**。
4. 使用者編號取 Zulip 回應的 `sender_id` —— 不採信訊息裡寫的任何編號或 email。
5. 原頁面顯示「即將綁定：Zulip 顯示名稱」，由原登入者確認，再**在同一個鎖裡**完成綁定與代碼消耗。
6. 存「本站使用者 → Zulip 站台＋user_id」，通知直接用它；可以解除、重新綁定。

這證明的是「操作的人同時控制兩邊的帳號」，不是「兩邊是同一個 LDAP 身分」（Zulip 目前沒有可信的共同欄位）。

## 安全與資源

* 代碼是長隨機字串（約 60 位元，可複製），**只存在記憶體**、不寫進記錄檔或稽核（稽核只記綁到哪個 user_id）。
* 每人同時只有一組代碼（新的取代舊的）；每人 10 分鐘最多產生 5 組；全站同時最多 200 組。
* 收訊：**有待綁定的代碼時**才由單一背景執行緒每 5 秒查一次（Zulip 每個帳號 200 次 ／ 60 秒，跟通知共用額度）；
  用訊息編號當游標、分頁補讀；429 照 `Retry-After` 等。
"""
from __future__ import annotations

import json
import logging
import re
import secrets
import threading
import time
from typing import Any, Optional

logger = logging.getLogger("app.zulip_link")

CODE_TTL = 600.0                 # 代碼有效 10 分鐘
POLL_INTERVAL = 5.0
MAX_PER_USER_WINDOW = 5          # 每人 10 分鐘最多產生幾組
MAX_ACTIVE = 200
PAGE = 100                       # 一次讀幾則
_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"     # 去掉容易看錯的 0 O 1 I L
_TIMEOUT = 15.0

_LOCK = threading.Lock()
#: 代碼 → 狀態。key＝本站使用者鍵（`u<id>`）
_CODES: dict[str, dict[str, Any]] = {}
_ISSUED: dict[str, list[float]] = {}             # 使用者鍵 → 產生時間（限速）
_POLLER: Optional[threading.Thread] = None
_STATE: dict[str, Any] = {"cursor": None, "bot_id": None, "bot": None, "site": None, "wait_until": 0.0, "error": ""}


class LinkError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# --------------------------------------------------------------------------- Zulip API

def _cfg() -> dict:
    from . import notify_settings as ns
    c = ns.get(reveal=True)
    z = (c.get("channels") or {}).get("zulip") or {}
    if not (c.get("enabled") and z.get("enabled") and z.get("zulip_site") and z.get("zulip_bot_email")
            and z.get("zulip_api_key")):
        raise LinkError("ZULIP_NOT_READY", "管理員還沒有設定好 Zulip 通知")
    return z


def _site(z: dict) -> str:
    return str(z["zulip_site"]).rstrip("/")


def _get(z: dict, path: str, params: Optional[dict] = None):
    """GET Zulip API；回（狀態碼, JSON, 標頭）。網路錯誤丟 LinkError。"""
    import httpx
    try:
        with httpx.Client(timeout=_TIMEOUT, follow_redirects=False) as c:
            r = c.get(_site(z) + path, params=params, auth=(z["zulip_bot_email"], z["zulip_api_key"]))
    except httpx.HTTPError as e:
        raise LinkError("ZULIP_UNREACHABLE", f"連不上 Zulip 站台（{type(e).__name__}）") from None
    try:
        body = r.json()
    except ValueError:
        body = {}
    return r.status_code, body if isinstance(body, dict) else {}, r.headers


def _api_error(status: int, body: dict) -> LinkError:
    from .notify_channels import _zulip_error_text
    return LinkError("ZULIP_ERROR", _zulip_error_text(status, body, ""))


def _bot(z: dict) -> dict:
    st, body, _h = _get(z, "/api/v1/users/me")
    if st != 200 or body.get("result") != "success":
        raise _api_error(st, body)
    return {"user_id": int(body["user_id"]), "full_name": str(body.get("full_name") or "")[:80],
            "email": str(body.get("email") or "")[:120]}


def _user(z: dict, uid: int) -> Optional[dict]:
    st, body, _h = _get(z, f"/api/v1/users/{int(uid)}")
    if st != 200 or body.get("result") != "success":
        return None
    u = body.get("user") or {}
    return {"user_id": int(u.get("user_id") or uid), "full_name": str(u.get("full_name") or "")[:80],
            "is_bot": bool(u.get("is_bot")), "is_active": u.get("is_active", True) is not False}


def _newest_dm_id(z: dict) -> int:
    """目前最新一則私訊的編號（代碼產生時記下來：只認之後的訊息，不靠兩邊的時鐘）。"""
    st, body, _h = _get(z, "/api/v1/messages", {
        "anchor": "newest", "num_before": 1, "num_after": 0,
        "narrow": json.dumps([{"operator": "is", "operand": "dm"}]), "apply_markdown": "false"})
    if st != 200 or body.get("result") != "success":
        raise _api_error(st, body)
    msgs = body.get("messages") or []
    return max((int(m.get("id") or 0) for m in msgs), default=0)


# --------------------------------------------------------------------------- 代碼

def _new_code() -> str:
    raw = "".join(secrets.choice(_ALPHABET) for _ in range(12))
    return f"JTDT-{raw[:4]}-{raw[4:8]}-{raw[8:]}"


_CODE_RE = re.compile(r"\bJTDT-[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4}\b")


def _purge(now: float) -> None:
    for c, s in list(_CODES.items()):
        if s["expires"] <= now or s.get("used"):
            _CODES.pop(c, None)


def start(key: str) -> dict:
    """產生（或換新）這個使用者的綁定代碼。回給畫面：代碼、到期時間、要私訊的 Bot。"""
    if not key or not key.startswith("u") or not key[1:].isdigit():
        raise LinkError("LOGIN_REQUIRED", "要登入才能綁定 Zulip")
    z = _cfg()
    bot = _bot(z)
    after = _newest_dm_id(z)
    now = time.time()
    with _LOCK:
        _purge(now)
        recent = [t for t in _ISSUED.get(key, []) if t > now - CODE_TTL]
        if len(recent) >= MAX_PER_USER_WINDOW:
            raise LinkError("TOO_MANY_CODES", "產生代碼太多次，請 10 分鐘後再試")
        if len(_CODES) >= MAX_ACTIVE:
            raise LinkError("TOO_MANY_CODES", "目前綁定的人太多，請稍後再試")
        for c, s in list(_CODES.items()):          # 每人只留最新一組
            if s["key"] == key:
                _CODES.pop(c, None)
        code = _new_code()
        while code in _CODES:
            code = _new_code()
        _CODES[code] = {"key": key, "site": _site(z), "created": now, "expires": now + CODE_TTL,
                        "after_id": after, "candidate": None, "used": False}
        _ISSUED[key] = recent + [now]
        # 游標：換了站台或還沒有就從現在開始；舊游標比較早也沒關係（每組代碼只認自己 after_id 之後的訊息）
        if _STATE.get("cursor") is None or _STATE.get("site") != _site(z):
            _STATE.update(cursor=after, site=_site(z))
        _STATE["bot_id"] = bot["user_id"]
        _STATE["bot"] = {"name": bot["full_name"], "email": bot["email"]}
    _ensure_poller()
    return {"code": code, "expires_at": now + CODE_TTL, "bot": {"name": bot["full_name"], "email": bot["email"]}}


def status(key: str) -> dict:
    """這個使用者的綁定狀態：已綁定、等待私訊、等待確認、或沒有進行中的綁定。"""
    from . import notify_settings as ns
    link = ns.get_zulip_link(key)
    now = time.time()
    with _LOCK:
        _purge(now)
        pend = next(({"code": c, **s} for c, s in _CODES.items() if s["key"] == key), None)
        err = _STATE.get("error") or ""
        bot = dict(_STATE.get("bot") or {})
    out: dict[str, Any] = {"linked": None, "pending": None}
    if link:
        out["linked"] = {"user_id": link["user_id"], "name": link.get("name", ""), "site": link.get("site", "")}
    if pend:
        out["pending"] = {"code": pend["code"], "expires_at": pend["expires"],
                          "candidate": ({"user_id": pend["candidate"]["user_id"], "name": pend["candidate"]["name"]}
                                        if pend.get("candidate") else None),
                          "bot": bot, "error": err}
    return out


def confirm(key: str, code: str) -> dict:
    """原登入者確認「即將綁定」的那個 Zulip 帳號 —— 綁定與代碼消耗在同一個鎖裡完成。"""
    from . import notify_settings as ns
    now = time.time()
    with _LOCK:
        s = _CODES.get(str(code or ""))
        if not s or s["key"] != key or s.get("used") or s["expires"] <= now:
            raise LinkError("CODE_INVALID", "代碼已過期或已使用，請重新綁定")
        cand = s.get("candidate")
        if not cand:
            raise LinkError("NOT_YET", "還沒收到這組代碼的私訊")
        s["used"] = True
        ns.save_zulip_link(key, {"site": s["site"], "user_id": int(cand["user_id"]), "name": cand["name"],
                                 "linked_at": now})
        _CODES.pop(code, None)
    return {"user_id": int(cand["user_id"]), "name": cand["name"]}


def cancel(key: str) -> None:
    with _LOCK:
        for c, s in list(_CODES.items()):
            if s["key"] == key:
                _CODES.pop(c, None)


# --------------------------------------------------------------------------- 收訊

def _accept(msg: dict, bot_id: int) -> Optional[int]:
    """這則訊息是不是「使用者跟 Bot 的一對一私訊」；是的話回寄件人編號。"""
    if msg.get("type") not in ("private", "direct"):
        return None
    if msg.get("last_edit_timestamp") or msg.get("edit_history"):
        return None                                        # 編輯過的不認（內容可能事後改成代碼）
    sender = msg.get("sender_id")
    if not isinstance(sender, int) or sender == bot_id:
        return None
    rec = msg.get("display_recipient")
    if not isinstance(rec, list):
        return None
    ids = {r.get("id") for r in rec if isinstance(r, dict)}
    if ids != {sender, bot_id}:
        return None                                        # 群組私訊、或沒有 Bot 在裡面
    return sender


def poll_once() -> int:
    """讀一輪新私訊、比對代碼。回處理了幾則。由背景執行緒呼叫；測試也直接叫它。"""
    now = time.time()
    with _LOCK:
        _purge(now)
        if not _CODES:
            return 0
        if now < _STATE.get("wait_until", 0):
            return 0
        cursor, bot_id = _STATE.get("cursor") or 0, _STATE.get("bot_id")
    try:
        z = _cfg()
        if bot_id is None:
            bot_id = _bot(z)["user_id"]
        seen = 0
        found_newest = False
        while not found_newest:
            st, body, headers = _get(z, "/api/v1/messages", {
                "anchor": str(cursor), "include_anchor": "false", "num_before": 0, "num_after": PAGE,
                "narrow": json.dumps([{"operator": "is", "operand": "dm"}]), "apply_markdown": "false"})
            if st == 429:
                wait = float(headers.get("retry-after") or body.get("retry-after") or 10)
                with _LOCK:
                    _STATE["wait_until"] = time.time() + min(max(wait, 1.0), 120.0)
                return seen
            if st != 200 or body.get("result") != "success":
                raise _api_error(st, body)
            msgs = sorted(body.get("messages") or [], key=lambda m: int(m.get("id") or 0))
            for m in msgs:
                mid = int(m.get("id") or 0)
                cursor = max(cursor, mid)
                seen += 1
                sender = _accept(m, bot_id)
                if sender is None:
                    continue
                for code in _CODE_RE.findall(str(m.get("content") or "").upper()):
                    _match(z, code, mid, sender)
            found_newest = bool(body.get("found_newest")) or not msgs
        with _LOCK:
            _STATE.update(cursor=cursor, bot_id=bot_id, error="")
        return seen
    except LinkError as e:
        with _LOCK:
            _STATE["error"] = e.message
        logger.warning("Zulip 綁定收訊失敗：%s", e.message)
        return 0


def _match(z: dict, code: str, mid: int, sender: int) -> None:
    with _LOCK:
        s = _CODES.get(code)
        if not s or s.get("used") or s["expires"] <= time.time() or mid <= s["after_id"] or s.get("candidate"):
            return
    u = _user(z, sender)                       # 名稱與 Bot 身分都以 Zulip 回的為準
    if not u or u["is_bot"] or not u["is_active"]:
        return
    with _LOCK:
        s = _CODES.get(code)
        if s and not s.get("used") and not s.get("candidate"):
            s["candidate"] = {"user_id": u["user_id"], "name": u["full_name"]}


def _loop() -> None:
    global _POLLER
    while True:
        with _LOCK:
            _purge(time.time())
            if not _CODES:
                _POLLER = None
                return
        try:
            poll_once()
        except Exception:  # noqa: BLE001 —— 收訊出錯不可以讓執行緒死掉，下一輪再試
            logger.exception("Zulip 綁定收訊例外")
        time.sleep(POLL_INTERVAL)


def _ensure_poller() -> None:
    global _POLLER
    with _LOCK:
        if _POLLER is not None and _POLLER.is_alive():
            return
        _POLLER = threading.Thread(target=_loop, name="zulip-link-poller", daemon=True)
        _POLLER.start()


def _reset_for_tests() -> None:
    with _LOCK:
        _CODES.clear()
        _ISSUED.clear()
        _STATE.update(cursor=None, bot_id=None, bot=None, site=None, wait_until=0.0, error="")
