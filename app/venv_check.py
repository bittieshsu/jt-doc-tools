"""Python 環境（venv）跟它底下的 Python 對不對得上 —— **只用標準函式庫**。

Linux 上舊的安裝把 venv 建在系統的 Python 上（`.venv/bin/python -> /usr/bin/python3`）。
作業系統升級換掉系統 Python（例如 Ubuntu 22.04 → 24.04，3.10 → 3.12）之後，venv 還在，
但套件裝在 `lib/python3.10/`，新的 3.12 看不到 —— 服務每次啟動都 `ModuleNotFoundError`，
而記錄裡看不出原因（2026-10-05 在另一個專案踩到，2026-10-08 使用者同意修）。

這支在 `app.main` 一開頭、任何第三方套件 import 之前就檢查：對不上就寫一行講清楚要做什麼
（`sudo jtdt update` 會用安裝目錄裡自己的 Python 重建），然後結束。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Optional

#: 對不上時的結束碼（sysexits 的 EX_CONFIG）
EXIT_CODE = 78


def built_version(venv_dir: Path) -> Optional[str]:
    """venv 是用哪一版 Python 建的（`主.次`）；讀不到回 None。"""
    try:
        text = (venv_dir / "pyvenv.cfg").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for key in ("version_info", "version"):
        m = re.search(rf"(?m)^\s*{key}\s*=\s*(\d+)\.(\d+)", text)
        if m:
            return f"{m.group(1)}.{m.group(2)}"
    return None


def mismatch(prefix: Optional[str] = None, base_prefix: Optional[str] = None,
             running: Optional[tuple] = None) -> Optional[tuple[str, str]]:
    """現在這個行程的 venv 對不上底下的 Python 時回 `(建的版本, 現在的版本)`；沒問題或不在 venv 裡回 None。"""
    prefix = sys.prefix if prefix is None else prefix
    base_prefix = sys.base_prefix if base_prefix is None else base_prefix
    if prefix == base_prefix:
        return None
    built = built_version(Path(prefix))
    if not built:
        return None
    v = running or sys.version_info
    cur = f"{v[0]}.{v[1]}"
    return (built, cur) if built != cur else None


def message(built: str, cur: str) -> str:
    # 服務記錄（journal / launchd 記錄檔）裡的一行：英文 ASCII，終端機與記錄檢視器都顯示得出來
    return (f"jt-doc-tools: the Python environment was built with Python {built}, but the "
            f"Python it runs on is now {cur} (operating-system upgrade?). The installed "
            f"packages are not visible to Python {cur}, so the service cannot start. "
            f"Fix: run `sudo jtdt update` - it rebuilds the environment with a private "
            f"Python inside the install directory.")


def exit_if_mismatched() -> None:
    m = mismatch()
    if m:
        print(message(*m), file=sys.stderr, flush=True)
        raise SystemExit(EXIT_CODE)
