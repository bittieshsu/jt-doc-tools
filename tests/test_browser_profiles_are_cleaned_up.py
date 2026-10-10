"""無頭瀏覽器每次啟動都要給**自己的**設定檔目錄，用完要刪。

## 由來（2026-10-10）

同一台開發機上的另一個專案查磁碟時發現 `~/snap/chromium/common/chromium-headless/`
累積到 55 GB：幾千個 `scoped_dir*`，全是我們的瀏覽器測試留下的。

沒給 `--user-data-dir` 時，無頭 Chromium 自己建一個暫存設定檔，**只有正常結束才刪**；
我們的測試一律用 `terminate()` / `kill()` 收尾，實測兩種都留下來（一個約 11 MB，
一天跑幾輪測試就是三百個左右）。

所以：每一處啟動瀏覽器都要帶 `profile_arg()`（或自己給 `--user-data-dir` 並自己刪），
`tools/browser_probe.py` 在行程結束時刪掉自己建的目錄，並在下一次建立時清掉
**建立者已經不在**的舊目錄。snap 自己的 `chromium-headless` 一律不碰。
"""
from __future__ import annotations

import ast
import os
import pathlib
import socket
import subprocess
import sys
import time
import urllib.request

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools import browser_probe as bp  # noqa: E402

_LAUNCH_FLAGS = ("--remote-debugging-port", "--dump-dom", "--screenshot", "--print-to-pdf")


def _str_head(node) -> str:
    """參數清單裡一個元素的字面開頭（f-string 取第一段）。"""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr) and node.values:
        first = node.values[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            return first.value
    return ""


def _gives_profile(node) -> bool:
    if _str_head(node).startswith("--user-data-dir"):
        return True
    if isinstance(node, ast.Call):
        f = node.func
        name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
        return name.endswith("profile_arg")
    return False


def _launch_lists():
    """所有「啟動瀏覽器」的參數清單：tests / tools / scripts 裡含除錯埠或一次性輸出旗標的 list。"""
    out = []
    for d in ("tests", "tools", "scripts"):
        for p in sorted((ROOT / d).glob("*.py")):
            if p.name == pathlib.Path(__file__).name:
                continue
            try:
                tree = ast.parse(p.read_text(encoding="utf-8"))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.List):
                    continue
                heads = [_str_head(e) for e in node.elts]
                if not any(h.startswith(_LAUNCH_FLAGS) for h in heads):
                    continue
                # soffice 也有 `--headless`，但沒有這幾個旗標；保險起見排除 soffice 的參數清單
                if any(h.startswith("--convert-to") for h in heads):
                    continue
                out.append((p.relative_to(ROOT).as_posix(), node.lineno, node))
    return out


def test_the_scan_finds_the_browser_launches():
    """**「掃 0 處」跟「全部合格」在輸出裡長得一樣**。實際約 38 處，取一半當下限。"""
    assert len(_launch_lists()) >= 19, len(_launch_lists())


def test_every_browser_launch_gives_its_own_profile_directory():
    bad = [f"{f}:{ln}" for f, ln, node in _launch_lists()
           if not any(_gives_profile(e) for e in node.elts)]
    assert not bad, (
        "這幾處啟動瀏覽器沒有給設定檔目錄 —— Chromium 會自己建一個暫存的，"
        "被 terminate / kill 時留在 ~/snap/chromium/common/chromium-headless，"
        "一次約 11 MB、永遠不刪。參數清單裡加 `browser_probe.profile_arg()`：\n  "
        + "\n  ".join(bad))


# ---- browser_probe 的收尾規則 ----

@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setattr(bp, "profile_root", lambda: str(tmp_path))
    monkeypatch.setattr(bp, "_ATEXIT", [False])
    monkeypatch.setattr(bp, "_MINE", [])
    registered = []
    monkeypatch.setattr(bp.atexit, "register", registered.append)
    from types import SimpleNamespace
    return SimpleNamespace(dir=tmp_path, registered=registered)


def _dead_pid() -> int:
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def test_each_launch_gets_a_new_directory_named_after_this_process(root):
    a, b = bp.profile_dir(), bp.profile_dir()
    assert a != b
    for d in (a, b):
        assert pathlib.Path(d).parent == root.dir and pathlib.Path(d).is_dir()
        assert pathlib.Path(d).name.split("-", 1)[0] == str(os.getpid())
    assert bp.profile_arg().startswith("--user-data-dir=" + str(root.dir))


def test_the_directories_are_removed_when_the_process_ends(root):
    a = bp.profile_dir()
    (pathlib.Path(a) / "Default").mkdir()
    (pathlib.Path(a) / "Default" / "Cookies").write_bytes(b"x" * 100)
    assert root.registered == [bp.cleanup_profiles], "沒有登記在行程結束時刪掉"
    root.registered[0]()
    assert not pathlib.Path(a).exists()


def test_leftovers_of_a_finished_process_are_swept_but_not_anyone_elses(root):
    dead = root.dir / f"{_dead_pid()}-old"
    alive_other = root.dir / f"{os.getppid()}-busy"       # 還活著的別的行程
    foreign = root.dir / "not-ours"                         # 不是照我們的規則命名的
    for d in (dead, alive_other, foreign):
        d.mkdir()
    bp.profile_dir()                                    # 第一次建立時順便清
    assert not dead.exists(), "建立者已經不在的舊目錄沒有清掉"
    assert alive_other.exists(), "別的行程還在用的目錄被刪了"
    assert foreign.exists(), "不是我們命名的目錄被刪了"


def test_liveness_never_signals_the_process():
    """`os.kill(pid, 0)` 在 Windows 上會**結束那個行程**，不可以拿來判斷活著沒。"""
    import inspect
    src = inspect.getsource(bp._pid_alive)
    assert "os.kill" not in src.replace("`os.kill(pid, 0)`", "")


# ---- 真的瀏覽器 ----

def _snap_headless_dir() -> pathlib.Path:
    return pathlib.Path(os.path.expanduser("~/snap/chromium/common/chromium-headless"))


def test_a_killed_browser_leaves_nothing_behind():
    """判準落在磁碟上：啟動 → kill → snap 的暫存設定檔目錄裡**沒有多出東西**，
    我們自己的目錄在收尾後也不在了。"""
    exe = bp.browser()
    if not exe or not bp.is_snap(exe) or not bp.browser_runs(exe):
        pytest.skip("這台沒有跑得起來的 snap 版 Chromium（暫存設定檔的位置只有 snap 版確定）")
    watch = _snap_headless_dir()
    before = set(os.listdir(watch)) if watch.is_dir() else set()
    s = socket.socket(); s.bind(("127.0.0.1", 0)); cdp = s.getsockname()[1]; s.close()
    arg = bp.profile_arg()
    prof = arg.split("=", 1)[1]
    br = subprocess.Popen([exe, "--headless=new", "--no-sandbox", "--disable-gpu", arg,
                           f"--remote-debugging-port={cdp}", "about:blank"],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(60):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{cdp}/json/version", timeout=1)
                break
            except Exception:  # noqa: BLE001
                time.sleep(0.5)
        else:
            pytest.skip("瀏覽器的除錯埠 30 秒內沒有開")
        assert any(pathlib.Path(prof).iterdir()), "瀏覽器沒有用我們給的設定檔目錄"
    finally:
        br.kill()
        br.wait(timeout=20)
    time.sleep(1)
    after = set(os.listdir(watch)) if watch.is_dir() else set()
    assert not (after - before), f"kill 之後 snap 的暫存設定檔目錄多了 {sorted(after - before)}"
    # 只刪這一次的（同一個 pytest 行程裡可能還有別的瀏覽器在用它自己的目錄）
    import shutil
    shutil.rmtree(prof, ignore_errors=True)
    bp._MINE.remove(prof)
    assert not pathlib.Path(prof).exists()
