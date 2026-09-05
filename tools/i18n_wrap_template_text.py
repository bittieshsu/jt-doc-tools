#!/usr/bin/env python3
"""把 Jinja 樣板裡**畫面上看得到的中文文字節點**包成 `{{ tr('…') }}`。

為什麼要一支工具而不是手改：漏掉的字串是**無聲的** —— 切成英文之後那一段還是
中文，測試全綠、也沒有任何錯誤訊息，只有真的把介面切過去看的人才發現
（`tools/i18n_untranslated_scan.py` 就是在做這件事）。手工逐檔改必然漏。

**中文輸出必須一個位元組都不變**：`tr()` 在 zh-Hant 直接回原字串，前後空白
留在 `tr()` 外面，所以包完 `tools/i18n_zh_baseline.py --compare` 要完全一致。

一律跳過（包了會壞或會改變輸出）：

* `<script>` / `<style>` / `<textarea>` / `<pre>` 裡面（前兩者由 JS 那條路處理）
* HTML 註解與 Jinja 註解
* 含 `{{` / `{%` 的（樣板語法，包了鍵就對不上）
* 含 `&`（HTML 實體會被二次轉義）、含 `'`（引號會撞）
* 屬性值裡的中文（`title=` / `placeholder=` 另有一支）

用法：
    python tools/i18n_wrap_template_text.py app/tools/einvoice_scan   # 包
    python tools/i18n_wrap_template_text.py --dry app/               # 只看數量
"""
from __future__ import annotations

import pathlib
import re
import sys

CJK = re.compile("[㐀-鿿]")
# 不可進入的區域：這幾個標記裡的內容不是「畫面文字」就是另一條路處理。
BLOCK = re.compile(r"<(script|style|textarea|pre)\b.*?</\1\s*>", re.S | re.I)
COMMENT = re.compile(r"<!--.*?-->|\{#.*?#\}", re.S)
# `{% with hint='…<b>中文</b>…' %}` 這種：HTML 長在 Jinja 標籤的字串裡，
# 包進去會變成 `{{ }}` 巢在 `{% %}` 裡面 —— 樣板直接編不過。
JINJA_TAG = re.compile(r"\{%.*?%\}|\{\{.*?\}\}", re.S)
# 文字節點：左邊界是 `>` 或 `}}`、右邊界是 `<` 或 `{{`，中間不含標記與樣板語法。
# 只認 `>` 是不夠的 —— `<button>{{ icon('gear') }}設定</button>` 這種左邊是 `}}`，
# 全站的按鈕文字幾乎都長這樣（第一版就是這樣整批漏掉的）。
NODE = re.compile(r"(>|\}\})([^<>{}]+)(<|\{\{)")


def _holes(src: str) -> list[tuple[int, int]]:
    return ([(m.start(), m.end()) for m in BLOCK.finditer(src)]
            + [(m.start(), m.end()) for m in COMMENT.finditer(src)]
            + [(m.start(), m.end()) for m in JINJA_TAG.finditer(src)])


def wrap(src: str) -> tuple[str, int]:
    holes = _holes(src)
    inside = lambda i: any(a <= i < b for a, b in holes)
    out, last, n = [], 0, 0

    for m in NODE.finditer(src):
        if inside(m.start(2)):   # 判斷「文字本身」在不在洞裡：左邊界 `}}` 屬於 Jinja 標籤
            continue
        raw = m.group(2)
        t = raw.strip()
        if not t or not CJK.search(t):
            continue
        # `&` 只有在**是 HTML 實體**時才有問題（包起來會被二次轉義）；
        # 單純一個 & 沒關係。單引號改用雙引號包就好。
        if any(c in t for c in ("{", "}", "\\")):
            continue
        if re.search(r"&[#\w]+;", t):
            continue
        q = '"' if "'" in t else "'"
        if q == '"' and '"' in t:
            continue
        lead = raw[:len(raw) - len(raw.lstrip())]
        trail = raw[len(raw.rstrip()):]
        out.append(src[last:m.start(2)])
        out.append(f"{lead}{{{{ tr({q}{t}{q}) }}}}{trail}")
        last = m.end(2)
        n += 1
    out.append(src[last:])
    return "".join(out), n


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    dry = "--dry" in sys.argv
    total = 0
    for a in args:
        p = pathlib.Path(a)
        files = sorted(p.rglob("*.html")) if p.is_dir() else [p]
        for f in files:
            src = f.read_text(encoding="utf-8")
            new, n = wrap(src)
            if n:
                print(f"  {n:4}  {f}")
                total += n
                if not dry:
                    f.write_text(new, encoding="utf-8")
    print(("（試跑）" if dry else "") + f"共 {total} 個文字節點")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
