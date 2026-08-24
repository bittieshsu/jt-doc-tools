"""本機「有沒有可用的中文字型」—— 給**一般使用者**看的那一層。

偵測與安裝指引其實早就有了（管理區的「相依套件檢查」與 `jtdt` 的相依摘要），
但那兩個地方**一般使用者都進不去**。真正會踩到的人，是拿浮水印打上中文、
發現印出來是一排方框的那一位 —— 而他看到的畫面上原本沒有任何線索，只會
以為是這個工具壞了。

判準刻意**不是**「標準路徑上有沒有那幾個檔案」（`sys_deps._probe_cjk_fonts`
是那樣做的，那份是給管理區看系統狀態用），而是問「**繪製的時候真的拿得到
字型檔嗎**」—— 走 `font_catalog.best_cjk_path()`，跟表單填寫 / 用印 / 頁碼 /
書籤 / 逐句翻譯實際取字型的是同一條路徑。兩個方向上的差別都是真的：

* 管理員從「字型管理」上傳了一支中文字型 → 標準路徑上仍然沒有檔案，但工具
  用得到 → 不該再叫使用者去裝字型（叫了他也裝不了，他不是管理員）。
* 系統裝了 CJK 字型卻被管理員在字型管理裡隱藏 → 檔案在，工具挑不到 → 該提醒。

**沒有中文字型時各工具的實際下場不一樣**，所以提示的措辭只講「可能不正確」，
不講死：走 PyMuPDF 的（頁碼 / 書籤 / 表單填寫 / 編輯器）會退回內建的
`china-t` 系列，中文畫得出來但字形不是文件上寫的那一套；走 Pillow 的
（浮水印、個資限用章）最後一段 fallback 會回一支畫不出中文的字型，那才是
真的一排缺字方框。
"""
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def cjk_status(cjk: str = "traditional") -> dict:
    """回 ``{"ok": bool, "font": str, "install_cmd": str}``。

    `ok=True` 代表黑體或明體**至少有一套**挑得到 —— 只有一套時工具仍會用
    另一套的替代品，字形會有出入，但不會缺字，不值得為此嚇使用者。
    """
    from . import font_catalog

    for style in ("sans", "serif"):
        try:
            hit = font_catalog.best_cjk_path(style, cjk)
        except Exception:  # noqa: BLE001 — 偵測不該讓工具頁整頁壞掉
            logger.debug("best_cjk_path(%s) failed", style, exc_info=True)
            hit = None
        if hit:
            return {"ok": True, "font": Path(hit[0]).name, "install_cmd": ""}

    # 安裝指令只有 `sys_deps._DEPS` 那一份，這裡引用不另外抄
    # （抄一份就是下一個會漂掉的清單）。
    try:
        from . import sys_deps
        cmd = sys_deps.install_cmd_for("cjk-fonts")
    except Exception:  # noqa: BLE001
        cmd = ""
    return {"ok": False, "font": "", "install_cmd": cmd}
