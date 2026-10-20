"""事務機掃描取件（v1.17.1）：工具頁與「設定 → 共用事務機」用同一份資料、已驗證支援清單、「可掃描」、進度條。

工具頁與共用事務機頁是同一份模板，原本兩支路由各自組資料 —— 工具頁加了東西，管理頁就漏了。
這裡用「改掉資料來源，兩頁都要跟著變」來驗，不靠讀原始碼。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TPL = ROOT / "app/tools/mfp_fetch/templates/mfp_fetch.html"


def _pages(client):
    return {"tool": client.get("/tools/mfp-fetch/"), "shared": client.get("/admin/mfp")}


def test_both_pages_show_the_verified_list_from_field_tests(client, auth_off):
    from app.core import mfp_profiles
    for name, r in _pages(client).items():
        assert r.status_code == 200, name
        html = r.text
        assert 'id="mfVerifiedBtn"' in html and 'id="mfVerified"' in html, f"{name} 頁要有已驗證支援清單"
        for f in mfp_profiles.FIELD_TESTS:
            for m in f["models"]:
                assert m in html, (name, m)
            if f["date"]:
                assert f["date"].replace("-", "/") in html, "清單上的日期照全站格式（斜線）"
                assert f"<small>{f['date']}</small>" not in html


def test_both_pages_follow_the_same_data_source(client, auth_off, monkeypatch):
    """改掉 FIELD_TESTS 與狀態名稱，兩頁都要跟著變 —— 證明兩支路由走同一份 page_context。"""
    from app.core import mfp_profiles
    fake = ({"vendor": "ZZVENDOR", "models": ("ZZ-MODEL-9",), "feature": "eSCL 掃描", "result": "verified",
             "note": "", "date": "2030-01-02"},)
    monkeypatch.setattr(mfp_profiles, "FIELD_TESTS", fake)
    labels = dict(mfp_profiles.STATUS_LABELS, scan_only=("ZZ-SCAN-LABEL", "x"))
    monkeypatch.setattr(mfp_profiles, "STATUS_LABELS", labels)
    for name, r in _pages(client).items():
        assert "ZZ-MODEL-9" in r.text and "2030/01/02" in r.text, name
        assert "ZZ-SCAN-LABEL" in r.text, name


def test_scan_only_status_is_defined_and_translated():
    from app.core import mfp_profiles
    assert "scan_only" in mfp_profiles.STATUS_LABELS and "scan_only" in mfp_profiles.STATUS_MESSAGES
    short, long_ = mfp_profiles.STATUS_LABELS["scan_only"]
    for loc in ("en", "ja"):
        cat = json.loads((ROOT / f"app/i18n/{loc}.json").read_text(encoding="utf-8"))
        for s in (short, long_, mfp_profiles.STATUS_MESSAGES["scan_only"]):
            assert cat.get(s), (loc, s)
    for f in mfp_profiles.FIELD_TESTS:
        assert f["result"] in mfp_profiles.FIELD_RESULT_LABELS


def _js(name_from: str, name_to: str) -> str:
    src = TPL.read_text(encoding="utf-8")
    return src[src.index(name_from):src.index(name_to)]


@pytest.mark.skipif(not shutil.which("node"), reason="需要 node")
def test_progress_and_scan_only_logic():
    js = re.search(r"function shownStatus\([^)]*\)[^\n]*", TPL.read_text(encoding="utf-8")).group(0)
    js += "\n" + _js("const SCAN_STEP", "function progressBar")
    js += r"""
    const cases = [
      scanProgress({status: 'queued'}),
      scanProgress({status: 'done'}),
      scanProgress({status: 'running', kind: 'scan', phase: 'checking'}),
      scanProgress({status: 'running', kind: 'scan', phase: 'scanning', settings: {source: 'feeder'}}),
      scanProgress({status: 'running', kind: 'scan', phase: 'scanning', settings: {source: 'platen'}, live_pages: 1}),
      scanProgress({status: 'running', kind: 'box', phase: 'downloading', live_doc: 1, live_docs: 4}),
      scanProgress({status: 'running', kind: 'share', phase: 'downloading', live_doc: 3, live_docs: 4}),
      scanProgress({status: 'running', kind: 'box', phase: 'saving'}),
      shownStatus('unsupported_vendor', true), shownStatus('unsupported_vendor', false), shownStatus('supported', true),
    ];
    console.log(JSON.stringify(cases));
    """
    r = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=20)
    assert r.returncode == 0, r.stderr
    got = json.loads(r.stdout)
    assert got[0] == {"pct": 0, "moving": True}, "排隊中：0%，流動條紋"
    assert got[1] is None, "做完不顯示進度條"
    assert got[2] == {"pct": 10, "moving": False}
    assert got[3] == {"pct": 30, "moving": True}, "送紙器掃描中不知道總頁數 → 流動，不假裝知道還要多久"
    assert got[4] == {"pct": 70, "moving": False}
    assert got[5]["pct"] == 10 and got[6]["pct"] == 50, "取件照第幾份 / 共幾份算"
    assert got[7] == {"pct": 95, "moving": False}
    assert got[8:] == ["scan_only", "unsupported_vendor", "supported"]
    for p in got[:8]:
        assert p is None or 0 <= p["pct"] <= 100


def test_progress_bar_is_accessible_and_rows_highlight():
    src = TPL.read_text(encoding="utf-8")
    bar = _js("function progressBar", "function renderScanState")
    for attr in ("'role', 'progressbar'", "'aria-valuemin', '0'", "'aria-valuemax', '100'", "'aria-valuenow'"):
        assert attr in bar, attr
    assert re.search(r"\.mf-list tbody tr:hover\s*\{[^}]*background", src), "清單列滑過要有底色"
