"""新增 Python 相依時的六處宣告，一處都不能漏。

歷史教訓（v1.1.68 客戶慘案）：`uv.lock` 漏了 `ldap3`，而 `pyproject.toml` 有 ——
安裝時用 `uv sync --frozen` 盲信 lockfile，客戶啟用 AD 認證後直接鎖死無法登入。

2026-08-27 又補了第七處：**相依套件檢查頁**。這一輪加了 `dnspython`，部署到
正式機時才發現那台的 venv 不會自動裝新套件 —— 而管理員在相依頁面上**看不到
這個套件缺了**，因為清單裡根本沒有它。缺什麼要看得見，才知道要去裝。

判準刻意寫成「從 `pyproject.toml` 實算」而不是寫死清單 —— 寫死的清單自己就是
下一個會漂掉的東西。
"""
from __future__ import annotations

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent

#: 這些相依不需要出現在相依頁面：要嘛是別的套件的傳遞相依、要嘛缺了會直接
#: 開不了機（開不了機就不需要用頁面告訴你缺什麼）。列在這裡是**明確決定**，
#: 不是漏掉 —— 新增例外要在這裡寫清楚為什麼。
NOT_ON_DEPS_PAGE = {
    "fastapi", "uvicorn", "starlette", "pydantic", "pydantic-settings",
    "jinja2", "python-multipart", "httpx", "itsdangerous", "cryptography",
    "argon2-cffi", "bcrypt", "truststore", "pyzipper", "rapidfuzz",
    "markdown-it-py", "python-docx", "odfpy", "openpyxl", "pdfplumber",
    "pyotp", "qrcode", "psutil", "lxml", "numpy", "fonttools", "pillow",
    "pymupdf", "pymupdf4llm", "pdf2docx", "ldap3", "pyjwt", "python3-saml",
    "xmlsec", "easyocr", "pytesseract", "torch", "torchvision", "setuptools",
    # 這兩個是**別的套件的傳遞相依**，我們只是為了修 CVE 顯式釘了版本下限
    # （pyasn1：DoS；idna：CVE-2026-45409）。程式沒有直接用它們，缺了也
    # 輪不到相依頁面提醒 —— 依賴它們的套件自己就裝不起來了。
    "pyasn1", "idna",
}


def _declared_deps() -> set[str]:
    """從 pyproject.toml 讀出宣告的相依名稱（不含版本限制）。"""
    # **不要用 `\[(.*?)\]` 抓整段** —— `"uvicorn[standard]>=..."` 裡的 `]`
    # 會讓比對提早結束，只抓到前兩個相依，而測試照樣全綠（2026-08-27 變異
    # 驗證時發現這個假通過）。逐行讀到獨立的 `]` 才對。
    lines = (ROOT / "pyproject.toml").read_text(encoding="utf-8").split("\n")
    try:
        start = next(i for i, ln in enumerate(lines)
                     if re.match(r"^dependencies\s*=\s*\[", ln))
    except StopIteration:
        pytest.fail("pyproject.toml 讀不到 dependencies")
    out = set()
    for line in lines[start + 1:]:
        if line.rstrip() == "]":
            break
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        mm = re.match(r'"([A-Za-z0-9_.\-]+)', s)
        if mm:
            out.add(mm.group(1).lower())
    assert len(out) > 10, f"只解析到 {len(out)} 個相依 —— 解析壞了，這份檢查等於沒跑"
    return out


def test_every_declared_dep_is_in_requirements_txt():
    """沒有 uv 的環境走 requirements.txt —— 漏了那邊就裝不到。"""
    req = (ROOT / "requirements.txt").read_text(encoding="utf-8").lower()
    missing = sorted(d for d in _declared_deps() if d not in req)
    assert not missing, f"這些相依沒寫進 requirements.txt：{missing}"


def test_every_declared_dep_is_locked():
    """`uv.lock` 漏了會讓 `uv sync` 裝不到 —— v1.1.68 客戶因此鎖死。"""
    lock = (ROOT / "uv.lock").read_text(encoding="utf-8").lower()
    missing = sorted(d for d in _declared_deps() if f'name = "{d}"' not in lock)
    assert not missing, (
        f"這些相依不在 uv.lock：{missing}\n"
        "  加完相依要重跑 `uv lock`（UV_CACHE_DIR=/tmp/uv-cache-jtdt uv lock）")


def test_new_optional_deps_appear_on_the_dependency_page():
    """**缺什麼要看得見。**

    相依頁面是管理員唯一能看到「這台機器缺了什麼」的地方。新增的選用相依沒
    列進去的話，缺了就只能從錯誤訊息猜（2026-08-27 部署 dnspython 時實際
    踩到）。核心相依缺了會直接開不了機，不需要頁面告訴你，列在
    `NOT_ON_DEPS_PAGE` 例外裡。
    """
    from app.core.sys_deps import collect_sys_deps
    keys = {str(r.get("key") or "").lower() for r in collect_sys_deps()}
    missing = sorted(d for d in _declared_deps()
                     if d not in NOT_ON_DEPS_PAGE and d not in keys)
    assert not missing, (
        f"這些相依沒出現在相依套件檢查頁：{missing}\n"
        "  要嘛加進 `app/core/sys_deps.py` 的清單，要嘛在這支測試的 "
        "`NOT_ON_DEPS_PAGE` 裡寫清楚為什麼不需要。")


@pytest.mark.parametrize("path,label", [
    ("github/install.sh", "Linux / macOS 安裝"),
    ("github/setup-python.cmd", "Windows 安裝"),
    ("app/cli.py", "jtdt update"),
])
def test_smoke_import_lists_stay_in_sync(path, label):
    """三處的 import 煙霧測試要涵蓋同一組套件。

    它們是「裝完之後真的載得起來嗎」的最後一道 —— 三份清單各寫一份，
    一定會漂（這個專案的老毛病）。這裡只驗它們彼此一致，不驗內容。
    """
    def _mods(text: str, where: str) -> set:
        m = re.search(r'import fastapi[^"\n]*', text)
        assert m, f"{where} 找不到 import 煙霧測試那一行"
        # `.cmd` 那行結尾接著 `; print('OK')`，切在分號前才不會把它算成套件
        body = m.group(0).replace("import ", "").split(";")[0]
        return {x.strip() for x in body.split(",") if x.strip()}

    mods = _mods((ROOT / path).read_text(encoding="utf-8"), f"{label}（{path}）")
    ref = _mods((ROOT / "github/install.sh").read_text(encoding="utf-8"),
                "install.sh")
    assert mods == ref, (
        f"{label} 的 import 煙霧清單跟 install.sh 不一致：\n"
        f"  只有它有：{sorted(mods - ref)}\n"
        f"  只有 install.sh 有：{sorted(ref - mods)}")
