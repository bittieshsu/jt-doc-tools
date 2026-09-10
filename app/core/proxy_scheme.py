"""偵測「反向代理宣稱的協定」與「瀏覽器實際使用的協定」不一致。

**為什麼要有這一支**（v1.15.26，客戶回報）：客戶照文件把 IIS 的 `web.config`
裡的 `HTTP_X_FORWARDED_PROTO` 寫死成 `https`，但站台其實只有 http。後端據此
在 `jtdt_csrf` / `jtdt_session` 上加了 `Secure`，**瀏覽器會直接把那個 cookie
丟掉**（明文連線不收 Secure cookie）→ 上傳一律「CSRF token 遺失或不正確」，
啟用認證的話則是登入後又被踢回登入頁。

**最會誤導人的地方**：`http://localhost` 是瀏覽器的安全來源例外，**照樣收下
Secure cookie**。所以在伺服器本機上怎麼測都正常，只有遠端會壞 —— 客戶回報的
原話就是「本機 8765 或 localhost:80 都不會有問題，只要在遠端電腦就會」。

判斷依據是 `Origin` / `Referer`：那是**瀏覽器自己填的**，反映使用者網址列上
真正的協定；`X-Forwarded-Proto` 則是代理填的。兩邊不一致就是代理設錯了。

**只回報、不自動改行為**：不可以因為看到 `Origin: http://…` 就不加 `Secure`
—— 那個標頭在跨站請求裡由對方網頁決定，會變成把 https 站台的 cookie 降級成
明文可讀（cookie tossing）。這裡只負責把原因說出來。
"""
from __future__ import annotations

_HINT = (
    "反向代理送出的 X-Forwarded-Proto 是 https，但瀏覽器實際上是用 http 連線"
    "（Origin/Referer: {origin}）。後端因此在 cookie 加上 Secure，瀏覽器會直接"
    "丟棄它。請讓代理送出與實際相符的協定，或改用 https 對外提供服務。"
    "註：http://localhost 是瀏覽器的例外，所以在伺服器本機測不出這個問題。"
)


def _header(scope, name: bytes) -> str:
    for k, v in scope.get("headers", []):
        if k == name:
            try:
                return v.decode("latin-1")
            except Exception:  # noqa: BLE001
                return ""
    return ""


def forwarded_scheme(scope) -> str:
    """代理宣稱的協定（`X-Forwarded-Proto` 最左側），沒有就回空字串。"""
    raw = _header(scope, b"x-forwarded-proto")
    return raw.split(",")[0].strip().lower()


def browser_scheme(scope) -> str:
    """瀏覽器實際使用的協定 —— 取 `Origin`，沒有才退而取 `Referer`。"""
    for name in (b"origin", b"referer"):
        val = _header(scope, name).strip()
        if val.startswith("https://"):
            return "https"
        if val.startswith("http://"):
            return "http"
    return ""


def secure_cookie_mismatch(scope) -> str | None:
    """代理說 https、瀏覽器其實是 http → 回一句說得出原因的說明，否則 `None`。

    反過來（代理說 http、瀏覽器是 https）**不算問題**：那時 cookie 少了
    `Secure`，功能照常，屬於硬化建議不是故障，不該拿故障訊息去嚇人。
    """
    if forwarded_scheme(scope) != "https":
        return None
    if browser_scheme(scope) != "http":
        return None
    origin = _header(scope, b"origin").strip() or _header(scope, b"referer").strip()
    return _HINT.format(origin=origin[:80] or "http://…")
