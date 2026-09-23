"""偵測 PDF 上的**數位簽章**。

## 為什麼需要這個

使用者 2026-09-14 問「PDF 編輯器如果開啟有憑證的 PDF 會怎樣」。實測
（用 pyhanko 做一份真的簽過章的 PDF，丟進編輯器**完全不編輯**就存檔）：

| | 原檔 | 存檔之後 |
|---|---|---|
| 簽章完整性 `intact` | True | **False** |

**這不是 bug，是簽章的本質** —— 它涵蓋整份檔案的位元組，動一個位元就不成立。
真正的問題是**我們沒有講**：存出來的檔案結構上仍然「有簽章」
（`/Sig`、`/ByteRange`、`/SigFlags` 都在），所以收件方在 Adobe Reader 會看到
**「簽章無效／文件已被變更」**的紅色警示 —— 比乾脆沒有簽章更難解釋。

## 判準

**只算「真的簽過」的欄位。** 版面上預留但還沒簽的簽名欄（`/V` 是空的）
被改掉沒有任何影響，把它算進來會讓一堆待簽核的表單跳出沒必要的警告，
而**誤報一多這個提示就會被忽略**（這個專案在用詞檢查上吃過這個虧）。
"""
from __future__ import annotations

from typing import Any

import fitz


def signed_field_count(doc: Any) -> int:
    """這份文件上**已經簽好**的簽章數。回 0 表示沒有（或只有空白簽名欄）。"""
    n = 0
    try:
        for page in doc:
            for w in page.widgets():
                if (w.field_type_string or "") != "Signature":
                    continue
                try:
                    v = doc.xref_get_key(w.xref, "V")
                except Exception:  # noqa: BLE001
                    v = None
                # `/V` 指向簽章字典才算真的簽過；沒有就是預留的空欄位
                if v and v[0] not in ("null", "unknown") and v[1] not in ("null", ""):
                    n += 1
    except Exception:  # noqa: BLE001 — 偵測失敗不可以讓開檔失敗
        return n
    return n


def describe(path) -> dict:
    """給前端用的摘要：`{"signed": N}`。**偵測不到就回 0，絕不丟例外** ——
    這只是個提示，不該讓「打得開的檔案」變成打不開。"""
    try:
        with fitz.open(str(path)) as doc:
            return {"signed": signed_field_count(doc)}
    except Exception:  # noqa: BLE001
        return {"signed": 0}
