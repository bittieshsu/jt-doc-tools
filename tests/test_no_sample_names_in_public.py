"""測試樣本的檔名 / 客戶公司名不可以出現在會公開的檔案裡。

## 由來

`temp_pdfs/` 放的是**真實的廠商表單**（客戶提供的空白表與已填表），
兩層 `.gitignore` 擋著不會上 git。但檔名本身也是線索 —— 寫進 CHANGELOG
或介紹站就等於告訴所有人「這家公司是他們的客戶」「他們拿到了這份表」。

發版時很容易順手寫「修好了 xx 公司廠商基本資料表的欄位」，那一行就外洩了。
所以這裡直接掃會公開的檔案。

## 掃描範圍

**整個 `github/` 底下的文字檔**，包含程式碼 —— 註解與 docstring 一樣會被
推上去。專案根目錄的 CLAUDE.md 不公開，可以寫得具體一點方便日後追查；
但 TEST_PLAN.md 在 `github/` 底下**有一份公開的**，那份要去識別化。
"""
from __future__ import annotations

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
CORPUS = ROOT / "temp_pdfs"

#: 會被推上 GitHub 的檔案 —— **整個 `github/` 都是**。
#:
#: 第一版寫死了 8 個檔名，於是兩個真的外洩躲過了檢查（v1.14.31 自查發現）：
#: `github/TEST_PLAN.md` 列著四家客戶的名字，而
#: `github/app/tools/.../over_indent_cleanup.py` 的 docstring 裡有一家客戶的
#: 公司全名 —— **程式碼註解也會被推上去**。
#:
#: 這份檔案原本的說明還寫著「TEST_PLAN.md 不公開」，那個假設本身就是錯的：
#: 它在專案根目錄與 `github/` 底下各有一份，後者是公開的。
def _public_files():
    root = pathlib.Path(__file__).resolve().parent.parent / "github"
    if not root.exists():
        return []
    out = []
    for p in root.rglob("*"):
        if not p.is_file() or p.suffix.lower() not in (
                ".md", ".html", ".py", ".js", ".css", ".txt", ".sh", ".ps1",
                ".yml", ".yaml", ".toml", ".cmd", ".nsi"):
            continue
        if "__pycache__" in p.parts or ".git" in p.parts:
            continue
        out.append(str(p.relative_to(root.parent)))
    return sorted(out)


PUBLIC = _public_files()

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

@pytest.fixture(scope="module")
def corpus_company_names() -> set[str]:
    """從樣本**內容**抽出來的公司名。

    檔名的詞只擋得住「檔名被寫進文件」，擋不住「順手把客戶的公司全名寫進
    註解或測試計畫」—— 而後者真的發生過（v1.14.31 自查在一支 fixer 的
    docstring 與測試計畫裡各找到一處）。

    這裡讀 `temp_pdfs/` 的內容（那個目錄兩層 `.gitignore` 都擋著），
    所以名單本身不會外洩；抽出來只在記憶體裡用。實測 24 份 PDF 0.6 秒。
    """
    import fitz

    if not CORPUS.exists():
        return set()
    pat = re.compile(r"[一-鿿A-Za-z0-9]{2,12}(?:股份有限公司|有限公司|企業社)")
    out: set[str] = set()
    for p in sorted(CORPUS.iterdir()):
        if p.suffix.lower() != ".pdf" or p.name.startswith("syn_"):
            continue
        try:
            with fitz.open(str(p)) as d:
                for pg in d:
                    out |= set(pat.findall(pg.get_text()))
        except Exception:  # noqa: BLE001 — 讀不動就跳過
            continue
    # **通用片段不算識別**。抽取時前綴常被切掉，剩下「科技股份有限公司」
    # 這種純行業字尾 —— 那是全台灣幾萬家公司共用的寫法，拿它去比對會讓
    # 幾十個正常檔案報錯，而誤報一多這份檢查就會被當雜訊忽略。
    _INDUSTRY = ("科技", "資訊", "實業", "企業", "工程", "貿易", "顧問",
                 "有限", "股份", "國際", "開發", "投資", "文化", "傳播",
                 "建設", "營造", "生技", "醫療", "物流", "運輸", "食品")
    _SUFFIX = ("股份有限公司", "有限公司", "企業社")

    def _distinctive(n: str) -> bool:
        for suf in _SUFFIX:
            if n.endswith(suf):
                head = n[: -len(suf)]
                break
        else:
            return False
        # 去掉行業詞之後還要剩下**兩個字以上**才算得上是一家特定公司
        for ind in _INDUSTRY:
            if head.endswith(ind):
                head = head[: -len(ind)]
        return len(head) >= 2 and not head.startswith(
            ("本人", "節省", "測試", "範例", "○", "△", "□"))

    return {n for n in out if _distinctive(n)}


@pytest.mark.parametrize("rel", PUBLIC)
def test_public_files_do_not_leak_customer_company_names(rel, corpus_company_names):
    """公開檔案不可以出現樣本**內容**裡的客戶公司名。

    註解與 docstring 一樣會被推上 GitHub。
    """
    if not corpus_company_names:
        pytest.skip("沒有語料可供比對")
    p = ROOT / rel
    text = p.read_text(encoding="utf-8", errors="ignore")
    hits = sorted(n for n in corpus_company_names if n in text)
    assert not hits, (
        f"{rel} 出現了樣本裡的客戶公司名（{len(hits)} 個，內容不列出）—— "
        "改用「某供應商」這種去識別化的說法。")
