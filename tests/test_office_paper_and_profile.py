"""soffice 的拋棄式設定檔：巨集硬化要真的生效，紙張預設要是 A4。

## 由來（v1.15.87，使用者 2026-09-19 回報「匯出 PDF 怎會這麼差」時一起查出來的）

全站 `convert_to_pdf` 產出的都是 **Letter（612×792）**，不是台灣在用的
**A4（595.3×841.9）**。台灣印出來 A4 短 18mm、寬 6mm，照 A4 邊界排的內容會跑掉。

根因不是 CSS：`@page { size: A4 }` LibreOffice 的 HTML 匯入**不理**，
`LANG` / `LC_PAPER` 也沒有用（實測 `LANG=zh_TW.UTF-8` 只換掉長度單位、
紙張仍是 Letter；而且多數伺服器根本沒有產生 zh_TW 這個 locale）。
LibreOffice 是從**系統語系**推紙張，而伺服器多半是 `en_US` → Letter。

**查的過程中挖出更重要的一件事**：每一支轉檔函式都帶著 `--safe-mode`，
而那個旗標會在啟動時把使用者設定檔**重設掉** —— 我們刻意種進去的
`DisableMacrosExecution`（處理的是使用者上傳的、不可信的檔案）
**跑完之後已經不在設定檔裡了**。也就是那層硬化從加進去那天起就沒有生效過。

所以這一份守的是四件事，缺一件另外三件就可能被無聲地弄回去。
"""
from __future__ import annotations

import pathlib
import re
import zipfile

import fitz
import pytest

import sys as _sys
_sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app.core import office_convert as oc
from tools.source_text import strip_py_comments  # noqa: F401  (存在性檢查)

needs_soffice = pytest.mark.skipif(
    oc.find_soffice() is None,
    reason="soffice (OxOffice/LibreOffice) not installed on this runner",
)

A4_PT = (595.3, 841.9)
LETTER_PT = (612.0, 792.0)


def _page_size(pdf: pathlib.Path) -> tuple[float, float]:
    r = fitz.open(pdf)[0].rect
    return (round(r.width, 1), round(r.height, 1))


def _close(got, want, tol=1.5) -> bool:
    return abs(got[0] - want[0]) <= tol and abs(got[1] - want[1]) <= tol


@needs_soffice
def test_a_source_without_a_page_size_comes_out_a4(tmp_path):
    """HTML / 純文字沒有頁面尺寸 —— 那時候紙張是**我們**要決定的。"""
    src = tmp_path / "t.html"
    src.write_text("<!doctype html><meta charset='utf-8'><h1>紙張</h1><p>一段。</p>",
                   encoding="utf-8")
    dst = tmp_path / "t.pdf"
    oc.convert_to_pdf(src, dst)
    got = _page_size(dst)
    assert _close(got, A4_PT), f"HTML 轉出來是 {got}，應該是 A4 {A4_PT}"

    txt = tmp_path / "t.txt"
    txt.write_text("純文字\n第二行\n", encoding="utf-8")
    dst2 = tmp_path / "t2.pdf"
    oc.convert_to_pdf(txt, dst2)
    got2 = _page_size(dst2)
    assert _close(got2, A4_PT), f"純文字轉出來是 {got2}，應該是 A4 {A4_PT}"


@needs_soffice
def test_a_source_that_declares_its_own_size_is_left_alone(tmp_path):
    """**別人文件的紙張不可以被我們改掉。**

    這條是上一條的反面。只驗「HTML 會變 A4」的話，把紙張無條件套成 A4
    也會過 —— 而那會把客戶自己排好的 Letter 文件整份改掉。
    """
    html = tmp_path / "s.html"
    html.write_text("<!doctype html><meta charset='utf-8'><p>x</p>", encoding="utf-8")
    odt = tmp_path / "s.odt"
    oc.convert_to_odt(html, odt)

    letter = tmp_path / "letter.odt"
    _rewrite_page_size(odt, letter, "8.5in", "11in")
    dst = tmp_path / "letter.pdf"
    oc.convert_to_pdf(letter, dst)
    got = _page_size(dst)
    assert _close(got, LETTER_PT), (
        f"來源自己宣告 Letter，轉出來卻是 {got} —— 我們把別人的紙張改掉了"
    )


def _rewrite_page_size(src: pathlib.Path, dst: pathlib.Path, w: str, h: str) -> None:
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zo:
        for it in zin.infolist():
            data = zin.read(it.filename)
            if it.filename == "styles.xml":
                s = data.decode("utf-8")
                s = re.sub(r'fo:page-width="[^"]+"', f'fo:page-width="{w}"', s)
                s = re.sub(r'fo:page-height="[^"]+"', f'fo:page-height="{h}"', s)
                data = s.encode("utf-8")
            zo.writestr(it, data)


@needs_soffice
def test_the_macro_hardening_survives_a_real_conversion(tmp_path, monkeypatch):
    """**判準是「soffice 跑完之後設定還在不在」，不是「我們有沒有寫出去」。**

    `--safe-mode` 時這一條會紅：soffice 啟動時把使用者設定檔重設，
    我們寫進去的 `DisableMacrosExecution` 跑完就不見了。
    只驗「檔案有寫出來」的話這個洞永遠看不到 —— 檔案確實有寫出來。

    轉檔用的是 `TemporaryDirectory`，跑完就刪掉了，所以這裡把它換成
    **留在 tmp_path 底下不刪**的版本，才看得到 soffice 動過什麼。
    """
    import tempfile as _t

    class KeepDir:
        def __init__(self, *a, **k):
            self.name = str(tmp_path / "keep")
            pathlib.Path(self.name).mkdir(parents=True, exist_ok=True)

        def __enter__(self):
            return self.name

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(oc.tempfile, "TemporaryDirectory", KeepDir)

    src = tmp_path / "m.html"
    src.write_text("<!doctype html><meta charset='utf-8'><p>x</p>", encoding="utf-8")
    oc.convert_to_pdf(src, tmp_path / "m.pdf")

    xcu = pathlib.Path(tmp_path / "keep" / "profile" / "user" / "registrymodifications.xcu")
    assert xcu.exists(), "soffice 跑完之後設定檔整個不見了"
    body = xcu.read_text(encoding="utf-8", errors="replace")
    assert "DisableMacrosExecution" in body, (
        "soffice 跑完之後巨集硬化設定被洗掉了 —— 那層防護等於不存在。"
        "先看是不是有人把 --safe-mode 加回去了。"
    )


def test_no_converter_passes_safe_mode():
    """`--safe-mode` 會把我們種下去的設定整份洗掉 —— 一個字都不可以再出現。

    它原本要防的兩件事已經有別的東西擋著：使用者自訂設定（每次呼叫都是**全新的
    拋棄式 profile**，沒有自訂可洗）、當機復原提示（`--norestore`）。
    拿掉之後用三份真實檔案逐項比對過頁數 / 尺寸 / 字數 / 逐頁像素雜湊完全一致。

    **判準要去掉註解再看** —— 上面那段說明正在解釋這個旗標（use vs mention）。
    """
    from tools.source_text import strip_py_comments
    src = pathlib.Path(__file__).resolve().parent.parent / "app" / "core" / "office_convert.py"
    body = strip_py_comments(src.read_text(encoding="utf-8"))
    hits = [i + 1 for i, ln in enumerate(body.splitlines()) if '"--safe-mode"' in ln]
    assert not hits, f"office_convert.py 第 {hits} 行又把 --safe-mode 加回去了"


def test_the_paper_default_only_applies_to_sources_without_a_page_size():
    """紙張那一項**不可以無條件套** —— 它同時是系統語系，會改 CJK 字型 fallback。

    實測一份真實廠商表 docx：套上去之後前導空白改用別的字型量寬度，整段標籤
    左移約 10pt（墨點數與字形完全相同，純粹是空白的寬度）。表單那一家工具對
    座標位移極度敏感，而它們的來源本來就寫著頁面尺寸、根本不需要這一項。
    """
    assert oc._needs_paper_default(pathlib.Path("a.html"))
    assert oc._needs_paper_default(pathlib.Path("a.TXT"))
    for name in ("a.docx", "a.odt", "a.xlsx", "a.pptx", "a.pdf", "a.odg"):
        assert not oc._needs_paper_default(pathlib.Path(name)), name
