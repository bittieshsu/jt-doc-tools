"""「從工作區載入」可選的格式要跟上傳框一致（客戶 2026-10-28 回報）。

「圖片轉 PDF」的上傳框收所有圖片（說明寫著 PNG / JPG / GIF / TIFF / WebP / HEIC），從工作區載入卻寫死成
`accept: ['png']`：iPhone 拍的 JPG 存進工作區之後，載入時說「工作區內沒有符合的檔案（PNG）」。
同一個毛病還有三支：文字去識別化、文件翻譯、清單處理的上傳框收 txt / docx / odt…，工作區卻只開放 PDF；
電子發票處理與掃描拼合沒寫死，但按鈕沒帶 `data-ws-exts`，一樣只剩 PDF / PNG。

共用的 `workspaceAcceptExts()`（上傳框的格式 ∩ 伺服器給的工作區清單 `data-ws-exts`）本來就算得對，
是這幾支自己刻的按鈕繞過了它：一個寫死格式、一個沒帶 `data-ws-exts`（沒帶的話退回保守的 {pdf, png}）。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = sorted((ROOT / "app").rglob("*.html"))
CALL = re.compile(r"attachWorkspaceLoadButton\(([^;]*?)\)\s*;", re.S)


def test_no_tool_hardcodes_the_picker_formats():
    bad = []
    for p in TEMPLATES:
        s = p.read_text(encoding="utf-8")
        for m in CALL.finditer(s):
            if re.search(r"\baccept\s*:", m.group(1)):
                bad.append(f"{p.relative_to(ROOT)}: {m.group(0)[:120]}")
    assert not bad, "從工作區載入不可以自己寫死格式，交給上傳框的 accept 與工作區清單算交集：\n" + "\n".join(bad)


def test_every_hand_made_load_button_carries_the_server_list():
    bad = []
    for p in TEMPLATES:
        s = p.read_text(encoding="utf-8")
        for m in CALL.finditer(s):
            first = m.group(1).split(",", 1)[0]
            mid = re.search(r"getElementById\('([^']+)'\)|\$\('([^']+)'\)", first)
            if not mid:
                continue
            bid = mid.group(1) or mid.group(2)
            tag = re.search(r'<button\b[^>]*\bid="' + re.escape(bid) + r'"[^>]*>', s)
            if tag and "data-ws-exts" not in tag.group(0):
                bad.append(f"{p.relative_to(ROOT)}: #{bid}")
    assert not bad, "按鈕沒帶 data-ws-exts 時會退回 {pdf, png}：\n" + "\n".join(bad)


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
@pytest.mark.parametrize("tool, must_have", [
    ("image_to_pdf", {"png", "jpg", "tif"}),
    ("text_deident", {"pdf", "txt", "md", "docx", "odt"}),
    ("translate_doc", {"pdf", "docx", "odt", "txt", "md"}),
    ("text_list", {"pdf", "txt", "md", "json", "xlsx", "docx"}),
    ("transit_proof", {"pdf"}),
    # 這兩支沒寫死格式，但按鈕沒帶 data-ws-exts → 退回 {pdf, png}，手機拍的 JPG 一樣選不到
    ("einvoice_scan", {"png", "jpg"}),
    ("scan_merge", {"pdf", "png", "jpg", "tif"}),
])
def test_each_tool_offers_what_its_upload_box_takes(tool, must_have):
    from app.core import workspace
    ws = " ".join(sorted({e.lstrip(".") for e in workspace.ALLOWED.values()}))
    html = next((ROOT / "app" / "tools" / tool / "templates").glob("*.html")).read_text(encoding="utf-8")
    accept = re.search(r'<input\b[^>]*type="file"[^>]*accept="([^"]*)"', html, re.S) \
        or re.search(r'<input\b[^>]*accept="([^"]*)"[^>]*type="file"', html, re.S)
    assert accept, tool
    js = (ROOT / "static" / "js" / "workspace_picker.js").read_text(encoding="utf-8")
    prog = ("global.window = global; global.document = {querySelector: () => null, addEventListener: () => {}};\n"
            + js + f"\nconsole.log(JSON.stringify(window.workspaceAcceptExts({accept.group(1)!r}, {ws!r})));")
    out = subprocess.run(["node", "-e", prog], capture_output=True, text=True, timeout=20)
    got = set(json.loads(out.stdout.strip().splitlines()[-1]))
    assert must_have <= got, f"{tool}: 上傳框 {accept.group(1)} → 工作區只開放 {sorted(got)}"
