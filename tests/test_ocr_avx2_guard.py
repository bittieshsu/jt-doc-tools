"""本機 EasyOCR 在缺 AVX2 的 CPU 上會 SIGILL 打掛整個服務。

客戶回報過一次（v1.12.10，PVE `x86-64-v2` CPU model）：OCR 辨識階段執行
非法指令 → 服務 core dump 重啟、**當下所有人的作業一起變 404**。
v1.12.12 加了偵測與指引，但**使用者明確要求不要自動切換引擎** —— 所以
OCR 工具維持手動選擇，這裡守的是另一件事：

「使用者根本沒選過引擎」的**內部自動退路**（PDF 編輯器點到壞 ToUnicode
的文字時自動 OCR）不可以因此打掛服務。開關預設 True，其他呼叫者行為不變。
"""
from __future__ import annotations

import app.core.ocr_engine as oe


def test_switch_defaults_to_on(monkeypatch):
    """預設值必須是 True —— 否則 OCR 工具會被這個開關悄悄改掉行為。"""
    calls = []
    monkeypatch.setattr(oe, "is_easyocr_available", lambda: True)
    monkeypatch.setattr(oe, "_easyocr_recognize",
                        lambda *a, **k: calls.append("easyocr") or [{"text": "x"}])
    monkeypatch.setattr(oe, "get_default_engine", lambda: "easyocr")
    words, used = oe.recognize_image(b"", "chi_tra")
    assert calls == ["easyocr"] and used == "easyocr"


def test_off_skips_local_easyocr_on_the_easyocr_path(monkeypatch):
    calls = []
    monkeypatch.setattr(oe, "is_easyocr_available", lambda: True)
    monkeypatch.setattr(oe, "_easyocr_recognize",
                        lambda *a, **k: calls.append("easyocr") or [{"text": "x"}])
    monkeypatch.setattr(oe, "_tesseract_recognize",
                        lambda *a, **k: calls.append("tesseract") or [{"text": "y"}])
    monkeypatch.setattr(oe, "get_default_engine", lambda: "easyocr")
    words, used = oe.recognize_image(b"", "chi_tra", allow_local_easyocr=False)
    assert "easyocr" not in calls, "關掉之後仍呼叫了本機 EasyOCR → 還是會 SIGILL"
    assert used == "tesseract"


def test_off_also_blocks_the_reverse_fallback(monkeypatch):
    """選 tesseract 時**也會**反向掉進 EasyOCR —— 這條才是最容易漏的。"""
    calls = []
    monkeypatch.setattr(oe, "is_tesseract_available", lambda: True)
    monkeypatch.setattr(oe, "_tesseract_recognize",
                        lambda *a, **k: calls.append("tesseract") or [])   # 認不出東西
    monkeypatch.setattr(oe, "is_easyocr_available", lambda: True)
    monkeypatch.setattr(oe, "_easyocr_recognize",
                        lambda *a, **k: calls.append("easyocr") or [{"text": "x"}])
    words, used = oe.recognize_image(b"", "chi_tra", engine="tesseract",
                                     allow_local_easyocr=False)
    assert "easyocr" not in calls, "反向退路沒擋住 → 選 tesseract 一樣會打掛"
    assert used == "none" and words == []


def test_recognize_text_passes_the_switch_through(monkeypatch):
    seen = {}
    monkeypatch.setattr(oe, "recognize_image",
                        lambda *a, **k: seen.update(k) or ([], "none"))
    oe.recognize_text(b"", "chi_tra", allow_local_easyocr=False)
    assert seen.get("allow_local_easyocr") is False


def test_safety_check_follows_cpu_probe(monkeypatch):
    import app.core.sys_deps as sd
    monkeypatch.setattr(sd, "probe_cpu_simd", lambda: {"ok": False, "missing": ["AVX2"]})
    assert oe.local_easyocr_safe() is False
    monkeypatch.setattr(sd, "probe_cpu_simd", lambda: {"ok": True})
    assert oe.local_easyocr_safe() is True


def test_safety_check_fails_open(monkeypatch):
    """判不出來（非 x86 / 讀不到 flags）一律放行，不可以誤擋掉好機器。"""
    import app.core.sys_deps as sd
    def boom():
        raise RuntimeError("讀不到")
    monkeypatch.setattr(sd, "probe_cpu_simd", boom)
    assert oe.local_easyocr_safe() is True


def test_editor_fallback_asks_for_the_check():
    """編輯器那條內部退路必須真的帶開關，不是只加了參數沒人用。"""
    import importlib
    import inspect
    # 注意：`from app.tools.pdf_editor import router` 拿到的是套件裡的
    # APIRouter 物件（同名遮蔽），不是模組。
    mod = importlib.import_module("app.tools.pdf_editor.router")
    src = inspect.getsource(mod._ocr_bbox)
    assert "allow_local_easyocr=_oe.local_easyocr_safe()" in src
