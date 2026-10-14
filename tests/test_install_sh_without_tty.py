"""install.sh 在沒有控制終端時不可以中止。

`ssh 主機 'curl … | sudo bash'`、排程、自動化工具跑一行安裝時沒有控制終端。
舊寫法用 `[ -r /dev/tty ]` 判斷要不要互動詢問：那只看權限位元（`/dev/tty` 永遠是 rw-rw-rw-），
於是照樣走進 `read </dev/tty`，打不開 → `set -e` 讓整個安裝停在「請選擇 [1/2/3]」那一行，
訊息只有 `/dev/tty: No such device or address`。

判準落在實際執行：把 `ask_bind_host` 抽出來，用 `setsid`（沒有控制終端）跑一次要退回預設值；
用 `script` 給一個終端機跑一次要照常讀到回答。
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.repo_paths import public_root as _public_root  # noqa: E402

PUB = _public_root(ROOT)

pytestmark = pytest.mark.skipif(sys.platform.startswith("win"), reason="要 bash")


def _script(answer_setup: str) -> str:
    src = (PUB / "install.sh").read_text(encoding="utf-8")
    fn = re.search(r"^ask_bind_host\(\) \{.*?^\}", src, re.M | re.S).group(0)
    return (
        "set -euo pipefail\n"
        "C_RST=; C_GRN=; C_CYN=\n"
        'warn() { printf "WARN %s\\n" "$*" >&2; }\n'
        f"{answer_setup}\n"
        'BIND_HOST_DEFAULT="127.0.0.1"\nNO_PROMPT=0\n'
        f"{fn}\n"
        'ask_bind_host\necho "HOST=$BIND_HOST"\n'
    )


@pytest.mark.skipif(not shutil.which("setsid"), reason="要 setsid")
def test_no_controlling_terminal_falls_back_to_the_default():
    out = subprocess.run(["setsid", "bash", "-c", _script('BIND_HOST=""')],
                         stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip().endswith("HOST=127.0.0.1"), out.stdout
    assert "JTDT_HOST=0.0.0.0" in out.stderr, "沒問到的時候要告訴使用者怎麼改"


@pytest.mark.skipif(not shutil.which("script"), reason="要 script（util-linux）")
def test_a_real_terminal_is_still_asked():
    """反向對照：有終端機時仍然要問，不可以為了不中止就乾脆永遠不問。"""
    body = _script('BIND_HOST=""').replace("'", "'\\''")
    out = subprocess.run(["script", "-qec", f"bash -c '{body}'", "/dev/null"],
                         input="2\n", capture_output=True, text=True, timeout=30)
    assert "HOST=0.0.0.0" in out.stdout.replace("\r", ""), out.stdout


def test_flag_or_env_skips_the_question():
    out = subprocess.run(["setsid", "bash", "-c", _script('BIND_HOST="0.0.0.0"')]
                         if shutil.which("setsid") else ["bash", "-c", _script('BIND_HOST="0.0.0.0"')],
                         stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
    assert out.returncode == 0 and out.stdout.strip() == "HOST=0.0.0.0", (out.stdout, out.stderr)
