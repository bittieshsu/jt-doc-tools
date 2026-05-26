"""OCR 引擎抽象層 — 統一 tesseract / easyocr 介面。

`recognize_image(png_bytes, langs)` 回標準化 word list：
    [{"text": "abc", "conf": 87.5, "left": 10, "top": 20, "width": 30, "height": 16}, ...]

支援引擎：
- `tesseract`：傳統 OCR，輕量、CJK 識別率較弱、per-CHAR 級 bbox（CJK 每字一個 word）
- `easyocr`：JaidedAI 泰國公司開源 OCR，中日韓辨識準確度高、per-LINE bbox（一行一個 word）

設定值在 `data/ocr_settings.json` 的 `engine` 欄位（'easyocr' / 'tesseract'）。

Engine 失敗 / 不可用會自動 fallback 到對方（避免 user 看到 import error）。
"""
from __future__ import annotations

import io
import json
import logging
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# 兩引擎之間的語言碼對應 — tesseract code → easyocr code
# 沒在表內的 lang code 在 easyocr 端會 fallback 到 tesseract
_TESS_TO_EASYOCR = {
    "chi_tra": "ch_tra",
    "chi_sim": "ch_sim",
    "eng": "en",
    "jpn": "ja",
    "kor": "ko",
    "deu": "de",
    "fra": "fr",
    "spa": "es",
    "ita": "it",
    "por": "pt",
    "nld": "nl",
    "rus": "ru",
    "vie": "vi",
    "tha": "th",
    "ind": "id",
    "ara": "ar",
    "heb": "he",
    "hin": "hi",
}

# Lazy-loaded EasyOCR Reader（per-langs cache，避免每次 OCR 都重建模型）
_easyocr_readers: dict[tuple, object] = {}


def get_default_engine() -> str:
    """讀取 admin 設定的預設 engine。預設 'easyocr'。"""
    p = _ocr_settings_path()
    if not p.exists():
        return "easyocr"
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        e = (d.get("engine") or "easyocr").strip().lower()
        return e if e in ("easyocr", "tesseract") else "easyocr"
    except Exception:
        return "easyocr"


def set_default_engine(engine: str) -> bool:
    if engine not in ("easyocr", "tesseract"):
        return False
    p = _ocr_settings_path()
    try:
        d = {}
        if p.exists():
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                d = {}
        d["engine"] = engine
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
        return True
    except Exception as e:
        log.warning("set_default_engine failed: %s", e)
        return False


def _ocr_settings_path() -> Path:
    try:
        from ..config import settings
        return Path(settings.data_dir) / "ocr_settings.json"
    except Exception:
        return Path("data") / "ocr_settings.json"


# ---- Engine availability ----

def is_easyocr_available() -> bool:
    """快速檢查 easyocr 是否可用 — 用 importlib.util.find_spec 只看 package
    是否存在，**不真的 import**（避免載 PyTorch 100+MB 拖慢 admin 頁渲染）。"""
    try:
        import importlib.util
        return importlib.util.find_spec("easyocr") is not None
    except Exception:
        return False


def is_tesseract_available() -> bool:
    """Tesseract 是否可用（binary + pytesseract）。"""
    try:
        from .sys_deps import _find_tesseract_binary, configure_pytesseract
        configure_pytesseract()
        return bool(_find_tesseract_binary())
    except Exception:
        return False


# ---- EasyOCR backend ----

def _get_easyocr_reader(easyocr_langs: tuple) -> Optional[object]:
    """Lazy-create or return cached EasyOCR Reader for given lang tuple.
    First call per lang combo loads model from disk (or downloads ~150MB
    if not yet downloaded) — 5-30s 延遲。"""
    if easyocr_langs in _easyocr_readers:
        return _easyocr_readers[easyocr_langs]
    try:
        import easyocr
        # gpu=False 安全預設（CPU 跑 — 有 GPU 客戶可在 admin 開啟）
        # download_enabled=True 第一次自動下模型到 ~/.EasyOCR/model/
        reader = easyocr.Reader(list(easyocr_langs), gpu=False,
                                 verbose=False, download_enabled=True)
        _easyocr_readers[easyocr_langs] = reader
        log.info("EasyOCR Reader loaded for langs=%s (cached)", easyocr_langs)
        return reader
    except Exception as e:
        log.warning("EasyOCR Reader init failed for %s: %s", easyocr_langs, e)
        return None


def _map_langs_to_easyocr(tess_langs: str) -> tuple:
    """tesseract '+' 串連語言碼 → easyocr list（過濾掉 easyocr 不支援的）。"""
    mapped = []
    for code in tess_langs.split("+"):
        code = code.strip()
        if not code:
            continue
        eo = _TESS_TO_EASYOCR.get(code)
        if eo and eo not in mapped:
            mapped.append(eo)
    # EasyOCR 限制：英文以外的語言一定要配 'en'（架構設計）
    if mapped and "en" not in mapped:
        mapped.append("en")
    return tuple(mapped)


def _easyocr_recognize(png_bytes: bytes, langs: str) -> list[dict]:
    """跑 EasyOCR 對 PNG 抽 word data。
    回 standardized format：[{text, conf, left, top, width, height}, ...]"""
    eo_langs = _map_langs_to_easyocr(langs)
    if not eo_langs:
        return []
    reader = _get_easyocr_reader(eo_langs)
    if reader is None:
        return []
    try:
        from PIL import Image
        import numpy as np
        img = Image.open(io.BytesIO(png_bytes))
        # easyocr.readtext 接受 numpy array 或 path；用 numpy 避免 round-trip
        arr = np.asarray(img.convert("RGB"))
        results = reader.readtext(arr, detail=1, paragraph=False)
    except Exception as e:
        log.warning("EasyOCR readtext failed: %s", e)
        return []

    out = []
    for item in results:
        if not item or len(item) < 3:
            continue
        bbox, text, conf = item[0], item[1], item[2]
        text = (text or "").strip()
        if not text:
            continue
        # EasyOCR bbox = 4 corner points [(x,y), ...]
        try:
            xs = [p[0] for p in bbox]
            ys = [p[1] for p in bbox]
            left = int(min(xs))
            top = int(min(ys))
            width = int(max(xs) - left)
            height = int(max(ys) - top)
        except Exception:
            continue
        out.append({
            "text": text,
            "conf": float(conf) * 100.0,  # easyocr 0..1, normalize to tesseract scale 0..100
            "left": left,
            "top": top,
            "width": width,
            "height": height,
        })
    return out


# ---- Tesseract backend ----

def _tesseract_recognize(png_bytes: bytes, langs: str, preprocess: bool = True) -> list[dict]:
    """委派給既有 pdf_ocr.ocr_core._tesseract_image_to_data。"""
    try:
        from app.tools.pdf_ocr.ocr_core import _tesseract_image_to_data
        return _tesseract_image_to_data(png_bytes, langs, preprocess=preprocess)
    except Exception as e:
        log.warning("tesseract recognize failed: %s", e)
        return []


# ---- Public dispatcher ----

def recognize_text(png_bytes: bytes, langs: str,
                    engine: Optional[str] = None,
                    preprocess: bool = True) -> tuple[str, str]:
    """簡化介面 — 對 caller 只關心「該圖的文字內容」（不要 bbox / conf）的場景。
    內部呼叫 recognize_image，把 words join 成單字串。
    回 (text, engine_used)。"""
    words, used = recognize_image(png_bytes, langs, engine=engine, preprocess=preprocess)
    if not words:
        return "", used
    return " ".join(w.get("text", "").strip() for w in words if w.get("text", "").strip()), used


def _remote_easyocr_recognize(png_bytes: bytes, langs: str,
                                preprocess: bool = True) -> list[dict]:
    """呼叫遠端 GPU EasyOCR server。失敗 raise(讓 caller fallback 本機)。
    langs 進來是 tesseract code (chi_tra+eng),要轉成 easyocr code (ch_tra+en)。"""
    from . import ocr_remote_settings as _ors
    import httpx
    d = _ors.get()
    url = (d.get("url") or "").rstrip("/")
    token = d.get("token") or ""
    timeout_s = d.get("timeout_s") or 60
    if not url or not token:
        raise RuntimeError("remote OCR url/token not configured")

    # 把 tesseract lang code 轉 easyocr code(chi_tra->ch_tra, eng->en, ...)
    raw_codes = [c.strip() for c in langs.replace(",", "+").split("+") if c.strip()]
    easy_codes = [_TESS_TO_EASYOCR.get(c, c) for c in raw_codes]
    easy_langs = "+".join(easy_codes)

    log.info("remote OCR -> %s (langs=%s -> %s, %d KB)", url, langs, easy_langs, len(png_bytes) // 1024)
    files = {"image": ("page.png", png_bytes, "image/png")}
    data = {"langs": easy_langs, "preprocess": "true" if preprocess else "false"}
    headers = {"Authorization": f"Bearer {token}"}
    with httpx.Client(timeout=float(timeout_s)) as cli:
        r = cli.post(f"{url}/ocr", files=files, data=data, headers=headers)
        if r.status_code != 200:
            raise RuntimeError(f"remote OCR HTTP {r.status_code}: {r.text[:200]}")
        body = r.json()
    raw_words = body.get("words") or []
    words: list[dict] = []
    for w in raw_words:
        bbox = w.get("bbox") or []
        if len(bbox) < 4:
            continue
        x0, y0, x1, y1 = bbox[0], bbox[1], bbox[2], bbox[3]
        words.append({
            "text": str(w.get("text") or ""),
            "left": float(x0),
            "top": float(y0),
            "width": float(x1 - x0),
            "height": float(y1 - y0),
            "conf": int(round(float(w.get("conf") or 0.95) * 100)),
        })
    log.info("remote OCR <- %d words, %.2fs (device=%s)",
             len(words), body.get("elapsed_s", 0), body.get("device", "?"))
    return words


def recognize_image(png_bytes: bytes, langs: str,
                     engine: Optional[str] = None,
                     preprocess: bool = True) -> tuple[list[dict], str]:
    """主要入口 — 跑 OCR 拿 standardized words，自動 fallback。

    engine: 'easyocr' / 'tesseract' / None（用 admin 預設值）
    preprocess: 是否做影像預處理（grayscale + autocontrast）— 兩 engine 都受惠

    遠端 GPU EasyOCR server: 若 admin 已設定 + enabled,優先使用,失敗自動退本機。

    回 (words_list, engine_used) — engine_used 紀錄實際用的 engine（可能是 fallback 後的）
    """
    chosen = engine or get_default_engine()

    # 遠端 GPU EasyOCR — 優先使用,失敗 fallback 本機
    if chosen == "easyocr":
        try:
            from . import ocr_remote_settings as _ors
            if _ors.is_enabled_and_configured():
                try:
                    words = _remote_easyocr_recognize(png_bytes, langs, preprocess=preprocess)
                    if words:
                        return words, "easyocr-remote"
                    log.info("remote EasyOCR returned 0 words, trying local")
                except Exception as e:
                    log.warning("remote EasyOCR failed (%s) — falling back to local", e)
        except Exception as e:
            log.warning("remote OCR settings check failed: %s", e)

    if chosen == "easyocr":
        if is_easyocr_available():
            words = _easyocr_recognize(png_bytes, langs)
            if words:
                return words, "easyocr"
            log.info("EasyOCR returned 0 words, trying tesseract fallback")
        else:
            log.info("EasyOCR not available, falling back to tesseract")
        # Fallback to tesseract
        words = _tesseract_recognize(png_bytes, langs, preprocess=preprocess)
        return words, "tesseract" if words else "none"

    # chosen == 'tesseract'
    if is_tesseract_available():
        words = _tesseract_recognize(png_bytes, langs, preprocess=preprocess)
        if words:
            return words, "tesseract"
        log.info("Tesseract returned 0 words, trying easyocr fallback")
    if is_easyocr_available():
        words = _easyocr_recognize(png_bytes, langs)
        return words, "easyocr" if words else "none"
    return [], "none"
