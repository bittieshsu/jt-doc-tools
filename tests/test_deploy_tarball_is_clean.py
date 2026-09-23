"""部署 tarball 不可以夾帶客戶資料或內部往來文件。

**為什麼要有這條**：往外送的路徑有三條 —— 公開樹同步（已經有檢查）、
部署 tarball、臨時手打的指令。第二條是每一版都會走的，原本完全沒有檢查，
靠的是「記得下對 `--exclude`」。

2026-09-18 真的踩到：照 CLAUDE.md 的鏡像流程跑 `rsync ./`（那段是 macOS
時代寫的，當時來源只有 `app/`），把 `docs-share/` 的內部往來文件與
`temp_pdfs/` 的客戶樣本複製進 `/tmp`。

**判準落在產出的 tarball 內容上**，不是落在 `--exclude` 參數的字面 ——
參數對不代表沒有別的路徑漏進來。
"""
from __future__ import annotations

import importlib.util
import sys
import tarfile
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load():
    path = ROOT / "tools" / "deploy_tarball.py"
    assert path.exists(), "tools/deploy_tarball.py 不見了"
    spec = importlib.util.spec_from_file_location("_deploy_tarball", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_deploy_tarball"] = mod
    spec.loader.exec_module(mod)
    return mod


DT = _load()


def test_the_deny_list_covers_the_directories_that_actually_hold_private_data():
    """清單不是隨手寫的 —— 每一條在這個專案裡都真的存在過機敏內容。"""
    for prefix in ("docs-share/", "temp_pdfs/", "data/", "temp/"):
        assert prefix in DT.PRIVATE_PREFIXES, f"{prefix} 必須列在封鎖清單裡"
        assert DT.PRIVATE_PREFIXES[prefix].strip(), f"{prefix} 要寫出理由"


@pytest.mark.parametrize("name", [
    "docs-share/partner-notes/DECISIONS.md",
    "temp_pdfs/customer/某某公司.pdf",
    "data/auth.sqlite",
    "temp/zap/report.json",
    "releases/jt-doc-tools-1.15.54-setup.exe",
    "./docs-share/anything.md",
    "app/core/secret.pem",
])
def test_private_paths_are_reported(name):
    assert DT.offending([name]), f"{name} 應該被判成不可外送"


@pytest.mark.parametrize("name", [
    "app/main.py",
    "static/css/platform.css",
    "tests/test_meeting_insight.py",
    "tools/deploy_tarball.py",
    "pyproject.toml",
    # 這幾個名字裡含有封鎖字但不是那個目錄 —— 不可以誤報
    "app/core/data_export.py",
    "app/tools/pdf_fill/templates_data.py",
    "static/js/temp_preview.js",
])
def test_ordinary_paths_are_not_reported(name):
    assert not DT.offending([name]), f"{name} 被誤判成機敏路徑"


def test_the_real_tarball_builds_and_is_clean():
    """真的建一次，驗產出 —— 不是驗參數字面。"""
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "deploy.tgz"
        DT.build(ROOT, out)
        assert out.exists() and out.stat().st_size > 0
        with tarfile.open(out, "r:gz") as tf:
            names = tf.getnames()
        # 先證明它真的裝到東西了（不然「0 筆機敏」是因為 tarball 是空的）
        assert len(names) > 500, f"tarball 只有 {len(names)} 個項目，看起來沒裝到東西"
        assert any(n.startswith("app/") for n in names), "tarball 裡沒有 app/"
        assert not DT.offending(names)


def test_verify_refuses_a_tarball_that_carries_private_content():
    """反向對照：只驗「乾淨的過得了」的話，把檢查整段拿掉也會全綠。"""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        planted = tmp_path / "docs-share" / "partner-notes" / "secret.md"
        planted.parent.mkdir(parents=True)
        planted.write_text("內部往來", encoding="utf-8")
        out = tmp_path / "dirty.tgz"
        with tarfile.open(out, "w:gz") as tf:
            tf.add(planted, arcname="docs-share/partner-notes/secret.md")
        with pytest.raises(SystemExit) as exc:
            DT.verify(out)
        assert exc.value.code == 1
        # 而且要把那個檔刪掉 —— 留著的話下一個人可能照樣 scp 出去
        assert not out.exists(), "驗不過的 tarball 必須刪除，不可以留在磁碟上"


def test_the_ship_list_is_explicit_not_an_exclude_list():
    """用允許清單，不用排除清單 —— 排除清單的預設是「送出去」。"""
    src = (ROOT / "tools" / "deploy_tarball.py").read_text(encoding="utf-8")
    assert "SHIP = (" in src
    for item in ("app", "static", "tools"):
        assert item in DT.SHIP
    for item in ("docs-share", "temp_pdfs", "data", "releases", "temp", "github"):
        assert item not in DT.SHIP, f"{item} 不可以列進交付清單"
