"""知識庫的管理端點：上傳（格式驗證、重複、背景作業）→ 啟用 → 檢索、資料集編輯、
設定備份把 embedding 金鑰換成目標機器的金鑰。"""
from __future__ import annotations

import time

import pytest

from app.core.kb import store
from tests._kb_support import HANDBOOK_TXT, kb_isolated, make_pdf  # noqa: F401


def _wait_versions(client, did, timeout=30.0) -> list[dict]:
    end = time.time() + timeout
    while time.time() < end:
        vs = client.get(f"/admin/knowledge/api/datasets/{did}/versions").json()["versions"]
        if vs and all(v["status"] not in ("uploaded", "indexing") for v in vs):
            return vs
        time.sleep(0.2)
    raise AssertionError("背景處理沒有在時限內結束")


@pytest.fixture
def client(admin_session, kb_isolated):
    c, _, _ = admin_session
    return c


def test_upload_validate_process_activate_search(client):
    r = client.post("/admin/knowledge/api/datasets",
                    json={"name": "文書規範", "category": "writing_rules", "description": "手冊"})
    assert r.status_code == 200
    did = r.json()["id"]
    pdf = make_pdf([["壹、總述", "一、公文用語：下級對上級稱「鈞」。"]])
    files = [
        ("files", ("手冊.txt", HANDBOOK_TXT.encode("utf-8"), "text/plain")),
        ("files", ("第二份.pdf", pdf, "application/pdf")),
        ("files", ("假的.pdf", b"this is not a pdf at all", "application/pdf")),   # 副檔名騙人
        ("files", ("病毒.exe", b"MZ\x90\x00", "application/octet-stream")),       # 不收的格式
    ]
    r = client.post(f"/admin/knowledge/api/datasets/{did}/upload", files=files,
                    data={"version_label": "112 年修正", "published_on": "2023-09-20",
                          "source_url": "https://www.example.gov.tw/handbook"})
    assert r.status_code == 200, r.text
    j = r.json()
    assert len(j["created"]) == 2 and j["job_id"]
    reasons = {s["filename"]: s["reason"] for s in j["skipped"]}
    assert "對不上" in reasons["假的.pdf"] and "不支援" in reasons["病毒.exe"]
    vs = _wait_versions(client, did)
    assert {v["status"] for v in vs} == {"ready"}
    assert all(v["version_label"] == "112 年修正" and v["published_on"] == "2023-09-20"
               and v["effective_on"] == "" for v in vs)      # 生效日期不用發布日期代填
    # 啟用之前查不到
    q = {"query": "下級機關對上級機關的稱謂用語"}
    assert client.post("/admin/knowledge/api/search", json=q).json()["results"] == []
    for v in vs:
        assert client.post(f"/admin/knowledge/api/versions/{v['id']}/activate").status_code == 200
    res = client.post("/admin/knowledge/api/search", json=q).json()
    assert res["results"] and "鈞" in res["results"][0]["text"]
    assert res["mode"] == "keyword" and res["note"]
    # 同一份再上傳一次：提示重複、不重建
    r = client.post(f"/admin/knowledge/api/datasets/{did}/upload",
                    files=[("files", ("again.txt", HANDBOOK_TXT.encode("utf-8"), "text/plain"))])
    j = r.json()
    assert j["created"] == [] and j["job_id"] is None
    assert "已經有內容完全相同" in j["skipped"][0]["reason"]
    # 預覽
    pv = client.get(f"/admin/knowledge/api/versions/{vs[0]['id']}/preview").json()
    assert pv["chunks"] and pv["chunks"][0]["text"]


def test_bad_metadata_rejects_the_whole_upload(client):
    did = client.post("/admin/knowledge/api/datasets",
                      json={"name": "x", "category": "examples"}).json()["id"]
    r = client.post(f"/admin/knowledge/api/datasets/{did}/upload",
                    files=[("files", ("a.txt", "一、內容。".encode("utf-8"), "text/plain"))],
                    data={"source_url": "javascript:alert(1)"})
    assert r.status_code == 400
    assert client.get(f"/admin/knowledge/api/datasets/{did}/versions").json()["versions"] == []


def test_active_document_must_be_deactivated_before_reprocess_or_delete_in_progress(client):
    did = client.post("/admin/knowledge/api/datasets",
                      json={"name": "y", "category": "examples"}).json()["id"]
    client.post(f"/admin/knowledge/api/datasets/{did}/upload",
                files=[("files", ("a.txt", "一、內容。".encode("utf-8"), "text/plain"))])
    v = _wait_versions(client, did)[0]
    client.post(f"/admin/knowledge/api/versions/{v['id']}/activate")
    r = client.post(f"/admin/knowledge/api/versions/{v['id']}/reprocess")
    assert r.status_code == 400
    # 編輯資料
    r = client.post(f"/admin/knowledge/api/versions/{v['id']}/meta",
                    json={"title": "新標題", "effective_on": "2026-01-01"})
    assert r.status_code == 200 and r.json()["title"] == "新標題"
    r = client.post(f"/admin/knowledge/api/versions/{v['id']}/meta", json={"published_on": "昨天"})
    assert r.status_code == 400


def test_dataset_update_and_delete_removes_files(client):
    r = client.post("/admin/knowledge/api/datasets", json={"name": "z", "category": "business_law"})
    d = r.json()
    assert d["purpose"] == "substantive_basis"
    dup = client.post("/admin/knowledge/api/datasets", json={"name": "z", "category": "examples"})
    assert dup.status_code == 400
    client.post(f"/admin/knowledge/api/datasets/{d['id']}/upload",
                files=[("files", ("a.txt", "一、內容。".encode("utf-8"), "text/plain"))])
    v = _wait_versions(client, d["id"])[0]
    p = store.stored_path(v)
    assert p.is_file()
    r = client.post(f"/admin/knowledge/api/datasets/{d['id']}",
                    json={"name": "z2", "category": "examples", "access": "all"})
    assert r.status_code == 200 and r.json()["purpose"] == "style_example"
    r = client.post(f"/admin/knowledge/api/datasets/{d['id']}/delete")
    assert r.status_code == 200 and r.json()["deleted_documents"] == 1
    assert not p.exists()


def test_settings_export_rekeys_the_embedding_key(kb_isolated, tmp_path, monkeypatch):
    """備份檔裡是明文（標成敏感），匯入時用目標機器的金鑰重新加密。"""
    from app.core import settings_export
    from app.core.kb import embed
    embed.save({"base_url": "http://h:1", "model": "m", "key_input": "k-777"})
    blob = settings_export._rekey_specs()["knowledge_settings.json"]
    mod, locate = blob
    assert mod is embed
    import json
    data = json.loads(embed.settings_path().read_text(encoding="utf-8"))
    holders = locate(data)
    assert holders and all(h is not None for h, _ in holders)
    assert mod.decrypt_secret(holders[0][0]["api_key_enc"]) == "k-777"
