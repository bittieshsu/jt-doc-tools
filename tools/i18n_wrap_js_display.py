#!/usr/bin/env python3
"""包 JS 裡**顯示脈絡**的中文字串。

為什麼還要這一支：`i18n_untranslated_scan.py` 只看得到「那一次真的渲染出來」的
東西 —— **對話框、要點開才載入的面板、有資料才出現的表格、綁 0.0.0.0 才顯示的
警告列**，一律掃不到。使用者一連截了四張圖都是這幾類（2026-09-05）。

所以改成從**脈絡**判斷：字串被丟進哪個位置，決定它會不會被人看到。

**寧可漏包也不要包錯**：翻掉一個拿去比較、當鍵、或送給伺服器的字串，畫面看起來
完全正常，只有邏輯默默壞掉 —— 而且**只在英文介面才壞**。所以

* 允許清單是「一定是顯示」的位置（`textContent` / `showConfirm` / …）。
* 另外有拒絕清單擋掉同一行裡的比較、物件鍵、選擇器。
"""
from __future__ import annotations

import pathlib
import re
import sys

CJK = re.compile("[㐀-鿿]")
SCRIPT = re.compile(r"(<script\b[^>]*>)(.*?)(</script\s*>)", re.S | re.I)
COMMENT = re.compile(r"//[^\n]*|/\*.*?\*/", re.S)

#: 一定是「拿給人看」的位置。字串**緊接在這些之後**才會被包。
SAFE = [
    r"\.textContent\s*=\s*$",
    r"\.textContent\s*\+=\s*$",
    r"\.innerHTML\s*=\s*$",
    r"\.innerHTML\s*\+=\s*$",
    r"\.title\s*=\s*$",
    r"\.placeholder\s*=\s*$",
    r"\.alt\s*=\s*$",
    r"\.label\s*=\s*$",
    r"(?<![\w.])alert\(\s*$",
    r"showToast\(\s*$",
    r"showConfirm\(\s*$",
    r"showModal\(\s*$",
    r"setStatus\(\s*$",
    r"friendlyServerError\([^,()]*,\s*$",
    r"createTextNode\(\s*$",
    r"setAttribute\(\s*['\"](?:title|placeholder|aria-label|alt)['\"]\s*,\s*$",
]
#: 同一行出現這些就整條跳過 —— 那是比較 / 鍵 / 選擇器，不是顯示。
DENY_LINE = re.compile(r"===|!==|querySelector|getAttribute|classList|"
                       r"localStorage|sessionStorage|JSON\.parse|dataset\.|"
                       r"\.setAttribute\((?!\s*['\"](?:title|placeholder|aria-label|alt)['\"])")
#: `--broad`：不限定在允許清單的位置，改用「像不像一句話」判斷。
#: 短字串多半是識別字 / 比較值，長的、或帶中文標點的幾乎一定是給人看的。
SENTENCE = re.compile("[，。：；！？（）「」…、]")


def _looks_like_display(s: str) -> bool:
    # 兩個中文字以上就算 —— 分類名（「企業」）、欄位名（「負責人」）
    # 都是兩三個字，用 5 當門檻會整批漏掉。
    return len(s) >= 2 or bool(SENTENCE.search(s))


def _wrap(body: str) -> tuple[str, int]:
    holes = [(m.start(), m.end()) for m in COMMENT.finditer(body)]
    in_comment = lambda i: any(a <= i < b for a, b in holes)
    out, last, n = [], 0, 0
    for m in re.finditer(r"(['\"])((?:[^'\"\\\n]|\\.)*?)\1", body):
        s = m.group(2)
        if not CJK.search(s) or in_comment(m.start()):
            continue
        before = body[max(0, m.start() - 60):m.start()]
        if before.rstrip().endswith("tr("):
            continue
        line_start = body.rfind("\n", 0, m.start()) + 1
        line_end = body.find("\n", m.end())
        line = body[line_start:line_end if line_end > 0 else len(body)]
        if DENY_LINE.search(line):
            continue
        if not any(re.search(p, before) for p in SAFE):
            if not (BROAD and _looks_like_display(s)):
                continue
            # 物件的鍵（`'中文':`）與比較的右側一律不碰
            after = body[m.end():m.end() + 2].lstrip()
            if after.startswith(":"):
                continue
        out.append(body[last:m.start()])
        out.append(f"tr({m.group(1)}{s}{m.group(1)})")
        last = m.end()
        n += 1
    out.append(body[last:])
    return "".join(out), n


def wrap_file(p: pathlib.Path, dry: bool) -> int:
    src = p.read_text(encoding="utf-8")
    if p.suffix == ".js":
        new, n = _wrap(src)
        if n and not dry:
            p.write_text(new, encoding="utf-8")
        return n
    parts, last, total = [], 0, 0
    for m in SCRIPT.finditer(src):
        new, n = _wrap(m.group(2))
        if n:
            parts.append(src[last:m.start(2)])
            parts.append(new)
            last = m.end(2)
            total += n
    if total and not dry:
        parts.append(src[last:])
        p.write_text("".join(parts), encoding="utf-8")
    return total


BROAD = False


def main() -> int:
    global BROAD
    dry = "--dry" in sys.argv
    BROAD = "--broad" in sys.argv
    args = [a for a in sys.argv[1:] if not a.startswith("--")] or ["app", "static/js"]
    total = 0
    for a in args:
        root = pathlib.Path(a)
        files = (sorted(root.rglob("*.html")) + sorted(root.rglob("*.js"))
                 if root.is_dir() else [root])
        for f in files:
            n = wrap_file(f, dry)
            if n:
                print(f"  {n:4}  {f}")
                total += n
    print(("（試跑）" if dry else "") + f"共包 {total} 條")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
