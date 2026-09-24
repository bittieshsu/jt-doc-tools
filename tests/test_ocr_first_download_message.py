"""第一次用本機 EasyOCR 時，狀態文字要說「正在下載辨識模型」。

模型是在**辨識第一頁的當下**才下載（繁中約 275 MB，放在 GitHub）。原本畫面寫
「OCR 辨識中」—— 2026-09-24 在 Windows 全新安裝上實測，到 GitHub 每秒只有 40 KB，
畫面停在那一句半小時，看起來跟當掉一樣。

作業一開始設的「準備中」只存在一瞬間（辨識前就被改寫），所以判準落在
**辨識那一頁之前最後設的那一句**。
"""
from __future__ import annotations

import io

import fitz
import pytest

from app.core import ocr_engine as oe
from app.tools.pdf_ocr import ocr_core


def _scan_pdf(tmp_path):
    from PIL import Image
    img = Image.new("RGB", (400, 200), "white")
    buf = io.BytesIO(); img.save(buf, "PNG")
    d = fitz.open(); pg = d.new_page(width=200, height=100)
    pg.insert_image(pg.rect, stream=buf.getvalue())
    src = tmp_path / "scan.pdf"; d.save(str(src))
    return src


@pytest.mark.parametrize("pending,want", [
    (True, "正在下載辨識模型"),
    (False, "OCR 辨識中"),       # 反向對照：模型在了就不可以一直喊下載
])
def test_status_before_recognising_the_first_page(tmp_path, monkeypatch, pending, want):
    seen: list[str] = []
    monkeypatch.setattr(oe, "get_default_engine", lambda: "easyocr")
    monkeypatch.setattr(oe, "easyocr_first_download_pending", lambda *a, **k: pending)
    from app.core import ocr_remote_settings as ors
    monkeypatch.setattr(ors, "is_enabled_and_configured", lambda: False)

    def fake_recognize(png, langs, preprocess=True, **kw):
        seen.append("<recognize>")
        return [], "easyocr"
    monkeypatch.setattr(oe, "recognize_image", fake_recognize)

    src = _scan_pdf(tmp_path)
    try:
        ocr_core.ocr_pdf_to_searchable(src, tmp_path / "out.pdf", langs="chi_tra+eng", dpi=72,
                                       skip_pages_with_text=False,
                                       progress_cb=lambda c, t, m: seen.append(m))
    except Exception:
        pass            # 假的辨識回 0 個字，後面怎麼收尾不是這條要驗的
    assert "<recognize>" in seen, f"根本沒走到辨識那一步：{seen}"
    before = seen[: seen.index("<recognize>")]
    assert before and want in before[-1], f"辨識前最後的狀態是 {before[-1:]!r}，應該含「{want}」"


def test_the_pending_check_does_not_import_easyocr():
    """判斷要便宜：不可以為了判斷「要不要下載」把 PyTorch 載進來。"""
    import ast, inspect
    src = inspect.getsource(oe.easyocr_first_download_pending)
    names = {a.name for n in ast.walk(ast.parse(src))
             if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names}
    mods = {n.module for n in ast.walk(ast.parse(src)) if isinstance(n, ast.ImportFrom)}
    assert "easyocr" not in names and "easyocr" not in mods and "torch" not in names


@pytest.mark.parametrize("files,langs,want", [
    ([], "chi_tra+eng", True),                                   # 完全沒下載過
    (["craft_mlt_25k.pth"], "chi_tra+eng", True),                # 下載到一半（實機碰到的）
    (["craft_mlt_25k.pth", "chinese.pth"], "chi_tra+eng", False),
    (["craft_mlt_25k.pth", "chinese.pth"], "jpn", True),         # 換一種語言要另一個模型
    (["craft_mlt_25k.pth", "english_g2.pth"], "eng", False),
])
def test_pending_looks_at_the_model_this_language_needs(tmp_path, monkeypatch, files, langs, want):
    (tmp_path / "model").mkdir()
    for f in files:
        (tmp_path / "model" / f).write_bytes(b"x")
    monkeypatch.setenv("EASYOCR_MODULE_PATH", str(tmp_path))
    monkeypatch.setattr(oe, "get_default_engine", lambda: "easyocr")
    monkeypatch.setattr(oe, "is_easyocr_available", lambda: True)
    from app.core import ocr_remote_settings as ors
    monkeypatch.setattr(ors, "is_enabled_and_configured", lambda: False)
    assert oe.easyocr_first_download_pending(langs) is want


def test_the_model_table_matches_the_installed_easyocr():
    """對照表是照 easyocr 抄的 —— 裝著的那一版要真的有這些檔名，
    不然 easyocr 升級改了檔名，這裡會一直喊「正在下載」。"""
    import importlib.util, pathlib as _pl
    spec = importlib.util.find_spec("easyocr")
    if spec is None:
        pytest.skip("這台沒有 easyocr")
    cfg = (_pl.Path(spec.origin).parent / "config.py").read_text(encoding="utf-8")
    for _code, fn in oe._EASYOCR_RECOG_ORDER:
        assert f"'{fn}'" in cfg or f'"{fn}"' in cfg, f"easyocr 的設定裡沒有 {fn}"
    for _codes, fn in oe._EASYOCR_RECOG_SETS:
        assert f"'{fn}'" in cfg or f'"{fn}"' in cfg, f"easyocr 的設定裡沒有 {fn}"
