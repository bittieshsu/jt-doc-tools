"""短效簽章網址 —— 讓**外部服務**取一個特定檔案，而不必交出任何 token。

## 為什麼不發 API Token（v1.15.93）

語音服務的 `source.type` 只能是 `url`：錄音檔不是我們上傳給對方，是
**對方拿我們給的網址自己來拉**。所以我們要給的是「一個可以取到這個檔案的網址」。

最順手的做法是發一把 API Token 給對方 —— **查過之後那條路不能走**：

    app/core/api_tokens.py   沒有 scope 概念
    _api_token_gate          一把 token 解鎖全部 /api/*

發出去就等於給了正式機上**所有 API** 的存取權（作業、通知、工作區、每一支
工具），只為了讓對方抓一個錄音檔。

簽章網址反過來：**沒有東西交出去**。網址本身就是授權，過期自動失效，
不需要撤銷，外洩的影響範圍是**那一個檔案到期為止**。

## 判準

* 簽的是 `檔案 id + 到期時間`，**兩者都在簽章裡** —— 只簽 id 的話，
  改 `exp` 就能無限延期。
* 驗不過一律當成**找不到**（404），不是 403。
  403 會告訴對方「這個 id 是存在的」，而 id 本身就是我們不想外流的東西。
* 密鑰用既有的 session 簽章密鑰（`auth_settings._ensure_secret`）——
  **不要再生一把**：多一把密鑰就多一個要備份、要輪替、會忘記的東西。
"""
from __future__ import annotations

import hashlib
import hmac
import time
from typing import Optional

from . import auth_settings

#: 預設有效期。**要短** —— 網址會出現在對方的日誌與我們的作業紀錄裡。
#: 兩小時足夠對方排隊 ＋ 拉檔（他們的 GPU 是共用的，尖峰會排隊）。
DEFAULT_TTL_SECONDS = 2 * 3600

_PREFIX = b"jtdt-signed-url-v1"


def _sig(kind: str, ident: str, exp: int) -> str:
    msg = b"\x00".join([_PREFIX, kind.encode(), ident.encode(), str(exp).encode()])
    return hmac.new(auth_settings._ensure_secret(), msg, hashlib.sha256).hexdigest()


def sign(kind: str, ident: str, *, ttl: int = DEFAULT_TTL_SECONDS) -> tuple[int, str]:
    """回 `(到期的 unix 秒數, 簽章)`。"""
    exp = int(time.time()) + max(1, int(ttl))
    return exp, _sig(kind, ident, exp)


def verify(kind: str, ident: str, exp: Optional[str], sig: Optional[str]) -> bool:
    """**任何一項不對就回 False** —— 呼叫端一律當成找不到。"""
    if not exp or not sig:
        return False
    try:
        exp_i = int(exp)
    except (TypeError, ValueError):
        return False
    if exp_i < int(time.time()):
        return False
    # **一定要 compare_digest** —— 一般的 `==` 會在第一個不同的位元組就回傳，
    # 逐位元組試出簽章是實際做得到的攻擊。
    return hmac.compare_digest(_sig(kind, ident, exp_i), str(sig))
