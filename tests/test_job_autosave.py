"""作業完成後自動存入工作區。

離開頁面之後，結果檔原本只靠 `data/jobs/` 的 24 小時保留期撐著 —— 隔天回來就
沒了。工作區才是「各工具輸出的檔案放這裡」的地方，結果理當自動流進去。

**工作區停用時不另外找地方存**：管理員關掉它是明確的決定，偷偷存到別處等於繞過
那個決定，還會變成第二個沒人管的磁碟成長來源。停用時改由 UI 講清楚保留期限。
"""
from __future__ import annotations

import io
import zipfile

import pytest

from app.core import job_autosave, workspace as ws
from app.core.job_manager import Job


@pytest.fixture(autouse=True)
def _data_dir(tmp_path, monkeypatch):
    d = tmp_path / "data"
    d.mkdir()
    monkeypatch.setattr("app.config.settings.data_dir", d)
    ws._CACHE = None
    yield d
    ws._CACHE = None


def _pptx(size_pad: int = 0) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", "<x/>")
        z.writestr("ppt/presentation.xml", "<x/>")
        if size_pad:
            z.writestr("pad.bin", b"\0" * size_pad)
    return buf.getvalue()


def _job(tmp_path, name="簡報.pptx", data=None, status="done", owner=None):
    p = tmp_path / name
    p.write_bytes(data if data is not None else _pptx())
    j = Job(id="a" * 32, tool_id="pdf-to-slides", status=status)
    j.result_path = p
    j.result_filename = name
    j.owner_id = owner
    return j


def test_saves_result_into_workspace(tmp_path, _data_dir):
    res = job_autosave.on_job_finished(_job(tmp_path))
    assert res and res["saved"] is True, res
    key = ws.key_for_user_id(None)
    files = list((_data_dir / "workspace" / key).iterdir())
    assert len(files) == 1


def test_pptx_is_accepted(tmp_path, _data_dir):
    """**這是整件事的前提**：「PDF 轉簡報檔」產出的就是 .pptx，
    工作區原本收不下 —— 自動存入對它會永遠失敗。"""
    res = job_autosave.on_job_finished(_job(tmp_path, "季報.pptx"))
    assert res["saved"] is True


def test_disabled_workspace_does_not_invent_another_store(tmp_path, _data_dir):
    """工作區停用時**不可**偷偷存到別處 —— 那等於繞過管理員的決定。"""
    ws.save_settings({"enabled": False})
    res = job_autosave.on_job_finished(_job(tmp_path))
    assert res["saved"] is False
    assert res["reason"] == "workspace_disabled"
    assert not (_data_dir / "workspace").exists() or \
        not any((_data_dir / "workspace").rglob("meta.json"))


def test_quota_exceeded_is_reported_not_silent(tmp_path, _data_dir):
    """額度滿要講原因 —— 無聲失敗最糟：使用者以為存好了，隔天檔案卻不在。"""
    ws.save_settings({"enabled": True, "per_user_quota_mb": 1})
    res = job_autosave.on_job_finished(
        _job(tmp_path, data=_pptx(size_pad=3 * 1024 * 1024)))
    assert res["saved"] is False
    assert res["reason"] in ("quota", "too_large"), res


def test_unsupported_type_is_reported(tmp_path, _data_dir):
    j = _job(tmp_path, "output.zip", data=b"PK\x03\x04" + b"\0" * 200)
    res = job_autosave.on_job_finished(j)
    assert res["saved"] is False and res["reason"] == "unsupported"


def test_oversized_result_not_auto_saved(tmp_path, _data_dir, monkeypatch):
    """自動把一個很大的產出塞進去，等於幫使用者把額度用掉大半 —— 超過上限
    就不自動存（仍可手動）。"""
    monkeypatch.setattr(job_autosave, "_AUTO_MAX_BYTES", 1024)
    res = job_autosave.on_job_finished(
        _job(tmp_path, data=_pptx(size_pad=64 * 1024)))
    assert res["saved"] is False and res["reason"] == "too_large"


def test_failed_job_is_not_saved(tmp_path, _data_dir):
    assert job_autosave.on_job_finished(
        _job(tmp_path, status="error")) is None


def test_missing_result_file_is_not_an_error(tmp_path, _data_dir):
    j = _job(tmp_path)
    j.result_path.unlink()
    assert job_autosave.on_job_finished(j) is None


def test_never_raises(tmp_path, _data_dir, monkeypatch):
    """自動存檔失敗**絕不**能把一個已經成功的轉換標記成失敗。"""
    def boom(*a, **k):
        raise RuntimeError("磁碟爆了")
    monkeypatch.setattr(ws, "save_bytes_for_key", boom)
    res = job_autosave.on_job_finished(_job(tmp_path))
    assert res["saved"] is False and res["reason"] == "error"


def test_end_to_end_through_job_manager(tmp_path, _data_dir):
    """走完整的 job_manager 流程 —— 光測 helper 不算，要確認真的有被接上。"""
    import time

    from app.core import job_store
    from app.core.job_manager import JobManager
    job_store.init()
    out = tmp_path / "產出.pptx"

    def run(job):
        out.write_bytes(_pptx())
        job.result_path = out
        job.result_filename = "產出.pptx"

    m = JobManager(workers=1)
    j = m.submit("pdf-to-slides", run)
    end = time.time() + 8
    while time.time() < end and j.status != "done":
        time.sleep(0.03)
    assert j.status == "done"
    assert j.meta.get("workspace", {}).get("saved") is True, j.meta


# ---------------- 只在使用者離開後才自動存 ----------------

def test_not_saved_while_the_user_is_watching(tmp_path, _data_dir):
    """頁面還開著就不必自動存 —— 人就在那裡，按下載或那顆「存至工作區」就好。
    硬存只會多一份重複檔案並吃掉他的額度。"""
    from app.core.job_manager import job_manager
    j = _job(tmp_path)
    job_manager._jobs[j.id] = j
    try:
        job_manager.mark_polled(j.id)          # 模擬頁面正在輪詢
        res = job_autosave.on_job_finished(j)
        assert res["saved"] is False
        assert res["reason"] == "still_watching"
    finally:
        job_manager._jobs.pop(j.id, None)


def test_saved_once_polling_stops(tmp_path, _data_dir, monkeypatch):
    """輪詢停了一段時間 = 使用者離開了 → 這時才自動存。"""
    import time as _t

    from app.core.job_manager import job_manager
    j = _job(tmp_path)
    job_manager._jobs[j.id] = j
    try:
        job_manager.mark_polled(j.id)
        j.last_polled_at = _t.time() - (job_manager.IDLE_AFTER + 5)
        res = job_autosave.on_job_finished(j)
        assert res["saved"] is True, res
    finally:
        job_manager._jobs.pop(j.id, None)


def test_never_polled_counts_as_left(tmp_path, _data_dir):
    """從沒被輪詢過 = 透過 API 送出、或送出後立刻關掉頁面 —— 正是最需要
    自動保存的情境。"""
    res = job_autosave.on_job_finished(_job(tmp_path))
    assert res["saved"] is True
