#!/usr/bin/env python3
"""把「依索引」寫的譯文安全地轉成「依中文原文」的譯文檔。

有時候待翻的字串太長 / 有換行，逐條把中文原文抄進譯文檔既慢又容易打錯。
折衷是依索引寫，但**索引一定要驗**（同一份清單在中間被重新產生過就會整批
偏移 —— 這個坑踩過兩次，見 tools/i18n_merge.py 的說明）。

所以這支要求每一條都附一段 `prefix`（中文原文的開頭幾個字），**對不上就整批
拒絕**，不會只錯一條而已。

    batch.json: [{"i": 5, "prefix": "共", "en": "Total"}, ...]
    python tools/i18n_batch_apply.py /tmp/admin_keys.json batch.json out.json
"""
from __future__ import annotations

import json
import pathlib
import sys


def main() -> int:
    if len(sys.argv) != 4:
        print(__doc__)
        return 2
    keys = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
    batch = json.loads(pathlib.Path(sys.argv[2]).read_text(encoding="utf-8"))
    out_path = pathlib.Path(sys.argv[3])
    bad, out = [], {}
    for item in batch:
        i, pre, en = item["i"], item["prefix"], item["en"]
        if not (0 <= i < len(keys)):
            bad.append(f"{i}: 索引超出範圍")
            continue
        k = keys[i]
        if not k.startswith(pre):
            bad.append(f"{i}: 對不上（期望開頭 {pre!r}，實際 {k[:len(pre)+6]!r}）")
            continue
        out[k] = en
    if bad:
        print("✗ 索引與清單對不上，整批拒絕：", file=sys.stderr)
        for b in bad[:10]:
            print("   " + b, file=sys.stderr)
        return 1
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n",
                        encoding="utf-8")
    print(f"✓ {len(out)} 條，全部對得上 → {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
