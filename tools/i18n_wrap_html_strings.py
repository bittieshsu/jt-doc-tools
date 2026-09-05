#!/usr/bin/env python3
"""把 JS 裡「**整段 HTML 寫在一個字串**」的中文包起來。

`list.innerHTML = '<div class="muted">尚無資料</div>'` 這種：整條字串當 key 不行
（含標籤，翻譯的人看到的是 HTML），但**裡面的文字節點可以逐個包**，接成
`'<div class="muted">' + tr('尚無資料') + '</div>'`。

只動 `>` 與 `<` 之間的文字，標籤與屬性一個字都不碰。文字裡含引號、反斜線、
`${`、HTML 實體的一律跳過 —— 接錯字串會直接是語法錯誤或是無聲的錯字。

用法：
    python tools/i18n_wrap_html_strings.py --dry app static/js
    python tools/i18n_wrap_html_strings.py app static/js
"""
from __future__ import annotations

import pathlib
import re
import sys

SCRIPT = re.compile(r"(<script\b[^>]*>)(.*?)(</script\s*>)", re.S | re.I)
COMMENT = re.compile(r"//[^\n]*|/\*.*?\*/", re.S)
CJK = re.compile("[㐀-鿿]")
#: 單引號字串（JS 裡最常見的寫法；雙引號的留給下一輪，混著改容易接錯）
STR = re.compile(r"'((?:[^'\\\n]|\\.)*)'")
NODE = re.compile(r"(>)([^<>{}]+)(<)")


def _rewrite(lit: str) -> tuple[str, int]:
    n = 0

    def rep(m):
        nonlocal n
        raw = m.group(2)
        t = raw.strip()
        if not t or not CJK.search(t):
            return m.group(0)
        if any(c in t for c in ("'", "\\", "&", "$")):
            return m.group(0)
        lead = raw[:len(raw) - len(raw.lstrip())]
        trail = raw[len(raw.rstrip()):]
        n += 1
        return f">{lead}' + tr('{t}') + '{trail}<"

    return NODE.sub(rep, lit), n


def _block(body: str) -> tuple[str, int]:
    holes = [(m.start(), m.end()) for m in COMMENT.finditer(body)]
    in_comment = lambda i: any(a <= i < b for a, b in holes)
    out, last, total = [], 0, 0
    for m in STR.finditer(body):
        lit = m.group(1)
        if in_comment(m.start()):
            continue
        if "<" not in lit or ">" not in lit or not CJK.search(lit):
            continue
        if body[max(0, m.start() - 40):m.start()].rstrip().endswith("tr("):
            continue
        new, n = _rewrite(lit)
        if not n:
            continue
        out.append(body[last:m.start(1)])
        out.append(new)
        last = m.end(1)
        total += n
    out.append(body[last:])
    return "".join(out), total


def wrap(p: pathlib.Path, dry: bool) -> int:
    src = p.read_text(encoding="utf-8")
    if p.suffix == ".js":
        new, n = _block(src)
        if n and not dry:
            p.write_text(new, encoding="utf-8")
        return n
    parts, last, total = [], 0, 0
    for m in SCRIPT.finditer(src):
        new, n = _block(m.group(2))
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
    dry = "--dry" in sys.argv
    args = [a for a in sys.argv[1:] if not a.startswith("--")] or ["app", "static/js"]
    total = 0
    for a in args:
        root = pathlib.Path(a)
        files = (sorted(root.rglob("*.html")) + sorted(root.rglob("*.js"))
                 if root.is_dir() else [root])
        for f in files:
            n = wrap(f, dry)
            if n:
                print(f"  {n:4}  {f}")
                total += n
    print(("（試跑）" if dry else "") + f"共包 {total} 個文字節點")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
