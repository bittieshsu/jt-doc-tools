"""jt-live-whisper 的縮寫在**使用者看得到的文字**裡一律寫 `JTLW`（使用者 2026-09-23 指示）。

**識別字維持小寫**，那些改了會壞：網址（`/admin/jtlw`、`/admin/api/jtlw/…`）、
模組名（`jtlw_client`）、`requires_setup="jtlw"`、設定匯出的 id、設定檔名、
CSS class（`jtlw-name`）、頁內錨點（`#jtlw-connect`）。

判準：一個獨立的小寫 `jtlw` —— 前後都不是英數、`_`、`/`、`-`（那些是網址、模組名、
檔名、class），也不是被同一種引號包住（那是識別字的值）—— 就是給人看的文字，要大寫。
程式註解與 docstring 不算（不是使用者看得到的）。

**更新記錄只看最新一版**：舊條目裡有幾處 `jtlw` 講的是**網址的尾段**
（「尾段 `jtlw` 撞上的」），那是識別字，不可以改。
"""
from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.repo_paths import public_root as _public_root  # noqa: E402

PUB = _public_root(ROOT)

# 自己寫一份判準，不共用轉換腳本的定義（判準跟著被驗的東西一起動就驗不到東西）
_WORD = re.compile(r"(?<![A-Za-z0-9_/.\-])jtlw(?![A-Za-z0-9_/\-])")
_QUOTES = "\"'`"


def _offenders(text: str) -> list[str]:
    bad = []
    for m in _WORD.finditer(text):
        i = m.start()
        a = text[i - 1] if i else ""
        b = text[i + 4] if i + 4 < len(text) else ""
        if a in _QUOTES and a == b:
            continue                    # "jtlw" / `jtlw` —— 識別字
        bad.append(text[max(0, i - 25): i + 25].replace("\n", " "))
    return bad


def _py_strings(path: Path) -> str:
    """字串常數（不含 docstring）接成一段文字。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docs = set()
    for n in ast.walk(tree):
        body = getattr(n, "body", None)
        if isinstance(body, list) and body and isinstance(body[0], ast.Expr) \
                and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
            docs.add(id(body[0].value))
    out = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docs:
            # 字串的值本身沒有引號 —— 補回去，讓「剛好等於 jtlw」的值照樣被當成識別字
            out.append(f"\"{n.value}\"")
    return "\n".join(out)


def _strip_comments(html: str) -> str:
    return re.sub(r"\{#.*?#\}|<!--.*?-->", "", html, flags=re.S)


def _sources() -> dict[str, str]:
    out: dict[str, str] = {}
    for p in sorted((ROOT / "app").rglob("*.py")):
        out[p.relative_to(ROOT).as_posix()] = _py_strings(p)
    for p in sorted((ROOT / "app").rglob("*.html")):
        out[p.relative_to(ROOT).as_posix()] = _strip_comments(p.read_text(encoding="utf-8"))
    for lang in ("en", "ja"):
        d = json.loads((ROOT / "app" / "i18n" / f"{lang}.json").read_text(encoding="utf-8"))
        out[f"app/i18n/{lang}.json"] = "\n".join(f"\"{k}\"\n\"{v}\"" for k, v in d.items())
    for name in ("README.md", "API.md"):
        out[name] = (PUB / name).read_text(encoding="utf-8")
    for name in ("index.html", "troubleshooting.html"):
        out[f"docs/{name}"] = _strip_comments((PUB / "docs" / name).read_text(encoding="utf-8"))
    for p in sorted((PUB / "docs" / "i18n").glob("*.json")):
        d = json.loads(p.read_text(encoding="utf-8"))
        out[f"docs/i18n/{p.name}"] = "\n".join(f"\"{k}\"\n\"{v}\"" for k, v in d.items())
    for name in ("CHANGELOG.md", "CHANGELOG_en.md", "CHANGELOG_ja.md"):
        text = (PUB / name).read_text(encoding="utf-8")
        heads = [m.start() for m in re.finditer(r"^## \[", text, re.M)]
        out[f"{name}(最新版)"] = text[heads[0]:heads[1]] if len(heads) > 1 else text
    return out


SOURCES = _sources()


@pytest.mark.parametrize("name", sorted(SOURCES))
def test_user_visible_text_writes_jtlw_in_capitals(name):
    bad = _offenders(SOURCES[name])
    assert not bad, (f"{name} 有小寫的 jtlw（縮寫要大寫 JTLW；識別字不算）：\n  "
                     + "\n  ".join(bad))


def test_the_scan_reaches_the_places_that_name_it():
    """掃 0 處跟「都合格」長得一樣 —— 先證明這些地方真的寫著 JTLW。"""
    must = ["app/core/jtlw_client.py", "app/admin/templates/admin_jtlw.html",
            "app/i18n/en.json", "app/i18n/ja.json", "docs/index.html", "README.md"]
    for m in must:
        assert "JTLW" in SOURCES[m], f"{m} 裡一個 JTLW 都沒有 —— 掃描範圍大概錯了"


@pytest.mark.parametrize("ident", [
    '/admin/jtlw', 'jtlw_client', 'requires_setup="jtlw"', 'class="jtlw-name"',
    '#jtlw-connect', '"rekey": "jtlw"', '尾段 `jtlw` 撞上的',
])
def test_identifiers_are_not_flagged(ident):
    """反向對照：識別字不可以被當成違規 —— 不然有人會照著把網址也改成大寫。"""
    assert not _offenders(ident), ident


@pytest.mark.parametrize("text", ["語音服務（jtlw）", "jtlw 回報：x", "via jtlw.", "JTDT → jtlw"])
def test_prose_is_flagged(text):
    assert _offenders(text), text
