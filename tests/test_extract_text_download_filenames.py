"""擷取文字的下載：**寫檔名與讀檔名一定要是同一個值**。

2026-09-18 使用者回報：正式機上四個下載按鈕全部失敗，瀏覽器把錯誤回應存成
`txt.json` / `md.json` / `odt.json`，「存至工作區」也一起壞掉。

根因是**寫入用使用者原樣的檔名、讀取卻 `.strip()` 一次**。使用者的檔案叫
`…會議紀錄逐字稿 .pdf`（副檔名前面一個空格 —— 從網頁複製檔名時很常見），
磁碟上寫成 `…逐字稿 .txt`，讀取端組出 `…逐字稿.txt` → **404**。
而 404 的訊息是「batch 不存在或已過期」，看起來像檔案被清掉了，
完全不像檔名對不上。

**判準是「真的下載得到」**，不是「有沒有回 200」—— 要比對內容。
"""
from __future__ import annotations

import io

import pytest


def _pdf(text="Meeting minutes"):
    import fitz
    d = fitz.open()
    p = d.new_page()
    p.insert_text((72, 100), text, fontsize=14)
    b = d.tobytes()
    d.close()
    return b


@pytest.mark.parametrize("name", [
    "會議紀錄逐字稿 .pdf",          # ← 使用者實際踩到的：副檔名前有空格
    " 前面也有空白.pdf",
    "會議紀錄..pdf",
    "normal.pdf",
    "含空格 的 檔名.pdf",
    ".pdf",                          # 只有副檔名
])
def test_every_format_downloads_for_awkward_filenames(client, auth_off, name):
    r = client.post("/tools/pdf-extract-text/extract",
                    files={"file": (name, io.BytesIO(_pdf()), "application/pdf")})
    assert r.status_code == 200, r.text
    d = r.json()
    for fmt, url in d["downloads"].items():
        if not url:
            continue
        dr = client.get(url)
        assert dr.status_code == 200, f"{name} 的 {fmt} 下載失敗：{dr.status_code} {dr.text[:120]}"
        assert dr.content, f"{name} 的 {fmt} 下載是空的"


def test_the_download_carries_a_usable_filename(client, auth_off):
    """**要同時送 ASCII 的 `filename=` 與 `filename*=`**。

    只送 `filename*=` 的話，拿不到它的瀏覽器會退回用網址尾段當檔名
    （`…/download/<id>/txt` → `txt`），再依內容型別補一個副檔名。
    """
    r = client.post("/tools/pdf-extract-text/extract",
                    files={"file": ("臺北市會議紀錄.pdf", io.BytesIO(_pdf()), "application/pdf")})
    url = r.json()["downloads"]["txt"]
    cd = client.get(url).headers.get("content-disposition") or ""
    assert "filename*=" in cd, "缺 RFC 5987 的檔名"
    assert "filename=" in cd.replace("filename*=", ""), \
        "缺 ASCII 退路 —— 有些瀏覽器會改用網址尾段當檔名（使用者看到 txt.json）"


def test_the_stem_is_cleaned_once_at_write_time():
    """清理只能發生在寫入那一次 —— 讀取端再清一次就是這個 bug 的來源。"""
    from app.tools.pdf_extract_text.router import _clean_stem
    assert _clean_stem("會議紀錄逐字稿 .pdf") == "會議紀錄逐字稿"
    assert _clean_stem(" 前後空白 .pdf") == "前後空白"
    assert _clean_stem("結尾點.。.pdf") == "結尾點.。"
    #  這種只有點開頭的名字：Path.stem 回 ".pdf"，去掉點之後是 "pdf"
    # —— 不漂亮但安全（重點是非空、不會變成隱藏檔）
    assert _clean_stem(".pdf") == "pdf"
    # Path.stem 本身就會去掉目錄 —— 這是安全的結果
    assert _clean_stem("../../etc/passwd.pdf") == "passwd"
    # **反斜線在 POSIX 上不是分隔符**，但 Windows 上不能當檔名
    assert _clean_stem("a\\b.pdf") == "a_b"
    assert _clean_stem(None) == "document"
    assert _clean_stem("   .pdf") == "document"


def test_reading_the_stem_does_not_clean_it_again():
    """反向對照：讀取端如果又 strip 一次，尾端有空白的名字就再也對不上。"""
    # **`from app.tools.x import router` 拿到的是 APIRouter 物件不是模組**
    # （`__init__.py` 有 `from .router import router`）—— 本專案踩過兩次。
    import importlib, inspect
    mod = importlib.import_module("app.tools.pdf_extract_text.router")
    src = inspect.getsource(mod._read_stem)
    assert ".strip()" not in src, "讀取端不可以再清理 —— 那正是寫讀不一致的來源"
