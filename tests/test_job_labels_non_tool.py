"""知識庫的背景作業不可以標成「公文撰擬」。

## 由來

使用者收到的通知信是「[完成] 公文撰擬：知識庫・全國法規資料庫：下載資料」，
「我的作業」那一列也標著公文撰擬 —— 那是管理區「知識庫 → 政府公開資料」送出的
下載，跟公文撰擬無關。原因是知識庫的三種作業（匯入、重建索引、政府公開資料）
送進作業佇列時借用了 `official-doc` 這個工具代號。

修法：知識庫用自己的作業代號（`job_labels.KB_JOB_ID`），名稱與圖示在
`job_labels` 定義一份，「我的作業」、站內通知、作業佇列、通知信都從那裡查；
升級前留下的舊作業（代號是 `official-doc`、meta 帶 `kb`）顯示時一樣標成知識庫。

**反向對照一定要有**：真的公文撰擬作業（沒有 `kb`）照樣是公文撰擬 —— 只驗
「知識庫顯示成知識庫」的話，把所有 `official-doc` 都改名也會過。
"""
from __future__ import annotations

import ast
import json
import pathlib
import time

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
KB_DIR = ROOT / "app" / "core" / "kb"


@pytest.fixture(autouse=True)
def _data_dir(tmp_path, monkeypatch):
    from app.core import notify_settings as ns
    d = tmp_path / "data"
    d.mkdir()
    monkeypatch.setattr("app.config.settings.data_dir", d)
    ns.invalidate_cache()
    yield d
    ns.invalidate_cache()


# ---------- 送出時用的代號 ----------

def _submit_calls() -> list[tuple[str, ast.Call]]:
    out = []
    for p in sorted(KB_DIR.glob("*.py")):
        tree = ast.parse(p.read_text(encoding="utf-8"))
        for n in ast.walk(tree):
            if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                    and n.func.attr == "submit"
                    and isinstance(n.func.value, ast.Name)
                    and n.func.value.id == "job_manager"):
                out.append((p.name, n))
    return out


def test_the_scan_reaches_every_kb_submit():
    """匯入、重建索引、政府公開資料 —— 三處都要掃到，不然下面那條只是空迴圈。"""
    files = sorted({f for f, _ in _submit_calls()})
    assert len(_submit_calls()) >= 3 and files == ["gov.py", "indexer.py"], files


def test_kb_jobs_are_not_submitted_as_a_tool():
    from app.core import job_labels
    from app.core.kb import indexer
    assert indexer.TOOL_ID == job_labels.KB_JOB_ID
    for fname, call in _submit_calls():
        first = call.args[0] if call.args else None
        assert not isinstance(first, ast.Constant), (
            f"{fname}:{call.lineno} 用字面代號 {getattr(first, 'value', None)!r} 送出 "
            "—— 知識庫的作業要用 job_labels.KB_JOB_ID")
        assert isinstance(first, ast.Name) and first.id in ("KB_JOB_ID", "TOOL_ID"), \
            f"{fname}:{call.lineno} 的作業代號不是 KB_JOB_ID"


def test_the_kb_job_id_is_not_a_registered_tool():
    """它不是工具：撞到工具代號的話，權限與側欄會把它當成那支工具。"""
    from app.core import job_labels
    from app.tool_registry import discover_tools
    ids = {t.metadata.id for t in discover_tools()}
    assert not set(job_labels.NON_TOOL_JOBS) & ids


# ---------- 顯示 ----------

def test_display_name_for_new_and_legacy_kb_jobs():
    from app.core import job_labels as jl
    assert jl.display_name(jl.KB_JOB_ID, {}) == "公文知識庫"
    for kind in ("import", "rebuild", "gov"):
        assert jl.display_name("official-doc", {"kb": kind}) == "公文知識庫", kind
        assert jl.display_id("official-doc", {"kb": kind}) == jl.KB_JOB_ID


def test_real_official_doc_jobs_still_say_official_doc():
    """反向對照：公文撰擬自己的作業不可以被改名。"""
    from app.core import job_labels as jl
    for meta in ({}, None, {"upload_id": "a" * 32}, {"kb": "something-else"}):
        assert jl.display_id("official-doc", meta) == "official-doc"
        assert jl.display_name("official-doc", meta) == "公文撰擬"
    assert jl.display_name("pdf-merge", {"kb": "gov"}) != "公文知識庫"


def test_non_tool_names_are_translated():
    """名稱是語系檔的鍵（前端一律 `tr()`）—— 英日介面不可以退回中文。"""
    from app.core import job_labels as jl
    for lang in ("en", "ja"):
        cat = json.loads((ROOT / "app" / "i18n" / f"{lang}.json")
                         .read_text(encoding="utf-8"))
        for v in jl.NON_TOOL_JOBS.values():
            assert cat.get(v["name"]), f"{lang}.json 沒有「{v['name']}」"


class _Job:
    id = "f" * 32
    status = "done"
    error = ""
    result_filename = ""

    def __init__(self, tool_id, meta):
        self.tool_id = tool_id
        self.meta = meta

    def elapsed(self):
        return 95.0


@pytest.mark.parametrize("tool_id", ["knowledge-base", "official-doc"])
def test_notification_names_the_knowledge_base(tool_id):
    """通知的標題、內文、HTML 都寫知識庫；舊代號的作業（升級前送出、升級後才結束）也一樣。"""
    from app.core import job_notify
    job = _Job(tool_id, {"filename": "知識庫・全國法規資料庫：下載資料", "kb": "gov"})
    subject, text = job_notify.build_message(job)
    assert subject == "[完成] 公文知識庫：知識庫・全國法規資料庫：下載資料", subject
    assert "公文撰擬" not in subject + text
    html = job_notify.build_html(job)
    assert "公文知識庫 已完成" in html and "公文撰擬" not in html
    # 信裡的工具圖示要產得出來（HTML 一律引用 cid:，少一張就是破圖）
    png = job_notify.build_images(job).get(job_notify.ICON_CID)
    assert png and png[:8] == b"\x89PNG\r\n\x1a\n"


def test_official_doc_notification_is_unchanged():
    from app.core import job_notify
    subject, _ = job_notify.build_message(_Job("official-doc", {"filename": "簽.odt"}))
    assert subject == "[完成] 公文撰擬：簽.odt"


# ---------- 清單（我的作業、站內通知、作業佇列） ----------

def _seed(job_id: str, tool_id: str, meta: dict):
    from app.core import job_store
    from app.core.job_manager import Job
    job_store.init()
    j = Job(id=job_id, tool_id=tool_id, status="done", meta=meta)
    j.client_ip = "10.1.2.3"
    j.created_at = time.time() - 100
    j.updated_at = j.finished_at = time.time()
    job_store.upsert(j)


def test_lists_show_the_knowledge_base(auth_off):
    from fastapi.testclient import TestClient

    from app import main as app_main
    c = TestClient(app_main.app, client=("10.1.2.3", 1234))
    meta = {"filename": "知識庫・全國法規資料庫：下載資料", "kb": "gov",
            "view_url": "/admin/knowledge/gov"}
    _seed("1" * 32, "knowledge-base", meta)
    _seed("2" * 32, "official-doc", meta)                       # 升級前的舊列
    _seed("3" * 32, "official-doc", {"filename": "簽.odt"})      # 真的公文撰擬

    want = {"1" * 32: ("knowledge-base", "公文知識庫"),
            "2" * 32: ("knowledge-base", "公文知識庫"),
            "3" * 32: ("official-doc", "公文撰擬")}
    for url in ("/api/jobs", "/api/my/inbox", "/admin/jobs/api/list"):
        r = c.get(url)
        assert r.status_code == 200, (url, r.status_code)
        body = r.json()
        rows = body.get("jobs") or body.get("items") or []
        got = {j["id"]: (j["tool_id"], j["tool_name"]) for j in rows}
        for jid, exp in want.items():
            assert got.get(jid) == exp, (url, jid, got.get(jid))
        if url == "/admin/jobs/api/list":
            res = {j["id"]: j["resources"] for j in rows}
            # 知識庫不起 soffice、也不走外部服務的號誌 —— 不要再標成 Office ＋ 外部服務
            assert res["1" * 32] == [] and res["2" * 32] == []
            assert "office" in res["3" * 32]


def test_job_icon_sprites_include_the_knowledge_base():
    """清單上的圖示是從隱藏的 sprite 複製的 —— 沒有這一格會退回通用圖示。"""
    from app import main as app_main
    tiles = {t["id"]: t for t in app_main._tpl_tool_tiles()}
    assert tiles["knowledge-base"]["icon"] == "book"
    assert tiles["knowledge-base"]["name"] == "公文知識庫"
