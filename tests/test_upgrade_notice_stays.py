"""改寫歷史之後的升級注意事項**要一直留著**（使用者 2026-09-13 指示）。

## 為什麼要有這條檢查

2026-09-13 改寫過 git 歷史（移除一筆誤入版控的資料），所有標籤都指向新的
commit。**2026-09-13 之前用 git 安裝的機器，第一次升級前要手動跑一次**：

    git -C <安裝目錄> fetch --tags --force origin

不跑的話，舊版的 `jtdt update` 會在 `git fetch --tags` 以離開碼 1 失敗
（`would clobber existing tag`），訊息只說 `git fetch failed`。

**這段說明不可以在下一次改版時被順手拿掉** —— 客戶可能半年後才升級，
那時候他仍然會踩到。使用者原話：「這個要持續放著，不要下次更版就拿掉」。

判準是**那行指令真的還在**（不是找標題或年份 —— 那些會被改寫）：
README 的**開頭**要有，介紹站的升級段也要有。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.repo_paths import public_root  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
PUB = public_root(ROOT)

#: 客戶要跑的那一行 —— 判準就是它
_CMD = "fetch --tags --force origin"


def test_the_readme_carries_the_recovery_command_near_the_top():
    text = (PUB / "README.md").read_text(encoding="utf-8")
    assert _CMD in text, (
        "README 少了改寫歷史後的升級指令 —— 2026-09-13 之前安裝的客戶"
        "會在 `jtdt update` 卡住而且看不出原因（使用者指示這段要持續放著）")
    # **要在開頭**：擺到最下面等於沒有人看得到
    head = "\n".join(text.splitlines()[:40])
    assert _CMD in head, "那段說明被移到很後面了 —— 要放在標題正下方"


def test_the_intro_site_upgrade_section_carries_it_too():
    html = (PUB / "docs" / "index.html").read_text(encoding="utf-8")
    assert _CMD in html, "介紹站的升級段少了那行指令"
    i = html.index(_CMD)
    around = html[max(0, i - 1500):i]
    assert "升級" in around, "那行指令沒有放在升級那一段裡"


def test_ops_guide_explains_it_as_well():
    text = (PUB / "OPS.md").read_text(encoding="utf-8")
    assert _CMD in text, "OPS.md 的升級流程少了那行指令"


def test_the_notice_warns_that_the_flag_is_uppercase():
    """**`-C` 是大寫**這件事要寫在說明旁邊。

    2026-09-14 客戶把它打成小寫的 `git -c /opt/jt-doc-tools/ …`，得到
    `fatal: not a git repository (or any of the parent directories): .git`
    —— 小寫的 `-c` 是**設定參數**，git 不會切到那個目錄，於是在目前的位置
    找 `.git`。**那個訊息看起來像「這個安裝不是用 git 裝的」，其實只是打錯
    一個字母**，客戶因此往完全錯誤的方向查。

    這條跟「照著做的步驟要用字面檢查釘住」（`OPS.md` 的 IIS 安裝順序）是
    同一條：文件裡叫人跑的指令，**最容易打錯的那個地方要先講**。
    """
    root = PUB
    checks = {
        "README.md": ("`-C` 是大寫", "not a git repository"),
        "OPS.md": ("`-C` 是大寫", "not a git repository"),
        "docs/index.html": ("大寫的 <code>-C</code>", "not a git repository"),
    }
    for rel, needles in checks.items():
        t = (root / rel).read_text(encoding="utf-8")
        for n in needles:
            assert n in t, f"{rel} 少了「{n}」這句提醒"


def test_no_copy_paste_block_uses_the_lowercase_flag():
    """**可以複製貼上的那幾塊**裡不可以出現 `git -c <路徑>`。

    `-c` 後面接的是 `key=value`，接路徑一定是打錯。

    **只掃「會被複製走」的地方**（Markdown 的 ``` 區塊、HTML 的 `<pre>`）——
    說明文字裡一定會**引用**那個錯誤寫法當反例（就在這一份文件上面），
    整份掃的話會把解釋規則的那句話自己判成違規。本專案在 use vs mention 上
    踩過很多次，這條是同一件事。
    """
    import re

    fenced = re.compile(r"```.*?```", re.S)
    pre = re.compile(r"<pre\b.*?</pre\b[^>]*>", re.S | re.I)
    wrong = re.compile(r"git -c\s+[/\"'A-Za-z]")
    bad = []
    for f in list(PUB.rglob("*.md")) + list(PUB.rglob("*.html")):
        if "/i18n/" in f.as_posix():
            continue
        text = f.read_text(encoding="utf-8")
        for block in [m.group(0) for m in fenced.finditer(text)] + \
                     [m.group(0) for m in pre.finditer(text)]:
            for m in wrong.finditer(block):
                tail = block[m.start():m.start() + 40]
                if "=" in tail.split()[2] if len(tail.split()) > 2 else False:
                    continue          # `git -c safe.directory=…` 是真的設定
                bad.append(f"{f.relative_to(PUB).as_posix()}: {m.group(0)}")
    assert not bad, "可複製的指令區塊裡出現小寫的 `git -c <路徑>`：" + str(bad)


def test_the_scan_actually_reaches_the_recovery_command():
    """**先證明掃得到東西** —— 掃 0 個區塊跟全部合格在 pytest 輸出裡一樣。"""
    import re

    fenced = re.compile(r"```.*?```", re.S)
    seen = 0
    for f in PUB.rglob("*.md"):
        for m in fenced.finditer(f.read_text(encoding="utf-8")):
            if _CMD in m.group(0):
                seen += 1
    assert seen >= 2, f"只在 {seen} 個指令區塊裡看到那一行 —— 掃描器大概壞了"
