"""語音服務要拉的錄音檔 —— 用**短效簽章網址**授權，不交出任何 token。

外部語音服務的 `source.type` 只能是 `url`：錄音檔不是我們上傳給它，是**它拿我們
給的網址自己來拉**。為什麼不發 API Token、簽章要簽什麼，見
`app/core/signed_url.py` 的說明。

## 這條路為什麼不會被任何一道檢查擋掉（實測過）

| 關卡 | 對這個 GET（沒有 Authorization） |
|---|---|
| API Token 閘 | 沒帶 bearer → 落回 session（issue #52 之後的行為） |
| session 認證閘 | `/api/` 在 `_PUBLIC_PREFIXES` 裡 → 跳過 |
| CSRF | 只管不安全的方法，GET 不檢查 |

**POST 不是這樣**：CSRF 是 pure-ASGI 中介層、跑在路由比對之前，
任何沒帶 token 的 POST 一律 403（連不存在的路徑也是）。
所以 webhook 那條要另外處理，**不能拿這條的結論套過去**。
"""
from __future__ import annotations

import hashlib
import re

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from ..config import settings
from ..core import signed_url

router = APIRouter()

#: 檔案 id 的格式 —— **有固定格式就照格式驗**，不要每次重新判斷危不危險。
#: 路徑參數裡的 `%2F` 會被解碼成 `/`（v1.15.35 踩過），驗格式就一併擋掉了。
_ID_RE = re.compile(r"^[a-f0-9]{32}$")

_KIND = "audio"


def audio_dir():
    return settings.data_dir / "speech_audio"


def audio_path(file_id: str):
    return audio_dir() / f"{file_id}.bin"


def sign_url(file_id: str, base: str, *, ttl: int = signed_url.DEFAULT_TTL_SECONDS) -> str:
    """組一個對方拉得到的完整網址。

    **`base` 一定要是設定裡寫定的位址，不可以拿請求的 Host 來組** ——
    同一套服務有三種到達方式（內網直連 / 反向代理的網域 / 測試機），
    照請求的 Host 組的話，從外部網址進來的人送出的作業會帶著對外網域，
    然後被對方的來源白名單正確擋掉，而症狀是「有些人可以、有些人不行」。
    """
    exp, sig = signed_url.sign(_KIND, file_id, ttl=ttl)
    return f"{base.rstrip('/')}/api/speech/audio/{file_id}?exp={exp}&sig={sig}"


def file_facts(file_id: str) -> dict:
    """對方送件時要填的 `sha256` 與 `size_bytes`（它會核對，對不上就退件）。"""
    p = audio_path(file_id)
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return {"sha256": h.hexdigest(), "size_bytes": p.stat().st_size}


@router.get("/api/speech/audio/{file_id}")
async def speech_audio(file_id: str, request: Request):
    """**驗不過一律 404。**

    回 403 等於告訴對方「這個 id 是存在的」，而 id 本身就是我們不想外流的東西
    —— 四種失敗（格式不對 / 簽章錯 / 過期 / 檔案不在）在外面看起來要一模一樣。
    """
    if not _ID_RE.match(file_id or ""):
        raise HTTPException(404, "not found")
    if not signed_url.verify(_KIND, file_id,
                             request.query_params.get("exp"),
                             request.query_params.get("sig")):
        raise HTTPException(404, "not found")
    p = audio_path(file_id)
    if not p.is_file():
        raise HTTPException(404, "not found")
    return FileResponse(str(p), media_type="application/octet-stream",
                        headers={"Cache-Control": "no-store"})
