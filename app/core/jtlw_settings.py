"""jtlw（語音服務）的整合設定。

**分工**（使用者 2026-09-17 拍板）：jtlw 只做「聲音 → 逐字稿（含發言者）」，
摘要 / 決議 / 待辦 / 心智圖 / 翻譯全部留在 JTDT。所以這裡存的是
「去哪裡送件、用什麼身分、錄音檔從哪個位址給對方拉」，不是分析參數。

**祕密欄位加密存放**（Fernet，鑰匙來自 session secret，同 `sso_settings`）：
API 金鑰與 webhook 簽章密鑰。檔案權限 0o600、原子寫入。

## ⚠ 三件動手前就決定好的事

1. **`audio_base_url` 一定是寫定的值，不可以從請求的 Host 推。**
   同一套服務有三種到達方式（內網直連 / 反向代理的網域 / 測試機），
   照請求的 Host 組的話，從對外網域進來的人送出的作業會帶著對外網域，
   **被對方的來源白名單擋掉** —— 而症狀是「有些人可以、有些人不行」。
   （`app/web/speech_routes.py` 的簽章網址是同一條理由。）

2. **`base_url` 是管理員填的 → 那是 SSRF 的入口。**
   一律過 `url_safety.safe_remote_base_url()`（遠端 OCR 走的同一道），
   不要自己再寫一份判斷。

3. **沒設定好 = 工具反灰**（使用者 2026-09-21 指示）。判準是
   `is_configured()`，而不是「這台裝了什麼」—— jtlw 是一個外部服務，
   裝不裝在我們這裡看不出來。
"""
from __future__ import annotations

import base64
import json
import threading
import time
from pathlib import Path
from typing import Any

from ..config import settings
from ..logging_setup import get_logger
from . import atomic_json

logger = get_logger(__name__)

#: 管理頁把已存在的祕密顯示成這個字串；存檔時原樣送回＝「不要動」。
SECRET_KEPT = "__KEPT__"

#: 預設的處理設定與任務。`translate` / `summarize` 那幾個**故意不列** ——
#: 對方會回 422 `task_not_supported`（那幾段是我們自己做的）。
#: 對方 API 的固定前綴。**由我們自己接，不是讓管理員打進來的** ——
#: `url_safety.safe_remote_base_url()` 刻意只回 `scheme://host[:port]`，
#: 路徑一律丟掉，那正是它當 SSRF barrier 的方式（「呼叫端自己接固定路徑」）。
#: 管理員照對方文件貼一整串 `http://host:8790/api/v1` 也沒關係，
#: 多出來的路徑會被丟掉再由這裡接回去。
#:
#: 第一版寫成直接用管理員填的整串，結果請求打到 `http://host:8990/jobs`
#: —— 少了前綴，對方回 404，而錯誤訊息只說「HTTP 404」。
#: **拿 mock 真的跑一次才看得到**（單元測試對得起來，因為那是我們自己算的字串）。
API_PREFIX = "/api/v1"

DEFAULT_PROFILE = "meeting.balanced"
DEFAULT_TASKS = ("transcribe", "diarize", "correct")

#: 校正力道。**對方讓我們決定預設值**（接入清單第 3 節）。
#:
#: 選最保守的那一個：校正改得越多，摘要與決議就越可能建立在被改過的字上。
#: 我們量過傳播率（`tools/meeting_eval/contamination.py`）——
#: 逐字稿的行有 10.2% 被改壞或沒改對，但抽出來的項目只有 4.5% 建立在那些字上，
#: 因為決議多半抽自結論句，而結論句正是校正比較不會動的。
#: 那組數字是在 `standard` 上量的，預設更保守只會更好。
DEFAULT_CORRECTION_LEVEL = "punctuation_only"

_DEFAULTS: dict[str, Any] = {
    "enabled": False,
    #: 例如 http://<主機>:8790/api/v1
    "base_url": "",
    "api_key_enc": "",
    #: 對方要來拉錄音檔時看到的我們 —— **寫定的值**，理由見模組說明第 1 點。
    "audio_base_url": "",
    "profile_id": DEFAULT_PROFILE,
    "tasks": list(DEFAULT_TASKS),
    "correction_level": DEFAULT_CORRECTION_LEVEL,
    #: `POST /webhooks` 註冊之後才有；密鑰對方只給一次。
    "webhook_endpoint_id": "",
    "webhook_secret_enc": "",
    #: 送件逾時（秒）。拉檔與辨識是對方的事，我們只等 HTTP 回應。
    "request_timeout": 30,
    #: 對方的憑證（PEM）。內部服務常用自簽憑證 —— **正解是信任它，不是關掉驗證**
    #: （對方 2026-09-21 也明確要求不要用關閉驗證繞過）。
    #: 填了之後這條連線改用這份憑證驗，**其餘連線不受影響**。
    #: 這是公開資訊（憑證本來就會在握手時送出），不加密存放。
    "ca_cert_pem": "",
    #: 要不要驗對方的 TLS 憑證。**預設驗**。
    #:
    #: 內部服務常用自簽憑證，驗不過。**先試著把內部 CA 裝進這台機器的信任庫**
    #: —— 那是一勞永逸的做法，而且全站每一條對外連線都受惠
    #: （`net_ssl.install_os_trust()` 啟動時就接上 OS 的信任庫了）。
    #: 真的做不到才關掉這個 —— 關掉之後**任何人都可以冒充對方**，
    #: 而我們每次送件都會把 API 金鑰放在標頭裡交出去。
    "verify_tls": True,
    "updated_at": 0.0,
}

_SECRET_FIELDS = ("api_key_enc", "webhook_secret_enc")

_LOCK = threading.RLock()
_CACHE: dict[str, Any] | None = None


def _path() -> Path:
    return settings.data_dir / "jtlw_settings.json"


# ---------- 加密（與 sso_settings 同一把鑰匙） ----------

def _fernet():
    from cryptography.fernet import Fernet
    from . import auth_settings
    return Fernet(base64.urlsafe_b64encode(auth_settings._ensure_secret()))


def encrypt_secret(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("ascii") if plaintext else ""


def decrypt_secret(ciphertext: str) -> str:
    if not ciphertext:
        return ""
    try:
        return _fernet().decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except Exception:
        logger.warning("jtlw_settings: secret decrypt failed (key rotated?)")
        return ""


# ---------- 讀寫 ----------

def _load() -> dict[str, Any]:
    global _CACHE
    with _LOCK:
        if _CACHE is not None:
            return _CACHE
        data = json.loads(json.dumps(_DEFAULTS))
        p = _path()
        if p.is_file():
            try:
                data.update(json.loads(p.read_text(encoding="utf-8")))
            except Exception:
                logger.warning("jtlw_settings: %s 讀不回來，改用預設值", p)
        _CACHE = data
        return data


def invalidate_cache() -> None:
    global _CACHE
    with _LOCK:
        _CACHE = None


def get(*, reveal: bool = False) -> dict[str, Any]:
    """給管理頁用。`reveal=False` 時祕密欄位換成 `SECRET_KEPT`。"""
    data = json.loads(json.dumps(_load()))
    for f in _SECRET_FIELDS:
        if data.get(f):
            data[f] = decrypt_secret(data[f]) if reveal else SECRET_KEPT
        else:
            data[f] = ""
    return data


def normalise_api_key(raw: str) -> str:
    """把管理員貼進來的東西收成「金鑰本身」。

    `Authorization: Bearer <金鑰>` 這條標頭是**我們自己組的**（`jtlw_client`），
    所以欄位裡只要那串金鑰。但對方文件寫的是整條標頭，照著複製很自然會把
    `Bearer ` 一起貼進來 —— 2026-09-22 拿對方正式 API 實測三種貼法：

    | 送出去的標頭 | 結果 |
    |---|---|
    | `Bearer jtlw_…`（現在的做法） | 200 |
    | `Bearer Bearer jtlw_…` | **401「缺少或無效的 API Key」** |
    | `Bearer Authorization: Bearer jtlw_…` | **401**（同上） |

    **訊息指錯方向**：它說金鑰無效，而金鑰其實是對的、錯的只是多了前綴。
    真的金鑰不會以這兩個詞開頭，所以在這裡收掉是安全的。

    收在 `save()` 裡＝**一個地方負責**，管理頁、設定匯入、CLI 都受惠。
    """
    s = (raw or "").strip().strip('"').strip("'").strip()
    for _ in range(3):                       # `Authorization: Bearer x` 要剝兩層
        low = s.lower()
        if low.startswith("authorization:"):
            s = s[len("authorization:"):].lstrip()
        elif low.startswith("bearer "):      # 要求空白 —— 沒有空白就當成金鑰本身
            s = s[len("bearer "):].lstrip()
        else:
            break
    return s.strip()


def save(new: dict[str, Any]) -> None:
    """存設定。祕密欄位收到 `SECRET_KEPT` 就保留原本的密文，
    收到其他非空值當成新的**明文**加密，收到空字串就清掉。"""
    global _CACHE
    with _LOCK:
        current = json.loads(json.dumps(_load()))
        incoming = json.loads(json.dumps(new))
        for f in _SECRET_FIELDS:
            if f not in incoming:
                continue
            val = incoming[f]
            if val == SECRET_KEPT:
                incoming[f] = current.get(f, "")
            elif val:
                if f == "api_key_enc":
                    val = normalise_api_key(val)
                incoming[f] = encrypt_secret(val)
            else:
                incoming[f] = ""
        merged = json.loads(json.dumps(_DEFAULTS))
        merged.update(current)
        merged.update(incoming)
        merged["updated_at"] = time.time()
        atomic_json.write_json(_path(), merged, mode=0o600)
        _CACHE = merged


# ---------- 給其他模組用的判斷 ----------

def is_configured() -> bool:
    """設定齊不齊 —— **工具反灰的判準**（使用者 2026-09-21 指示）。

    三項缺一不可：開關打開、送件位址、API 金鑰。
    `audio_base_url` **不算在內**：沒填時退回應用程式自己的對外位址設定，
    送件那一步才會發現，而那時候的錯誤訊息說得出是哪一項。
    """
    d = _load()
    return bool(d.get("enabled") and d.get("base_url") and d.get("api_key_enc"))


def api_key() -> str:
    return decrypt_secret(_load().get("api_key_enc", ""))


def webhook_secret() -> str:
    return decrypt_secret(_load().get("webhook_secret_enc", ""))


def base_url() -> str:
    """送件位址，**過完 SSRF 檢查**才回傳。

    管理員填的值是 SSRF 的入口，判斷一律走 `url_safety` 那一份
    （遠端 OCR 走的同一道），不要在這裡另寫。
    """
    from app.core.url_safety import safe_remote_base_url
    raw = (_load().get("base_url") or "").strip()
    if not raw:
        raise ValueError("還沒設定 JTLW 的送件位址")
    return safe_remote_base_url(raw).rstrip("/") + API_PREFIX


_CA_FILENAME = "jtlw_ca.pem"


def ca_cert_path() -> Path | None:
    """自簽憑證寫到磁碟上的位置；沒設定就回 `None`。

    **httpx / ssl 只吃檔案路徑**，所以存的是 PEM、用的時候落一份檔案。
    內容沒變就不重寫（每次送件都寫檔是沒必要的 I/O）。
    """
    pem = (_load().get("ca_cert_pem") or "").strip()
    p = settings.data_dir / _CA_FILENAME
    if not pem:
        p.unlink(missing_ok=True)
        return None
    body = pem + "\n"
    try:
        if not p.is_file() or p.read_text(encoding="utf-8") != body:
            atomic_json.write_text(p, body) if hasattr(atomic_json, "write_text") else p.write_text(body, encoding="utf-8")
    except OSError:
        logger.warning("JTLW: 憑證寫不出去，這次改用系統信任庫", exc_info=True)
        return None
    return p


def ca_fingerprint() -> str:
    """憑證的 SHA-256 指紋（`AA:BB:…`）。

    **管理員要拿它跟對方公布的值核對再信任** —— 貼上一份沒核對過的憑證，
    等於把「我信任誰」這件事交給貼上來的那個人。算不出來回空字串。
    """
    import hashlib
    import ssl
    pem = (_load().get("ca_cert_pem") or "").strip()
    if not pem:
        return ""
    try:
        der = ssl.PEM_cert_to_DER_cert(pem + "\n")
    except (ValueError, TypeError):
        return ""
    h = hashlib.sha256(der).hexdigest().upper()
    return ":".join(h[i:i + 2] for i in range(0, len(h), 2))


def verify_tls():
    """給 httpx 的 `verify`：憑證檔路徑（字串）、或 True / False。

    **預設驗**。順序是「有自簽憑證就用它 → 否則走系統信任庫 →
    管理員明確關掉才不驗」。
    """
    path = ca_cert_path()
    if path is not None:
        return str(path)
    return bool(_load().get("verify_tls", True))


def audio_base_url() -> str:
    """對方來拉錄音檔時看到的我們。寫定的值，**不從請求推**（理由見模組說明）。

    一樣過 `safe_remote_base_url()` 正規化成 `scheme://host[:port]`：
    這個值會被組成網址交給對方，管理員手滑貼了一整條路徑進來的話，
    簽章網址會變成對不上的樣子 —— 而症狀是「對方說找不到檔案」。
    """
    from app.core.url_safety import safe_remote_base_url
    raw = (_load().get("audio_base_url") or "").strip()
    if not raw:
        return ""
    try:
        return safe_remote_base_url(raw).rstrip("/")
    except ValueError:
        logger.warning("JTLW 的錄音檔對外位址不合法，當成沒設定")
        return ""
