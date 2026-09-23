"""`jtdt update` 的 fetch 不可以被「移動過的標籤」擋死。

## 由來（2026-09-13，我自己造成的）

為了把一筆誤入版控的個人資料從歷史裡移除，改寫了歷史並 force-push。
所有標籤都指向新的 commit，而既有安裝的本地標籤還指著舊的 —— 於是：

    $ git fetch --tags origin
     ! [rejected]  v1.15.26 -> v1.15.26  (would clobber existing tag)
    離開碼 1

`svc_update` 一看到非零就**中止升級並還原**，訊息只說 `git fetch failed`。
也就是**每一台用 git 安裝的機器都再也更新不了**，而且看不出原因是標籤。
（實測：不帶 `--tags` 的 fetch 回 0，帶 `--force` 的也回 0。）

## 判準

* 拉標籤時**一定要帶 `--force`** —— 標籤本來就會動（重新打過的 release、
  上游改寫歷史）。
* **分支是必要的、標籤是附加的**：分支拉不到才算失敗；標籤拉不到只警告。
  升級真正需要的只有 `origin/main`。
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "app" / "cli.py"


def _fetch_calls() -> list[list[str]]:
    """從 AST 撈出所有 `git ... fetch ...` 的引數清單（字面值部分）。"""
    tree = ast.parse(CLI.read_text(encoding="utf-8"))
    out: list[list[str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        first = node.args[0]
        if not isinstance(first, ast.List):
            continue
        lits = [e.value for e in first.elts
                if isinstance(e, ast.Constant) and isinstance(e.value, str)]
        if "fetch" in lits:
            out.append(lits)
    return out


def test_tags_are_fetched_with_force():
    calls = _fetch_calls()
    assert calls, "找不到任何 git fetch 呼叫 —— 這條檢查的判準失效了"
    tagged = [c for c in calls if "--tags" in c]
    assert tagged, "沒有任何一個 fetch 帶 --tags"
    for c in tagged:
        assert "--force" in c, (
            "拉標籤沒帶 --force —— 上游改寫過歷史時 git 會回 "
            "`would clobber existing tag` 並以離開碼 1 結束，"
            "於是每一台既有安裝都更新不了")


def test_a_failed_tag_fetch_does_not_abort_the_upgrade():
    """標籤拉不到只能警告。判準：**分支與標籤要分成兩次 fetch**。

    合在一起的話，標籤的失敗就等於整個 fetch 的失敗 —— 無法只警告。
    """
    calls = _fetch_calls()
    plain = [c for c in calls if "--tags" not in c]
    assert plain, (
        "只有一個帶 --tags 的 fetch —— 分支與標籤要分開拉，"
        "否則標籤出問題時會把升級一起擋掉")
    src = CLI.read_text(encoding="utf-8")
    i = src.index('"--tags", "--force"')
    after = src[i:i + 500]
    assert "svc_start()" not in after.split("return")[0], (
        "標籤 fetch 失敗之後看起來仍然走了還原流程 —— 它應該只印警告")
