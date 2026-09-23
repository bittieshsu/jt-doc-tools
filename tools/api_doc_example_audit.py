r"""照 `github/API.md` 的每一條 curl 範例實際呼叫一遍。

    .venv/bin/python tools/api_doc_example_audit.py

## 這支在守什麼

既有的兩支測試管**對照層**（端點有沒有寫進文件、幾支挑出來的參數對不對），
都看不到「文件寫的那條指令整條送出去會怎樣」。v1.15.34 第一次這樣跑 82 條，
抓到 `/admin/api/llm/test-connection` 的範例根本沒有 body，而端點
`await request.json()` 對空 body 丟例外 → 使用者看到
`400 Invalid JSON body`（看起來像「你送錯東西」，其實是我們沒寫參數）。

## 怎麼判讀輸出

| 回應 | 意思 |
|---|---|
| 2xx | 好 |
| 503 | **這台機器**缺相依（soffice / OCR），不是文件的問題 |
| 其他 4xx / 5xx | **要人看**：多半是素材不足（空白 PDF 抽不到圖片、假統編查不到），也可能是真的文件與程式不一致 |

素材是合成的（空白 PDF、純色圖），所以「找不到內容」類的 404 / 422 是預期的；
會動到真實資料的（統編、job id、entity id）文件本來就寫佔位值。
**判準是看 detail 說的是「你的檔案裡沒有那個東西」還是「你送的參數我不認得」**
—— 後者才是要修的。

跑完 `temp/api-audit/results.json` 會有每一條的完整結果（相對於**專案根**，在公開樹上就是 clone 的根目錄；兩層 `.gitignore` 都擋著 `temp/`，不會被提交）。

需要 soffice；沒有 AVX2 的機器跑到 pdf-ocr 那條會 core dump（那是 CPU 不是
這支程式，見 CLAUDE.md v1.15.4），結果檔在那之前就已經寫好了。
"""
from __future__ import annotations

import io
import json
import os
import pathlib
import re
import shlex
import shutil
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.repo_paths import public_root   # noqa: E402

_DATA = pathlib.Path(tempfile.mkdtemp(prefix="apiaudit-data-"))
os.environ["JTDT_DATA_DIR"] = str(_DATA)
os.environ["JTDT_CSRF_DISABLE"] = "1"

import fitz                                        # noqa: E402
from fastapi.testclient import TestClient          # noqa: E402

OUT_DIR = ROOT / "temp" / "api-audit"

# --------------------------------------------------------------- 解析 API.md



def _blocks(md: str) -> list[tuple[int, str]]:
    """回 (起始行號, 區塊內容) 的 ```bash / ```sh 圍籬。"""
    out = []
    lines = md.splitlines()
    i = 0
    while i < len(lines):
        m = re.match(r"^```(bash|sh|shell)\s*$", lines[i])
        if not m:
            i += 1
            continue
        start = i + 1
        j = i + 1
        buf = []
        while j < len(lines) and not lines[j].startswith("```"):
            buf.append(lines[j])
            j += 1
        out.append((start + 1, "\n".join(buf)))
        i = j + 1
    return out


def _commands(block: str) -> list[str]:
    """把續行接起來，切出每一條 curl 指令（去掉註解行與 pipe 之後的東西）。"""
    joined = []
    cur = ""
    for raw in block.splitlines():
        line = raw.rstrip()
        if not cur and (not line.strip() or line.lstrip().startswith("#")):
            continue
        if line.endswith("\\"):
            cur += line[:-1] + " "
            continue
        cur += line
        if cur.count('"') % 2 or cur.count("'") % 2:
            # 引號還沒收 —— 值裡面有真的換行（markdown 內容就是這樣寫的）
            cur += "\n"
            continue
        joined.append(cur)
        cur = ""
    if cur:
        joined.append(cur)
    return [c for c in joined if c.lstrip().startswith("curl")]


def parse(cmd: str) -> dict:
    # 砍掉 pipe 之後（| jq）與 shell 重導
    body = re.split(r"\s\|\s", cmd)[0]
    try:
        toks = shlex.split(body)
    except ValueError as e:
        return {"error": f"shlex: {e}", "cmd": cmd}
    method = "GET"
    url = None
    files: list[tuple[str, str]] = []
    data: list[tuple[str, str]] = []
    headers: dict[str, str] = {}
    raw_body = None
    i = 1
    while i < len(toks):
        t = toks[i]
        if t == "-X":
            method = toks[i + 1].upper(); i += 2; continue
        if t in ("-F", "--form", "--form-string"):
            kv = toks[i + 1]
            k, _, v = kv.partition("=")
            (files if v.startswith("@") else data).append((k, v.lstrip("@")))
            i += 2; continue
        if t in ("-d", "--data", "--data-raw", "--data-binary"):
            raw_body = toks[i + 1]
            if method == "GET":
                method = "POST"
            i += 2; continue
        if t in ("-H", "--header"):
            k, _, v = toks[i + 1].partition(":")
            headers[k.strip()] = v.strip()
            i += 2; continue
        if t in ("--output", "-o", "-T"):
            i += 2; continue
        if t.startswith("-"):
            i += 1; continue
        if t.startswith("http"):
            url = t
        i += 1
    if url is None:
        # 文件也有 `"$API/api/jobs/$JOB/download"` 這種寫法（教人串起來的那節）。
        # 那不是解析失敗 —— 取 `$API` 之後的路徑就好，`$JOB` 由呼叫端從前一條
        # 的回應接。**寫成「解析不了」會讓那一條從此不再被檢查。**
        for tok in toks[1:]:
            if "/api/" in tok or "/tools/" in tok:
                url = "http://localhost" + tok[tok.index("/", tok.find("$") + 1):] \
                    if tok.startswith("$") else tok
                break
    if url is None:
        return {"error": "找不到 URL", "cmd": cmd}
    if method == "GET" and (files or data):
        method = "POST"
    path = re.sub(r"^https?://[^/]+", "", url)
    return {"method": method, "path": path, "files": files, "data": data,
            "headers": headers, "raw_body": raw_body, "cmd": cmd}



# ------------------------------------------------------------------- 素材


def _pdf(n: int = 3) -> bytes:
    doc = fitz.open()
    for i in range(n):
        pg = doc.new_page(width=595, height=842)
        pg.insert_text((72, 100 + 20 * i), f"Page {i + 1} sample text", fontsize=14)
    buf = io.BytesIO(); doc.save(buf); doc.close()
    return buf.getvalue()


def _png() -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (600, 400), (240, 240, 240)).save(buf, "PNG")
    return buf.getvalue()


def _jpg() -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (600, 400), (230, 230, 230)).save(buf, "JPEG")
    return buf.getvalue()


def _docx() -> bytes:
    from docx import Document
    d = Document(); d.add_paragraph("API audit sample paragraph.")
    buf = io.BytesIO(); d.save(buf); return buf.getvalue()


_CACHE: dict[str, bytes] = {}


def _via_soffice(ext: str) -> bytes:
    """用 soffice 從 docx 轉出真的 odt；pptx / odp 用 flat ODF 簡報當來源。"""
    if ext in _CACHE:
        return _CACHE[ext]
    from app.core import office_convert as oc
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="apiaudit-fx-"))
    try:
        if ext == "odt":
            src = tmp / "s.docx"; src.write_bytes(_docx())
            dst = tmp / "s.odt"; oc.convert_to_odt(src, dst)
        else:  # pptx / odp
            src = tmp / "s.fodp"
            src.write_text(_FODP, encoding="utf-8")
            dst = tmp / f"s.{ext}"
            filt = {"pptx": "Impress MS PowerPoint 2007 XML",
                    "odp": "impress8"}[ext]
            oc.convert_with_filter(src, dst, ext, filt)
        _CACHE[ext] = dst.read_bytes()
        return _CACHE[ext]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


_FODP = """<?xml version="1.0" encoding="UTF-8"?>
<office:document xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
 xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0"
 xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"
 office:version="1.3"
 office:mimetype="application/vnd.oasis.opendocument.presentation">
 <office:body><office:presentation>
  <draw:page draw:name="page1">
   <draw:frame svg:width="20cm" svg:height="3cm" svg:x="2cm" svg:y="2cm"
     xmlns:svg="urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0">
    <draw:text-box><text:p>API audit slide</text:p></draw:text-box>
   </draw:frame>
  </draw:page>
 </office:presentation></office:body>
</office:document>
"""


def _xlsx() -> bytes:
    import openpyxl
    wb = openpyxl.Workbook(); wb.active["A1"] = "audit"
    buf = io.BytesIO(); wb.save(buf); return buf.getvalue()


def fixture(filename: str) -> tuple[bytes, str]:
    ext = pathlib.Path(filename).suffix.lower().lstrip(".")
    if ext == "pdf":
        return _pdf(), "application/pdf"
    if ext == "png":
        return _png(), "image/png"
    if ext in ("jpg", "jpeg"):
        return _jpg(), "image/jpeg"
    if ext == "docx":
        return _docx(), "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    if ext == "xlsx":
        return _xlsx(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    if ext in ("odt", "pptx", "odp"):
        return _via_soffice(ext), "application/octet-stream"
    if ext in ("md", "markdown"):
        return b"# Title\n\nHello **world**.\n", "text/markdown"
    if ext in ("txt", "csv"):
        return b"a,b\n1,2\n", "text/plain"
    return b"placeholder", "application/octet-stream"


# ---------- 執行 ----------

#: 合成素材 / 佔位值必然會回的 4xx —— 鍵是**正規化後的路徑**，值是為什麼。
#:
#: 這張表是為了讓輸出保持可讀。誤報一多這份檢查就會被當雜訊忽略
#: （本專案在用詞檢查上踩過），所以「預期的」與「要看的」要分開印。
_EXPECTED: dict[str, str] = {
    "/tools/pdf-to-office/preview/{}/orig/{}": "作業還沒跑完（文件範例是接在 job 完成之後）",
    "/tools/pdf-to-office/preview/{}/result/{}": "同上",
    "/tools/scan-merge/api/scan-merge": "合成的空白 PDF 沒有內容區塊",
    "/tools/pdf-extract-images/api/pdf-extract-images": "合成的空白 PDF 裡沒有圖片",
    "/tools/pdf-attachments/api/pdf-attachments": "合成的空白 PDF 裡沒有附件",
    "/tools/vat-lookup/api/vat-lookup": "文件用的是佔位統編",
    "/api/vat-lookup/{}": "同上",
    "/tools/submission-check/api/self-entities/{}": "文件用的是佔位 entity id",
    "/api/llm-review": "全新資料目錄沒有啟用 LLM",
    "/api/jobs/{}": "文件用的是佔位 job id",
    "/api/jobs/{}/download": "同上",
    "/api/jobs/{}/download-png": "同上",
}

_PARAM_RE = re.compile(r"/(?:[0-9a-f]{8,}|abc123|12345678|\d+)(?=/|$)")


def _norm_path(path: str) -> str:
    return _PARAM_RE.sub("/{}", path)


def _parse_md() -> list[dict]:
    """解析出所有 curl 範例。**解析不了的要出聲** —— 安靜跳過就會變成
    「掃 0 條還全綠」，本專案在逐檔檢查上踩過這個坑不只一次。"""
    md = (public_root(ROOT) / "API.md").read_text(encoding="utf-8")
    out: list[dict] = []
    unparsed: list[dict] = []
    for lineno, block in _blocks(md):
        for cmd in _commands(block):
            p = parse(cmd)
            p["line"] = lineno
            (unparsed if "error" in p else out).append(p)
    print(f"curl 範例：{len(out)} 條可解析、{len(unparsed)} 條解析不了",
          file=sys.stderr)
    for u in unparsed:
        print("  !!", u["error"], "|", u["cmd"][:90], file=sys.stderr)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "examples.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    return out


def main() -> int:
    examples = _parse_md()
    import app.main as app_main
    client = TestClient(app_main.app)

    last_job = None
    rows = []
    for e in examples:
        path = e["path"]
        if "$" in path:
            if last_job:
                path = re.sub(r"\$[A-Za-z_]+", last_job, path, count=1)
            else:
                rows.append({**e, "status": "skip", "note": "沒有可接的 job_id"})
                continue
        files = []
        for k, fn in e["files"]:
            try:
                blob, ctype = fixture(fn)
            except Exception as ex:  # noqa: BLE001
                rows.append({**e, "status": "fixture-fail", "note": f"{fn}: {ex}"})
                files = None
                break
            files.append((k, (pathlib.Path(fn).name, blob, ctype)))
        if files is None:
            continue
        data = dict(e["data"])
        headers = {k: v for k, v in e["headers"].items()
                   if k.lower() not in ("authorization",)}
        kw: dict = {"headers": headers}
        if files:
            kw["files"] = files
            if data:
                kw["data"] = data
        elif e["raw_body"]:
            if "json" in headers.get("Content-Type", "").lower():
                try:
                    kw["json"] = json.loads(e["raw_body"])
                except Exception:
                    kw["content"] = e["raw_body"]
            else:
                kw["content"] = e["raw_body"]
        elif data:
            kw["data"] = data
        try:
            r = client.request(e["method"], path, **kw)
        except Exception as ex:  # noqa: BLE001
            rows.append({**e, "status": "raise", "note": f"{type(ex).__name__}: {ex}"})
            continue
        body = ""
        ctype = r.headers.get("content-type", "")
        if "json" in ctype or "text" in ctype:
            body = r.text[:300]
            m = re.search(r'"job_id"\s*:\s*"([0-9a-f]{8,})"', r.text)
            if m:
                last_job = m.group(1)
        rows.append({**e, "status": r.status_code, "resolved": path,
                     "ctype": ctype, "note": body})

    (OUT_DIR / "results.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")

    bad = [r for r in rows if isinstance(r["status"], int) and r["status"] >= 400]
    env = [r for r in bad if r["status"] == 503]
    rest = [r for r in bad if r["status"] != 503]
    known = [r for r in rest
             if _norm_path(r.get("resolved", r["path"])) in _EXPECTED]
    hard = [r for r in rest
            if _norm_path(r.get("resolved", r["path"])) not in _EXPECTED]
    other = sum(1 for r in rows if not isinstance(r["status"], int))
    print(f"跑了 {len(rows)} 條：2xx/3xx {len(rows) - len(bad) - other}"
          f"、缺相依 503 {len(env)}、素材 / 佔位值 {len(known)}、"
          f"**要看的 {len(hard)}**、其他 {other}")
    for r in hard:
        print(f"  [{r['status']}] L{r['line']} {r['method']} {r.get('resolved', r['path'])}")
        print(f"        {r['note'][:200]}")
    if known:
        print(f"  （素材 / 佔位值造成的 {len(known)} 條，預期如此）")
        for r in known:
            why = _EXPECTED[_norm_path(r.get("resolved", r["path"]))]
            print(f"    [{r['status']}] {r['method']} "
                  f"{_norm_path(r.get('resolved', r['path']))} — {why}")
    for r in rows:
        if not isinstance(r["status"], int):
            print(f"  ({r['status']}) L{r['line']} {r['method']} {r['path']} — {r['note'][:120]}")
    shutil.rmtree(_DATA, ignore_errors=True)

    # **不要等行程自己結束。** 範例裡有幾支會送出背景作業（pdf-ocr、
    # office-convert），那些工作執行緒會讓解譯器一直活著；而在沒有 AVX2 的
    # 機器上 EasyOCR 載進來就 SIGILL（CPU 不是程式，見 CLAUDE.md v1.15.4），
    # 於是這支稽核工具看起來像「跑完之後掛掉」。結果早就印完也寫檔了。
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)


if __name__ == "__main__":
    raise SystemExit(main())
