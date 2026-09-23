#!/usr/bin/env python3
"""替截圖用的拋棄式實例塞一份**全部是虛構的**示範資料。

介紹站的截圖要看起來像「真的在用」，但**一個字都不可以是真的**：
公司名、統編、地址、電話、銀行帳號、人名一律杜撰
（CLAUDE.md：個人 / 客戶資料一個字都不可以進 git，而截圖是要公開的）。

塞三樣：
  1. 一間示範公司（表單自動填寫要有公司資料才填得出東西）
  2. 一顆示範印章（印章 / 騎縫章的頁面沒有資產時只會顯示「還沒有共用資產」）
  3. 一份示範的廠商資料表 PDF（`temp/demo/vendor-form.pdf`）與一份報價單

用法（`JTDT_DATA_DIR` 要先指到那個拋棄式的資料目錄）：
    JTDT_DATA_DIR=/tmp/xxx python tools/seed_demo_data.py
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from app.core.profile_manager import (  # noqa: E402  （要先設好 sys.path）
    DEFAULT_FIELDS as _SHIPPED_FIELDS,
)

_SHIPPED_LABELS = {k: lab for k, lab, _v in _SHIPPED_FIELDS}

#: **全部杜撰。** 統編用不會通過檢查碼的號碼、電話用 02-1234-5678 這種
#: 明顯是範例的號碼、地址用不存在的門牌 —— 看得出是範例，也不會撞到真人。
DEMO_COMPANY = {
    "company_name": "範例科技股份有限公司",
    "short_name": "範例科技",
    "english_name": "Example Technology Co., Ltd.",
    "tax_id": "12345675",
    "founded_date": "105/03/18",
    "capital": "5,000,000",
    "owner": "王小明",
    "owner_title_zh": "董事長",
    "address": "臺北市中正區範例路 100 號 5 樓",
    "invoice_address": "臺北市中正區範例路 100 號 5 樓",
    "zip_code": "100",
    "phone": "02-1234-5678",
    "fax": "02-1234-5679",
    "mobile": "0912-345-678",
    "email": "service@example.com.tw",
    "company_email": "service@example.com.tw",
    "company_website": "www.example.com.tw",
    "contact": "陳小華",
    "bank_name": "範例商業銀行",
    "bank_code": "999",
    "bank_branch": "中正分行",
    "bank_branch_code": "0012",
    "bank_account_name": "範例科技股份有限公司",
    "bank_account_no": "999-12-345678-9",
    "payment_method": "匯款",
    "payment_terms": "月結 30 天",
    "vat_status": "一般稅額",
    "invoice_title": "範例科技股份有限公司",
}

#: 欄位標題（畫面上顯示用）。**一定要從出貨的那份取**，不可以在這裡自己再寫
#: 一份 —— 同一份清單放兩個地方一定會漂：2026-09-16 實際漂了 6 個欄位，其中
#: 兩個還漂成大陸用語「郵箱」（台灣要寫「信箱」），而示範資料正是要拿去拍
#: 截圖公開的。檢查 `tests/test_demo_labels_come_from_the_shipped_defaults.py`。
LABELS = {k: _SHIPPED_LABELS[k] for k in DEMO_COMPANY}

#: 廠商資料表要填的欄位（標籤 → 右邊留白給工具填）。
FORM_ROWS = [
    ("廠商名稱", ""), ("統一編號", ""), ("負責人", ""), ("公司電話", ""),
    ("傳真", ""), ("登記地址", ""), ("公司網址", ""), ("成立日期", ""),
    ("資本額", ""), ("聯絡人", ""), ("行動電話", ""), ("Email", ""),
    ("收款銀行", ""), ("分行名稱", ""), ("銀行代碼", ""), ("帳號", ""),
    ("付款方式", ""), ("付款條件", ""),
]


def _cjk_font() -> tuple[str, int]:
    """(字型檔, .ttc 子字型索引)。

    **一定要真的挑得到** —— 缺字型時中文會畫成方框，那樣的截圖比沒有還糟
    （合成樣本「字集要列全」是同一條教訓）。
    """
    from app.core.font_catalog import best_cjk_path
    got = best_cjk_path("sans", "traditional")
    if not got:
        raise SystemExit("找不到中日韓字型 —— 合成表單的字會變方框，不能這樣出截圖")
    path, idx = got
    return str(path), idx


def make_vendor_form(dst: Path) -> Path:
    """做一份**空白的**廠商資料表，給表單自動填寫當素材。

    自己合成而不是拿 `temp_pdfs/` 的真實樣本 —— 那些是客戶資料，
    而截圖是要公開的。
    """
    import fitz

    path, idx = _cjk_font()
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)      # A4
    buf = _subset_buffer(path, idx)
    page.insert_font(fontname="cjk", fontbuffer=buf)
    page.insert_text((180, 70), "供應商基本資料表", fontname="cjk", fontsize=20)
    page.insert_text((60, 100), "（範例表單，內容全為虛構）",
                     fontname="cjk", fontsize=9, color=(0.45, 0.45, 0.45))

    y = 125
    for label, _ in FORM_ROWS:
        page.draw_rect(fitz.Rect(60, y, 180, y + 32), color=(0.2, 0.2, 0.2), width=0.8)
        page.draw_rect(fitz.Rect(180, y, 535, y + 32), color=(0.2, 0.2, 0.2), width=0.8)
        page.insert_text((70, y + 21), label, fontname="cjk", fontsize=11)
        y += 32
    dst.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(dst))
    doc.close()
    return dst


def _subset_buffer(path: str, idx: int) -> bytes:
    """把 .ttc 的子字型抽出來 —— PyMuPDF 沒有 ttc 索引參數。"""
    from fontTools.ttLib import TTFont, TTCollection
    import io

    if path.lower().endswith(".ttc"):
        coll = TTCollection(path, lazy=True)
        font = coll.fonts[idx]
    else:
        font = TTFont(path, lazy=True)
    bio = io.BytesIO()
    font.save(bio)
    return bio.getvalue()


def make_stamp_png() -> bytes:
    """畫一顆示範用的紅色方形印（不是任何真實單位的印）。"""
    import io
    from PIL import Image, ImageDraw, ImageFont

    path, idx = _cjk_font()
    size = 420
    im = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    red = (200, 30, 30, 255)
    d.rounded_rectangle([8, 8, size - 8, size - 8], radius=18, outline=red, width=14)
    f = ImageFont.truetype(path, 150, index=idx)
    for i, ch in enumerate("範例"):
        d.text((52 + (i % 2) * 160, 40), ch, font=f, fill=red)
    for i, ch in enumerate("之印"):
        d.text((52 + (i % 2) * 160, 215), ch, font=f, fill=red)
    out = io.BytesIO()
    im.save(out, "PNG")
    return out.getvalue()



#: 報價單（中文 / 英文各一份）。內容全部虛構。
QUOTE_ZH = [
    ("項次", "品項", "數量", "單價", "金額"),
    ("1", "年度維護服務（到府）", "12", "1,500", "18,000"),
    ("2", "遠端支援時數", "40", "120", "4,800"),
    ("3", "備品包", "2", "3,250", "6,500"),
]
QUOTE_EN = [
    ("No.", "Description", "Qty", "Unit", "Amount"),
    ("1", "Annual maintenance, on-site", "12", "1,500", "18,000"),
    ("2", "Remote support hours", "40", "120", "4,800"),
    ("3", "Spare parts kit", "2", "3,250", "6,500"),
]

#: 去識別化用的素材：**虛構的**個資，讓偵測真的抓得到東西。
#: 號碼一律用規定不指派 / 不會通過檢查碼的那種（同 `fake_values` 的原則）。
DEIDENT_LINES = [
    "客戶資料表（範例，內容全為虛構）",
    "",
    "姓名：王小明",
    "身分證字號：A123456789",
    "出生日期：1985-01-05",
    "行動電話：0912-345-678",
    "電子郵件：ming.wang@example.com.tw",
    "通訊地址：臺北市中正區範例路 100 號 5 樓",
    "統一編號：12345675",
    "銀行帳號：999-12-345678-9",
    "信用卡：4111 1111 1111 1111",
]


def _page_with_text(doc, lines, *, title_size=18, body_size=11, font="cjk"):
    import fitz
    page = doc.new_page(width=595, height=842)
    return page


def make_quotation(dst: Path, rows, title: str, header: list[str]) -> Path:
    import fitz

    path, idx = _cjk_font()
    buf = _subset_buffer(path, idx)
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_font(fontname="cjk", fontbuffer=buf)
    for i, line in enumerate(header):
        page.insert_text((60, 70 + i * 16), line, fontname="cjk",
                         fontsize=10, color=(0.35, 0.35, 0.35))
    page.insert_text((60, 130), title, fontname="cjk", fontsize=22)
    xs = [60, 110, 330, 390, 460, 535]
    y = 170
    for r, row in enumerate(rows):
        page.draw_line(fitz.Point(60, y - 14), fitz.Point(535, y - 14),
                       color=(0.6, 0.6, 0.6), width=0.7)
        for c, cell in enumerate(row):
            page.insert_text((xs[c] + 4, y), cell, fontname="cjk",
                             fontsize=11 if r else 10.5)
        y += 30
    page.draw_line(fitz.Point(60, y - 14), fitz.Point(535, y - 14),
                   color=(0.6, 0.6, 0.6), width=0.7)
    total = sum(int(r[4].replace(",", "")) for r in rows[1:])
    page.insert_text((390, y + 14), f"{rows[0][4]}", fontname="cjk", fontsize=11)
    page.insert_text((460, y + 14), f"{total:,}", fontname="cjk", fontsize=13)
    dst.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(dst))
    doc.close()
    return dst


#: 英文版的去識別化素材。英文介面用的是英美那一組式子（SSN / 電話 / 地址），
#: 塞中文個資進去偵測不到 —— 素材要跟著規則走。號碼全部用**規定不指派**的號段。
DEIDENT_LINES_EN = [
    "Employee record (sample — every value here is made up)",
    "",
    "Name: John Doe",
    "SSN: 900-12-3456",
    "Date of birth: January 5, 1985",
    "Phone: +1 (555) 010-4477",
    "Email: john.doe@example.com",
    "Address: 1842 Maple Street, Springfield, IL 62704",
    "Card: 4111 1111 1111 1111",
    "IBAN: GB00 EXMP 6016 1331 9268 19",
]


def make_deident_doc(dst: Path, lines=None) -> Path:
    import fitz

    path, idx = _cjk_font()
    buf = _subset_buffer(path, idx)
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_font(fontname="cjk", fontbuffer=buf)
    y = 90
    for i, line in enumerate(lines or DEIDENT_LINES):
        page.insert_text((70, y), line, fontname="cjk",
                         fontsize=18 if i == 0 else 12)
        y += 34 if i == 0 else 26
    dst.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(dst))
    doc.close()
    return dst


def make_scan(dst: Path, src: Path) -> Path:
    """把一份 PDF 轉成「掃描件」（整頁是圖、沒有文字層）給 OCR 當素材。"""
    import fitz

    src_doc = fitz.open(str(src))
    out = fitz.open()
    for page in src_doc:
        pix = page.get_pixmap(dpi=150)
        p = out.new_page(width=page.rect.width, height=page.rect.height)
        p.insert_image(p.rect, pixmap=pix)
    dst.parent.mkdir(parents=True, exist_ok=True)
    out.save(str(dst))
    out.close(); src_doc.close()
    return dst


#: 示範用的帳號與群組。**全部虛構** —— 介紹站的「使用者管理」「權限矩陣」
#: 兩張截圖原本是空狀態（「還沒有任何使用者或群組」、使用者 0 人），
#: 當產品截圖等於什麼都沒說（使用者 2026-09-15）。
#: 名字用「王小明 / 陳小華」這種一望即知是範例的，不要用真人。
DEMO_USERS = [
    ("chen.mh", "陳美華", "finance"),
    ("lin.cw", "林志偉", "sales"),
    ("wang.ym", "王怡君", "clerk"),
    ("chang.ky", "張家瑜", "legal-sec"),
    ("huang.ts", "黃大生", "default-user"),
]
DEMO_GROUPS = [
    ("財務部", "發票、請款、廠商資料", ["finance"]),
    ("業務部", "報價單、合約、客戶資料", ["sales"]),
    ("法務資安", "個資去識別化、隱藏內容掃描", ["legal-sec"]),
]


def seed_users_and_groups() -> tuple[int, int]:
    """建立示範帳號與群組（**可以重複跑**：已經有的就跳過）。"""
    from app.core import auth_db, group_manager, permissions, roles, user_manager

    auth_db.init()
    # **內建角色要先種**：正式啟動時是 `app/main.py` 做的，這支獨立腳本
    # 自己跑的話角色表是空的 → 指派角色會撞外鍵（實測 IntegrityError）。
    roles.seed_builtin_roles()
    n_u = 0
    for username, display, role in DEMO_USERS:
        if user_manager.get_by_username(username):
            continue
        # 密碼是隨機的，沒有人會用這些帳號登入 —— 截圖只需要列表上有東西。
        import secrets
        uid = user_manager.create_local(username, display, secrets.token_urlsafe(24),
                                        roles=[role])
        permissions.set_subject_roles("user", str(uid), [role])
        n_u += 1

    # **建了使用者就要明寫「認證關閉」**：`auth_settings.json` 不存在時，
    # 產品的 fail-secure 會看到「資料庫裡有使用者」而自動改用本機認證
    # （v1.15.34 那條刻意的防護）—— 於是這個拋棄式實例整站要登入，
    # 截圖全部變成登入頁。這裡是明確宣告「示範站不啟用認證」，不是繞過防護。
    from app.core import auth_settings
    s = auth_settings.get()
    s["backend"] = "off"
    auth_settings.save(s)

    # **同名帳號可以在不同認證來源並存** —— 那正是 `users-multi-realm.png`
    # 要展示的東西（`chen.mh@local` ＋ `chen.mh@ldap`，各自獨立的角色）。
    # 這一列平常是 LDAP 登入時 JIT 建出來的鏡射列，示範站直接插一筆。
    from app.core import db as _db
    import time as _t
    with auth_db.conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM users WHERE username=? AND source='ldap'",
            ("chen.mh",)).fetchone()
        if not row:
            with _db.tx(conn):
                cur = conn.execute(
                    "INSERT INTO users(username, display_name, source, external_dn, "
                    "enabled, is_admin_seed, created_at, last_login_at, email, "
                    "directory_seen_at) VALUES (?, ?, 'ldap', ?, 1, 0, ?, ?, ?, ?)",
                    ("chen.mh", "陳美華（目錄）",
                     "CN=chen.mh,OU=Finance,DC=example,DC=com,DC=tw",
                     _t.time(), _t.time(), "chen.mh@example.com.tw", _t.time()))
                ldap_uid = cur.lastrowid
            permissions.set_subject_roles("user", str(ldap_uid), ["clerk"])
            n_u += 1

    existing = {g["name"] for g in group_manager.list_groups()}
    n_g = 0
    for name, desc, roles in DEMO_GROUPS:
        if name in existing:
            continue
        gid = group_manager.create_local(name, desc)
        permissions.set_subject_roles("group", str(gid), roles)
        n_g += 1
    return n_u, n_g


#: 示範用的會議逐字稿。**內容全部虛構** —— 截圖是要公開的。
#:
#: 用 `.vtt` 是因為它**自己帶時間**：有時間才畫得出發言佔比與章節時間軸，
#: 而那兩張圖正是這支工具最值得看的地方（純文字版只會顯示「沒有時間戳記」）。
#: 每位發言者都講好幾次 —— 只講一次的名字會被剖析器判成句子不是發言者。
MEETING_VTT = """WEBVTT

1
00:00:01.000 --> 00:00:09.500
王小明：那我們開始。今天三件事：測試機的網路、防火牆告警、還有儲存的方案。

2
00:00:10.000 --> 00:00:21.000
李美華：第一件我先講。測試機的 NAT 還沒開通，所以 7993 跟 7143 這兩個埠測不到。

3
00:00:21.500 --> 00:00:28.000
王小明：那就先開通。網管那邊我來發單，這禮拜五以前會好。

4
00:00:28.500 --> 00:00:40.000
陳大維：防火牆那邊比較麻煩。上禮拜維護的時候關了十二個小時，後來沒有再打開。

5
00:00:40.500 --> 00:00:52.000
陳大維：告警系統一直顯示 inactive，我以為是誤報，查了才發現是真的沒開。

6
00:00:52.500 --> 00:01:02.000
李美華：那我們要不要把它跟通知系統解開？每次維護都要記得手動開，遲早再出一次。

7
00:01:02.500 --> 00:01:12.000
王小明：解開。之後防火牆的開關改成手動，維護完由值班的人確認一次。

8
00:01:12.500 --> 00:01:24.000
陳大維：第三件是儲存。現在的環境還撐得住，明年如果效能不夠再提採購。

9
00:01:24.500 --> 00:01:33.000
李美華：那 RAID 卡的部分呢？上次說要換的那張，現在還是用軟體的。

10
00:01:33.500 --> 00:01:44.000
王小明：先用現有的環境測，不行再說。備份有做好，硬碟壞了立刻換就行。

11
00:01:44.500 --> 00:01:52.000
陳大維：了解。那我把測試結果整理一份，下禮拜會議前寄給大家。
"""


def make_meeting_transcript(dst: Path) -> Path:
    """寫出示範逐字稿（給「會議摘要」的截圖用）。"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(MEETING_VTT, encoding="utf-8")
    return dst


def main() -> int:
    import os
    if not os.environ.get("JTDT_DATA_DIR"):
        raise SystemExit("要先設 JTDT_DATA_DIR（不可以動到開發樹的 data/）")

    from app.core.profile_manager import profile_manager
    from app.core.asset_manager import asset_manager

    cid = profile_manager.active_id()
    profile_manager.save(cid, DEMO_COMPANY["company_name"], DEMO_COMPANY, LABELS)
    print(f"公司資料：{DEMO_COMPANY['company_name']}（{len(DEMO_COMPANY)} 個欄位）")

    # **要可以重複跑**：多跑幾次就多幾顆一模一樣的章，畫面上排成一排很蠢
    # （實測跑了五次就有五顆）。同名的先清掉再建。
    for old_a in asset_manager.list():
        if old_a.name == "範例之印":
            asset_manager.delete(old_a.id)
    a = asset_manager.create_from_bytes("範例之印", "stamp", make_stamp_png())
    asset_manager.update(a.id, is_default=True)
    print(f"印章資產：{a.name} ({a.id})")

    # **LLM 只是「打開」不是真的接**：逐句翻譯 / 文件翻譯的頁面在沒有設定
    # LLM 時整頁被擋住，只看得到一張「本工具需要 LLM 服務」的卡片 ——
    # 那不是產品截圖。打開之後頁面就會正常渲染，而抽句子是本機做的
    # （PyMuPDF），不必真的呼叫模型（截圖也不會按下去翻譯）。
    from app.core.llm_settings import llm_settings
    s = llm_settings.update({"enabled": True})
    print(f"LLM：已啟用（截圖用；model={s.get('model')}，不會真的呼叫）")

    demo = REPO / "temp" / "demo"
    form = make_vendor_form(demo / "vendor-form.pdf")
    print(f"示範表單：{form}")

    zh = make_quotation(demo / "quotation.zh-Hant.pdf", QUOTE_ZH, "報價單",
                        ["範例科技股份有限公司", "臺北市中正區範例路 100 號 5 樓",
                         "報價單號 Q-2026-0417"])
    en = make_quotation(demo / "quotation.pdf", QUOTE_EN, "QUOTATION",
                        ["Example Technology Co., Ltd.",
                         "100 Example Rd, Taipei", "No. Q-2026-0417"])
    print(f"報價單：{zh.name} / {en.name}")
    print(f"去識別化素材：{make_deident_doc(demo / 'deident.zh-Hant.pdf').name}"
          f" / {make_deident_doc(demo / 'deident.pdf', DEIDENT_LINES_EN).name}")
    print(f"掃描件：{make_scan(demo / 'scan.pdf', en).name}")
    print(f"會議逐字稿：{make_meeting_transcript(demo / 'meeting.vtt').name}")

    n_u, n_g = seed_users_and_groups()
    print(f"示範帳號 / 群組：新增 {n_u} 位使用者、{n_g} 個群組（全部虛構）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
