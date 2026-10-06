"""批次檔（.cmd / .bat）裡，區塊中的 echo 不可以有沒跳脫的右括號；記錄裡不寫 %DATE%。

## 由來（v1.16.56，Windows 實機全新安裝時在 installer.log 看到）

setup-python.cmd 驗 EasyOCR 那段：

    if !ERRORLEVEL! equ 0 (
        echo [OK] EasyOCR available
    ) else (
        echo [WARN] EasyOCR not installed - OCR will fall back to tesseract (lower CJK accuracy)
        echo [WARN]   Manual install: ...
    )

第三行文字裡的 ``)`` 被 cmd 當成 ``else (`` 區塊的結尾，於是下一行「請手動安裝」
**跑到區塊外面、不管成功失敗都會印** —— installer.log 裡 ``[OK] EasyOCR available``
後面緊接著叫人手動裝 EasyOCR，讀記錄的人會以為裝壞了。cmd 不會報任何錯。

同一份記錄的時間戳記寫的是 ``%DATE%``：在介面語言是中文、非 Unicode 程式的系統地區
是英文的機器上，``%DATE%`` 開頭的中文星期幾被 cmd 用 OEM 字碼頁寫進檔案，變成
``[?? 2026/10/06 …]``。``%DATE%`` 的格式本來就隨地區設定而變，記錄只寫 ``%TIME%``。

## 判準

* 區塊（括號）裡的 echo，去掉雙引號包住的部分之後，不可以有沒跳脫（``^)``）的 ``)``。
* .cmd / .bat 不可以出現 ``%DATE%``。
* 先證明掃描真的走進了區塊（不然「掃 0 行」跟「全部合格」長得一樣）。
"""
from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tools.repo_paths import public_root  # noqa: E402

PUB = public_root(ROOT)


def _scripts() -> list[pathlib.Path]:
    out = []
    for pat in ("*.cmd", "*.bat"):
        for p in PUB.rglob(pat):
            if any(part in (".venv", "node_modules", ".git") for part in p.parts):
                continue
            out.append(p)
    return sorted(out)


def _unquoted(s: str) -> str:
    return re.sub(r'"[^"]*"', "", s)


def _scan(text: str) -> tuple[list[tuple[int, str]], int]:
    """回傳（違規的 echo 行, 掃到的區塊內 echo 行數）。"""
    bad: list[tuple[int, str]] = []
    in_block = 0
    depth = 0
    for i, raw in enumerate(text.splitlines(), 1):
        s = raw.strip()
        if not s or s.upper().startswith("REM ") or s.upper() == "REM" or s.startswith("::"):
            continue
        is_echo = re.match(r"(?i)@?echo[\s.:]", s) is not None
        if is_echo:
            if depth > 0:
                in_block += 1
                body = _unquoted(s[s.lower().index("echo") + 4:])
                if re.search(r"(?<!\^)\)", body):
                    bad.append((i, s))
            continue
        t = _unquoted(s)
        depth += len(re.findall(r"(?<!\^)\(", t)) - len(re.findall(r"(?<!\^)\)", t))
        depth = max(depth, 0)
    return bad, in_block


def test_the_scan_reaches_echo_lines_inside_blocks():
    files = _scripts()
    assert files, "公開樹裡找不到任何 .cmd / .bat —— 搬家了的話這支檢查要跟著改"
    total = sum(_scan(p.read_text(encoding="utf-8", errors="replace"))[1] for p in files)
    assert total >= 3, f"只掃到 {total} 行區塊內的 echo，掃描大概沒走進區塊"


def test_no_echo_inside_a_block_closes_it_early():
    bad = []
    for p in _scripts():
        for ln, s in _scan(p.read_text(encoding="utf-8", errors="replace"))[0]:
            bad.append(f"{p.relative_to(PUB).as_posix()}:{ln}: {s}")
    assert not bad, (
        "區塊裡的 echo 有沒跳脫的 ')'，cmd 會把它當成區塊結尾、後面的行跑到區塊外"
        "（要寫成 ^)）：\n" + "\n".join(bad))


def test_the_scanner_catches_the_original_mistake():
    sample = (
        "if !ERRORLEVEL! equ 0 (\n"
        "    echo [OK] fine\n"
        ") else (\n"
        "    echo [WARN] falls back (lower accuracy)\n"
        "    echo [WARN] manual install\n"
        ")\n"
    )
    bad, _ = _scan(sample)
    assert [ln for ln, _ in bad] == [4]
    fixed = sample.replace("(lower accuracy)", "^(lower accuracy^)")
    assert _scan(fixed)[0] == []
    quoted = sample.replace("(lower accuracy)", '"(lower accuracy)"')
    assert _scan(quoted)[0] == [], "雙引號裡的括號 cmd 不會當成區塊結尾，不該報"


def test_scripts_do_not_log_the_locale_dependent_date():
    bad = []
    for p in _scripts():
        for i, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if line.strip().upper().startswith("REM"):
                continue
            if re.search(r"(?i)%DATE%", line):
                bad.append(f"{p.relative_to(PUB).as_posix()}:{i}: {line.strip()}")
    assert not bad, "%DATE% 隨地區設定而變（中文星期幾會在 OEM 字碼頁變成 ??）：\n" + "\n".join(bad)
