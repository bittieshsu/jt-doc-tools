"""事務機掃描取件：FUJIFILM 機內信箱的 WebDAV 請求格式（v1.16.99，**實驗功能，尚未在實機上驗證**）。

依據：原廠取件工具 Network Scanner Utility 3（2.0.60）的相容性分析（內部文件）。客戶實機 Apeos C5571
對我們原本的單次 `allprop` 請求，HTTPS 與 HTTP 都回 406；原廠工具的做法是：

* **探測**（`probe`）：只問 `d:resourcetype`、`Depth: 1`、帶 `X-Singlepage: On`、不帶帳密。
* **列文件**（`list`）：指定屬性、**省略** `X-Singlepage`（不是送 Off）。
* `Content-Type: text/xml`（不帶 charset）；XML 宣告 FUJIFILM 的命名空間，新舊機種是兩套。

這不是原廠公開的介面，也不代表各機型都相容。列文件挑哪些屬性、只在探測 406 時換一次舊格式，
都是本專案的整合選擇（流程在 `mfp_box.open_box`）。
"""
from __future__ import annotations

PROFILES = {
    "modern": {
        "sss": "http://www.fujifilm.com/fb/2021/04/ssm/storageService",
        "jt": "http://www.fujifilm.com/fb/2021/04/ssm/jobTemplate",
        "br": "http://www.fujifilm.com/fb/2021/04/backupService",
        "pb": "http://www.fujifilm.com/fb/2021/04/boxAccess",
    },
    "legacy": {
        "sss": "http://www.fujixerox.co.jp/2003/12/ssm/storageService",
        "jt": "http://www.fujixerox.co.jp/2003/12/ssm/jobTemplate",
        "br": "http://www.fujixerox.co.jp/2004/09/backupService",
        "pb": "http://www.fujixerox.co.jp/2005/06/boxAccess",
    },
}
# 列文件要的屬性：DAV 標準的加上分析裡看到的廠商屬性名稱。是本專案挑的組合，
# 不是原廠每個畫面都用這一組。
LIST_PROPERTIES = (
    "d:resourcetype", "d:displayname", "d:creationdate", "d:getlastmodified",
    "d:getcontenttype", "d:getcontentlength", "sss:Pages", "sss:Compression",
    "sss:ColorMode", "sss:DocStatusCode", "br:numofresources",
    "br:totalimagesize", "pb:deleteAfterGet",
)


def request_spec(profile: str, stage: str) -> tuple[bytes, str, dict[str, str]]:
    namespaces = PROFILES[profile]
    if stage not in ("probe", "list"):
        raise ValueError("invalid FUJIFILM request stage")
    properties = ("d:resourcetype",) if stage == "probe" else LIST_PROPERTIES
    attrs = 'xmlns:d="DAV:" ' + " ".join(
        f'xmlns:{prefix}="{uri}"' for prefix, uri in namespaces.items())
    body = ("<?xml version='1.0' encoding='utf-8'?>"
            f"<d:propfind {attrs}><d:prop>"
            + "".join(f"<{name}/>" for name in properties)
            + "</d:prop></d:propfind>").encode("utf-8")
    # 列文件不送這個標頭；探測（含認證那一次）送 On
    extra = {"X-Singlepage": "On"} if stage == "probe" else {}
    return body, "text/xml", extra


def response_profile(qnames: list[str], current: str) -> str:
    """從回應裡實際出現的屬性名稱（QName）判斷是新格式還是舊格式，不比對文件名稱裡的字串。

    只有 DAV 屬性、或兩套都有時，不能證明是舊機種 —— 維持目前的格式
    （刻意比原廠「沒看到新命名空間就當舊的」保守）。
    """
    observed = {q[1:].split("}", 1)[0] for q in qnames if q.startswith("{")}
    candidates = [name for name, ns in PROFILES.items() if observed & set(ns.values())]
    return candidates[0] if len(candidates) == 1 else current
