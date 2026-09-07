"""自動驗證 jtdt-reform：對 temp/*.pdf 跑 convert → OxOffice render → 量測 cell widths。

User SOP 要求：不要靠他每次截圖，自己跑 + 自己看 + 自己修。

對每份 PDF：
  1. convert_via_jtdt_reform → docx
  2. OxOffice headless render docx → PDF
  3. fitz 讀 PDF 找 table grid (vertical lines) → 量實際 col widths
  4. 跟 docx 內 gridCol 設定比對 → 差距 > 30% 視為 OxOffice 不認 fixed layout
  5. 印 summary 表

用法：source .venv/bin/activate && python scripts/auto_verify_pdf_to_office.py
"""
from __future__ import annotations
import sys
import shutil
import signal
import subprocess
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _alarm_handler(_sig, _frame):
    raise TimeoutError("convert hung")


def render_docx_to_pdf(docx_path: Path, work: Path) -> Path | None:
    """用 OxOffice headless 把 docx 轉 PDF。"""
    profile = work / "_so_profile"
    profile.mkdir(exist_ok=True)
    candidates = [
        "/Applications/OxOffice.app/Contents/MacOS/soffice",
        "/Applications/LibreOffice.app/Contents/MacOS/soffice",
        shutil.which("soffice"),
    ]
    soffice = next((c for c in candidates if c and Path(c).exists()), None)
    if not soffice:
        print(f"  [no soffice]", flush=True)
        return None
    try:
        subprocess.run(
            [soffice, "--headless",
             f"-env:UserInstallation=file://{profile}",
             "--convert-to", "pdf", "--outdir", str(work), str(docx_path)],
            capture_output=True, timeout=120,
        )
    except subprocess.TimeoutExpired:
        return None
    rendered = work / (docx_path.stem + ".pdf")
    return rendered if rendered.exists() else None


def measure_table_cols_from_pdf(pdf_path: Path) -> tuple[float, list[float]]:
    """從 render 後 PDF 抽 vertical lines，cluster 得實際 col 邊界 (pt)。
    回 (table_left_x_pt, col_widths_pt)。"""
    import fitz
    doc = fitz.open(str(pdf_path))
    page = doc.load_page(0)
    vs = []
    for d in page.get_drawings():
        for it in d.get("items", []):
            kind = it[0] if it else ""
            if kind == "l":
                p0, p1 = it[1], it[2]
                if abs(p1.x - p0.x) < 1.0 and abs(p1.y - p0.y) > 50:
                    vs.append((p0.x + p1.x) / 2.0)
            elif kind == "re":
                r = it[1]
                if r.x1 - r.x0 < 2 and r.y1 - r.y0 > 50:
                    vs.append((r.x0 + r.x1) / 2.0)
    doc.close()
    if not vs:
        return 0.0, []
    vs = sorted(vs)
    # cluster ±2pt
    out = [vs[0]]
    for v in vs[1:]:
        if v - out[-1] > 2.0:
            out.append(v)
    if len(out) < 2:
        return out[0] if out else 0.0, []
    widths = [out[i + 1] - out[i] for i in range(len(out) - 1)]
    return out[0], widths


def get_docx_gridcols(docx_path: Path) -> tuple[float, list[float]]:
    """從 docx XML 抽 left margin + gridCol widths (pt)。"""
    import zipfile
    from xml.etree import ElementTree as ET
    W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    with zipfile.ZipFile(docx_path, "r") as z:
        with z.open("word/document.xml") as f:
            tree = ET.parse(f)
    root = tree.getroot()
    left_margin_pt = 0.0
    for pgMar in root.iter(f"{W}pgMar"):
        try:
            left_margin_pt = float(pgMar.get(f"{W}left", "0")) / 20.0
        except Exception:
            pass
        break
    col_widths_pt = []
    for tbl in root.iter(f"{W}tbl"):
        grid = tbl.find(f"{W}tblGrid")
        if grid is not None:
            for g in grid.findall(f"{W}gridCol"):
                w = g.get(f"{W}w")
                if w:
                    col_widths_pt.append(float(w) / 20.0)
            break
    return left_margin_pt, col_widths_pt


def main():
    from app.tools.pdf_to_office.engines.jtdt_reform import convert_via_jtdt_reform

    temp_dir = Path(__file__).resolve().parent.parent / "temp"
    pdfs = sorted(temp_dir.glob("*.pdf"))
    print(f"== auto verify on {len(pdfs)} PDFs ==\n", flush=True)

    signal.signal(signal.SIGALRM, _alarm_handler)
    summary = []
    for pdf in pdfs:
        signal.alarm(60)
        try:
            with tempfile.TemporaryDirectory() as td:
                work = Path(td)
                docx = work / (pdf.stem + ".docx")
                try:
                    res = convert_via_jtdt_reform(pdf, docx)
                except Exception as e:
                    print(f"  CONVERT-ERR {pdf.name}: {e}", flush=True)
                    summary.append((pdf.name, "convert-err", 0, [], 0, []))
                    continue
                if not res.get("ok") or not docx.exists():
                    print(f"  CONVERT-FAIL {pdf.name}: {res.get('error')}", flush=True)
                    summary.append((pdf.name, "convert-fail", 0, [], 0, []))
                    continue

                doc_left_pt, doc_widths_pt = get_docx_gridcols(docx)

                rendered_pdf = render_docx_to_pdf(docx, work)
                if rendered_pdf is None:
                    print(f"  RENDER-FAIL {pdf.name}", flush=True)
                    summary.append((pdf.name, "render-fail", doc_left_pt, doc_widths_pt, 0, []))
                    continue
                render_left_pt, render_widths_pt = measure_table_cols_from_pdf(rendered_pdf)
                # 判定：第一 col 寬度差超過 30% → OxOffice 不認 fixed
                ok = False
                if doc_widths_pt and render_widths_pt:
                    # 取共有的 col 數比較
                    n = min(len(doc_widths_pt), len(render_widths_pt))
                    diffs = [abs(doc_widths_pt[i] - render_widths_pt[i]) / max(1.0, doc_widths_pt[i])
                             for i in range(n)]
                    max_diff = max(diffs) if diffs else 0
                    ok = max_diff < 0.30
                    status = f"diff={max_diff:.1%}" + (" OK" if ok else " FAIL")
                else:
                    status = "no-grid"
                print(f"  {pdf.name}: doc={[round(w,1) for w in doc_widths_pt]} render={[round(w,1) for w in render_widths_pt]} → {status}", flush=True)
                summary.append((pdf.name, status, doc_left_pt, doc_widths_pt,
                                 render_left_pt, render_widths_pt))
        except TimeoutError:
            print(f"  TIMEOUT {pdf.name}", flush=True)
            summary.append((pdf.name, "timeout", 0, [], 0, []))
        signal.alarm(0)

    print("\n=== summary ===")
    ok = sum(1 for s in summary if "OK" in s[1])
    print(f"  {ok} / {len(summary)} OxOffice renders within 30% of docx grid widths")


if __name__ == "__main__":
    main()
