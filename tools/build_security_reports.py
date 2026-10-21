#!/usr/bin/env python3
"""交付用資安報告：弱點掃描（ZAP）、原始碼檢測（CodeQL）、第三方元件弱點（Dependabot）各一份中文 PDF。

客戶要「弱點掃描」與「原始碼檢測」報告（使用者 2026-10-21）。掃描對象是**發版前測試用的環境**：
本機起一個啟用認證的拋棄式實例、以管理員身分登入，跟 `run_zap_scan.py` 的 `auth` 目標同一套。
跟每次發版的 ZAP 不一樣的地方是**多跑主動掃描**（會真的送攻擊字串），所以只打拋棄式實例，
不打正式機。

    python tools/build_security_reports.py                  # 三份都產
    python tools/build_security_reports.py --skip-zap       # 只產 CodeQL 與 Dependabot（幾秒）
    python tools/build_security_reports.py --minutes 30     # 主動掃描時間上限（預設 60 分鐘）

輸出 `temp/security-reports/<版本>-<時間>/`（`temp/` 不進公開樹、不進部署包）：
三份 PDF 給客戶；`raw/` 是原始資料（ZAP 的原始報告含請求內容，**不交付**）。

GitHub 的資料用唯讀 Token 讀（`temp/codeql/token`，Code scanning alerts 與 Dependabot alerts 唯讀）。
Token 只放在請求標頭，不印、不寫進任何輸出。

還開著的告警可以在 `temp/codeql/dispositions.json` 寫處置說明（客戶審查時一定會問）：

    {"codeql": {"225": "誤判：…"}, "dependabot": {"pillow": "僅影響 Python 3.10 …"}}
"""
from __future__ import annotations

import argparse
import html
import json
import re
import shutil
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools import run_zap_scan as rz  # noqa: E402

REPO = "jasoncheng7115/jt-doc-tools"
TOKEN_FILE = ROOT / "temp" / "codeql" / "token"
DISPOSITIONS = ROOT / "temp" / "codeql" / "dispositions.json"
OUT_BASE = ROOT / "temp" / "security-reports"

#: 主動掃描不打的路徑：按下去會**對外連線**（測試 LLM / LDAP / 通知 / 事務機、下載統編資料、檢查新版…）
#: 或拿資料目錄做重度工作的端點。掃描的是拋棄式實例，打壞它沒關係，但不可以讓攻擊字串變成往外送的請求。
ACTIVE_SCAN_EXCLUDE = [
    r"/logout.*", r"/auth/.*/sls.*",
    r"/admin/api/check-latest-version.*", r"/admin/api/jtlw/test.*", r"/admin/api/llm/test-connection.*",
    r"/admin/api/ocr-langs/external/test.*", r"/admin/api/upload-limit/probe.*",
    r"/admin/auth-settings/ldap-test-.*", r"/admin/groups/directory-sync/run.*", r"/admin/groups/sync-ldap.*",
    r"/admin/knowledge/api/embedding/test.*", r"/admin/knowledge/api/gov/.*/update.*",
    r"/admin/notify/test/.*", r"/admin/sso/test.*", r"/admin/vat-db/.*download.*",
    r"/admin/ocr-remote.*", r"/tools/mfp-fetch/api/.*",
]

RISK_ZH = {"High": "高", "Medium": "中", "Low": "低", "Informational": "資訊"}
SEV_ZH = {"critical": "嚴重", "high": "高", "medium": "中", "moderate": "中", "low": "低",
          "error": "錯誤", "warning": "警告", "note": "提示"}
DISMISS_ZH = {"false positive": "誤判（False positive）", "used in tests": "只用在測試（Used in tests）",
              "won't fix": "不修（Won't fix）", "tolerable_risk": "風險可接受（Tolerable risk）",
              "inaccurate": "資料不正確（Inaccurate）", "not_used": "沒有用到（Not used）",
              "no_bandwidth": "暫不處理（No bandwidth）", "fix_started": "修正中（Fix started）"}

_CSS = """
body { font-size: 9.5pt; color: #1f2937; line-height: 1.45; }
h1 { font-size: 17pt; margin: 0 0 2px 0; }
h2 { font-size: 12.5pt; margin: 16px 0 6px 0; padding-bottom: 3px; border-bottom: 1px solid #d1d5db; }
h3 { font-size: 10.5pt; margin: 12px 0 3px 0; }
.sub { color: #4b5563; font-size: 10pt; margin: 0 0 10px 0; }
.note { color: #4b5563; font-size: 8.5pt; }
table { border-collapse: collapse; margin: 4px 0 8px 0; }
th, td { border: 1px solid #e5e7eb; padding: 3px 5px; text-align: left; vertical-align: top; font-size: 8.5pt; }
th { font-weight: bold; }
.kv th { width: 95pt; } .kv td { width: 396pt; }
.h { color: #b91c1c; font-weight: bold; } .m { color: #c2410c; font-weight: bold; }
.l { color: #a16207; font-weight: bold; } .i { color: #1d4ed8; }
.ok { color: #047857; font-weight: bold; }
.url { font-size: 8pt; color: #374151; }
"""


def _e(s) -> str:
    return html.escape("" if s is None else str(s), quote=True)


def _strip(s: str | None, limit: int = 900) -> str:
    """ZAP 的說明是 HTML 片段：去標籤、收空白、限長。"""
    t = re.sub(r"<[^>]+>", " ", s or "")
    t = " ".join(html.unescape(t).split())
    return t if len(t) <= limit else t[:limit] + "…"


def _kv(rows: list[tuple[str, str]]) -> str:
    return '<table class="kv">' + "".join(f"<tr><th>{_e(k)}</th><td>{v}</td></tr>" for k, v in rows) + "</table>"


#: A4 扣掉左右邊界的版面寬（pt）。每欄另有左右內距 5pt＋框線，所以欄寬要從這裡扣
_PAGE_W = 515


def _fit(widths: list[int]) -> list[int]:
    """把欄寬比例換成實際 pt：總寬＋每欄內距不超過版面（超過的話 MuPDF 直接畫出頁面右緣）。"""
    room = _PAGE_W - 11 * len(widths) - 2
    tot = sum(widths)
    return [max(20, int(w * room / tot)) for w in widths]


def _table(head: list[str], rows: list[list[str]], widths: list[int]) -> str:
    """表頭與每格都是已經跳脫過的 HTML。MuPDF 不認百分比寬度，欄寬用 pt（`widths` 是比例）。"""
    widths = _fit(widths)
    th = "".join(f'<th style="width:{w}pt">{h}</th>' for h, w in zip(head, widths))
    body = "".join("<tr>" + "".join(f'<td style="width:{w}pt">{c}</td>' for c, w in zip(r, widths)) + "</tr>"
                   for r in rows)
    return f"<table><tr>{th}</tr>{body}</table>"


def _cls(level: str) -> str:
    return {"High": "h", "Medium": "m", "Low": "l", "critical": "h", "high": "h", "error": "h",
            "medium": "m", "moderate": "m", "warning": "m", "low": "l", "note": "l"}.get(level, "i")


def _pdf(body: str, title: str) -> bytes:
    from app.core import mfp_report
    return mfp_report.story_pdf(body, title, "zh-TW", css=_CSS)


def _head(title: str, version: str, kind: str) -> str:
    return (f"<h1>{_e(title)}</h1><p class=\"sub\">Jason Tools 文件工具箱（jt-doc-tools）v{_e(version)}"
            f"<br>{_e(kind)}　·　產出時間 {time.strftime('%Y/%m/%d %H:%M')}</p>")


# --------------------------------------------------------------------------- GitHub（唯讀 Token）

def _token() -> str:
    if not TOKEN_FILE.is_file():
        raise SystemExit(f"找不到唯讀 Token：{TOKEN_FILE}（要有 Code scanning alerts 與 Dependabot alerts 唯讀）")
    return TOKEN_FILE.read_text(encoding="utf-8").strip()


def _gh(path: str, token: str) -> list | dict:
    """GET GitHub API；清單照回應的 `Link: rel="next"` 翻頁（Dependabot 只認游標，不認 `page`）。
    錯誤只回狀態碼，不印 Token。"""
    out: list = []
    sep = "&" if "?" in path else "?"
    url: str | None = f"https://api.github.com/repos/{REPO}{path}{sep}per_page=100"
    while url:
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}",
                                                   "Accept": "application/vnd.github+json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.load(r)
                link = r.headers.get("Link") or ""
        except urllib.error.HTTPError as e:
            raise SystemExit(f"GitHub API {path} 回 {e.code}（401 / 403：Token 過期或少了權限）") from None
        if not isinstance(data, list):
            return data
        out += data
        m = re.search(r'<([^>]+)>;\s*rel="next"', link)
        url = m.group(1) if m and m.group(1).startswith("https://api.github.com/") else None
    return out


def _dispositions() -> dict:
    try:
        return json.loads(DISPOSITIONS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


# --------------------------------------------------------------------------- 弱點掃描（ZAP）

def zap_plan(url: str, outdir: Path, cookie: str, minutes: int) -> Path:
    plan = {
        "env": {"contexts": [{"name": "jtdt-delivery", "urls": [url], "includePaths": [f"{url}.*"],
                              "excludePaths": [url + p for p in ACTIVE_SCAN_EXCLUDE]}],
                "parameters": {"failOnError": False, "failOnWarning": False, "progressToStdout": True}},
        "jobs": [
            {"type": "replacer", "parameters": {"deleteAllRules": True},
             "rules": [{"description": "admin session cookie", "matchType": "req_header", "matchString": "Cookie",
                        "matchRegex": False, "replacementString": f"jtdt_session={cookie}"}]},
            {"type": "spider", "parameters": {"context": "jtdt-delivery", "url": url, "maxDuration": 8, "maxDepth": 10}},
            {"type": "passiveScan-wait", "parameters": {"maxDuration": 10}},
            {"type": "activeScan", "parameters": {"context": "jtdt-delivery", "maxScanDurationInMins": minutes,
                                                  "maxRuleDurationInMins": 5}},
            {"type": "passiveScan-wait", "parameters": {"maxDuration": 10}},
            {"type": "report", "parameters": {"template": "traditional-json", "reportDir": str(outdir),
                                              "reportFile": "zap-raw"}},
            {"type": "report", "parameters": {"template": "traditional-html", "reportDir": str(outdir),
                                              "reportFile": "zap-raw", "reportTitle": "jt-doc-tools ZAP"}},
        ],
    }
    p = outdir / "zap-plan.yaml"
    p.write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
    return p


def _path_only(uri: str) -> str:
    """報告上只寫路徑與查詢參數名稱：主機是本機拋棄式實例（每次的埠都不一樣），參數值可能是攻擊字串。"""
    sp = urlsplit(uri)
    q = "、".join(sorted({kv.split("=", 1)[0] for kv in sp.query.split("&") if kv}))
    # 參數用頓號分隔：連成一串 `a&b&c` 沒有斷行點，長的會畫出頁面右緣
    return (sp.path or "/") + (f"（參數：{q}）" if q else "")


def _zap_key(a: dict, inst: dict) -> str:
    """處置說明的鍵：`<規則編號> <路徑>`（例 `40024 /ui-locale`）。不含主機、埠與查詢字串。"""
    return f"{a.get('pluginid')} {urlsplit(inst.get('uri', '')).path or '/'}"


def _zap_alert_block(a: dict, notes: dict | None = None) -> str:
    r = (a.get("riskdesc") or "").split(" ")[0]
    conf = (a.get("riskdesc") or "").split("(")[-1].rstrip(")") if "(" in (a.get("riskdesc") or "") else ""
    ins = a.get("instances", [])
    paths = sorted({_path_only(i.get("uri", "")) for i in ins})
    shown = "<br>".join(_e(p) for p in paths[:8]) + (f"<br>…另 {len(paths) - 8} 個" if len(paths) > 8 else "")
    out = f'<h3><span class="{_cls(r)}">[{RISK_ZH.get(r, r)}]</span> {_e(a.get("name") or a.get("alert"))}</h3>'
    rows = [
        ("風險 / 可信度", f"{_e(RISK_ZH.get(r, r))} / {_e(conf)}"),
        ("CWE / WASC", " / ".join(f"{t}-{_e(v)}" if str(v) not in ("", "-1", "0", "None") else "—"
                                  for t, v in (("CWE", a.get("cweid")), ("WASC", a.get("wascid"))))),
        ("出現次數", _e(a.get("count") or len(ins))),
        ("說明", _e(_strip(a.get("desc")))),
        ("建議", _e(_strip(a.get("solution")))),
        ("出現位置", f'<span class="url">{shown}</span>'),
    ]
    if notes:
        rows.append(("審查結果", "<br>".join(f"{_e(k.split(' ', 1)[1])}：{_e(v)}" for k, v in sorted(notes.items()))))
    return out + _kv(rows)


def render_zap(data: dict, version: str, minutes: int, disp: dict | None = None) -> str:
    """`disp["zap"]` 寫了處置說明、而且**每一個出現位置都有**的告警，移到「經審查為誤判」一節，
    不算進風險統計（原始告警與查證方式照樣列出）。只有部分位置有說明的告警留在明細裡。"""
    notes = (disp or {}).get("zap", {})
    alerts = [a for s in data.get("site", []) for a in s.get("alerts", [])]
    urls = {i.get("uri") for a in alerts for i in a.get("instances", []) if i.get("uri")}
    reviewed, active = [], []
    for a in alerts:
        keys = {_zap_key(a, i) for i in a.get("instances", [])}
        (reviewed if keys and all(k in notes for k in keys) else active).append(a)
    by_risk: Counter = Counter()
    inst: Counter = Counter()
    for a in active:
        r = (a.get("riskdesc") or "").split(" ")[0]
        by_risk[r] += 1
        inst[r] += int(a.get("count") or len(a.get("instances", [])))
    body = _head("弱點掃描報告", version, "動態應用程式安全測試（DAST）")
    body += "<h2>掃描範圍與方法</h2>" + _kv([
        ("受測系統", f"jt-doc-tools v{_e(version)}（發版前測試環境：全新資料目錄、啟用帳號認證，以管理員身分登入掃描）"),
        ("掃描工具", f"OWASP ZAP {_e(data.get('@version', ''))}"),
        ("掃描時間", _e(data.get("@generated", ""))),
        ("掃描方式", "爬蟲（含表單）→ 被動掃描 → 主動掃描（ZAP 預設規則，會實際送出注入、跨站指令碼等攻擊字串）"
                     f"→ 被動掃描。主動掃描時間上限 {minutes} 分鐘、每條規則 5 分鐘。"),
        ("出現告警的網址", f"{len(urls)} 個（爬蟲找到的頁面與 API 都在主動掃描範圍內）"),
        ("排除範圍", "登出、SAML 登出，以及按下去會對外連線的測試與下載功能（LLM / LDAP / 通知 / 事務機連線測試、"
                     "統編資料下載、檢查新版等）。這些端點仍經過爬蟲與被動掃描，只是不送攻擊字串，避免攻擊內容變成往外送的請求。"),
    ])
    body += "<h2>結果摘要</h2>"
    rows = [[f'<span class="{_cls(k)}">{RISK_ZH[k]}（{k}）</span>', str(by_risk.get(k, 0)), str(inst.get(k, 0))]
            for k in ("High", "Medium", "Low", "Informational")]
    body += _table(["風險等級", "告警種類", "出現次數"], rows, [200, 150, 165])
    hml = sum(by_risk.get(k, 0) for k in ("High", "Medium", "Low"))
    body += ('<p class="ok">高、中、低風險告警皆為 0。</p>' if hml == 0 else
             f'<p class="h">高、中、低風險告警共 {hml} 種，明細如下。</p>')
    if reviewed:
        body += (f'<p>另有 {len(reviewed)} 種告警經人工查證為誤判，不列入上表，'
                 '原始告警與查證方式見「經審查為誤判的告警」。</p>')
    body += '<p class="note">「資訊」等級是 ZAP 的提示性項目（例如快取標頭建議、網站使用的技術），不屬於弱點。</p>'
    order = {"High": 0, "Medium": 1, "Low": 2, "Informational": 3}
    key = lambda a: (order.get((a.get("riskdesc") or "").split(" ")[0], 9), a.get("name", ""))  # noqa: E731
    body += "<h2>告警明細</h2>"
    if not active:
        body += "<p>沒有任何告警。</p>"
    for a in sorted(active, key=key):
        part = {k: notes[k] for k in {_zap_key(a, i) for i in a.get("instances", [])} if k in notes}
        body += _zap_alert_block(a, part or None)
    if reviewed:
        body += "<h2>經審查為誤判的告警</h2>"
        body += '<p class="note">以下告警逐一在未加負載的測試環境重現與查證，判定為誤判。</p>'
        for a in sorted(reviewed, key=key):
            body += _zap_alert_block(a, {k: notes[k] for k in {_zap_key(a, i) for i in a.get("instances", [])}})
    return body


def run_zap(outdir: Path, minutes: int) -> dict:
    raw = outdir / "raw"
    tmp = ROOT / "temp" / f"zap-data-delivery-{time.strftime('%Y%m%d-%H%M%S')}"
    tmp.mkdir(parents=True, exist_ok=True)
    proc = None
    try:
        proc, port, cookie = rz._start_authenticated_instance(tmp)
        plan = zap_plan(f"http://127.0.0.1:{port}", raw, cookie, minutes)
        rz._run(plan)
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except Exception:  # noqa: BLE001
                proc.kill()
        shutil.rmtree(tmp, ignore_errors=True)
    rep = raw / "zap-raw.json"
    if not rep.is_file():
        raise SystemExit("ZAP 沒有產出報告 —— 這次等於沒掃（先確認 JAVA_HOME 與 ZAP 位置）")
    return json.loads(rep.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- 原始碼檢測（CodeQL）

def render_codeql(alerts: list[dict], analyses: list[dict], version: str, disp: dict) -> str:
    latest: dict[str, dict] = {}
    for a in analyses:
        cat = a.get("category") or a.get("analysis_key") or ""
        if a.get("ref") == "refs/heads/main" and (cat not in latest or a["created_at"] > latest[cat]["created_at"]):
            latest[cat] = a
    state = Counter(a["state"] for a in alerts)
    op = [a for a in alerts if a["state"] == "open"]
    dis = [a for a in alerts if a["state"] == "dismissed"]
    sev = lambda a: (a["rule"].get("security_severity_level") or a["rule"].get("severity") or "")  # noqa: E731
    body = _head("原始碼檢測報告", version, "靜態原始碼分析（SAST）")
    an = sorted(latest.values(), key=lambda a: a.get("category") or "")
    tool = an[0]["tool"] if an else {}
    body += "<h2>檢測範圍與方法</h2>" + _kv([
        ("受測程式", f"jt-doc-tools 公開原始碼（github.com/{REPO}，main 分支）"),
        ("檢測工具", f"{_e(tool.get('name', 'CodeQL'))} {_e(tool.get('version', ''))}（GitHub code scanning）"),
        ("檢測項目", "<br>".join(
            f"{_e((a.get('category') or '').replace('/language:', ''))}：commit {_e(a['commit_sha'][:7])}、"
            f"{_e(a['created_at'][:10])}、規則 {_e(a.get('rules_count'))} 條"
            for a in an) or "（沒有分析紀錄）"),
        ("檢測時機", "每次推送 main 分支自動執行；每一版發版前與發版後都核對告警並處置"),
    ])
    body += "<h2>結果摘要</h2>"
    open_sev = Counter(sev(a) for a in op)
    rows = [[f'<span class="{_cls(k)}">{SEV_ZH.get(k, k)}</span>', str(open_sev.get(k, 0))]
            for k in ("critical", "high", "medium", "low", "error", "warning", "note") if open_sev.get(k)]
    body += _kv([("累計告警", str(len(alerts))), ("已修正", str(state.get("fixed", 0))),
                 ("已審查關閉", f"{len(dis)}（" + "、".join(
                     f"{DISMISS_ZH.get(k, k)} {v}" for k, v in Counter(a.get('dismissed_reason') for a in dis).items())
                  + "）" if dis else "0"),
                 ("尚未關閉", str(len(op)))])
    if rows:
        body += _table(["尚未關閉：嚴重度", "則數"], rows, [300, 215])
    note = disp.get("codeql", {})
    body += "<h2>尚未關閉的告警</h2>"
    if not op:
        body += '<p class="ok">沒有尚未關閉的告警。</p>'
    else:
        body += _table(["#", "嚴重度", "規則", "位置", "處置說明"], [[
            _e(a["number"]), f'<span class="{_cls(sev(a))}">{_e(SEV_ZH.get(sev(a), sev(a)))}</span>',
            _e(a["rule"]["id"]),
            _e(f"{a['most_recent_instance']['location']['path']}:{a['most_recent_instance']['location'].get('start_line')}"),
            _e(note.get(str(a["number"]), "待處置"))] for a in sorted(op, key=lambda a: a["number"])],
            [28, 40, 120, 140, 187])
    body += "<h2>已審查關閉的告警</h2>"
    body += '<p class="note">經人工審查後關閉：誤判、只在測試程式中執行，或評估後不修並寫明理由。</p>'
    body += _table(["#", "嚴重度", "規則", "位置", "原因與說明"], [[
        _e(a["number"]), f'<span class="{_cls(sev(a))}">{_e(SEV_ZH.get(sev(a), sev(a)))}</span>',
        _e(a["rule"]["id"]), _e(a["most_recent_instance"]["location"]["path"]),
        _e(DISMISS_ZH.get(a.get("dismissed_reason"), a.get("dismissed_reason"))) +
        (f"<br><span class=\"note\">{_e(a.get('dismissed_comment'))}</span>" if a.get("dismissed_comment") else "")]
        for a in sorted(dis, key=lambda a: a["number"])], [28, 40, 130, 150, 167]) if dis else "<p>無。</p>"
    fixed = Counter(a["rule"]["id"] for a in alerts if a["state"] == "fixed")
    body += "<h2>已修正的告警（依規則）</h2>"
    body += _table(["規則", "則數"], [[_e(k), str(v)] for k, v in fixed.most_common()], [400, 115]) if fixed else "<p>無。</p>"
    return body


# --------------------------------------------------------------------------- 第三方元件弱點（Dependabot）

def render_dependabot(alerts: list[dict], version: str, disp: dict) -> str:
    op = [a for a in alerts if a["state"] == "open"]
    dis = [a for a in alerts if a["state"] == "dismissed"]
    fixed = [a for a in alerts if a["state"] == "fixed"]
    body = _head("第三方元件弱點報告", version, "軟體組成分析（SCA）")
    body += "<h2>檢測範圍與方法</h2>" + _kv([
        ("受測程式", f"jt-doc-tools 公開原始碼（github.com/{REPO}，main 分支）的相依宣告"),
        ("檢測工具", "GitHub Dependabot（比對 GitHub Advisory Database）"),
        ("宣告檔", "、".join(sorted({a["dependency"]["manifest_path"] for a in alerts})) or "—"),
        ("檢測時機", "相依宣告或弱點資料庫有變動時自動比對；每一版發版前與發版後都核對"),
    ])
    sevc = Counter(a["security_advisory"]["severity"] for a in op)
    body += "<h2>結果摘要</h2>" + _kv([
        ("累計告警", str(len(alerts))), ("已修正（升級）", str(len(fixed))),
        ("已審查關閉", str(len(dis))),
        ("尚未關閉", f"{len(op)}" + ("（" + "、".join(f"{SEV_ZH.get(k, k)} {v}" for k, v in sevc.most_common()) + "）"
                                       if op else "")),
    ])
    note = disp.get("dependabot", {})
    body += "<h2>尚未關閉的告警</h2>"
    if not op:
        body += '<p class="ok">沒有尚未關閉的告警。</p>'
    else:
        groups: dict[tuple, list] = {}
        for a in op:
            v = a["security_vulnerability"]
            key = (v["package"]["name"].lower(), a["security_advisory"]["ghsa_id"])
            groups.setdefault(key, []).append(a)
        for pkg in sorted({k[0] for k in groups}):
            items = [(k, v) for k, v in groups.items() if k[0] == pkg]
            body += f"<h3>{_e(pkg)}</h3>"
            if note.get(pkg):
                body += f"<p><b>處置說明：</b>{_e(note[pkg])}</p>"
            body += _table(["嚴重度", "公告", "受影響版本", "修正版本", "宣告檔"], [[
                f'<span class="{_cls(v[0]["security_advisory"]["severity"])}">'
                f'{_e(SEV_ZH.get(v[0]["security_advisory"]["severity"]))}</span>',
                _e(k[1]) + "<br><span class=\"note\">" + _e(v[0]["security_advisory"].get("summary", "")) + "</span>",
                _e(v[0]["security_vulnerability"]["vulnerable_version_range"]),
                _e((v[0]["security_vulnerability"].get("first_patched_version") or {}).get("identifier", "—")),
                _e("、".join(sorted({x["dependency"]["manifest_path"] for x in v})))]
                for k, v in sorted(items, key=lambda kv: kv[0][1])], [45, 190, 95, 60, 125])
    if dis:
        body += "<h2>已審查關閉的告警</h2>" + _table(["#", "套件", "公告", "原因與說明"], [[
            _e(a["number"]), _e(a["security_vulnerability"]["package"]["name"]),
            _e(a["security_advisory"]["ghsa_id"]),
            _e(DISMISS_ZH.get(a.get("dismissed_reason"), a.get("dismissed_reason"))) +
            (f"<br><span class=\"note\">{_e(a.get('dismissed_comment'))}</span>" if a.get("dismissed_comment") else "")]
            for a in sorted(dis, key=lambda a: a["number"])], [30, 90, 130, 265])
    if fixed:
        body += "<h2>已修正的告警（依套件）</h2>" + _table(["套件", "則數"], [[
            _e(k), str(v)] for k, v in Counter(a["security_vulnerability"]["package"]["name"].lower()
                                               for a in fixed).most_common()], [400, 115])
    return body


# --------------------------------------------------------------------------- 入口

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-zap", action="store_true")
    ap.add_argument("--minutes", type=int, default=60, help="主動掃描時間上限（分鐘）")
    ap.add_argument("--zap-json", type=Path, help="不重新掃描，改用之前的 raw/zap-raw.json 重排報告（例如補了處置說明之後）")
    args = ap.parse_args(argv)
    version = rz._version()
    outdir = OUT_BASE / f"{version}-{time.strftime('%Y%m%d-%H%M%S')}"
    (outdir / "raw").mkdir(parents=True, exist_ok=True)
    disp = _dispositions()
    token = _token()

    codeql = _gh("/code-scanning/alerts?state=all", token)
    analyses = _gh("/code-scanning/analyses?ref=refs/heads/main", token)
    dep = _gh("/dependabot/alerts", token)   # 不帶 state＝全部狀態
    for name, data in (("codeql-alerts", codeql), ("codeql-analyses", analyses), ("dependabot-alerts", dep)):
        (outdir / "raw" / f"{name}.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    files = {
        f"jt-doc-tools-{version}-原始碼檢測報告.pdf": _pdf(render_codeql(codeql, analyses, version, disp),
                                                       f"jt-doc-tools {version} 原始碼檢測報告"),
        f"jt-doc-tools-{version}-第三方元件弱點報告.pdf": _pdf(render_dependabot(dep, version, disp),
                                                         f"jt-doc-tools {version} 第三方元件弱點報告"),
    }
    if args.zap_json:
        zap = json.loads(args.zap_json.read_text(encoding="utf-8"))
        shutil.copy2(args.zap_json, outdir / "raw" / "zap-raw.json")
    elif not args.skip_zap:
        zap = run_zap(outdir, args.minutes)
    if args.zap_json or not args.skip_zap:
        files[f"jt-doc-tools-{version}-弱點掃描報告.pdf"] = _pdf(render_zap(zap, version, args.minutes, disp),
                                                           f"jt-doc-tools {version} 弱點掃描報告")
    for name, data in files.items():
        (outdir / name).write_bytes(data)
        print(f"  {name}  {len(data) // 1024} KB")
    print(f"輸出：{outdir}（PDF 給客戶；raw/ 是原始資料，不交付）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
