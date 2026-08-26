"""替換模式用的假值產生器。

「文件去識別化」的第三種模式：把偵測到的敏感資料**換成別的值**（使用者自己
填，或由這裡自動產生）。與另外兩種模式的差別：

* 編修（Redaction）—— 原文真刪 + 塗黑，看不出原本有什麼
* 遮罩（Masking）  —— 保留格式，中段變 `*`（`0912****678`）
* **替換**         —— 換成一個看起來正常、但不是真的值（`0938271654`）

替換的用途是「文件還要能被當成正常文件使用」：拿去測試系統、給外部看報表、
內部教學範例。所以產出的假值要**符合該欄位該有的樣子**。

## 兩種形式（使用者可選）

`valid_checksum=False`（預設，比較安全）
    產生**明顯是假的**值：固定前綴 + 流水號，而且刻意不通過檢查碼。
    絕對不可能撞到真人的資料。

`valid_checksum=True`
    身分證 / 統編 / 信用卡都算出**正確的檢查碼**，拿去測試系統不會被擋。
    代價：算得出來的號碼可能剛好是某個真人的 —— 介面上要講清楚。

檢查碼**不自己重寫**，一律用 `patterns.py` 既有的驗證函式反推最後一碼，
確保產出的假值跟偵測邏輯永遠一致（自己寫一份就是下一個會漂掉的地方）。

## 一致性

同一份文件裡，**同一個原值一定對應同一個假值**。少了這條，一份報表裡同一個
客戶會變成三個不同的人，文件就沒辦法用了。所以是 `Replacer` 這個物件持有
對應表，不是一支無狀態的函式。

Email 用 `example.com`（RFC 2606 保留給文件用）、IP 用 `192.0.2.x`
（RFC 5737 保留段）—— 這兩種本來就不會撞到真的東西，兩種形式都一樣安全。
"""
from __future__ import annotations

from . import patterns as P

#: 人名用的字池 —— 刻意用「範例感」明顯的組合，不用真的常見姓名。
_NAME_POOL = [
    "王大明", "陳小華", "林志文", "張淑芬", "李建國",
    "黃美玲", "吳俊宏", "劉雅婷", "蔡宗翰", "鄭佩君",
]

_COMPANY_POOL = [
    "範例股份有限公司", "測試企業有限公司", "示範科技股份有限公司",
    "樣本商行", "演示國際有限公司",
]


def _digits_with_valid_tail(prefix: str, length: int, validator) -> str:
    """把最後一碼換成能讓 `validator` 通過的數字。湊不出來就回原字串。"""
    body = (prefix + "0" * length)[:length - 1]
    for tail in "0123456789":
        cand = body + tail
        if validator(cand):
            return cand
    return body + "0"


class Replacer:
    """一份文件用一個 —— 持有「原值 → 假值」的對應表。"""

    def __init__(self, valid_checksum: bool = False):
        self.valid_checksum = bool(valid_checksum)
        self._map: dict[tuple[str, str], str] = {}
        self._counter: dict[str, int] = {}

    def _next(self, type_id: str) -> int:
        n = self._counter.get(type_id, 0) + 1
        self._counter[type_id] = n
        return n

    def for_value(self, type_id: str, value: str) -> str:
        """同一個原值永遠回同一個假值。"""
        key = (type_id or "", value or "")
        if key in self._map:
            return self._map[key]
        made = self._make(type_id or "", value or "")
        self._map[key] = made
        return made

    # --- 各型別的產生規則 ---------------------------------------------

    def _make(self, type_id: str, value: str) -> str:
        n = self._next(type_id)
        vc = self.valid_checksum

        if type_id == "tw_id":
            if not vc:
                return f"X0{n:08d}"[:10]          # 開頭 X + 0，一定不通過檢查碼
            return _digits_with_valid_tail(f"A{1 if n % 2 else 2}{n:07d}", 10,
                                           P._tw_id_valid)
        if type_id == "tw_arc":
            if not vc:
                return f"XA{n:08d}"[:10]
            return _digits_with_valid_tail(f"A{'A'}{n:07d}", 10, P._tw_arc_valid)
        if type_id == "tw_biz":
            if not vc:
                return f"{n:08d}"                 # 全 0 開頭，不會通過統編檢查
            return _digits_with_valid_tail(f"{n:07d}", 8, P._twbiz_valid)
        if type_id == "cc":
            base = f"4000{n:012d}"        # 4 + 12 = 16 碼，維持卡號長度
            if not vc:
                return base
            return _digits_with_valid_tail(base[:15], 16, P._luhn_valid)

        if type_id == "mobile":
            return f"09{(n % 90) + 10:02d}{n % 1000000:06d}"
        if type_id == "landline":
            return f"0{(n % 8) + 2}-{n % 10000000:07d}"
        if type_id == "email":
            return f"user{n:02d}@example.com"     # RFC 2606 保留網域
        if type_id == "ip":
            return f"192.0.2.{(n % 254) + 1}"     # RFC 5737 文件用保留段
        if type_id == "hostname":
            return f"host{n:02d}.example.com"
        if type_id == "mac":
            return f"00:00:5E:00:53:{n % 256:02X}"   # RFC 7042 文件用保留段

        if type_id == "person_name":
            return _NAME_POOL[(n - 1) % len(_NAME_POOL)]
        if type_id in ("company", "account_name"):
            return _COMPANY_POOL[(n - 1) % len(_COMPANY_POOL)]
        if type_id == "addr":
            return f"台北市中正區範例路 {n} 號"
        if type_id == "dob":
            return _same_shape_date(value, n)
        if type_id == "plate":
            return f"AB{(n % 10)}-{n % 10000:04d}"
        if type_id in ("bank_account", "bank_code", "bank_branch"):
            return _same_shape_digits(value, n)
        if type_id in ("passport", "driver_license", "tw_einvoice",
                       "order_num", "vin", "flight", "pnr", "hic"):
            return _same_shape_alnum(value, n)

        # 沒有專屬規則的（含使用者自訂的字詞）—— 保持長度，看得出是替換過的
        return _same_shape_alnum(value, n)


def _same_shape_digits(value: str, n: int) -> str:
    """保留非數字的符號位置，數字換成流水號。"""
    seq = f"{n:0{max(1, sum(c.isdigit() for c in value))}d}"
    out, i = [], 0
    for c in value:
        if c.isdigit():
            out.append(seq[i % len(seq)])
            i += 1
        else:
            out.append(c)
    return "".join(out)


def _same_shape_alnum(value: str, n: int) -> str:
    """英數換成流水號 / 固定字母，其餘符號原樣保留（維持長度與版面）。"""
    seq = f"{n:04d}"
    out, i = [], 0
    for c in value:
        if c.isdigit():
            out.append(seq[i % 4]); i += 1
        elif c.isalpha() and c.isascii():
            out.append("X")
        elif c.isalpha():
            out.append("範")            # 中日韓字改成「範」，長度不變
        else:
            out.append(c)
    return "".join(out)


def _same_shape_date(value: str, n: int) -> str:
    """出生日期換成同樣寫法的假日期（分隔符號、位數都照原樣）。"""
    y, m, d = 1990 + (n % 20), (n % 12) + 1, (n % 28) + 1
    digits = [c for c in value if c.isdigit()]
    if len(digits) <= 6:                      # 兩位數年份（民國或 yy）
        parts = [f"{(n % 80) + 10:02d}", f"{m:02d}", f"{d:02d}"]
    else:
        parts = [f"{y:04d}", f"{m:02d}", f"{d:02d}"]
    seq = "".join(parts)
    out, i = [], 0
    for c in value:
        if c.isdigit():
            out.append(seq[i % len(seq)]); i += 1
        else:
            out.append(c)
    return "".join(out)
