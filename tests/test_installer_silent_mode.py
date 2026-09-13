"""安裝程式在**無介面模式**下不可以停下來等人按對話框。

## 由來（v1.15.36，第一次真的測 Release 上的安裝程式）

使用者要求「每次打 tag build 安裝檔，上去後都必須測試過」。第一次照做就抓到
兩個只有實機才看得到的問題：

1. **升級既有安裝時沒有先停服務** —— `Fetch-Code` 只在「不是 git repo」那條
   分支停，而**升級走的是 git 那條**。服務還跑著 → `uv venv --clear` 刪不掉
   被 `python.exe` 佔住的 `.venv` → `[X] uv venv failed`。
2. **失敗之後安裝程式掛住不退出** —— 失敗路徑的
   `MessageBox MB_ICONSTOP "$(ERR_INSTALL)"` **沒有 `/SD`**。
   無介面安裝（`/S`，或從遠端管理跑）根本沒有桌面可以按那個對話框，
   於是它**永遠等下去**：setup.exe 掛著、CPU 0、使用者只看到「什麼都沒發生」。
   實測卡了 30 分鐘才被發現。

## 判準

掃 NSIS 原始碼與 PowerShell core：
- 每一個 `MessageBox` 都要有 `/SD`（無介面時的預設答案）
- `Fetch-Code` 的**兩條分支**都要先停服務

註解會騙人，所以**去註解之後再比對整行的指令**（本專案在 NSIS 註解上被騙過，
`tools/nsis_source.py` 就是為此收的）。
"""
from __future__ import annotations

import pathlib
import re
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tools.nsis_source import code_text  # noqa: E402
from tools.repo_paths import public_root  # noqa: E402

PKG = public_root(ROOT) / "packaging" / "windows"
NSI = PKG / "installer.nsi"
CORE = PKG / "install_core.ps1"


@pytest.fixture(scope="module")
def nsi() -> str:
    assert NSI.exists(), f"找不到 {NSI}"
    return code_text(NSI.read_text(encoding="utf-8", errors="ignore"))


def _statements(nsi: str) -> list[str]:
    """把 NSIS 的**續行**（行尾 `\\`）接起來再看。

    不接的話 `MessageBox MB_YESNO|MB_ICONQUESTION \\` 會被當成獨立一行，
    而它的 `/SD IDNO` 在下一行 —— 守門就會誤報一條其實正確的程式碼
    （我第一版就是這樣紅的）。**解析器要認原始碼裡真正的寫法。**
    """
    out, cur = [], ""
    for line in nsi.splitlines():
        s = line.strip()
        if s.endswith("\\"):
            cur += s[:-1] + " "
            continue
        out.append((cur + s).strip())
        cur = ""
    if cur:
        out.append(cur.strip())
    return out


def test_every_messagebox_has_a_silent_default(nsi):
    """少了 `/SD` 的對話框在無介面模式下會**永遠等下去**。"""
    bad = []
    for s in _statements(nsi):
        if not s.startswith("MessageBox "):
            continue
        if "/SD " not in s:
            bad.append(s[:90])
    assert not bad, (
        "這些 MessageBox 沒有指定無介面模式的預設答案（`/SD`）：\n  "
        + "\n  ".join(bad)
        + "\n少了它，`/S` 安裝會停在一個沒有人看得到的對話框上 —— "
          "安裝程式掛著不結束也不報錯（v1.15.36 實機實測，卡了 30 分鐘）。")


def test_the_scan_sees_the_messageboxes_at_all(nsi):
    """守門自己要有牙齒：掃 0 個跟「都合格」在 pytest 輸出裡一模一樣。"""
    n = sum(1 for s in _statements(nsi) if s.startswith("MessageBox "))
    assert n >= 4, f"只掃到 {n} 個 MessageBox，比對基準本身就不對"


def test_the_installer_stops_the_service_on_every_path():
    """**升級既有安裝**走的是 git 那條分支 —— 它也必須先停服務。"""
    src = CORE.read_text(encoding="utf-8", errors="ignore")
    m = re.search(r"function Fetch-Code \{(.*?)\n\}", src, re.S)
    assert m, "install_core.ps1 裡找不到 Fetch-Code（改名了？）"
    body = m.group(1)
    head = body.split("if (Test-Path", 1)[0]
    assert "Stop-RunningService" in head, (
        "Fetch-Code 沒有在**進入點**停服務 —— 只在某一條分支停的話，"
        "升級既有安裝（git repo 那條）會讓 uv venv 撞到被佔住的 .venv")
    assert "Stop-Service" in src, "找不到真的停服務的呼叫"


def test_stopping_waits_for_the_process_to_let_go():
    """WinSW 停下來之後 `python.exe` 還要一點時間放掉檔案握把 ——
    沒有等待的話 uv 照樣會撞到「檔案使用中」。"""
    src = CORE.read_text(encoding="utf-8", errors="ignore")
    m = re.search(r"function Stop-RunningService \{(.*?)\n\}", src, re.S)
    assert m, "找不到 Stop-RunningService"
    body = m.group(1)
    assert "Start-Sleep" in body and re.search(r"for \(|while \(", body), (
        "停完服務之後沒有等它真的停下來")
