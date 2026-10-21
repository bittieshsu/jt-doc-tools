"""ZAP 掃描紀錄留存（使用者 2026-10-21：每次的報告都要留下來）。

* 每次掃描都產 html / json / **pdf** 三種報告（pdf 給人留存 / 交付）。
* 每個報告資料夾旁有 `summary.json`（版本、時間、各目標網址數與 High/Medium/Low）。
* `temp/zap/INDEX.md` 一次掃描一行：舊資料夾（沒有摘要）從 json 報告重算、版本記「—」；
  沒有報告的資料夾也要列出來（標「沒有報告」）——「沒掃到」跟「掃了 0 則」不可以長得一樣。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools import run_zap_scan as rz  # noqa: E402


def _report(risks: dict[str, int], n_urls: int) -> dict:
    alerts = []
    for i, (lvl, cnt) in enumerate(risks.items()):
        if cnt:
            alerts.append({"riskdesc": f"{lvl} (Medium)", "count": str(cnt),
                           "instances": [{"uri": f"http://h/{i}/{k}"} for k in range(n_urls)]})
    return {"site": [{"alerts": alerts}]}


def test_every_scan_also_produces_a_pdf(tmp_path):
    plan = json.loads(rz._plan("auth", "http://127.0.0.1:1", tmp_path, "t", None).read_text(encoding="utf-8"))
    templates = [j["parameters"]["template"] for j in plan["jobs"] if j["type"] == "report"]
    assert {"traditional-html", "traditional-json", "traditional-pdf"} <= set(templates)
    files = {j["parameters"]["template"]: j["parameters"]["reportFile"] for j in plan["jobs"] if j["type"] == "report"}
    assert files["traditional-pdf"] != files["traditional-html"], \
        "pdf 範本會刪掉同名的 html 中間檔 —— 同名就把 html 報告一起刪了"
    title = next(j["parameters"]["reportTitle"] for j in plan["jobs"] if j["parameters"].get("template") == "traditional-pdf")
    assert title.isascii(), "pdf 範本沒有中文字型，中文標題會整段消失"


def test_summary_and_index(tmp_path, monkeypatch):
    new = tmp_path / "20261021-200000"
    new.mkdir()
    (new / "zap-auth-report.json").write_text(json.dumps(_report({"Low": 2}, 3)), encoding="utf-8")
    rz._write_summary(new, new.name, ["auth", "doc"], {"auth": rz._summarise(new / "zap-auth-report.json")})
    sm = json.loads((new / "summary.json").read_text(encoding="utf-8"))
    assert sm["version"] == rz._version() and sm["results"]["auth"]["Low"] == 2 and sm["missing"] == ["doc"]

    old = tmp_path / "20260901-120000"
    old.mkdir()
    (old / "zap-30-report.json").write_text(json.dumps(_report({"High": 0}, 0)), encoding="utf-8")
    (tmp_path / "20260707-01").mkdir()

    text = rz.write_index(tmp_path).read_text(encoding="utf-8")
    rows = [l for l in text.splitlines() if l.startswith("| 20")]
    assert [r.split(" | ")[0] for r in rows] == ["| 20261021-200000", "| 20260901-120000", "| 20260707-01"], "新的在上面"
    assert "要修" in rows[0] and "doc：沒有報告" in rows[0] and rz._version() in rows[0]
    assert "| — | 0 / 0 / 0 |" in rows[1], "舊的沒有摘要：從報告重算、版本記「—」"
    assert "沒有報告" in rows[2]


def test_the_index_lives_outside_the_public_tree():
    assert rz.INDEX.relative_to(ROOT).parts[0] == "temp", "報告與總表只放 temp/（不同步、不進部署包）"
