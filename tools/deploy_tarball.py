#!/usr/bin/env python3
"""建立部署用的 tarball，並且**驗過內容**才交出去。

存在的理由：往外送的路徑只有三條 —— `sync-to-github.sh`（有守門）、
部署 tarball（原本沒有）、以及臨時手打的複製指令。第二條是每一版都會走的，
所以它不可以靠人記得下對 `--exclude`。

2026-09-18 實際踩到：照 CLAUDE.md 寫的鏡像流程跑 `rsync ./ /tmp/jtdt-test-src/`
（那段是 macOS 時代的寫法，當時來源只有 `app/`），把 `docs-share/` 的
內部往來文件與 `temp_pdfs/` 的客戶樣本一起複製到 `/tmp` —— 而這台機器
同時有別的專案在跑。沒有外流，但只差一個 `git push`。

**判準放在產出的 tarball 上，不是放在 `--exclude` 參數上** ——
參數寫對了不代表沒有別的路徑漏進來（符號連結、之後有人加新目錄）。
"""
from __future__ import annotations

import subprocess
import sys
import tarfile
from pathlib import Path

#: 交付給部署目標的東西。加新的頂層目錄時要**明確**列進來 ——
#: 用「排除清單」的話，下一個新目錄預設會被送出去。
SHIP = (
    "app", "static", "tests", "tools", "scripts",
    "pyproject.toml", "uv.lock", "requirements.txt", "run.py", "TEST_PLAN.md",
)

#: 一個位元組都不可以出現在 tarball 裡的路徑前綴。
#: 每一條都寫得出後果，不是「看起來不該送」。
PRIVATE_PREFIXES = {
    "docs-share/": "內部往來文件（使用者指示不可外流）",
    "temp_pdfs/": "真實廠商表單與客戶資料",
    "data/": "正式資料目錄；蓋過去會毀掉部署目標上的使用者資料",
    "releases/": "已簽章的安裝檔，體積大且不需要部署",
    "temp/": "工作暫存，含掃描報告與實驗素材",
    ".git/": "版控目錄",
}

#: 附檔名層級的封鎖（跟目錄無關）。
PRIVATE_SUFFIXES = {
    ".sqlite": "資料庫",
    ".pem": "私鑰",
    ".key": "私鑰",
}


def offending(names) -> list[str]:
    """回傳 tarball 裡不該存在的項目（含理由）。"""
    bad: list[str] = []
    for raw in names:
        name = raw.lstrip("./")
        for prefix, why in PRIVATE_PREFIXES.items():
            if name == prefix.rstrip("/") or name.startswith(prefix):
                bad.append(f"{raw}  <- {why}")
                break
        else:
            for suffix, why in PRIVATE_SUFFIXES.items():
                if name.endswith(suffix):
                    bad.append(f"{raw}  <- {why}")
                    break
    return bad


def build(root: Path, out: Path) -> None:
    missing = [item for item in SHIP if not (root / item).exists()]
    if missing:
        raise SystemExit(f"[X] 交付清單裡的項目不存在：{', '.join(missing)}")
    cmd = [
        "tar", "--exclude=__pycache__", "--exclude=*.pyc", "--exclude=.pytest_cache",
        "-czf", str(out), *SHIP,
    ]
    subprocess.run(cmd, cwd=root, check=True)


def verify(out: Path) -> None:
    with tarfile.open(out, "r:gz") as tf:
        names = tf.getnames()
    if not names:
        raise SystemExit("[X] tarball 是空的")
    bad = offending(names)
    if bad:
        out.unlink(missing_ok=True)
        print("[X] tarball 含有不可外送的內容（已刪除該檔）：", file=sys.stderr)
        for line in bad[:20]:
            print(f"      {line}", file=sys.stderr)
        if len(bad) > 20:
            print(f"      …另外 {len(bad) - 20} 筆", file=sys.stderr)
        raise SystemExit(1)
    print(f"[OK] {out}  {len(names)} 個項目，機敏路徑 0")


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("用法：python tools/deploy_tarball.py <輸出的 .tgz 路徑>")
    root = Path(__file__).resolve().parent.parent
    out = Path(sys.argv[1]).resolve()
    build(root, out)
    verify(out)


if __name__ == "__main__":
    main()
