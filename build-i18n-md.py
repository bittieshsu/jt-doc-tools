#!/usr/bin/env python3
"""從中文 README 產出英文版（`README.md` → `README_en.md`）。

跟介紹站同一個原則：**中文版是唯一的來源，英文版用生成的**。同一份文件放兩個
地方一定會漂 —— 這個專案已經吃過好幾次虧。

Markdown 用**逐行**對照（一行一條）：README 的每一行本來就自成一個句子或項目，
比在行內拆標記可靠得多。程式碼區塊（``` 圍起來的）整段跳過。

用法：
    python3 github/build-i18n-md.py --extract    # 抽出待翻的行
    python3 github/build-i18n-md.py              # 產生 README_en.md
    python3 github/build-i18n-md.py --check      # 只檢查有沒有漏翻
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

GH = Path(__file__).resolve().parent
I18N = GH / "docs" / "i18n"
CJK = re.compile(r"[㐀-鿿]")
FENCE = re.compile(r"^\s*```")
# 版本號每一版都會變。把它換成佔位符再查表，否則「標題」那一行每次 bump
# 就變成新的鍵 —— 譯文查不到、英文版標題無聲地退回中文（v1.14.95 踩到）。
VER = re.compile(r"v\d+\.\d+\.\d+")
VER_PH = "vX.Y.Z"


def _key(line: str) -> str:
    return VER.sub(VER_PH, line)


def _apply(rep: str, line: str) -> str:
    """把譯文裡的佔位符換回這一行實際的版本號。"""
    m = VER.search(line)
    return rep.replace(VER_PH, m.group(0)) if m else rep


def _lines(md: str) -> list[str]:
    """回傳要翻的行（去重、保持順序）。程式碼區塊整段跳過。"""
    out, in_code = [], False
    for ln in md.splitlines():
        if FENCE.match(ln):
            in_code = not in_code
            continue
        if in_code or not CJK.search(ln):
            continue
        t = _key(ln.strip())
        if t and t not in out:
            out.append(t)
    return out


def extract(src: Path, cat_path: Path) -> int:
    md = src.read_text(encoding="utf-8")
    cat = json.loads(cat_path.read_text(encoding="utf-8")) if cat_path.exists() else {}
    keys = _lines(md)
    for k in keys:
        cat.setdefault(k, "")
    cat = {k: v for k, v in cat.items() if k in keys}
    cat_path.parent.mkdir(parents=True, exist_ok=True)
    cat_path.write_text(json.dumps(cat, ensure_ascii=False, indent=1) + "\n",
                        encoding="utf-8")
    todo = sum(1 for v in cat.values() if not v)
    print(f"{src.name}: {len(keys)} 行，未翻 {todo}")
    return todo


def build(src: Path, cat_path: Path, dst: Path) -> int:
    md = src.read_text(encoding="utf-8")
    cat = json.loads(cat_path.read_text(encoding="utf-8"))
    out, in_code, missing = [], False, 0
    for ln in md.splitlines():
        if FENCE.match(ln):
            in_code = not in_code
            out.append(ln)
            continue
        if in_code or not CJK.search(ln):
            out.append(ln)
            continue
        raw = ln.strip()
        rep = cat.get(_key(raw)) or ""
        if not rep:
            missing += 1
            out.append(ln)                      # 沒翻的原樣留著中文
        else:
            out.append(ln.replace(raw, _apply(rep, raw), 1))  # 保留縮排與清單符號
    dst.write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"{dst.name}: 產生完成（{missing} 行還沒翻）")
    return missing


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--extract", action="store_true")
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()
    src, cat, dst = GH / "README.md", I18N / "readme.en.json", GH / "README_en.md"
    if a.extract or a.check:
        return 1 if extract(src, cat) and a.check else 0
    return 1 if build(src, cat, dst) else 0


if __name__ == "__main__":
    raise SystemExit(main())
