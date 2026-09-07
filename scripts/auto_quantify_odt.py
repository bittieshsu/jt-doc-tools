"""自動量化 pdf-to-office jtdt-reform 對齊度。

對 temp/ 下每份 PDF：
  1. convert → ODT → OxOffice render → result PDF
  2. 量「原 PDF vs render PDF」對齊度，三個 metric：
     a) page_match：頁數相符 (0 / 1)
     b) text_recall：原 PDF 文字內容多少比例出現在 render 文字層 (0-1)
     c) y_dev_pt：對應 text line 的 Y 偏差中位數 (pt)
     d) y_dev_p95：Y 偏差 95th percentile (pt)
3. 輸出表格，總分 = page_match × 50 + text_recall × 30 + (1 - min(1, y_dev_pt/30)) × 20

設計重點：metric 是 RENDER vs ORIGINAL 的 PDF 物件層次比對，不需 OCR；
直接用 PyMuPDF 抓 text 與 bbox。

用法：source .venv/bin/activate && python scripts/auto_quantify_odt.py [--only filename]
"""
from __future__ import annotations
import argparse
import shutil
import signal
import subprocess
import tempfile
from pathlib import Path

import fitz


def _alarm_handler(_sig, _frame):
    raise TimeoutError("convert hung")


def render_odt_to_pdf(odt_path: Path, work: Path) -> Path | None:
    profile = work / "_so_profile"
    profile.mkdir(exist_ok=True)
    candidates = [
        "/Applications/OxOffice.app/Contents/MacOS/soffice",
        "/Applications/LibreOffice.app/Contents/MacOS/soffice",
        shutil.which("soffice"),
    ]
    soffice = next((c for c in candidates if c and Path(c).exists()), None)
    if not soffice:
        return None
    try:
        subprocess.run(
            [soffice, "--headless",
             f"-env:UserInstallation=file://{profile}",
             "--convert-to", "pdf", "--outdir", str(work), str(odt_path)],
            capture_output=True, timeout=180,
        )
    except subprocess.TimeoutExpired:
        return None
    rendered = work / (odt_path.stem + ".pdf")
    return rendered if rendered.exists() else None


def _collect_text_lines(pdf_path: Path) -> list[tuple[str, float, float]]:
    """回 [(text_normalized, y_top, x_left), ...] 每行純文字 + bbox。"""
    out = []
    d = fitz.open(str(pdf_path))
    for page in d:
        for blk in page.get_text("dict")["blocks"]:
            if "lines" not in blk:
                continue
            for ln in blk["lines"]:
                txt = "".join(sp.get("text", "") for sp in ln.get("spans", []))
                txt = txt.strip()
                if not txt or len(txt) < 1:
                    continue
                bbox = ln.get("bbox", (0, 0, 0, 0))
                out.append((txt, float(bbox[1]), float(bbox[0])))
    return out


def _quantify(orig_pdf: Path, render_pdf: Path) -> dict:
    """量化原 vs render 對齊度。"""
    od = fitz.open(str(orig_pdf))
    rd = fitz.open(str(render_pdf))
    o_pages = len(od)
    r_pages = len(rd)
    page_match = 1.0 if o_pages == r_pages else max(0.0, 1.0 - abs(o_pages - r_pages) * 0.5)

    o_lines = _collect_text_lines(orig_pdf)
    r_lines = _collect_text_lines(render_pdf)
    # text recall: 對 orig 每行 text，看 render 是否含
    # 改 v1.9.4：用 multiset containment 比對（PyMuPDF 對 multi-span line 抽出 span
    # 順序可能不同 → 純 substring match 會誤判 missing。改檢查每個 char 是否在
    # render 全文中出現足夠次數。
    if o_lines:
        r_text_concat = "".join("".join(t.split()) for t, _, _ in r_lines)
        from collections import Counter
        r_chars = Counter(r_text_concat)
        hits = 0
        for t, _, _ in o_lines:
            tnorm = "".join(t.split())
            if not tnorm:
                continue
            t_chars = Counter(tnorm)
            # 若所有 char count 都被 render Counter 涵蓋 → 視為 present
            if all(r_chars[ch] >= cnt for ch, cnt in t_chars.items()):
                hits += 1
        text_recall = hits / len(o_lines)
    else:
        text_recall = 1.0

    # y 偏差：對每行 orig text，找 render 內同 text 對應行的 y 差
    # 過濾 ≤ 5 char 的 text — 短字常重複出現會誤配對；用 (X dist + Y dist) 雙
    # 軸權重 + 同一 unique key 才納入計算
    y_diffs = []
    r_index = {}
    for t, y, x in r_lines:
        key = "".join(t.split())
        if len(key) >= 6:
            r_index.setdefault(key, []).append((y, x))
    for t, oy, ox in o_lines:
        key = "".join(t.split())
        if len(key) < 6:
            continue
        if key in r_index:
            # 配對：用 X+Y combined distance，避免 X 相近但 Y 差很多的誤配
            best = min(r_index[key],
                       key=lambda yx: abs(yx[1] - ox) + abs(yx[0] - oy) * 0.3)
            y_diffs.append(abs(best[0] - oy))
    y_diffs.sort()
    if y_diffs:
        n = len(y_diffs)
        median = y_diffs[n // 2]
        p95 = y_diffs[max(0, min(n - 1, int(n * 0.95)))]
    else:
        median = float("nan")
        p95 = float("nan")

    score = (page_match * 50.0
             + text_recall * 30.0
             + (1.0 - min(1.0, (median if y_diffs else 30) / 30.0)) * 20.0)
    return {
        "o_pages": o_pages,
        "r_pages": r_pages,
        "page_match": page_match,
        "text_recall": text_recall,
        "y_med": median,
        "y_p95": p95,
        "score": score,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    ap.add_argument("--temp", default="temp")
    args = ap.parse_args()
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from app.tools.pdf_to_office.engines.jtdt_reform import convert_via_jtdt_reform_to_odt

    temp_dir = Path(args.temp)
    if args.only:
        pdfs = [temp_dir / f for f in [args.only] if (temp_dir / f).exists()]
    else:
        pdfs = sorted(temp_dir.glob("*.pdf"))
    signal.signal(signal.SIGALRM, _alarm_handler)
    results = []
    print(f"== auto-quantify on {len(pdfs)} PDFs ==", flush=True)
    for pdf in pdfs:
        signal.alarm(180)
        try:
            with tempfile.TemporaryDirectory() as td:
                work = Path(td)
                odt = work / (pdf.stem + ".odt")
                convert_via_jtdt_reform_to_odt(pdf, odt)
                if not odt.exists():
                    print(f"  CONVERT-FAIL {pdf.name}", flush=True)
                    continue
                rendered = render_odt_to_pdf(odt, work)
                if rendered is None:
                    print(f"  RENDER-FAIL {pdf.name}", flush=True)
                    continue
                m = _quantify(pdf, rendered)
                results.append((pdf.name, m))
                tag = "✓" if m["score"] >= 80 else ("△" if m["score"] >= 60 else "✗")
                print(f"  {tag} {pdf.name[:40]:40s} score={m['score']:5.1f}  "
                      f"page={m['o_pages']}→{m['r_pages']}  text={m['text_recall']:5.1%}  "
                      f"y_med={m['y_med']:5.1f}pt  y_p95={m['y_p95']:5.1f}pt", flush=True)
        except TimeoutError:
            print(f"  TIMEOUT {pdf.name}", flush=True)
        signal.alarm(0)
    if results:
        scores = [m["score"] for _, m in results]
        page_ok = sum(1 for _, m in results if m["page_match"] >= 1.0)
        print(f"\n=== summary ===")
        print(f"  avg score: {sum(scores)/len(scores):.1f} / 100")
        print(f"  page match: {page_ok}/{len(results)}")
        print(f"  text recall avg: {sum(m['text_recall'] for _, m in results)/len(results):.1%}")
        y_meds = [m['y_med'] for _, m in results if m['y_med'] == m['y_med']]
        if y_meds:
            print(f"  y_med avg: {sum(y_meds)/len(y_meds):.1f}pt")


if __name__ == "__main__":
    main()
