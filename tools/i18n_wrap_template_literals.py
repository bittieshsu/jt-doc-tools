#!/usr/bin/env python3
"""包 template literal 裡**HTML 文字節點**的中文。

`el.innerHTML = \\`<b>共 ${n} 筆</b><span>已完成</span>\\`` 這種 —— 整條當 key 不行
（含變數、含標籤），但**裡面的文字節點可以逐個包**：`>${tr('已完成')}<`。
template literal 本來就支援 `${}`，所以放 `tr()` 進去是合法的。

只在**顯示脈絡**動（innerHTML / outerHTML / insertAdjacentHTML / return / join
/ push / map 的結果）。文字節點裡有 `${`、引號、`&`（HTML 實體）的一律跳過。
"""
import re, sys, pathlib

SCRIPT = re.compile(r"(<script\b[^>]*>)(.*?)(</script>)", re.S | re.I)
CJK = re.compile(r"[㐀-鿿]")
TPL = re.compile(r"`((?:[^`\\]|\\.)*)`", re.S)
COMMENT = re.compile(r"//[^\n]*|/\*.*?\*/", re.S)
SAFE = [r"\.innerHTML\s*=\s*$", r"\.outerHTML\s*=\s*$", r"\.innerHTML\s*\+=\s*$",
        r"insertAdjacentHTML\([^,]*,\s*$", r"\breturn\s+$", r"push\(\s*$",
        r"=>\s*$"]
#: 文字節點：`>` 之後到 `<` 或**literal 結尾**之間。只認 `<` 的話，
#: `<span class="spinner"></span>合成中…` 這種收尾的文字會整條漏掉。
NODE = re.compile(r"(>)([^<>{}`\n]+)(<|$)")


def _wrap_nodes(lit: str) -> tuple[str, int]:
    n = 0

    def rep(m):
        nonlocal n
        raw = m.group(2)
        t = raw.strip()
        if not t or not CJK.search(t):
            return m.group(0)
        if "'" in t or "\\" in t or "&" in t or "${" in t:
            return m.group(0)
        lead = raw[:len(raw) - len(raw.lstrip())]
        trail = raw[len(raw.rstrip()):]
        n += 1
        return f">{lead}${{tr('{t}')}}{trail}<"

    return NODE.sub(rep, lit), n


def wrap_block(body: str) -> tuple[str, int]:
    holes = [(m.start(), m.end()) for m in COMMENT.finditer(body)]
    in_comment = lambda i: any(a <= i < b for a, b in holes)
    out, last, total = [], 0, 0
    for m in TPL.finditer(body):
        lit = m.group(1)
        if in_comment(m.start()) or not CJK.search(lit) or "<" not in lit:
            continue
        before = body[max(0, m.start() - 60):m.start()]
        # **含 HTML 標記的 template literal 幾乎一定是要塞進 innerHTML 的** ——
        # 只認 `.innerHTML =` / `return` 這幾個位置會漏掉 `html = \`<h3>…\`` 這種
        # 先組字串再塞的寫法（PDF 編輯器整個屬性面板都是這樣寫的）。
        looks_like_markup = re.search(r"<[a-zA-Z][^>]*>", lit) is not None
        if not looks_like_markup and not any(re.search(s, before) for s in SAFE):
            continue
        new, n = _wrap_nodes(lit)
        if not n:
            continue
        out.append(body[last:m.start(1)])
        out.append(new)
        last = m.end(1)
        total += n
    out.append(body[last:])
    return "".join(out), total


def wrap(path: pathlib.Path) -> int:
    src = path.read_text(encoding="utf-8")
    total, parts, last = 0, [], 0
    for m in SCRIPT.finditer(src):
        new, n = wrap_block(m.group(2))
        if n:
            parts.append(src[last:m.start(2)]); parts.append(new)
            last = m.end(2); total += n
    if total:
        parts.append(src[last:])
        path.write_text("".join(parts), encoding="utf-8")
    return total


if __name__ == "__main__":
    t = 0
    for a in sys.argv[1:]:
        p = pathlib.Path(a)
        for f in (sorted(p.rglob("*.html")) if p.is_dir() else [p]):
            c = wrap(f)
            if c:
                print(f"  {c:4}  {f}")
            t += c
    print("共包了", t, "個文字節點")
