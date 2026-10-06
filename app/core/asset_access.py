"""資產庫的圖（印章 / 簽名 / Logo / 浮水印）誰可以看、誰可以蓋進文件 —— 只在這裡定義。

GitHub issue #54（2026-10-05）：一般使用者預設**沒有**「用印與簽名」權限，卻能在
PDF 編輯器的「套印 / 簽名」挑到資產庫裡的公司印章，存檔時直接蓋進 PDF；圖檔網址
（`/assets/{id}/file`）也只要登入就拿得到 —— 拿到印章 PNG 再用「上傳新圖片」貼回去，
等於繞過權限。所以三個地方都照這張表擋：編輯器的資產清單、編輯器存檔、圖檔網址。

規則是「能在哪支工具用到這種資產，就看得到、用得到這種資產」：

* 印章：用印與簽名、騎縫章
* 簽名：用印與簽名
* 浮水印：浮水印
* Logo：不限（常用在信頭，不代表核准；2026-10-05 使用者同意不限）

**保護的是資產庫裡公司正式的章**，不是任何長得像章的圖 —— 使用者自己上傳的圖擋不了，
也不該擋。認證關閉（單機模式）時沒有帳號的概念，一律放行，跟原本一樣。
"""
from __future__ import annotations

from typing import Optional

#: 資產種類 → 能用這種資產的工具（任一個有權限就可以）。`None` ＝ 登入即可。
#: 不在表上的種類一律不給（新增資產種類時要決定它歸誰管，不可以預設全開）。
ASSET_TOOLS: dict[str, Optional[tuple[str, ...]]] = {
    "stamp": ("pdf-stamp", "pdf-seam-stamp"),
    "signature": ("pdf-stamp",),
    "watermark": ("pdf-watermark",),
    "logo": None,
}

#: 擋下時給使用者看的說明（`tr()` 鍵）—— 講得出要找誰、要什麼權限。
DENIED_MESSAGE = "資產庫的印章與簽名要有「用印與簽名」的使用權限才能使用，請洽管理員"


def can_use(request, asset_type: str) -> bool:
    """目前這個請求的使用者能不能看 / 用這種資產。"""
    from . import auth_settings
    if not auth_settings.is_enabled():
        return True
    if asset_type not in ASSET_TOOLS:
        return False
    tools = ASSET_TOOLS[asset_type]
    if tools is None:
        return True
    user = getattr(getattr(request, "state", None), "user", None)
    if not user or not user.get("user_id"):
        return False
    from . import permissions
    return any(permissions.user_can_use_tool(user["user_id"], t) for t in tools)
