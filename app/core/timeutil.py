"""管理區的時間解讀 —— **後端不猜使用者在哪個時區**。

## 由來（GitHub issue #48）

`/admin/audit` 的三件事各自用不同的時區基準，而它們必須是同一個：

| 位置 | 原本的基準 | 對不對 |
|---|---|---|
| 畫面顯示 | epoch 交給瀏覽器 `new Date(ts)` 轉 | ✅ 使用者看到的是**自己時區**的時間 |
| 日期篩選 | `datetime.fromisoformat(s).timestamp()` | ❌ 變成**伺服器行程**的時區 |
| CSV 時間欄 | `datetime.fromtimestamp(ts)` | ❌ 同上 |

`<input type="datetime-local">` 送出的 `2026-08-21T09:00` **不帶時區**，它的意思
是「使用者畫面上選的那個本地時間」。伺服器若跑在 UTC，這個字串會被當成
UTC 09:00 —— 於是篩選整整偏 8 小時，**而且是無聲的**：畫面照樣列出資料，
只是少了一段本來該在的紀錄；CSV 的時間欄則與畫面差 8 小時，人工比對必錯。

## 為什麼不是寫死 `Asia/Taipei`

那只是把「猜伺服器時區」換成「猜台灣」，同一個錯誤換個方向再犯一次 ——
海外部署、或使用者出差在別的時區，照樣對不上。**唯一一致的定義是「使用者
畫面上看到的時區」**，所以由前端把偏移一起送上來（瀏覽器本來就知道），
後端只做明確的換算。

沒帶偏移時（curl / 對外 API / 排程匯出）才退回伺服器本地時區 —— 那是
**明確的退路**，而且 CSV 的時間欄一律帶偏移，讀的人看得出來是哪個時區。

## 偏移的正負號

參數 `tz_offset_min` 一律是 **UTC 偏移（東為正）**：台北 = `+480`。

**注意 JavaScript 的 `getTimezoneOffset()` 是反的**（台北回 `-480`），所以前端
要送 `-new Date().getTimezoneOffset()`。正負號搞反的話偏移會變成兩倍時差，
而且看起來「有處理時區」—— 比完全沒處理更難發現，因此這裡與前端都寫死同一個
慣例並由測試守著。
"""
from __future__ import annotations

import datetime as _dt
from typing import Optional

#: 合理的 UTC 偏移範圍（分鐘）。世界上最極端的是 UTC+14 / UTC-12。
_MAX_OFFSET_MIN = 14 * 60
_MIN_OFFSET_MIN = -12 * 60


def clean_offset(tz_offset_min) -> Optional[int]:
    """把外部傳進來的偏移正規化；不合理就當作沒給（回 None）。"""
    if tz_offset_min is None or tz_offset_min == "":
        return None
    try:
        v = int(tz_offset_min)
    except (TypeError, ValueError):
        return None
    if v < _MIN_OFFSET_MIN or v > _MAX_OFFSET_MIN:
        return None
    return v


def _tz(tz_offset_min: Optional[int]):
    """偏移 → tzinfo；沒給就回 None（代表「用伺服器本地時區」）。"""
    off = clean_offset(tz_offset_min)
    if off is None:
        return None
    return _dt.timezone(_dt.timedelta(minutes=off))


def local_input_to_epoch(value: str,
                         tz_offset_min: Optional[int] = None) -> Optional[float]:
    """`datetime-local` 字串 → epoch 秒。無法解析回 None（呼叫端當作沒篩）。

    字串本身帶了偏移（`2026-08-21T09:00+08:00`）時**以字串為準**，
    `tz_offset_min` 只用在不帶偏移的字串上。
    """
    s = (value or "").strip()
    if not s:
        return None
    try:
        dt = _dt.datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        tz = _tz(tz_offset_min)
        dt = dt.replace(tzinfo=tz) if tz else dt.astimezone()
    return dt.timestamp()


def epoch_to_iso(ts: float, tz_offset_min: Optional[int] = None) -> str:
    """epoch → **帶偏移的** ISO-8601（`2026-08-21 09:00:00+08:00`）。

    偏移一定要印出來 —— 這份 CSV 會被拿去跟畫面比對，沒有偏移的時間字串
    正是 issue #48 裡「差 8 小時卻看不出原因」的來源。
    """
    if not ts:
        return ""
    tz = _tz(tz_offset_min)
    dt = _dt.datetime.fromtimestamp(float(ts), tz=_dt.timezone.utc)
    dt = dt.astimezone(tz) if tz else dt.astimezone()
    return dt.isoformat(sep=" ", timespec="seconds")
