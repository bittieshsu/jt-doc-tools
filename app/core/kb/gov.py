"""知識庫：匯入政府公開資料（Beta）—— 全國法規資料庫、國發會行政規則、行政院文書處理釋例。

## 這支在做什麼

「公文撰擬」的參考資料要有法規與文書規範。這裡把三個**開放授權**的政府資料來源
變成知識庫的資料集：

| 群組 | 來源 | 用途 | 一個資料集是 |
|---|---|---|---|
| `moj` | 法務部全國法規資料庫 Open API（法律 ＋ 命令） | 業務依據 | 一部法規（pcode） |
| `ndc` | 國發會主管行政規則 XML（data.gov.tw 39506） | 文書規範（格式與用語參考） | 一則規則（`id=`） |
| `ey` | 行政院〈文書處理相關釋例〉兩份 PDF | 文書規範（格式與用語參考） | 一份 PDF |

〈文書處理手冊〉**不下載**：手冊版權頁寫明保留所有權利（要行政院書面同意），畫面只給
官方連結，並說明機關可以用一般的上傳放自己的那一份當內部參考。

## 什麼時候會連外

**只有管理員按按鈕的時候**（同 `official_doc_sources`）：沒有排程、import 時不碰網路、
開頁面不連外。下載走 `safe_fetch.fetch_public()`（只准公開網路、每次重新導向重驗）。
網址可以改（資料搬家、改用舊鏡像），「還原預設」放回內建的。

## 流程

畫面上只有一顆「**下載並匯入**」（2026-10-08 使用者：「請改為下載並匯入 不要分兩步」，動作 `update`）：

1. **下載**：把整包（法規 6 MB ＋ 26 MB 的 zip）抓下來、解析成**清單**（名稱、
   代碼、異動日期、是否廢止）。全國法規資料庫的 robots.txt 不准爬單一法規的頁面，
   所以一律下載整包、在本機挑。這次全部下載失敗、但這台有上次的清單 → 照那份清單接著匯入，
   結果裡講明（`summary.stale_list`）；連一份清單都沒有才整件失敗。
2. **匯入勾選的項目**：每一部建一個資料集、一個版本（一條一段），處理完**自動啟用**。
   比對**每部法規自己的異動日期**（不看整包的日期 —— API 比網站晚一、兩週，整包每天都會變）；
   日期變了才建新版本、啟用新的、舊的停用但留著（舊草稿引用的段落還找得到）。已廢止的不刪：停用、標「已廢止」。
3. **挑選**：清單下載之後，管理員翻整份清單、搜尋、依位階篩選、全選，勾要的再按一次「下載並匯入」。
   預設勾 20 部公文相關的法規（只是建議，第一次就會匯入）。勾太多（全部法規約 2,300 萬字）會把檢索淹掉 —— 超過門檻要確認。

不能連外時「上傳」＝離線版的下載並匯入（`install`，換上清單之後一樣接著匯入）。
`download`（只下載）與 `import`（只匯入）兩個動作 API 照舊收，畫面不再分兩步。

## 授權與顯名

三個來源都是「政府資料開放授權條款－第1版」：可以重製、改作、散布，條件是**顯名**。
顯名文字由 `attribution_for()` 產生，**每個匯入的版本各存一份**（`kb_gov_versions`）。

## 存放（`data/knowledge/gov/`）

* `config.json` —— 改過的網址與選取的項目（沒改過就沒有這個檔）。
* `packages/<代碼>/current/` —— 下載的原始檔（`package.bin`）與清單（`index.json`）；
  先寫 `.staging-*`、全部驗過才換上去，失敗時舊的一個位元組都不動。
* `status.json` —— 每個群組最後一次動作的結果。
都在知識庫的資料夾裡，所以跟知識庫一樣**不進設定備份**（可以重新下載）。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from ...logging_setup import get_logger
from .. import atomic_json, cjk_fts, safe_fetch
from ..job_labels import KB_JOB_ID
from . import gov_packages as gp
from . import law_text, store

logger = get_logger(__name__)

LICENSE = "政府資料開放授權條款－第1版"
LICENSE_URL = "https://data.gov.tw/license"

# ---------------------------------------------------------------- 來源（內建）
#: 群組：一張卡片。`kind`：laws（結構化條文）/ rules（全文）/ pdfs。
GROUPS: dict[str, dict] = {
    "moj": {
        "name": "全國法規資料庫",
        "publisher": "法務部",
        "kind": "laws",
        "category": "business_law",
        "packages": ("moj-law", "moj-order"),
        "home": "https://law.moj.gov.tw/",
        "about": "中文法規（法律、命令）。整包下載後在本機挑選，每一部法規一個資料集、一條一段。",
        # 公文相關的 20 部（只是預設勾選的建議；下載後會逐一確認代碼真的存在）
        "defaults": ("A0030018", "A0030031", "A0030049", "A0030011", "A0030012", "I0060003",
                     "I0060005", "A0030055", "A0030133", "A0030134", "A0030085", "J0080037",
                     "I0020026", "I0050021", "I0050022", "A0030126", "A0030089", "A0030020",
                     "A0030210", "A0010036"),
        "default_by": "key",
        # 還沒下載清單時畫面也要叫得出名稱（下載後以清單上的名稱為準）
        "default_names": {
            "A0030018": "公文程式條例", "A0030031": "機關公文傳真作業辦法",
            "A0030049": "機關公文電子交換作業辦法", "A0030011": "印信條例",
            "A0030012": "印信製發啟用管理換發及廢舊印信繳銷辦法", "I0060003": "國家機密保護法",
            "I0060005": "國家機密保護法施行細則", "A0030055": "行政程序法",
            "A0030133": "中央法規標準法", "A0030134": "檔案法", "A0030085": "檔案法施行細則",
            "J0080037": "電子簽章法", "I0020026": "政府資訊公開法", "I0050021": "個人資料保護法",
            "I0050022": "個人資料保護法施行細則", "A0030126": "機密檔案管理辦法",
            "A0030089": "機關檔案保存年限及銷毀辦法", "A0030020": "訴願法",
            "A0030210": "行政罰法", "A0010036": "中央行政機關組織基準法",
        },
    },
    "ndc": {
        "name": "國家發展委員會主管行政規則",
        "publisher": "國家發展委員會",
        "kind": "rules",
        "category": "writing_rules",
        "packages": ("ndc-rules-1", "ndc-rules-2"),
        "home": "https://data.gov.tw/dataset/39506",
        "about": "國發會主管的行政規則（含文書流程、文書格式、公文電子交換的規範）。只取規則本文，附件連結只列出不下載。",
        "defaults": ("文書流程管理作業規範", "政府文書格式參考規範",
                     "文書及檔案管理電腦化作業規範", "公文電子交換系統資訊安全管理規範"),
        "default_by": "name",
    },
    "ey": {
        "name": "行政院文書處理相關釋例",
        "publisher": "行政院綜合業務處",
        "kind": "pdfs",
        "category": "writing_rules",
        "packages": ("ey-mailbox", "ey-interp"),
        "home": ("https://www.ey.gov.tw/Page/F0CD366C64B5A15C/"
                 "43164b06-c95c-48d5-bcb6-566c3296d705"),
        "about": "行政院對文書處理問題的答覆（院長電子信箱、函釋）。檔案網址每次更新都會換，換了請到行政院網頁複製新的網址貼上。",
        "defaults": ("ey-mailbox", "ey-interp"),
        "default_by": "key",
    },
}

#: 下載檔。`max_mb` 是下載上限（實際：法律 zip 6 MB、命令 zip 26 MB、規則 XML 0.9 MB、釋例 PDF 3.7 MB）。
PACKAGES: dict[str, dict] = {
    "moj-law": {"group": "moj", "name": "中文法規・法律",
                "url": "https://law.moj.gov.tw/api/ch/law/json",
                "alt_url": "https://sendlaw.moj.gov.tw/PublicData/GetFile.ashx?DType=XML&AuData=CF",
                "max_mb": 60, "file_label": "中文法規法律資料檔"},
    "moj-order": {"group": "moj", "name": "中文法規・命令",
                  "url": "https://law.moj.gov.tw/api/ch/order/xml",
                  "alt_url": "https://sendlaw.moj.gov.tw/PublicData/GetFile.ashx?DType=XML&AuData=CM",
                  "max_mb": 80, "file_label": "中文法規命令資料檔"},
    "ndc-rules-1": {"group": "ndc", "name": "行政規則（行政程序法第159條第2項第1款）",
                    "url": ("https://ws.ndc.gov.tw/Download.ashx?u=LzAwMS9hZG1pbmlzdHJhdG9yLzEwL3Jl"
                            "bGZpbGUvNTc4MS8yNjE5Mi9lZjliMDc3Yi04YzI2LTRlYzctYTE5My1mOTFkNDU2NjRiNmIu"
                            "eG1s&n=MTU5SUkxLnhtbA%3d%3d&icon=..xml"),
                    "alt_url": "", "max_mb": 20},
    "ndc-rules-2": {"group": "ndc", "name": "行政規則（行政程序法第159條第2項第2款）",
                    "url": ("https://ws.ndc.gov.tw/001/administrator/10/relfile/5781/26192/"
                            "18370f9e-4372-4b12-8224-3a2f4c54921e.xml"),
                    "alt_url": "", "max_mb": 20},
    "ey-mailbox": {"group": "ey", "name": "文書處理相關釋例（院長電子信箱）",
                   "url": "https://www.ey.gov.tw/File/D7D7E4A102F4B02A?A=C",
                   "alt_url": "", "max_mb": 40},
    "ey-interp": {"group": "ey", "name": "文書處理相關釋例（函釋）",
                  "url": "https://www.ey.gov.tw/File/703AEC181C91221D?A=C",
                  "alt_url": "", "max_mb": 40},
}

#: 只給連結、不下載的（版權頁保留所有權利）。
LINK_ONLY: tuple[dict, ...] = (
    {"id": "ey-handbook", "name": "文書處理手冊", "publisher": "行政院",
     "edition": "第7版（112年9月）",
     "url": "https://www.ey.gov.tw/Page/F0CD366C64B5A15C/ecb75289-a85d-45be-9fb0-0fa64c302b54",
     "note": "手冊的版權頁寫明行政院保有所有權利（重製需書面同意），所以這裡不下載。"
             "機關可以用一般的「上傳文件」放自己的那一份當內部參考（行政院保有所有權利，僅供機關內部參考）。"},
)

#: 連線（每次讀取）逾時與整次下載的上限。政府網站有時很慢。
FETCH_TIMEOUT_S = 60.0
FETCH_DEADLINE_S = 900.0
#: 選取超過這麼多部（或這麼多字）要確認 —— 全部法規約 2,300 萬字、16 萬條，
#: 全匯進去的話任何問題都會查到一堆不相干的條文。
WARN_ITEMS = 100
WARN_CHARS = 1_500_000
#: 選取的硬上限。
MAX_SELECTED = 1000

MESSAGES = {
    "not_found": "找不到這個資料來源",
    "busy": "已經有一件政府公開資料的作業在進行，請等它完成",
    "not_downloaded": "還沒有下載資料，請先按「下載並匯入」",
    "too_many": "最多只能選 {0} 項",
    "confirm": "選了 {0} 項（約 {1} 字），量很大：檢索時會查到很多不相干的條文。確定要這樣選嗎？",
    "bad_key": "選取的項目格式不正確",
    "unknown_key": "有選取的項目不在已下載的清單裡",
    "unexpected": "處理失敗（未預期的錯誤，詳細原因已寫入服務記錄）",
    "cancelled": "已取消",
}


class GovError(ValueError):
    """操作被拒絕或失敗的原因（訊息給管理員看）。"""


class GovNotFound(GovError):
    pass


class GovBusy(GovError):
    pass


class NeedsConfirm(GovError):
    """選取的量很大，要管理員確認。"""

    def __init__(self, msg: str, count: int, chars: int):
        super().__init__(msg)
        self.count, self.chars = count, chars


# ---------------------------------------------------------------- 路徑
def _root() -> Path:
    return store.kb_dir() / "gov"


def _config_path() -> Path:
    return _root() / "config.json"


def _status_path() -> Path:
    return _root() / "status.json"


def _pkg_dir(pid: str) -> Path:
    """**只收內建的代碼**（直接組進路徑）。"""
    if pid not in PACKAGES:
        raise GovNotFound(MESSAGES["not_found"])
    return _root() / "packages" / pid


def _group_or_404(gid: str) -> dict:
    g = GROUPS.get(gid) if isinstance(gid, str) else None
    if g is None:
        raise GovNotFound(MESSAGES["not_found"])
    return g


# ---------------------------------------------------------------- 設定（網址與選取）
_CFG_LOCK = threading.RLock()


def _read_config() -> dict:
    try:
        obj = json.loads(_config_path().read_text(encoding="utf-8"))
        return obj if isinstance(obj, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        logger.warning("知識庫政府公開資料設定讀不到，用預設值：%s", type(e).__name__)
        return {}


def _write_config(cfg: dict) -> None:
    cfg["version"] = 1
    atomic_json.write_json(_config_path(), cfg)


def _clean_url(v: Any, *, required: bool) -> str:
    s = str(v or "").strip()
    if not s:
        if required:
            raise GovError("請填下載網址")
        return ""
    try:
        return safe_fetch.normalize_public_url(s)
    except (safe_fetch.BlockedDestination, safe_fetch.UnsafeUrlScheme) as e:
        raise GovError(f"網址不正確：{e}") from None


def get_package(pid: str) -> dict:
    """一個下載檔目前的設定（內建值 ＋ 管理員改過的網址）。"""
    if pid not in PACKAGES:
        raise GovNotFound(MESSAGES["not_found"])
    base = PACKAGES[pid]
    over = (_read_config().get("packages") or {}).get(pid) or {}
    out = {"id": pid, **base, "default_url": base["url"], "default_alt_url": base["alt_url"]}
    for k in ("url", "alt_url"):
        v = over.get(k)
        if isinstance(v, str):
            try:
                out[k] = _clean_url(v, required=(k == "url")) if v or k == "url" else ""
            except GovError:
                pass       # 手動改壞的設定檔：退回內建值
    out["customized"] = out["url"] != base["url"] or out["alt_url"] != base["alt_url"]
    return out


def set_package_urls(pid: str, fields: dict) -> dict:
    """改網址（`url` 必填；`alt_url` 可以清空）。"""
    if pid not in PACKAGES:
        raise GovNotFound(MESSAGES["not_found"])
    if not isinstance(fields, dict):
        raise GovError("資料格式不正確")
    with _CFG_LOCK:
        cfg = _read_config()
        pk = cfg.setdefault("packages", {})
        cur = dict(pk.get(pid) or {})
        if "url" in fields:
            cur["url"] = _clean_url(fields.get("url"), required=True)
        if "alt_url" in fields:
            cur["alt_url"] = _clean_url(fields.get("alt_url"), required=False)
        pk[pid] = cur
        _write_config(cfg)
    return get_package(pid)


def reset_package(pid: str) -> dict:
    """網址還原成內建的（已下載的資料不動）。"""
    if pid not in PACKAGES:
        raise GovNotFound(MESSAGES["not_found"])
    with _CFG_LOCK:
        cfg = _read_config()
        if pid in (cfg.get("packages") or {}):
            cfg["packages"].pop(pid, None)
            _write_config(cfg)
    return get_package(pid)


_KEY_RE = re.compile(r"^(?:[A-Z0-9]{1,20}|N:[^\x00-\x1f]{1,80})$")


def _valid_key(gid: str, key: Any) -> bool:
    if not isinstance(key, str):
        return False
    if GROUPS[gid]["kind"] == "pdfs":
        return key in GROUPS[gid]["packages"]
    return bool(_KEY_RE.match(key))


def _resolve_defaults(gid: str, idx: dict[str, dict]) -> list[str]:
    g = GROUPS[gid]
    if g["default_by"] == "key":
        return list(g["defaults"])
    out = []
    for name in g["defaults"]:
        hits = [k for k, e in idx.items() if e["name"] == name]
        hits.sort(key=lambda k: (idx[k]["abolished"], -int(idx[k]["modified"] or 0)))
        if hits:
            out.append(hits[0])
    return out


def get_selection(gid: str) -> list[str]:
    """目前選取的項目代碼。沒選過 ＝ 內建的預設（依名稱的預設要下載後才對得到代碼）。"""
    _group_or_404(gid)
    sel = (_read_config().get("selected") or {}).get(gid)
    if isinstance(sel, list):
        return [k for k in sel if _valid_key(gid, k)]
    return _resolve_defaults(gid, merged_index(gid))


def selection_is_default(gid: str) -> bool:
    return not isinstance((_read_config().get("selected") or {}).get(gid), list)


def set_selection(gid: str, keys: Any, *, confirm: bool = False) -> list[str]:
    """存選取的項目。量很大時丟 `NeedsConfirm`（帶 `confirm=True` 再送一次）。"""
    _group_or_404(gid)
    if not isinstance(keys, list):
        raise GovError(MESSAGES["bad_key"])
    out: list[str] = []
    for k in keys:
        if not _valid_key(gid, k):
            raise GovError(MESSAGES["bad_key"])
        if k not in out:
            out.append(k)
    if len(out) > MAX_SELECTED:
        raise GovError(MESSAGES["too_many"].replace("{0}", str(MAX_SELECTED)))
    idx = merged_index(gid)
    if GROUPS[gid]["kind"] != "pdfs":
        prev = set(get_selection(gid))
        bad = [k for k in out if k not in idx and k not in prev]
        if bad:
            raise GovError(MESSAGES["unknown_key"])
    chars = sum(idx[k]["chars"] for k in out if k in idx)
    if not confirm and (len(out) > WARN_ITEMS or chars > WARN_CHARS):
        raise NeedsConfirm(MESSAGES["confirm"].replace("{0}", str(len(out)))
                           .replace("{1}", f"{chars:,}"), len(out), chars)
    with _CFG_LOCK:
        cfg = _read_config()
        cfg.setdefault("selected", {})[gid] = out
        _write_config(cfg)
    return out


def reset_selection(gid: str) -> list[str]:
    """選取的項目回到內建的預設建議。"""
    _group_or_404(gid)
    with _CFG_LOCK:
        cfg = _read_config()
        if gid in (cfg.get("selected") or {}):
            cfg["selected"].pop(gid, None)
            _write_config(cfg)
    return get_selection(gid)


# ---------------------------------------------------------------- 清單（已下載的）
_CACHE: dict[str, tuple] = {}
_CACHE_LOCK = threading.Lock()


def _stamp(p: Path) -> Optional[tuple]:
    try:
        st = p.stat()
    except OSError:
        return None
    return (st.st_ino, st.st_mtime_ns, st.st_size)


def package_index(pid: str) -> Optional[dict]:
    """一個下載檔的清單（沒下載過回 None）。依檔案的 inode / mtime 快取。"""
    p = _pkg_dir(pid) / "current" / "index.json"
    stamp = _stamp(p)
    if stamp is None:
        with _CACHE_LOCK:
            _CACHE.pop(pid, None)
        return None
    with _CACHE_LOCK:
        hit = _CACHE.get(pid)
        if hit and hit[0] == stamp:
            return hit[1]
    try:
        obj = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(obj, dict) or not isinstance(obj.get("items"), list):
        return None
    with _CACHE_LOCK:
        _CACHE[pid] = (stamp, obj)
    return obj


def merged_index(gid: str) -> dict[str, dict]:
    """群組裡所有已下載的項目：代碼 → 清單項（同一個代碼出現在兩包時取前面那包）。"""
    out: dict[str, dict] = {}
    for pid in GROUPS[gid]["packages"]:
        idx = package_index(pid)
        for e in (idx or {}).get("items") or []:
            if isinstance(e, dict) and e.get("key") and e["key"] not in out:
                out[e["key"]] = e
    return out


#: 「全選」一次最多回幾個代碼（全國法規資料庫整份約 1.2 萬項 —— 取消全選要知道整個範圍）
KEYS_ONLY_MAX = 20000


def search(gid: str, q: str = "", *, limit: int = 50, offset: int = 0, browse: bool = False,
           level: str = "", keys_only: bool = False) -> dict:
    """在已下載的清單裡找（名稱逐字、或代碼開頭）。空白分開的詞要全部出現。

    * `q` 空白：`browse` 為真時**整份清單**照名稱排（翻頁看；2026-10-08 使用者：「這邊說169項，
      可是我看只有四項可以勾」）—— 已廢止的排最後；`browse` 為假時只回選取的（舊行為）。
    * `level`：只看這一種「法規位階」（法律 / 命令…）；回應的 `levels` 是整份清單各有幾項（畫面的篩選）。
    * `keys_only`：「全選 / 取消全選」用 —— 回這個範圍（不分頁）的全部代碼；不超過選取上限時
      另附名稱與字數（畫面算「已選幾項、約幾字」要用）。
    """
    g = _group_or_404(gid)
    try:
        limit = max(1, min(int(limit), 200))
    except (TypeError, ValueError):
        limit = 50
    try:
        offset = max(0, int(offset))
    except (TypeError, ValueError):
        offset = 0
    level = (level or "").strip()[:40]
    idx = merged_index(gid)
    sel = get_selection(gid)
    selset = set(sel)
    q = (q or "").strip()[:100]
    terms = [cjk_fts.normalize(t) for t in q.split() if t.strip()]
    # 位階用顯示的名稱比對與統計（國發會的檔案寫的是代碼，見 `law_text.level_label`）
    lv_of = {k: law_text.level_label(idx[k].get("level", "")) for k in idx}
    pool = [k for k in idx if not level or lv_of[k] == level]
    if terms:
        first = terms[0]
        hits = []
        for k in pool:
            n = cjk_fts.normalize(idx[k]["name"])
            if all(t in n for t in terms) or (len(terms) == 1 and k.lower().startswith(first)):
                hits.append(k)
        hits.sort(key=lambda k: (idx[k]["abolished"],
                                 0 if cjk_fts.normalize(idx[k]["name"]) == "".join(terms) else 1,
                                 len(idx[k]["name"]), k))
    elif browse or keys_only:
        hits = sorted(pool, key=lambda k: (bool(idx[k].get("abolished")),
                                           cjk_fts.normalize(idx[k]["name"]), k))
    else:
        hits = [k for k in sel if k in idx and (not level or lv_of[k] == level)]
    total = len(hits)
    levels: dict[str, int] = {}
    level_notes: dict[str, str] = {}
    for k, e in idx.items():
        lv = lv_of[k]
        if lv:
            levels[lv] = levels.get(lv, 0) + 1
            note = law_text.level_basis(e.get("level", ""))
            if note:
                level_notes[lv] = note
    base = {"group": gid, "query": q, "total": total, "offset": offset, "level": level,
            "levels": dict(sorted(levels.items(), key=lambda x: (-x[1], x[0]))),
            "level_notes": level_notes,
            "downloaded": bool(idx), "kind": g["kind"], "max_selected": MAX_SELECTED}
    if keys_only:
        keys = hits[:KEYS_ONLY_MAX]
        out = {**base, "keys": keys, "truncated": total > len(keys)}
        if total <= MAX_SELECTED:
            out["items"] = [{"key": k, "name": idx[k]["name"], "chars": idx[k].get("chars", 0),
                             "level": lv_of[k], "abolished": bool(idx[k].get("abolished"))}
                            for k in keys]
        return out
    keys = hits[offset:offset + limit]
    items = store.gov_items(gid)
    return {**base, "limit": limit,
            "items": [_item_out(gid, idx[k], k in selset, items.get(k)) for k in keys]}


def _item_out(gid: str, e: dict, selected: bool, st: Optional[dict]) -> dict:
    out = {"key": e["key"], "name": e["name"], "level": law_text.level_label(e.get("level", "")),
           "category": e.get("category", ""), "modified": e.get("modified", ""),
           "modified_roc": law_text.roc_date(e.get("modified", "")),
           "abolished": bool(e.get("abolished")), "articles": e.get("articles", 0),
           "chars": e.get("chars", 0), "selected": selected, "pkg": e.get("pkg", ""),
           "imported": False, "needs_update": False, "dataset_id": None}
    if st:
        out["imported"] = bool(st.get("dataset_id"))
        out["dataset_id"] = st.get("dataset_id")
        out["imported_modified"] = st.get("modified_on", "")
        out["needs_update"] = out["imported"] and st.get("modified_on") != e.get("modified")
    return out


def selected_items(gid: str) -> list[dict]:
    """選取的項目（給畫面列「已選」那一區）。

    不在清單裡的也列出來：**清單已經下載**卻找不到 ＝ `missing`（代碼改了或已移除）；
    **還沒下載** ＝ `pending`（不是錯誤，只是還不知道細節）。"""
    g = GROUPS[gid]
    idx = merged_index(gid)
    items = store.gov_items(gid)
    names = dict(g.get("default_names") or {})
    out = []
    for k in get_selection(gid):
        e = idx.get(k)
        if e is None:
            st = items.get(k) or {}
            name = st.get("name") or names.get(k) or PACKAGES.get(k, {}).get("name") or k
            downloaded = (package_index(k) is not None) if g["kind"] == "pdfs" else bool(idx)
            out.append({"key": k, "name": name, "missing": downloaded, "pending": not downloaded,
                        "selected": True, "imported": bool(st.get("dataset_id")),
                        "dataset_id": st.get("dataset_id")})
        else:
            out.append(_item_out(gid, e, True, items.get(k)))
    return out


# ---------------------------------------------------------------- 顯名
def _roc_from_update(s: str) -> str:
    m = re.search(r"(\d{4})\D(\d{1,2})\D(\d{1,2})", s or "") or re.match(r"(\d{4})(\d{2})(\d{2})", s or "")
    if not m:
        return ""
    return law_text.roc_date(f"{int(m.group(1)):04d}{int(m.group(2)):02d}{int(m.group(3)):02d}")


def attribution_for(gid: str, *, pkg: Optional[str] = None, update_date: str = "") -> str:
    """顯名文字（政府資料開放授權條款要求：提供機關、資料名稱、授權條款版本）。"""
    roc = _roc_from_update(update_date)
    if gid == "moj":
        label = PACKAGES.get(pkg or "", {}).get("file_label") or "中文法規法律／命令資料檔"
        when = f"（資料更新日期 {roc}）" if roc else ""
        return (f"資料來源：法務部全國法規資料庫（https://law.moj.gov.tw），{label}{when}，"
                f"依{LICENSE}（{LICENSE_URL}）提供。法規內容以各主管機關公布者為準。")
    if gid == "ndc":
        return (f"國家發展委員會，國家發展委員會主管行政規則（政府資料開放平臺資料集 39506），"
                f"依{LICENSE}提供。")
    if gid == "ey":
        if roc:
            when = f"（{roc.replace('民國', '')}更新）"
        else:
            when = f"（更新日期不詳，{law_text.roc_date(time.strftime('%Y%m%d')).replace('民國', '')}下載）"
        return (f"行政院綜合業務處，《文書處理相關釋例》{when}，取自行政院全球資訊網，"
                f"依{LICENSE}提供。")
    raise GovNotFound(MESSAGES["not_found"])


# ---------------------------------------------------------------- 下載與安裝
def _sweep(pdir: Path) -> None:
    for p in list(pdir.glob(".staging-*")) + list(pdir.glob(".old-*")):
        shutil.rmtree(p, ignore_errors=True)


_IO_LOCK = threading.RLock()


def install_package(pid: str, raw: bytes, *, origin: str, final_url: str = "",
                    filename: str = "", cancelled: Optional[Callable[[], bool]] = None) -> dict:
    """驗證並換上一個下載檔（下載與手動上傳共用）。失敗丟 `GovError`，舊的不動。"""
    pkg = PACKAGES.get(pid)
    if pkg is None:
        raise GovNotFound(MESSAGES["not_found"])
    if not raw:
        raise GovError("檔案是空的")
    if len(raw) > pkg["max_mb"] * 1024 * 1024:
        raise GovError(f"檔案太大（上限 {pkg['max_mb']} MB）")
    kind = GROUPS[pkg["group"]]["kind"]
    pdir = _pkg_dir(pid)
    pdir.mkdir(parents=True, exist_ok=True)
    _sweep(pdir)
    staging = pdir / f".staging-{secrets.token_hex(6)}"
    staging.mkdir()
    try:
        bin_path = staging / "package.bin"
        bin_path.write_bytes(raw)
        sha = hashlib.sha256(raw).hexdigest()
        update_date = ""
        items: list[dict] = []
        if kind == "pdfs":
            if not raw.startswith(b"%PDF"):
                raise GovError("不是 PDF 檔（行政院釋例的網址可能換了，請到行政院網頁複製新的網址）")
            try:
                info = gp.pdf_info(raw)
            except gp.PackageError as e:
                raise GovError(str(e)) from None
            update_date = info["updated"]
            items.append({"key": pid, "name": pkg["name"], "level": "", "category": "",
                          "modified": info["updated"] or "sha:" + sha[:12],
                          "effective": "", "abolished": False, "articles": 0,
                          "chars": 0, "pages": info["pages"], "pkg": pid})
        else:
            uds: list[str] = []
            seen: set[str] = set()
            structured = text_only = 0
            try:
                for rec in gp.iter_records(bin_path, on_update_date=uds.append,
                                           cancelled=cancelled):
                    if rec["key"] in seen:
                        continue
                    seen.add(rec["key"])
                    if rec.get("text") is not None:
                        text_only += 1
                    elif rec.get("articles"):
                        structured += 1
                    items.append(gp.index_entry(rec, pid))
            except gp.PackageError as e:
                raise GovError(str(e)) from None
            update_date = uds[0] if uds else ""
            if kind == "laws" and text_only > structured:
                raise GovError("看起來不是全國法規資料庫的法規資料（沒有條文結構）")
            if kind == "rules" and structured > text_only:
                raise GovError("看起來不是國發會行政規則的資料")
        index = {"pkg": pid, "group": pkg["group"], "kind": kind, "count": len(items),
                 "update_date": update_date, "sha256": sha, "size": len(raw),
                 "origin": origin, "final_url": final_url, "filename": filename[:120],
                 "installed_at": time.time(), "items": items}
        atomic_json.write_json(staging / "index.json", index)
        with _IO_LOCK:
            cur = pdir / "current"
            old = None
            if cur.exists():
                old = pdir / f".old-{secrets.token_hex(6)}"
                os.replace(cur, old)
            try:
                os.replace(staging, cur)
            except OSError:
                if old is not None:
                    os.replace(old, cur)
                raise
            with _CACHE_LOCK:
                _CACHE.pop(pid, None)
        if old is not None:
            shutil.rmtree(old, ignore_errors=True)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return {k: index[k] for k in ("pkg", "count", "update_date", "sha256", "size", "origin")}


#: `indexer.process_version` 回的錯誤代碼 → 給管理員看的句子
_INDEX_ERRORS = {
    "internal": "處理時發生錯誤，詳細原因在服務記錄。",
    "gone": "這個版本已經不在了，請重新匯入。",
}


def _friendly(e: BaseException) -> str:
    if isinstance(e, (GovError, safe_fetch.BlockedDestination, safe_fetch.UnsafeUrlScheme,
                      safe_fetch.FetchFailed)):
        return str(e)
    return MESSAGES["unexpected"]


def download_package(pid: str, *, progress: Optional[Callable[[int, Optional[int]], None]] = None,
                     cancelled: Optional[Callable[[], bool]] = None) -> dict:
    """下載一個檔（主要網址失敗再試備用網址）並換上去。失敗丟 `GovError`。"""
    pkg = get_package(pid)
    tries = [("url", pkg["url"])] + ([("alt_url", pkg["alt_url"])] if pkg["alt_url"] else [])
    errors: list[str] = []
    for origin, url in tries:
        if cancelled and cancelled():
            raise GovError(MESSAGES["cancelled"])
        try:
            res = safe_fetch.fetch_public(url, max_bytes=pkg["max_mb"] * 1024 * 1024,
                                          timeout=FETCH_TIMEOUT_S, deadline_s=FETCH_DEADLINE_S,
                                          progress=progress)
            return install_package(pid, res.data, origin=origin, final_url=res.final_url,
                                   cancelled=cancelled)
        except (GovError, safe_fetch.BlockedDestination, safe_fetch.UnsafeUrlScheme,
                safe_fetch.FetchFailed) as e:
            errors.append(_friendly(e))
            logger.warning("知識庫政府公開資料：%s 從%s下載失敗：%s", pid,
                           "主要網址" if origin == "url" else "備用網址", type(e).__name__)
    raise GovError("；".join(errors) if len(errors) == 1
                   else "主要網址：" + errors[0] + "；備用網址：" + errors[1])


# ---------------------------------------------------------------- 匯入
def _history_kind(rec: dict) -> str:
    if rec.get("abolished"):
        return "廢止"
    return "修正" if rec.get("history_n", 0) >= 2 else "公布"


def _notice_for(rec: dict) -> str:
    eff = rec.get("effective") or ""
    if eff == law_text.UNDETERMINED_EFFECTIVE:
        # 說明本身也是照固定寬度斷行的：接回斷行、連續空白收成一個（「第 3 條」的空白留著）
        note = re.sub(r"[ \t\u3000]{2,}", " ", " ".join(law_text.unwrap(rec.get("effective_note") or "")))
        return ("部分條文施行日期由主管機關另定（還沒有施行的條文也在內文裡）："
                + (note[:900] if note else "（資料沒有附說明）"))
    iso = law_text.iso_date(eff)
    if iso and iso > time.strftime("%Y-%m-%d"):
        return f"生效日期是 {law_text.roc_date(eff)}（還沒到）。"
    return ""


def _dataset_for(gid: str, key: str, name: str, attribution: str, *, actor: str,
                 current: Optional[dict]) -> str:
    """這個項目的資料集（沒有就建）。名稱撞到既有的資料集時加上來源。"""
    if current and current.get("dataset_id"):
        try:
            store.get_dataset(current["dataset_id"])
            return current["dataset_id"]
        except store.KBNotFound:
            pass
    g = GROUPS[gid]
    base = name if len(name) <= store.MAX_NAME else name[:store.MAX_NAME - 1] + "…"
    cands = [base, f"{base[:80]}（{g['name']}）", f"{base[:80]}（{key[:16]}）"]
    desc = f"由「政府公開資料」匯入（{g['name']}，代碼 {key}）。{attribution}"
    for nm in cands:
        if store.dataset_name_taken(nm):
            continue
        try:
            d = store.create_dataset({"name": nm[:store.MAX_NAME], "category": g["category"],
                                      "description": desc[:store.MAX_DESC], "access": "all"},
                                     actor=actor)
            return d["id"]
        except store.KBError:
            continue
    raise GovError(f"「{name}」的資料集建不起來（名稱都被用掉了）")


def _activate(new_vid: str, gid: str, key: str, *, actor: str, abolished: bool) -> None:
    """新版本處理完：啟用它（已廢止的不啟用）、同一個項目的舊版本停用（留著，舊草稿查得到）。"""
    for v in store.gov_item_versions(gid, key):
        if v["version_id"] != new_vid and v["status"] == "active":
            store.transition(v["version_id"], allowed_from=("active",), to="inactive")
    cur = store.get_version(new_vid)
    if abolished:
        if cur["status"] in ("ready", "active"):
            store.transition(new_vid, allowed_from=("ready", "active"), to="inactive")
        return
    if cur["status"] in ("ready", "inactive"):
        store.transition(new_vid, allowed_from=("ready", "inactive"), to="active",
                         activated_by=actor or "政府公開資料")


def _mark_abolished(gid: str, key: str, st: dict, *, actor: str) -> None:
    for v in store.gov_item_versions(gid, key):
        if v["status"] == "active":
            store.transition(v["version_id"], allowed_from=("active",), to="inactive")
    did = st.get("dataset_id")
    if did:
        try:
            d = store.get_dataset(did)
            if not d["description"].startswith("（已廢止）"):
                store.set_dataset_description(did, ("（已廢止）" + d["description"])[:store.MAX_DESC],
                                              actor=actor)
        except store.KBNotFound:
            pass


def _version_meta(gid: str, pid: str, e: dict, rec: Optional[dict]) -> dict:
    g = GROUPS[gid]
    if g["kind"] == "pdfs":
        upd = e["modified"] if not e["modified"].startswith("sha:") else ""
        label = (law_text.roc_date(upd) + "更新") if upd else time.strftime("%Y-%m-%d") + " 下載"
        return {"source_url": g["home"], "publisher": g["publisher"], "version_label": label,
                "published_on": law_text.iso_date(upd), "effective_on": ""}
    r = rec or {}
    roc = law_text.roc_date(e.get("modified", ""))
    eff = e.get("effective", "")
    return {
        "source_url": r.get("url") if re.match(r"^https?://", r.get("url") or "") else "",
        "publisher": "" if gid == "moj" else g["publisher"],
        "version_label": (roc + _history_kind(r)) if roc else "",
        "published_on": law_text.iso_date(e.get("modified", "")),
        "effective_on": law_text.iso_date(eff) if eff != law_text.UNDETERMINED_EFFECTIVE else "",
    }


def sync(gid: str, *, actor: str = "", job=None) -> dict:
    """把選取的項目匯入知識庫（新的建資料集；異動日期變了才建新版本）。同步；背景作業裡呼叫。"""
    from . import indexer
    g = _group_or_404(gid)
    cancelled = (lambda: bool(job and job.cancelled))
    idx = merged_index(gid)
    if not idx:
        raise GovError(MESSAGES["not_downloaded"])
    selected = get_selection(gid)
    states = store.gov_items(gid)
    summary = {"selected": len(selected), "new": 0, "updated": 0, "unchanged": 0,
               "failed": 0, "abolished": 0, "missing": [], "failed_names": []}
    plan: list[str] = []
    for key in selected:
        e = idx.get(key)
        if e is None:
            summary["missing"].append(key)
            continue
        st = states.get(key)
        ds_ok = False
        if st and st.get("dataset_id"):
            try:
                store.get_dataset(st["dataset_id"])
                ds_ok = True
            except store.KBNotFound:
                ds_ok = False
        good = ds_ok and st["modified_on"] == e["modified"] and any(
            v["modified_on"] == e["modified"] and v["status"] in ("active", "ready", "inactive")
            for v in store.gov_item_versions(gid, key))
        if good:
            if e["abolished"] and not st["abolished"]:
                _mark_abolished(gid, key, st, actor=actor)
                store.put_gov_item(gid, key, dataset_id=st["dataset_id"], name=e["name"],
                                   modified_on=e["modified"], abolished=True)
                summary["abolished"] += 1
            else:
                summary["unchanged"] += 1
            continue
        plan.append(key)

    # 要匯入的那幾部從原始檔撈出來（一包只掃一遍；只留要的那幾筆在記憶體）
    recs: dict[str, dict] = {}
    if plan and g["kind"] != "pdfs":
        want = set(plan)
        for i, pid in enumerate(g["packages"]):
            if package_index(pid) is None:
                continue
            _job(job, 0.05 + 0.15 * i / len(g["packages"]), "讀取下載的資料…")
            try:
                for rec in gp.iter_records(_pkg_dir(pid) / "current" / "package.bin",
                                           cancelled=cancelled):
                    if rec["key"] in want and rec["key"] not in recs:
                        recs[rec["key"]] = {**rec, "pkg": pid}
                        if len(recs) == len(want):
                            break
            except gp.PackageError as e:
                raise GovError(str(e)) from None

    n = len(plan)
    for i, key in enumerate(plan):
        if cancelled():
            raise GovError(MESSAGES["cancelled"])
        e = idx[key]
        _job(job, 0.2 + 0.8 * i / max(1, n), f"匯入第 {i + 1}/{n} 項…")
        try:
            res = _import_one(gid, key, e, recs.get(key), states.get(key), actor=actor,
                              indexer=indexer)
        except (GovError, store.KBError) as ex:
            logger.warning("知識庫政府公開資料：%s/%s 匯入失敗：%s", gid, key, ex)
            res = "failed"
        except Exception:
            logger.exception("知識庫政府公開資料：%s/%s 匯入失敗", gid, key)
            res = "failed"
        if res == "failed":
            summary["failed"] += 1
            summary["failed_names"].append(e["name"])
        else:
            summary[res] += 1
            if e["abolished"]:
                summary["abolished"] += 1
    summary["failed_names"] = summary["failed_names"][:20]
    return summary


def _import_one(gid: str, key: str, e: dict, rec: Optional[dict], st: Optional[dict], *,
                actor: str, indexer) -> str:
    g = GROUPS[gid]
    pid = e["pkg"]
    if g["kind"] == "pdfs":
        data = (_pkg_dir(pid) / "current" / "package.bin").read_bytes()
        ext, fmt = ".pdf", ""
        upd = e["modified"] if not e["modified"].startswith("sha:") else ""
        attribution = attribution_for(gid, update_date=upd)
        notice = ""
        name = e["name"]
    else:
        if rec is None:
            raise GovError("在下載的資料裡找不到這一項")
        idx_meta = package_index(pid) or {}
        attribution = attribution_for(gid, pkg=pid, update_date=idx_meta.get("update_date", ""))
        if g["kind"] == "laws":
            if not rec.get("articles"):
                raise GovError("這部法規沒有條文")
            text, fmt = law_text.render_law(rec, attribution=attribution), law_text.FORMAT_LAW
            notice = _notice_for(rec)
        else:
            if not (rec.get("text") or "").strip():
                raise GovError("這則規則沒有內容")
            text, fmt = law_text.render_rules(rec, attribution=attribution), law_text.FORMAT_RULES
            notice = ""
        data, ext, name = text.encode("utf-8"), ".md", rec["name"]
    did = _dataset_for(gid, key, name, attribution, actor=actor, current=st)
    sha = hashlib.sha256(data).hexdigest()
    meta = _version_meta(gid, pid, e, rec)
    title = name[:store.MAX_TITLE]
    fname = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "_", name)[:120] + ext
    dup = store.find_duplicate(did, sha)
    if dup:
        vid = dup["id"]          # 上一次匯入到一半被中斷、或內容其實沒變
    else:
        vid = store.create_version(did, title=title, filename=fname, ext=ext, data=data,
                                   sha256=sha, meta=meta, actor=actor or "政府公開資料")["id"]
    store.put_gov_version(vid, group_id=gid, item_key=key, fmt=fmt, modified_on=e["modified"],
                          license=LICENSE, attribution=attribution, notice=notice,
                          abolished=bool(e["abolished"]))
    status = store.get_version(vid)["status"]
    if status in ("uploaded", "indexing", "failed"):
        r = indexer.process_version(vid)
        if not r.get("ok"):
            # 代碼（internal / gone）不是給人看的句子；ExtractError 那一類本來就是固定句子
            err = str(r.get("error") or "")
            raise GovError(_INDEX_ERRORS.get(err) or err or "處理失敗")
    _activate(vid, gid, key, actor=actor, abolished=bool(e["abolished"]))
    store.put_gov_item(gid, key, dataset_id=did, name=name, modified_on=e["modified"],
                       abolished=bool(e["abolished"]))
    if e["abolished"]:
        _mark_abolished(gid, key, {"dataset_id": did}, actor=actor)
    # 沿用原本的資料集 ＝ 更新；第一次匯入、或資料集被刪掉後重建 ＝ 新匯入
    return "updated" if st and st.get("dataset_id") == did else "new"


# ---------------------------------------------------------------- 背景作業
_RUN_LOCK = threading.Lock()
#: 正在進行的那一件（同一時間只做一件：下載的檔大、解析吃記憶體）。**只在記憶體裡** ——
#: 服務重啟時作業本來就沒了，持久化的話畫面會永遠停在「進行中」。
_RUNNING: dict[str, Any] = {}

ACTIONS = ("download", "import", "update", "install")


def _job(job, progress: Optional[float] = None, message: Optional[str] = None) -> None:
    if job is None:
        return
    if progress is not None:
        job.progress = max(0.0, min(0.99, progress))
    if message is not None:
        job.message = message


def _read_status() -> dict:
    try:
        obj = json.loads(_status_path().read_text(encoding="utf-8"))
        return obj if isinstance(obj, dict) else {}
    except (OSError, ValueError):
        return {}


def _record(gid: str, action: str, *, ok: bool, message: str, summary: Optional[dict] = None,
            packages: Optional[dict] = None) -> None:
    with _CFG_LOCK:
        st = _read_status()
        cur = st.setdefault(gid, {})
        cur["last"] = {"at": time.time(), "action": action, "ok": bool(ok), "message": message,
                       "summary": summary or {}}
        if packages:
            cur.setdefault("packages", {}).update(packages)
        atomic_json.write_json(_status_path(), st)


_TERMINAL = ("done", "error", "cancelled", "interrupted")


def running() -> Optional[dict]:
    """正在進行的那一件（沒有回 None）。作業在排隊時就被取消的話 `run()` 不會執行、
    也就不會自己清掉 —— 這裡看到作業已經結束就順手清掉，不然會永遠「進行中」。"""
    from ..job_manager import job_manager
    with _RUN_LOCK:
        if not _RUNNING:
            return None
        jid = _RUNNING.get("job_id")
        if jid:
            j = job_manager.get(jid)
            if j is None or j.status in _TERMINAL:
                _RUNNING.clear()
                return None
        return dict(_RUNNING)


def _summary_message(s: dict) -> str:
    """一句固定格式的結果（數字以外的字固定 —— 畫面的 `tr()` 才翻得動）。細節（廢止、
    清單裡找不到的）畫面從 `summary` 另外組。"""
    return (f"完成：新匯入 {s['new']} 項、更新 {s['updated']} 項、"
            f"沒有變動 {s['unchanged']} 項、失敗 {s['failed']} 項")


def start(gid: str, action: str, *, request=None, actor: str = "",
          upload: Optional[tuple[str, bytes, str]] = None) -> str:
    """送出背景作業；回作業編號。`action`：

    * `update`：**下載並匯入**（畫面上唯一的按鈕；2026-10-08 使用者：「請改為下載並匯入 不要分兩步」）——
      重新下載清單、接著匯入選取的項目。全部下載失敗但這台有上次的清單時照那份匯入（`summary.stale_list`）。
    * `install`：手動上傳的檔案（`upload=(代碼, 內容, 檔名)`），換上之後一樣接著匯入。
    * `download`（只下載清單）/ `import`（只匯入選取的）：API 照舊收（腳本與既有呼叫不壞），畫面不再分兩步。
    """
    from ..job_manager import job_manager
    g = _group_or_404(gid)
    if action not in ACTIONS:
        raise GovError("不認得的動作")
    if action == "install":
        if not upload or upload[0] not in g["packages"]:
            raise GovNotFound(MESSAGES["not_found"])
    if action == "import" and not merged_index(gid):
        raise GovError(MESSAGES["not_downloaded"])
    running()               # 清掉排隊時就被取消、沒有機會自己清的那一件
    with _RUN_LOCK:
        if _RUNNING:
            raise GovBusy(MESSAGES["busy"])
        _RUNNING.update({"group": gid, "action": action, "started_at": time.time(), "job_id": ""})

    def run(job) -> None:
        pkgs_state: dict[str, dict] = {}
        stale_list = False
        try:
            if action in ("download", "update"):
                pids = list(g["packages"])
                span = 1.0 if action == "download" else 0.4
                for i, pid in enumerate(pids):
                    if job.cancelled:
                        raise GovError(MESSAGES["cancelled"])
                    _job(job, span * i / len(pids), f"下載第 {i + 1}/{len(pids)} 個檔案…")

                    def prog(received: int, total: Optional[int], _i=i) -> None:
                        _job(job, None, f"下載第 {_i + 1}/{len(pids)} 個檔案… "
                                        f"{received // (1024 * 1024)} MB")

                    try:
                        info = download_package(pid, progress=prog, cancelled=lambda: job.cancelled)
                        pkgs_state[pid] = {"at": time.time(), "ok": True,
                                           "message": f"已取得 {info['count']} 項",
                                           "count": info["count"]}
                    except GovError as e:
                        pkgs_state[pid] = {"at": time.time(), "ok": False, "message": str(e)}
                ok_n = sum(1 for v in pkgs_state.values() if v["ok"])
                if ok_n == 0:
                    if action == "update" and merged_index(gid):
                        # 下載並匯入：這次全部下載失敗、但這台有上次下載的清單 → 照那份清單匯入，
                        # 結果裡講明（`summary.stale_list`），不要整件失敗 —— 選取的項目照樣補得上
                        stale_list = True
                    else:
                        raise GovError("全部下載失敗：" + "；".join(
                            f"{PACKAGES[p]['name']}：{v['message']}" for p, v in pkgs_state.items()))
            if action == "install":
                pid, raw, fname = upload  # type: ignore[misc]
                _job(job, 0.1, "讀取上傳的檔案…")
                try:
                    info = install_package(pid, raw, origin="upload", filename=fname,
                                           cancelled=lambda: job.cancelled)
                except GovError as e:
                    # 原因要記在那個檔案的欄位上（畫面在那裡講「最後一次」）
                    pkgs_state[pid] = {"at": time.time(), "ok": False, "message": str(e)}
                    raise
                pkgs_state[pid] = {"at": time.time(), "ok": True,
                                   "message": f"已取得 {info['count']} 項（上傳的檔案）",
                                   "count": info["count"]}
            summary = None
            if action in ("import", "update", "install"):
                # 上傳＝離線版的「下載並匯入」：換上清單之後一樣接著匯入選取的項目
                if action != "import":
                    _job(job, 0.4, "比對選取的項目…")
                summary = sync(gid, actor=actor, job=job)
                if stale_list:
                    summary["stale_list"] = True
                msg = _summary_message(summary)
            else:
                ok_pk = [v for v in pkgs_state.values() if v["ok"]]
                msg = (f"下載完成：{len(ok_pk)} 個檔案、共 {sum(v.get('count', 0) for v in ok_pk)} 項"
                       + ("；有檔案下載失敗，原因寫在那個檔案的欄位裡" if len(ok_pk) < len(pkgs_state)
                          else ""))
            failed_pkgs = [p for p, v in pkgs_state.items() if not v["ok"]]
            _record(gid, action, ok=not failed_pkgs and not (summary or {}).get("failed"),
                    message=msg, summary=summary, packages=pkgs_state)
            _job(job, 1.0, msg)
        except GovError as e:
            _record(gid, action, ok=False, message=str(e), packages=pkgs_state)
            job.status = "error"
            job.error = str(e)
        except Exception:
            logger.exception("知識庫政府公開資料：%s/%s 失敗", gid, action)
            _record(gid, action, ok=False, message=MESSAGES["unexpected"], packages=pkgs_state)
            job.status = "error"
            job.error = MESSAGES["unexpected"]
        finally:
            with _RUN_LOCK:
                _RUNNING.clear()

    label = {"download": "下載資料", "import": "匯入選取的項目", "update": "下載並匯入",
             "install": "上傳並匯入"}[action]
    try:
        # 作業代號是「知識庫」，不是公文撰擬 —— 不然通知信與「我的作業」會寫成
        # 「公文撰擬：知識庫・…」（`job_labels` 的說明）
        job = job_manager.submit(
            KB_JOB_ID, run,
            meta={"filename": f"公文知識庫・{g['name']}：{label}", "kb": "gov",
                  "view_url": "/admin/knowledge/gov"},
            request=request)
    except Exception:
        with _RUN_LOCK:
            _RUNNING.clear()
        raise
    with _RUN_LOCK:
        if _RUNNING.get("group") == gid and _RUNNING.get("action") == action:
            _RUNNING["job_id"] = job.id
    return job.id


# ---------------------------------------------------------------- 狀態
def status() -> dict:
    """管理頁要的全部狀態（**不連外**）。"""
    st = _read_status()
    run = running()
    groups = []
    for gid, g in GROUPS.items():
        idx = merged_index(gid)
        sel = get_selection(gid)
        items = store.gov_items(gid)
        pk = []
        for pid in g["packages"]:
            p = get_package(pid)
            pi = package_index(pid)
            pk.append({
                "id": pid, "name": p["name"], "url": p["url"], "alt_url": p["alt_url"],
                "default_url": p["default_url"], "default_alt_url": p["default_alt_url"],
                "customized": p["customized"], "max_mb": p["max_mb"],
                "downloaded": pi is not None,
                "count": (pi or {}).get("count", 0),
                "update_date": (pi or {}).get("update_date", ""),
                "update_roc": _roc_from_update((pi or {}).get("update_date", "")),
                "installed_at": (pi or {}).get("installed_at"),
                "size": (pi or {}).get("size", 0),
                "origin": (pi or {}).get("origin", ""),
                "last": ((st.get(gid) or {}).get("packages") or {}).get(pid),
                "attribution": attribution_for(gid, pkg=pid,
                                               update_date=(pi or {}).get("update_date", "")),
            })
        defaults = _resolve_defaults(gid, idx) if idx else list(g["defaults"])
        # 依名稱的預設（國發會）要下載後才對得到代碼；之前畫面先列名稱
        default_hint = list(g["defaults"]) if (g["default_by"] == "name" and not idx) else []
        missing_defaults = ([k for k in g["defaults"] if k not in idx] if idx and
                            g["default_by"] == "key" else
                            ([n for n in g["defaults"]
                              if not any(e["name"] == n for e in idx.values())] if idx else []))
        imported = [s for s in items.values() if s.get("dataset_id")]
        needs = sum(1 for k, s in items.items() if s.get("dataset_id") and k in idx
                    and idx[k]["modified"] != s["modified_on"] and k in sel)
        groups.append({
            "id": gid, "name": g["name"], "publisher": g["publisher"], "kind": g["kind"],
            "about": g["about"], "home": g["home"], "category": g["category"],
            "category_label": store.CATEGORIES[g["category"]][0],
            "purpose_label": store.PURPOSES[store.CATEGORIES[g["category"]][1]],
            "license": LICENSE, "license_url": LICENSE_URL,
            "attribution": next((x["attribution"] for x in pk if x["downloaded"]),
                                pk[0]["attribution"]),
            "packages": pk, "downloaded": bool(idx), "available": len(idx),
            "selected_count": len(sel),
            "selected_chars": sum(idx[k]["chars"] for k in sel if k in idx),
            "selection_is_default": selection_is_default(gid),
            "defaults": defaults, "missing_defaults": missing_defaults,
            "default_hint": default_hint,
            "imported_count": len(imported),
            "abolished_count": sum(1 for s in imported if s.get("abolished")),
            "needs_update_count": needs,
            "last": (st.get(gid) or {}).get("last"),
            "running": bool(run and run.get("group") == gid),
        })
    return {"groups": groups, "link_only": [dict(x) for x in LINK_ONLY], "running": run,
            "limits": {"warn_items": WARN_ITEMS, "warn_chars": WARN_CHARS,
                       "max_selected": MAX_SELECTED}}


# ---------------------------------------------------------------- 給工具用
def abolished_laws() -> list[dict]:
    """已下載的全國法規資料庫清單裡**已廢止**的法規：`[{"key","name","modified"}]`。

    給「公文撰擬」之後做「草稿引用了已廢止的法規」提醒用（還沒接上；見 notes）。
    沒下載過回空清單（**不會**為了這個去連外）。"""
    out = []
    for e in merged_index("moj").values():
        if e.get("abolished"):
            out.append({"key": e["key"], "name": e["name"], "modified": e.get("modified", "")})
    return out


def gov_info_for_versions(dataset_id: str) -> dict[str, dict]:
    """管理頁「文件」清單用：這個資料集裡每個政府公開資料版本的出處與說明。"""
    out = {}
    for vid, g in store.gov_versions_of_dataset(dataset_id).items():
        out[vid] = {"group": g["group_id"], "group_name": GROUPS.get(g["group_id"], {}).get("name", ""),
                    "key": g["item_key"], "license": g["license"],
                    "attribution": g["attribution"], "notice": g["notice"],
                    "abolished": g["abolished"]}
    return out
