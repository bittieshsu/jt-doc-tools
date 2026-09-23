"""會議摘要工具的端點。

**這支工具唯一不可退讓的判準**：每一條決議 / 待辦 / 風險都要指得回原文那一段。
會議記錄會被拿去當依據，一條沒有出處的決議比沒有那條更糟。
"""
from __future__ import annotations

import io
import json

import pytest

VTT = """WEBVTT

00:00:01.000 --> 00:00:06.000
<v 王小明>各位早，今天只談一件事：第四季的預算。

00:00:06.500 --> 00:00:12.000
<v 李美華>我看過草案了，行銷那一塊超出去兩百萬。

00:00:12.500 --> 00:00:18.000
<v 王小明>那就照原案走，行銷不加。李美華月底前把修訂版寄給法務。
"""


def _upload(client, data=VTT.encode(), name="meeting.vtt"):
    return client.post("/tools/meeting-summary/upload",
                       files={"file": (name, io.BytesIO(data), "text/vtt")})


# ------------------------------------------------------------------ 上傳

def test_upload_returns_a_preview_before_any_llm_work(client, auth_off):
    """**先看解析結果再花那幾分鐘** —— 發言者判錯要在開始之前就看得出來。"""
    r = _upload(client)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["segments"] >= 2
    assert d["speakers"] == ["李美華", "王小明"] or set(d["speakers"]) == {"王小明", "李美華"}
    assert d["has_times"] is True
    assert d["duration_ms"] == 18000
    assert d["preview"], "要給前幾段讓使用者確認"
    assert len(d["preview"]) <= 8


def test_a_transcript_we_cannot_read_is_400_not_500(client, auth_off):
    """使用者送錯東西不是伺服器壞了。"""
    r = client.post("/tools/meeting-summary/upload",
                    files={"file": ("x.pdf", io.BytesIO(b"%PDF-1.4"), "application/pdf")})
    assert r.status_code == 400
    assert ".vtt" in r.json()["detail"], "訊息要說得出支援哪些格式"


def test_an_empty_file_is_400(client, auth_off):
    r = client.post("/tools/meeting-summary/upload",
                    files={"file": ("x.txt", io.BytesIO(b""), "text/plain")})
    assert r.status_code == 400


def test_plain_text_without_times_says_so(client, auth_off):
    """沒有時間是正常情況 —— 要讓使用者事先知道哪些東西不會出現。"""
    r = _upload(client, "第一句\n第二句\n第三句\n".encode(), "a.txt")
    assert r.status_code == 200
    d = r.json()
    assert d["has_times"] is False
    assert d["duration_ms"] is None


# ------------------------------------------------------------------ 分析

def test_start_without_llm_is_503_not_500(client, auth_off):
    """缺相依 / 沒啟用是**部署問題**，不是使用者送錯東西。"""
    uid = _upload(client).json()["upload_id"]
    r = client.post("/tools/meeting-summary/start", json={"upload_id": uid})
    assert r.status_code == 503, r.text


def test_the_analysis_runs_end_to_end_and_every_citation_resolves(client, auth_off, monkeypatch):
    """端到端：**把產出打開來看**，不是看「有沒有回 200」。"""
    from app.core import llm_settings as ls

    class FakeClient:
        """假模型 —— **只驗我們自己的程式**，驗不到「模型會不會照做」。
        真的模型會不會照格式回答，要拿真的模型測（本專案記過）。"""
        def text_query(self, prompt, model=None, **kw):
            if "你是會議記錄整理員" in prompt:
                return json.dumps({
                    "decisions": [{"text": "第四季行銷預算不加",
                                   "segment_ids": [3]}],
                    "actions": [{"text": "月底前把修訂版寄給法務",
                                 "owner": "李美華", "due_text": "月底",
                                 "segment_ids": [3]}],
                    "risks": [], "questions": []
                }, ensure_ascii=False)
            if "切成" in prompt and "章節" in prompt:
                return json.dumps({"chapters": [
                    {"title": "第四季預算", "start_seq": 1, "end_seq": 3}]},
                    ensure_ascii=False)
            if "三到五句" in prompt:
                return json.dumps(
                    {"summary": "會議確認第四季行銷預算不加，修訂版月底前送法務。"},
                    ensure_ascii=False)
            # 複審：全部保留
            return json.dumps({"keep": [1, 2], "drop": [], "split": []},
                              ensure_ascii=False)

    monkeypatch.setattr(ls.llm_settings, "is_enabled", lambda: True)
    monkeypatch.setattr(ls.llm_settings, "make_client", lambda: FakeClient())
    monkeypatch.setattr(ls.llm_settings, "get_model_for", lambda _t: "fake")

    uid = _upload(client).json()["upload_id"]
    r = client.post("/tools/meeting-summary/start", json={"upload_id": uid})
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]

    from app.core.job_manager import job_manager
    import time
    for _ in range(200):
        j = job_manager.get(job_id)
        if j and j.status in ("done", "error"):
            break
        time.sleep(0.05)
    j = job_manager.get(job_id)
    assert j.status == "done", getattr(j, "error", None)
    # **判準是 `to_public()` 的 `has_result`，不是「result_path 有值」** ——
    # 放字串的話 `.exists()` 會炸，`/api/jobs/{id}` 每次都 500，
    # 而背景作業本身完全正常（結果檔真的產出來了）。只驗有沒有值抓不到。
    pub = j.to_public()
    assert pub["has_result"] is True, "「我的作業」不會有下載鈕"
    assert pub["result_filename"]

    out = client.get(f"/tools/meeting-summary/result/{uid}").json()
    segs = client.get(f"/tools/meeting-summary/segments/{uid}").json()["segments"]
    by_seq = {s["seq"]: s["text"] for s in segs}

    assert out["items"]["decisions"], "應該抽得到那條決議"
    for kind, items in out["items"].items():
        for it in items:
            ids = it.get("segment_ids") or ([it["seq"]] if it.get("seq") else [])
            assert ids, f"{kind} 有一條沒有段號 —— 沒有出處的項目不可以留"
            for n in ids:
                assert n in by_seq, f"{kind} 指到不存在的第 {n} 段"


def test_the_markdown_export_carries_the_citations(client, auth_off, monkeypatch):
    """下載的會議記錄要**帶著出處** —— 那是這份文件會被拿去用的理由。"""
    from app.core import llm_settings as ls

    class FakeClient:
        def text_query(self, prompt, model=None, **kw):
            if "你是會議記錄整理員" in prompt:
                return json.dumps({"decisions": [
                    {"text": "第四季行銷預算不加", "segment_ids": [3]}],
                    "actions": [], "risks": [], "questions": []}, ensure_ascii=False)
            if "切成" in prompt and "章節" in prompt:
                return json.dumps({"chapters": []}, ensure_ascii=False)
            if "三到五句" in prompt:
                return json.dumps({"summary": "確認第四季行銷預算不加。"},
                                  ensure_ascii=False)
            return json.dumps({"keep": [1], "drop": [], "split": []},
                              ensure_ascii=False)

    monkeypatch.setattr(ls.llm_settings, "is_enabled", lambda: True)
    monkeypatch.setattr(ls.llm_settings, "make_client", lambda: FakeClient())
    monkeypatch.setattr(ls.llm_settings, "get_model_for", lambda _t: "fake")

    uid = _upload(client).json()["upload_id"]
    job_id = client.post("/tools/meeting-summary/start",
                         json={"upload_id": uid}).json()["job_id"]
    from app.core.job_manager import job_manager
    import time
    for _ in range(200):
        j = job_manager.get(job_id)
        if j and j.status in ("done", "error"):
            break
        time.sleep(0.05)
    assert job_manager.get(job_id).status == "done"

    r = client.get(f"/tools/meeting-summary/download/{uid}?fmt=md")
    assert r.status_code == 200
    md = r.content.decode("utf-8")
    assert "## 決議" in md
    assert "第四季行銷預算不加" in md
    assert "第 3 段" in md, "匯出的會議記錄要帶著出處"


def test_an_unknown_export_format_is_400_not_500(client, auth_off, monkeypatch):
    uid = _upload(client).json()["upload_id"]
    r = client.get(f"/tools/meeting-summary/download/{uid}?fmt=pdf")
    assert r.status_code in (400, 410), r.text


def test_result_before_the_analysis_ran_is_410_not_500(client, auth_off):
    uid = _upload(client).json()["upload_id"]
    r = client.get(f"/tools/meeting-summary/result/{uid}")
    assert r.status_code == 410


def test_a_bogus_upload_id_never_reaches_the_filesystem(client, auth_off):
    for bad in ("../../etc/passwd", "..%2f..%2fetc", "zzz"):
        r = client.get(f"/tools/meeting-summary/result/{bad}")
        assert r.status_code in (400, 404, 422), f"{bad} → {r.status_code}"


# ------------------------------------------------------------------ 註冊

def test_the_tool_is_registered_and_wired_into_the_conventions():
    """新工具 SOP 的每一項漏掉都出過事。"""
    from app.tool_registry import discover_tools
    from app.core.roles import _NON_ADMIN_TOOL_IDS
    from app.core.concurrency_settings import REMOTE_TOOL_IDS, resource_tags
    from app.core.llm_settings import llm_settings
    from app.main import _TOOL_ALIASES

    ids = {t.metadata.id for t in discover_tools()}
    assert "meeting-summary" in ids
    assert "meeting-summary" in _NON_ADMIN_TOOL_IDS, "一般使用者要用得到"
    assert "meeting-summary" in REMOTE_TOOL_IDS, "會呼叫 LLM，要走外部服務那條號誌"
    assert "外部服務" in resource_tags("meeting-summary") or \
           "remote" in resource_tags("meeting-summary")
    assert "meeting-summary" in _TOOL_ALIASES, "沒有搜尋關鍵字等於找不到"
    assert any("會議" in _TOOL_ALIASES["meeting-summary"] for _ in [1])
    assert "meeting-summary" in {t["id"] for t in llm_settings.KNOWN_LLM_TOOLS}


def test_old_installs_get_the_tool_through_a_migration():
    """從舊版升上來的安裝，角色早就存在 —— 新工具不會自己長出來。"""
    from app.core import auth_db
    names = [m.__name__ for m in auth_db.MIGRATIONS]
    assert any("meeting_summary" in n for n in names), \
        "缺 backfill migration：既有安裝的一般使用者永遠看不到這支"


# ---------------------------------------------------------------------------
# 心智圖的版面：沒有子節點的章節不可以留一片空白
#
# 使用者 2026-09-19 回報「畫面右邊太空了」。真實會議的決議與待辦幾乎都集中在
# 後段，所以十個章節常常只有三個有子節點 —— 原本的兩欄版面讓另外七列的
# **右半邊整片空白**，看起來像圖畫壞了。
#
# 那些章節**確實沒有產出**（是真的資料不是缺陷），所以做法不是隱藏、
# 更不是編一個節點出來，而是換一種畫法：整寬的扁條 ＋ 右邊註明「無決議／待辦」。
#
# **判準要落在版面上（框有多寬），不是落在「有沒有畫出東西」** ——
# 只驗「畫得出 SVG」的話，退回兩欄版面照樣全綠。
# ---------------------------------------------------------------------------

def _mm_rect_widths(svg: str) -> list[int]:
    import re
    return [int(m) for m in re.findall(r'<rect x="14" y="\d+" width="(\d+)"', svg)]


def test_a_chapter_with_no_items_spans_the_full_width():
    from app.core import meeting_charts as mc

    nodes = [{"node_id": f"c{i}", "parent_id": None, "label": f"議題{i}", "type": "topic"}
             for i in range(5)]
    nodes.append({"node_id": "i0", "parent_id": "c4", "label": "這件事下週處理",
                  "type": "action", "segment_ids": [42]})
    svg = mc.mindmap(nodes)
    assert svg, "五個章節應該畫得出心智圖"

    widths = set(_mm_rect_widths(svg))
    full = 980 - 14 * 2          # width - pad*2
    assert full in widths, (
        f"沒有子節點的章節應該畫成整寬（{full}px）的扁條，實際看到的寬度是 {sorted(widths)}。"
        "留在窄欄的話右半邊會是一片空白，使用者會以為圖壞了。")
    assert "無決議／待辦" in svg, "整寬的那幾條要說得出「為什麼右邊沒有東西」"


def test_a_chapter_that_has_items_keeps_the_two_column_tree():
    """反向釘住 —— 只驗上一條的話，把每一列都畫成整寬也會過（樹就不見了）。"""
    from app.core import meeting_charts as mc

    nodes = [{"node_id": "c0", "parent_id": None, "label": "議題", "type": "topic"},
             {"node_id": "i0", "parent_id": "c0", "label": "決定這樣做",
              "type": "decision", "segment_ids": [7]},
             {"node_id": "c1", "parent_id": None, "label": "另一個議題", "type": "topic"},
             {"node_id": "i1", "parent_id": "c1", "label": "也決定這樣做",
              "type": "decision", "segment_ids": [9]}]
    svg = mc.mindmap(nodes)
    assert svg
    widths = set(_mm_rect_widths(svg))
    assert 230 in widths, (
        f"有子節點的章節要留在左欄（230px）畫成樹，實際寬度 {sorted(widths)}")
    assert 980 - 14 * 2 not in widths, "每一個章節都有子節點時，不該出現整寬的扁條"
    assert "無決議／待辦" not in svg, "有子節點就不該說它沒有決議"


# ---------------------------------------------------------------------------
# 匯出的 PDF 要是**一份文件**，不是幾張圖
#
# 使用者 2026-09-19：「網頁上的會議摘要很美 但是匯出 pdf 怎會這麼差」。
# 拿到的是三頁圖、一個字的內文都沒有，而且頁面尺寸是 980×2640 這種數字
# —— 那是圖的原始大小，不是紙張。
#
# 根因是 `convert_to_pdf(src, dst_pdf) -> None` 的簽章被用錯：第二個參數給了
# 目錄，而且把回傳值當成結果（它永遠是 `None`），所以那段**從來沒有成功過**，
# 每次都默默退回「只有圖」的版本 —— 而回 `None` 不是例外，日誌一句話都沒有。
#
# **判準落在產出的 PDF 上**（§0.5）：頁面是不是標準紙張、裡面有沒有內文。
# 只驗「有沒有下載到檔案」的話，退化版照樣全綠。
# ---------------------------------------------------------------------------

def _sample_analysis() -> dict:
    return {
        "summary": {"text": "這場會議確認了第四季預算與上線時程。"},
        "items": {"decisions": [{"text": "預算維持原案", "segment_ids": [3]}],
                  "actions": [{"text": "月底前寄修訂版", "owner": "李美華",
                               "segment_ids": [7]}],
                  "risks": [], "questions": []},
        "chapters": [{"title": "預算討論", "start_seq": 1, "end_seq": 5,
                      "segment_ids": [1, 2, 3, 4, 5], "start_ms": 0,
                      "end_ms": 300000, "duration_ms": 300000},
                     {"title": "上線時程", "start_seq": 6, "end_seq": 12,
                      "segment_ids": [6, 7, 8], "start_ms": 300000,
                      "end_ms": 600000, "duration_ms": 300000}],
        "mindmap": [{"node_id": "c1", "parent_id": None, "label": "預算討論",
                     "type": "topic", "segment_ids": [1]},
                    {"node_id": "i1", "parent_id": "c1", "label": "預算維持原案",
                     "type": "decision", "segment_ids": [3]}],
        "speaker_stats": {"王小明": {"turn_count": 5, "chars": 100,
                                    "char_pct": 60.0, "speaking_ms": 200000,
                                    "first_seq": 1},
                          "李美華": {"turn_count": 3, "chars": 66,
                                    "char_pct": 40.0, "speaking_ms": 100000,
                                    "first_seq": 2}},
        "source": {"filename": "第四季預算會議.txt", "segments": 12},
        "llm_calls": 9,
    }


def test_the_exported_pdf_is_a_document_not_just_charts():
    import importlib
    import fitz
    from app.core import office_convert

    if not office_convert.find_soffice():
        pytest.skip("這台機器沒有 Office 引擎")
    mod = importlib.import_module("app.tools.meeting_summary.router")
    data = mod._report_doc(_sample_analysis(), "第四季預算會議")[0]
    doc = fitz.open("pdf", data)

    # ① 頁面是標準紙張 —— 退化版的頁面是圖的原始大小（980×2640 那種）
    w, h = doc[0].rect.width, doc[0].rect.height
    assert 500 < w < 650 and 700 < h < 900, (
        f"第一頁是 {w:.0f}×{h:.0f} —— 那是圖的尺寸不是紙張，"
        "代表又退回「只有圖」的版本了")

    # ② 裡面真的有內文（摘要、決議那幾段字）
    text = "".join(p.get_text() for p in doc)
    for want in ("會議記錄", "摘要", "決議", "預算維持原案"):
        assert want in text, f"匯出的 PDF 裡找不到「{want}」—— 只有圖沒有內文"

    # ③ 標題不要帶副檔名（那是要發出去給人看的文件標題）
    assert ".txt 會議記錄" not in text, "文件標題帶著副檔名"


def test_the_markdown_title_drops_the_extension():
    """不需要 Office 引擎也驗得到的那一半。"""
    import importlib
    mod = importlib.import_module("app.tools.meeting_summary.router")
    md = mod._md(_sample_analysis(), charts=False, embed=False)
    assert md.startswith("# 第四季預算會議 會議記錄"), md.splitlines()[0]


def test_no_chart_in_the_exported_pdf_runs_off_the_page():
    """圖不可以超出版面（2026-09-19 使用者回報「pdf，圖超過範圍」）。

    兩個都是量出來的：

    * soffice **不理 CSS 的 `max-width`**（實測 980px 的圖照樣放 980px，
      右邊整片被切掉），要用 HTML 的 `width` 屬性。
      跟表格框線同一課：**HTML 4 時代的實作，CSS 走不通時試表現屬性。**
    * **光限寬還不夠** —— 心智圖縮到頁寬之後仍然比一頁高，
      而 soffice 不會自己分頁，它就放一張、超出的部分裁掉
      （實測圖底 849pt > 頁高 792pt）。太高的要自己切片。

    **判準是每一張圖的外框都落在頁面裡**，不是「有沒有加 width 屬性」——
    加了但算錯一樣會超出去。
    """
    import importlib
    import fitz
    from app.core import office_convert

    if not office_convert.find_soffice():
        pytest.skip("這台機器沒有 Office 引擎")

    # 造一份夠大的會議，心智圖才會高到需要切片
    nodes, chaps = [], []
    items = {"decisions": [], "actions": [], "risks": [], "questions": []}
    for i in range(11):
        nodes.append({"node_id": f"c{i}", "parent_id": None,
                      "label": f"第{i + 1}個議題與相關爭議", "type": "topic",
                      "segment_ids": [i * 12 + 1]})
        chaps.append({"title": f"第{i + 1}個議題與相關爭議",
                      "start_seq": i * 12 + 1, "end_seq": i * 12 + 12,
                      "segment_ids": list(range(i * 12 + 1, i * 12 + 13)),
                      "start_ms": i * 300000, "end_ms": (i + 1) * 300000,
                      "duration_ms": 300000})
        for j in range(3):
            nodes.append({"node_id": f"i{i}_{j}", "parent_id": f"c{i}",
                          "label": f"第{i + 1}個議題底下的第{j + 1}條決議，內容要夠長",
                          "type": "decision", "segment_ids": [i * 12 + j + 2]})
            items["decisions"].append({"text": f"議題{i + 1}決議{j + 1}",
                                       "segment_ids": [i * 12 + j + 2]})
    out = {"summary": {"text": "測試摘要。"}, "items": items, "chapters": chaps,
           "mindmap": nodes,
           "speaker_stats": {f"與會者{k}": {"turn_count": 5 + k, "chars": 100 * (k + 1),
                                           "char_pct": 20.0,
                                           "speaking_ms": 100000 * (k + 1),
                                           "first_seq": k + 1} for k in range(5)},
           "source": {"filename": "大型會議.txt", "segments": 132}, "llm_calls": 30}

    mod = importlib.import_module("app.tools.meeting_summary.router")
    doc = fitz.open("pdf", mod._report_doc(out, "大型會議")[0])
    over = []
    total = 0
    for i, page in enumerate(doc, 1):
        for im in page.get_image_info():
            total += 1
            x0, y0, x1, y1 = im["bbox"]
            if x1 > page.rect.width + 1 or y1 > page.rect.height + 1:
                over.append(f"第{i}頁 右{x1:.0f} 底{y1:.0f}"
                            f"（頁 {page.rect.width:.0f}x{page.rect.height:.0f}）")
    assert total > 0, "匯出的 PDF 裡一張圖都沒有 —— 判準失效了"
    assert not over, "這幾張圖超出版面：\n" + "\n".join(over)


def test_the_chart_headings_are_not_doubled():
    """圖自己畫著標題，Markdown 不要再加一個 `##`（2026-09-19 使用者截圖）。"""
    import importlib
    mod = importlib.import_module("app.tools.meeting_summary.router")
    md = mod._md(_sample_analysis(), charts=True, embed=False)
    assert "## 各議題佔多少時間" not in md, (
        "圖的標題被寫了兩次（一次在 Markdown 的 `##`、一次畫在圖裡）")


def test_the_exported_chart_matches_what_the_page_shows():
    """匯出的圖要跟畫面上看到的是同一件事（2026-09-19 使用者回報）。

    畫面把發言者長條圖併進表格、換成「發言分布」之後，**匯出用的是伺服器畫的
    另一份** —— 於是網頁上是分布、PDF 裡還是舊的長條圖，連未標示的發言者都
    還顯示 `unknown`。**同一份東西寫在兩個地方一定會漂**，這次漂在
    「畫面」與「匯出」之間。

    判準兩條：
    * 有逐段資料時畫的是**時間軸**（跟畫面同一件事），不是長條圖
    * `unknown` 只能留在資料裡，**畫面與匯出都不可以顯示它**
    """
    from app.core import meeting_charts as mc
    from app.core import meeting_insight as mi

    who = ["林冠宇", "趙明哲", None]      # None → 代號 unknown
    segs = [{"seq": i + 1, "speaker": who[i % 3], "text": f"第{i + 1}句",
             "start_ms": i * 20000, "end_ms": i * 20000 + 15000}
            for i in range(30)]
    stats = mi.speaker_stats(segs)
    out = {"summary": {"text": "測試。"},
           "items": {k: [] for k in ("decisions", "actions", "risks", "questions")},
           "chapters": [], "mindmap": [], "speaker_stats": stats,
           "source": {"filename": "t.txt", "segments": len(segs)}, "llm_calls": 1}

    charts = mc.build_all(out, segs)
    svg = charts.get("speaker_share")
    assert svg, "有逐段資料時應該畫得出發言者那張圖"
    assert "誰在什麼時候講話" in svg, (
        "匯出的發言者圖還是舊的長條圖 —— 畫面上早就換成發言分布了")
    assert "unknown" not in svg, "圖上顯示了 `unknown` 這個代號"
    assert mc.UNLABELLED in svg, "未標示的發言者沒有換成看得懂的字"

    # 沒有逐段資料（公開 API 那條路）仍然要畫得出東西 —— 退回長條圖
    fallback = mc.build_all(out)
    assert fallback.get("speaker_share"), "沒有逐段資料時應該退回長條圖"
    assert "各發言者佔多少" in fallback["speaker_share"]
    assert "unknown" not in fallback["speaker_share"]


def test_the_exported_table_does_not_show_the_placeholder_speaker():
    import importlib
    from app.core import meeting_insight as mi
    mod = importlib.import_module("app.tools.meeting_summary.router")
    who = ["甲", "乙", None]
    segs = [{"seq": i + 1, "speaker": who[i % 3], "text": f"第{i + 1}句"}
            for i in range(12)]
    out = {"summary": {"text": "x"},
           "items": {k: [] for k in ("decisions", "actions", "risks", "questions")},
           "chapters": [], "mindmap": [], "speaker_stats": mi.speaker_stats(segs),
           "source": {"filename": "t.txt", "segments": 12}, "llm_calls": 1}
    md = mod._md(out, charts=False, embed=False, segments=segs)
    assert "unknown" not in md, "匯出的表格裡出現了 `unknown` 這個代號"
    assert "未標示發言者" in md


@pytest.mark.parametrize("fmt", ["pdf", "docx", "odt"])
def test_the_report_exports_in_every_document_format(fmt):
    """PDF / Word / ODF 三種都要匯得出**有內容**的文件（2026-09-19 使用者要求）。

    判準是**打開產出看裡面有沒有那幾段字**（§0.5），不是「有沒有拿到檔案」
    —— 這支工具的 PDF 匯出就曾經連續好幾版都只產出幾張圖，而下載永遠是 200。

    **ODT 另外驗 mimetype**：HTML 輸入若沒指定 Writer 篩選器，soffice 會走
    Writer/Web，產出的 `.odt` mimetype 變成 `…text-web`，使用者開檔看到的是
    網頁排版（v1.11.36 踩過）。
    """
    import importlib
    import io
    import re
    import zipfile
    from app.core import office_convert

    if not office_convert.find_soffice():
        pytest.skip("這台機器沒有 Office 引擎")
    mod = importlib.import_module("app.tools.meeting_summary.router")
    data, media, ext = mod._report_doc(_sample_analysis(), "第四季預算會議", fmt)
    assert data and len(data) > 2000, f"{fmt} 產出太小（{len(data)} bytes）"
    assert ext == "." + fmt and media

    if fmt == "pdf":
        import fitz
        doc = fitz.open("pdf", data)
        text = "".join(p.get_text() for p in doc)
    else:
        z = zipfile.ZipFile(io.BytesIO(data))
        if fmt == "odt":
            mt = z.read("mimetype").decode()
            assert mt == "application/vnd.oasis.opendocument.text", (
                f"ODT 的 mimetype 是 {mt} —— 走到 Writer/Web 了，"
                "使用者開檔會看到網頁排版")
            raw = z.read("content.xml")
        else:
            main = [n for n in z.namelist() if n.startswith("word/document")][0]
            raw = z.read(main)
        text = re.sub(r"<[^>]+>", "", raw.decode("utf-8", "replace"))

    for want in ("摘要", "決議", "預算維持原案"):
        assert want in text, f"{fmt} 裡找不到「{want}」—— 匯出的是空殼"


def test_the_theme_actually_changes_the_output():
    """版面主題要真的有作用 —— 選了卻沒變，使用者看不出來。"""
    import importlib
    from app.core import office_convert

    if not office_convert.find_soffice():
        pytest.skip("這台機器沒有 Office 引擎")
    mod = importlib.import_module("app.tools.meeting_summary.router")
    a, _m, _e = mod._report_doc(_sample_analysis(), "t", "pdf", "report")
    b, _m, _e = mod._report_doc(_sample_analysis(), "t", "pdf", "mono")
    assert a != b, "換了主題但產出一模一樣 —— 主題沒有被套用"

    # 認不得的主題要退回預設，不可以整個失敗
    c, _m, _e = mod._report_doc(_sample_analysis(), "t", "pdf", "不存在的主題")
    assert c and len(c) > 2000


def test_the_theme_list_comes_from_the_markdown_tool():
    """主題清單只有一份 —— 那邊加主題時這裡要跟著有。"""
    import importlib
    from app.tools.markdown_to_doc import themes as th
    mod = importlib.import_module("app.tools.meeting_summary.router")
    got = dict(mod._doc_themes())
    assert set(got) == set(th.THEMES), (
        "主題清單跟「Markdown 轉辦公文件」對不起來 —— 有人自己抄了一份")


# ---------------------------------------------------------------------------
# 文件標題：貼上的逐字稿不可以把系統自己塞的檔名當成主題
# ---------------------------------------------------------------------------

def _out_with(context: str = "", filename: str = "會議.txt") -> dict:
    return {"summary": {"text": "x"},
            "items": {k: [] for k in ("decisions", "actions", "risks", "questions")},
            "chapters": [], "mindmap": [], "speaker_stats": {},
            "context": context,
            "source": {"filename": filename, "segments": 1}, "llm_calls": 1}


def test_the_title_comes_from_the_background_when_the_text_was_pasted():
    """貼上逐字稿時，標題要從**會議背景寫的主題**來（2026-09-19 使用者回報）。

    貼上時的檔名是我們自己塞的「貼上的逐字稿.txt」——
    拿它當標題會變成「貼上的逐字稿 會議記錄」，那是**系統的內部說法**
    出現在要發出去的文件上。
    """
    import importlib
    mod = importlib.import_module("app.tools.meeting_summary.router")

    ctx = ("公司重要電商基礎架構營運與維運會議逐字稿\n\n"
           "會議主題：電商平台營運穩定度、重大事故檢討與大促容量整備\n"
           "會議日期：2026 年 9 月 19 日\n")
    got = mod.meeting_title(_out_with(ctx, "貼上的逐字稿.txt"))
    assert got == "電商平台營運穩定度、重大事故檢討與大促容量整備 會議記錄", got
    assert "貼上的逐字稿" not in got


def test_the_title_falls_back_to_the_filename_then_to_a_plain_one():
    import importlib
    mod = importlib.import_module("app.tools.meeting_summary.router")
    # 有真的檔名就用檔名（而且不帶副檔名）
    assert mod.meeting_title(_out_with("", "第四季預算會議.txt")) == "第四季預算會議 會議記錄"
    # 貼上、又沒填背景 —— **不要編一個主題出來**
    assert mod.meeting_title(_out_with("", "貼上的逐字稿.txt")) == "會議記錄"


def test_pasted_is_decided_by_the_flag_not_by_the_filename():
    """判準是 `pasted` 旗標，**不是檔名**。

    貼上時前端塞的檔名**使用者看得到**（作業名稱、通知），所以它會照介面
    語言翻。拿翻過的檔名去比字串的話，英 / 日介面下那個比對永遠不成立 ——
    標題就變成「Pasted transcript 會議記錄」，而畫面上完全看不出哪裡錯了
    （本專案記過的「翻掉一個拿去比較的字串」那一類）。

    舊資料的退路也要留著：這個旗標是 v1.16.6 才加的，在那之前存下來的
    meta 沒有這個欄位，只能靠檔名認。
    """
    import importlib
    mod = importlib.import_module("app.tools.meeting_summary.router")

    # 檔名翻成英文了，但旗標說「這是貼上的」→ 不可以拿檔名當標題
    out = _out_with("", "Pasted transcript.txt")
    out["source"]["pasted"] = True
    assert mod.meeting_title(out) == "會議記錄"

    # 反過來：使用者真的上傳了一個叫這個名字的檔案，旗標是 False
    # → 那就是他的檔名，要用
    out2 = _out_with("", "Pasted transcript.txt")
    out2["source"]["pasted"] = False
    assert mod.meeting_title(out2) == "Pasted transcript 會議記錄"

    # 舊資料（沒有旗標）仍然靠檔名認得出來
    assert mod.meeting_title(_out_with("", "貼上的逐字稿.txt")) == "會議記錄"


def test_the_title_does_not_guess_from_the_summary():
    """**不從摘要猜主題** —— 截前幾個字會斷在半句，而且每次跑的講法都不一樣。"""
    import importlib
    mod = importlib.import_module("app.tools.meeting_summary.router")
    out = _out_with("", "貼上的逐字稿.txt")
    out["summary"] = {"text": "這場會議確認了第四季預算與上線時程，並決定把驗收往後挪兩週。"}
    assert mod.meeting_title(out) == "會議記錄", "從摘要截字當標題了"


def test_office_formats_do_not_leave_white_text_on_white():
    """`.odt` / `.docx` 裡不可以有白字（2026-09-19 使用者回報「標題字是白色」）。

    soffice 的 HTML 匯入**保留文字顏色、但丟掉段落底色** ——
    `report` 主題的標題是「白字 ＋ 深色橫幅」，底色一掉就整行看不見。
    PDF 沒這問題（底色畫得出來），所以只有文件格式疊覆寫。

    **判準是產出的樣式裡有沒有白字**，不是「有沒有套覆寫」——
    覆寫寫錯地方一樣會留下白字。
    """
    import importlib
    import io
    import re
    import zipfile
    from app.core import office_convert

    if not office_convert.find_soffice():
        pytest.skip("這台機器沒有 Office 引擎")
    mod = importlib.import_module("app.tools.meeting_summary.router")
    for fmt in ("odt", "docx"):
        data, _m, _e = mod._report_doc(_sample_analysis(), "t", fmt, "report")
        z = zipfile.ZipFile(io.BytesIO(data))
        blob = "".join(z.read(n).decode("utf-8", "replace")
                       for n in z.namelist()
                       if n.endswith(".xml") and ("styles" in n or "document" in n))
        white = re.findall(r'(?:fo:color|w:color w:val)="#?[fF]{6}"', blob)
        assert not white, (
            f"{fmt} 裡有 {len(white)} 個白字樣式 —— 底色在轉檔時會掉，"
            "白字白底整行看不見")


def test_renaming_a_speaker_rewrites_the_transcript_and_the_stats(client, auth_off):
    """**改名字要一次改完**：逐字稿、發言統計、下載的內容走的是同一份資料。

    統計是**以代號當鍵**的 —— 不一起搬的話，圖上還是 `S1` 而逐字稿已經是人名，
    同一個畫面上兩套名字（本專案「同一份東西寫在兩個地方」那一族）。

    **`seq` 一個都不能動** —— 決議與待辦的引用綁的是 `seq`，
    改名字不可以讓任何一條引用失效。
    """
    import importlib
    import json
    # **不可以 `from ... import router`** —— 套件的 `__init__` 做過
    # `from .router import router`，那個名字把同名的子模組遮住了，
    # 拿到的會是 `APIRouter` 物件（CLAUDE.md 記過同一課）。
    ms = importlib.import_module("app.tools.meeting_summary.router")

    uid = _upload(client).json()["upload_id"]
    segs = [{"seq": 1, "speaker": "S1", "text": "第一句"},
            {"seq": 2, "speaker": "S2", "text": "第二句"},
            {"seq": 3, "speaker": "S1", "text": "第三句"}]
    ms._seg_path(uid).write_text(json.dumps(segs, ensure_ascii=False), encoding="utf-8")
    ms._out_path(uid).write_text(json.dumps(
        {"speaker_stats": {"S1": {"chars": 6}, "S2": {"chars": 3}}},
        ensure_ascii=False), encoding="utf-8")

    r = client.post(f"/tools/meeting-summary/speakers/{uid}",
                    json={"map": {"S1": "王小明"}, "overrides": {"2": "臨時來賓"}})
    assert r.status_code == 200, r.text

    after = json.loads(ms._seg_path(uid).read_text(encoding="utf-8"))
    assert [s["speaker"] for s in after] == ["王小明", "臨時來賓", "王小明"]
    assert [s["seq"] for s in after] == [1, 2, 3], "seq 被動到了 —— 引用會全部失效"

    stats = json.loads(ms._out_path(uid).read_text(encoding="utf-8"))["speaker_stats"]
    assert "王小明" in stats and "S1" not in stats, (
        "統計還掛在舊代號上 —— 圖上會是 S1 而逐字稿已經是人名")
    assert stats["王小明"] == {"chars": 6}, "搬鍵的時候把值弄丟了"


def test_a_speaker_name_cannot_smuggle_newlines(client, auth_off):
    """名字會被寫進逐字稿與下載的檔案 —— 換行會把一段拆成兩段。"""
    import importlib
    import json
    ms = importlib.import_module("app.tools.meeting_summary.router")

    uid = _upload(client).json()["upload_id"]
    ms._seg_path(uid).write_text(json.dumps([{"seq": 1, "speaker": "S1", "text": "x"}]),
                                 encoding="utf-8")
    client.post(f"/tools/meeting-summary/speakers/{uid}",
                json={"map": {"S1": "壞\n人\r\x00"}})
    got = json.loads(ms._seg_path(uid).read_text(encoding="utf-8"))[0]["speaker"]
    assert "\n" not in got and "\r" not in got and "\x00" not in got, got
