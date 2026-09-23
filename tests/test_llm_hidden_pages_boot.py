"""LLM 停用 ＋「停用時一併隱藏」時，每一支有 LLM 功能的頁面在**真瀏覽器**裡開一次。

為什麼要真的開：藏起來的是各工具 JS 會去 `getElementById` 的元素。只要有一支
把整段不輸出、而它的 JS 沒處理 null，那一頁的行內腳本就停在那一行 ——
**畫面看起來完全正常，只是按鈕按了沒反應**（本專案踩過好幾次）。
靜態檢查看不到這一類，只有跑一次 JS 才看得到。

預設狀態（停用、沒勾隱藏＝反灰）由 `test_pages_boot_in_a_browser.py` 涵蓋 ——
那支的拋棄式實例本來就是 LLM 停用。
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

import pytest

from tests import test_pages_boot_in_a_browser as boot

ROOT = boot.ROOT

pytestmark = boot.pytestmark


def _llm_paths() -> list[str]:
    sys.path.insert(0, str(ROOT))
    from app.core.llm_settings import LLMSettingsManager
    from app.tool_registry import discover_tools
    registered = {t.metadata.id for t in discover_tools()}
    ids = sorted(k["id"] for k in LLMSettingsManager.KNOWN_LLM_TOOLS if k["id"] in registered)
    return ["/", "/admin/llm-settings"] + [f"/tools/{i}/" for i in ids]


PATHS = _llm_paths()


@pytest.fixture(scope="module")
def live_hidden():
    data = tempfile.mkdtemp(prefix="llmhide-")
    boot._seed_setup_gated_tools(pathlib.Path(data))
    (pathlib.Path(data) / "llm_settings.json").write_text(json.dumps(
        {"enabled": False, "hide_when_disabled": True}), encoding="utf-8")
    port, cdp = boot._free_port(), boot._free_port()
    env = {**os.environ, "JTDT_DATA_DIR": data, "JTDT_CSRF_DISABLE": "1"}
    srv = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    br = subprocess.Popen(
        [boot._browser(), "--headless=new", "--no-sandbox", "--disable-gpu",
         f"--remote-debugging-port={cdp}", "--remote-allow-origins=*",
         "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(120):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1)
                urllib.request.urlopen(f"http://127.0.0.1:{cdp}/json/version", timeout=1)
                break
            except Exception:
                time.sleep(0.5)
        else:
            pytest.skip("實例或瀏覽器起不來")
        yield port, cdp
    finally:
        br.terminate(); srv.terminate()
        try:
            br.wait(timeout=5); srv.wait(timeout=5)
        except Exception:
            br.kill(); srv.kill()
        shutil.rmtree(data, ignore_errors=True)


def test_the_seed_really_hides_them(live_hidden):
    """**先證明種子有效** —— 不然下面那一輪掃的是反灰版，照樣全綠。"""
    port, _ = live_hidden
    html = urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=10).read().decode()
    assert 'data-tool-id="translate-doc"' not in html, "隱藏沒生效 —— 下面掃的不是隱藏版"
    page = urllib.request.urlopen(
        f"http://127.0.0.1:{port}/tools/pdf-wordcount/", timeout=10).read().decode()
    assert "llm-gate-augment" in page and " hidden" in page


@pytest.mark.parametrize("path", PATHS)
def test_page_boots_with_llm_hidden(live_hidden, path):
    port, cdp = live_hidden
    errs = boot._visit(cdp, f"http://127.0.0.1:{port}{path}")
    assert not errs, (
        f"{path}（LLM 停用＋一併隱藏）的主控台有錯誤：\n  " + "\n  ".join(errs)
        + "\n多半是藏掉的元素被 JS 拿去 getElementById —— 元素要留著、加 hidden。")


def test_the_sweep_covers_every_llm_tool():
    assert len(PATHS) >= 12, f"只列出 {PATHS}"
