"""作業完成通知信的 HTML 版型。

## 為什麼要有 HTML 版

原本的通知信是**純文字**，看起來像系統告警而不是「你的檔案好了」。使用者拿它跟
jt-glogarch 的通知信比較 —— 那邊已經是 multipart（純文字 + 簡單 HTML）。

## 寫 HTML 郵件的實際限制（不是裝飾性的講究）

信件不是網頁，下面每一條都會實際壞掉：

* **`<style>` 區塊會被丟掉**（Gmail 的網頁版直接移除 head）→ 一律**行內樣式**。
* **flex / grid 不能用**（Outlook 走 Word 排版引擎）→ 版面用 `<table>`。
* **外部圖片預設被擋** → 不放 logo 圖檔，改用純色標題列 + 文字。
* **深色模式會反轉顏色** → 每個區塊都明確指定背景色與文字色，不要靠預設值。
* **使用者提供的內容一定要跳脫** —— 檔名是使用者自己取的，直接塞進 HTML 就是
  一封可被注入的信。

所以這裡不用任何模板引擎、不引外部 CSS，就是一份把上述限制寫死的字串組裝。

## 一律附純文字版

`multipart/alternative`：讀信軟體挑得到哪個就顯示哪個。純文字版不是敷衍 ——
命令列讀信、無障礙輔助、以及把信件轉成通知摘要的服務都靠它。
"""
from __future__ import annotations

from html import escape

#: 品牌色（與側欄同一個紫）
_BRAND = "#6d5ae0"
#: 內文的深色（欄位值用）。
_INK = "#334155"
#: 標題的深色。刻意比純黑淡一階 —— 近黑（#0f172a）在信件裡配上方的紫色標題列
#: 顯得過重（使用者回報「標題字顏色太深」）。
_TITLE = "#334155"
_MUTED = "#64748b"
_LINE = "#e5e7eb"
_OK_BG, _OK_FG = "#ecfdf5", "#047857"
_ERR_BG, _ERR_FG = "#fef2f2", "#b91c1c"


def _row(label: str, value: str) -> str:
    """一列「標籤 / 值」。用 table row —— 這是唯一在所有讀信軟體都可靠的排版。"""
    return (
        '<tr>'
        f'<td style="padding:7px 0;color:{_MUTED};font-size:13px;'
        'white-space:nowrap;vertical-align:top;width:72px">'
        f'{escape(label)}</td>'
        f'<td style="padding:7px 0;color:{_INK};font-size:14px;'
        'word-break:break-all">'
        f'{escape(value)}</td>'
        '</tr>'
    )


def _link(url: str, text: str) -> str:
    """信裡的連結。沒有網址時退回純文字的引號寫法。

    沒設定站台網址就**不要**放連結 —— 指向 localhost 的連結點了只會更困惑。
    """
    if not url:
        return f'「{escape(text)}」'
    return (f'<a href="{escape(url, quote=True)}" '
            f'style="color:{_BRAND};font-weight:600;text-decoration:underline">'
            f'{escape(text)}</a>')


def _note_box(kind: str, fallback: str, workspace_url: str,
              jobs_url: str) -> str:
    """結果放在哪裡的那一行。

    句子在**這裡**組，不由呼叫端傳現成的 HTML 進來 —— 那樣遲早會有人把沒跳脫的
    字串傳進來。呼叫端只給「哪一種情況」與網址。
    """
    if kind == "workspace":
        body = "結果已自動存入" + _link(workspace_url, "我的工作區") + "。"
    elif kind == "jobs":
        body = "可到" + _link(jobs_url, "我的作業") + "頁下載結果。"
    elif fallback:
        body = escape(fallback)
    else:
        return ""
    return (
        f'<div style="margin-top:14px;padding:11px 13px;background:#f8fafc;'
        f'border:1px solid {_LINE};border-radius:8px;color:{_MUTED};'
        f'font-size:13px;line-height:1.6">{body}</div>'
    )


def _header(site_name: str, logo_cid: str) -> str:
    """標題列：站台 logo + 站名。

    圖片走 `cid:` 內嵌附件 —— 外部網址會被讀信軟體擋掉（見
    `notify_email_assets` 的說明）。**一定要寫死 width / height**：沒有尺寸時，
    圖還沒載入前版面會跳動，Outlook 甚至會用原始像素大小把版面撐爆。
    """
    logo = ""
    if logo_cid:
        logo = (
            f'<img src="cid:{escape(logo_cid, quote=True)}" width="28" '
            'height="28" alt="" style="display:block;border:0;'
            'border-radius:6px">'
        )
    return (
        # **圓角要畫在這一格自己身上**，不能只靠外層表格的 `overflow:hidden`
        # 去裁切 —— 多數讀信軟體不支援 overflow 裁切子元素，結果就是卡片是圓角、
        # 裡面的色塊卻是直角（使用者實際收到的信就是這樣）。
        f'<tr><td style="background:{_BRAND};padding:14px 24px;'
        'border-radius:12px 12px 0 0">'
        '<table role="presentation" cellpadding="0" cellspacing="0"><tr>'
        + (f'<td style="padding-right:10px">{logo}</td>' if logo else '')
        + '<td style="color:#ffffff;font-size:14px;font-weight:600;'
          f'vertical-align:middle">{escape(site_name)}</td>'
        '</tr></table></td></tr>'
    )


def _headline(headline: str, icon_cid: str) -> str:
    """標題列（工具圖示 + 文字）。圖示用的是網站上同一顆（同色塊、同圖形）。"""
    icon = ""
    if icon_cid:
        icon = (
            f'<td style="padding-right:11px;width:40px">'
            f'<img src="cid:{escape(icon_cid, quote=True)}" width="40" '
            'height="40" alt="" style="display:block;border:0;'
            'border-radius:10px"></td>'
        )
    return (
        '<table role="presentation" cellpadding="0" cellspacing="0"><tr>'
        + icon +
        f'<td style="font-size:19px;font-weight:700;color:{_TITLE};'
        f'line-height:1.4;vertical-align:middle">{escape(headline)}</td>'
        '</tr></table>'
    )


def render(*, site_name: str, ok: bool, tool: str, filename: str,
           elapsed: str, error: str = "", note: str = "",
           action_url: str = "", logo_cid: str = "", icon_cid: str = "",
           note_kind: str = "", workspace_url: str = "") -> str:
    """組出通知信的 HTML。所有參數都當成不可信的字串處理。"""
    status_bg, status_fg = (_OK_BG, _OK_FG) if ok else (_ERR_BG, _ERR_FG)
    status_text = "已完成" if ok else "失敗"
    headline = f"{tool} {status_text}"

    rows = [_row("工具", tool)]
    if filename:
        rows.append(_row("檔案", filename))
    rows.append(_row("耗時", elapsed))
    if error:
        rows.append(_row("原因", error[:300]))

    action = ""
    if action_url and ok:
        # 按鈕用 table + 背景色做 —— `<button>` 在信裡不會被渲染成按鈕。
        action = (
            '<table role="presentation" cellpadding="0" cellspacing="0" '
            'style="margin:18px 0 4px">'
            '<tr><td style="border-radius:8px;background:' + _BRAND + '">'
            f'<a href="{escape(action_url, quote=True)}" '
            'style="display:inline-block;padding:11px 22px;color:#ffffff;'
            'font-size:14px;font-weight:600;text-decoration:none;'
            'border-radius:8px">開啟「我的作業」</a>'
            '</td></tr></table>'
        )

    note_html = _note_box(note_kind, note, workspace_url, action_url)

    # 用 list + join，不要靠字串隱式相接 —— 中間夾了函式呼叫時，
    # 隱式相接會變成語法錯誤（改這裡時踩過一次）。
    parts = [
        '<!DOCTYPE html><html><body style="margin:0;padding:0;'
        'background:#f1f5f9">',
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="background:#f1f5f9;padding:24px 12px">',
        '<tr><td align="center">',
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="max-width:560px;background:#ffffff;border-radius:12px;'
        f'overflow:hidden;border:1px solid {_LINE};'
        'font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\','
        '\'Noto Sans TC\',sans-serif">',

        _header(site_name, logo_cid),

        '<tr><td style="padding:22px 24px">',
        _headline(headline, icon_cid),
        f'<div style="display:inline-block;margin-top:10px;padding:4px 11px;'
        f'border-radius:999px;background:{status_bg};color:{status_fg};'
        f'font-size:12.5px;font-weight:600">{escape(status_text)}</div>',
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'style="margin-top:14px;border-top:1px solid {_LINE}">',
        "".join(rows),
        '</table>',
        action,
        note_html,
        '</td></tr>',

        f'<tr><td style="padding:14px 24px;border-top:1px solid {_LINE};'
        'border-radius:0 0 12px 12px;'
        f'color:{_MUTED};font-size:11.5px;line-height:1.6">'
        '這封信只包含工具名稱、檔名與狀態，<b>不含檔案內容</b>。<br>'
        '不想再收到可到「我的作業 → 通知設定」關閉。'
        '</td></tr>',

        '</table></td></tr></table></body></html>',
    ]
    return "".join(parts)
