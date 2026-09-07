"""自動驗證 jtdt-reform → ODT pipeline：跑 temp/15 PDF，OxOffice render，
量 cell widths + 對照圖。

User SOP（PLAN_PDF_TO_OFFICE_ODT.md §5）：不靠 user 截圖，自己跑+自己看+自己修。

對每份 PDF：
  1. convert_via_jtdt_reform_to_odt → .odt
  2. OxOffice headless render .odt → .pdf
  3. fitz 讀 PDF 找 table grid → 量實際 col widths
  4. 與 ODT 內 column widths (expected) 比對
  5. 產 compare PNG（原 PDF / ODT-render 並排）

用法：source .venv/bin/activate && python scripts/auto_verify_odt.py
"""
from __future__ import annotations
import sys
import shutil
import signal
import subprocess
import tempfile
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


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
            capture_output=True, timeout=120,
        )
    except subprocess.TimeoutExpired:
        return None
    rendered = work / (odt_path.stem + ".pdf")
    return rendered if rendered.exists() else None


def measure_table_cols_from_pdf(pdf_path: Path) -> list[float]:
    """量 PDF 內主表（最寬 table 區域）的 vertical line X 位置 → col widths。

    改進（v1.8.83）：把所有 vertical strokes 收進來（不再只限 height>50pt），
    然後找「最寬連續 vertical 線群」當主表（避免 1-row table 被 height 門檻排除）。
    """
    import fitz
    doc = fitz.open(str(pdf_path))
    page = doc.load_page(0)
    # 收所有 vertical 線 (含位置 + 高度)
    raw: list[tuple[float, float]] = []  # (x_center, length)
    for d in page.get_drawings():
        for it in d.get("items", []):
            kind = it[0] if it else ""
            if kind == "l":
                p0, p1 = it[1], it[2]
                dx, dy = abs(p1.x - p0.x), abs(p1.y - p0.y)
                if dx < 1.0 and dy > 5:  # 任何 ≥ 5pt 的 vertical 都收
                    raw.append(((p0.x + p1.x) / 2.0, dy))
            elif kind == "re":
                r = it[1]
                if r.x1 - r.x0 < 2 and r.y1 - r.y0 > 5:
                    raw.append(((r.x0 + r.x1) / 2.0, r.y1 - r.y0))
    doc.close()
    if not raw:
        return []
    # cluster X
    raw.sort(key=lambda t: t[0])
    clustered: list[tuple[float, float]] = [raw[0]]
    for x, h in raw[1:]:
        if x - clustered[-1][0] < 2.0:
            # 合進去（取 max length）
            clustered[-1] = (clustered[-1][0], max(clustered[-1][1], h))
        else:
            clustered.append((x, h))
    # 找「最長 vertical 線群」 — 假設主表 vertical 線比文字 underline 更長
    if not clustered:
        return []
    max_h = max(h for _, h in clustered)
    threshold = max_h * 0.4  # 至少達最長線 40% — 排除短 underline
    table_xs = [x for x, h in clustered if h >= threshold]
    if len(table_xs) < 2:
        return []
    return [table_xs[i + 1] - table_xs[i] for i in range(len(table_xs) - 1)]


def get_odt_column_widths_pt(odt_path: Path) -> list[float]:
    """從 ODT XML 抽第一個 table 的 column widths (pt)。"""
    with zipfile.ZipFile(odt_path, "r") as z:
        try:
            content = z.read("content.xml")
        except KeyError:
            return []
    tree = ET.fromstring(content)
    NS_TABLE = "{urn:oasis:names:tc:opendocument:xmlns:table:1.0}"
    NS_STYLE = "{urn:oasis:names:tc:opendocument:xmlns:style:1.0}"
    NS_OFFICE = "{urn:oasis:names:tc:opendocument:xmlns:office:1.0}"
    # 蒐 column style → width 對應
    col_style_widths: dict = {}
    for s in tree.iter(f"{NS_STYLE}style"):
        if s.get(f"{NS_STYLE}family") != "table-column":
            continue
        for cp in s.iter(f"{NS_STYLE}table-column-properties"):
            w = cp.get(f"{NS_STYLE}column-width", "")
            if w.endswith("pt"):
                try:
                    col_style_widths[s.get(f"{NS_STYLE}name")] = float(w[:-2])
                except Exception:
                    pass
    for tbl in tree.iter(f"{NS_TABLE}table"):
        widths = []
        for col in tbl.iter(f"{NS_TABLE}table-column"):
            sname = col.get(f"{NS_TABLE}style-name", "")
            widths.append(col_style_widths.get(sname, 0))
        if widths:
            return widths
    return []


def make_compare_png(orig_pdf: Path, render_pdf: Path, out_png: Path) -> None:
    import fitz
    from PIL import Image
    a = fitz.open(str(orig_pdf)).load_page(0).get_pixmap(dpi=110)
    b = fitz.open(str(render_pdf)).load_page(0).get_pixmap(dpi=110)
    ta = a.tobytes("png")
    tb = b.tobytes("png")
    from io import BytesIO
    ia = Image.open(BytesIO(ta)).convert("RGB")
    ib = Image.open(BytesIO(tb)).convert("RGB")
    canvas = Image.new("RGB", (ia.width + ib.width + 12, max(ia.height, ib.height)),
                          (235, 235, 235))
    canvas.paste(ia, (0, 0))
    canvas.paste(ib, (ia.width + 12, 0))
    canvas.save(str(out_png))


def make_triple_compare_png(orig_pdf: Path, odt_render_pdf: Path,
                              docx_render_pdf: Path, out_png: Path) -> None:
    """三圖並列：原 PDF / ODT-render / docx-render — PLAN §5.3.1 強制驗證圖。"""
    import fitz
    from PIL import Image, ImageDraw, ImageFont
    from io import BytesIO
    pages = []
    for path, label in [(orig_pdf, "Original PDF"),
                          (odt_render_pdf, "ODT render"),
                          (docx_render_pdf, "ODT to docx render")]:
        if path.exists():
            pix = fitz.open(str(path)).load_page(0).get_pixmap(dpi=100)
            img = Image.open(BytesIO(pix.tobytes("png"))).convert("RGB")
        else:
            img = Image.new("RGB", (500, 700), (200, 200, 200))
        pages.append((label, img))
    LABEL_H = 28
    PAD = 8
    max_h = max(p.height for _, p in pages)
    total_w = sum(p.width for _, p in pages) + (len(pages) + 1) * PAD
    canvas = Image.new("RGB", (total_w, max_h + LABEL_H + PAD * 2), (235, 235, 235))
    d = ImageDraw.Draw(canvas)
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial Unicode.ttf", 14)
    except Exception:
        font = ImageFont.load_default()
    x = PAD
    for label, img in pages:
        d.text((x + 4, 4), label, fill=(40, 40, 40), font=font)
        canvas.paste(img, (x, LABEL_H))
        x += img.width + PAD
    canvas.save(str(out_png))


def odt_pipeline_with_docx(pdf: Path, work: Path) -> tuple[Path | None, Path | None, Path | None]:
    """跑完整 pipeline：PDF→ODT → render PDF / ODT→docx → render PDF。

    回 (odt_path, odt_render_pdf, docx_render_pdf)，個別失敗 None。
    """
    from app.tools.pdf_to_office.engines.jtdt_reform import convert_via_jtdt_reform_to_odt
    from app.tools.pdf_to_office.odt_to_docx import convert_odt_to_docx
    odt = work / (pdf.stem + ".odt")
    try:
        convert_via_jtdt_reform_to_odt(pdf, odt)
    except Exception:
        return None, None, None
    if not odt.exists():
        return None, None, None
    # render ODT → 立刻 rename，避免後面 docx → pdf 用同 stem 覆蓋
    odt_pdf_tmp = render_odt_to_pdf(odt, work)
    odt_pdf = None
    if odt_pdf_tmp and odt_pdf_tmp.exists():
        odt_pdf = work / (pdf.stem + "_from_odt.pdf")
        try:
            odt_pdf_tmp.rename(odt_pdf)
        except Exception:
            odt_pdf = odt_pdf_tmp
    # ODT → docx
    docx = work / (pdf.stem + ".docx")
    d_res = convert_odt_to_docx(odt, docx)
    docx_pdf = None
    if d_res.get("ok") and docx.exists():
        docx_pdf_tmp = render_odt_to_pdf(docx, work)  # render_odt_to_pdf 對 docx 也 work
        if docx_pdf_tmp and docx_pdf_tmp.exists():
            docx_pdf = work / (pdf.stem + "_from_docx.pdf")
            try:
                docx_pdf_tmp.rename(docx_pdf)
            except Exception:
                docx_pdf = docx_pdf_tmp
    return odt, odt_pdf, docx_pdf


def main():
    from app.tools.pdf_to_office.engines.jtdt_reform import convert_via_jtdt_reform_to_odt

    # --triple flag → 跑三圖驗證 (PLAN §5.3.1)
    triple_mode = "--triple" in sys.argv
    target_pdfs = [a for a in sys.argv[1:] if not a.startswith("--")]

    temp_dir = Path(__file__).resolve().parent.parent / "temp"
    out_dir = Path("/tmp/odt_verify")
    out_dir.mkdir(exist_ok=True)
    if target_pdfs:
        pdfs = [temp_dir / n for n in target_pdfs if (temp_dir / n).exists()]
    else:
        pdfs = sorted(temp_dir.glob("*.pdf"))
    if triple_mode:
        print(f"== triple-compare on {len(pdfs)} PDF(s) ==\n", flush=True)
        signal.signal(signal.SIGALRM, _alarm_handler)
        for pdf in pdfs:
            signal.alarm(120)
            try:
                with tempfile.TemporaryDirectory() as td:
                    work = Path(td)
                    odt, odt_pdf, docx_pdf = odt_pipeline_with_docx(pdf, work)
                    if not odt or not odt_pdf:
                        print(f"  {pdf.name}: ODT pipeline fail", flush=True)
                        continue
                    out_png = out_dir / (pdf.stem + "_triple.png")
                    make_triple_compare_png(pdf, odt_pdf,
                                              docx_pdf if docx_pdf else odt_pdf,
                                              out_png)
                    docx_ok = "✓" if docx_pdf else "✗ (docx fail)"
                    print(f"  {pdf.name}: triple → {out_png.name} ({docx_ok})", flush=True)
            except TimeoutError:
                print(f"  TIMEOUT {pdf.name}", flush=True)
            signal.alarm(0)
        print(f"\n  Triple compare images: {out_dir}/")
        return
    print(f"== auto verify ODT pipeline on {len(pdfs)} PDFs ==\n", flush=True)

    signal.signal(signal.SIGALRM, _alarm_handler)
    summary = []
    for pdf in pdfs:
        signal.alarm(60)
        try:
            with tempfile.TemporaryDirectory() as td:
                work = Path(td)
                odt = work / (pdf.stem + ".odt")
                try:
                    res = convert_via_jtdt_reform_to_odt(pdf, odt)
                except Exception as e:
                    print(f"  CONVERT-ERR {pdf.name}: {e}", flush=True)
                    summary.append((pdf.name, "convert-err", [], []))
                    continue
                if not res.get("ok") or not odt.exists():
                    print(f"  CONVERT-FAIL {pdf.name}: {res.get('error')}", flush=True)
                    summary.append((pdf.name, "convert-fail", [], []))
                    continue

                doc_widths = get_odt_column_widths_pt(odt)
                rendered_pdf = render_odt_to_pdf(odt, work)
                if rendered_pdf is None:
                    print(f"  RENDER-FAIL {pdf.name}", flush=True)
                    summary.append((pdf.name, "render-fail", doc_widths, []))
                    continue
                render_widths = measure_table_cols_from_pdf(rendered_pdf)
                # 把對照圖留下
                compare_png = out_dir / (pdf.stem + "_compare.png")
                make_compare_png(pdf, rendered_pdf, compare_png)
                # 判定
                ok = False
                status = ""
                if doc_widths and render_widths:
                    # render 內可能含 border padding（每 col 之間多 0.5-1pt）— 取
                    # render 內 width > 5pt 的當 real col，跳過 < 5pt 的 border
                    real_render = [w for w in render_widths if w > 5]
                    n = min(len(doc_widths), len(real_render))
                    diffs = []
                    for i in range(n):
                        d = doc_widths[i]
                        r = real_render[i]
                        if d > 0:
                            diffs.append(abs(d - r) / d)
                    max_diff = max(diffs) if diffs else 0
                    ok = max_diff < 0.10  # 10% 容差（含 border ~1pt 誤差）
                    status = f"diff={max_diff:.1%}" + (" OK" if ok else " FAIL")
                else:
                    status = "no-grid"
                doc_short = [round(w, 1) for w in doc_widths]
                render_short = [round(w, 1) for w in render_widths]
                print(f"  {pdf.name}: doc={doc_short} render={render_short} → {status}", flush=True)
                summary.append((pdf.name, status, doc_widths, render_widths))
        except TimeoutError:
            print(f"  TIMEOUT {pdf.name}", flush=True)
            summary.append((pdf.name, "timeout", [], []))
        signal.alarm(0)

    print("\n=== summary ===")
    ok = sum(1 for s in summary if "OK" in s[1])
    print(f"  L3 widths: {ok} / {len(summary)} OxOffice renders within 10% of ODT col widths")
    print(f"  L4 compare images: {out_dir}/")


if __name__ == "__main__":
    main()
