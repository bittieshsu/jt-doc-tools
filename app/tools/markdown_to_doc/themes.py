"""CSS theme presets for markdown-to-doc rendering.

Each theme defines a full <style> block applied to the rendered HTML before
soffice converts it to PDF / DOCX / ODT. All themes are designed to be
print-friendly (light backgrounds, dark text), since they target paper-output
formats.

Web font stacks list CJK fonts first (PingFang TC / Microsoft JhengHei /
Noto CJK) so Chinese content always picks a readable font on whatever the
host has installed.
"""
from __future__ import annotations

# Shared base used by every theme — resets, code styling, table baseline,
# print page setup. Theme-specific colours / fonts come on top.
_BASE = """
@page { size: A4; margin: 22mm 20mm 22mm 20mm; }
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; }
body {
  line-height: 1.7;
  font-size: 11pt;
  -webkit-print-color-adjust: exact;
  print-color-adjust: exact;
}
h1, h2, h3, h4, h5, h6 {
  line-height: 1.3; margin: 1.4em 0 0.6em;
  font-weight: 700;
}
h1 { font-size: 24pt; }
h2 { font-size: 18pt; }
h3 { font-size: 15pt; }
h4 { font-size: 13pt; }
h5 { font-size: 12pt; }
h6 { font-size: 11pt; opacity: 0.85; }
p { margin: 0.7em 0; }
ul, ol { margin: 0.6em 0; padding-left: 1.6em; }
li { margin: 0.25em 0; }
li > p { margin: 0.3em 0; }
code, kbd, samp {
  font-family: 'SF Mono', 'JetBrains Mono', Menlo, Consolas,
               'Liberation Mono', monospace;
  font-size: 0.88em;
}
pre {
  /* **`padding` 在 soffice 裡無效** —— 實測它被當成縮排（框和字一起右移，
     中間還是沒有空隙），`div` 包起來連底色都不畫，包進單格表格則會讓長行
     不折行、整塊超出頁面。三種都試過。
     所以改成「整塊色帶往右縮排、不畫框線」：沒有框線就沒有「字貼著框」，
     而縮排讓程式碼跟正文明顯分開。 */
  margin: 0.8em 0 0.8em 12pt;
  font-size: 9.5pt; line-height: 1.55;
  page-break-inside: avoid;
}
pre code { background: transparent !important; padding: 0; font-size: inherit; }
table { border-collapse: collapse; margin: 0.8em 0; width: auto; }
/* 表格的列高：soffice 把內文的行高與段落間距也套進儲存格，一列撐得很高、字貼在上面
   （使用者 2026-09-24 截圖回報）。儲存格裡明確收掉。 */
th, td { padding: 3pt 8pt; margin: 0; line-height: 1.35; vertical-align: top; }
img { max-width: 100%; }
hr { border: 0; margin: 1.4em 0; }
blockquote {
  margin: 0.8em 0; padding: 4pt 14pt;
  page-break-inside: avoid;
}
blockquote > :first-child { margin-top: 0; }
blockquote > :last-child { margin-bottom: 0; }
a { text-decoration: none; }
a:hover { text-decoration: underline; }
"""

# Heading-anchor / footnote helpers
_EXTRAS = """
sup.fn-ref a { font-size: 0.75em; vertical-align: super; text-decoration: none; }
.task-list-item { list-style: none; }
.task-list-item input { margin-right: 6px; }
"""

#: 每個主題對應的 pygments 語法上色樣式。
#:
#: **`mono` 是 `None`（不上色）** —— 那個主題的承諾就是「純黑白配色，
#: 無修飾，適合需要絕對中性視覺的場合」。在上面加顏色等於把它的用途毀掉，
#: 而使用者選它的理由正是不要顏色。
#: 行內 `code` 的顏色。**改成由渲染器寫成內嵌樣式，不再放 CSS 規則。**
#:
#: 原因是 soffice 的 HTML 匯入：只要 `code { color: X }` 這條規則存在，
#: 它就把整個 `<code>` 當成一段字元樣式，**把裡面的上色 span 全部丟掉** ——
#: 於是 HTML 預覽有顏色、轉出來的 PDF / DOCX 沒有，而且完全不會報錯。
#:
#: 實測過三種寫法：`!important` **soffice 不理**、
#: `:not(pre) > code` **soffice 不支援、整條規則被丟掉**。
#: **只有內嵌樣式會被帶過去。**
INLINE_CODE_COLORS: dict[str, str] = {
    "classic": "#be185d", "github": "#cf222e", "academic": "#444",
    "book": "#8b3a00", "report": "#c53030", "mono": "#000",
}


#: 程式碼區塊的底色。**用 HTML 的 `bgcolor` 屬性套**，不走 CSS。
CODE_BG: dict[str, str] = {
    "classic": "#f1f5f9", "github": "#f6f8fa", "academic": "#f9f9f9",
    "book": "#f3e9d5", "report": "#edf2f7", "mono": "#f5f5f5",
}


def code_bg(theme_id: str) -> str:
    return CODE_BG.get(theme_id, "#f1f5f9")


#: 表格的表頭底色 / 框線顏色 / 隔行底色。**用 HTML 屬性套**（`bgcolor` / `bordercolor`），
#: 不走 CSS —— soffice 把 `th { background }` 套在**文字**上而不是整格，匯出的 PDF
#: 表頭變成「一小塊深色只包住字」（使用者 2026-09-24 截圖回報會議摘要的「誰講了多少」）；
#: `tbody tr:nth-child(even)` 的隔行底色則完全沒套上。`None` ＝ 不上色。
TABLE_STYLES: dict[str, dict] = {
    "classic":  {"head_bg": "#eff6ff", "border": "#cbd5e1", "zebra": "#f8fafc"},
    "github":   {"head_bg": "#f6f8fa", "border": "#d0d7de", "zebra": "#f6f8fa"},
    "academic": {"head_bg": None,      "border": "#555555", "zebra": None},
    "book":     {"head_bg": "#efe1c5", "border": "#d6c3a0", "zebra": "#faf5ec"},
    "report":   {"head_bg": "#2c5282", "border": "#cbd5e1", "zebra": "#f7fafc"},
    "mono":     {"head_bg": "#e8e8e8", "border": "#999999", "zebra": None},
}


def table_style(theme_id: str) -> dict:
    return TABLE_STYLES.get(theme_id, TABLE_STYLES["classic"])


def inline_code_color(theme_id: str) -> str:
    return INLINE_CODE_COLORS.get(theme_id, "#be185d")


CODE_STYLES: dict[str, str | None] = {
    "classic": "friendly",      # 柔和、白底
    "github":  "default",       # 接近 GitHub README 的觀感
    "academic": "bw",           # 論文/公文：只用粗體斜體，不用顏色
    "book":    "friendly",
    "report":  "vs",            # 商務：偏保守的藍綠
    "mono":    None,            # 不上色
}


def code_style(theme_id: str) -> str | None:
    return CODE_STYLES.get(theme_id, "friendly")


THEMES: dict[str, dict] = {
    "classic": {
        "name": "清爽（預設）",
        "desc": "白底、藍色標題、暗灰內文，閱讀舒適、列印俐落。",
        "css": _BASE + _EXTRAS + """
body {
  font-family: 'Noto Sans TC', -apple-system, 'PingFang TC',
               'Microsoft JhengHei', sans-serif;
  color: #1e293b; background: #ffffff;
}
h1 { color: #1e3a8a; border-bottom: 3px solid #3b82f6; padding-bottom: 6pt; }
h2 { color: #1e40af; border-bottom: 1px solid #cbd5e1; padding-bottom: 3pt; }
h3 { color: #1d4ed8; }
h4, h5, h6 { color: #334155; }
strong { color: #0f172a; }
a { color: #2563eb; }
pre { background: #f1f5f9; color: #1e293b;  }
pre code { color: inherit; background: transparent; }
table { font-size: 10.5pt; }
th { background: #eff6ff; color: #1e3a8a;  }
td {  }
blockquote { background: #f8fafc; border-left: 4px solid #94a3b8; color: #475569; }
hr { border-top: 1px dashed #cbd5e1; }
""",
    },
    "github": {
        "name": "GitHub 風",
        "desc": "模擬 GitHub README 樣式，工程師熟悉的灰白配色。",
        "css": _BASE + _EXTRAS + """
body {
  font-family: -apple-system, 'Segoe UI', 'Noto Sans TC',
               'PingFang TC', 'Microsoft JhengHei', sans-serif;
  color: #24292f; background: #ffffff;
}
h1 { color: #1f2328; border-bottom: 1px solid #d0d7de; padding-bottom: 6pt; }
h2 { color: #1f2328; border-bottom: 1px solid #d0d7de; padding-bottom: 4pt; }
h3, h4, h5, h6 { color: #1f2328; }
a { color: #0969da; }
pre { background: #f6f8fa; color: #24292f;  }
pre code { color: inherit; background: transparent; }
table th { background: #f6f8fa;  }
table td {  }
blockquote { background: #f6f8fa; border-left: 4px solid #d0d7de; color: #59636e; }
hr { border-top: 1px solid #d0d7de; }
""",
    },
    "academic": {
        "name": "學術論文（襯線）",
        "desc": "Times-style 襯線字、保守配色，適合論文 / 公文 / 法規。",
        "css": _BASE + _EXTRAS + """
@page { margin: 25mm 22mm; }
body {
  font-family: 'Source Han Serif TC', 'Noto Serif TC',
               'Times New Roman', 'PingFang TC', serif;
  color: #1c1c1c; background: #ffffff;
  font-size: 11pt; line-height: 1.85;
  text-align: justify;
}
h1 { font-size: 22pt; text-align: center; margin: 1em 0 1em; color: #1c1c1c; }
h2 { color: #1c1c1c; border-bottom: 1.5px solid #1c1c1c; padding-bottom: 3pt; }
h3 { color: #2c2c2c; }
strong { font-weight: 700; }
a { color: #1c1c1c; text-decoration: underline; }
pre { background: #f9f9f9; color: #1c1c1c;  }
pre code { color: inherit; background: transparent; }
table { font-size: 10.5pt; margin: 1em auto; }
th, td {  border-bottom: 1px solid #1c1c1c; }
th {  background: transparent; }
blockquote { border-left: 3px solid #444; color: #444; font-style: italic; background: transparent; }
hr { border-top: 1px solid #1c1c1c; }
""",
    },
    "book": {
        "name": "書籍 / 暖色",
        "desc": "米色紙底、棕色標題、襯線字。閱讀感舒適，適合報告 / 書本印刷。",
        "css": _BASE + _EXTRAS + """
@page { margin: 24mm 24mm; }
body {
  font-family: 'Noto Serif TC', 'Source Han Serif TC',
               Georgia, 'PingFang TC', serif;
  color: #3d2914; background: #fbf6ec;
  line-height: 1.85;
}
h1 { color: #8b4513; border-bottom: 3px double #8b4513; padding-bottom: 6pt; text-align: center; }
h2 { color: #a0522d; }
h3 { color: #b8743f; }
h4, h5, h6 { color: #6f3a1e; }
strong { color: #6f3a1e; }
a { color: #8b4513; }
pre { background: #f3e9d5; color: #3d2914;  }
pre code { color: inherit; background: transparent; }
table { font-size: 10.5pt; }
th { background: #efe1c5; color: #6f3a1e;  }
td {  }
blockquote { background: #f3e9d5; border-left: 4px solid #b8743f; color: #6f3a1e; font-style: italic; }
hr { border-top: 1px solid #c9a87e; }
""",
    },
    "report": {
        "name": "商務報告",
        "desc": "深藍標題、灰色強調、嚴謹清晰，適合對外提案 / 季報。",
        "css": _BASE + _EXTRAS + """
body {
  font-family: 'Noto Sans TC', -apple-system, 'PingFang TC',
               'Microsoft JhengHei', sans-serif;
  color: #2d3748; background: #ffffff;
}
h1 {
  color: #ffffff; background: #2c5282;
  padding: 12pt 18pt; margin: 0 0 18pt -20mm; margin-right: -20mm;
  font-size: 22pt; letter-spacing: 0.04em;
}
h2 { color: #2c5282; border-bottom: 2px solid #2c5282; padding-bottom: 4pt; }
h3 { color: #2b6cb0; }
h4 { color: #4a5568; }
strong { color: #1a365d; }
a { color: #2c5282; }
pre { background: #edf2f7; color: #1a202c;  }
pre code { color: inherit; background: transparent; }
table { font-size: 10.5pt; box-shadow: 0 1pt 3pt rgba(0,0,0,0.08); }
th { background: #2c5282; color: #ffffff;  font-weight: 600; }
td {  }
tbody tr:nth-child(even) td { background: #f7fafc; }
blockquote { background: #edf2f7; border-left: 4px solid #2c5282; color: #2d3748; }
hr { border-top: 2px solid #2c5282; }
""",
    },
    "mono": {
        "name": "極簡黑白",
        "desc": "純黑白配色，無修飾，適合需要絕對中性視覺的場合。",
        "css": _BASE + _EXTRAS + """
body {
  font-family: 'Noto Sans TC', -apple-system, 'PingFang TC',
               'Microsoft JhengHei', sans-serif;
  color: #000000; background: #ffffff;
}
h1 { border-bottom: 3px solid #000; padding-bottom: 4pt; }
h2 { border-bottom: 1.5px solid #000; padding-bottom: 3pt; }
h3, h4, h5, h6 { color: #000; }
strong { color: #000; }
a { color: #000; text-decoration: underline; }
pre { background: #f5f5f5; color: #000;  }
pre code { color: inherit; background: transparent; }
table { font-size: 10.5pt; }
th, td {  }
th { background: #e8e8e8; }
blockquote { border-left: 4px solid #000; color: #333; background: transparent; }
hr { border-top: 1.5px solid #000; }
""",
    },
}


def get_theme(name: str) -> dict:
    """Return theme dict (name / desc / css). Falls back to classic if unknown."""
    return THEMES.get(name) or THEMES["classic"]


def theme_options() -> list[dict]:
    """For UI: list of {id, name, desc} suitable for a select/radio."""
    return [{"id": k, "name": v["name"], "desc": v["desc"]}
            for k, v in THEMES.items()]


# 字型選項 — 每個是一個 CSS font-family stack,前段 CJK + fallback,soffice 找到哪個用哪個
FONTS: dict[str, dict] = {
    "default": {
        "name": "預設(隨主題)",
        "stack": "",
        "desc": "使用主題內建字型",
    },
    "noto-sans": {
        "name": "Noto Sans TC(黑體)",
        "stack": "'Noto Sans TC', 'Source Han Sans TC', 'PingFang TC', 'Microsoft JhengHei', 'Heiti TC', sans-serif",
        "desc": "Google Noto 思源黑體系列繁中，易讀現代感",
    },
    "noto-serif": {
        "name": "Noto Serif TC(明體)",
        "stack": "'Noto Serif TC', 'Source Han Serif TC', 'Songti TC', 'Times New Roman', serif",
        "desc": "Google Noto 思源宋體系列繁中，正式書面感",
    },
    "kaiti": {
        "name": "標楷體 / Kaiti",
        "stack": "'DFKai-SB', 'BiauKai', 'Kaiti TC', 'STKaiti', 'AR PL UKai TW', cjk-kaiti, serif",
        "desc": "公文 / 教材常用，需主機已安裝楷體",
    },
    "mingti": {
        "name": "新細明體 / MingLiU",
        "stack": "'PMingLiU', 'MingLiU', 'AR PL UMing TW', 'Songti TC', cjk-mingti, serif",
        "desc": "Windows 早期預設，需主機已安裝細明體",
    },
    "monospace": {
        "name": "等寬字型",
        "stack": "'JetBrains Mono', 'Fira Code', Menlo, Consolas, 'Liberation Mono', 'Noto Sans Mono CJK TC', monospace",
        "desc": "全篇等寬，適合程式碼 / 技術文件",
    },
}


def get_font(name: str) -> dict:
    """Return font dict. Falls back to default if unknown."""
    return FONTS.get(name) or FONTS["default"]


def font_options() -> list[dict]:
    """給下拉選單用的字型清單。

    **`stack` 一定要一起回**。模板寫的是
    `{% if f.stack %}data-preview-style="font-family: {{ f.stack }};"{% endif %}`，
    少了它那個條件永遠是 false → 屬性一次都沒有渲染過 → 使用者在下拉裡選
    標楷體 / 新細明體 / 等寬時**每一項都用同一個介面預設字型顯示**，看不出
    自己選了什麼（v1.14.31 對抗式驗證：六個選項的 data-preview-style 全是
    null）。`FONTS` 裡六款有五款早就寫好 stack 了，只是沒有被送出去。
    """
    return [{"id": k, "name": v["name"], "desc": v["desc"],
             "stack": v.get("stack") or ""}
            for k, v in FONTS.items()]


def font_css_override(font_id: str) -> str:
    """Return CSS to override body font-family if font_id != 'default'."""
    f = get_font(font_id)
    if not f.get("stack"):
        return ""
    # 強制覆蓋 body / h1-h6 / p / li / td / th 的 font-family,但 code/pre 維持等寬
    return (
        f"\nbody, h1, h2, h3, h4, h5, h6, p, li, td, th, blockquote "
        f"{{ font-family: {f['stack']}; }}\n"
    )


#: **轉成 .odt / .docx 時要疊上去的覆寫。**
#:
#: soffice 的 HTML 匯入**會保留文字顏色、但丟掉段落底色**（實測：`report`
#: 主題的 `h1 { color:#fff; background:#2c5282 }` 轉成 ODT 之後樣式裡只剩
#: `fo:color="#ffffff"`，底色與內距都沒了）→ **白字白底，整個標題看不見**
#: （2026-09-19 使用者回報「匯出 .odt 時 標題字是白色 結果看不到」）。
#:
#: **PDF 沒有這個問題**（底色畫得出來），所以不動主題本身 ——
#: 那個橫幅在 PDF 上是好看的。只有文件格式疊這一層。
#:
#: **通則：不要讓可讀性依賴底色。** 底色是最容易在轉檔途中掉的東西，
#: 而掉了之後的症狀是「什麼都看不到」，不是「顏色怪怪的」。
_OFFICE_SAFE_CSS = """
/* 轉成 .odt / .docx 時：底色會掉，所以淺色文字要改回深色 */
h1 { color: #1a365d; background: transparent; padding: 0 0 6pt 0;
     margin: 0 0 14pt 0; border-bottom: 3px solid #2c5282; }
th { background: transparent; color: #1a365d;
     border-bottom: 2px solid #2c5282; }
"""


def office_safe_css() -> str:
    """疊在主題 CSS 後面，給 `.odt` / `.docx` 用（見 `_OFFICE_SAFE_CSS`）。"""
    return _OFFICE_SAFE_CSS
