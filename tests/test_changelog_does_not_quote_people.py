"""公開的更新記錄裡不可以引述使用者 / 客戶說的話。

2026-09-15 使用者看到 v1.15.47 那一節寫著

    使用者：「截圖 要放文件進去啊」「很多日文截圖都沒開範例檔」

指出**這種東西不該放進公開檔案** —— 這次的措辭還算中性，但同一個寫法
下一次可能就把一句抱怨、或一句看得出是誰的話公開出去。

## 判準

擋的是「**把某人說的話原樣括起來**」：`使用者回報「…」`、`客戶問：「…」`
這種形狀。改成用自己的話描述症狀就好 ——
**症狀本身要留著**（別人靠那句話搜尋到自己遇到的問題），要拿掉的是引號與
「某某人說」這一層框。

`「」` 本身完全正常，不要一律擋：UI 標籤（「我的作業」）、錯誤訊息
（「CSRF token 遺失或不正確」）、工具名稱、強調用語都會用到它。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.repo_paths import public_root  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]

#: 說話動詞。三個收窄的地方，每一個都是實際踩到的誤報：
#:
#: * **「指出」不收** —— 它後面接的多半是一個**詞**而不是一句話
#:   （`使用者指出「在線」是大陸用法`）。
#: * **`說` 不可以是「說明」的一半** —— `使用者指出安裝說明的「方式一…」`
#:   會從那個「說」開始配對，整句被判成引述。
#: * **引號裡至少要四個字** —— 引述的是句子；一兩個字的多半是術語或
#:   那個字本身（`使用者回報「中」字上方多一截空白`）。
#:
#: 誤報一多這份檢查就會被當雜訊忽略（用詞檢查那次的教訓），所以寧可窄一點。
_SAID = r"(?:說(?!明)|問|回報|表示|反映|交代|指示)"

_QUOTES_A_PERSON = re.compile(
    r"(?:客戶|使用者|user|User)[^。\n]{0,6}" + _SAID + r"[^。\n]{0,3}「[^」\n]{4,}」")

#: 冒號式的引述：`客戶回報：「…」`（引號可能跨行，所以不要求收尾）
_QUOTES_WITH_COLON = re.compile(
    r"(?:客戶|使用者)[^。\n]{0,6}" + _SAID + r"[^。\n]{0,3}[：:]\s*「[^」\n]{4,}")

#: **連動詞都省掉的那種**：`使用者：「…」` —— 正是使用者看到的那一行。
#: 引號必須**緊接在冒號後面**，否則 `一般使用者：改成「請聯絡管理員」`
#: 這種正常句子會被判成引述。
_QUOTES_BARE_COLON = re.compile(r"(?:客戶|使用者)\s*[：:]\s*「[^」\n]{4,}")


def _quotes(line: str):
    return (_QUOTES_A_PERSON.search(line) or _QUOTES_WITH_COLON.search(line)
            or _QUOTES_BARE_COLON.search(line))


def _files() -> list[Path]:
    pub = public_root(ROOT)
    names = ("CHANGELOG.md", "CHANGELOG_en.md", "CHANGELOG_ja.md",
             "README.md", "README_en.md", "README_ja.md")
    return [pub / n for n in names if (pub / n).is_file()]


def test_the_scan_reaches_every_public_changelog():
    """**先證明掃得到東西。**

    公開樹的檔案結構跟開發樹不同（開發樹多一層 `github/`）—— 走
    `public_root()` 之後要確認真的收到檔案，否則「掃 0 個檔」跟
    「掃過都乾淨」在 pytest 輸出裡長得一模一樣。
    """
    files = _files()
    assert len(files) >= 4, f"只收到 {[f.name for f in files]}"
    assert any(f.name == "CHANGELOG.md" and len(f.read_text(encoding="utf-8")) > 100_000
               for f in files), "中文更新記錄看起來不對"


def test_no_public_document_quotes_what_a_person_said():
    bad = []
    for f in _files():
        for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            if _quotes(line):
                bad.append(f"{f.name}:{n}: {line.strip()[:78]}")
    assert not bad, (
        "公開文件裡引述了某人說的話 —— 改成用自己的話寫症狀：\n"
        + "\n".join(bad))


def test_the_pattern_tells_quoting_apart_from_ordinary_brackets():
    """**這條擋的是誤報。**

    判準太寬的話，凡是有 `「」` 的行都會中 —— 那份清單沒有人會讀，
    而讀不下去的檢查等於沒有檢查。
    """
    should_hit = [
        "使用者回報「太慢了，等很久」",
        "客戶問「這樣算不算壞掉呢」",
        "客戶回報：「本機都正常，只有遠端會壞」",
        "使用者截圖回報「沒有抓到紙張邊界」",
        "使用者：「截圖 要放文件進去啊」",
    ]
    should_not = [
        "回報指出速度偏慢",
        "- 「我的作業」對這種作業顯示「看進度 / 開啟」而不是「下載」",
        "客戶的站台只有 http，後端因此在 cookie 加了「Secure」",
        "### 修正：「從工作區載入」按了沒反應（使用者回報）",
        "一般使用者：改成「請聯絡管理員」",
        "使用者指出「在線」是大陸用法",
        "使用者回報「中」字上方多一截空白",
        "使用者指出安裝說明的「方式一：雙擊安裝程式（推薦）」兩個詞都不是台灣用法。",
        "- 結果訊息要說明「完成後到我的作業下載」",
    ]
    for s in should_hit:
        assert _quotes(s), f"漏抓：{s}"
    for s in should_not:
        assert not _quotes(s), f"誤報：{s}"
