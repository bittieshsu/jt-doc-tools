"""測試樣本的檔名 / 客戶公司名不可以出現在會公開的檔案裡。

## 由來

`temp_pdfs/` 放的是**真實的廠商表單**（客戶提供的空白表與已填表），
兩層 `.gitignore` 擋著不會上 git。但檔名本身也是線索 —— 寫進 CHANGELOG
或介紹站就等於告訴所有人「這家公司是他們的客戶」「他們拿到了這份表」。

發版時很容易順手寫「修好了 xx 公司廠商基本資料表的欄位」，那一行就外洩了。
所以這裡直接掃會公開的檔案。

## 掃描範圍

只掃**會被推到 GitHub 的檔案**（`github/` 底下）。專案內部的 CLAUDE.md、
TEST_PLAN.md 不公開，可以寫得具體一點，方便日後追查。
"""
from __future__ import annotations

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
CORPUS = ROOT / "temp_pdfs"

#: 會被推上 GitHub 的檔案。
PUBLIC = [
    "github/CHANGELOG.md",
    "github/README.md",
    "github/API.md",
    "github/LLM.md",
    "github/OPS.md",
    "github/INSTALL.md",
    "github/docs/index.html",
    "github/docs/api.html",
]

#: 這些英文字在技術文件裡本來就會出現，不構成識別。
_COMMON_WORDS = {
    "file", "test", "form", "data", "final", "copy", "web", "scan",
    "release", "notes", "migrate", "guide", "proxmox", "purchase",
    "evidence", "revision",
}

#: 通用詞：任何表單都可能叫這個名字，不構成識別。
GENERIC = {
    "廠商基本資料表", "供應商基本資料表", "供應商基本資料", "廠商代號申請表",
    "交易對象基本資料表", "供應商付款資料表", "帳號申請單", "報價單",
    "修訂版", "委託匯款同意書", "廠商電匯申請書", "廠商匯款申請書",
}


def _sample_tokens() -> set[str]:
    """從樣本檔名抽出可識別的詞（去掉通用詞）。"""
    if not CORPUS.exists():
        return set()
    out: set[str] = set()
    for p in CORPUS.iterdir():
        if p.is_dir() or p.name.startswith("."):
            continue
        # **合成樣本是我們自己造的**（`syn_*`），檔名是版型描述、不含任何
        # 客戶資訊。把它拆出來的 column / label / suffix 這種通用英文字
        # 收進來只會誤報 —— 而誤報一多，這份檢查就會被當雜訊忽略。
        # 網路抓的公開空白表（`pub_*`）同理：那是各公司官網公開下載的。
        if p.name.startswith(("syn_", "pub_")):
            continue
        stem = re.sub(r"[\d_\-()（）\s]+", " ", p.stem)
        for tok in re.findall(r"[一-鿿]{3,}", stem):
            if tok in GENERIC:
                continue
            # 通用詞的一部分也放過（「廠商基本資料」是「廠商基本資料表」的前綴）
            if any(tok in g or g in tok for g in GENERIC):
                continue
            out.add(tok)
        # 英文的品牌 / 公司代號。**只認樣本檔名獨有的**：像 Proxmox、
        # Release、migrate 這種是通用技術詞，本來就會出現在說明文件裡，
        # 收進來只會製造誤報，而誤報一多這份檢查就會被當雜訊忽略。
        for tok in re.findall(r"[a-zA-Z]{4,}", p.stem):
            if tok.lower() in _COMMON_WORDS:
                continue
            out.add(tok)
    return out


@pytest.mark.parametrize("rel", PUBLIC)
def test_public_docs_do_not_leak_sample_names(rel):
    """公開檔案不可以出現樣本檔名 / 客戶名。"""
    p = ROOT / rel
    if not p.exists():
        pytest.skip(f"{rel} 不存在")
    text = p.read_text(encoding="utf-8", errors="ignore")
    hits = sorted(t for t in _sample_tokens()
                  if len(t) >= 3 and t in text)
    assert not hits, (
        f"{rel} 出現了測試樣本的識別名稱：{hits}\n"
        "樣本檔名與客戶公司名不可以寫進會公開的檔案 —— "
        "改用「18 份表單樣本」這種去識別化的說法。")


def test_corpus_is_gitignored():
    """樣本本身更不可以上 git —— 兩層 .gitignore 都要擋。"""
    for rel in (".gitignore", "github/.gitignore"):
        p = ROOT / rel
        if not p.exists():
            continue
        assert "temp_pdfs/" in p.read_text(encoding="utf-8"), (
            f"{rel} 沒有排除 temp_pdfs/ —— 真實客戶表單會被推上 GitHub")
