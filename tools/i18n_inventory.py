#!/usr/bin/env python3
"""i18n 盤點：把「要翻的 UI 字串」跟「絕對不能翻的中文資料」分開，並算出各層工作量。

**為什麼要先做這一支**：這個專案有 1,100 多條中文是**領域資料**（表單欄位的
標籤關鍵字、會計科目詞庫、去識別化的式子）—— 它們比對的是客戶文件裡的中文，
翻掉會讓功能**安靜地失效**（表單自動填寫抓不到欄位、去識別化什麼都遮不到，
而畫面上看起來都正常）。任何自動抽取工具都會把它們一起掃進去，所以在動手翻譯
之前，得先有一份分得清楚的清單。

用法：
    python tools/i18n_inventory.py            # 摘要
    python tools/i18n_inventory.py --list shell > shell-strings.txt
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CJK = re.compile(r"[㐀-鿿]")

#: **這些檔案裡的中文是資料，不是介面文字 —— 一個字都不可以進語系檔。**
#: 它們比對的是客戶文件裡的中文（欄位標籤、科目名稱、個資樣態）。
DOMAIN_DATA = (
    "app/core/pdf_form_detect.py",
    "app/core/pdf_layout.py",
    "app/core/same_as_ref.py",
    "app/tools/einvoice_scan/accounting_classifier.py",
    # 文字去識別化共用同一份式子，不另外列
    "app/tools/doc_deident/patterns.py",
    "app/core/vat_db.py",
)

#: 階段 A 的範圍：共用外殼。工具內部的字串留到階段 B 逐支做。
SHELL_FILES = (
    "app/web/templates/base.html",
    "app/web/templates/home.html",
    "app/web/templates/login.html",
    "app/web/templates/my_jobs.html",
    "app/web/templates/my_workspace.html",
    "app/web/templates/setup_admin.html",
    "app/web/templates/twofa_verify.html",
    "app/web/templates/me_2fa.html",
)
SHELL_DIRS = ("app/web/templates/components/", "static/js/")


def bucket_for(rel: str) -> str:
    if rel in DOMAIN_DATA:
        return "domain-data"
    if rel in SHELL_FILES or any(rel.startswith(d) for d in SHELL_DIRS):
        return "shell"
    if rel.startswith("app/admin/"):
        return "admin"
    if rel.startswith("app/tools/"):
        return "tool"
    return "core"


def py_ui_strings(path: Path) -> list[str]:
    """Python 裡使用者看得到的字串：例外訊息、指定給 message / label 這類欄位的。

    **排除 docstring**（跟用詞守門同一套做法）—— 註解與說明不是介面文字。
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return []
    msg_calls = {"HTTPException", "ValueError", "RuntimeError"}
    ui_names = {"message", "msg", "detail", "label", "title", "name",
                "description", "note", "hint", "error", "text", "summary"}
    found: set[str] = set()

    def take(node):
        for a in ast.walk(node):
            if isinstance(a, ast.Constant) and isinstance(a.value, str) and CJK.search(a.value):
                found.add(a.value)

    for n in ast.walk(tree):
        if isinstance(n, ast.Call):
            fn = n.func.id if isinstance(n.func, ast.Name) else getattr(n.func, "attr", "")
            if fn in msg_calls:
                take(n)
        elif isinstance(n, ast.Assign):
            t = n.targets[0]
            if (getattr(t, "id", None) or getattr(t, "attr", None)) in ui_names:
                take(n.value)
        elif isinstance(n, ast.Dict):
            for k, v in zip(n.keys, n.values):
                if (isinstance(k, ast.Constant) and k.value in ui_names
                        and isinstance(v, ast.Constant) and isinstance(v.value, str)
                        and CJK.search(v.value)):
                    found.add(v.value)
    return sorted(found)


def all_py_strings(path: Path) -> list[str]:
    """整個檔案裡的中文字串常數（排除 docstring）—— 領域資料用這個算。"""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return []
    docs = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            b = n.body
            if (b and isinstance(b[0], ast.Expr) and isinstance(b[0].value, ast.Constant)
                    and isinstance(b[0].value.value, str)):
                docs.add(id(b[0].value))
    return [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and id(n) not in docs and CJK.search(n.value)]


def html_strings(path: Path) -> tuple[list[str], list[str]]:
    """回傳 (標記文字, 行內 JS 字串)。註解先去掉，否則會把反例也算進去。"""
    s = path.read_text(encoding="utf-8", errors="ignore")
    s = re.sub(r"<!--.*?-->", "", s, flags=re.S)
    scripts = re.findall(r"<script\b[^>]*>(.*?)</script\s*>", s, flags=re.S | re.I)
    body = re.sub(r"<script\b.*?</script\s*>", " ", s, flags=re.S | re.I)
    body = re.sub(r"<style\b.*?</style>", " ", body, flags=re.S | re.I)
    text = re.findall(r">([^<>]*[㐀-鿿][^<>]*)<", body)
    attrs = re.findall(
        r'(?:placeholder|title|alt|aria-label|value)="([^"]*[㐀-鿿][^"]*)"', body)
    js = []
    for blk in scripts:
        js += js_strings(blk)
    return sorted({t.strip() for t in text + attrs if t.strip()}), sorted(set(js))


def js_strings(text: str) -> list[str]:
    text = re.sub(r"//[^\n]*", "", text)
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return [m for m in re.findall(
        r"""['"`]([^'"`\n]*[㐀-鿿][^'"`\n]*)['"`]""", text)]


def tool_metadata_strings() -> list[str]:
    """47 支工具的名稱與說明 —— 階段 A 一定要翻的部分（側欄與首頁卡片就是它們）。"""
    sys.path.insert(0, str(ROOT))
    import importlib
    out = []
    for d in sorted((ROOT / "app" / "tools").iterdir()):
        if not d.is_dir() or not (d / "__init__.py").exists():
            continue
        try:
            m = importlib.import_module(f"app.tools.{d.name}")
        except Exception:      # noqa: BLE001 - 掃描工具，載不進來就跳過
            continue
        md = getattr(m, "metadata", None)
        if md is None:
            continue
        for attr in ("name", "description"):
            v = getattr(md, attr, "")
            if isinstance(v, str) and CJK.search(v):
                out.append(v)
    return out


def collect() -> dict:
    per_bucket = defaultdict(list)
    per_file = Counter()
    for path in list((ROOT / "app").rglob("*.py")) + list((ROOT / "app").rglob("*.html")) \
            + list((ROOT / "static").rglob("*.js")):
        if "__pycache__" in str(path):
            continue
        rel = str(path.relative_to(ROOT))
        b = bucket_for(rel)
        if path.suffix == ".py":
            # 領域資料要算**全部**的中文（重點是警告「這裡有多少不可翻的東西」），
            # 其餘檔案只算看得到的訊息
            items = all_py_strings(path) if b == "domain-data" else py_ui_strings(path)
        elif path.suffix == ".html":
            text, js = html_strings(path)
            items = text + js
        else:
            items = js_strings(path.read_text(encoding="utf-8", errors="ignore"))
        if items:
            per_bucket[b] += items
            per_file[rel] = len(items)
    per_bucket["shell"] += tool_metadata_strings()
    return {"buckets": per_bucket, "files": per_file}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", choices=("shell", "admin", "tool", "core", "domain-data"))
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    r = collect()
    if a.list:
        for s in sorted(set(r["buckets"][a.list])):
            print(s)
        return 0
    if a.json:
        print(json.dumps({k: len(set(v)) for k, v in r["buckets"].items()},
                         ensure_ascii=False, indent=2))
        return 0

    order = ("shell", "tool", "admin", "core", "domain-data")
    print("i18n 盤點（去重後的字串數）\n")
    for b in order:
        n = len(set(r["buckets"][b]))
        tag = "  ← 階段 A" if b == "shell" else ("  ← **不可翻**" if b == "domain-data" else "")
        print(f"  {b:<12} {n:>5}{tag}")
    trans = sum(len(set(r['buckets'][b])) for b in ("shell", "tool", "admin", "core"))
    print(f"\n  要翻的合計   {trans:>5}")
    print(f"  其中階段 A   {len(set(r['buckets']['shell'])):>5} "
          f"（{len(set(r['buckets']['shell'])) * 100 // max(1, trans)}%）")
    print("\n字串最多的十個檔案：")
    for f, c in r["files"].most_common(10):
        print(f"  {c:>4}  {f}  [{bucket_for(f)}]")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BrokenPipeError:      # `| head` 會這樣，不是錯誤
        sys.stderr.close()
        raise SystemExit(0)
