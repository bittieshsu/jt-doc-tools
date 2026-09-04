#!/usr/bin/env python3
"""把譯文併進語系檔 —— **一律用中文原文當鍵，絕不用索引**。

## 為什麼要有這一支

譯文很多時，很自然會想「印出待翻清單 → 依序寫譯文 → 依索引併回去」。
**這個做法已經害過兩次**：

* 介紹站的英文版：`免責聲明` 拿到的是上一條的譯文、表格欄位變成 `; JSON:`，
  使用者截圖回報才發現（v1.14.97）。
* 工具頁的 JS 字串：清單在我修掉兩條之後重新產生，順序變了，
  **從第 8 條起整批偏移 2 格**（當場抓到，沒有流出去）。

清單只要在「印出來」和「併回去」之間變動過一次 —— 修掉一條、補上一條、
排序規則變了 —— 後面全錯，而且**畫面上一個中文字都沒有，守門全綠**。

所以規則是：**譯文檔的鍵就是中文原文**。

    python tools/i18n_merge.py app/i18n/en.json /tmp/batch.json

`batch.json` 形如 `{"中文原文": "English", ...}`。已存在的鍵不覆寫（要覆寫加
`--force`），併完印出還缺幾條。
"""
from __future__ import annotations

import json
import pathlib
import sys


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    force = "--force" in sys.argv
    if len(args) != 2:
        print(__doc__)
        return 2
    target, batch = pathlib.Path(args[0]), pathlib.Path(args[1])
    cat = json.loads(target.read_text(encoding="utf-8"))
    add = json.loads(batch.read_text(encoding="utf-8"))
    if any(k.isdigit() for k in add):
        print("✗ 這份譯文的鍵看起來是「索引」不是中文原文 —— 拒絕合併。\n"
              "  用索引對譯文已經害過兩次（清單一變順序就整批偏移，而且無聲）。",
              file=sys.stderr)
        return 1
    n = 0
    for k, v in add.items():
        if not v:
            continue
        if k in cat and not force:
            continue
        cat[k] = v
        n += 1
    target.write_text(json.dumps(cat, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
                      encoding="utf-8")
    print(f"併入 {n} 條；語系檔現有 {len(cat)} 條")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
