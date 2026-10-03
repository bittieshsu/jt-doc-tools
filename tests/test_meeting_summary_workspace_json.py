"""從工作區載入「轉逐字稿轉送過來的那個檔」（2026-10-02 使用者回報）。

轉逐字稿按「轉送會議摘要」時把整份結果的 JSON 經工作區中轉；工作區只收 `.txt` / `.md`
兩種文字檔名，存進去就變成 `…-逐字稿.txt`。之後從工作區手動載入那個檔，會議摘要照
副檔名當純文字切 —— 畫面上是一行 JSON 一段、0 位發言者、沒有時間。

這裡走一次真的路徑：存進工作區 → 從工作區取回（帶著工作區給的名字）→ 送進會議摘要。
"""
from __future__ import annotations

import io
import json

#: 「會議錄音轉逐字稿」結果的形狀（轉送時整份送出）
TRANSCRIBE_RESULT = {
    "source": {"filename": "錄音.mp3", "size_bytes": 1000},
    "summary": {"correction_level": "punctuation_only"},
    "segments": [
        {"seq": 1, "text": "各位早，今天要談三件事。", "speaker": "S1",
         "start_ms": 1000, "end_ms": 4000},
        {"seq": 2, "text": "預算我看過了。", "speaker": "S2",
         "start_ms": 4200, "end_ms": 7000},
        {"seq": 3, "text": "那就照原案走。", "speaker": "S1",
         "start_ms": 7500, "end_ms": 9000},
    ],
    "speaker_names": {"S1": "王小明"},
}


import pytest  # noqa: E402


@pytest.mark.parametrize("saved_as", ["錄音-逐字稿.txt", "錄音-逐字稿.json"])
def test_a_transcript_json_from_the_workspace_keeps_its_speakers_and_times(client, auth_off, saved_as):
    """`.txt` ＝ v1.16.42 以前存進工作區的舊檔（工作區把 JSON 改名成 `.txt`，使用者工作區裡還留著）；
    `.json` ＝ 現在存進去的（工作區認得 JSON 了，名字照舊）。兩種都要讀得對。"""
    data = json.dumps(TRANSCRIBE_RESULT, ensure_ascii=False).encode("utf-8")
    r = client.post("/workspace/save",
                    data={"name": saved_as, "source_tool": "meeting-transcribe"},
                    files={"file": (saved_as, io.BytesIO(data), "application/json")})
    assert r.status_code == 200, r.text
    fid = r.json()["file"]["file_id"]
    meta = next(f for f in client.get("/workspace/api/list").json()["files"]
                if f["file_id"] == fid)
    # 前提：工作區照存進去的名字留著（舊檔是 `.txt`、新檔是 `.json`）
    assert meta["name"] == saved_as, meta["name"]

    body = client.get(f"/workspace/file/{fid}").content
    r = client.post("/tools/meeting-summary/upload",
                    files={"file": (meta["name"], io.BytesIO(body), "text/plain")})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["segments"] == len(TRANSCRIBE_RESULT["segments"]), d["segments"]
    assert d["speakers"] == ["S2", "王小明"], d["speakers"]
    assert d["has_times"] is True
    client.post("/workspace/delete", data={"file_id": fid})


# ------------------------------------------------------------------ 先前的分析結果存在工作區裡
# 2026-10-03 使用者到工作區找已經完成的摘要：作業完成時使用者已經離開頁面，結果自動存進
# 工作區，名字是 `…-會議摘要.txt`（工作區只收 `.txt` / `.md`）。之後在會議摘要按
# 「從工作區載入」挑它 —— 原本只認 `.json` 的匯出檔，`.txt` 被當成逐字稿解析。

from tests.test_meeting_summary_export_contents import _analysis  # noqa: E402

_VTT = ("WEBVTT\n\n00:00:01.000 --> 00:00:06.000\n<v 王小明>今天談預算。\n\n"
        "00:00:06.500 --> 00:00:12.000\n<v Bianca>預算維持原案。\n\n"
        "00:00:12.500 --> 00:00:18.000\n<v 王小明>我把報價寄給 Acme-Kevin。\n").encode()


def _through_the_workspace(client, payload: bytes, name: str, tool: str):
    """存進工作區、再照工作區給的名字取回來送進會議摘要（就是「從工作區載入」那條路）。
    `name` 用 `.txt` 模擬 v1.16.42 以前存進去的舊檔（工作區當時把 JSON 改名成 `.txt`）。"""
    r = client.post("/workspace/save", data={"name": name, "source_tool": tool},
                    files={"file": (name, io.BytesIO(payload), "application/json")})
    assert r.status_code == 200, r.text
    fid = r.json()["file"]["file_id"]
    meta = next(f for f in client.get("/workspace/api/list").json()["files"]
                if f["file_id"] == fid)
    assert meta["name"] == name, meta["name"]
    body = client.get(f"/workspace/file/{fid}").content
    up = client.post("/tools/meeting-summary/upload",
                     files={"file": (meta["name"], io.BytesIO(body), "text/plain")})
    client.post("/workspace/delete", data={"file_id": fid})
    return up


@pytest.mark.parametrize("saved_as", ["週會-會議摘要.txt", "週會-會議摘要.json"])
def test_an_export_kept_in_the_workspace_opens_as_the_result(client, auth_off, saved_as):
    """匯出的 JSON（有標記、附逐字稿、帶會議背景）存進工作區再載回來：是那份結果，背景也在。"""
    r = __import__("importlib").import_module("app.tools.meeting_summary.router")
    uid = client.post("/tools/meeting-summary/upload",
                      files={"file": ("m.vtt", io.BytesIO(_VTT), "text/vtt")}).json()["upload_id"]
    out = _analysis() | {"context": "第四季預算會議", "source": {"filename": "週會.txt"}}
    r._out_path(uid).write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    exported = client.get(f"/tools/meeting-summary/download/{uid}?fmt=json").content

    up = _through_the_workspace(client, exported, saved_as, "meeting-summary")
    assert up.status_code == 200, up.text
    d = up.json()
    assert d.get("imported") is True, f"工作區裡的分析結果被當成逐字稿解析了：{d}"
    assert d.get("has_transcript") is True
    res = client.get(f"/tools/meeting-summary/result/{d['upload_id']}").json()
    assert res.get("context") == "第四季預算會議", "會議背景沒有跟著回來"
    assert res["items"]["actions"][0]["owner"] == "Bianca"


@pytest.mark.parametrize("saved_as", ["週會-會議摘要.txt", "週會-會議摘要.json"])
def test_the_auto_saved_result_opens_too(client, auth_off, saved_as):
    """作業完成時自動存進工作區的是結果檔本身（沒有標記、沒有逐字稿）—— 也要認得出來，
    只是引用沒有原文可以跳。存的時候帶著 BOM 也一樣（別的程式改存過的檔常有）。"""
    payload = b"\xef\xbb\xbf" + json.dumps(_analysis(), ensure_ascii=False).encode("utf-8")
    up = _through_the_workspace(client, payload, saved_as, "meeting-summary")
    assert up.status_code == 200, up.text
    d = up.json()
    assert d.get("imported") is True and d.get("has_transcript") is False, d


def test_a_plain_text_transcript_that_starts_with_a_brace_is_still_a_transcript(client, auth_off):
    """反向對照：純文字逐字稿就算第一個字是 `{`，不是結果的形狀就照逐字稿讀。"""
    text = "{開場}\n王小明：各位早，今天談預算。\n李美華：好。\n王小明：那就照原案。\n李美華：了解。\n"
    up = client.post("/tools/meeting-summary/upload",
                     files={"file": ("逐字稿.txt", io.BytesIO(text.encode()), "text/plain")})
    assert up.status_code == 200, up.text
    assert not up.json().get("imported"), up.json()
