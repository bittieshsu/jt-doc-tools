#!/usr/bin/env python3
"""eval_engines.py — pdf-to-office 兩引擎自動評分。

對 temp/ 內每份 PDF：
  1. 用 pdf2docx-refine 引擎跑（v1.9.81+）
  2. 用 jtdt-reform 引擎跑
  3. 把兩個輸出（.docx / .odt）用 soffice render 回 PDF
  4. 計算 4 個指標：
     - text_score: rendered 字數 / orig 字數（cap 1.0；0.95 視為完美）
     - page_score: 1 - abs(rend_pages - orig_pages) / max(orig_pages, 1)
     - layout_score: orig 與 rend 把文字分到 24 y-bin，計算 bin-wise text 字
       數的 cosine similarity（簡單 spatial proxy）
     - visual_score: orig 與 rend 各 page 渲成 thumbnail，計算 pixel MAE
       (~ 1 - mean_abs_err/255)。捕捉表格框線 / 顏色 / 背景色 / 位置整體
       視覺相似度 — **是終極 KPI**（越一樣越好）。
     - composite = 0.15*text + 0.15*page + 0.10*layout + 0.60*visual
       （視覺權重 60%，反映「直接比對 pdf 越一樣越好」的終極標準）

輸出：
  /tmp/eval-result.csv  — per-PDF + per-engine 得分
  /tmp/eval-result.md   — ranking + 落後 case 整理

通用工具 — 不針對特定 PDF。

執行：.venv/bin/python scripts/eval_engines.py [--limit N] [--engine NAME]
"""
from __future__ import annotations

import argparse
import hashlib
import math
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import fitz  # PyMuPDF

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.tools.pdf_to_office.service import convert_pdf_to_office  # noqa: E402

ENGINES = ["pdf2docx-refine", "jtdt-reform"]
SOFFICE_CANDIDATES = [
    "/opt/oxoffice/program/soffice",
    "/Applications/OxOffice.app/Contents/MacOS/soffice",
    "/Applications/LibreOffice.app/Contents/MacOS/soffice",
]


def _find_soffice() -> str | None:
    for c in SOFFICE_CANDIDATES:
        if Path(c).exists():
            return c
    return None


def _normalize_text(s: str) -> str:
    """剝空白 / 換行 / 標點，留 letter/digit/CJK chars 計數。"""
    keep = []
    for ch in s:
        code = ord(ch)
        if 0x30 <= code <= 0x39:  # digit
            keep.append(ch)
        elif 0x41 <= code <= 0x5A or 0x61 <= code <= 0x7A:  # letter
            keep.append(ch)
        elif (0x3400 <= code <= 0x9FFF
                or 0xAC00 <= code <= 0xD7AF
                or 0x3040 <= code <= 0x309F
                or 0x30A0 <= code <= 0x30FF):  # CJK / kana
            keep.append(ch)
    return "".join(keep)


def _y_bin_histogram(pdf_path: Path, n_bins: int = 24) -> list[float]:
    """把 PDF 所有頁文字字數分到 n_bins 個 y-bin（page-normalized）。

    回 list[float] of length n_bins，每元素是該 bin 累積字數。
    跨頁取 average y position (page i 的 y bin 是 page-relative）。
    """
    try:
        d = fitz.open(str(pdf_path))
    except Exception:
        return [0.0] * n_bins
    bins = [0.0] * n_bins
    for pi in range(d.page_count):
        page = d[pi]
        page_h = page.rect.height or 1.0
        for blk in page.get_text("dict")["blocks"]:
            if blk["type"] != 0:
                continue
            for ln in blk.get("lines", []):
                for sp in ln.get("spans", []):
                    txt = sp.get("text", "") or ""
                    nch = len(_normalize_text(txt))
                    if not nch:
                        continue
                    y_mid = (sp.get("bbox", (0, 0, 0, 0))[1]
                             + sp.get("bbox", (0, 0, 0, 0))[3]) / 2
                    rel = max(0.0, min(0.999, y_mid / page_h))
                    bi = int(rel * n_bins)
                    bins[bi] += nch
    d.close()
    return bins


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na <= 0 or nb <= 0:
        return 0.0
    return dot / (na * nb)


def _pixel_visual_score(orig_pdf: Path, rend_pdf: Path,
                          thumb_w: int = 120) -> float:
    """像素級視覺相似度。對每對 (orig page i, rend page i) 渲成小 thumbnail
    (灰階) + Gaussian blur 降 sub-pixel 位移噪音，計算 mean absolute error，
    回 1 - MAE/255 平均。

    v2 改善：縮到 120px 寬 + blur(radius=1.5)，避免「黑字白底差 1px 就重罰」
    使視覺等同的 raster fallback 不被低估。仍對「整塊區域空白 / 位置大偏 /
    顏色背景錯」敏感（這才是真的視覺差）。

    page count 不同時：以 orig page 數為基準；超過 rend 範圍的 orig 頁配
    白頁；超過 orig 的 rend 頁忽略（penalty 已在 page_score 體現）。
    """
    try:
        from PIL import Image, ImageFilter
    except Exception:
        return 0.0
    try:
        d_o = fitz.open(str(orig_pdf))
        d_r = fitz.open(str(rend_pdf))
    except Exception:
        return 0.0
    n_o = d_o.page_count
    n_r = d_r.page_count
    if n_o == 0:
        d_o.close(); d_r.close()
        return 0.0

    def _thumb(pix_img, w, h):
        img = pix_img.resize((w, h), Image.LANCZOS)
        return img.filter(ImageFilter.GaussianBlur(radius=1.5))

    scores = []
    for pi in range(n_o):
        opg = d_o[pi]
        page_w = opg.rect.width or 595
        page_h = opg.rect.height or 842
        dpi = max(20, int(thumb_w / max(page_w, 1) * 72))
        try:
            opix = opg.get_pixmap(dpi=dpi, colorspace=fitz.csGRAY)
        except Exception:
            continue
        ow, oh = opix.width, opix.height
        if pi < n_r:
            try:
                rpix = d_r[pi].get_pixmap(dpi=dpi, colorspace=fitz.csGRAY)
            except Exception:
                rpix = None
        else:
            rpix = None
        oimg = _thumb(Image.frombytes("L", (ow, oh), opix.samples), ow, oh)
        if rpix is not None:
            rimg_raw = Image.frombytes("L", (rpix.width, rpix.height), rpix.samples)
            if (rimg_raw.width, rimg_raw.height) != (ow, oh):
                rimg_raw = rimg_raw.resize((ow, oh), Image.LANCZOS)
            rimg = rimg_raw.filter(ImageFilter.GaussianBlur(radius=1.5))
        else:
            rimg = Image.new("L", (ow, oh), 255)
        ob = oimg.tobytes()
        rb = rimg.tobytes()
        total = len(ob)
        if total == 0:
            continue
        s = sum(abs(a - b) for a, b in zip(ob, rb))
        mae = s / total
        scores.append(1.0 - mae / 255.0)
    d_o.close(); d_r.close()
    return sum(scores) / len(scores) if scores else 0.0


def _count_chars(pdf_path: Path) -> int:
    try:
        d = fitz.open(str(pdf_path))
    except Exception:
        return 0
    total = 0
    for pi in range(d.page_count):
        total += len(_normalize_text(d[pi].get_text()))
    d.close()
    return total


def _page_count(pdf_path: Path) -> int:
    try:
        d = fitz.open(str(pdf_path))
        n = d.page_count
        d.close()
        return n
    except Exception:
        return 0


def _render_office_to_pdf(office_doc: Path, soffice: str,
                            out_dir: Path) -> Path | None:
    with tempfile.TemporaryDirectory(prefix="sof_") as profile:
        try:
            subprocess.run(
                [soffice, "--headless",
                 f"-env:UserInstallation=file://{profile}",
                 "--convert-to", "pdf",
                 "--outdir", str(out_dir), str(office_doc)],
                capture_output=True, timeout=240,
            )
        except subprocess.TimeoutExpired:
            return None
        except Exception:
            return None
    rendered = out_dir / (office_doc.stem + ".pdf")
    return rendered if rendered.exists() else None


def score_pdf(orig_pdf: Path, soffice: str,
               work_root: Path) -> dict:
    """跑兩引擎 + 計算指標。回 dict per engine + 整體 result。"""
    orig_chars = _count_chars(orig_pdf)
    orig_pages = _page_count(orig_pdf)
    orig_bins = _y_bin_histogram(orig_pdf)

    result = {
        "pdf": orig_pdf.name,
        "orig_chars": orig_chars,
        "orig_pages": orig_pages,
        "engines": {},
    }
    for engine in ENGINES:
        out_format = "docx" if "pdf2docx" in engine else "odt"
        eng_dir = work_root / engine.replace("-", "_") / hashlib.sha1(
            orig_pdf.name.encode()).hexdigest()[:10]
        eng_dir.mkdir(parents=True, exist_ok=True)
        for p in list(eng_dir.glob("*")):
            try: p.unlink()
            except Exception: pass

        t0 = time.time()
        try:
            r = convert_pdf_to_office(
                orig_pdf, eng_dir, out_format,
                enable_postprocess=False, keep_intermediate=False,
                engine=engine,
            )
        except Exception as e:
            result["engines"][engine] = {
                "ok": False, "error": str(e)[:100],
                "text_score": 0, "page_score": 0,
                "layout_score": 0, "composite": 0,
            }
            continue
        conv_secs = time.time() - t0

        if not r.ok:
            result["engines"][engine] = {
                "ok": False, "error": r.error or "convert failed",
                "text_score": 0, "page_score": 0,
                "layout_score": 0, "composite": 0,
                "conv_secs": round(conv_secs, 1),
            }
            continue

        # find office doc
        ext = f".{out_format}"
        office_docs = list(eng_dir.glob(f"*{ext}"))
        if not office_docs:
            result["engines"][engine] = {
                "ok": False, "error": f"no output {out_format}",
                "text_score": 0, "page_score": 0,
                "layout_score": 0, "composite": 0,
                "conv_secs": round(conv_secs, 1),
            }
            continue
        office_doc = office_docs[0]

        # render to PDF
        rendered_pdf = _render_office_to_pdf(office_doc, soffice, eng_dir)
        if rendered_pdf is None:
            result["engines"][engine] = {
                "ok": False, "error": "render PDF failed",
                "text_score": 0, "page_score": 0,
                "layout_score": 0, "composite": 0,
                "conv_secs": round(conv_secs, 1),
            }
            continue

        # metrics
        rend_chars = _count_chars(rendered_pdf)
        rend_pages = _page_count(rendered_pdf)
        rend_bins = _y_bin_histogram(rendered_pdf)

        text_score = (min(1.0, rend_chars / orig_chars)
                      if orig_chars > 0 else 0.0)
        page_score = (1.0 - min(1.0, abs(rend_pages - orig_pages) /
                                       max(orig_pages, 1)))
        layout_score = _cosine(orig_bins, rend_bins)
        visual_score = _pixel_visual_score(orig_pdf, rendered_pdf)
        # 視覺權重 60% — 反映「直接比對 pdf 越一樣越好」的終極標準
        composite = (0.15 * text_score + 0.15 * page_score
                     + 0.10 * layout_score + 0.60 * visual_score)

        result["engines"][engine] = {
            "ok": True,
            "rend_chars": rend_chars,
            "rend_pages": rend_pages,
            "text_score": round(text_score, 3),
            "page_score": round(page_score, 3),
            "layout_score": round(layout_score, 3),
            "visual_score": round(visual_score, 3),
            "composite": round(composite, 3),
            "conv_secs": round(conv_secs, 1),
        }
    return result


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0,
                     help="only process N PDFs (0 = all)")
    ap.add_argument("--engine", default="all", choices=["all"] + ENGINES,
                     help="filter engine")
    ap.add_argument("--temp-dir", default="temp")
    ap.add_argument("--out-dir", default="/tmp/eval-engines")
    ap.add_argument("--csv", default="/tmp/eval-result.csv")
    ap.add_argument("--md", default="/tmp/eval-result.md")
    args = ap.parse_args()

    soffice = _find_soffice()
    if not soffice:
        print("ERROR: no soffice found", file=sys.stderr)
        return 2

    work_root = Path(args.out_dir)
    work_root.mkdir(exist_ok=True)

    temp_dir = Path(args.temp_dir)
    pdfs = sorted(temp_dir.glob("*.pdf")) + sorted(temp_dir.glob("*.PDF"))
    pdfs = [p for p in pdfs if p.is_file()]
    if args.limit > 0:
        pdfs = pdfs[: args.limit]

    print(f"Scoring {len(pdfs)} PDFs with engines: {ENGINES}")
    print(f"soffice: {soffice}")
    print(f"work: {work_root}")

    # kill soffice for clean run
    subprocess.run(["pkill", "-9", "-f", "soffice"], capture_output=True)
    time.sleep(2)

    results = []
    for i, pdf in enumerate(pdfs, 1):
        print(f"\n[{i}/{len(pdfs)}] {pdf.name}")
        try:
            r = score_pdf(pdf, soffice, work_root)
        except Exception as e:
            print(f"  EXC: {e}")
            continue
        results.append(r)
        for eng in ENGINES:
            e = r["engines"].get(eng, {})
            if e.get("ok"):
                print(f"  {eng:18s}  text={e['text_score']:.2f} page={e['page_score']:.2f} "
                       f"layout={e['layout_score']:.2f} visual={e.get('visual_score', 0):.2f} "
                       f"composite={e['composite']:.2f} "
                       f"({e.get('conv_secs', 0):.1f}s)")
            else:
                print(f"  {eng:18s}  FAIL: {e.get('error', '?')[:60]}")

    # write CSV
    csv_lines = ["pdf,engine,ok,orig_chars,orig_pages,rend_chars,rend_pages,"
                  "text,page,layout,visual,composite,secs"]
    for r in results:
        for eng in ENGINES:
            e = r["engines"].get(eng, {})
            csv_lines.append(
                f"{r['pdf']},{eng},{e.get('ok', False)},"
                f"{r['orig_chars']},{r['orig_pages']},"
                f"{e.get('rend_chars', '')},{e.get('rend_pages', '')},"
                f"{e.get('text_score', '')},{e.get('page_score', '')},"
                f"{e.get('layout_score', '')},{e.get('visual_score', '')},"
                f"{e.get('composite', '')},{e.get('conv_secs', '')}"
            )
    Path(args.csv).write_text("\n".join(csv_lines), encoding="utf-8")
    print(f"\nCSV written: {args.csv}")

    # markdown summary
    md_lines = ["# pdf-to-office 兩引擎評分"]
    md_lines.append("")
    md_lines.append(f"PDFs: {len(results)}, engines: {', '.join(ENGINES)}")
    md_lines.append("")
    # global average
    md_lines.append("## 平均得分")
    md_lines.append("")
    md_lines.append("| engine | text | page | layout | visual | composite | OK count |")
    md_lines.append("|---|---:|---:|---:|---:|---:|---:|")
    for eng in ENGINES:
        oks = [r["engines"].get(eng, {}) for r in results
                if r["engines"].get(eng, {}).get("ok")]
        if not oks:
            md_lines.append(f"| {eng} | - | - | - | - | - | 0 |")
            continue
        avg_t = sum(e["text_score"] for e in oks) / len(oks)
        avg_p = sum(e["page_score"] for e in oks) / len(oks)
        avg_l = sum(e["layout_score"] for e in oks) / len(oks)
        avg_v = sum(e.get("visual_score", 0) for e in oks) / len(oks)
        avg_c = sum(e["composite"] for e in oks) / len(oks)
        md_lines.append(f"| **{eng}** | {avg_t:.3f} | {avg_p:.3f} | "
                         f"{avg_l:.3f} | **{avg_v:.3f}** | **{avg_c:.3f}** | {len(oks)} |")
    md_lines.append("")

    # per-PDF table
    md_lines.append("## per-PDF composite score")
    md_lines.append("")
    md_lines.append("| PDF | pdf2docx | jtdt-reform | winner |")
    md_lines.append("|---|---:|---:|:---:|")
    for r in results:
        pe = r["engines"].get("pdf2docx-refine", {})
        je = r["engines"].get("jtdt-reform", {})
        ps = pe.get("composite", 0) if pe.get("ok") else 0
        js = je.get("composite", 0) if je.get("ok") else 0
        winner = "jtdt" if js > ps + 0.01 else ("pdf2docx" if ps > js + 0.01 else "tie")
        md_lines.append(f"| {r['pdf'][:60]} | {ps:.3f} | {js:.3f} | {winner} |")

    # jtdt-reform 落後最多的 case
    md_lines.append("")
    md_lines.append("## jtdt-reform 落後最多的 case (jtdt 比 pdf2docx 低 > 0.05)")
    md_lines.append("")
    md_lines.append("| PDF | pdf2docx | jtdt-reform | gap |")
    md_lines.append("|---|---:|---:|---:|")
    gaps = []
    for r in results:
        pe = r["engines"].get("pdf2docx-refine", {})
        je = r["engines"].get("jtdt-reform", {})
        if not (pe.get("ok") and je.get("ok")):
            continue
        gap = pe["composite"] - je["composite"]
        if gap > 0.05:
            gaps.append((gap, r["pdf"], pe["composite"], je["composite"]))
    gaps.sort(reverse=True)
    for gap, name, ps, js in gaps:
        md_lines.append(f"| {name[:60]} | {ps:.3f} | {js:.3f} | **+{gap:.3f}** |")
    if not gaps:
        md_lines.append("| (無) | | | |")

    Path(args.md).write_text("\n".join(md_lines), encoding="utf-8")
    print(f"MD written: {args.md}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
