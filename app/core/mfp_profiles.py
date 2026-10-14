"""事務機取件：型號辨識與相容性狀態。

**這裡的辨識只是「從網頁上看到什麼」**，不代表支援：
規格書（docs-share/mfp-fetch/）規定未經實機驗證的型號一律不可以標「已支援」，
使用者也交代實機實測通過之前整支工具標 Beta。所以目前**沒有任何型號會得到 `supported`**
—— 等某一款在實機上通過取件測試，才在 `VERIFIED_PROFILES` 加上它。

辨識依據一律附在結果裡（哪個頁面、哪個欄位、摘錄），讓看報告的人知道判斷從哪來；
單一個 Server 標頭或標題不足以確認完整型號，所以也記下比對到幾個依據。
"""
from __future__ import annotations

import re
from typing import Any, Optional

#: 規格書列的首批四款（**廠牌系列＋型號都要對上**，不能只比對 C5570 這種尾碼 ——
#: ApeosPort C5570 與 Apeos C5570 是不同系列）。
FIRST_BATCH: frozenset[tuple[str, str]] = frozenset({
    ("Apeos", "C5571"),
    ("Apeos", "C5570"),
    ("ApeosPort", "C4570"),
    ("ApeosPort-V", "C6680"),
})

#: 已在實機上通過取件測試的（系列, 型號）。**目前是空的。**
VERIFIED_PROFILES: frozenset[tuple[str, str]] = frozenset()

#: 狀態代碼 → 固定訊息（翻譯檔有英日文）。
STATUS_MESSAGES: dict[str, str] = {
    "supported": "已支援（已通過實機驗證）",
    "first_batch_unverified": "首批規劃的型號，尚未完成實機驗證",
    "same_vendor_unverified": "同廠牌，尚未驗證是否支援",
    "unsupported_vendor": "不支援：目前只規劃 FUJIFILM（富士軟片）事務機",
    "login_required": "需要登入後才能辨識型號",
    "unknown": "無法辨識型號",
    "unreachable": "連不上設備，無法辨識",
}

#: 畫面上的「相容性」：短的狀態放進膠囊標籤，說明另外一行。整句塞進膠囊會折成兩行、變成一顆橢圓
#: （使用者 2026-10-14 指出）。`STATUS_MESSAGES` 是完整的一句，API、診斷報告、服務紀錄檔照舊用那一份。
STATUS_LABELS: dict[str, tuple[str, str]] = {
    "supported": ("已支援", "已通過實機驗證"),
    "first_batch_unverified": ("尚未驗證", "首批規劃的型號，尚未完成實機驗證"),
    "same_vendor_unverified": ("尚未驗證", "同廠牌，尚未驗證是否支援"),
    "unsupported_vendor": ("不支援", "目前只規劃 FUJIFILM（富士軟片）事務機"),
    "login_required": ("需要登入", "登入後才能辨識型號"),
    "unknown": ("無法辨識", "首頁上找不到廠牌或型號"),
    "unreachable": ("連不上", ""),
}

_FUJI_VENDOR = re.compile(
    r"FUJI\s?FILM|Fuji\s?Xerox|FUJI\s?XEROX|富士フイルム|富士ゼロックス|富士軟片|富士全錄", re.I)
#: 系列名稱＋型號。世代（-V、-VI、-VII）要留著：ApeosPort-V C6680 與 ApeosPort C6680 不同。
_FUJI_MODEL = re.compile(
    r"\b(ApeosPort(?:-(?:VII|VI|V|IV))?|ApeosPrint|ApeosPro|Apeos|DocuCentre(?:-(?:VII|VI|V|IV))?|DocuPrint)"
    r"[\s\-]*([A-Z]{0,2}\d{3,4}[A-Z]{0,2})\b")
_FUJI_CWIS = re.compile(r"CentreWare\s+Internet\s+Services|Internet\s+Services", re.I)
_FIRMWARE = re.compile(
    r"(?:Controller\s*(?:ROM|Version)|Firmware\s*Version|ファームウェア(?:バージョン)?|コントローラー?\s*ROM|韌體版本)"
    r"\s*[:：]?\s*([0-9][0-9A-Za-z.\-]{1,30})", re.I)

#: 其他廠牌的特徵（只用來判斷「不是 FUJIFILM」）。順序：FUJI 先判，避免 Fuji Xerox 被當成 Xerox。
_OTHER_VENDORS: tuple[tuple[str, re.Pattern], ...] = (
    ("Xerox", re.compile(r"\bXerox\b|AltaLink|VersaLink|WorkCentre", re.I)),
    ("Canon", re.compile(r"\bCanon\b|imageRUNNER|Remote\s*UI", re.I)),
    ("Ricoh", re.compile(r"\bRICOH\b|Web\s*Image\s*Monitor", re.I)),
    ("Konica Minolta", re.compile(r"KONICA|MINOLTA|bizhub|PageScope", re.I)),
    ("Kyocera", re.compile(r"KYOCERA|TASKalfa|ECOSYS|Command\s*Center", re.I)),
    ("Sharp", re.compile(r"\bSHARP\b", re.I)),
    ("Toshiba", re.compile(r"TOSHIBA|e-STUDIO|TopAccess", re.I)),
    ("HP", re.compile(r"\bHP\b|Hewlett|LaserJet|Embedded\s*Web\s*Server", re.I)),
    ("Epson", re.compile(r"\bEPSON\b", re.I)),
    ("Brother", re.compile(r"\bBrother\b", re.I)),
)


def _excerpt(text: str, m: re.Match, width: int = 60) -> str:
    a, b = max(0, m.start() - width // 2), min(len(text), m.end() + width // 2)
    return re.sub(r"\s+", " ", text[a:b]).strip()[:120]


def _sources(pages: list[dict]) -> list[tuple[dict, str]]:
    """（依據位置, 文字）。位置用代碼（`field`：title / text / header），畫面再翻譯。
    標題與可見文字之外，也看 Server 標頭。"""
    out: list[tuple[dict, str]] = []
    for p in pages:
        page = f"{p.get('scheme', '')}:{p.get('port', '')}{p.get('path', '/')}"
        if p.get("title"):
            out.append(({"page": page, "field": "title"}, p["title"]))
        if p.get("text_full_for_detection"):
            out.append(({"page": page, "field": "text"}, p["text_full_for_detection"]))
        for k, v in p.get("headers") or []:
            if k.lower() in ("server", "x-powered-by"):
                out.append(({"page": page, "field": "header", "header": k}, v))
    return out


def detect(pages: list[dict]) -> dict[str, Any]:
    """從連線測試收集到的頁面判斷廠牌、型號、韌體與相容性狀態。"""
    res: dict[str, Any] = {"vendor": None, "family": None, "model": None, "firmware": None,
                           "status": "unknown", "evidence": [], "evidence_count": 0}
    if not pages:
        res["status"] = "unreachable"
        res["message"] = STATUS_MESSAGES["unreachable"]
        return res
    srcs = _sources(pages)
    ev: list[dict] = []
    fuji = False
    for where, text in srcs:
        m = _FUJI_VENDOR.search(text)
        if m:
            fuji = True
            ev.append({"what": "vendor", **where, "excerpt": _excerpt(text, m)})
            break
    model: Optional[tuple[str, str]] = None
    for where, text in srcs:
        m = _FUJI_MODEL.search(text)
        if m:
            model = (m.group(1), m.group(2).upper())
            ev.append({"what": "model", **where, "excerpt": _excerpt(text, m)})
            break
    if not fuji:
        for where, text in srcs:
            m = _FUJI_CWIS.search(text)
            if m and model:
                ev.append({"what": "ui", **where, "excerpt": _excerpt(text, m)})
                break
    for where, text in srcs:
        m = _FIRMWARE.search(text)
        if m:
            res["firmware"] = m.group(1)
            ev.append({"what": "firmware", **where, "excerpt": _excerpt(text, m)})
            break
    if fuji or model:
        res["vendor"] = "FUJIFILM"
        if model:
            res["family"], res["model"] = model
        if model and model in VERIFIED_PROFILES:
            res["status"] = "supported"
        elif model and model in FIRST_BATCH:
            res["status"] = "first_batch_unverified"
        elif model:
            res["status"] = "same_vendor_unverified"
        elif any(p.get("password_inputs") for p in pages):
            res["status"] = "login_required"
        else:
            res["status"] = "same_vendor_unverified"
    else:
        for name, rx in _OTHER_VENDORS:
            hit = next(((w, t, m) for w, t in srcs for m in [rx.search(t)] if m), None)
            if hit:
                res["vendor"] = name
                res["status"] = "unsupported_vendor"
                ev.append({"what": "vendor", **hit[0], "excerpt": _excerpt(hit[1], hit[2])})
                break
        else:
            if any(p.get("password_inputs") for p in pages):
                res["status"] = "login_required"
    res["evidence"] = ev
    res["evidence_count"] = len(ev)
    res["message"] = STATUS_MESSAGES[res["status"]]
    return res
