"""這台機器實際能收多大的檔案 —— 把散落各處的上限集中報出來。

## 為什麼要有這支

「我們可以拉多大的檔案進來？」這個問題目前沒有人答得出來：反向代理的上限在
別台機器的設定檔裡，各工具的上限寫死在十幾個 `.py` 檔中，工作區的額度在管理
設定裡。管理員唯一的辦法是自己去翻程式碼。

## 反向代理的上限：**沒有 header 可以讀，但問得到**

HTTP 沒有任何標準 header 會公告「這條路徑最大接受多少 body」——
`client_max_body_size` 是 nginx 自己的設定，不會出現在回應裡。

但 HTTP/1.1 的 `Expect: 100-continue` 正好是為這件事設計的：客戶端先送 header
（含 `Content-Length`）**不送 body**，對方回：

* `100 Continue` —— 收，可以開始傳
* `413 Request Entity Too Large` —— 不收

所以用二分搜尋問幾次就能得到實際上限，**完全不傳輸資料**。這比讀設定檔更有
意義：它反映的是**整條路徑**（可能有多層代理）真正的瓶頸，而不是某一台的設定。
"""
from __future__ import annotations

import socket
import ssl
from typing import Optional

#: 二分搜尋的上界。超過這個大小的上傳在實務上不會發生，繼續往上找只是浪費
#: 請求（而且很多代理對超大 Content-Length 會直接斷線而不是回 413）。
PROBE_CEILING_MB = 4096

#: 每次探測的逾時。只送 header 不送 body，正常在毫秒級回來。
PROBE_TIMEOUT_S = 8.0


def _ask_once(host: str, port: int, use_tls: bool, path: str,
              length: int, timeout: float = PROBE_TIMEOUT_S) -> Optional[int]:
    """宣告 `Content-Length: length` 但**不送 body**，回對方的狀態碼。

    連不上或對方不理會 `Expect` 時回 None —— 呼叫端要能分辨「問不到」和
    「問到了上限」，不可以把問不到當成沒有限制。
    """
    req = (
        f"POST {path} HTTP/1.1\r\n"
        f"Host: {host}\r\n"
        f"Content-Type: application/octet-stream\r\n"
        f"Content-Length: {length}\r\n"
        f"Expect: 100-continue\r\n"
        f"Connection: close\r\n\r\n"
    ).encode("ascii", "ignore")
    sock = None
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
        if use_tls:
            ctx = ssl.create_default_context()
            sock = ctx.wrap_socket(sock, server_hostname=host)
        sock.sendall(req)
        sock.settimeout(timeout)
        head = sock.recv(256).decode("latin-1", "replace")
        first = head.split("\r\n", 1)[0]
        parts = first.split(" ")
        return int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
    except Exception:
        return None
    finally:
        try:
            if sock is not None:
                sock.close()
        except Exception:
            pass


def probe_max_body_mb(host: str, port: int, use_tls: bool,
                      path: str = "/healthz") -> dict:
    """二分搜尋出實際可上傳的大小（MB）。

    回 `{"ok": bool, "max_mb": int|None, "unlimited": bool, "detail": str,
        "requests": int}`。

    **問不到時要說問不到**，不要回一個看起來像答案的數字 —— 管理員照著一個
    錯的數字去設定，比沒有這個功能更糟。
    """
    probes = 0

    def accepts(mb: int) -> Optional[bool]:
        nonlocal probes
        probes += 1
        code = _ask_once(host, port, use_tls, path, mb * 1024 * 1024)
        if code is None:
            return None
        # 100 = 收；413 = 太大。其他狀態碼（401/403/404…）代表對方**沒有**
        # 因為大小拒絕 —— 那也算「這個大小過得了大小這一關」。
        return code != 413

    # 先確認最小的請求問得通，否則後面的二分搜尋全是垃圾
    base = accepts(1)
    if base is None:
        return {"ok": False, "max_mb": None, "unlimited": False, "requests": probes,
                "detail": "對方沒有回應 100-continue（可能是 HTTP/2、"
                          "或中間有不支援的代理）—— 無法用這個方式問出上限"}
    if base is False:
        return {"ok": True, "max_mb": 0, "unlimited": False, "requests": probes,
                "detail": "連 1 MB 都被拒絕 —— 反向代理的上限設得極小"}

    # 指數往上找到第一個被拒的大小
    lo, hi = 1, 2
    while hi <= PROBE_CEILING_MB:
        got = accepts(hi)
        if got is None:
            return {"ok": False, "max_mb": None, "unlimited": False,
                    "requests": probes,
                    "detail": f"問到 {hi} MB 時對方不再回應 —— 結果不可信"}
        if not got:
            break
        lo, hi = hi, hi * 2
    else:
        return {"ok": True, "max_mb": None, "unlimited": True, "requests": probes,
                "detail": f"到 {PROBE_CEILING_MB} MB 都沒有被拒絕 —— "
                          "這條路徑上沒有設定大小限制"}

    # 二分收斂
    while hi - lo > 1:
        mid = (lo + hi) // 2
        got = accepts(mid)
        if got is None:
            break
        if got:
            lo = mid
        else:
            hi = mid
    return {"ok": True, "max_mb": lo, "unlimited": False, "requests": probes,
            "detail": f"實測 {lo} MB 收、{hi} MB 拒"}


def app_side_limits() -> list[dict]:
    """**應用程式這一側**已知的大小限制，集中列出。

    這些值原本散在十幾個檔案裡，管理員只能翻程式碼才知道。`configurable`
    標示能不能在管理介面調 —— 說「可調」卻其實寫死在程式裡最糟。
    """
    from . import workspace as ws
    from .branding import MAX_LOGO_BYTES
    from .job_autosave import _AUTO_MAX_BYTES

    s = ws.get_settings() if hasattr(ws, "get_settings") else {}
    per_user = int(s.get("per_user_quota_mb") or 0)
    max_file = int(s.get("max_file_mb") or 0)

    def mb(n: int) -> float:
        return round(n / 1024 / 1024, 1)

    return [
        {"key": "app_global", "label": "應用程式全域上傳上限",
         "value_mb": None, "configurable": False,
         # **不要在這裡寫 markdown** —— 這個字串是丟進 HTML 樣板顯示的，
         # 星號會原樣印出來（2026-08-27 使用者截圖抓到）。要強調就靠措辭。
         "note": "目前沒有全域上限 —— 大小由反向代理與各工具自己的上限決定。"
                 "直連本機埠時等於沒有限制。"},
        {"key": "ws_file", "label": "工作區單檔上限",
         "value_mb": max_file if max_file > 0 else None, "configurable": True,
         "note": "0 或 -1 = 不限。在「工作區設定」調整。"},
        {"key": "ws_quota", "label": "工作區每人總額度",
         "value_mb": per_user if per_user > 0 else None, "configurable": True,
         "note": "0 或 -1 = 不限。在「工作區設定」調整。"},
        {"key": "autosave", "label": "作業完成自動存入工作區的上限",
         "value_mb": mb(_AUTO_MAX_BYTES), "configurable": False,
         "note": "超過就不自動存（仍可手動按「存至工作區」）。"},
        {"key": "office_convert", "label": "辦公文件格式互轉",
         "value_mb": 200, "configurable": False, "note": "單檔上限。"},
        {"key": "einvoice", "label": "電子發票處理",
         "value_mb": 20, "configurable": False, "note": "單檔上限。"},
        {"key": "markdown", "label": "Markdown 轉文書",
         "value_mb": 5, "configurable": False, "note": "原始 Markdown 大小上限。"},
        {"key": "asset", "label": "資產（印章 / 簽名 / 浮水印）上傳",
         "value_mb": 200, "configurable": False, "note": "臨時印章另有 5 MB 上限。"},
        {"key": "logo", "label": "企業 Logo",
         "value_mb": mb(MAX_LOGO_BYTES), "configurable": False, "note": ""},
        {"key": "vat_db", "label": "統編資料庫匯入",
         "value_mb": 1024, "configurable": False, "note": "政府開放資料本來就大。"},
        {"key": "settings_import", "label": "設定匯入",
         "value_mb": 512, "configurable": False,
         "note": "單一檔案 512 MB、解壓後總量 2 GB。"},
    ]
