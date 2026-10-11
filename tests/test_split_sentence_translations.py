"""一句話不可以拆成片段分開翻譯（v1.16.80：SSO 設定頁、OCR 引擎頁與五支工具的說明，共 7 處）。

中文「把 X 換成 Y」拆成 `tr('把')` ＋ `<code>X</code>` ＋ `tr('換成 Y')` 分開翻，英文的語序跟中文不同，
於是印成「Turns <...> Replace these with your own values」「Takes the Embedded images (…)」。
修法：**整句一個 `tr()`**，粗體 / 程式碼用 `**…**` / `` `…` `` 標記，`marks` 濾鏡渲染時才換成標籤 ——
標籤放在句子哪裡由各語言的譯文自己決定，也不會有「片段大寫開頭卡在句子中間」的問題。

判準：

* 七頁在中文、英文、日文介面都印出**完整的一句**（標籤在句子裡正確的位置）。
* 中文介面跟原本一樣（HTML 逐字相同）。
* `marks` 先跳脫再換標記：譯文裡的 `<` 不會變成標籤。
* 每一個用 `marks` 的句子，譯文裡的 `**` 與 `` ` `` 數量跟原文一樣（翻譯漏掉標記，粗體就不見了）。
* 樣板裡不再有「把…」片段後面緊接著粗體 / 程式碼 / 連結的寫法。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_EXT = ".doc .docx .xls .xlsx .ppt .pptx .odt .ods .odp .rtf .csv .txt"

CASES = {
    "/admin/ocr-langs": {
        "zh-Hant": "把指令裡的 <code>GPUServer</code> 換成 GPU 主機的 IP 或主機名稱，例如 <code>ocr.example.com</code>。",
        "en": "Replace <code>GPUServer</code> in the commands with the GPU host&#39;s IP address or host name, "
              "for example <code>ocr.example.com</code>.",
        "ja": "コマンド中の <code>GPUServer</code> を GPU ホストの IP アドレスまたはホスト名に置き換えてください"
              "（例：<code>ocr.example.com</code>）。",
    },
    "/admin/sso": {
        "zh-Hant": "把 <code>&lt;...&gt;</code> 換成你的實際值。各家都要先在 IdP 端把 <b>Redirect URI</b>（OIDC）或 "
                   "<b>ACS</b>（SAML）設成本站對外網址，見上方提示。",
        "en": "Replace <code>&lt;...&gt;</code> with your own values. Every provider needs <b>Redirect URI</b> (OIDC) or "
              "<b>ACS</b> (SAML) set to this site&#39;s public address on the IdP side; see the note above.",
        "ja": "<code>&lt;...&gt;</code> を実際の値に置き換えてください。どの IdP でも、まず IdP 側で <b>Redirect URI</b>"
              "（OIDC）または <b>ACS</b>（SAML）を本サイトの外部向け URL に設定してください。",
    },
    "/tools/office-to-pdf/": {
        "zh-Hant": f"把<b>辦公文件</b>（{_EXT}）批次轉成 PDF。",
        "en": f"Converts <b>Office documents</b> ({_EXT}) to PDF in a batch.",
        "ja": f"<b>オフィス文書</b>（{_EXT}）を一括で PDF に変換します。",
    },
    "/tools/pdf-to-slides/": {
        "zh-Hant": "把 PDF 轉成 <b>PowerPoint (.pptx)</b> 或 <b>OpenDocument 簡報 (.odp)</b>，<b>一頁對一張投影片</b>，"
                   "位置 / 圖片 / 框線依原始座標還原，投影片尺寸沿用原稿。",
        "en": "Converts a PDF into <b>PowerPoint (.pptx)</b> or <b>OpenDocument presentation (.odp)</b>, "
              "<b>one page per slide</b>, with positions",
        "ja": "PDF を <b>PowerPoint (.pptx)</b> または <b>OpenDocument プレゼンテーション (.odp)</b> に変換します。"
              "<b>1 ページが 1 枚のスライドに対応</b>し、",
    },
    "/tools/pdf-extract-images/": {
        "zh-Hant": "把 PDF 中<b>嵌入的圖片</b>（照片、Logo、圖示等）擷取出來，可勾選想要的下載成 ZIP 或單張下載。",
        "en": "Extracts the <b>embedded images</b> (photographs, logos, icons and so on) from a PDF",
        "ja": "PDF の中の<b>埋め込まれた画像</b>（写真、ロゴ、アイコンなど）を抽出し、",
    },
    "/tools/pdf-to-office/": {
        "zh-Hant": "把 PDF 轉成 <b>Word (.docx)</b> 或 <b>OpenDocument (.odt)</b>，內含字型 / 段落 / 雜訊智慧校正。",
        "en": "Converts a PDF into <b>Word (.docx)</b> or <b>OpenDocument (.odt)</b>, including intelligent correction",
        "ja": "PDF を <b>Word (.docx)</b> または <b>OpenDocument (.odt)</b> に変換します。",
    },
    "/tools/pdf-to-image/": {
        "zh-Hant": "把 PDF 或<b>辦公文件</b>每一頁轉成圖片（PNG / WebP / JPEG），",
        "en": "Turns every page of a PDF or <b>Office document</b> into an image (PNG / WebP / JPEG)",
        "ja": "PDF または<b>オフィス文書</b>の各ページを画像（PNG / WebP / JPEG）に変換します。",
    },
}


@pytest.mark.parametrize("path", sorted(CASES))
@pytest.mark.parametrize("lang", ["zh-Hant", "en", "ja"])
def test_the_sentence_comes_out_whole(auth_off, path, lang):
    from app import main as app_main
    from tools.source_text import strip_blocks
    c = TestClient(app_main.app)
    c.cookies.set("jtdt_locale", lang)
    html = c.get(path).text
    assert CASES[path][lang] in html, f"{path}（{lang}）沒有印出完整的那一句"
    page = strip_blocks(html, "script", "style")
    assert "Turns <" not in page and "Takes the" not in page, f"{path}（{lang}）還有片段翻譯留下的殘句"
    assert "**" not in page, f"{path}（{lang}）有沒換掉的粗體標記"


def test_marks_escapes_before_it_adds_tags():
    from app.main import _tpl_marks
    assert str(_tpl_marks("a **b** `<i>` <script>")) == "a <b>b</b> <code>&lt;i&gt;</code> &lt;script&gt;"


def _marked_keys() -> list[str]:
    keys = []
    for p in (ROOT / "app").rglob("*.html"):
        text = p.read_text(encoding="utf-8")
        keys += [m.group(1) for m in re.finditer(r"""tr\(\s*'((?:[^'\\]|\\.)*)'\s*\)\s*\|\s*marks""", text, re.S)]
    return keys


@pytest.mark.parametrize("lang", ["en", "ja"])
def test_translations_keep_the_marks(lang):
    keys = _marked_keys()
    assert len(keys) >= 7, keys
    cat = json.loads((ROOT / "app" / "i18n" / f"{lang}.json").read_text(encoding="utf-8"))
    bad = []
    for k in keys:
        v = cat.get(k)
        if v is None:
            bad.append(f"沒有譯文：{k[:30]}")
        elif (v.count("**"), v.count("`")) != (k.count("**"), k.count("`")):
            bad.append(f"標記數量不同：{k[:30]} → {v[:40]}")
    assert not bad, bad


def test_no_template_splits_a_ba_sentence_around_a_tag():
    bad = []
    pat = re.compile(r"""tr\(\s*['"]把[^'"]{0,12}['"]\s*\)\s*\}\}\s*<(b|code|a|strong)\b""")
    for p in (ROOT / "app").rglob("*.html"):
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if pat.search(line):
                bad.append(f"{p.relative_to(ROOT)}:{i}")
    assert not bad, f"「把…」片段後面接標籤，英日文的語序一定會錯（整句一個 tr，用 **…** 標記）：{bad}"
