"""事務機連線測試的診斷報告：同一份報告輸出成 txt / md / json / html / pdf。

* 輸入一定是 `mfp_client.probe()` 產出的報告 —— **產出時就已經遮蔽過帳密與工作階段資訊**
  （`mfp_redact`），這裡只換格式，不碰設備、不加任何報告裡沒有的東西。
* json 是原始報告（給回報問題用，欄位齊全）；其他格式是給人看的，內容跟畫面上「詳細」一致，
  固定字串依介面語言翻譯，報告裡的值原樣輸出。
* 報告的值來自設備網頁（標題、標頭、頁面文字），**一律當資料**：HTML 全部跳脫、不帶指令碼，
  Markdown 的表格符號與程式碼區塊圍欄都處理過。
* PDF 用 PyMuPDF 的 Story 排版，嵌入繁中字型子集（只嵌用到的字），找不到中文字型時交給
  MuPDF 內建的後備字型。
"""
from __future__ import annotations

import html
import io
import json
import re
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Optional

from .i18n import translate

FORMATS: dict[str, tuple[str, str]] = {
    "json": ("application/json", "json"),
    "txt": ("text/plain; charset=utf-8", "txt"),
    "md": ("text/markdown; charset=utf-8", "md"),
    "html": ("text/html; charset=utf-8", "html"),
    "pdf": ("application/pdf", "pdf"),
}

#: 跟畫面上「詳細」的欄位名稱同一份（`mfp_fetch.html` 的 KEY_LABELS）
KEY_LABELS = {
    "ip": "位址", "port": "連接埠", "host": "主機", "path": "路徑", "status": "狀態碼",
    "reason": "原因", "http_version": "HTTP 版本", "bytes": "位元組", "title": "頁面標題",
    "error": "錯誤", "errno": "錯誤代碼", "from": "來源", "to": "目的地",
    "refresh": "自動轉址", "tls_verified": "TLS 已驗證", "password_inputs": "密碼欄位數",
    "script_redirects": "腳本轉址", "stub_html": "頁面原始內容（節錄）",
    "version": "TLS 版本", "cipher": "加密套件", "verified": "憑證已驗證",
    "verify_code": "驗證錯誤代碼", "verify_message": "驗證失敗原因",
    "subject": "主體", "issuer": "簽發者", "not_before": "生效日", "not_after": "到期日",
    "self_signed": "自簽憑證", "expired": "已過期", "not_yet_valid": "尚未生效",
    "san": "主體別名", "sha256": "SHA-256 指紋", "public_key": "公開金鑰",
    "unverified_error": "未驗證連線的錯誤", "allowed": "允許的連接埠", "input": "輸入",
    "code": "代碼",
}
STEP_NAMES = {
    "dns": "位址檢查", "tcp": "TCP 連線", "tls": "TLS", "http": "HTTP 請求",
    "follow": "跟隨轉址", "budget": "時間預算", "escl": "掃描介面（eSCL）",
}
EVIDENCE_WHAT = {"vendor": "廠牌", "model": "型號", "ui": "網頁介面名稱", "firmware": "韌體（可能）"}
EVIDENCE_FIELD = {"title": "頁面標題", "text": "頁面文字", "header": "回應標頭", "escl": "掃描介面（eSCL）"}
SOURCE_NAMES = {"platen": "平台玻璃", "adf": "自動送紙器（單面）", "adf_duplex": "自動送紙器（雙面）"}
COLOR_NAMES = {"color": "彩色", "gray": "灰階", "bw": "黑白"}
#: 細節表格不直接列、另外整理的欄位（同畫面）
_SKIP_KEYS = {"headers", "forms", "frames", "certificate", "reason", "text", "addresses"}

#: 這支模組自己用到、不在上面幾張表裡的固定字串（測試用來確認每一條都有英日文）
PHRASES = (
    "事務機連線測試診斷報告", "報告資訊", "測試時間", "測試的協定與連接埠", "總耗時", "程式版本",
    "本報告已遮蔽帳號、密碼與工作階段資訊，可以直接附在問題回報裡。",
    "測試結果", "連線", "可連線", "連不上", "沒有使用 HTTPS", "TLS 憑證已驗證", "TLS 憑證未通過驗證",
    "廠牌", "型號", "韌體", "相容性", "從本站掃描", "支援", "不支援", "未辨識", "辨識依據",
    "掃描介面（eSCL）", "協定", "eSCL 版本",
    "逐步診斷", "憑證", "回應標頭", "頁面文字摘要（已去除標籤）",
    "表單欄位（只列名稱與型別，不含值）", "框架網址", "未命名", "是", "否", "項目", "內容",
    "［已遮蔽，長度 {0}］", "［已遮蔽］",
)

_MASK_LEN = re.compile(r"［已遮蔽，長度 (\d+)］")
_MASK_PLAIN = "［已遮蔽］"


class _Ctx:
    """一次輸出用的語言設定：翻譯、標點（中日文全形，英文半形 —— 英文不可以出現「：」）。"""

    def __init__(self, locale: Optional[str]):
        self.locale = locale
        fw = not locale or locale.startswith(("zh", "ja"))
        self.colon, self.lp, self.rp, self.sep = ("：", "（", "）", "、") if fw else (": ", " (", ")", ", ")

    def t(self, s: str) -> str:
        return translate(s, self.locale)

    def val(self, v: Any) -> str:
        """報告裡的值 → 顯示字串。遮蔽標記照介面語言顯示（報告檔本身的 json 不改）。"""
        if v is True:
            s = self.t("是")
        elif v is False:
            s = self.t("否")
        elif v is None:
            s = "—"
        elif isinstance(v, (list, tuple)):
            s = ", ".join(self.val(x) for x in v)
        elif isinstance(v, dict):
            s = json.dumps(v, ensure_ascii=False)
        else:
            s = str(v)
        if self.locale and self.locale != "zh-Hant":
            s = _MASK_LEN.sub(lambda m: self.t("［已遮蔽，長度 {0}］").replace("{0}", m.group(1)), s)
            s = s.replace(_MASK_PLAIN, self.t(_MASK_PLAIN))
        return s


# --------------------------------------------------------------------------- 中介結構

def _rows(ctx: _Ctx, obj: Any) -> list[tuple[str, str]]:
    out = []
    for k, v in (obj or {}).items() if isinstance(obj, dict) else []:
        if v is None or v == "" or (isinstance(v, (list, tuple)) and not v) or k in _SKIP_KEYS:
            continue
        out.append((ctx.t(KEY_LABELS[k]) if k in KEY_LABELS else str(k), ctx.val(v)))
    return out


def _step(ctx: _Ctx, s: dict) -> dict:
    d = s.get("detail") if isinstance(s.get("detail"), dict) else {}
    tables: list[tuple[str, list[tuple[str, str]]]] = []
    blocks: list[tuple[str, str]] = []
    base = _rows(ctx, d)
    if base:
        tables.append(("", base))
    if d.get("addresses"):
        tables.append(("", [(str(a.get("ip", "")), ctx.t(a.get("message") or a.get("code") or ""))
                            for a in d["addresses"] if isinstance(a, dict)]))
    if isinstance(d.get("reason"), dict):
        tables.append(("", _rows(ctx, d["reason"])))
    elif d.get("reason"):
        tables.append(("", [(ctx.t("原因"), ctx.val(d["reason"]))]))
    if isinstance(d.get("certificate"), dict) and d["certificate"]:
        tables.append((ctx.t("憑證"), _rows(ctx, d["certificate"])))
    if d.get("headers"):
        hs = [(str(h[0]), ctx.val(h[1])) for h in d["headers"] if isinstance(h, (list, tuple)) and len(h) == 2]
        tables.append((ctx.t("回應標頭") + ctx.lp + str(len(hs)) + ctx.rp, hs))
    if d.get("text"):
        blocks.append((ctx.t("頁面文字摘要（已去除標籤）"), ctx.val(d["text"])))
    if d.get("forms"):
        lines = []
        for f in d["forms"]:
            if not isinstance(f, dict):
                continue
            lines.append(f"{str(f.get('method') or '').upper()} {ctx.val(f.get('action') or '')}".strip())
            for i in f.get("inputs") or []:
                if isinstance(i, dict):
                    name = i.get("name") or (ctx.lp.strip() + ctx.t("未命名") + ctx.rp.strip())
                    lines.append(f"  {ctx.val(name)} : {ctx.val(i.get('type') or '')}")
        blocks.append((ctx.t("表單欄位（只列名稱與型別，不含值）") + ctx.lp + str(len(d["forms"])) + ctx.rp,
                       "\n".join(lines)))
    if d.get("frames"):
        blocks.append((ctx.t("框架網址"), "\n".join(ctx.val(x) for x in d["frames"])))
    return {
        "ok": s.get("ok"),
        "name": ctx.t(STEP_NAMES[s["step"]]) if s.get("step") in STEP_NAMES else str(s.get("step") or ""),
        "message": ctx.t(s.get("message") or s.get("code") or ""),
        "ms": s.get("ms") if isinstance(s.get("ms"), (int, float)) else None,
        "tables": [t for t in tables if t[1]],
        "blocks": blocks,
        "note": ctx.t(s.get("note_message") or s.get("note")) if s.get("note") else "",
    }


def _when(iso: Any) -> str:
    try:
        return datetime.fromisoformat(str(iso)).astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
    except (TypeError, ValueError):
        return ""


def build(report: dict, locale: Optional[str] = None) -> dict:
    """報告 → 給人看的結構（標題、段落、逐步診斷）。txt / md / html / pdf 都從這裡來，內容一致。"""
    from . import mfp_profiles
    ctx = _Ctx(locale)
    rep = report if isinstance(report, dict) else {}
    det = rep.get("detection") or {}
    summ = rep.get("summary") or {}
    target = rep.get("target") or {}
    escl = rep.get("escl") or None

    info = [(ctx.t("主機"), ctx.val(target.get("host") or ""))]
    if _when(rep.get("started_at")):
        info.append((ctx.t("測試時間"), _when(rep.get("started_at"))))
    tries = [f"{str(x.get('scheme') or '').upper()} {x.get('port')}" for x in target.get("tries") or []
             if isinstance(x, dict)]
    if tries:
        info.append((ctx.t("測試的協定與連接埠"), ctx.sep.join(tries)))
    if isinstance(summ.get("elapsed_ms"), (int, float)):
        info.append((ctx.t("總耗時"), f"{summ['elapsed_ms']} ms"))
    if rep.get("app_version"):
        info.append((ctx.t("程式版本"), ctx.val(rep["app_version"])))

    reach = summ.get("reachable_on") or []
    if summ.get("reachable"):
        conn = ctx.t("可連線") + (ctx.lp + ctx.sep.join(str(x) for x in reach) + ctx.rp if reach else "")
    else:
        conn = ctx.t("連不上")
    tls = summ.get("tls")
    tls_text = ctx.t("沒有使用 HTTPS") if not tls else (
        ctx.t("TLS 憑證已驗證") if tls.get("verified") else ctx.t("TLS 憑證未通過驗證"))
    status = det.get("status") or ""
    lab = mfp_profiles.STATUS_LABELS.get(status, (status or "—", ""))
    compat = ctx.t(lab[0]) + (ctx.lp + ctx.t(lab[1]) + ctx.rp if lab[1] else "")
    srcs = list((escl or {}).get("sources") or {})
    scan = (ctx.t("支援") + ctx.lp + ctx.sep.join(ctx.t(SOURCE_NAMES[k]) if k in SOURCE_NAMES else k
                                                 for k in srcs) + ctx.rp) if srcs else ctx.t("不支援")
    model = " ".join(str(x) for x in (det.get("family"), det.get("model")) if x) \
        or str((escl or {}).get("model") or "") or ctx.t("未辨識")
    result = [
        (ctx.t("連線"), conn), ("TLS", tls_text),
        (ctx.t("廠牌"), ctx.val(det.get("vendor")) if det.get("vendor") else ctx.t("未辨識")),
        (ctx.t("型號"), ctx.val(model)), (ctx.t("韌體"), ctx.val(det.get("firmware") or "—")),
        (ctx.t("相容性"), compat), (ctx.t("從本站掃描"), scan),
    ]
    evidence = []
    for e in det.get("evidence") or []:
        if not isinstance(e, dict):
            continue
        what = ctx.t(EVIDENCE_WHAT[e.get("what")]) if e.get("what") in EVIDENCE_WHAT else str(e.get("what") or "")
        field = ctx.t(EVIDENCE_FIELD[e.get("field")]) if e.get("field") in EVIDENCE_FIELD else str(e.get("field") or "")
        where = f"{ctx.val(e.get('page') or '')} {field}".strip() + (
            ctx.lp + ctx.val(e["header"]) + ctx.rp if e.get("header") else "")
        evidence.append((what, where, ctx.val(e.get("excerpt") or "")))

    escl_rows: list[tuple[str, str]] = []
    if escl:
        escl_rows.append((ctx.t("協定"), f"{str(escl.get('scheme') or '').upper()} {escl.get('port')}"))
        if escl.get("version"):
            escl_rows.append((ctx.t("eSCL 版本"), ctx.val(escl["version"])))
        if escl.get("model"):
            escl_rows.append((ctx.t("型號"), ctx.val(escl["model"])))
        for k, c in (escl.get("sources") or {}).items():
            if not isinstance(c, dict):
                continue
            parts = [ctx.sep.join(ctx.t(COLOR_NAMES[x]) if x in COLOR_NAMES else str(x)
                                  for x in c.get("colors") or [])]
            if c.get("resolutions"):
                parts.append(", ".join(str(r) for r in c["resolutions"]) + " dpi")
            if c.get("formats"):
                parts.append(", ".join(str(f).upper() for f in c["formats"]))
            escl_rows.append((ctx.t(SOURCE_NAMES[k]) if k in SOURCE_NAMES else str(k),
                              "; ".join(p for p in parts if p)))

    groups = [(ctx.t("位址檢查"), [_step(ctx, s) for s in rep.get("steps") or [] if isinstance(s, dict)])]
    for a in rep.get("attempts") or []:
        if isinstance(a, dict):
            groups.append((f"{str(a.get('scheme') or '').upper()} {a.get('port')}",
                           [_step(ctx, s) for s in a.get("steps") or [] if isinstance(s, dict)]))
    return {
        "locale": locale or "zh-Hant", "ctx": ctx,
        "title": ctx.t("事務機連線測試診斷報告"),
        "note": ctx.t("本報告已遮蔽帳號、密碼與工作階段資訊，可以直接附在問題回報裡。"),
        "info": (ctx.t("報告資訊"), info),
        "result": (ctx.t("測試結果"), result),
        "evidence": (ctx.t("辨識依據"), evidence),
        "escl": (ctx.t("掃描介面（eSCL）"), escl_rows),
        "steps": (ctx.t("逐步診斷"), groups),
    }


# --------------------------------------------------------------------------- 文字

def _mark(ok: Any) -> str:
    return "✓" if ok is True else "✗" if ok is False else "•"


def _step_head(ctx: _Ctx, st: dict) -> str:
    head = f"{_mark(st['ok'])} {st['name']}"
    if st["message"]:
        head += f"{ctx.colon}{st['message']}"
    if st["ms"] is not None:
        head += f"{ctx.lp}{st['ms']} ms{ctx.rp}"
    return head


def to_txt(doc: dict) -> str:
    ctx: _Ctx = doc["ctx"]
    out = [doc["title"], "=" * 40, doc["note"], ""]

    def section(title: str):
        out.extend([title, "-" * 40])

    def rows(rs, indent=""):
        for k, v in rs:
            v = str(v).replace("\n", "\n" + indent + "    ")
            out.append(f"{indent}{k}{ctx.colon}{v}")

    for key in ("info", "result"):
        section(doc[key][0])
        rows(doc[key][1])
        out.append("")
    if doc["evidence"][1]:
        section(doc["evidence"][0])
        for what, where, ex in doc["evidence"][1]:
            out.append(f"- {what}{ctx.colon}{where}{ctx.colon}{ex}")
        out.append("")
    if doc["escl"][1]:
        section(doc["escl"][0])
        rows(doc["escl"][1])
        out.append("")
    section(doc["steps"][0])
    for gtitle, steps in doc["steps"][1]:
        out.extend(["", f"[{gtitle}]"])
        for st in steps:
            out.append(_step_head(ctx, st))
            for ttitle, rs in st["tables"]:
                if ttitle:
                    out.append(f"    {ttitle}")
                rows(rs, "    ")
            for btitle, text in st["blocks"]:
                out.append(f"    {btitle}")
                out.extend("      " + line for line in text.splitlines())
            if st["note"]:
                out.append(f"    {st['note']}")
    return "\n".join(out).rstrip() + "\n"


_MD_SPECIAL = re.compile(r"([\\`*_\[\]<>|!#~&])")


def _md_cell(s: str) -> str:
    """設備給的值放進 Markdown：標記符號一律反斜線跳脫（不會變成連結、粗體、HTML 或拆掉表格），換行改成空白。"""
    return _MD_SPECIAL.sub(r"\\\1", str(s)).replace("\r", "").replace("\n", " ")


def _md_fence(text: str) -> str:
    n = max([3] + [len(m) + 1 for m in re.findall(r"`{3,}", text)])
    return "`" * n + "text\n" + text + "\n" + "`" * n


def to_md(doc: dict) -> str:
    ctx: _Ctx = doc["ctx"]
    t = ctx.t
    out = [f"# {_md_cell(doc['title'])}", "", f"> {_md_cell(doc['note'])}", ""]

    def table(rs):
        out.extend([f"| {t('項目')} | {t('內容')} |", "| --- | --- |"])
        out.extend(f"| {_md_cell(k)} | {_md_cell(v)} |" for k, v in rs)
        out.append("")

    for key in ("info", "result"):
        out.extend([f"## {doc[key][0]}", ""])
        table(doc[key][1])
    if doc["evidence"][1]:
        out.extend([f"## {doc['evidence'][0]}", ""])
        for what, where, ex in doc["evidence"][1]:
            out.append(f"- {_md_cell(what)}{ctx.colon}{_md_cell(where)}{ctx.colon}{_md_cell(ex)}")
        out.append("")
    if doc["escl"][1]:
        out.extend([f"## {doc['escl'][0]}", ""])
        table(doc["escl"][1])
    out.extend([f"## {doc['steps'][0]}", ""])
    for gtitle, steps in doc["steps"][1]:
        out.extend([f"### {_md_cell(gtitle)}", ""])
        for st in steps:
            out.extend([f"#### {_md_cell(_step_head(ctx, st))}", ""])
            for ttitle, rs in st["tables"]:
                if ttitle:
                    out.extend([f"**{_md_cell(ttitle)}**", ""])
                table(rs)
            for btitle, text in st["blocks"]:
                out.extend([f"**{_md_cell(btitle)}**", "", _md_fence(text), ""])
            if st["note"]:
                out.extend([f"_{_md_cell(st['note'])}_", ""])
    return "\n".join(out).rstrip() + "\n"


# --------------------------------------------------------------------------- HTML / PDF

_CSS = """
body { font-family: sans-serif; font-size: 10pt; color: #1f2937; margin: 24px; line-height: 1.45; }
h1 { font-size: 16pt; margin: 0 0 4px 0; }
h2 { font-size: 12.5pt; margin: 18px 0 6px 0; padding-bottom: 3px; border-bottom: 1px solid #d1d5db; }
h3 { font-size: 11pt; margin: 14px 0 4px 0; color: #374151; }
h4 { font-size: 10pt; margin: 10px 0 3px 0; }
.note { color: #4b5563; font-size: 9pt; margin: 0 0 8px 0; }
table { border-collapse: collapse; width: 100%; margin: 2px 0 6px 0; }
th, td { border: 1px solid #e5e7eb; padding: 3px 6px; text-align: left; vertical-align: top; font-size: 9pt; }
th { background-color: #f3f4f6; width: 30%; font-weight: bold; }
td { word-break: break-all; }
pre { white-space: pre-wrap; word-break: break-all; background-color: #f9fafb; border: 1px solid #e5e7eb;
      padding: 4px 6px; font-size: 8.5pt; margin: 2px 0 6px 0; }
.cap { font-size: 9pt; color: #4b5563; margin: 4px 0 2px 0; font-weight: bold; }
.ok { color: #047857; } .fail { color: #b91c1c; } .neutral { color: #6b7280; }
.ms { color: #6b7280; font-weight: normal; }
code { font-family: monospace; background-color: #f3f4f6; }
"""


def _e(s: Any) -> str:
    return html.escape(str(s), quote=True)


def _html_body(doc: dict) -> str:
    ctx: _Ctx = doc["ctx"]
    out = [f"<h1>{_e(doc['title'])}</h1>", f"<p class=\"note\">{_e(doc['note'])}</p>"]

    def table(rs):
        out.append("<table>")
        out.extend(f"<tr><th>{_e(k)}</th><td>{_e(v)}</td></tr>" for k, v in rs)
        out.append("</table>")

    for key in ("info", "result"):
        out.append(f"<h2>{_e(doc[key][0])}</h2>")
        table(doc[key][1])
    if doc["evidence"][1]:
        out.append(f"<h2>{_e(doc['evidence'][0])}</h2><ul>")
        out.extend(f"<li>{_e(what)}{_e(ctx.colon)}{_e(where)}{_e(ctx.colon)}<code>{_e(ex)}</code></li>"
                   for what, where, ex in doc["evidence"][1])
        out.append("</ul>")
    if doc["escl"][1]:
        out.append(f"<h2>{_e(doc['escl'][0])}</h2>")
        table(doc["escl"][1])
    out.append(f"<h2>{_e(doc['steps'][0])}</h2>")
    for gtitle, steps in doc["steps"][1]:
        out.append(f"<h3>{_e(gtitle)}</h3>")
        for st in steps:
            cls = "ok" if st["ok"] is True else "fail" if st["ok"] is False else "neutral"
            if st["ok"] is not None:
                cls += " mk"          # ✓ ✗：PDF 用 Dingbats 畫（見 to_pdf）
            head = f"<span class=\"{cls}\">{_mark(st['ok'])}</span> {_e(st['name'])}"
            if st["message"]:
                head += _e(ctx.colon + st["message"])
            if st["ms"] is not None:
                head += f" <span class=\"ms\">{_e(ctx.lp + str(st['ms']) + ' ms' + ctx.rp)}</span>"
            out.append(f"<h4>{head}</h4>")
            for ttitle, rs in st["tables"]:
                if ttitle:
                    out.append(f"<p class=\"cap\">{_e(ttitle)}</p>")
                table(rs)
            for btitle, text in st["blocks"]:
                out.append(f"<p class=\"cap\">{_e(btitle)}</p><pre>{_e(text)}</pre>")
            if st["note"]:
                out.append(f"<p class=\"note\">{_e(st['note'])}</p>")
    return "\n".join(out)


_HTML_LANG = {"zh-Hant": "zh-Hant-TW", "en": "en", "ja": "ja"}


def to_html(doc: dict) -> str:
    # 獨立檔案、不連外、不跑指令碼：CSP 只開內嵌樣式
    return ("<!doctype html>\n"
            f"<html lang=\"{_e(_HTML_LANG.get(doc['locale'], doc['locale']))}\"><head><meta charset=\"utf-8\">"
            "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; style-src 'unsafe-inline'\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            f"<title>{_e(doc['title'])}</title><style>{_CSS}</style></head>\n<body>\n"
            + _html_body(doc) + "\n</body></html>\n")


#: 報告裡要中日文字型負責的字元（漢字、假名、全形標點）。✓ ✗ 這類符號中日文字型本來就沒有，交給 MuPDF。
_CJK_RANGES = ((0x3000, 0x30FF), (0x3400, 0x4DBF), (0x4E00, 0x9FFF), (0xF900, 0xFAFF), (0xFF00, 0xFFEF))


def _cjk_chars(text: str) -> frozenset:
    return frozenset(ord(ch) for ch in text if any(lo <= ord(ch) <= hi for lo, hi in _CJK_RANGES))


@lru_cache(maxsize=32)
def _cmap_of(path_str: str, mtime: float, idx: int) -> frozenset:
    try:
        from fontTools.ttLib import TTFont
        with TTFont(path_str, fontNumber=idx, lazy=True) as f:
            return frozenset((f.getBestCmap() or {}).keys())
    except Exception:  # noqa: BLE001 —— 讀不到就當作不涵蓋，換下一個
        return frozenset()


def _pick_font(text: str, locale: Optional[str]) -> Optional[tuple]:
    """挑一個**涵蓋報告裡每個中日文字**的字型 `(path, idx)`。

    預設是最佳的繁中黑體；日文介面遇到泛中日韓的 `.ttc`（Noto CJK）改用其中的日文子字型
    （字形才是日文寫法）。缺字時往下找其他已安裝的中日文字型 —— Windows 的 Noto Sans TC
    沒有「済」這類日文字形，不找的話 MuPDF 會把整支後備字型（3.5 MB）嵌進來。
    都不涵蓋就回第一個，由 MuPDF 補缺的字。"""
    from . import font_catalog
    cands: list[tuple] = []
    best = font_catalog.best_cjk_path("sans", "traditional")
    if best:
        path, idx = best
        if (locale or "").startswith("ja") and path.suffix.lower() == ".ttc":
            idx = font_catalog._ttc_index_for(str(path), font_catalog._mtime(path), "japanese")
        cands.append((path, idx))
    others = [f for f in font_catalog.list_fonts()
              if f.get("path") and f.get("category") in ("custom", "taiwan", "free-cjk", "cjk")]
    # 黑體優先、粗體最後（正文用粗體很怪）
    others.sort(key=lambda f: (f.get("style") != "sans", "bold" in Path(f["path"]).name.lower()))
    for f in others:
        c = (Path(f["path"]), int(f.get("idx") or 0))
        if c not in cands:
            cands.append(c)
    need = _cjk_chars(text)
    for path, idx in cands:
        if need <= _cmap_of(str(path), font_catalog._mtime(path), idx):
            return (path, idx)
    return cands[0] if cands else None


def to_pdf(doc: dict) -> bytes:
    """Story 排版成 A4。中文字型只嵌用到的字（整支 Noto CJK 十幾 MB）。"""
    import fitz
    from . import font_catalog
    body = _html_body(doc)
    archive = fitz.Archive()
    # MuPDF 的表格不認百分比寬度（欄名會被擠成一字一行），PDF 另外給固定寬度（加上內距＝版面寬 515pt）；
    # 欄名底色拿掉：MuPDF 會在表格上方多畫一塊欄名底色的殘影
    css = _CSS.replace("margin: 24px;", "margin: 0;") + \
        "\nth { width: 120pt; background-color: transparent; } td { width: 360pt; }\n"
    text = html.unescape(re.sub(r"<[^>]+>", "", body)) + doc["title"]
    best = _pick_font(text, doc.get("locale"))
    if best:
        # 只寫 `cjk` 一個字型：MuPDF 不會往字型清單的第二個找，寫成「sans-serif, cjk」
        # 中文會落到它內建的後備字型（整支 3.5 MB 嵌進去）。`pre` 要另外寫一次，
        # `*` 蓋不過內建的等寬字型（中文一樣落到後備字型）
        fontfile, buf = font_catalog.embeddable_font(best[0], best[1], text=text, layout=False)
        if buf is None and fontfile:
            with open(fontfile, "rb") as fh:
                buf = fh.read()
        if buf:
            archive.add(buf, "cjk.ttf")
            css = "@font-face { font-family: cjk; src: url(cjk.ttf); }\n" + css + \
                "\n* { font-family: cjk; }\npre, code { font-family: cjk; }\n"
    # ✓ ✗ 中日文字型多半沒有：交給 MuPDF 自己找的話會嵌進整支後備字型（Windows 上是 3.5 MB 的 Droid，
    # Linux 是 375 KB 的 Noto Sans Symbols2）。PDF 標準字型 Dingbats 本來就有這兩個符號，只嵌 25 KB
    css += "\n.mk { font-family: Dingbats; }\n"
    story = fitz.Story(html=f"<html><body>{body}</body></html>", user_css=css, archive=archive)
    out = io.BytesIO()
    writer = fitz.DocumentWriter(out)
    page = fitz.paper_rect("a4")
    where = page + (40, 40, -40, -48)
    more, n = True, 0
    while more and n < 500:
        dev = writer.begin_page(page)
        more, _ = story.place(where)
        story.draw(dev)
        writer.end_page()
        n += 1
    writer.close()
    data = out.getvalue()
    # 頁碼與標題資訊：Story 沒有頁尾，另外補
    with fitz.open("pdf", data) as d:
        d.set_metadata({"title": doc["title"], "creator": "Jason Tools", "producer": "Jason Tools"})
        total = d.page_count
        for i, p in enumerate(d):
            p.insert_text((page.width - 80, page.height - 24), f"{i + 1} / {total}", fontsize=8,
                          color=(0.42, 0.45, 0.5))
        return d.tobytes(garbage=3, deflate=True)


# --------------------------------------------------------------------------- 入口

_RENDER: dict[str, Callable[[dict], Any]] = {"txt": to_txt, "md": to_md, "html": to_html, "pdf": to_pdf}


def render(report: dict, fmt: str, locale: Optional[str] = None) -> tuple[bytes, str, str]:
    """回 `(內容, media_type, 副檔名)`。`fmt` 不在 `FORMATS` 裡丟 `ValueError`。"""
    if fmt not in FORMATS:
        raise ValueError(fmt)
    media, ext = FORMATS[fmt]
    if fmt == "json":
        return json.dumps(report, ensure_ascii=False, indent=2).encode("utf-8"), media, ext
    data = _RENDER[fmt](build(report, locale))
    return (data if isinstance(data, bytes) else data.encode("utf-8")), media, ext


def filename(host: str, stamp: str, ext: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9.\-]", "_", str(host or "mfp"))[:80] or "mfp"
    return f"mfp-diagnostic-{safe}-{stamp}.{ext}"
