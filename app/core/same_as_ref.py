"""把「同上」「同登記地址」這類**指涉值**展開成被指涉欄位的實際內容。

## 為什麼需要

廠商資料表上很常出現「發票地址：同上」「聯絡人：同上」。使用者把公司資料存進本站
時也會照抄那兩個字。結果填出來的表單上就印著「同上」—— 而**新表單的版面跟原本那張
不一樣**，上面那一格根本不是同一個欄位，收件方看到的是一個指不到任何東西的「同上」。

## 三條界線（這個功能真正的難處在這裡，不在字串比對）

1. **只認明確的指涉，不做模糊猜測。**
   「同登記地址」指得很清楚 —— 那是某個欄位的名字。「同上」沒有指名任何東西，
   而本站的公司資料是一份 key-value，**沒有「上面那一格」這種概念**（那是版面的
   性質，不是資料的性質）。所以裸指涉只在**少數約定俗成的配對**上展開，其餘保留字面。

2. **展不開就保留原字面，絕不填空。**
   使用者親手打的字有它的意思；換成空白是把資訊刪掉。

3. **展開過的要說得出來。**
   `resolve_profile()` 會回報每一筆「哪個欄位、從哪裡展開、變成什麼」，畫面上要
   顯示 —— 悄悄把使用者打的字換掉，是這個功能最可能造成的傷害。
"""
from __future__ import annotations

import re
from typing import Optional

#: 裸指涉詞：沒有指名任何欄位，只說「跟旁邊一樣」。
#: `〃` 與 `"` 是表格裡的重複記號（ditto mark）。
_BARE_REFS = frozenset({
    "同上", "同前", "如上", "同上述", "同前述", "同左", "同右", "同上欄", "同左列",
    "同上所述", "略同上", "ditto", "同",
    "〃", "″", "”", '"', "，, ",
})

#: 裸指涉的**約定俗成配對**：key → 它「同上」時實際指的是誰。
#:
#: 只列業界表單上真的長期這樣用的。**寧可少列**：多列一筆，就是多一種把 A 欄位的值
#: 悄悄填進 B 欄位的可能。
#:
#: 刻意**不列**的：
#: * `english_address` ← `address`：中文地址填進英文欄位是明顯的錯。
#: * `bank_address` ← `address`：銀行地址跟公司地址沒有任何關係。
#: * 任何電話 / 信箱：「電話同上」指的多半是版面上一格，不是固定的某個欄位。
_DITTO_PAIRS: dict[str, str] = {
    "invoice_address": "address",       # 發票地址：同上 → 公司地址
    "factory_address": "address",       # 工廠地址：同上 → 公司地址
    "payee_address_en": "english_address",
    "bank_account_name": "company_name",  # 戶名：同上 → 公司全名
    "short_name": "company_name",
}

#: 具名指涉的開頭。「同」「與」後面接欄位名稱。
_NAMED_PREFIXES = ("同", "與", "如")
#: 具名指涉的結尾贅詞（「同公司地址相同」）。
_NAMED_SUFFIXES = ("相同", "同", "一致", "者同")


def _clean(value: str) -> str:
    """去掉空白與常見的包覆符號，方便比對。"""
    v = (value or "").strip()
    v = v.strip("（）()［］[]｛｝{}「」『』<>《》")
    return v.strip()


def is_reference(value: str, _idx: Optional[dict[str, str]] = None) -> bool:
    """這個值是不是一個**指得到東西的**指涉（而不是真的內容）。

    「同」開頭不代表就是指涉 —— 「同心圓實業有限公司」「同泰路 12 號」都是真的內容。
    所以具名指涉還要求**後面那段真的對得上某個欄位的標籤**才算數。

    第一版只看「同」開頭就回 True，結果公司名叫「同心圓…」的客戶會讓
    `bank_account_name: 同上` 展不開（迴圈防護誤判成「來源也是指涉」）—— 安全方向
    的錯，但使用者會覺得功能壞了。
    """
    v = _clean(value)
    if not v or len(v) > 20:          # 太長的不可能是指涉，是真的地址
        return False
    if v in _BARE_REFS:
        return True
    from . import pdf_form_detect as _pfd
    target = _named_target_text(v)
    if not target:
        return False
    idx = _idx if _idx is not None else _label_index()
    return _pfd._normalize(target) in idx


def _named_target_text(value: str) -> Optional[str]:
    """從「同○○」取出 ○○ 的部分；不是具名指涉就回 None。"""
    v = _clean(value)
    if not v or v in _BARE_REFS:
        return None
    if not v.startswith(_NAMED_PREFIXES):
        return None
    body = v[1:].strip()
    for suf in _NAMED_SUFFIXES:
        if body.endswith(suf) and len(body) > len(suf):
            body = body[: -len(suf)].strip()
    # 「同上地址」這種：前面是裸指涉詞 + 欄位名 —— 當成裸指涉處理（指不到特定欄位）
    if not body or body in ("上", "前", "左", "右"):
        return None
    if len(body) < 2:                 # 一個字指不出東西
        return None
    return body


def _label_index() -> dict[str, str]:
    """欄位標籤（正規化後）→ 正式 key。用 `pdf_form_detect` 的同義詞表，
    這樣「同登記地址」跟表單上的標籤是同一套認定，不會各講各話。"""
    from . import pdf_form_detect as _pfd
    idx: dict[str, str] = {}
    for key, labels in _pfd.LABEL_MAP.items():
        for lab in labels:
            idx[_pfd._normalize(lab)] = key
    return idx


def resolve_one(key: str, value: str, profile: dict[str, str],
                _idx: Optional[dict[str, str]] = None
                ) -> tuple[str, Optional[str]]:
    """把單一欄位的指涉值展開。

    回 `(展開後的值, 來源 key)`。不是指涉、或展不開時回 `(原值, None)`
    —— **絕不回空字串**。
    """
    from . import pdf_form_detect as _pfd
    if not value or not value.strip():
        return value, None
    v = _clean(value)
    idx = _idx if _idx is not None else _label_index()

    # ① 具名指涉：「同登記地址」→ 用標籤同義詞查出正式 key
    target_text = _named_target_text(v)
    if target_text:
        tgt = idx.get(_pfd._normalize(target_text))
        if tgt and tgt != key:
            got = (profile.get(tgt) or "").strip()
            # 目標本身也是指涉時不再往下追（避免 A→B→A 繞圈）
            if got and not is_reference(got, idx):
                return got, tgt
        return value, None            # 指名了但查不到 → 保留字面

    # ② 裸指涉：只在約定俗成的配對上展開
    if v in _BARE_REFS:
        tgt = _DITTO_PAIRS.get(key)
        if tgt:
            got = (profile.get(tgt) or "").strip()
            if got and not is_reference(got, idx):
                return got, tgt
    return value, None


def resolve_profile(profile: dict[str, str]
                    ) -> tuple[dict[str, str], list[dict[str, str]]]:
    """把整份公司資料裡的指涉值展開。

    回 `(展開後的 profile, 展開明細)`。明細每一筆是
    `{"key", "from", "original", "value"}` —— 畫面要顯示，讓使用者看得到
    我們把他打的字換成了什麼。
    """
    idx = _label_index()
    out = dict(profile)
    expanded: list[dict[str, str]] = []
    for key, value in profile.items():
        new, src = resolve_one(key, value, profile, idx)
        if src and new != value:
            out[key] = new
            expanded.append({"key": key, "from": src,
                             "original": value, "value": new})
    return out, expanded
