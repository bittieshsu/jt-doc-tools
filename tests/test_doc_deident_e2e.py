"""文件去識別化：**走完整條路徑**的驗收（issue #50 / #51）。

為什麼一定要有這一支：#51 那個 bug（`[縣市]` 被寫成字面 `<縣市>`）在單元層級
其實一眼可見，卻活了很多版 —— 因為沒有任何測試是「拿一份真的有地址的檔案跑
一次，看它有沒有被遮掉」。對去識別化工具而言，**唯一有意義的驗收就是最後那份
檔案裡還看不看得到那段個資**，中間任何一層綠燈都不算數。

這支從上傳一路走到產出：
    POST /detect  → 找得到哪些個資
    POST /process → 依選取結果產生新檔
    然後把產出的 PDF 重新抽文字，確認該遮的字**真的不在裡面了**。

合成素材的注意事項：用 NotoSansCJK 某些子字型寫「路」，`get_text()` 抽回來會
變成**相容表意文字 U+F937**（不是 U+8DEF）—— 那是測試素材的問題不是產品的，
所以這裡的地址一律避開「路」，改用「街」與「大道」。
"""
from __future__ import annotations

import io

import fitz
import pytest

from app.core.font_catalog import best_cjk_path


ADDR_NEW_TAIPEI = "新北市板橋區文化街一段88號3樓"      # 舊版**完全抓不到**（issue #51）
ADDR_KAOHSIUNG = "高雄市三民區建工大道 415 號"        # 數字前後有空白（PDF 常態）
ADDR_TAIPEI = "臺北市大安區信義街四段1號"             # 舊版就抓得到 —— 防止修壞
TW_ID = "A123456789"


def _cjk_fontbuffer() -> bytes:
    import fontTools.ttLib as ttlib
    picked = best_cjk_path("sans", "traditional")
    if not picked:
        pytest.skip("這台機器沒有中文字型，無法合成中文 PDF")
    path, idx = picked
    if str(path).lower().endswith(".ttc"):
        face = ttlib.TTCollection(str(path))[idx]
    else:
        face = ttlib.TTFont(str(path))
    buf = io.BytesIO()
    face.save(buf)
    return buf.getvalue()


def _make_pdf(lines: list[str]) -> bytes:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_font(fontname="cjk", fontbuffer=_cjk_fontbuffer())
    for i, line in enumerate(lines):
        page.insert_text((50, 80 + 26 * i), line, fontname="cjk", fontsize=12)
    data = doc.tobytes()
    doc.close()
    return data


@pytest.fixture(scope="module")
def detected(client):
    pdf = _make_pdf([
        "立契約書人：王大明",
        f"戶籍地址：{ADDR_NEW_TAIPEI}",
        f"通訊地址：{ADDR_KAOHSIUNG}",
        f"另一個地址：{ADDR_TAIPEI}",
        f"身分證字號：{TW_ID}",
    ])
    r = client.post("/tools/doc-deident/detect",
                    files={"file": ("契約.pdf", pdf, "application/pdf")},
                    data={"types": "addr,tw_id"})
    assert r.status_code == 200, r.text
    return r.json()


def test_detect_finds_every_address(detected):
    """三個縣市都要找到 —— 其中新北與高雄在 v1.14.63 以前一個都抓不到。"""
    found = [f["value"] for f in detected["findings"] if f["type"] == "addr"]
    joined = " | ".join(found)
    assert any("新北市板橋區" in v for v in found), f"漏抓新北市地址：{joined}"
    assert any("高雄市三民區" in v for v in found), f"漏抓高雄市地址（數字前後有空白）：{joined}"
    assert any("臺北市大安區" in v for v in found), f"連舊版抓得到的都漏了：{joined}"


def test_detect_still_finds_the_id_number(detected):
    vals = [f["value"] for f in detected["findings"] if f["type"] == "tw_id"]
    assert TW_ID in vals


def test_processed_pdf_no_longer_contains_the_addresses(client, detected):
    """**最後一關**：產出的檔案裡不可以再抽得到那幾段地址。

    偵測到、畫面上列出來，但輸出檔沒有真的處理掉 —— 這種失敗方式最危險，
    使用者會拿著「已處理」的檔案送出去。
    """
    selections = [{"page": f["page"], "bbox": f["bbox"], "type": f["type"]}
                  for f in detected["findings"]]
    r = client.post("/tools/doc-deident/process",
                    json={"upload_id": detected["upload_id"],
                          "mode": "redact", "selections": selections})
    assert r.status_code == 200, r.text

    dl = client.get(f"/tools/doc-deident/download/{detected['upload_id']}")
    assert dl.status_code == 200, dl.text
    with fitz.open(stream=dl.content, filetype="pdf") as out:
        text = "".join(p.get_text() for p in out)
    for needle in ("新北市板橋區", "高雄市三民區", "臺北市大安區", TW_ID):
        assert needle not in text, f"產出的 PDF 裡還抽得到「{needle}」"
