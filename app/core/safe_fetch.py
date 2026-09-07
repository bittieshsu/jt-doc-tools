"""只允許 http / https 的下載包裝。

## 為什麼

`urllib.request.urlopen()` **什麼 scheme 都吃** —— `file://` 會讀本機檔案、
`ftp://` 會連出去。全站有六處在下載東西（版本檢查、語言包、WinSW、VC++
runtime、健康檢查），其中**有幾處的網址是變數**：管理員設定的鏡像、
從設定檔讀來的來源。那些地方如果被設成 `file:///etc/passwd`，
`urlopen` 會老老實實把檔案讀給你。

bandit 的 B310 指的就是這件事。**擋法是白名單，不是黑名單** ——
只放行 http 與 https，其餘一律拒絕。

## 為什麼不直接關掉 bandit 的這條

把門檻從 Medium 調到 High 就看不到這 6 個了 —— 那是把問題藏起來，不是解決。
"""
from __future__ import annotations

import urllib.request
from typing import Any, Optional
from urllib.parse import urlparse

_ALLOWED = ("http", "https")


class UnsafeUrlScheme(ValueError):
    """網址用了 http / https 以外的協定。"""


def _check(url: str) -> str:
    scheme = (urlparse(url).scheme or "").lower()
    if scheme not in _ALLOWED:
        raise UnsafeUrlScheme(
            f"只接受 http / https 的網址（收到 {scheme or '(沒有協定)'}）")
    return url


def urlopen(target: Any, timeout: Optional[float] = None):
    """`urllib.request.urlopen` 的白名單版本。

    `target` 可以是網址字串，也可以是 `Request` 物件（版本檢查那邊要帶
    User-Agent 標頭，所以要收得下 Request）。
    """
    url = target if isinstance(target, str) else getattr(target, "full_url", "")
    _check(url)
    # scheme 已在上面用白名單擋過 —— 這裡是全站唯一真正呼叫 urlopen 的地方
    return urllib.request.urlopen(target, timeout=timeout)  # nosec B310


def urlopen_direct(target: Any, timeout: Optional[float] = None):
    """同上，但**不經過任何代理伺服器**。

    `urlopen` 會照 `http_proxy` / `https_proxy` 環境變數走。探測自己這台機器
    的 `healthz` 時那是錯的：企業環境的管理員 shell 常設著代理，而
    `no_proxy` 不見得列了 127.0.0.1 —— 於是「連自己」被送去代理伺服器、
    失敗，看起來就像服務沒起來。**本機探測一律直連。**
    """
    url = target if isinstance(target, str) else getattr(target, "full_url", "")
    _check(url)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return opener.open(target, timeout=timeout)


def urlretrieve(url: str, filename: str):
    """`urllib.request.urlretrieve` 的白名單版本。"""
    _check(url)
    # scheme 已在上面用白名單擋過
    return urllib.request.urlretrieve(url, filename)  # nosec B310
