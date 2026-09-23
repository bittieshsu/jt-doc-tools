"""跟 jtlw（語音服務）講話的客戶端。

**分工**：jtlw 只做「聲音 → 逐字稿（含發言者）」。摘要、決議、待辦、心智圖、
翻譯全部在 JTDT —— 所以這裡不會出現 `summarize` / `translate` 之類的任務，
送過去也會被對方回 422 `task_not_supported`。

## 這支刻意做的幾個決定

* **位址每次呼叫都重新過 SSRF 檢查**（`jtlw_settings.base_url()` 做的），
  不在 `__init__` 存一份 —— 管理員改設定之後不必重啟。
* **回傳碼不可靠的那一課不適用**（那是 soffice）：這裡是 HTTP，
  但**錯誤訊息要說得出是哪一項出問題**。對方的錯誤格式是
  `{"error":{"code":…,"category":…,"retryable":…,"details":{…}}}`，
  我們把 `code` 與 `details.field` 一起帶出來，不要壓成一句「處理失敗」。
* **逾時分兩種**：`connect` 短（位址填錯要快點知道）、`read` 長一點
  （對方要拉檔、排隊）。但**這裡等的只是 HTTP 回應**，辨識本身是非同步的。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional

import httpx

from ..logging_setup import get_logger
from . import jtlw_settings

logger = get_logger(__name__)


class JtlwError(RuntimeError):
    """對方明確回報的錯誤。`code` / `field` 讓呼叫端講得出是哪一項。"""

    def __init__(self, message: str, *, code: str = "", category: str = "",
                 field: str = "", retryable: bool = False, status: int = 0,
                 details: Optional[dict] = None):
        super().__init__(message)
        self.code = code
        self.category = category
        self.field = field
        self.retryable = retryable
        self.status = status
        #: 對方 `error.details` 原樣帶出來 —— `retry_after_ms`（佇列滿）與
        #: `http_status`（他們拉我們的檔案時拿到的碼）都在這裡面。
        self.details = details or {}


class JtlwUnavailable(JtlwError):
    """連不上、或還沒設定好。**這是部署問題不是使用者送錯東西** ——
    呼叫端要映射成 503，不可以回 500（同本專案「缺相依回 503」那條）。"""


#: 對方的錯誤碼 → 講得出下一步的話。
#:
#: **一份就好。** 原本只有「終態作業的 `errors[]`」那條路在用（在工具裡），
#: 而送件當下被拒絕走的是**例外**那條 —— 於是使用者看到的是
#: `jtlw 拒絕了這個請求：source_not_allowed（欄位 source.url）`，
#: 一個對照表裡明明就有人話的錯誤碼（2026-09-22 正式機實際踩到）。
#:
#: 對方 v1.7 起**把拉檔提前到送件當下**，所以來源相關的錯誤現在多半
#: 走例外那條 —— 兩條路一定要講同一句話。
ERROR_TEXT: dict[str, str] = {
    "source_not_allowed":
        "對方不接受我們這台的位址 —— 請管理員把「錄音檔對外位址」給對方加進允許清單",
    "source_auth_failed": "對方來拉錄音檔時被我們擋掉了（401 / 403）",
    "source_checksum_mismatch": "錄音檔在傳送過程中對不上（雜湊或大小不符），請重試",
    "source_too_large": "錄音檔超過對方的大小上限",
    "unsupported_media": "這個音訊格式對方解不開，請換一種格式",
    "audio_too_long": "錄音太長（超過對方的長度上限），請先分段",
    "language_not_supported": "對方不支援這個語言，請改選一個",
    "profile_not_found": "對方沒有這個辨識模式了 —— 請到設定頁重新挑一個",
    "task_not_supported": "對方不支援這項工作",
    "queue_full": "對方的佇列滿了，請稍後再送一次",
    "glossary_unreachable": "對方拉不到詞彙庫（與錄音檔無關）",
    "glossary_too_large": "詞彙庫太大（與錄音檔無關）",
}


#: 不屬於對方錯誤碼、但一樣會顯示給使用者的固定訊息。
#:
#: **收在這裡是為了讓翻譯檢查看得到**：這些字在背景執行緒裡產生、送中文由前端翻
#: （`tests/test_i18n_dynamic_labels.py` 的「語音服務的失敗原因」那一類），
#: 原本散寫在函式裡，漏翻了也不會有任何測試紅。
#: **不可以在尾巴接變數**（主機名、權限名）—— 接了之後整句在語系檔裡查不到，
#: 英 / 日介面會退回中文。要給的細節寫進記錄。
MESSAGES: dict[str, str] = {
    # 401 與 403 是兩件事（對方 v2.9 指正）：401 是金鑰錯或被撤銷；
    # 403 是金鑰**對**、但沒有這個動作的權限 —— 合成一句的話，遇到 403 的人
    # 照著「打錯 / 撤銷」去查，會查不出問題。
    "key_rejected": "jtlw 不接受這把金鑰（401）—— 請到設定頁確認金鑰有沒有打錯，或是不是已經被撤銷",
    "key_no_permission": "jtlw 認得這把金鑰，但它沒有這個動作的權限（403）—— 請對方的管理員幫這把金鑰加上權限（缺哪一個權限寫在服務記錄裡）",
    # **不是排隊造成的**（對方 v2.9 指正）：對方收到送件就立刻拉檔，之後排多久都
    # 不會再拉這個網址。原本寫「多半是排隊太久；請管理員延長有效期」—— 前半句會讓
    # 人去懷疑對方的佇列，後半句叫管理員去改一個**根本不存在的設定**（2 小時是刻意寫死的）。
    "url_expired": "對方來拉錄音檔時網址已經過期 —— 對方收到送件就會拉檔，所以多半是送出之前重試或等了太久。請重新送一次。",
    "source_unreachable": "對方連不到我們的錄音檔位址，請確認「錄音檔對外位址」從對方那台連得到",
    # 對方的缺陷（v2.9 告知，`.223` 升級前都可能發生）：GPU 伺服器在傳結果途中重啟，
    # 可能把 0 段當成功回給我們。我們拿到 0 段本來就判失敗，這裡讓使用者知道重送通常就好。
    "empty_result": "jtlw 回報成功，但一段逐字稿都沒有 —— 可能是語音服務在傳結果的途中重啟過。錄音裡確實有人講話的話，請重新送一次。",
    # 送出的語言代碼對方不收（2026-09-23 正式機：頁面把「中文」送成 `zh`，
    # 對方要的是 `zh-Hant`，回 400 `invalid_request`、欄位 `language`）。
    # 原本畫面上印的是 `jtlw 回報：invalid_request（欄位 language）` —— 使用者看不懂，
    # 也不知道改選「自動判斷」就能送出去。
    "language_rejected": "語音服務不接受這個語言設定 —— 請改選「自動判斷」再送一次；一直發生的話，請管理員確認語音服務支援哪些語言。",
}


def describe_error(code: str, *, field: str = "", retryable: bool = False,
                   http_status: str = "", host: str = "") -> str:
    """把對方的錯誤碼講成使用者做得了下一步的話。

    `http_status` 是**對方拉我們的檔案時**拿到的碼（在 `details` 裡），
    不是這次請求的 HTTP 狀態。

    `host` 是被擋下來的主機（`details.host`）—— **一定要講出來**：
    管理員看到「請把位址加進允許清單」還是不知道要放行哪一個
    （對方 2026-09-22 建議，他們自己那側也補了記錄）。
    """
    if code == "source_not_allowed":
        msg = ERROR_TEXT[code]
        return msg + (f"：{host}" if host else "")
    if code == "source_unreachable":
        # **這一條特別容易誤導**：對方說 `source_unreachable` 涵蓋「網址過期」，
        # 而我們給的是短效簽章網址。404 ＋ 短效網址＝十之八九是過期，
        # 不是「檔案不見了」—— 照字面講的話使用者會去找一個還在的檔案。
        if str(http_status) == "404":
            return MESSAGES["url_expired"]
        return MESSAGES["source_unreachable"]
    if not code:
        return ""
    # 對方把「語言代碼不認得」回成 `invalid_request`（實際正式 API 的行為），
    # 文件上則寫 `language_not_supported` —— 兩個都認，判準是**欄位**。
    if field == "language" and code in ("invalid_request", "language_not_supported"):
        return MESSAGES["language_rejected"]
    msg = ERROR_TEXT.get(code)
    if msg:
        return msg + ("（這一類可以再試一次）" if retryable else "")
    msg = f"jtlw 回報：{code}"
    if field:
        msg += f"（欄位 {field}）"
    if retryable:
        msg += "（這一類可以再試一次）"
    return msg


def _raise_for(resp: httpx.Response) -> None:
    if resp.status_code < 400:
        return
    code = category = field = ""
    details: dict = {}
    retryable = False
    msg = f"jtlw 回了 HTTP {resp.status_code}"
    try:
        err = (resp.json() or {}).get("error") or {}
        code = str(err.get("code") or "")
        category = str(err.get("category") or "")
        retryable = bool(err.get("retryable"))
        details = err.get("details") or {}
        field = str(details.get("field") or "")
        if code:
            # **走共用的那一份** —— 不要在這裡再寫一次措辭
            msg = describe_error(code, field=field, retryable=retryable,
                                 http_status=str(details.get("http_status") or ""),
                                 host=str(details.get("host") or ""))
    except (json.JSONDecodeError, ValueError, AttributeError):
        pass
    if resp.status_code == 401:
        msg = MESSAGES["key_rejected"]
    elif resp.status_code == 403:
        msg = MESSAGES["key_no_permission"]
        # 缺哪一個權限（`details.required_scope`）寫進記錄，不接在訊息尾巴 ——
        # 接上變數的整句在語系檔裡查不到（見 `MESSAGES` 的說明）。
        logger.warning("jtlw 403：這把金鑰缺少權限 %s",
                       details.get("required_scope") or "（對方沒有說）")
    raise JtlwError(msg, code=code, category=category, field=field, details=details,
                    retryable=retryable, status=resp.status_code)


@dataclass
class Capabilities:
    api_revision: str = ""
    limits: dict[str, Any] = None          # type: ignore[assignment]
    raw: dict[str, Any] = None             # type: ignore[assignment]


class JtlwClient:
    """每次建立都重新讀設定；不要存成模組層級的單例。"""

    def __init__(self, *, timeout: Optional[float] = None):
        if not jtlw_settings.is_configured():
            raise JtlwUnavailable("還沒設定語音服務（jtlw）")
        self._base = jtlw_settings.base_url()          # 內含 SSRF 檢查
        self._key = jtlw_settings.api_key()
        t = float(timeout or jtlw_settings.get().get("request_timeout") or 30)
        self._timeout = httpx.Timeout(connect=min(8.0, t), read=t, write=t, pool=t)
        #: 憑證驗證。可能是**自簽憑證的路徑**（對方用自簽憑證時的正解）、
        #: `True`（走系統信任庫）或 `False`（管理員明確關掉）。
        #: 關掉之後任何人都可以冒充對方，而我們每次送件都把金鑰交出去 ——
        #: 所以設定頁把「貼上對方的憑證」擺在前面，關閉驗證只是最後手段。
        self._verify = jtlw_settings.verify_tls()

    # ---------- 低階 ----------

    def _headers(self, extra: Optional[dict] = None) -> dict:
        h = {"Authorization": f"Bearer {self._key}", "Accept": "application/json"}
        if extra:
            h.update(extra)
        return h

    def _request(self, method: str, path: str, **kw) -> httpx.Response:
        url = f"{self._base}{path}"
        try:
            with httpx.Client(timeout=self._timeout, follow_redirects=False,
                              verify=self._verify) as c:
                resp = c.request(method, url, headers=self._headers(kw.pop("headers", None)), **kw)
        except httpx.TimeoutException as e:
            raise JtlwUnavailable(f"連 jtlw 逾時（{self._base}）") from e
        except httpx.HTTPError as e:
            raise JtlwUnavailable("連不上 jtlw",
                                  details={"base": self._base}) from e
        _raise_for(resp)
        return resp

    # ---------- 端點 ----------

    def health(self) -> dict:
        """免認證的健康檢查。**不要拿它當「設定對不對」的判準** ——
        金鑰錯的時候這支照樣 200。"""
        try:
            with httpx.Client(timeout=self._timeout, verify=self._verify) as c:
                r = c.get(f"{self._base}/health")
        except httpx.HTTPError as e:
            raise JtlwUnavailable("連不上 jtlw",
                                  details={"base": self._base}) from e
        _raise_for(r)
        return r.json()

    def capabilities(self) -> Capabilities:
        d = self._request("GET", "/capabilities").json()
        return Capabilities(api_revision=str(d.get("api_revision") or ""),
                            limits=d.get("limits") or {}, raw=d)

    def profiles(self) -> list[dict]:
        d = self._request("GET", "/profiles").json()
        return d if isinstance(d, list) else (d.get("profiles") or [])

    def submit(self, body: dict, *, idempotency_key: str) -> dict:
        """送件。**`Idempotency-Key` 是必要的** —— 對方保證相同的鍵一律回
        原本那件作業（差異放在 `warnings`），所以我們重送不會變成兩件。"""
        return self._request("POST", "/jobs", json=body,
                             headers={"Idempotency-Key": idempotency_key,
                                      "Content-Type": "application/json"}).json()

    def job(self, job_id: str) -> dict:
        return self._request("GET", f"/jobs/{job_id}").json()

    def events(self, job_id: str, *, after_seq: int = 0) -> list[dict]:
        d = self._request("GET", f"/jobs/{job_id}/events",
                          params={"after_seq": after_seq}).json()
        return d if isinstance(d, list) else (d.get("events") or [])

    def segments(self, job_id: str, layer: str, *, after_seq: int = 0,
                 limit: int = 500) -> dict:
        return self._request("GET", f"/jobs/{job_id}/segments",
                             params={"layer": layer, "after_seq": after_seq,
                                     "limit": limit}).json()

    def result(self, job_id: str) -> dict:
        return self._request("GET", f"/jobs/{job_id}/result").json()

    def cancel(self, job_id: str) -> dict:
        return self._request("POST", f"/jobs/{job_id}/cancel").json()

    def ack(self, job_id: str) -> dict:
        """**只能在內容已經落地之後送** —— `ack` 的意思是「你可以刪了」。

        順序必須是「寫完 → fsync → 才 ACK」。反過來的話我們回報成功、
        他們刪掉、而我們其實沒寫進去。對方文件寫明**可以重複呼叫**，
        所以逾時重送是安全的。
        """
        return self._request("POST", f"/jobs/{job_id}/ack").json()

    def register_webhook(self, url: str) -> dict:
        """註冊 webhook。**回傳的 `secret` 只出現這一次**，拿到要馬上存起來。"""
        return self._request("POST", "/webhooks", json={"url": url},
                             headers={"Content-Type": "application/json"}).json()

    def list_webhooks(self) -> list[dict]:
        d = self._request("GET", "/webhooks").json()
        return d if isinstance(d, list) else (d.get("webhooks") or [])
