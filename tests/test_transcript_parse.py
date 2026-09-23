"""逐字稿解析：各種格式進來，段落出去。

**這一層的錯會安靜地毀掉整個會議摘要**：發言者判錯 → 發言者佔比變垃圾；
段落切太碎 → 引用指到半句話；讀不到東西卻回空清單 → 分析「成功」產出空摘要，
而畫面上看起來完全正常。
"""
from __future__ import annotations

import io
import json
import zipfile

import pytest

from app.core import transcript_parse as tp


# ------------------------------------------------------------------ VTT / SRT

VTT = """WEBVTT

00:00:01.000 --> 00:00:04.000
<v 王小明>各位早，今天要談三件事。

00:00:04.200 --> 00:00:07.000
<v 王小明>第一件是預算。

00:00:20.000 --> 00:00:24.000
<v 李美華>預算我看過了，沒問題。
"""

SRT = """1
00:00:01,000 --> 00:00:04,000
各位早，今天要談三件事。

2
00:00:04,200 --> 00:00:07,000
第一件是預算。
"""


def test_vtt_gives_text_speaker_and_times():
    segs, _ = tp.parse(VTT.encode(), "a.vtt")
    assert [s["speaker"] for s in segs] == ["王小明", "李美華"]
    assert segs[0]["start_ms"] == 1000
    assert segs[0]["end_ms"] == 7000, "同一個發言者連續的段落，結束時間要取最後一段的"
    assert "預算" in segs[0]["text"]
    assert [s["seq"] for s in segs] == [1, 2]


def test_consecutive_cues_from_one_speaker_are_merged():
    """不合併的話一場會議會切出幾千段，**引用會指到半句話**。"""
    segs, _ = tp.parse(VTT.encode(), "a.vtt")
    assert len(segs) == 2, f"三個 cue 應該併成兩段，實際 {len(segs)}"


def test_a_long_gap_stops_the_merge():
    """隔太久就是換話題了，不要黏在一起。"""
    vtt = ("WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n<v A>甲\n\n"
           "00:01:00.000 --> 00:01:01.000\n<v A>乙\n")
    segs, _ = tp.parse(vtt.encode(), "a.vtt")
    assert len(segs) == 2


def test_srt_parses_and_ignores_the_index_lines():
    segs, _ = tp.parse(SRT.encode(), "a.srt")
    assert len(segs) == 1                      # 同一段話（沒有發言者、時間相鄰）
    assert "1" not in segs[0]["text"].split()  # 序號行不可以變成內容
    assert segs[0]["start_ms"] == 1000 and segs[0]["end_ms"] == 7000


def test_vtt_cue_settings_after_the_end_time_are_ignored():
    vtt = "WEBVTT\n\n00:00:01.000 --> 00:00:04.000 align:start position:10%\n內容\n"
    segs, _ = tp.parse(vtt.encode(), "a.vtt")
    assert segs[0]["end_ms"] == 4000


# ------------------------------------------------------------------ 發言者判定

def test_a_colon_inside_a_sentence_is_not_a_speaker():
    """**這條是這支解析器最容易錯的地方。**

    `我們下週要做三件事：A、B、C` 的前半段也符合「冒號前面是一小段字」，
    單看一行分不出來。判準是「同一個名字要出現兩次以上」。
    """
    text = ("王小明：各位早。\n"
            "我們下週要做三件事：設計、開發、測試。\n"
            "王小明：先講第一件。\n")
    segs, _ = tp.parse(text.encode(), "a.txt")
    speakers = [s.get("speaker") for s in segs]
    assert "我們下週要做三件事" not in speakers
    assert speakers.count("王小明") >= 1


def test_a_one_off_prefix_is_kept_in_the_text():
    """判成不是發言者的時候，那段字要**留在內文裡**，不可以吃掉。"""
    text = "王小明：各位早。\n注意事項：請準時。\n王小明：開始吧。\n"
    segs, _ = tp.parse(text.encode(), "a.txt")
    joined = " ".join(s["text"] for s in segs)
    assert "注意事項" in joined, "被判成不是發言者的前綴不可以消失"


def test_a_name_that_repeats_is_taken_as_a_speaker():
    text = "Alice: morning\nBob: hi\nAlice: let's start\n"
    segs, _ = tp.parse(text.encode(), "a.txt")
    assert {s.get("speaker") for s in segs} == {"Alice", "Bob"}


def test_a_long_prefix_is_never_a_speaker():
    long = "這是一段非常長的開場白而且後面還有冒號"
    text = f"{long}：內容一\n{long}：內容二\n"
    segs, _ = tp.parse(text.encode(), "a.txt")
    assert all(s.get("speaker") is None for s in segs), "名字不會有那麼長"


# ------------------------------------------------------------------ 純文字

def test_plain_text_with_leading_timestamps():
    text = "[00:00:05] 王小明：開始了\n[00:01:00] 李美華：好\n[00:02:00] 王小明：結束\n"
    segs, _ = tp.parse(text.encode(), "a.txt")
    assert segs[0]["start_ms"] == 5000
    assert segs[0]["end_ms"] == 60000, "下一段的開始就是這一段的結束"


def test_plain_text_without_times_still_works():
    """**沒有時間是正常情況** —— 發言者佔比與時間軸自動不出現，不是失敗。"""
    segs, _ = tp.parse("第一句話\n第二句話\n第三句話\n".encode(), "a.txt")
    assert len(segs) >= 1
    assert all("start_ms" not in s for s in segs)


def test_paragraphs_win_over_lines_when_both_exist():
    text = "第一段第一行\n第一段第二行\n\n第二段\n"
    segs, _ = tp.parse(text.encode(), "a.txt")
    assert len(segs) == 1 or "第一段第二行" in segs[0]["text"]


# ------------------------------------------------------------------ JSON

def test_json_list_of_segments():
    data = json.dumps([
        {"text": "甲說的話", "speaker": "S1", "start_ms": 0, "end_ms": 1000},
        {"text": "乙說的話", "speaker": "S2", "start_ms": 1000, "end_ms": 2000},
    ]).encode()
    segs, _ = tp.parse(data, "a.json")
    assert [s["speaker"] for s in segs] == ["S1", "S2"]
    assert segs[1]["end_ms"] == 2000


def test_json_wrapped_in_a_segments_key():
    data = json.dumps({"segments": [{"text": "只有一句", "speaker_id": "S1"}]}).encode()
    segs, _ = tp.parse(data, "a.json")
    assert segs[0]["speaker"] == "S1"


def test_json_without_a_segment_list_says_what_is_wrong():
    with pytest.raises(tp.TranscriptError) as e:
        tp.parse(b'{"hello": 1}', "a.json")
    assert "segments" in str(e.value)


# ------------------------------------------------------------------ Office

def _docx(paras: list[str]) -> bytes:
    ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    body = "".join(f'<w:p><w:r><w:t>{p}</w:t></w:r></w:p>' for p in paras)
    xml = f'<?xml version="1.0"?><w:document xmlns:w="{ns}"><w:body>{body}</w:body></w:document>'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("word/document.xml", xml)
    return buf.getvalue()


def test_docx_paragraphs_become_segments():
    segs, _ = tp.parse(_docx(["王小明：開場", "李美華：回應", "王小明：結論"]), "a.docx")
    assert {s.get("speaker") for s in segs} == {"王小明", "李美華"}


def test_a_file_that_is_not_a_zip_says_so():
    with pytest.raises(tp.TranscriptError) as e:
        tp.parse(b"not a zip at all", "a.docx")
    assert "毀損" in str(e.value) or "讀不開" in str(e.value)


# ------------------------------------------------------------------ 失敗路徑

def test_an_unsupported_extension_lists_what_is_supported():
    with pytest.raises(tp.TranscriptError) as e:
        tp.parse(b"x", "a.pdf")
    assert ".vtt" in str(e.value), "訊息要說得出可以用什麼"


def test_an_empty_file_raises_instead_of_returning_nothing():
    """**不可以回空清單** —— 那會讓後面的分析「成功」產出一份空摘要。"""
    with pytest.raises(tp.TranscriptError):
        tp.parse(b"   \n\n  \n", "a.txt")


def test_the_supported_list_has_no_duplicates_and_is_all_lowercase():
    assert len(set(tp.SUPPORTED)) == len(tp.SUPPORTED)
    assert all(e == e.lower() and e.startswith(".") for e in tp.SUPPORTED)


# ------------------------------------------------------------------ 接得上分析

def test_the_output_is_what_meeting_insight_expects():
    """判準落在**下游真的吃得下**，不是欄位名長得像。"""
    from app.core import meeting_insight as mi
    segs, _ = tp.parse(VTT.encode(), "a.vtt")
    wins = mi.make_windows(segs)
    assert wins and wins[0].segments
    rendered = mi.render_window(wins[0])
    assert "王小明" in rendered or "預算" in rendered
    stats = mi.speaker_stats(segs)
    assert set(stats) == {"王小明", "李美華"}
    assert stats["王小明"]["speaking_ms"] > 0


# ------------------------------------------------------------------ 名字長度的尺

def test_a_one_off_name_is_judged_against_the_names_this_file_already_uses():
    """**那把尺是從文件自己量出來的，不是寫死的常數。**

    同一段文字，只要把已確認的名字換成比較長的，原本被判掉的那個就會被接受 ——
    寫死常數的話這兩種情況會得到一樣的結果。
    """
    short = "王小明：一\n注意事項：二\n王小明：三\n"
    long_ = "王小明副理：一\n注意事項：二\n王小明副理：三\n"
    a = {s.get("speaker") for s in tp.parse(short.encode(), "a.txt")[0]}
    b = {s.get("speaker") for s in tp.parse(long_.encode(), "a.txt")[0]}
    assert "注意事項" not in a, "已確認的名字只有 3 個字，4 個字的就不是名字"
    assert "注意事項" in b, "已確認的名字有 5 個字，這把尺就該放寬"


def test_nothing_is_a_speaker_when_no_name_ever_repeats():
    """沒有任何名字重複 = 這份檔案根本沒在用「發言者：內容」的格式。"""
    text = "第一件事：要做 A\n第二件事：要做 B\n第三件事：要做 C\n"
    segs, _ = tp.parse(text.encode(), "a.txt")
    assert all(s.get("speaker") is None for s in segs)
    assert "第一件事" in " ".join(s["text"] for s in segs), "判掉的字要留在內文"


def test_a_latin_name_longer_than_a_chinese_one_is_still_accepted():
    """中日韓與拉丁兩套上限 —— 用同一個數字會把正常的英文名字判掉。"""
    text = "Dr. Jennifer Rodriguez: hello\nBob: hi\nDr. Jennifer Rodriguez: bye\n"
    segs, _ = tp.parse(text.encode(), "a.txt")
    assert "Dr. Jennifer Rodriguez" in {s.get("speaker") for s in segs}


def test_a_sentence_ending_period_is_still_not_a_name():
    """縮寫的點號放行，句號不放行 —— 不然半句話又會被當成名字。"""
    text = ("This is a full sentence. Then: something\n"
            "This is a full sentence. Then: another\n")
    segs, _ = tp.parse(text.encode(), "a.txt")
    assert all(s.get("speaker") is None for s in segs)


# ------------------------------------------------------------------ 界線：什麼時候不猜

def test_a_transcript_that_already_names_the_speaker_is_never_guessed_at():
    """**逐字稿自己帶發言者時，一個字都不猜。**

    發言者分離（diarization）是語音服務的事 —— 它的產出每一段都帶 `speaker_id`。
    我們這邊的判斷**只是給「不是語音服務產的」逐字稿用的退路**
    （Teams / Zoom 匯出的字幕、誰打字打出來的 .txt）。

    判準用**最容易被誤判的那個句子**：`我們下週要做三件事：設計、開發、測試`
    —— 判斷那條路一定會拿它去試，所以只要結果原封不動，就證明沒走那條路。
    """
    trap = "我們下週要做三件事：設計、開發、測試"

    # ① 語音服務的段落格式
    partner_json = json.dumps({"segments": [
        {"text": trap, "speaker_id": "S1", "start_ms": 0, "end_ms": 5000},
        {"text": "注意事項：請準時", "speaker_id": "S2",
         "start_ms": 5000, "end_ms": 8000}]}).encode()
    segs, _ = tp.parse(partner_json, "a.json")
    assert [s["speaker"] for s in segs] == ["S1", "S2"]
    assert segs[0]["text"] == trap, "帶了發言者就不該再去拆前綴"

    # ② WebVTT 的 <v>（字幕的標準寫法，也是結構化的）
    vtt = f"WEBVTT\n\n00:00:01.000 --> 00:00:05.000\n<v 王小明>{trap}\n"
    segs2, _ = tp.parse(vtt.encode(), "a.vtt")
    assert segs2[0]["speaker"] == "王小明"
    assert segs2[0]["text"] == trap


def test_only_plain_text_falls_back_to_guessing():
    """反向對照：同一句話，沒有結構化發言者時才會走判斷 ——
    而且這一句**判斷的結果是「不是發言者」**（見上面那幾條）。"""
    trap = "我們下週要做三件事：設計、開發、測試"
    segs, _ = tp.parse(f"{trap}\n{trap}\n".encode(), "a.txt")
    assert all(s.get("speaker") is None for s in segs)
    assert trap in segs[0]["text"], "判掉之後那段字要完整留著"


# ================================================================== 多種排法
#
# 逐字稿的來源很多（會議軟體匯出、語音服務、人工打字），**每一家的排法都不一樣**。
# 少認一種，那一份的發言者就整個不見，而畫面上只顯示「沒有認出任何發言者」——
# 看不出是格式沒支援（2026-09-18 使用者回報：時間明明在檔案裡卻說沒有）。
#
# 下面每一種都是真的看過的排法。**加新格式時在這裡補一條**。

SHAPES = {
 "標頭自己一行＋時間": (
   "[00:00] PM - 雅婷：\n好，先確認三件事。\n"
   "[00:45] Frontend - 小明：\n第一件我有疑問。\n"
   "[01:02] PM - 雅婷：\n你說。\n"),
 "標頭自己一行沒時間": (
   "王小明：\n各位早。\n"
   "李美華：\n我看過草案了。\n"
   "王小明：\n那就開始。\n"),
 "一行一句帶方括號時間": (
   "[00:00] 王小明：各位早。\n[00:45] 李美華：我看過了。\n[01:02] 王小明：好。\n"),
 "一行一句沒有時間": (
   "王小明：各位早。\n李美華：我看過了。\n王小明：好。\n"),
 "名字在前時間在括號": (
   "王小明 (00:00)：各位早。\n李美華 (00:45)：我看過了。\n王小明 (1:02)：好。\n"),
 "清單符號開頭": (
   "- 王小明：各位早。\n- 李美華：我看過了。\n- 王小明：好。\n"),
 "冒號是半形": (
   "Alice: morning everyone\nBob: I read the draft\nAlice: let's start\n"),
 "中括號包名字": (
   "[王小明] 各位早。\n[李美華] 我看過了。\n[王小明] 好。\n"),
}


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_every_known_transcript_shape_gets_speakers(shape):
    segs, _ = tp.parse(SHAPES[shape].encode(), "a.txt")
    spk = {s.get("speaker") for s in segs if s.get("speaker")}
    assert len(spk) >= 2, f"{shape}：只認出 {spk}"
    joined = " ".join(s["text"] for s in segs)
    assert "各位早" in joined or "morning" in joined or "先確認三件事" in joined, \
        f"{shape}：內容掉了"
    # **發言者名字不可以留在內文裡**（留著的話摘要會把它當成句子的一部分）
    assert "：各位早" not in joined and ": morning" not in joined


@pytest.mark.parametrize("shape", ["標頭自己一行＋時間", "一行一句帶方括號時間",
                                   "名字在前時間在括號"])
def test_the_shapes_that_carry_time_really_produce_time(shape):
    """**時間在檔案裡就要讀得出來** —— 使用者回報過「明明有時間卻說沒有」。"""
    segs, _ = tp.parse(SHAPES[shape].encode(), "a.txt")
    assert any("start_ms" in s for s in segs), f"{shape}：時間沒讀到"


def test_markdown_headings_and_the_meeting_header_are_not_speakers():
    """逐字稿前面那段會議資訊（主題／時間／與會人員）**不是發言**。

    不濾掉的話 `## 會議主題`、`* 與會人員` 會各自變成一位「發言者」，
    而它們後面那一大串名單會變成「發言」—— 發言者佔比直接報廢。
    """
    text = (
        "## 會議主題：系統升級\n\n"
        "* 會議時間：15 分鐘\n"
        "* 與會人員：\n"
        "  * PM：雅婷\n"
        "  * 工程：凱文\n\n"
        "------------------------------\n"
        "## 完整逐字稿\n"
        "[00:00] PM - 雅婷：\n好，開始吧。\n"
        "[00:30] 工程 - 凱文：\n我這邊先報告。\n"
        "[01:00] PM - 雅婷：\n請說。\n")
    segs, _ = tp.parse(text.encode(), "a.txt")
    spk = {s.get("speaker") for s in segs if s.get("speaker")}
    assert spk == {"PM - 雅婷", "工程 - 凱文"}, f"多認了：{spk}"
    assert not any(s.get("speaker", "").startswith(("#", "*")) for s in segs)
    # 會議資訊要留著給模型看（誰參加、談什麼），只是沒有發言者
    joined = " ".join(s["text"] for s in segs)
    assert "系統升級" in joined and "雅婷" in joined


def test_a_long_role_prefixed_name_is_accepted_when_it_is_a_bare_header():
    """`Backend Lead - 凱文` 有 17 個字，超過一般的名字長度上限 ——
    但**冒號後面空著的那種行沒有歧義**（句子裡的冒號後面一定有字），
    所以這種形狀可以放寬。"""
    text = ("[00:00] Backend Lead - 凱文：\n我先報告架構。\n"
            "[00:30] UI/UX Designer - 萱萱：\n介面我調整過了。\n"
            "[01:00] Backend Lead - 凱文：\n好。\n")
    segs, _ = tp.parse(text.encode(), "a.txt")
    assert {s.get("speaker") for s in segs if s.get("speaker")} == \
        {"Backend Lead - 凱文", "UI/UX Designer - 萱萱"}


# ------------------------------------------------------------------ 手動指定排法

def test_the_detected_shape_is_reported():
    """**要告訴使用者用了哪一種排法** —— 判斷錯的時候他才知道要去改。"""
    segs, shape = tp.parse(SHAPES["標頭自己一行＋時間"].encode(), "a.txt")
    assert shape == "header"
    segs, shape = tp.parse(SHAPES["一行一句沒有時間"].encode(), "a.txt")
    assert shape == "inline"
    segs, shape = tp.parse(VTT.encode(), "a.vtt")
    assert shape == "cues"


def test_the_user_can_force_a_shape():
    """自動判斷一定有猜錯的時候，**要有路可走**。"""
    text = SHAPES["標頭自己一行＋時間"]
    auto, _ = tp.parse(text.encode(), "a.txt")
    assert any(s.get("speaker") for s in auto)
    # 明講「這份沒有發言者」→ 一個都不猜
    forced, shape = tp.parse(text.encode(), "a.txt", shape="plain")
    assert shape == "plain"
    assert all(s.get("speaker") is None for s in forced), "指定 plain 就不該猜發言者"
    assert " ".join(s["text"] for s in forced), "內容不可以掉"


def test_an_unknown_shape_falls_back_to_auto_instead_of_failing():
    """壞掉的參數不該讓整個上傳失敗 —— 那是使用者看不懂的錯誤。"""
    segs, shape = tp.parse(SHAPES["一行一句沒有時間"].encode(), "a.txt", shape="不存在")
    assert shape in tp.SHAPES and segs


def test_the_shape_list_is_the_single_source_for_the_ui():
    assert "auto" in tp.SHAPES and tp.SHAPES["auto"]
    assert all(isinstance(v, str) and v for v in tp.SHAPES.values())
