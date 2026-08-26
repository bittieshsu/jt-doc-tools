"""從**字形本身**還原 PDF 文字 —— 對付壞掉的 ToUnicode 對照表。

## 這是在解什麼

有些 PDF 的 ToUnicode 對照表是壞的：把每個字碼都對應到同一個符號（常見是
圓點 `•`），或對應到一堆罕用字。**畫面完全正常** —— 因為畫面是照字形畫的，
跟對照表無關；但任何「抽文字」的動作拿到的都不是原文。

客戶回報（2026-08-26）：在 PDF 編輯器點文件上原本的中文要修改，文字框裡
整排變成 `••••••`。

## 為什麼不是用 OCR

編輯器本來有一條 OCR 退路（給「字形真的認不出來」的掃描件用）。但這個情況
**資訊完全沒有遺失**：PDF 記著每個字用了哪個字形編號，而內嵌字型檔裡就有
字形編號 ↔ Unicode 的對照表。反查回去是**精確**的，不是猜的。

OCR 相比之下：會認錯字、要幾秒鐘、要下載模型，而且本機 EasyOCR 在缺 AVX2
的機器上會把整個服務打掛。能精確反查就不該用 OCR。

## 做法

1. `page.get_texttrace()` —— 每個字給 `(ucs, gid, ...)`，`ucs` 是（壞掉的）
   對照表結果，`gid` 是**真正畫出來的字形編號**。
2. `doc.extract_font(xref)` 把內嵌字型抽出來，讀它自己的 `cmap`
   （Unicode → 字形名），反過來得到 字形編號 → Unicode。
3. 兩者一接，字就回來了。

字型被子集化過也沒關係 —— 子集化通常會保留 cmap。真的沒有 cmap（或整份是
CID-keyed 沒有可用字形名）時回空字串，讓呼叫端自己決定要不要退回 OCR。
"""
from __future__ import annotations

import hashlib
import io
import logging
from typing import Optional

log = logging.getLogger(__name__)

# 字型檔不小（CJK 動輒數百 KB），同一份文件連點好幾個字很常見 → 快取
# 反查表。鍵用字型內容的指紋，不用 xref（換文件就不會撞在一起）。
_gid_map_cache: dict[str, dict] = {}
_CACHE_MAX = 24


def _font_fingerprint(buf: bytes) -> str:
    """快取鍵用的指紋。用 sha256 純粹是為了不讓源碼掃描把 sha1 標成告警
    （這裡沒有任何安全用途），只讀前 4 KB 讓大字型也很快。"""
    h = hashlib.sha256()
    h.update(str(len(buf)).encode("ascii"))
    h.update(buf[:4096])
    return h.hexdigest()


def _gid_to_unicode(buf: bytes) -> dict:
    """字形編號 → Unicode 字碼。讀不出來回空 dict。"""
    key = _font_fingerprint(buf)
    cached = _gid_map_cache.get(key)
    if cached is not None:
        return cached
    table: dict = {}
    try:
        from fontTools.ttLib import TTFont
        font = TTFont(io.BytesIO(buf), fontNumber=0, lazy=True)
        order = font.getGlyphOrder()
        # cmap 是 Unicode → 字形名；反過來建 字形名 → Unicode。
        # 一個字形可能對應多個字碼（異體字），取最小的那個（通常是本體）。
        name_to_uni: dict = {}
        for uni, gname in font.getBestCmap().items():
            if gname not in name_to_uni or uni < name_to_uni[gname]:
                name_to_uni[gname] = uni
        for gid, gname in enumerate(order):
            uni = name_to_uni.get(gname)
            if uni is not None:
                table[gid] = uni
    except Exception as e:
        log.debug("讀不出內嵌字型的對照表：%s", e)
        table = {}
    if len(_gid_map_cache) >= _CACHE_MAX:
        _gid_map_cache.clear()
    _gid_map_cache[key] = table
    return table


def _page_font_tables(doc, page) -> list[dict]:
    """這一頁所有內嵌字型的反查表。"""
    out = []
    for f in page.get_fonts(full=False):
        xref = f[0]
        try:
            _name, _ext, _subtype, buf = doc.extract_font(xref)
        except Exception:
            continue
        if not buf:
            continue
        table = _gid_to_unicode(buf)
        if table:
            out.append(table)
    return out


def recover_text_in_bbox(page, bbox, doc=None) -> str:
    """把 bbox 範圍內的文字從字形反查回來。查不到回空字串。

    `page` 是 PyMuPDF 的 Page；`bbox` 是 (x0, y0, x1, y1)（PDF 點）。
    """
    try:
        import fitz
    except Exception:
        return ""
    doc = doc if doc is not None else page.parent
    try:
        rect = fitz.Rect(*[float(v) for v in bbox])
    except Exception:
        return ""
    tables = _page_font_tables(doc, page)
    if not tables:
        return ""

    try:
        trace = page.get_texttrace()
    except Exception as e:
        log.debug("get_texttrace 失敗：%s", e)
        return ""

    picked = []
    for item in trace:
        if item.get("type") not in (None, 0):     # 0 = 一般文字（非裁切路徑）
            continue
        for ch in item.get("chars") or ():
            try:
                gid = ch[1]
                origin = ch[2]
            except Exception:
                continue
            # 用字的原點（基線起點）判斷在不在框內；用整個字框會讓上下行
            # 的字互相沾到。垂直方向稍微放寬，基線本來就貼著框底。
            #
            # **水平方向必須是半開區間**：一個字的原點就是它的左緣，而
            # 下一個字的原點正好落在這個框的右緣 —— 用閉區間會把隔壁的字
            # 一起抓進來（實測 `□` 變成 `□主`、`□刪除` 尾巴多一個字元）。
            ox, oy = float(origin[0]), float(origin[1])
            if not (rect.x0 - 0.5 <= ox < rect.x1 - 0.5):
                continue
            if not (rect.y0 - 2 <= oy <= rect.y1 + 2):
                continue
            uni = None
            for table in tables:
                uni = table.get(gid)
                if uni is not None:
                    break
            # 反查到控制字元表示這張表本身不對勁（實測踩到 NUL）——
            # 當成查不到，讓整段放棄比塞一個看不見的字進去安全。
            if uni is not None and uni < 0x20:
                uni = None
            picked.append((item.get("seqno", 0), ox, uni))

    if not picked:
        return ""
    picked.sort(key=lambda t: (t[0], t[1]))
    # 有任何一個字查不到就整段放棄 —— 半段正確、半段問號比整段失敗更糟，
    # 使用者會以為那就是原文（缺字方框那次的教訓）。
    if any(uni is None for _seq, _x, uni in picked):
        return ""
    return "".join(chr(uni) for _seq, _x, uni in picked)
