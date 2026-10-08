"""知識庫：抽字與切段（項次、條文、長段拆分、頁首頁尾、各種格式）。"""
from __future__ import annotations

import pytest

from app.core.kb import chunker, extract
from app.core.kb.extract import Line
from tests._kb_support import HANDBOOK_TXT, make_docx, make_odt, make_pdf


def _lines(text: str) -> list[Line]:
    return extract.extract(text.encode("utf-8"), ".txt").lines


# ---------------------------------------------------------------- 項次判斷
@pytest.mark.parametrize("line,rank,label", [
    ("壹、總述", 3, "壹"),
    ("第 7 條 本法所稱公文", 4, "第7條"),
    ("第１３條", 4, "第13條"),                      # 全形數字
    ("第　2　條", 4, "第2條"),                       # 全形空白
    ("第三章 國家機密之維護", 1, "第三章"),
    ("十八、公文用語規定如下：", 5, "十八"),
    ("( 十二) 各機關對於…", 6, "（十二）"),           # PDF 抽出來的半形括號加空白
    ("（一）期望用語", 6, "（一）"),
    ("１、令：公布法律", 7, "1"),
    ("(３) 同級機關", 8, "（3）"),
    ("附件1、通知單用紙格式", 3, "附件1"),
])
def test_markers_are_recognised(line, rank, label):
    d = chunker.detect(line)
    assert d is not None, line
    assert d[0] == rank and d[1] == label


@pytest.mark.parametrize("line", [
    "準法第8條第2項規定，得就授權範圍訂定",          # 句子中間引用條號，不是新的條
    "第8條第2項規定，得就授權範圍訂定",              # 換行剛好斷在條號前面
    "（02）3356-6500 郵遞區號",                       # 電話區碼不是項次
    "2.5公分",                                        # 小數不是項次
    "112 年 9 月",
])
def test_things_that_only_look_like_markers(line):
    assert chunker.detect(line) is None, line


# ---------------------------------------------------------------- 樹與上層標題
def test_heading_path_and_parent_ref():
    chunks = chunker.chunk(_lines(HANDBOOK_TXT))
    by_text = {c["text"].split("\n")[0]: c for c in chunks}
    assert any("（二）直接稱謂用語" in c["text"] for c in chunks)
    # 「十八」底下的（一）（二）跟「十八」在同一段，母條文是「十八」
    c18 = next(c for c in chunks if "十八、公文用語規定如下" in c["text"])
    assert c18["parent_ref"] in ("十八", "一～十八", "十八～十九") or "十八" in c18["parent_ref"]
    # 章（壹、貳、參）不跟兄弟裝在同一段
    for c in chunks:
        assert not ("壹、總述" in c["text"] and "貳、公文製作" in c["text"]), c["text"]
    assert by_text  # 有切出東西


def test_long_article_is_split_with_parent_and_parts():
    long_item = "（一）" + "各機關處理文書應注意事項，均應依規定辦理。" * 60   # 約 1,300 字
    text = "肆、收文處理\n二十二、簽收應注意事項如下：\n" + long_item + "\n（二）收件應注意封口是否完整。\n"
    chunks = chunker.chunk(_lines(text))
    parts = [c for c in chunks if c["parent_ref"] == "二十二"]
    assert len(parts) >= 2, chunks
    assert all(len(c["text"]) <= chunker.CHUNK_MAX for c in chunks)
    # 第幾段／共幾段
    assert [c["part"] for c in parts] == list(range(1, len(parts) + 1))
    assert all(c["parts"] == len(parts) for c in parts)
    # 拆出來的段落仍然帶著母條文的標題
    assert all("二十二、簽收應注意事項如下" in " ".join(c["heading_path"]) or
               "二十二、簽收應注意事項如下" in c["text"] for c in parts)


def test_every_chunk_respects_the_size_limit_and_nothing_is_lost():
    body = "\n".join(f"{n}、第 {n} 點的內容，說明某件事情應該怎麼辦理。" * 3 for n in range(1, 60))
    text = "壹、總則\n一、通則如下：\n" + body
    chunks = chunker.chunk(_lines(text))
    assert all(len(c["text"]) <= chunker.CHUNK_MAX for c in chunks)
    joined = "".join(c["text"] for c in chunks)
    for n in (1, 30, 59):
        assert f"第 {n} 點的內容" in joined


def test_split_long_breaks_on_sentences():
    pieces = chunker.split_long("甲。" * 500)
    assert len(pieces) > 1 and all(len(p) <= chunker.PIECE_TARGET for p in pieces)
    assert "".join(pieces) == "甲。" * 500


# ---------------------------------------------------------------- PDF
def test_pdf_running_headers_page_numbers_and_split_article_numbers():
    data = make_pdf([
        ["文書處理手冊", "1", "壹、總述", "一、本手冊所稱文書，指處理公務", "或與公務有關之資料。"],
        ["文書處理手冊", "2", "壹、總述", "二、文書製作應採橫行格式。", "第", "7", "條", "本條文內容。"],
        ["文書處理手冊", "3", "三、第三點的內容。"],
    ])
    ex = extract.extract(data, ".pdf")
    texts = [ln.text for ln in ex.lines]
    assert "文書處理手冊" not in texts          # 每頁的書名頁首
    assert texts.count("壹、總述") == 1          # 章名頁首只留第一次（真正的標題）
    assert "1" not in texts and "2" not in texts  # 頁碼
    assert "第7條" in texts                      # 直排的條號接回來
    assert ex.page_count == 3
    chunks = chunker.chunk(ex.lines)
    c1 = next(c for c in chunks if "本手冊所稱文書" in c["text"])
    assert c1["page_from"] == 1
    assert "指處理公務或與公務有關之資料" in c1["text"]   # 換行接回同一句


def test_pdf_toc_lines_are_dropped():
    data = make_pdf([["目錄", "壹、總述……………………2", "貳、公文製作………………4", "附件10、紀錄單… … 60"],
                     ["壹、總述", "一、內容。"]])
    texts = [ln.text for ln in extract.extract(data, ".pdf").lines]
    assert not any("……" in t or "… …" in t for t in texts), texts


def test_scanned_pdf_without_text_fails_with_a_reason():
    import fitz
    doc = fitz.open()
    doc.new_page()
    with pytest.raises(extract.ExtractError) as e:
        extract.extract(doc.tobytes(), ".pdf")
    assert "OCR" in str(e.value)


# ---------------------------------------------------------------- 其他格式
def test_docx_paragraphs_headings_and_tables():
    data = make_docx([("公文寫作規範", "Heading 1"), ("一、主旨要具體。", None), ("二、說明分項。", None)])
    ex = extract.extract(data, ".docx")
    texts = [ln.text for ln in ex.lines]
    assert texts[:3] == ["公文寫作規範", "一、主旨要具體。", "二、說明分項。"]
    assert ex.lines[0].style_rank == 1
    assert any("欄一" in t and "欄二" in t for t in texts)
    chunks = chunker.chunk(ex.lines)
    assert any("主旨要具體" in c["text"] for c in chunks)


def test_odt_headings_and_paragraphs():
    data = make_odt([("機關規定", 1), ("第 1 條 本規定適用於全機關。", None)])
    ex = extract.extract(data, ".odt")
    assert [ln.text for ln in ex.lines] == ["機關規定", "第 1 條 本規定適用於全機關。"]
    assert ex.lines[0].style_rank == 1


def test_markdown_headings_and_code_blocks():
    md = "# 總則\n\n一、適用範圍。\n\n```\n不要切這裡\n```\n\n## 細則\n\n二、其他。\n"
    ex = extract.extract(md.encode("utf-8"), ".md")
    texts = [ln.text for ln in ex.lines]
    assert texts[0] == "總則" and ex.lines[0].style_rank == 1
    assert "不要切這裡" in texts


def test_big5_text_is_decoded():
    ex = extract.extract("一、繁體中文 Big5 編碼。".encode("big5"), ".txt")
    assert ex.lines[0].text.startswith("一、繁體中文")


@pytest.mark.parametrize("data,ext,ok", [
    (b"%PDF-1.4\n", ".pdf", True),
    (b"not a pdf", ".pdf", False),
    (b"PK\x03\x04garbage", ".docx", False),
    ("純文字".encode("utf-8"), ".txt", True),
    (b"\x00\x01\x02\x03\x04\x05binary", ".txt", False),
])
def test_content_is_sniffed_not_trusted_by_extension(data, ext, ok):
    assert extract.sniff_ok(data, ext) is ok


def test_docx_sniff_and_odt_sniff():
    assert extract.sniff_ok(make_docx([("x", None)]), ".docx")
    assert extract.sniff_ok(make_odt([("x", None)]), ".odt")
    assert not extract.sniff_ok(make_docx([("x", None)]), ".odt")
