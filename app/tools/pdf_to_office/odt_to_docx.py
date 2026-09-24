"""ODT → docx 轉換 helper（用 LibreOffice / OxOffice headless）。

ODT-first 路線下，docx output 不再由 jtdt-reform 直寫 OOXML，而是先產 ODT
再用 soffice 引擎自動轉成 docx — 由 LO 引擎自己保證 OOXML 兼容性，避開直
寫 OOXML 的所有 quirks。

外部入口：
- convert_odt_to_docx(odt_path, docx_path) -> dict {ok, error}
"""
from __future__ import annotations

from pathlib import Path


def convert_odt_to_docx(odt_path: Path, docx_path: Path,
                          timeout_sec: float = 180.0) -> dict:
    """把 ODT 轉成 docx。

    args:
        odt_path: 輸入 .odt
        docx_path: 輸出 .docx
        timeout_sec: soffice 子行程上限

    回 {ok: bool, error: str (若失敗)}
    """
    odt_path = Path(odt_path)
    docx_path = Path(docx_path)
    if not odt_path.exists():
        return {"ok": False, "error": f"odt 不存在: {odt_path}"}

    # **一律走 office_convert**（理由同 router.py 的結果預覽）：自己組的
    # `file://{profile_dir}` 在 Windows 上不是合法網址，jtdt-reform 輸出 docx
    # 會卡到逾時才失敗。office_convert 另外還處理了逾時時整棵行程一起殺、
    # 並行上限、先看產出再看回傳碼。
    from ...core import office_convert as _oc
    if not _oc.find_soffice():
        return {"ok": False, "error": "找不到 soffice — 需安裝 OxOffice 或 LibreOffice"}
    try:
        _oc.convert_to_docx(odt_path, docx_path, timeout=timeout_sec)
    except Exception as e:
        return {"ok": False, "error": f"soffice 轉檔失敗: {e}"}
    return {"ok": True}

