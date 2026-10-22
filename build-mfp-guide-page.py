#!/usr/bin/env python3
"""產生介紹站的「事務機掃描取件」使用說明頁 `docs/mfp-fetch.html`。

內容只寫在這一支；導覽列與頁尾取自 `docs/troubleshooting.html`（同一套樣式），頁首、頁內導覽、
區塊沿用資料保護頁（`cp-*`），步驟、流程圖、廠牌對照用 `mg-*`（`style.css`）。
改完重跑這支，再跑 `build-i18n-page.py` 產英文與日文版（語系檔 `docs/i18n/mfp-fetch.<語言>.json`）。

    python3 build-mfp-guide-page.py

截圖由 `tools/capture_mfp_guide.py` 產生（對著假設備跑真的流程，三種語言各一組）。
**步驟圖用 HTML 排，不用 SVG 文字**：文字要能翻譯、窄螢幕要能換行。
"""
from __future__ import annotations

import html
import re
import struct
from pathlib import Path

HERE = Path(__file__).resolve().parent
LAYOUT = HERE / "docs" / "troubleshooting.html"
DST = HERE / "docs" / "mfp-fetch.html"
SHOTS = "screenshots/mfp-guide"

TITLE = "事務機掃描取件使用說明 | Jason Tools 文件工具箱"
DESC = ("Jason Tools 文件工具箱「事務機掃描取件」的使用說明：管理員設定共用事務機與成員、使用者從本站掃描、"
        "把存在事務機裡的文件取回工作區，以及各廠牌的支援方式。")

#: 圖示（Lucide 線條圖示，同介紹站其他區塊）
_IC = {
    "printer": '<path d="M6 9V2h12v7"/><path d="M6 18H4a2 2 0 0 1-2-2v-5a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2v5a2 2 0 0 1-2 2h-2"/>'
               '<rect x="6" y="14" width="12" height="8"/>',
    "server": '<rect x="2" y="2" width="20" height="8" rx="2"/><rect x="2" y="14" width="20" height="8" rx="2"/>'
              '<path d="M6 6h.01M6 18h.01"/>',
    "folder": '<path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/>',
    "download": '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="m7 10 5 5 5-5"/><path d="M12 15V3"/>',
    "users": '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/>'
             '<path d="M22 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/>',
    "user": '<path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/>',
    "scan": '<path d="M3 7V5a2 2 0 0 1 2-2h2"/><path d="M17 3h2a2 2 0 0 1 2 2v2"/><path d="M21 17v2a2 2 0 0 1-2 2h-2"/>'
            '<path d="M7 21H5a2 2 0 0 1-2-2v-2"/><path d="M7 12h10"/>',
    # Lucide「usb」（MIT）
    "usb": '<circle cx="10" cy="7" r="1"/><circle cx="4" cy="20" r="1"/><path d="M4.7 19.3 19 5"/>'
           '<path d="m21 3-3 1 2 2Z"/><path d="M9.26 7.68 5 12l2 5"/><path d="m10 14 5 2 3.5-3.5"/>'
           '<path d="m18 12 1-1 1 1-1 1Z"/>',
    "mail": '<rect x="2" y="4" width="20" height="16" rx="2"/><path d="m22 7-10 6L2 7"/>',
    "shield": '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/><path d="m9 12 2 2 4-4"/>',
    "lock": '<rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/>',
    "eye": '<path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/>',
    "clock": '<circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/>',
    "log": '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/>'
           '<path d="M16 13H8"/><path d="M16 17H8"/>',
    "wifi": '<path d="M5 12.55a11 11 0 0 1 14.08 0"/><path d="M1.42 9a16 16 0 0 1 21.16 0"/>'
            '<path d="M8.53 16.11a6 6 0 0 1 6.95 0"/><path d="M12 20h.01"/>',
    "repeat": '<path d="m17 2 4 4-4 4"/><path d="M3 11v-1a4 4 0 0 1 4-4h14"/><path d="m7 22-4-4 4-4"/>'
              '<path d="M21 13v1a4 4 0 0 1-4 4H3"/>',
    "check": '<path d="M20 6 9 17l-5-5"/>',
    "arrow": '<path d="M5 12h14"/><path d="m12 5 7 7-7 7"/>',
    "tool": '<path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91'
            'a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/>',
}


def _icon(name: str, size: int = 20, cls: str = "mg-ic") -> str:
    return (f'<svg class="{cls}" viewBox="0 0 24 24" width="{size}" height="{size}" fill="none" stroke="currentColor" '
            f'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">{_IC[name]}</svg>')


def _png_size(path: Path) -> tuple[int, int]:
    with open(path, "rb") as f:
        head = f.read(24)
    return struct.unpack(">II", head[16:24])


def _inline(s: str) -> str:
    """`**粗體**`、`` `程式碼` `` → HTML（其他照原樣跳脫）。"""
    s = html.escape(s, quote=False)
    s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
    return re.sub(r"`([^`]+)`", r"<code>\1</code>", s)


def _shot(name: str, alt: str, cap: str) -> str:
    p = HERE / "docs" / SHOTS / name
    w, h = _png_size(p)
    return (f'<figure class="mg-shot"><a href="{SHOTS}/{name}" target="_blank" rel="noopener">'
            f'<img src="{SHOTS}/{name}" alt="{html.escape(alt)}" width="{w}" height="{h}" loading="lazy"></a>'
            f'<figcaption>{_inline(cap)}</figcaption></figure>')


def _steps(items: list[tuple[str, str, str]]) -> str:
    """編號步驟：(圖示, 標題, 說明)。寬螢幕排成一列、之間有箭頭；窄螢幕直排。"""
    out = ['<ol class="mg-steps">']
    for n, (ic, title, body) in enumerate(items, 1):
        out.append(f'<li class="mg-step"><div class="mg-step-head"><span class="mg-step-n">{n}</span>'
                   f'{_icon(ic, 22, "mg-step-ic")}</div><h4>{_inline(title)}</h4><p>{_inline(body)}</p></li>')
    out.append("</ol>")
    return "\n".join(out)


def _section(sid: str, title: str, lead: str, body: str, alt: bool) -> str:
    return (f'<section id="{sid}" class="cp-sec{" cp-sec-alt" if alt else ""}"><div class="container">'
            f'<h2 class="cp-sec-title">{_inline(title)}</h2><p class="cp-sec-lead">{_inline(lead)}</p>'
            f"{body}</div></section>")


NAV = [("how", "運作方式"), ("admin", "管理員設定"), ("mine", "我的事務機"), ("scan", "掃描"),
       ("fetch", "取件"), ("brands", "各廠牌"), ("safety", "安全"), ("faq", "常見問題")]


def _flow() -> str:
    nodes = [("printer", "事務機", "eSCL 掃描、機內信箱、文件伺服器、檔案分享"),
             ("server", "本站", "在同一個內網，替你連線、取件、轉成 PDF"),
             ("folder", "你的工作區", "每個帳號各自一份，保留時間由管理員設定"),
             ("tool", "下載或接著處理", "OCR、合併、壓縮、翻譯、用印…")]
    out = ['<div class="mg-flow" role="list">']
    for i, (ic, t, d) in enumerate(nodes):
        if i:
            out.append(f'<div class="mg-flow-arrow" aria-hidden="true">{_icon("arrow", 22, "mg-ic")}</div>')
        out.append(f'<div class="mg-flow-node" role="listitem">{_icon(ic, 26, "mg-flow-ic")}'
                   f'<b>{_inline(t)}</b><span>{_inline(d)}</span></div>')
    out.append("</div>")
    roles = [("users", "管理員", ["設定共用事務機，按「成員」決定誰可以用（使用者、群組、組織單位）",
                                "決定要不要開放使用者自行輸入 IP 新增事務機（預設開放；關掉之後大家只用共用的）",
                                "連線測試的逐步診斷與診斷報告，追查連不上的原因"]),
             ("user", "使用者", ["在「事務機掃描取件」頁看到自己新增的與管理員共用的事務機",
                               "管理員有開放時，也可以自己輸入事務機的 IP 新增，只有自己的帳號看得到",
                               "按「掃描」從本站叫事務機掃描，或按「取件」取回存在事務機裡的文件",
                               "取好的檔案在工作區，可以開啟、下載或接著用其他工具"])]
    cards = ['<div class="mg-roles">']
    for ic, t, items in roles:
        lis = "".join(f"<li>{_inline(x)}</li>" for x in items)
        cards.append(f'<div class="mg-role"><div class="mg-role-h">{_icon(ic, 22, "mg-role-ic")}<b>{_inline(t)}</b></div>'
                     f"<ul>{lis}</ul></div>")
    cards.append("</div>")
    return "\n".join(out + cards)


BRANDS = [
    ("FUJIFILM", "機內信箱", "在面板選「掃描至信箱」，存進指定的信箱",
     "信箱編號；信箱有設密碼才填密碼", "已支援：Apeos C5571（同系列 C7071、C6571、C4571、C3571、C3071、C2571）。可以「取件並刪除」", "good"),
    ("RICOH", "文件伺服器", "在面板選「掃描至資料夾」並「儲存檔案」，存進文件伺服器的共用資料夾或設備資料夾",
     "登入方式（不需登入、帳號密碼、User Code）；資料夾有設密碼才填", "已支援：IM C2000、IM C2010、IM C2510、IM C3500、IM C3510；其他型號是實驗功能", "good"),
    ("KYOCERA", "文件匣", "存進自訂資料盒（機型要有硬碟或 SSD）",
     "事務機帳號密碼；文件匣有設密碼才填", "實驗功能。沒有自訂資料盒的機型，遙控資料盒目前只能列出", "exp"),
    ("Canon", "Advanced Box", "存進 Advanced Box 的共用空間或個人空間",
     "事務機帳號密碼", "實驗功能（WebDAV 或 SMB）", "exp"),
    ("Konica Minolta", "公用信箱", "存進公用信箱", "信箱的帳號密碼", "實驗功能（SMB）", "exp"),
    ("SHARP", "NAS 資料夾", "存進事務機的 NAS 資料夾", "事務機帳號密碼", "實驗功能（SMB）", "exp"),
    ("支援 eSCL 的機型", "從本站掃描", "不用先存，直接在本站按「掃描」",
     "不用帳號密碼", "可以掃描（HP、Epson、Brother 等支援 eSCL 的機型）", "scan"),
]


def _brands() -> str:
    head = "".join(f"<th>{_inline(h)}</th>" for h in ("廠牌", "方式", "在事務機上怎麼存", "要填什麼", "支援狀況"))
    rows = []
    for name, way, save, need, status, cls in BRANDS:
        rows.append(f'<tr><th scope="row">{_inline(name)}</th><td>{_inline(way)}</td><td>{_inline(save)}</td>'
                    f'<td>{_inline(need)}</td><td><span class="mg-pill mg-pill-{cls}">{_inline(status)}</span></td></tr>')
    return f'<div class="mg-table-wrap"><table class="mg-table"><thead><tr>{head}</tr></thead><tbody>{"".join(rows)}</tbody></table></div>'


#: 頁首的三個痛點（使用者 2026-10-22：副標題要把痛點寫出來）
PAINS = [
    ("mail", "掃描寄到電子郵件",
     "每台事務機都要設定郵件伺服器與寄件帳號，維護麻煩；大型文件常超過附件或信箱容量上限，寄不出去。"),
    ("folder", "掃描到共用資料夾",
     "共用區通常很多人都有權限，合約、人事資料這類機密文件放進去，誰都能開啟或取走。"),
    ("usb", "用 USB 隨身碟取件",
     "隨身碟容易遺失，也可能帶進惡意程式；不少單位的資安規範已限制使用。"),
]

SAFETY = [
    ("wifi", "只連內網", "事務機的位址只能是內部網路；連線測試只讀首頁與掃描介面的能力表，不送任何帳號密碼。"),
    ("lock", "不儲存帳密", "取件時填的帳號密碼只用在這一次；記住的是信箱編號、信任的憑證這類設定，跟著帳號存，不含密碼。"),
    ("repeat", "登入只送一次", "帳號、信箱或資料夾密碼錯了就停下來，不換方式重試，避免連續輸錯讓事務機鎖住帳號。"),
    ("eye", "只讀不改", "取件只列出、下載，不刪除、不改名；只有 FUJIFILM 可以選「取件並刪除」，而且確認存好才刪。"),
    ("clock", "一次一個人", "同一台事務機一次只服務一個人，其他人自動排隊，不會互相打斷。"),
    ("log", "全程留紀錄", "誰在什麼時候對哪一台掃描、取件、刪了哪幾份，都寫進稽核記錄（不記密碼與文件內容）。"),
]

FAQ = [
    ("看不到管理員共用的事務機",
     "先看頁面上的「目前登入的帳號」：同一個人的 AD、SSO、本機帳號是不同帳號，成員要加的是你實際登入的那一個。"
     "管理員也可以把你所在的群組加進成員；群組旁邊顯示「本站 0 人」代表登入時沒有掛上這個群組。改完切回分頁就會更新。"),
    ("連線測試失敗或列不出檔案",
     "看連線測試的逐步診斷，哪一步失敗就從那一步查；也可以下載診斷報告（txt、md、json、html、pdf）給管理員。"
     "KYOCERA 要在事務機上啟用 9091 或 9090 連接埠的文件服務；FUJIFILM 某個連接埠回 406 時，換另一個連線方式。"),
    ("RICOH 的資料夾要密碼",
     "在「位置」選那個資料夾，輸入資料夾密碼後按「開啟資料夾」。密碼錯了不會自動重試，確認後再輸入一次。"),
    ("清單上標「可重新測試」",
     "辨識規則更新過，這台之前沒認出型號。按一次「重新測試」就會用新的規則判斷。"),
    ("取好的檔案在哪裡",
     "在你的工作區，可以開啟、下載，或在其他工具裡直接選工作區的檔案。保留時間由管理員設定（預設 24 小時）。"),
    ("要回報問題",
     "展開「技術細節」，按「複製協定紀錄」貼給客服。協定紀錄不含帳號、密碼與文件內容。"),
]


def _body() -> str:
    hero = ['<header class="cp-hero"><div class="container">',
            '<span class="cp-eyebrow">使用說明</span>',
            f'<h1 class="cp-title">{_icon("printer", 40, "mg-title-ic")}事務機掃描取件</h1>',
            f'<p class="cp-lead">{_inline("掃描完的文件要送到自己手上，常見的三種做法各有難處：")}</p>',
            '<div class="mg-pains">'
            + "".join(f'<div class="mg-pain">{_icon(ic, 22, "mg-pain-ic")}<b>{_inline(t)}</b><p>{_inline(d)}</p></div>'
                      for ic, t, d in PAINS)
            + "</div>",
            f'<p class="cp-lead mg-answer">{_inline("**事務機掃描取件**：在本站直接叫事務機掃描，或取回存在事務機裡的文件，檔案只存進你自己的工作區，接著做 OCR、合併、壓縮或翻譯。")}</p>',
            '<div class="mg-chips">'
            + "".join(f'<span class="mg-chip">{_icon(ic, 16)}{_inline(t)}</span>' for ic, t in
                      (("wifi", "檔案不經過雲端"), ("lock", "不儲存帳密"), ("clock", "一次服務一個人"), ("shield", "已在實機驗證")))
            + "</div></div></header>"]
    # 置中的膠囊導覽（使用者 2026-10-22：偏左不好看）。資料保護頁共用 cp-pagenav 但右邊有搜尋框，所以只在這頁加 mg-pagenav
    nav = ['<nav class="cp-pagenav mg-pagenav" aria-label="本頁"><div class="container cp-pagenav-in">'
           '<div class="cp-pagenav-links">']
    nav += [f'<a href="#{sid}">{_inline(t)}</a>' for sid, t in NAV]
    nav.append("</div></div></nav>")

    secs = []
    secs.append(_section("how", "運作方式", "事務機、本站、你的工作區在同一個內網裡；檔案從事務機直接進工作區，不經過任何雲端服務。",
                         _flow(), False))
    secs.append(_section("admin", "管理員：設定共用事務機",
                         "在「設定 → 共用事務機」建立一次，指定成員之後，成員在自己的「事務機掃描取件」頁就看得到、用得到。",
                         _steps([("printer", "輸入位址、連線測試", "輸入事務機的 IP 或主機名稱，按「連線測試」。只讀首頁與掃描介面的能力表，不送帳號密碼。"),
                                 ("check", "確認型號並儲存", "看一下廠牌、型號與相容性，按「儲存為共用事務機」。"),
                                 ("users", "設定成員", "按「成員」勾選使用者或群組。群組旁邊的「本站 N 人」是已經算進這個群組的帳號數。"),
                                 ("user", "把自己也加進去", "管理員也要列在成員裡，這台才會出現在自己的「事務機掃描取件」頁（標「你是成員」）。")])
                         + _shot("08-members.png", "共用事務機的成員設定：使用者寫成帳號@來源，群組旁標出本站人數",
                                 "成員面板：使用者寫成 `帳號@來源`，同名的標「同名帳號」；群組旁標出本站人數。")
                         + f'<p class="mg-note">{_inline("想讓大家只用共用的事務機：在同一頁的「使用設定」關掉「允許使用者自行新增事務機」。管理員自己不受影響。")}</p>',
                         True))
    secs.append(_section("mine", "使用者：我的事務機",
                         "清單上有你自己新增的（只有你的帳號看得到）與管理員共用的；相容性、可以做的事都標在同一列。",
                         _shot("01-list.png", "我的事務機清單：來源、位址、型號、相容性與掃描、取件按鈕",
                               "「來源」分「自己新增」與「管理員共用」；相容性「已支援」是已在實機驗證的型號。")
                         + _steps([("printer", "新增自己的事務機", "在「新增事務機」輸入位址，按「連線測試」。"),
                                   ("eye", "看結果", "廠牌、型號、相容性、能不能從本站掃描，以及每一步的診斷。"),
                                   ("download", "儲存", "取個好認的名稱後按「儲存這台事務機」；換一台電腦用同一個帳號登入照樣在。")])
                         + _shot("02-test.png", "連線測試結果：連線、TLS、廠牌、型號、相容性與逐步診斷",
                                 "連線測試的結果卡片與逐步診斷；需要時可以下載診斷報告。"),
                         False))
    secs.append(_section("scan", "從本站掃描",
                         "支援 eSCL 的事務機，清單上有「掃描」：在本站選好設定，事務機就開始掃，掃好的檔案直接進工作區。",
                         _steps([("printer", "放好文件", "把文件放在平台玻璃上，或放進自動送紙器。"),
                                 ("scan", "選設定", "按「掃描」，選來源、色彩、解析度、紙張、存成 PDF 或 JPEG，可以先取檔名。"),
                                 ("clock", "開始掃描", "按「開始掃描」。有人在用時會自動排隊，畫面顯示前面還有幾件。"),
                                 ("folder", "存進工作區", "完成後可以開啟、下載，或到工作區接著處理。")])
                         + '<div class="mg-shots2">'
                         + _shot("03-scan.png", "掃描面板：來源、色彩、解析度、紙張、存成與檔名", "掃描設定：只列出這台事務機支援的選項。")
                         + _shot("04-scan-done.png", "掃描完成：頁數、協定與開啟、下載、前往工作區", "完成：存進工作區，可以開啟或下載。")
                         + "</div>",
                         True))
    secs.append(_section("fetch", "取件：把存在事務機裡的文件取回來",
                         "先在事務機的面板上把文件存進信箱、文件伺服器或資料盒，再到本站按「取件」（FUJIFILM 是「信箱取件」）。",
                         _steps([("mail", "在事務機上存好", "依廠牌在面板上選存到信箱、文件伺服器或資料盒（見下方「各廠牌」）。"),
                                 ("lock", "填登入資訊", "依面板顯示填信箱編號、帳號或 User Code。只送一次，錯了就停下來。"),
                                 ("folder", "列出並選位置", "按「列出檔案」；有資料夾的在「位置」切換，資料夾有密碼時輸入後按「開啟資料夾」。"),
                                 ("check", "勾選並取件", "勾選要的文件，選存成 PDF（建議）後按「取件」，檔案存進工作區。")])
                         + '<div class="mg-shots2">'
                         + _shot("05-box.png", "FUJIFILM 信箱取件：信箱編號、文件清單、取件與取件並刪除",
                                 "FUJIFILM 信箱取件：可以選「取件並刪除」，確認存好之後才刪事務機上的原件。")
                         + _shot("06-share.png", "RICOH 文件伺服器取件：登入方式、位置選資料夾、資料夾密碼與文件清單",
                                 "RICOH 文件伺服器：在「位置」切換共用資料夾與設備資料夾，有密碼的標出來。")
                         + "</div>"
                         + _shot("07-log.png", "技術細節：協定紀錄與複製協定紀錄按鈕",
                                 "出問題時展開「技術細節」，按「複製協定紀錄」貼給客服；紀錄不含帳號、密碼與文件內容。"),
                         False))
    secs.append(_section("brands", "各廠牌的取件方式",
                         "「已支援」是在實機上驗證過取件的型號；「實驗功能」依原廠手冊或原廠工具的分析寫成，還沒在實機上驗證。",
                         _brands(), True))
    cards = ['<div class="mg-cards">']
    for ic, t, d in SAFETY:
        cards.append(f'<div class="mg-card">{_icon(ic, 22, "mg-card-ic")}<b>{_inline(t)}</b><p>{_inline(d)}</p></div>')
    cards.append("</div>")
    secs.append(_section("safety", "安全與隱私", "事務機上常有合約、薪資單這類文件，所以每一步都照最保守的方式做。",
                         "\n".join(cards), False))
    faq = ['<div class="mg-faq">']
    for q, a in FAQ:
        faq.append(f"<details><summary>{_inline(q)}</summary><p>{_inline(a)}</p></details>")
    faq.append(f'<p class="mg-note">{_inline("要用程式呼叫：見")} <a href="api.html#連線測試">{_inline("API 使用手冊")}</a>{_inline("。")}</p>')
    faq.append("</div>")
    secs.append(_section("faq", "常見問題", "還是遇到問題時，先看這幾個。", "\n".join(faq), True))
    return "\n".join(hero + nav + secs)


def build() -> str:
    lay = LAYOUT.read_text(encoding="utf-8")
    head = lay[:lay.index('<main class="container ts-main">')]
    foot = lay[lay.index('<footer class="footer">'):]
    head = re.sub(r'<meta name="description" content="[^"]*">',
                  f'<meta name="description" content="{html.escape(DESC)}">', head, count=1)
    head = re.sub(r"<title>[^<]*</title>", f"<title>{html.escape(TITLE)}</title>", head, count=1)
    for lang in ("", "-en", "-ja"):
        head = head.replace(f'value="troubleshooting{lang}.html"', f'value="mfp-fetch{lang}.html"')
    return head + '<main class="cp-main mg-main">\n' + _body() + "\n</main>\n\n" + foot


if __name__ == "__main__":
    DST.write_text(build(), encoding="utf-8")
    print(f"{DST.name}: 產生完成")
