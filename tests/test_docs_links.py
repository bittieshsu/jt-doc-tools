"""介紹網站與 API 手冊的連結不可以指向不存在的東西。

## 由來

使用者點介紹網站頁尾的「Changelog」得到 GitHub 404。原因是連結寫成
`blob/main/github/CHANGELOG.md` —— 多了 `github/` 這一層。

會寫錯是因為**開發樹與發佈樹的結構不同**：專案裡那份檔案在 `github/CHANGELOG.md`，
但 `github/` 這個資料夾**本身就是 repo 的根**，發佈之後路徑是 `CHANGELOG.md`。
照著本機路徑寫就會多一層。

壞掉的連結不會有任何錯誤訊息 —— 沒有例外、沒有日誌，只有點下去的人看得到 404，
所以它在網站上活了很久都沒被發現。這份就是把它變成會紅的測試。

## 這份檢查什麼

1. **`blob/main/<路徑>` 指向的檔案要真的存在**（比對 `github/` 這棵樹）。
   不連網，所以在離線環境與 CI 都跑得動。
2. **站內錨點要有對應的目標**。API 手冊是單頁式的，用 `data-sec` 當目標而不是
   `id`，兩種都算數。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PUB = ROOT / "github"          # 這個資料夾就是發佈到 GitHub 的 repo 根
DOCS = [PUB / "docs" / "index.html", PUB / "docs" / "api.html"]

_REPO = "https://github.com/jasoncheng7115/jt-doc-tools"


def _hrefs(text: str) -> list[str]:
    return re.findall(r'href="([^"]+)"', text)


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: p.name)
def test_repo_file_links_point_at_files_that_exist(doc: Path):
    """`blob/main/xxx` 的 xxx 要真的在發佈樹裡。

    這條就是那個 404 的守門員：`blob/main/github/CHANGELOG.md` 在發佈樹裡
    找不到 `github/CHANGELOG.md`（`github/` 是根，不是子目錄），會直接紅。
    """
    if not doc.exists():
        pytest.skip(f"{doc.name} 不存在")
    missing = []
    for href in _hrefs(doc.read_text(encoding="utf-8")):
        m = re.match(re.escape(_REPO) + r"/blob/main/([^\"#?]+)", href)
        if not m:
            continue
        rel = m.group(1)
        if not (PUB / rel).exists():
            missing.append(rel)
    assert not missing, (
        f"{doc.name} 連到發佈樹裡不存在的檔案：{sorted(set(missing))}。"
        "常見原因是照著本機路徑寫（多了 `github/` 這一層）——"
        "`github/` 資料夾本身就是 repo 的根。")


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: p.name)
def test_internal_anchors_have_targets(doc: Path):
    """`#foo` 要有對應的 `id="foo"` 或 `data-sec="foo"`。

    API 手冊是單頁式的：切換章節靠 JS 讀 hash 去比對 `data-sec`，不是真的錨點，
    所以兩種都算數。只認 `id` 的話會把正常的導覽全部誤判成壞連結。
    """
    if not doc.exists():
        pytest.skip(f"{doc.name} 不存在")
    s = doc.read_text(encoding="utf-8")
    targets = set(re.findall(r'\bid="([^"]+)"', s))
    targets |= set(re.findall(r'\bdata-sec="([^"]+)"', s))
    broken = []
    for href in _hrefs(s):
        if not href.startswith("#") or href == "#":
            continue           # `href="#"` 是給 JS 用的，不是導覽目標
        from urllib.parse import unquote
        if unquote(href[1:]) not in targets and href[1:] not in targets:
            broken.append(href)
    assert not broken, f"{doc.name} 的這些站內連結沒有對應目標：{sorted(set(broken))}"


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: p.name)
def test_no_relative_links_to_files_outside_the_published_site(doc: Path):
    """`docs/` 就是 Pages 的站台根 —— 相對連結只能指向 `docs/` 裡真的有的東西。

    API 手冊是從 `API.md` 生成的，而 `API.md` 裡寫 `[CHANGELOG.md](./CHANGELOG.md)`
    是**對的**（兩份檔案在 repo 根目錄並排）。但生成到 `docs/api.html` 之後，
    那個相對路徑會指到站台根，而 `CHANGELOG.md` 不在發佈的網站裡 → 404。
    生成器要把這種連結改寫成絕對的 GitHub 網址。
    """
    if not doc.exists():
        pytest.skip(f"{doc.name} 不存在")
    site = doc.parent
    bad = []
    for href in _hrefs(doc.read_text(encoding="utf-8")):
        if href.startswith(("http://", "https://", "#", "mailto:", "//")):
            continue
        target = href.split("#")[0].split("?")[0]
        if not target:
            continue
        if not (site / target).exists():
            bad.append(href)
    assert not bad, (
        f"{doc.name} 的相對連結指向站台裡沒有的東西：{sorted(set(bad))}。"
        "`docs/` 就是 Pages 的根，repo 根目錄的檔案要用絕對的 GitHub 網址。")


def test_readme_repo_links_exist():
    """README 裡指向 repo 檔案的連結也要對得上（同樣的錯法會發生在這裡）。"""
    readme = PUB / "README.md"
    if not readme.exists():
        pytest.skip("README 不存在")
    missing = []
    for m in re.finditer(re.escape(_REPO) + r"/blob/main/([^\s\)\"#?]+)",
                         readme.read_text(encoding="utf-8")):
        if not (PUB / m.group(1)).exists():
            missing.append(m.group(1))
    assert not missing, f"README 連到不存在的檔案：{sorted(set(missing))}"


# ------------------------------------------------- 導覽拿掉但錨點要留

#: 這幾段**刻意不放在導覽列**（項目太多反而找不到東西），但內容仍在頁面上，
#: 而且既有的直接連結 / 書籤 / 外部引用都指著這些 id —— **不可以因為
#: 「導覽沒用到」就把 id 拿掉**，那會讓所有既有連結變成連到頁首。
_ANCHORS_KEPT_WITHOUT_NAV = ["workspace", "jobs", "enterprise", "disclaimer"]


def test_sections_dropped_from_nav_keep_their_anchors():
    import pathlib
    html = (pathlib.Path(__file__).resolve().parent.parent
            / "github" / "docs" / "index.html").read_text(encoding="utf-8")
    missing = [a for a in _ANCHORS_KEPT_WITHOUT_NAV
               if f'id="{a}"' not in html]
    assert not missing, (
        f"這些區塊的 id 不見了：{missing}。它們沒放進導覽，但既有的 "
        f"#{missing[0] if missing else ''} 連結還指著它們。")
