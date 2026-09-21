"""soffice 逾時要殺掉**整棵行程樹**，不是只殺我們拿到的那個 PID。

## 踩到的（正式機 2026-09-18）

`markdown-to-doc` 轉一份 43 KB 的 Markdown，HTML → PDF 逾時 120 秒。
程式跑了 `proc.kill()`，旁邊的註解寫著
「force-kill so it doesn't leave a zombie soffice」——**但它沒有做到**：

```
PID=500501 PPID=1 狀態=R (running) 起於 01:22:51   ← 逾時是 01:24:53
```

因為 `/opt/oxoffice/program/soffice` 是 **shell 包裝腳本**，真正在解析檔案的
`soffice.bin` 是它 fork 出來的子行程。殺掉包裝腳本，子行程**變成孤兒繼續空轉**。

**後果會累積**：soffice 被我們刻意降優先權（讓網頁保持回應），
所以每一次逾時都留下一支在背景搶 CPU 的行程，**讓下一次更容易逾時**。

> 同一份檔案在閒置的開發機上 **6 秒**就轉完 —— 這件事跟檔案無關。
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap
import time

import pytest

from app.core import office_convert as oc

pytestmark = pytest.mark.skipif(sys.platform.startswith("win"),
                                reason="行程群組的作法在 Windows 不同（走 taskkill /T）")


def _spawn_wrapper(tmp_path):
    """做一支**會 fork 子行程**的假 soffice —— 那正是真的 soffice 的形狀。

    父行程印出孫子的 PID 之後就等著；孫子是真正「在算」的那一個。
    """
    child = tmp_path / "child.py"
    child.write_text(textwrap.dedent("""
        import time
        while True:
            time.sleep(0.2)
    """), encoding="utf-8")

    wrapper = tmp_path / "fake_soffice.sh"
    wrapper.write_text(textwrap.dedent(f"""\
        #!/bin/sh
        "{sys.executable}" "{child}" &
        echo $! > "{tmp_path}/child.pid"
        wait
    """), encoding="utf-8")
    wrapper.chmod(0o755)

    _, kwargs = oc._build_soffice_cmd(str(wrapper), [])
    proc = subprocess.Popen([str(wrapper)], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, **kwargs)
    for _ in range(100):
        f = tmp_path / "child.pid"
        if f.exists() and f.read_text().strip():
            break
        time.sleep(0.05)
    return proc, int((tmp_path / "child.pid").read_text().strip())


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def test_the_grandchild_survives_a_plain_kill():
    """**先證明這個 bug 是真的。** 只殺我們拿到的 PID，子行程會活下來。

    沒有這一條的話，下面那條測試證明不了任何事 —— 說不定 shell 本來就會
    把子行程一起帶走。
    """
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        proc, grandchild = _spawn_wrapper(tmp)
        proc.kill()
        try:
            proc.communicate(timeout=5)
        except Exception:                                # noqa: BLE001
            pass
        time.sleep(0.5)
        try:
            assert _alive(grandchild), "舊做法應該留下孤兒 —— 留不下就代表這個測試沒抓到重點"
        finally:
            try:
                os.kill(grandchild, signal.SIGKILL)
            except Exception:                            # noqa: BLE001
                pass


def test_kill_tree_takes_the_grandchild_with_it(tmp_path):
    """**修法要驗的就是這一條。**"""
    proc, grandchild = _spawn_wrapper(tmp_path)
    assert _alive(grandchild)
    oc._kill_tree(proc)
    for _ in range(50):
        if not _alive(grandchild):
            break
        time.sleep(0.1)
    assert not _alive(grandchild), "整棵行程樹都要死掉，不可以留下孤兒繼續燒 CPU"


def test_kill_tree_never_raises(tmp_path):
    """**這是錯誤處理路徑** —— 在這裡再炸一次，使用者看到的會是堆疊
    而不是「轉檔逾時」。"""
    proc, grandchild = _spawn_wrapper(tmp_path)
    oc._kill_tree(proc)
    oc._kill_tree(proc)          # 第二次：行程已經不在了
    class Broken:
        pid = -12345
        def kill(self): raise OSError("boom")
        def communicate(self, timeout=None): raise OSError("boom")
    oc._kill_tree(Broken())      # 完全壞掉的物件也不可以丟例外
    try:
        os.kill(grandchild, signal.SIGKILL)
    except Exception:                                    # noqa: BLE001
        pass


def test_soffice_is_spawned_in_its_own_session():
    """判準放在**真的有開新 session** 上 —— 沒有它 `killpg` 會殺到我們自己。"""
    _, kwargs = oc._build_soffice_cmd("/usr/bin/soffice", ["--headless"])
    assert kwargs.get("start_new_session") is True


def test_every_timeout_path_uses_the_tree_kill():
    """**七支轉檔函式都要用同一個收尾** —— 漏掉一支，那條路就繼續留孤兒。"""
    import ast
    import inspect
    src = inspect.getsource(oc)
    tree = ast.parse(src)
    bad = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
        attrs = {a.attr for a in ast.walk(node) if isinstance(a, ast.Attribute)}
        if "TimeoutExpired" not in attrs and "TimeoutExpired" not in names:
            continue
        called = {a.func.id for a in ast.walk(node)
                  if isinstance(a, ast.Call) and isinstance(a.func, ast.Name)}
        if "_kill_tree" not in called:
            bad.append(getattr(node, "lineno", "?"))
    assert not bad, f"這幾行的逾時收尾沒有走 _kill_tree：{bad}"
