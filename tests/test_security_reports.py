"""交付用資安報告（`tools/build_security_reports.py`；使用者 2026-10-21：客戶要弱點掃描與原始碼檢測報告）。

不連網、不跑 ZAP：拿假資料驗三份報告的內容與保護措施。
* 主動掃描只打拋棄式實例（127.0.0.1），會對外連線的端點不送攻擊字串。
* 報告不寫主機與參數值（埠每次不同；參數值可能是攻擊字串）、不出現 session cookie。
* 欄寬換算後不超出 A4 版面；三份都排得成中文 PDF。
* Token 不印、不進任何輸出；Dependabot 用游標翻頁（`page` 參數會被回 400）。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools import build_security_reports as bsr  # noqa: E402

_COOKIE = "c00kie-value-should-never-appear"


def _zap() -> dict:
    return {"@version": "2.17.0", "@generated": "Wed, 21 Oct 2026", "site": [{"alerts": [
        {"name": "Cross Site Scripting (Reflected)", "riskdesc": "High (Medium)", "cweid": "79", "wascid": "8",
         "count": "1", "desc": "<p>XSS</p>", "solution": "<p>Escape output</p>",
         "instances": [{"uri": "http://127.0.0.1:40123/tools/x/?q=%3Cscript%3E&page=2", "method": "GET",
                        "param": "q", "attack": "<script>alert(1)</script>",
                        "evidence": f"Set-Cookie: jtdt_session={_COOKIE}"}]},
        {"name": "Re-examine Cache-control Directives", "riskdesc": "Informational (Low)", "cweid": "525",
         "wascid": "13", "count": "3", "desc": "x", "solution": "y",
         "instances": [{"uri": "http://127.0.0.1:40123/"}]},
    ]}]}


def test_zap_report_hides_hosts_parameter_values_and_cookies():
    body = bsr.render_zap(_zap(), "9.9.9", 60)
    assert "/tools/x/（參數：page、q）" in body
    assert "127.0.0.1:40123" not in body, "埠每次不同、主機是本機實例，報告只寫路徑"
    assert "alert(1)" not in body and "%3Cscript" not in body, "參數值可能是攻擊字串，不寫進報告"
    assert _COOKIE not in body
    assert "高、中、低風險告警共 1 種" in body and "[資訊]" in body


def test_zap_report_says_zero_when_clean():
    data = _zap()
    data["site"][0]["alerts"] = data["site"][0]["alerts"][1:]
    assert "高、中、低風險告警皆為 0" in bsr.render_zap(data, "9.9.9", 60)


def test_active_scan_only_targets_the_throwaway_instance_and_skips_outbound_endpoints(tmp_path):
    plan = json.loads(bsr.zap_plan("http://127.0.0.1:1", tmp_path, "x", 30).read_text(encoding="utf-8"))
    ctx = plan["env"]["contexts"][0]
    assert ctx["urls"] == ["http://127.0.0.1:1"]
    jobs = [j["type"] for j in plan["jobs"]]
    assert jobs.index("spider") < jobs.index("activeScan")
    act = next(j for j in plan["jobs"] if j["type"] == "activeScan")
    assert act["parameters"]["maxScanDurationInMins"] == 30
    excl = " ".join(ctx["excludePaths"])
    for p in ("/admin/notify/test/", "/admin/api/llm/test-connection", "/tools/mfp-fetch/api/",
              "/admin/vat-db/", "/admin/auth-settings/ldap-test-", "/logout"):
        assert p in excl, f"{p} 會對外連線或登出，主動掃描要排除"


def _codeql_alert(n, state, sev="high", reason=None, path="app/x.py"):
    return {"number": n, "state": state, "dismissed_reason": reason, "dismissed_comment": None,
            "rule": {"id": "py/demo", "security_severity_level": sev},
            "most_recent_instance": {"location": {"path": path, "start_line": 7}}}


def test_codeql_report_counts_and_dispositions():
    alerts = [_codeql_alert(1, "fixed"), _codeql_alert(2, "dismissed", reason="false positive"),
              _codeql_alert(3, "open", "critical")]
    analyses = [{"ref": "refs/heads/main", "category": "/language:python", "commit_sha": "abcdef123",
                 "created_at": "2026-10-21T00:00:00Z", "rules_count": 50,
                 "tool": {"name": "CodeQL", "version": "2.27.2"}}]
    body = bsr.render_codeql(alerts, analyses, "9.9.9", {"codeql": {"3": "說明 A"}})
    assert "CodeQL 2.27.2" in body and "python：commit abcdef1" in body and "規則 50 條" in body
    assert "說明 A" in body and "誤判（False positive）" in body
    assert "待處置" not in body
    assert "待處置" in bsr.render_codeql(alerts, analyses, "9.9.9", {}), "沒寫處置說明要看得出來"


def test_dependabot_report_groups_by_package():
    def a(n, ghsa, manifest, state="open"):
        return {"number": n, "state": state, "dismissed_reason": None,
                "dependency": {"manifest_path": manifest},
                "security_advisory": {"severity": "high", "ghsa_id": ghsa, "summary": "s"},
                "security_vulnerability": {"package": {"name": "Pillow"}, "vulnerable_version_range": "< 13.0.0",
                                           "first_patched_version": {"identifier": "13.0.0"}}}
    body = bsr.render_dependabot([a(1, "GHSA-1", "uv.lock"), a(2, "GHSA-1", "requirements.txt"),
                                  a(3, "GHSA-2", "uv.lock", "fixed")], "9.9.9",
                                 {"dependabot": {"pillow": "只影響 3.10"}})
    assert body.count("GHSA-1") == 1, "同一則公告在多個宣告檔只列一列"
    assert "requirements.txt、uv.lock" in body and "只影響 3.10" in body


def test_tables_fit_the_page():
    for widths in ([28, 40, 120, 140, 187], [45, 190, 95, 60, 125], [400, 115]):
        fitted = bsr._fit(widths)
        assert sum(fitted) + 11 * len(fitted) + 2 <= bsr._PAGE_W


@pytest.mark.parametrize("which", ["zap", "codeql", "dependabot"])
def test_each_report_renders_to_a_pdf_with_chinese(which):
    import fitz
    body = {"zap": lambda: bsr.render_zap(_zap(), "9.9.9", 60),
            "codeql": lambda: bsr.render_codeql([_codeql_alert(1, "open")], [], "9.9.9", {}),
            "dependabot": lambda: bsr.render_dependabot([], "9.9.9", {})}[which]()
    data = bsr._pdf(body, "測試報告")
    with fitz.open("pdf", data) as d:
        text = d[0].get_text()
    assert data.startswith(b"%PDF") and "報告" in text


def test_dependabot_paging_follows_the_link_header_and_never_prints_the_token(monkeypatch, capsys):
    seen = []

    class R:
        def __init__(self, body, link):
            self._b, self.headers = json.dumps(body).encode(), {"Link": link}

        def read(self, *a):
            return self._b

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    pages = {"first": ([{"n": 1}], '<https://api.github.com/repos/x/next?after=abc>; rel="next"'),
             "next": ([{"n": 2}], "")}

    def fake(req, timeout=0):
        seen.append(req.full_url)
        assert req.headers["Authorization"] == "Bearer SECRET"
        return R(*pages["next" if "after=abc" in req.full_url else "first"])
    monkeypatch.setattr(bsr.urllib.request, "urlopen", fake)
    out = bsr._gh("/dependabot/alerts", "SECRET")
    assert out == [{"n": 1}, {"n": 2}]
    assert not re.search(r"[?&]page=", seen[0]), "Dependabot 不認 page 參數（回 400）"
    assert "SECRET" not in capsys.readouterr().out


def test_reviewed_false_positives_move_out_of_the_counts_but_stay_in_the_report():
    data = _zap()
    data["site"][0]["alerts"][0]["pluginid"] = "40024"
    disp = {"zap": {"40024 /tools/x/": "誤判（已查證）：參數化查詢，依序重現時間不受輸入影響"}}
    body = bsr.render_zap(data, "9.9.9", 60, disp)
    assert "高、中、低風險告警皆為 0" in body
    assert "另有 1 種告警經人工查證為誤判" in body and "經審查為誤判的告警" in body
    assert "Cross Site Scripting (Reflected)" in body and "參數化查詢" in body, "原始告警與查證方式照樣列出"


def test_a_note_for_only_some_locations_keeps_the_alert_in_the_counts():
    data = _zap()
    a = data["site"][0]["alerts"][0]
    a["pluginid"] = "40024"
    a["instances"].append({"uri": "http://127.0.0.1:40123/other"})
    body = bsr.render_zap(data, "9.9.9", 60, {"zap": {"40024 /tools/x/": "誤判"}})
    assert "高、中、低風險告警共 1 種" in body, "還有沒查證的位置，就不可以當成誤判"


def test_missing_cwe_is_shown_as_a_dash():
    data = _zap()
    data["site"][0]["alerts"][0].update(cweid="-1", wascid="-1")
    body = bsr.render_zap(data, "9.9.9", 60)
    assert "CWE--1" not in body and "WASC--1" not in body
