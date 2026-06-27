"""非 JSON / 壞掉的 request body 應回 400（而非 500）。

端點以 `await request.json()` 解析 body；收到非 JSON（如 multipart）或空 body 時
json.JSONDecodeError 原本會冒成 500。app/main.py 註冊全域 JSONDecodeError 處理器
統一改回 400「Invalid JSON body」。涵蓋全部 ~80 個 request.json() 呼叫點。
"""
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

# 一個用 `await request.json()` 的 JSON 端點（admin branding 站名）
_JSON_ENDPOINT = "/admin/branding/site-name"


def test_malformed_json_body_returns_400_not_500():
    """送非 JSON（multipart）body → 400，不可 500。"""
    r = client.post(_JSON_ENDPOINT, files={"x": ("a.txt", b"1")})
    assert r.status_code != 500, "非 JSON body 不應 500"
    assert r.status_code == 400
    assert "Invalid JSON" in r.text


def test_empty_body_returns_400_not_500():
    """完全空 body → 400，不可 500。"""
    r = client.post(_JSON_ENDPOINT, headers={"Content-Type": "application/json"})
    assert r.status_code != 500
    assert r.status_code == 400


def test_garbage_json_returns_400():
    """壞掉的 JSON 片段 → 400。"""
    r = client.post(_JSON_ENDPOINT, content=b"{not json",
                    headers={"Content-Type": "application/json"})
    assert r.status_code == 400


def test_valid_json_still_works():
    """正確 JSON → 不是 400/500（正常流程不受影響）。"""
    r = client.post(_JSON_ENDPOINT, json={"name": "測試站台"})
    assert r.status_code not in (400, 500)
