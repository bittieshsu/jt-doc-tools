"""解析台鐵 / 高鐵乘車（購票）證明 PDF → 結構化 dict。

支援兩種官方憑證：
  - 台灣高鐵「電子車票證明」（THSRC）：label ：value 版面，好抓。
    乘車日期 / 起程站 / 到達站 / 票價 / 銷售額 / 營業稅額 / 卡號票號 / 統一編號。
  - 台鐵「購票證明」（TRA）：表格版面，PyMuPDF 抽出的文字順序會打散，
    因此一律用「特徵正則」直接抓值（不依賴行順序）：
    票號 / 乘車日 / 乘車區間（含起訖時間與站名）/ 車種車次 / 票種 / 票價。

回傳 dict 欄位（缺的給 None / ""）：
  transport      "高鐵" | "台鐵"
  date           ISO "YYYY-MM-DD"
  depart_time    "HH:MM"（發車）
  arrive_time    "HH:MM"（到達；高鐵證明無到達時間 → ""）
  origin         起站（簡化站名）
  destination    到站（簡化站名）
  fare           票價（int）
  amount_untaxed 銷售額（int，高鐵有）
  tax            營業稅額（int，高鐵有）
  train          車種 / 車次（台鐵有）
  ticket_type    票種（台鐵有）
  ticket_no      票號 / 卡號
  buyer_tax_id   統一編號（高鐵有）
"""
from __future__ import annotations

import re
from typing import Optional


class ParseError(Exception):
    pass


def _int(s) -> Optional[int]:
    if s is None:
        return None
    m = re.search(r"-?\d[\d,]*", str(s))
    if not m:
        return None
    try:
        return int(m.group(0).replace(",", ""))
    except ValueError:
        return None


def _simplify_station(name: str) -> str:
    """高鐵站名精簡：去「高鐵」前綴 + 「車站」/「站」後綴 → 只留地名。
    台鐵站名（臺北 / 臺中 …）本就簡潔，原樣返回。"""
    s = (name or "").strip()
    s = re.sub(r"^高鐵", "", s)
    s = re.sub(r"車站$", "", s)
    s = re.sub(r"站$", "", s)
    return s.strip()


def detect_kind(text: str) -> Optional[str]:
    """判斷憑證類型：'thsrc'（高鐵）/ 'tra'（台鐵）/ None。"""
    if "高鐵" in text or "THSRC" in text or "thsrc" in text.lower():
        return "thsrc"
    if "購票證明" in text or "車次" in text or "臺鐵" in text or "台鐵" in text:
        return "tra"
    return None


def _thsrc_field(text: str, label: str) -> str:
    """抓高鐵『label ： value』一行的 value（到行尾）。"""
    m = re.search(rf"{re.escape(label)}\s*[:：]\s*(.*)", text)
    return m.group(1).strip() if m else ""


def parse_thsrc(text: str) -> dict:
    ride = _thsrc_field(text, "乘車日期")   # 2026-06-09 07:20:00
    date, dtime = "", ""
    m = re.search(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})(?:\s+(\d{1,2}):(\d{2}))?", ride)
    if m:
        date = f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
        if m.group(4):
            dtime = f"{int(m.group(4)):02d}:{m.group(5)}"
    origin = _simplify_station(_thsrc_field(text, "起程站"))
    dest = _simplify_station(_thsrc_field(text, "到達站"))
    return {
        "transport": "高鐵",
        "date": date,
        "depart_time": dtime,
        "arrive_time": "",
        "origin": origin,
        "destination": dest,
        "fare": _int(_thsrc_field(text, "票價")),
        "amount_untaxed": _int(_thsrc_field(text, "銷售額")),
        "tax": _int(_thsrc_field(text, "營業稅額")),
        "train": "",
        "ticket_type": "",
        "ticket_no": _thsrc_field(text, "卡號/票號") or _thsrc_field(text, "票號"),
        "buyer_tax_id": _thsrc_field(text, "統一編號"),
    }


def parse_tra(text: str) -> dict:
    # 票號：一個英文字母 + 一長串數字（例：N 開頭 + 14 碼）
    m = re.search(r"\b([A-Z]\d{10,})\b", text)
    ticket_no = m.group(1) if m else ""

    # 乘車日：所有 yyyy/mm/dd 中，排除「印製日期」的那個。
    printed = ""
    mp = re.search(r"印製日期\s*\n?\s*(\d{4}/\d{1,2}/\d{1,2})", text)
    if mp:
        printed = mp.group(1)
    date = ""
    for md in re.finditer(r"(\d{4})/(\d{1,2})/(\d{1,2})", text):
        raw = md.group(0)
        if raw == printed:
            continue
        date = f"{int(md.group(1)):04d}-{int(md.group(2)):02d}-{int(md.group(3)):02d}"
        break
    # 若只有一個日期（沒印製日期標籤），退而取第一個
    if not date and printed:
        md = re.search(r"(\d{4})/(\d{1,2})/(\d{1,2})", printed)
        if md:
            date = f"{int(md.group(1)):04d}-{int(md.group(2)):02d}-{int(md.group(3)):02d}"

    # 乘車區間：18:41 臺北 > 20:50 臺中（> 可能是全形＞）
    origin = dest = dtime = atime = ""
    mr = re.search(
        r"(\d{1,2}:\d{2})\s*([^\s>＞]+)\s*[>＞]\s*(\d{1,2}:\d{2})\s*([^\s\n]+)", text)
    if mr:
        dtime, origin, atime, dest = (mr.group(1), mr.group(2).strip(),
                                      mr.group(3), mr.group(4).strip())

    # 車種 + 車次：自強(3000) … 477 車次
    # (?<!乘車) 排除「乘車區間」label 裡的「區間」被誤當車種（它排在真正車種之前）。
    train = ""
    mt = re.search(r"(?<!乘車)(自強|莒光|復興|普悠瑪|太魯閣|新自強|PP自強|EMU\d*|區間快|區間)"
                   r"\s*(\([^)]*\))?", text)
    car = re.search(r"(\d+)\s*車次", text)
    if mt:
        train = mt.group(1) + (mt.group(2) or "")
    if car:
        train = (train + f" {car.group(1)}車次").strip()

    # 票種：全票 / 孩童 / 敬老 …（可能後面接 (商務) / (自由座)）
    ticket_type = ""
    mk = re.search(r"(全票|孩童|敬老|愛心|優待|團體|軍警)\s*(\([^)]*\))?", text)
    if mk:
        ticket_type = mk.group(1) + (mk.group(2) or "")

    # 票價：950 元
    fare = None
    mf = re.search(r"(\d[\d,]*)\s*元", text)
    if mf:
        fare = _int(mf.group(1))

    return {
        "transport": "台鐵",
        "date": date,
        "depart_time": dtime,
        "arrive_time": atime,
        "origin": origin,
        "destination": dest,
        "fare": fare,
        "amount_untaxed": None,
        "tax": None,
        "train": train,
        "ticket_type": ticket_type,
        "ticket_no": ticket_no,
        "buyer_tax_id": "",
    }


def parse_text(text: str) -> dict:
    """依偵測到的類型解析。抓不到任何有效欄位 → ParseError。"""
    kind = detect_kind(text or "")
    if kind == "thsrc":
        d = parse_thsrc(text)
    elif kind == "tra":
        d = parse_tra(text)
    else:
        raise ParseError("無法辨識為台鐵 / 高鐵乘車證明")
    # 至少要有日期或起訖或票價，否則視為解析失敗
    if not (d.get("date") or d.get("origin") or d.get("fare")):
        raise ParseError("辨識到憑證類型，但抽不到有效欄位（版面可能不同）")
    # 會計科目預設「旅費」（交通票券報帳慣例）；可在表格內編輯調整。
    d.setdefault("subject", "旅費")
    return d
