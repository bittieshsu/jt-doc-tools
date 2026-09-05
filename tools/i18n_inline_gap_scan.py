#!/usr/bin/env python3
"""找出「中文不用空白、英文需要空白」的相鄰標記。

`{{ tr('SSO 是') }}<b>{{ tr('附加') }}</b>{{ tr('登入方式…') }}` 在中文完全正常
（中文本來就不用空格），切成英文卻變成 `SSO is an` + `additional` + `way…`
黏成 **anadditionalway** —— 使用者截圖回報過。

判準：`tr()` 的呼叫與相鄰的行內標記之間**原始碼裡沒有空白**時，
英文譯文在那一側就必須自己帶空白。中文譯文（原文）不受影響。

    python tools/i18n_inline_gap_scan.py          # 只報告
    python tools/i18n_inline_gap_scan.py --fix    # 直接把空白補進 en.json
"""
from __future__ import annotations

import json
import pathlib
import re
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
CAT = REPO / "app" / "i18n" / "en.json"
# 只認行內標記；<div> / <p> 這類區塊標記本來就會斷行，不需要空白。
INLINE = r"(?:b|strong|i|em|code|span|a|kbd|small|u|mark)"
# tr('…') 後面緊接著行內標記的開頭 → 該譯文的**結尾**要有空白
AFTER = re.compile(r"tr\((['\"])(.+?)\1\s*\)\s*\}\}<" + INLINE + r"[ >]")
# 行內標記的結尾後面緊接著 tr('…') → 該譯文的**開頭**要有空白
BEFORE = re.compile(r"</" + INLINE + r">\{\{\s*tr\((['\"])(.+?)\1\s*\)")


def scan() -> tuple[set[str], set[str]]:
    need_tail: set[str] = set()
    need_head: set[str] = set()
    for p in sorted(REPO.glob("app/**/*.html")):
        src = p.read_text(encoding="utf-8")
        for m in AFTER.finditer(src):
            need_tail.add(m.group(2))
        for m in BEFORE.finditer(src):
            need_head.add(m.group(2))
    return need_tail, need_head


#: 英文以這些標點開頭時**不可以**再補前導空白 —— `x</code> : add` 會多一個
#: 空格在冒號前面，反而更醜。中文原文的「：」「，」翻成英文就是這種形狀。
_NO_LEAD = ",.;:!?)]}%"
#: 英文以這些結尾時同理不補後綴空白。
_NO_TAIL = "([{\u201c"


def main() -> int:
    fix = "--fix" in sys.argv
    cat = json.loads(CAT.read_text(encoding="utf-8"))
    tail, head = scan()
    bad: list[str] = []
    changed = 0
    for k in sorted(tail):
        en = cat.get(k)
        if en and not en.endswith(" ") and en[-1:] not in _NO_TAIL:
            bad.append(f"結尾缺空白  {k!r} -> {en!r}")
            if fix:
                cat[k] = en + " "
                changed += 1
    for k in sorted(head):
        en = cat.get(k)
        if en and not en.startswith(" ") and en[:1] not in _NO_LEAD:
            bad.append(f"開頭缺空白  {k!r} -> {en!r}")
            if fix:
                cat[k] = " " + en
                changed += 1
    for b in bad:
        print("  " + b)
    print(f"相鄰處 {len(tail) + len(head)} 個，缺空白 {len(bad)} 條")
    if fix and changed:
        CAT.write_text(json.dumps(cat, ensure_ascii=False, indent=1) + "\n",
                       encoding="utf-8")
        print(f"已補 {changed} 條")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
