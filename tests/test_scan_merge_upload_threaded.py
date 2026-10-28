"""掃描拼合上傳：處理丟到執行緒、各頁同時處理，結果不變。

2026-10-28 客戶回報「從工作區載入」一份 PDF 要等 20～40 秒、以為當機。
原因是上傳端點在 async 函式裡直接做畫頁面、偵測、存 PNG（還開 `optimize=True`），
一份多頁 PDF 處理的那段時間**整台服務的事件迴圈被佔住**，別人的頁面也一起卡。

這裡驗三件事：
1. 處理的時候，同一台服務的其他請求照樣馬上回（判準是延遲，不是「有沒有呼叫 to_thread」）；
2. 各頁同時處理之後，來源頁與內容區塊的順序仍照頁次；
3. 單次上限 30 塊照舊。
"""
from __future__ import annotations

import asyncio
import importlib
import io
import time

import fitz
import httpx
from PIL import Image

import app.main as app_main

R = importlib.import_module("app.tools.scan_merge.router")


def _pdf(boxes_per_page: list[list[tuple[int, int, int, int]]]) -> bytes:
    """每頁白底，畫幾塊彩色方塊（座標以 A4 點為單位）。"""
    doc = fitz.open()
    for boxes in boxes_per_page:
        page = doc.new_page(width=595, height=842)
        for (x0, y0, x1, y1) in boxes:
            page.draw_rect(fitz.Rect(x0, y0, x1, y1), color=None, fill=(0.2, 0.4, 0.8))
    data = doc.tobytes()
    doc.close()
    return data


def test_other_requests_are_not_blocked_while_a_scan_is_processed(auth_off, monkeypatch):
    real = R.detect_regions

    def slow(img):
        time.sleep(1.5)          # 模擬一頁大掃描的偵測時間（同步、吃 CPU 的那種）
        return real(img)

    monkeypatch.setattr(R, "detect_regions", slow)
    pdf = _pdf([[(60, 60, 260, 200)], [(300, 500, 520, 700)]])

    async def main():
        transport = httpx.ASGITransport(app=app_main.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            t_up = time.perf_counter()
            up = asyncio.create_task(c.post(
                "/tools/scan-merge/upload",
                files={"file": ("images.pdf", pdf, "application/pdf")}))
            await asyncio.sleep(0.4)
            hz = await c.get("/healthz")
            # **從「預定送出的時間」起算**：事件迴圈被卡住時，連上面那個 sleep 都要等
            # 處理完才醒得來 —— 從醒來那一刻算的話延遲看起來很短，測試永遠綠
            # （第一版就是這樣，變異驗證才抓到）。
            latency = time.perf_counter() - (t_up + 0.4)
            res = await up
            return hz.status_code, latency, res, time.perf_counter() - t_up

    hz_status, latency, res, total = asyncio.run(main())
    assert res.status_code == 200, res.text[:300]
    assert hz_status == 200
    # 上傳本身至少 1.5 秒；健康檢查要在它處理的期間就回來
    assert total >= 1.5
    assert latency < 0.8, f"處理掃描檔時其他請求被卡住 {latency:.2f} 秒"


def test_pages_and_regions_keep_page_order(client, auth_off):
    pdf = _pdf([
        [(60, 60, 260, 200)],
        [(320, 80, 540, 220), (60, 600, 260, 760)],
        [(300, 400, 520, 560)],
    ])
    r = client.post("/tools/scan-merge/upload",
                    files={"file": ("images.pdf", pdf, "application/pdf")})
    assert r.status_code == 200, r.text[:300]
    body = r.json()
    assert [s["page"] for s in body["sources"]] == [1, 2, 3]
    pages = [c["page"] for c in body["crops"]]
    assert pages == sorted(pages) and set(pages) == {1, 2, 3}
    assert len(body["crops"]) == 4
    # 第 2 頁兩塊：位置跟畫上去的一致（右上、左下）
    p2 = [c for c in body["crops"] if c["page"] == 2]
    assert {round(c["fx"], 1) for c in p2} == {round(320 / 595, 1), round(60 / 595, 1)}
    # 每塊都真的存了圖、取得到
    for c in body["crops"]:
        img = client.get(c["url_raw"])
        assert img.status_code == 200
        assert Image.open(io.BytesIO(img.content)).size == (c["natural_w"], c["natural_h"])


def test_crop_cap_still_applies_across_pages(client, auth_off, monkeypatch):
    monkeypatch.setattr(R, "_MAX_CROPS_PER_UPLOAD", 3)
    pdf = _pdf([[(60, 60, 200, 160), (300, 500, 480, 640)]] * 3)
    r = client.post("/tools/scan-merge/upload",
                    files={"file": ("images.pdf", pdf, "application/pdf")})
    assert r.status_code == 200, r.text[:300]
    body = r.json()
    assert len(body["sources"]) == 3
    assert [c["page"] for c in body["crops"]] == [1, 1, 2]


def test_no_png_optimize_in_scan_merge():
    """`optimize=True` 讓一張預覽圖從 0.3 秒變 1 秒（掃描帶雜訊），一份 8 頁就多等十幾秒。"""
    import inspect
    src = inspect.getsource(R)
    code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith(("#", '"""')))
    assert "optimize=True" not in code.replace("`optimize=True`", "")
