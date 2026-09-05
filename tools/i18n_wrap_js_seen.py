#!/usr/bin/env python3
"""只包「**瀏覽器上真的看得到**」的 JS 字串。

為什麼要用掃描結果當輸入：程式裡的中文字串有一千多條，其中一大半**不是介面
文字** —— 是選擇器、物件的鍵、跟後端回傳值做比較的字面值。把那些包進 `tr()`
會在英文介面上安靜地壞掉（比較永遠不成立、選擇器選不到東西），而中文介面完全
正常，等於埋一顆只在英文炸的雷。

所以輸入是 `tools/i18n_untranslated_scan.py` 產生的 report.json：那份清單裡的
每一條都是**真的被畫到畫面上**的字。

即使如此仍然排除三種脈絡（同一個字串可能兩種用途）：

* 物件的鍵（`'中文':`）
* 比較（`=== '中文'` / `!== '中文'`）
* `querySelector` / `getAttribute` / `setAttribute` 的參數

用法：
    python tools/i18n_wrap_js_seen.py temp/i18n-scan/<run>/report.json
    python tools/i18n_wrap_js_seen.py <report> --dry
"""
from __future__ import annotations

import json
import pathlib
import re
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = re.compile(r"(<script\b[^>]*>)(.*?)(</script\s*>)", re.S | re.I)
COMMENT = re.compile(r"//[^\n]*|/\*.*?\*/", re.S)
BAD_BEFORE = re.compile(r"(===|!==|==|!=|querySelector(All)?\(|"
                        r"getAttribute\(|setAttribute\(|classList\.[a-z]+\()\s*$")


def _wrap_block(body: str, wanted: set[str]) -> tuple[str, int]:
    holes = [(m.start(), m.end()) for m in COMMENT.finditer(body)]
    in_comment = lambda i: any(a <= i < b for a, b in holes)
    out, last, n = [], 0, 0
    for m in re.finditer(r"(['\"])((?:[^'\"\\\n]|\\.)*?)\1", body):
        s = m.group(2)
        if s not in wanted or in_comment(m.start()):
            continue
        before = body[max(0, m.start() - 40):m.start()]
        after = body[m.end():m.end() + 2]
        if before.rstrip().endswith("tr("):        # 已經包過
            continue
        if BAD_BEFORE.search(before):              # 比較 / 選擇器
            continue
        if after.lstrip().startswith(":"):         # 物件的鍵
            continue
        out.append(body[last:m.start()])
        out.append(f"tr({m.group(1)}{s}{m.group(1)})")
        last = m.end()
        n += 1
    out.append(body[last:])
    return "".join(out), n


def wrap_file(p: pathlib.Path, wanted: set[str], dry: bool) -> int:
    src = p.read_text(encoding="utf-8")
    if p.suffix == ".js":
        new, n = _wrap_block(src, wanted)
        if n and not dry:
            p.write_text(new, encoding="utf-8")
        return n
    parts, last, total = [], 0, 0
    for m in SCRIPT.finditer(src):
        new, n = _wrap_block(m.group(2), wanted)
        if n:
            parts.append(src[last:m.start(2)])
            parts.append(new)
            last = m.end(2)
            total += n
    if total and not dry:
        parts.append(src[last:])
        p.write_text("".join(parts), encoding="utf-8")
    return total


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    dry = "--dry" in sys.argv
    report = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
    wanted = {h["text"] for hits in report.values() for h in hits}
    files = (sorted(REPO.glob("app/**/*.html"))
             + sorted(REPO.glob("static/js/*.js")))
    total = 0
    for f in files:
        n = wrap_file(f, wanted, dry)
        if n:
            print(f"  {n:4}  {f.relative_to(REPO)}")
            total += n
    print(("（試跑）" if dry else "") + f"共包 {total} 條")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
