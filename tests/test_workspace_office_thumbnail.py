"""工作區的 Office / ODF 檔要有第一頁縮圖。

## 由來

使用者回報：「我的工作區 為何有些檔案類型沒有縮圖？好像非 PDF 都沒有？」——
接著指定「第一頁縮圖」。

原本的程式碼直接放棄：

    if ext in (".docx", ".odt", ".xlsx", …):
        # Office documents have no cheap first-page render (would need soffice)
        raise WorkspaceError("此格式無縮圖預覽")

理由沒錯（這些格式確實沒有便宜的畫第一頁方法），但結論不對 —— 畫面上就是一片
空白，而使用者存進工作區的東西**大部分**就是轉出來的 Office 檔。

## 做法與代價

先用既有的 Office 引擎轉成 PDF，再畫第一頁，然後**快取**。轉一次要幾秒，所以：

* 結果存在該檔案自己的目錄裡，跟著檔案一起被清掉。
* 失敗會留一個記號，下次直接跳過 —— 沒有這個的話，每次開工作區頁面都會對同一個
  壞檔重跑一次 soffice。
* 太大的檔案不做（轉一份幾十 MB 的簡報只為了畫一張小圖，會把 Office 引擎的名額
  佔住，真正在等轉檔的人就得排隊）。
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from app.core import office_convert, workspace as ws


@pytest.fixture(scope="module")
def small_odt(tmp_path_factory) -> Path:
    """用既有引擎產一份小的 .odt 當素材（測試庫裡不放二進位檔）。"""
    import fitz
    d = tmp_path_factory.mktemp("src")
    pdf = d / "src.pdf"
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 100), "Workspace thumbnail", fontsize=22)
    doc.save(str(pdf))
    doc.close()
    odt = d / "src.odt"
    try:
        office_convert.convert_to_odt(pdf, odt)
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"這台機器沒有可用的 Office 引擎：{e.__class__.__name__}")
    if not odt.exists():
        pytest.skip("Office 引擎沒有產出檔案")
    return odt


def _entry(tmp_path: Path, src: Path, ext: str) -> Path:
    d = tmp_path / "entry"
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True)
    (d / f"file{ext}").write_bytes(src.read_bytes())
    return d


def test_office_file_gets_a_first_page_thumbnail(tmp_path, small_odt):
    d = _entry(tmp_path, small_odt, ".odt")
    thumb, mime = ws._office_thumbnail(d, ".odt", blocking=True)
    assert mime == "image/png"
    assert thumb.exists() and thumb.stat().st_size > 1000
    from PIL import Image
    with Image.open(thumb) as im:
        assert im.width > 100 and im.height > 100
        # 第一頁是直式 A4 —— 高度應該大於寬度（確認畫的是頁面不是別的東西）
        assert im.height > im.width


def test_thumbnail_is_cached(tmp_path, small_odt):
    """第二次要直接讀檔，不可以再跑一次 Office 引擎。"""
    d = _entry(tmp_path, small_odt, ".odt")
    ws._office_thumbnail(d, ".odt", blocking=True)
    called = []
    orig = office_convert.convert_to_pdf
    try:
        office_convert.convert_to_pdf = lambda *a, **k: called.append(1)
        ws._office_thumbnail(d, ".odt")
    finally:
        office_convert.convert_to_pdf = orig
    assert not called, "第二次又去轉檔了（沒有用到快取）"


def test_oversized_file_is_skipped(tmp_path, monkeypatch, small_odt):
    """大檔不做縮圖 —— 轉檔會佔住 Office 引擎的名額。"""
    d = _entry(tmp_path, small_odt, ".odt")
    monkeypatch.setattr(ws, "_THUMB_MAX_BYTES", 10)
    with pytest.raises(ws.WorkspaceError):
        ws._office_thumbnail(d, ".odt", blocking=True)


def test_failure_is_remembered(tmp_path, monkeypatch):
    """轉檔失敗一次之後要記住，不可以每次開頁面都重跑一次 soffice。

    這裡用「讓轉檔丟例外」來模擬失敗，而不是丟一個壞檔進去 ——
    soffice 其實會把一份亂碼當成純文字乖乖轉成 PDF（試過了），所以壞檔測不到
    這條路徑。
    """
    d = tmp_path / "bad"
    d.mkdir()
    (d / "file.docx").write_bytes(b"whatever")

    def boom(*a, **k):
        raise RuntimeError("soffice 掛了")

    monkeypatch.setattr(office_convert, "convert_to_pdf", boom)
    with pytest.raises(ws.WorkspaceError):
        ws._office_thumbnail(d, ".docx", blocking=True)
    assert (d / ws._THUMB_FAIL_MARK).exists()

    called = []
    monkeypatch.setattr(office_convert, "convert_to_pdf",
                        lambda *a, **k: called.append(1))
    with pytest.raises(ws.WorkspaceError):
        ws._office_thumbnail(d, ".docx", blocking=True)
    assert not called, "失敗過的檔案又被拿去轉了一次"


def test_missing_file_raises_not_found(tmp_path):
    d = tmp_path / "empty"
    d.mkdir()
    with pytest.raises(ws.NotFound):
        ws._office_thumbnail(d, ".odt", blocking=True)


def test_all_workspace_office_types_are_covered():
    """工作區收得下的每一種 Office / ODF 格式都要能走縮圖路徑。

    漏一種的症狀就是「這個格式沒有縮圖」—— 正是使用者回報的那件事。
    """
    # ALLOWED 是 {mime: 副檔名} —— 要比對的是**副檔名**
    office = {ext for ext in ws.ALLOWED.values()
              if ext not in (".pdf", ".png")}
    assert office, "抓不到工作區允許的格式，掃描邏輯可能改壞了"
    missing = office - set(ws._OFFICE_THUMB_EXTS)
    assert not missing, f"這些格式沒有縮圖：{sorted(missing)}"


def test_request_path_does_not_wait_for_conversion(tmp_path, small_odt,
                                                   monkeypatch):
    """HTTP 請求裡**不可以**等轉檔。

    一頁 17 個 Office 檔就是 17 個縮圖請求，而 Office 引擎同時只跑得了少數幾個
    —— 同步做的話最後一個要等上一分鐘，期間還佔著 worker。第一次請求要立刻回
    （由路由層回空白圖），實際轉檔排到背景。
    """
    import time
    d = _entry(tmp_path, small_odt, ".odt")
    scheduled = []
    monkeypatch.setattr(ws, "_schedule_thumbnail",
                        lambda dd, ee: scheduled.append((dd, ee)))
    t0 = time.perf_counter()
    with pytest.raises(ws.WorkspaceError):
        ws._office_thumbnail(d, ".odt")          # 非 blocking
    assert time.perf_counter() - t0 < 0.5, "請求路徑卡住了"
    assert scheduled, "沒有排到背景"


def test_background_generation_is_deduped(tmp_path, small_odt, monkeypatch):
    """同一個檔連續被要求多次，只會排一次轉檔。"""
    d = _entry(tmp_path, small_odt, ".odt")
    calls = []
    monkeypatch.setattr(
        ws, "_office_thumbnail",
        lambda dd, ee, blocking=False: (calls.append(1), None)[1]
        if blocking else None)
    # 直接測排程本身的去重（不真的轉檔）
    ws._thumb_building.clear()
    ws._thumb_building.add(str(d))
    ws._schedule_thumbnail(d, ".odt")
    assert not calls, "已經在排隊了還再排一次"
    ws._thumb_building.clear()
