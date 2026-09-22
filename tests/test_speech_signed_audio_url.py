"""語音服務拉錄音檔的簽章網址：驗得過才給，**驗不過一律當成找不到**。

## 由來（v1.15.93）

外部語音服務的 `source.type` 只能是 `url` —— 錄音檔是**對方自己來拉**的。
最順手的做法是發一把 API Token 給對方，但**那條路不能走**：
`api_tokens` 沒有 scope，一把 token 解鎖全部 `/api/*`
（作業、通知、工作區、每一支工具），只為了讓對方抓一個錄音檔。

簽章網址反過來：**沒有東西交出去**，過期自動失效，不需要撤銷。
"""
from __future__ import annotations

import time

import pytest

from app.core import signed_url as su

KIND, IDENT = "audio", "a" * 32


def test_a_fresh_signature_verifies():
    exp, sig = su.sign(KIND, IDENT)
    assert su.verify(KIND, IDENT, str(exp), sig)


@pytest.mark.parametrize("why,args", [
    ("換一個檔案 id", (KIND, "b" * 32)),
    ("換一種用途",   ("other", IDENT)),
])
def test_a_signature_is_bound_to_what_it_signed(why, args):
    exp, sig = su.sign(KIND, IDENT)
    assert not su.verify(args[0], args[1], str(exp), sig), why


def test_extending_the_expiry_breaks_the_signature():
    """**到期時間要在簽章裡。** 只簽 id 的話，改 `exp` 就能無限延期。"""
    exp, sig = su.sign(KIND, IDENT)
    assert not su.verify(KIND, IDENT, str(exp + 86400), sig)


def test_an_expired_signature_is_refused():
    past = int(time.time()) - 10
    assert not su.verify(KIND, IDENT, str(past), su._sig(KIND, IDENT, past))


@pytest.mark.parametrize("exp,sig", [
    (None, "x" * 64), ("", "x" * 64), ("不是數字", "x" * 64),
    ("99999999999", None), ("99999999999", ""),
])
def test_missing_or_malformed_parameters_are_refused(exp, sig):
    assert not su.verify(KIND, IDENT, exp, sig)


def test_the_endpoint_says_not_found_for_every_kind_of_failure(tmp_path, monkeypatch):
    """**四種失敗在外面要長得一模一樣。**

    回 403 等於告訴對方「這個 id 是存在的」，而 id 本身就是我們不想外流的。
    只驗「簽對的過」是不夠的 —— 那樣把端點改成一律放行也會過。
    """
    from fastapi.testclient import TestClient
    from app.config import settings
    from app.web import speech_routes as sr
    from app.main import app

    monkeypatch.setattr(settings, "data_dir", tmp_path, raising=False)
    sr.audio_dir().mkdir(parents=True, exist_ok=True)
    sr.audio_path(IDENT).write_bytes(b"ID3fake-audio-bytes")

    c = TestClient(app)
    exp, sig = su.sign(KIND, IDENT)
    ok = c.get(f"/api/speech/audio/{IDENT}?exp={exp}&sig={sig}")
    assert ok.status_code == 200, "簽對了卻拿不到 —— 這條測試自己壞了"
    assert ok.content == b"ID3fake-audio-bytes"

    past = int(time.time()) - 10
    cases = {
        "簽章是錯的": f"?exp={exp}&sig={'0' * 64}",
        "已經過期":   f"?exp={past}&sig={su._sig(KIND, IDENT, past)}",
        "什麼都沒帶": "",
    }
    for why, qs in cases.items():
        r = c.get(f"/api/speech/audio/{IDENT}{qs}")
        assert r.status_code == 404, f"{why} 應該回 404，卻回 {r.status_code}"

    # 檔案不存在、以及 id 格式不對 —— 同樣是 404
    other = "b" * 32
    e2, s2 = su.sign(KIND, other)
    assert c.get(f"/api/speech/audio/{other}?exp={e2}&sig={s2}").status_code == 404
    assert c.get("/api/speech/audio/../etc/passwd").status_code == 404


def test_the_url_is_built_from_a_configured_base_not_the_request_host():
    """**網址要用寫定的位址組。**

    照請求的 Host 組的話，從對外網域進來的人送出的作業會帶著那個網域，
    被對方的來源白名單正確擋掉 —— 症狀是「有些人可以、有些人不行」。
    """
    import inspect
    from app.web import speech_routes as sr

    url = sr.sign_url(IDENT, "http://192.168.0.1:8765")
    assert url.startswith("http://192.168.0.1:8765/api/speech/audio/")
    assert "exp=" in url and "sig=" in url

    src = inspect.getsource(sr.sign_url)
    for banned in ("request", "headers", "url.hostname"):
        assert banned not in src, f"`sign_url` 用到了請求的東西：{banned}"


def test_the_audio_endpoint_supports_range_requests(tmp_path, monkeypatch):
    """**對方會續傳** —— 接入清單第 2 節：「支援 Range 要求」。

    目前是 `FileResponse` 自己做的（Starlette 會回 206 ＋ `Content-Range`），
    但那是實作細節。哪天有人為了加標頭或串流改寫這個端點，
    續傳就會**安靜地退回整檔重拉** —— 大檔在不穩的線路上可能永遠拉不完，
    而我們這側只會看到 `source_unreachable`，看不出是少了 Range。

    所以判準放在**行為**上：206 ＋ 正確的 `Content-Range` ＋ 正確的位元組。
    """
    from fastapi.testclient import TestClient
    from app.config import settings
    from app.web import speech_routes as sr
    from app.main import app

    monkeypatch.setattr(settings, "data_dir", tmp_path, raising=False)
    sr.audio_dir().mkdir(parents=True, exist_ok=True)
    body = bytes(range(256)) * 8                      # 2048 bytes
    sr.audio_path(IDENT).write_bytes(body)

    c = TestClient(app)
    exp, sig = su.sign(KIND, IDENT)
    url = f"/api/speech/audio/{IDENT}?exp={exp}&sig={sig}"

    full = c.get(url)
    assert full.status_code == 200
    assert full.headers.get("accept-ranges") == "bytes", (
        "沒有公告支援 Range —— 對方不會嘗試續傳")

    part = c.get(url, headers={"Range": "bytes=100-199"})
    assert part.status_code == 206, f"Range 要求沒有回 206（{part.status_code}）"
    assert part.content == body[100:200], "回的位元組不對"
    assert part.headers.get("content-range") == f"bytes 100-199/{len(body)}"

    tail = c.get(url, headers={"Range": "bytes=2040-"})
    assert tail.status_code == 206 and tail.content == body[2040:]
