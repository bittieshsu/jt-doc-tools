#!/usr/bin/env python3
"""從 `COMPLIANCE.md` 產生介紹站的獨立頁 `docs/compliance.html`。

內容只寫在 `COMPLIANCE.md` 一份；這支把它排進介紹站的版面（導覽列與頁尾取自
`docs/troubleshooting.html`，同一套樣式）。改了 `COMPLIANCE.md` 或那一頁的導覽列 / 頁尾，
重跑這支，再跑 `build-i18n-page.py` 產英文與日文版。

    python3 build-compliance-page.py
"""
from __future__ import annotations

import html
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE / "COMPLIANCE.md"
LAYOUT = HERE / "docs" / "troubleshooting.html"
DST = HERE / "docs" / "compliance.html"
REPO = "https://github.com/jasoncheng7115/jt-doc-tools/blob/main/"

DESC = ("Jason Tools 文件工具箱的 ISO/IEC 27001:2022 與 ISO/IEC 42001:2023 合規支援："
        "本工具提供的控制功能，以及導入單位如何使用、在哪裡留下紀錄。")
TITLE = "合規支援 ISO/IEC 27001:2022 與 42001:2023 | Jason Tools 文件工具箱"


#: 圖示（Lucide 的線條圖示，同介紹站其他區塊的畫法）
_IC = {
    "lock": '<rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/>',
    "users": '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/>'
             '<path d="M22 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/>',
    "shield": '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/><path d="m9 12 2 2 4-4"/>',
    "layers": '<path d="m12 2 10 5-10 5L2 7z"/><path d="m2 17 10 5 10-5"/><path d="m2 12 10 5 10-5"/>',
    "log": '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/>'
           '<path d="M16 13H8"/><path d="M16 17H8"/><path d="M10 9H8"/>',
    "clock": '<circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/>',
    "key": '<circle cx="7.5" cy="15.5" r="5.5"/><path d="m21 2-9.6 9.6"/><path d="m15.5 7.5 3 3L22 7l-3-3"/>',
    "box": '<rect x="4" y="4" width="16" height="16" rx="2"/><rect x="9" y="9" width="6" height="6"/>'
           '<path d="M9 1v3M15 1v3M9 20v3M15 20v3M20 9h3M20 14h3M1 9h3M1 14h3"/>',
    "eyeoff": '<path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94"/>'
              '<path d="M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19"/><path d="m1 1 22 22"/>',
    "db": '<ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/>'
          '<path d="M3 12c0 1.66 4 3 9 3s9-1.34 9-3"/>',
    "globe": '<circle cx="12" cy="12" r="10"/><path d="M2 12h20"/>'
             '<path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/>',
    "test": '<path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><path d="M22 4 12 14.01l-3-3"/>',
    "list": '<path d="M8 6h13"/><path d="M8 12h13"/><path d="M8 18h13"/><path d="M3 6h.01"/>'
            '<path d="M3 12h.01"/><path d="M3 18h.01"/>',
    "check": '<path d="m9 11 3 3L22 4"/><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/>',
    "grid": '<rect x="3" y="3" width="18" height="18" rx="2"/><path d="M3 9h18"/><path d="M3 15h18"/>'
            '<path d="M9 3v18"/>',
    "clip": '<rect x="8" y="2" width="8" height="4" rx="1"/>'
            '<path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2"/><path d="m9 14 2 2 4-4"/>',
}

#: 大節與小節標題的圖示（照標題文字對；新增小節時要補一個，不然測試會擋）
HEAD_ICONS = {
    "ISO/IEC 27001:2022 資訊安全": "shield", "ISO/IEC 42001:2023 人工智慧": "box", "導入時的檢查清單": "clip",
    "身分認證": "lock", "帳號生命週期": "users", "存取控制與職責分離": "shield", "使用者資料隔離": "layers",
    "稽核記錄": "log", "資料留存與清除": "clock", "資料保留與清除": "clock", "機密設定的保護": "key",
    "文件處理的隔離": "box", "敏感資料處理工具": "eyeoff", "備份與復原": "db", "網頁與傳輸安全": "globe",
    "安全開發與測試": "test", "AI 功能清冊": "list", "降低 AI 產出錯誤的機制": "check",
    "導入單位的做法": "clip", "條文與控制項對照": "grid",
    "ISO/IEC 27001:2022 條文": "shield", "ISO/IEC 27001:2022 附錄 A 控制項": "shield",
    "ISO/IEC 42001:2023 條文": "box", "ISO/IEC 42001:2023 附錄 A 控制項": "box",
}

#: 各節開頭的「對應條文 / 對應控制項」一行：md 寫 `**對應控制項**：A.5.16、A.8.5`，
#: 網頁畫成一排小標籤，每一個連到「條文與控制項對照」裡那一列。
_MAP_LINE = re.compile(r"^\*\*(對應條文|對應控制項)\*\*：(.+)$")
#: 對照表第一欄開頭的編號（`A.5.16 身分管理`、`6.1.3、8.3 AI 風險處理`）
_CODE = re.compile(r"^((?:A\.)?\d+(?:\.\d+)+)")


def _std_of(title: str):
    """標題講的是哪一份標準（`27` / `42`），都不是回 None。"""
    if "27001" in title:
        return "27"
    if "42001" in title:
        return "42"
    return None


def anchor(std: str, code: str) -> str:
    """對照表那一列的 id：附錄 A 控制項是 `a27-A.5.16`、條文是 `c42-6.1.3`。"""
    return f"{'a' if code.startswith('A.') else 'c'}{std}-{code}"

#: 小節後面放的畫面截圖（介紹站既有的那幾張；英文、日文版由 build-i18n-page.py 換成該語言的截圖）
FIGS = {
    "身分認證": ("users-multi-realm.png", "使用者管理：本機、LDAP / AD 與單一登入的帳號並存，標出各自的來源"),
    "存取控制與職責分離": ("premissions.png", "權限矩陣：依角色、群組或組織單位指派工具權限"),
    "敏感資料處理工具": ("deident-1.png", "文件去識別化：偵測到的個資逐項列出，確認後再編修"),
    "降低 AI 產出錯誤的機制": ("official-doc.png", "公文撰擬：草稿旁邊列出檢查結果與參考資料，找不到依據的地方會標出來"),
}


def _icon(name: str, size: int = 20) -> str:
    return (f'<svg class="cp-ic" viewBox="0 0 24 24" width="{size}" height="{size}" fill="none" '
            f'stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" '
            f'aria-hidden="true">{_IC[name]}</svg>')


def _head_key(title: str) -> str:
    """「1. 身分認證」「一、ISO/IEC 27001:2022 資訊安全」→ 拿掉前面的編號。"""
    return re.sub(r"^(?:\d+\.\s*|[一二三四五六七八九十]+、)", "", title).strip()


#: 頁首的插圖：一份文件、一面盾牌、一顆 AI 晶片（純幾何，跟介紹站同一組顏色）
HERO_SVG = (
    '<svg class="cp-hero-art" viewBox="0 0 240 180" role="img" aria-hidden="true">'
    '<rect x="34" y="18" width="110" height="140" rx="12" fill="#eef2ff" stroke="#6366f1" stroke-width="3"/>'
    '<rect x="52" y="40" width="74" height="8" rx="4" fill="#a5b4fc"/>'
    '<rect x="52" y="60" width="60" height="6" rx="3" fill="#c7d2fe"/>'
    '<rect x="52" y="76" width="68" height="6" rx="3" fill="#c7d2fe"/>'
    '<rect x="52" y="92" width="50" height="6" rx="3" fill="#c7d2fe"/>'
    '<path d="M52 116l7 7 13-14" fill="none" stroke="#10b981" stroke-width="5" stroke-linecap="round" stroke-linejoin="round"/>'
    '<rect x="80" y="113" width="40" height="6" rx="3" fill="#c7d2fe"/>'
    '<path d="M168 52c14 6 28 8 40 8v34c0 26-18 44-40 54-22-10-40-28-40-54V60c12 0 26-2 40-8z" '
    'fill="#4f46e5" stroke="#312e81" stroke-width="3" stroke-linejoin="round"/>'
    '<path d="M152 98l11 11 21-23" fill="none" stroke="#fff" stroke-width="7" stroke-linecap="round" stroke-linejoin="round"/>'
    '<rect x="150" y="128" width="62" height="40" rx="9" fill="#fff" stroke="#f59e0b" stroke-width="3"/>'
    '<text x="181" y="154" text-anchor="middle" font-family="system-ui, sans-serif" font-size="16" '
    'font-weight="700" fill="#b45309">AI</text>'
    '<path d="M160 128v-6M172 128v-6M190 128v-6M202 128v-6M160 168v6M172 168v6M190 168v6M202 168v6" '
    'stroke="#f59e0b" stroke-width="3" stroke-linecap="round"/>'
    '</svg>'
)


def _png_size(path: Path) -> tuple[int, int]:
    """PNG 的寬高（讀檔頭，不需要 Pillow）。

    圖片要寫明寬高，瀏覽器才會先留好位置：沒寫的話，從左側目錄跳到下面某一節時，
    上方延遲載入的截圖載進來會把整頁往下推，停在錯的那一節。"""
    import struct
    with open(path, "rb") as f:
        head = f.read(24)
    return struct.unpack(">II", head[16:24])


def _inline(s: str) -> str:
    s = html.escape(s, quote=False)
    s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    # 文件裡提到的另外幾份說明連到 GitHub 上那一份
    return re.sub(r"<code>((?:TEST_PLAN_SECURITY|OPS|CHANGELOG|LLM)\.md)</code>",
                  lambda m: f'<a href="{REPO}{m.group(1)}" target="_blank" rel="noopener">'
                            f"<code>{m.group(1)}</code></a>", s)


def _body(md: str) -> str:
    lines = md.split("\n")
    out: list[str] = []
    toc: list[tuple[str, str]] = []
    lst = [None]

    def close():
        if lst[0]:
            out.append(f"</{lst[0]}>")
            lst[0] = None

    pending = [None]

    def flush():
        """上一個小節的截圖放在小節最後（表格下面）。"""
        if pending[0]:
            img, cap = pending[0]
            w, h = _png_size(HERE / "docs" / "screenshots" / img)
            out.append(f'<figure class="cp-fig"><img src="screenshots/{img}" alt="{html.escape(cap)}" '
                       f'width="{w}" height="{h}" loading="lazy"><figcaption>{_inline(cap)}</figcaption></figure>')
            pending[0] = None

    i, n, m = 0, 0, 0
    std = [None]          # 目前這一節講的是哪一份標準
    in_map = [False]      # 是不是在「條文與控制項對照」那一大節
    while i < len(lines):
        ln = lines[i]
        mm = _MAP_LINE.match(ln)
        if ln.startswith("# "):
            out.append(f'<h1 class="ts-h1">{_inline(ln[2:])}</h1>')
        elif ln.startswith("## "):
            close()
            flush()
            n, m = n + 1, 0
            toc.append((f"cp{n}", ln[3:], 2))
            std[0] = _std_of(ln[3:])
            in_map[0] = _head_key(ln[3:]) == "條文與控制項對照"
            ic = _icon(HEAD_ICONS[_head_key(ln[3:])], 24)
            out.append(f'<h2 class="cp-h2" id="cp{n}"><span class="cp-h-ic">{ic}</span>{_inline(ln[3:])}</h2>')
        elif ln.startswith("### "):
            close()
            flush()
            m += 1
            toc.append((f"cp{n}-{m}", ln[4:], 3))
            if in_map[0]:
                std[0] = _std_of(ln[4:])
            key = _head_key(ln[4:])
            ic = _icon(HEAD_ICONS[key], 18)
            out.append(f'<h3 class="cp-h3" id="cp{n}-{m}"><span class="cp-h-ic">{ic}</span>{_inline(ln[4:])}</h3>')
            pending[0] = FIGS.get(key)
        elif mm:
            close()
            codes = [c.strip() for c in mm.group(2).split("、") if c.strip()]
            chips = "".join(f'<a class="cp-ctl" href="#{anchor(std[0], c)}">{html.escape(c)}</a>'
                            for c in codes)
            out.append(f'<div class="cp-map"><span class="cp-map-k">{mm.group(1)}</span>{chips}</div>')
        elif ln.startswith("|"):
            close()
            rows = []
            while i < len(lines) and lines[i].startswith("|"):
                rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            wrap = "cp-table-wrap cp-maptab" if in_map[0] else "cp-table-wrap"
            t = [f'<div class="{wrap}"><table class="cp-table"><thead><tr>']
            t += [f"<th>{_inline(h)}</th>" for h in rows[0]]
            t.append("</tr></thead><tbody>")
            for r in rows[2:]:
                code = _CODE.match(r[0]) if in_map[0] and std[0] else None
                rid = f' id="{anchor(std[0], code.group(1))}"' if code else ""
                t.append(f"<tr{rid}>" + "".join(f"<td>{_inline(c)}</td>" for c in r) + "</tr>")
            t.append("</tbody></table></div>")
            out.append("".join(t))
            continue
        elif ln.startswith("- ") or re.match(r"^\d+\. ", ln):
            kind = "ul" if ln.startswith("- ") else "ol"
            if lst[0] != kind:
                close()
                out.append(f'<{kind} class="cp-list">')
                lst[0] = kind
            out.append(f"<li>{_inline(re.sub(r'^(- |\d+\. )', '', ln))}</li>")
        elif not ln.strip() or ln.strip() == "---":
            close()
        else:
            close()
            para = [ln]
            while (i + 1 < len(lines) and lines[i + 1].strip()
                   and not re.match(r"^(#|\||- |\d+\. |---)", lines[i + 1])):
                i += 1
                para.append(lines[i])
            out.append(f'<p class="cp-p">{_inline("".join(para))}</p>')
        i += 1
    close()
    flush()
    # 頁首：標題與開頭兩段說明在左、插圖在右
    h1 = next(k for k, x in enumerate(out) if x.startswith("<h1"))
    first = next(k for k, b in enumerate(out) if b.startswith("<p"))
    out[h1:first + 2] = (['<div class="cp-hero"><div class="cp-hero-text">'] + out[h1:first + 2]
                         + ["</div>", HERO_SVG, "</div>"])
    # 三大節的目錄放在開頭兩段說明之後
    nav = ('<div class="ts-toc cp-toc">'
           + "".join(f'<a href="#{sid}">{_icon(HEAD_ICONS[_head_key(t)], 15)}{_inline(t)}</a>'
                     for sid, t, lv in toc if lv == 2) + "</div>")
    out.insert(out.index("</div>", out.index(HERO_SVG)) + 1, nav)
    return "\n".join(out)


def build() -> str:
    lay = LAYOUT.read_text(encoding="utf-8")
    head = lay[:lay.index('<main class="container ts-main">')]
    foot = lay[lay.index('<footer class="footer">'):]
    head = re.sub(r'<meta name="description" content="[^"]*">',
                  f'<meta name="description" content="{html.escape(DESC)}">', head, count=1)
    head = re.sub(r"<title>[^<]*</title>", f"<title>{html.escape(TITLE)}</title>", head, count=1)
    head = head.replace('value="troubleshooting.html"', 'value="compliance.html"') \
               .replace('value="troubleshooting-en.html"', 'value="compliance-en.html"') \
               .replace('value="troubleshooting-ja.html"', 'value="compliance-ja.html"')
    return (head + '<main class="container ts-main cp-main">\n'
            + _body(SRC.read_text(encoding="utf-8")) + "\n</main>\n\n" + foot)


if __name__ == "__main__":
    DST.write_text(build(), encoding="utf-8")
    print(f"{DST.name}: 產生完成")
