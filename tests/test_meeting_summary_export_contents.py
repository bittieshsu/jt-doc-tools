"""會議摘要匯出的內容（v1.16.37，2026-10-02 使用者一連串回報）。

一條一條對應使用者看到的問題：
* 摘要明明有「Bianca」，卻被標成「查不到依據」
* 匯出的 PDF / Word / ODF 裡**沒有逐字稿**
* 圖超出頁面
* 「各議題時間佔比」要由高到低
* 英文長字被折成兩半（`OfflineMi / rror`）
* 版面主題要能先預覽；預設要是「清爽」

**判準一律落在產出上**（TEST_PLAN §0.5）：打開 Markdown / docx / odt 看裡面有什麼，
不是看「有沒有回 200」。
"""
from __future__ import annotations

import importlib
import io
import re
import zipfile

import pytest

from app.core import meeting_charts as mc
from app.core import meeting_insight as mi


def _router():
    return importlib.import_module("app.tools.meeting_summary.router")


def _analysis() -> dict:
    return {
        "summary": {"text": "會議確認預算。"},
        "items": {"decisions": [{"text": "預算維持原案", "segment_ids": [2]}],
                  "actions": [{"text": "把報價寄給Acme-Kevin", "owner": "Bianca",
                               "segment_ids": [3]}],
                  "risks": [], "questions": []},
        "chapters": [{"title": "預算", "segment_ids": [1, 2], "start_ms": 0,
                      "end_ms": 60000, "duration_ms": 60000},
                     {"title": "時程", "segment_ids": [3, 4], "start_ms": 60000,
                      "end_ms": 300000, "duration_ms": 240000}],
        "mindmap": [{"node_id": "c1", "parent_id": None, "label": "預算",
                     "type": "topic", "segment_ids": [1]}],
        "speaker_stats": {"王小明": {"turn_count": 2, "chars": 40, "char_pct": 50.0,
                                    "speaking_ms": 30000, "first_seq": 1},
                          "Bianca": {"turn_count": 2, "chars": 40, "char_pct": 50.0,
                                     "speaking_ms": 30000, "first_seq": 2}},
        "source": {"filename": "週會.txt", "segments": 4},
    }


def _segments() -> list[dict]:
    return [
        {"seq": 1, "speaker": "王小明", "start_ms": 0, "end_ms": 5000,
         "text": "今天談預算。"},
        {"seq": 2, "speaker": "Bianca", "start_ms": 5000, "end_ms": 9000,
         "text": "預算維持原案 | 不加。"},
        {"seq": 3, "speaker": "Bianca", "start_ms": 61000, "end_ms": 65000,
         "text": "我把報價寄給 *Acme-Kevin*。"},
        {"seq": 4, "speaker": "王小明", "start_ms": 70000, "end_ms": 80000,
         "text": "好。"},
    ]


# ------------------------------------------------------------------ 依據檢查

def test_a_name_that_is_in_the_items_is_not_flagged_as_unsupported():
    """待辦內文以英文結尾、負責人又是英文名字時，兩段原本直接黏在一起
    （`Acme-JackBianca`），兩個名字都被判成「查不到依據」。"""
    a = _analysis()
    ok, bad = mi.summary_is_grounded("Bianca 會把報價寄給 Acme-Kevin。",
                                     a["chapters"], a["items"])
    assert ok, f"素材裡有的名字被標成查不到依據：{bad}"


def test_a_name_that_is_nowhere_is_still_flagged():
    """反向對照：只驗上面那條的話，把整個檢查拿掉也會過。"""
    a = _analysis()
    ok, bad = mi.summary_is_grounded("Bob 會把報價寄給 Acme-Kevin。",
                                     a["chapters"], a["items"])
    assert not ok and "bob" in [b.lower() for b in bad], bad


# ------------------------------------------------------------------ 逐字稿

def test_the_export_carries_the_whole_transcript_as_a_table():
    md = _router()._md(_analysis(), charts=False, embed=False, segments=_segments())
    assert "## 逐字稿" in md, "匯出的檔案裡沒有逐字稿"
    body = md.split("## 逐字稿", 1)[1]
    rows = [ln for ln in body.splitlines() if ln.startswith("| ") and "---" not in ln]
    assert rows[0] == "| 段 | 時間 | 發言者 | 內容 |", rows[0]
    assert len(rows) == 1 + len(_segments()), f"段數不對：{len(rows) - 1}"
    # 內文裡的 `|` 要跳脫 —— 不然那一列會多出一欄、整張表錯位
    for r in rows:
        cells = re.split(r"(?<!\\)\|", r)
        assert len(cells) == 6, f"這一列的欄數不對（`|` 沒有跳脫）：{r}"
    # `*` 不可以變成斜體
    assert r"\*Acme-Kevin\*" in body


def test_the_transcript_can_be_left_out():
    md = _router()._md(_analysis(), charts=False, embed=False, segments=_segments(),
                       transcript=False)
    assert "## 逐字稿" not in md


def test_columns_without_data_are_left_out():
    """純文字逐字稿沒有時間也沒有發言者 —— 一整欄空白比沒有那一欄更難看。"""
    segs = [{"seq": 1, "text": "第一句"}, {"seq": 2, "text": "第二句"}]
    md = _router()._md(_analysis(), charts=False, embed=False, segments=segs)
    assert "| 段 | 內容 |" in md


# ------------------------------------------------------------------ 圖

def test_document_charts_do_not_repeat_the_section_heading():
    """文件裡每張圖已經有章節標題，圖自己再畫一次就是重複的。"""
    for name, svg in mc.build_all(_analysis(), _segments(), titled=False).items():
        assert 'font-weight="700"' not in svg, f"{name} 還畫著自己的標題"


def test_the_topic_list_comes_first():
    keys = list(mc.build_all(_analysis(), _segments()))
    assert keys[:2] == ["chapters", "timeline"], keys


def test_the_share_chart_puts_the_longest_topic_first():
    svg = mc.chapter_timeline(_analysis()["chapters"])
    titles = [m for m in re.findall(r'<text x="18" y="\d+"[^>]*>([^<]+)</text>', svg)]
    assert titles == ["時程", "預算"], f"清單沒有由高到低：{titles}"
    bars = re.findall(r'<rect x="(\d+)" y="44" width="\d+" height="26" fill="([^"]+)"', svg)
    first = min(bars, key=lambda b: int(b[0]))[1]
    assert first == mc.PALETTE[1], "長條最左邊不是最長的那個議題（或顏色沒有跟著議題走）"


def test_latin_words_are_not_cut_in_half():
    lines = mc._wrap("請把 OfflineMirror 的設定寄給 Acme-Kevin 確認", 10)
    joined = "\n".join(lines)
    assert "OfflineMirror" in joined and "Acme-Kevin" in joined, lines


def test_images_in_word_and_odf_are_never_wider_than_the_print_width():
    import base64

    from PIL import Image

    def png(w: int) -> str:
        b = io.BytesIO()
        Image.new("RGB", (w, 20), "white").save(b, "PNG")
        return base64.b64encode(b.getvalue()).decode()

    r = _router()
    html = (f'<img src="data:image/png;base64,{png(980)}" alt="a">'
            f'<img src="data:image/png;base64,{png(300)}" alt="b">')
    widths = [int(w) for w in re.findall(r'width="(\d+)"', r._limit_image_width(html))]
    assert widths == [r._PRINT_IMG_W, 300], f"寬圖要縮到上限、窄圖照原寸：{widths}"


def _cm(v: str) -> float:
    n = float(re.match(r"[\d.]+", v).group())
    return n * {"cm": 1, "mm": 0.1, "in": 2.54, "pt": 2.54 / 72}[re.search(r"[a-z]+$", v).group()]


@pytest.mark.parametrize("fmt", ["docx", "odt"])
def test_no_image_runs_off_the_page_in_word_or_odf(fmt):
    """2026-10-02 使用者回報「有些圖有超過頁面」—— 判準是**產出裡每張圖的寬度**
    不超過頁面扣掉邊界的寬度。"""
    from app.core import office_convert
    if not office_convert.find_soffice():
        pytest.skip("這台機器沒有 Office 引擎")
    data = _router()._report_doc(_analysis(), "週會", fmt, segments=_segments())[0]
    z = zipfile.ZipFile(io.BytesIO(data))
    if fmt == "docx":
        doc = z.read("word/document.xml").decode("utf-8")
        pg = re.search(r'<w:pgSz w:w="(\d+)"', doc)
        mar = re.search(r'<w:pgMar[^>]*w:right="(\d+)"[^>]*w:left="(\d+)"', doc) or \
            re.search(r'<w:pgMar[^>]*w:left="(\d+)"[^>]*w:right="(\d+)"', doc)
        text_twips = int(pg.group(1)) - int(mar.group(1)) - int(mar.group(2))
        widths = [int(x) for x in re.findall(r'<wp:extent cx="(\d+)"', doc)]
        assert widths, "docx 裡一張圖都沒有 —— 這條沒有東西可驗"
        for w in widths:
            assert w <= text_twips * 635 * 1.01, (
                f"圖寬 {w / 360000:.1f} cm 超過版面 {text_twips * 635 / 360000:.1f} cm")
    else:
        styles = z.read("styles.xml").decode("utf-8")
        content = z.read("content.xml").decode("utf-8")
        lay = re.search(r'<style:page-layout-properties[^>]*>', styles).group()
        pw = _cm(re.search(r'fo:page-width="([^"]+)"', lay).group(1))
        ml = _cm(re.search(r'fo:margin-left="([^"]+)"', lay).group(1))
        mr = _cm(re.search(r'fo:margin-right="([^"]+)"', lay).group(1))
        widths = [_cm(w) for w in re.findall(
            r'<draw:frame[^>]*svg:width="([^"]+)"', content)]
        assert widths, "odt 裡一張圖都沒有 —— 這條沒有東西可驗"
        for w in widths:
            assert w <= (pw - ml - mr) * 1.01, f"圖寬 {w:.1f} cm 超過版面 {pw - ml - mr:.1f} cm"


@pytest.mark.parametrize("fmt", ["docx", "odt"])
def test_the_transcript_is_in_word_and_odf_too(fmt):
    from app.core import office_convert
    if not office_convert.find_soffice():
        pytest.skip("這台機器沒有 Office 引擎")
    data = _router()._report_doc(_analysis(), "週會", fmt, segments=_segments())[0]
    z = zipfile.ZipFile(io.BytesIO(data))
    raw = z.read("word/document.xml" if fmt == "docx" else "content.xml").decode("utf-8")
    text = re.sub(r"<[^>]+>", "", raw)
    assert "逐字稿" in text and "今天談預算" in text, f"{fmt} 裡沒有逐字稿"


# ------------------------------------------------------------------ 版面主題

def test_the_default_theme_is_the_one_labelled_default():
    """下拉寫著「清爽（預設）」，選中的卻是「商務報告」（2026-10-02 使用者回報）。"""
    r = _router()
    labels = {t["id"]: t["name"] for t in r._doc_themes()}
    assert r.DEFAULT_THEME == "classic"
    assert "預設" in labels[r.DEFAULT_THEME], labels


def test_the_page_selects_the_default_theme(client, auth_off):
    html = client.get("/tools/meeting-summary/").text
    sel = re.search(r'<select class="[^"]*" id="msTheme">(.*?)</select>', html, re.S).group(1)
    picked = re.findall(r'<option value="([^"]+)" selected', sel)
    assert picked == ["classic"], f"頁面預設選中的主題：{picked}"


def test_the_theme_preview_renders_the_theme(client, auth_off):
    a = client.get("/tools/meeting-summary/theme-preview/classic")
    b = client.get("/tools/meeting-summary/theme-preview/report")
    assert a.status_code == 200 and b.status_code == 200
    assert "<table" in a.text and a.text != b.text, "兩個主題的預覽一模一樣"
    # CSP 的 style-src 沒有 unsafe-inline —— 沒有 nonce 的 `<style>` 會被整段擋掉
    assert "<style>" not in a.text and '<style nonce="' in a.text


def test_an_unknown_theme_preview_is_404(client, auth_off):
    assert client.get("/tools/meeting-summary/theme-preview/nope").status_code == 404


# ------------------------------------------------------------------ 匯出的 JSON 傳回來呈現
# 2026-10-02 使用者問「匯出過的 .json 可以再傳回來在這工具裡呈現嗎」。

_VTT = ("WEBVTT\n\n00:00:01.000 --> 00:00:06.000\n<v 王小明>今天談預算。\n\n"
        "00:00:06.500 --> 00:00:12.000\n<v Bianca>預算維持原案。\n\n"
        "00:00:12.500 --> 00:00:18.000\n<v 王小明>我把報價寄給 Acme-Kevin。\n").encode()


def _analysed(client) -> str:
    """上傳一份逐字稿，直接放一份分析結果（不必真的跑模型）。"""
    uid = client.post("/tools/meeting-summary/upload",
                      files={"file": ("m.vtt", io.BytesIO(_VTT), "text/vtt")}).json()["upload_id"]
    r = _router()
    out = _analysis() | {"context": "第四季預算會議", "source": {"filename": "週會.txt"}}
    r._out_path(uid).write_text(__import__("json").dumps(out, ensure_ascii=False),
                                encoding="utf-8")
    return uid


def _reupload(client, payload: bytes, name="會議摘要.json"):
    return client.post("/tools/meeting-summary/upload",
                       files={"file": (name, io.BytesIO(payload), "application/json")})


def test_an_exported_json_comes_back_as_the_same_result(client, auth_off):
    import json
    uid = _analysed(client)
    exp = client.get(f"/tools/meeting-summary/download/{uid}?fmt=json")
    assert exp.status_code == 200
    data = exp.json()
    assert data.get("format") == _router().EXPORT_FORMAT
    assert len(data.get("segments") or []) == 3, "匯出的 JSON 沒有附逐字稿 —— 傳回來引用點不到原文"

    d = _reupload(client, exp.content).json()
    assert d.get("imported") is True and d.get("has_transcript") is True, d
    new = d["upload_id"]
    assert new != uid
    res = client.get(f"/tools/meeting-summary/result/{new}").json()
    assert res["items"] == json.loads(json.dumps(data["items"])), "傳回來的項目跟匯出的不一樣"
    assert res.get("context") == "第四季預算會議"
    segs = client.get(f"/tools/meeting-summary/segments/{new}").json()["segments"]
    assert [s["text"] for s in segs] == [s["text"] for s in data["segments"]]
    # 匯出的文件照樣做得出來（下游讀得進去）
    assert client.get(f"/tools/meeting-summary/download/{new}?fmt=md").status_code == 200


def test_an_old_export_without_the_transcript_still_opens(client, auth_off):
    """舊版匯出只有結果、沒有逐字稿 —— 照樣讀得進來，只是引用沒有原文可以跳。"""
    import json
    d = _reupload(client, json.dumps(_analysis(), ensure_ascii=False).encode()).json()
    assert d.get("imported") is True and d.get("has_transcript") is False, d
    res = client.get(f"/tools/meeting-summary/result/{d['upload_id']}").json()
    assert res["items"]["actions"][0]["owner"] == "Bianca"


def test_a_tampered_export_is_400_not_stored(client, auth_off):
    import json
    r = _router()
    bad = {"format": r.EXPORT_FORMAT, "summary": {"text": "x"}, "items": "不是字典"}
    resp = _reupload(client, json.dumps(bad).encode())
    assert resp.status_code == 400, resp.text
    # 欄位的形狀被改過（標籤不是字、段號不是整數）：那一條丟掉，不可以原樣存起來 ——
    # 下游多半會把它轉成字串照畫，畫面上就出現 `[object Object]`
    worse = _analysis() | {"format": r.EXPORT_FORMAT,
                           "mindmap": [{"node_id": "c1", "parent_id": None, "label": {"x": 1},
                                        "type": "topic", "segment_ids": [1]},
                                       {"node_id": "c2", "parent_id": None, "label": "時程",
                                        "type": "topic", "segment_ids": [3, "x", True]}]}
    worse["items"]["decisions"].append({"text": ["不是字"], "segment_ids": [1]})
    d = _reupload(client, json.dumps(worse, ensure_ascii=False).encode())
    assert d.status_code == 200, d.text
    res = client.get(f"/tools/meeting-summary/result/{d.json()['upload_id']}").json()
    assert [n["label"] for n in res["mindmap"]] == ["時程"], res["mindmap"]
    assert res["mindmap"][0]["segment_ids"] == [3]
    assert [it["text"] for it in res["items"]["decisions"]] == ["預算維持原案"]


def test_unknown_fields_are_not_stored(client, auth_off):
    """匯入檔是使用者傳上來的：只留認得的欄位，字串限長。"""
    import json
    r = _router()
    payload = _analysis() | {"format": r.EXPORT_FORMAT, "evil": "x" * 10,
                             "summary": {"text": "長" * (r._MAX_IMPORT_STR + 500)}}
    d = _reupload(client, json.dumps(payload, ensure_ascii=False).encode()).json()
    res = client.get(f"/tools/meeting-summary/result/{d['upload_id']}").json()
    assert "evil" not in res
    assert len(res["summary"]["text"]) == r._MAX_IMPORT_STR


def test_a_transcript_json_is_still_read_as_a_transcript(client, auth_off):
    """逐字稿的 JSON 也有 `segments` —— 沒有標記、沒有結果欄位的，照舊當逐字稿解析。"""
    import json
    rows = {"segments": [{"text": "大家好", "speaker": "S1", "start_ms": 0, "end_ms": 900},
                         {"text": "開始吧", "speaker": "S2", "start_ms": 1000, "end_ms": 1800}]}
    d = _reupload(client, json.dumps(rows, ensure_ascii=False).encode(), "t.json").json()
    assert not d.get("imported") and d["segments"] == 2, d


def test_segments_endpoint_carries_the_parse_summary(client, auth_off):
    """從「我的作業」打開時，解析區（連同「開始分析」）要靠這份摘要畫回來。"""
    uid = client.post("/tools/meeting-summary/upload",
                      files={"file": ("m.vtt", io.BytesIO(_VTT), "text/vtt")}).json()["upload_id"]
    info = client.get(f"/tools/meeting-summary/segments/{uid}").json()["info"]
    assert info["upload_id"] == uid and info["segments"] == 3
    assert set(info["speakers"]) == {"王小明", "Bianca"} and info["has_times"] is True
    assert info["shape"] == "cues"


# ------------------------------------------------------------------ 會議背景跟著匯出
# 2026-10-02 使用者要求：「會議摘要存 json 時 或工作區時 也要把會議背景存進去」。
# 收到文件的人要知道這份記錄是在什麼前提下整理的（誰是主管、代號指什麼）。
# 所有文件格式與「存至工作區」都由 `_md` 產生，所以判準落在 `_md` 的產出上，
# 另外各驗一次 Word / ODF 與網頁下載（存至工作區走的就是那支下載）。

_CTX = "會議主題：第四季預算\n與會者\n王小明：財務部經理 | *會議主席*\n# 不是標題\n\n李美華：法務專員"


def _md_of(ctx, **kw) -> str:
    out = _analysis()
    if ctx is not None:
        out["context"] = ctx
    return _router()._md(out, charts=False, segments=_segments(), **kw)


def test_the_background_comes_after_the_title_and_before_the_summary():
    md = _md_of(_CTX)
    lines = md.splitlines()
    assert lines[0].startswith("# "), lines[:3]
    assert "## 會議背景" in lines and "## 摘要" in lines, md[:400]
    assert lines.index("## 會議背景") < lines.index("## 摘要"), "會議背景要在摘要前面"
    assert "第四季預算" in md and "法務專員" in md


@pytest.mark.parametrize("ctx", [None, "", "   \n  ", 123, ["x"]])
def test_no_background_section_when_there_is_none(ctx):
    """沒填就不出現這一節 —— 空的標題比沒有更糟（看起來像資料掉了）。"""
    assert "會議背景" not in _md_of(ctx)


def test_the_background_is_kept_verbatim_not_read_as_markdown():
    """原文照錄：分行要留著、`|` `*` `#` 不可以變成表格 / 粗體 / 標題。"""
    from markdown_it import MarkdownIt
    md = _md_of(_CTX)
    html = MarkdownIt("commonmark").render(md)
    sec = html.split("<h2>會議背景</h2>", 1)[1].split("<h2>", 1)[0]
    assert "*會議主席*" in sec and "<em>" not in sec, sec
    assert "<h1>不是標題" not in sec and "# 不是標題" in sec, sec
    assert "<table" not in sec, sec
    # 同一段裡的每一行各自一行（硬換行），空行分段
    assert sec.count("<br") >= 3, f"分行被併成一行：{sec}"
    assert sec.count("<p>") == 2, sec


@pytest.mark.parametrize("fmt", ["docx", "odt"])
def test_the_background_is_in_word_and_odf_too(fmt):
    from app.core import office_convert
    if not office_convert.find_soffice():
        pytest.skip("這台機器沒有 Office 引擎")
    out = _analysis() | {"context": _CTX}
    data = _router()._report_doc(out, "週會", fmt, segments=_segments())[0]
    z = zipfile.ZipFile(io.BytesIO(data))
    raw = z.read("word/document.xml" if fmt == "docx" else "content.xml").decode("utf-8")
    text = re.sub(r"<[^>]+>", "", raw)
    assert "會議背景" in text and "李美華：法務專員" in text, f"{fmt} 裡沒有會議背景"
    assert text.index("會議背景") < text.index("摘要"), "會議背景要在摘要前面"


def test_downloads_and_the_json_export_carry_the_background(client, auth_off):
    """網頁下載（存至工作區也是這支）與 JSON 匯出都要有背景。"""
    uid = _analysed(client)
    md = client.get(f"/tools/meeting-summary/download/{uid}?fmt=md").text
    assert "## 會議背景" in md and "第四季預算會議" in md, md[:300]
    data = client.get(f"/tools/meeting-summary/download/{uid}?fmt=json").json()
    assert data.get("context") == "第四季預算會議"


def test_the_api_result_carries_the_background(client, auth_off, monkeypatch):
    """同步 API 的結果也要寫 `context`（跟網頁那條一致）；沒送就沒有這個欄位。"""
    import json
    from app.core import llm_settings as ls

    class FakeClient:
        def text_query(self, prompt, model=None, **kw):
            if "你是會議記錄整理員" in prompt:
                return json.dumps({"decisions": [{"text": "預算維持原案",
                                                  "segment_ids": [2]}],
                                   "actions": [], "risks": [], "questions": []},
                                  ensure_ascii=False)
            if "切成" in prompt and "章節" in prompt:
                return json.dumps({"chapters": [{"title": "預算", "start_seq": 1,
                                                 "end_seq": 3}]}, ensure_ascii=False)
            if "三到五句" in prompt:
                return json.dumps({"summary": "會議確認預算維持原案。"}, ensure_ascii=False)
            return json.dumps({"keep": [1], "drop": [], "split": []})

    monkeypatch.setattr(ls.llm_settings, "is_enabled", lambda: True)
    monkeypatch.setattr(ls.llm_settings, "make_client", lambda *a, **k: FakeClient())
    monkeypatch.setattr(ls.llm_settings, "get_model_for", lambda _t: "fake")
    url = "/tools/meeting-summary/api/meeting-summary"
    r = client.post(url, files={"file": ("m.vtt", io.BytesIO(_VTT), "text/vtt")},
                    data={"context": "  會議主題：第四季預算\n王小明：財務部經理  "})
    assert r.status_code == 200, r.text
    assert r.json().get("context") == "會議主題：第四季預算\n王小明：財務部經理"
    r2 = client.post(url, files={"file": ("m.vtt", io.BytesIO(_VTT), "text/vtt")})
    assert r2.status_code == 200 and "context" not in r2.json()
