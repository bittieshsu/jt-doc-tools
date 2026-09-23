"""會議錄音轉逐字稿 —— 端到端（對象是自己起的假 jtlw）。

**為什麼不用對方給的 mock**：那份在 `docs-share/`（不進版控，也不會同步到
公開樹），測試不可以依賴它。這裡起一個最小的假服務，只實作我們真的會打的
那幾支 —— 而且**順便驗我們送出去的請求長什麼樣**，那是假 mock 驗不到的。

**已經用對方的 mock 實跑過一次**（115 段、2 位發言者、ACK 有送），
當場抓到一個真 bug：`safe_remote_base_url()` 刻意把路徑丟掉，
於是請求打到 `/jobs` 而不是 `/api/v1/jobs`，對方回 404。
**只有真的送出去才看得到** —— 單元測試對得起來，因為那是我們自己算的字串。
"""
from __future__ import annotations

import json
import pathlib
import socket
import threading
import time

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.core import jtlw_settings as js


# --------------------------------------------------------------- 假 jtlw

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeJtlw:
    """只做我們會打的那幾支。`seen` 留下收到的請求供斷言。"""

    def __init__(self, *, fail_with: dict | None = None, segments: int = 5,
                 running_polls: int = 0, reject_with: dict | None = None):
        self.seen: dict = {}
        self.acked: list[str] = []
        self.cancelled: list[str] = []
        self.fail_with = fail_with
        #: 送件當下就被拒絕（對方 v1.7 起把拉檔提前到這一刻，
        #: 所以來源相關的錯誤多半走這條路而不是終態的 `errors[]`）。
        self.reject_with = reject_with
        self.n = segments
        #: 先回幾次「執行中」再回成功 —— 用來驗進度真的會動。
        self.running_polls = running_polls
        self.polls = 0
        self.app = self._build()
        self.port = _free_port()
        self._server = None

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def _build(self) -> FastAPI:
        app = FastAPI()
        me = self

        @app.get("/api/v1/health")
        async def health():
            return {"status": "ok"}

        @app.get("/api/v1/capabilities")
        async def caps(request: Request):
            if not request.headers.get("authorization", "").startswith("Bearer "):
                return JSONResponse({"error": {"code": "unauthorized"}}, status_code=401)
            return {"api_revision": "2.1", "limits": {"max_duration_s": 28800}}

        @app.post("/api/v1/jobs")
        async def create(request: Request):
            if me.reject_with:
                return JSONResponse({"error": me.reject_with}, status_code=422)
            me.seen["body"] = await request.json()
            me.seen["idempotency_key"] = request.headers.get("idempotency-key")
            me.seen["auth"] = request.headers.get("authorization")
            return JSONResponse({"job_id": "job_fake_1", "status": "queued"}, status_code=202)

        @app.get("/api/v1/jobs/{job_id}")
        async def job(job_id: str):
            me.polls += 1
            if me.fail_with:
                # 終態的錯誤在 `errors` 陣列裡（對方 2026-09-21 給的實際欄位）
                return {"status": "failed", "errors": [me.fail_with]}
            if job_id in me.cancelled:
                return {"status": "cancelled"}
            if me.polls <= me.running_polls:
                # **`progress` 是物件不是數字，`stage` 在它裡面**。
                # 第一版這支假伺服器回的是 `progress: 1.0` ＋ 頂層 `stage`
                # —— 那是我猜的形狀，於是它**完全抓不到**我們接錯欄位這件事
                # （進度條從頭到尾不會動）。是對方看到我們的說明才指出來的。
                # **假伺服器要照對方文件的契約寫，不是照我們以為的樣子寫。**
                return {"status": "running",
                        "queue_position": None,
                        "progress": {"stage": "asr", "stage_index": 3,
                                     "stage_count": 6,
                                     "processed_audio_ms": 30000,
                                     "total_audio_ms": 60000,
                                     "percent": 50.0}}
            return {"status": "succeeded",
                    "progress": {"stage": "finalize", "percent": 100.0},
                    "result": {"duration_s": 60, "language": "zh"}}

        @app.get("/api/v1/jobs/{job_id}/segments")
        async def segs(job_id: str, layer: str, after_seq: int = 0, limit: int = 500):
            # 對方確認：鍵一定是 `segments`、`has_more` 一定會出現（布林），
            # **不是用空陣列表示結尾**。
            if after_seq:                      # 只有一頁
                return {"segments": [], "has_more": False, "complete": True}
            rows = []
            for i in range(1, me.n + 1):
                if layer == "raw":
                    rows.append({"seq": i, "text": f"原始第 {i} 句",
                                 "start_ms": i * 1000, "end_ms": i * 1000 + 800})
                elif layer == "final":
                    rows.append({"seq": i, "text": f"校正後第 {i} 句。"})
                else:
                    # **刻意讓第 2 段沒有發言者** —— 三層對不上時不可以硬湊
                    if i != 2:
                        rows.append({"seq": i, "speaker_id": f"S{(i % 2) + 1}"})
            return {"segments": rows, "has_more": False, "complete": True}

        @app.post("/api/v1/jobs/{job_id}/ack")
        async def ack(job_id: str):
            me.acked.append(job_id)
            return {"ok": True}

        @app.post("/api/v1/jobs/{job_id}/cancel")
        async def cancel(job_id: str):
            me.cancelled.append(job_id)
            return {"status": "cancelled"}

        return app

    def __enter__(self):
        import uvicorn
        cfg = uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_level="error")
        self._server = uvicorn.Server(cfg)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        for _ in range(100):                   # 等它真的聽得到
            if getattr(self._server, "started", False):
                break
            time.sleep(0.05)
        return self

    def __exit__(self, *exc):
        self._server.should_exit = True
        self._thread.join(timeout=5)


@pytest.fixture
def unconfigured():
    js.save({"enabled": False, "base_url": "", "api_key_enc": "", "audio_base_url": ""})
    js.invalidate_cache()
    yield
    js.save({"enabled": False, "base_url": "", "api_key_enc": "", "audio_base_url": ""})
    js.invalidate_cache()


def _configure(fake: FakeJtlw, *, audio_base: str = "http://audio.test:8765") -> None:
    js.save({"enabled": True, "base_url": fake.base,
             "api_key_enc": "jtlw_test_key", "audio_base_url": audio_base})
    js.invalidate_cache()


def _upload(client, name: str = "會議.m4a") -> dict:
    data = b"\x00\x00\x00\x20ftypM4A " + b"x" * 5000
    r = client.post("/tools/meeting-transcribe/upload",
                    files={"file": (name, data, "audio/mp4")})
    assert r.status_code == 200, r.text
    return r.json()


def _run(client, upload_id: str, timeout: float = 60.0) -> dict:
    r = client.post("/tools/meeting-transcribe/start", json={"upload_id": upload_id})
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]
    t0 = time.time()
    while time.time() - t0 < timeout:
        j = client.get(f"/api/jobs/{job_id}").json()
        if j.get("status") in ("done", "error", "cancelled"):
            return j
        time.sleep(0.3)
    raise AssertionError(f"作業沒有結束：{j}")


# --------------------------------------------------------------- 沒設定好

def test_the_tool_is_locked_until_jtlw_is_configured(unconfigured):
    """使用者 2026-09-21 指示：沒設定好就反灰。"""
    import app.main as m
    groups = m._nav_groups_for_locale(None)
    row = [t for g in groups for t in g["tools"] if t["id"] == "meeting-transcribe"]
    assert row, "側欄裡找不到這支工具"
    assert row[0].get("locked"), "沒設定 jtlw 卻沒有反灰"
    assert row[0].get("lock_reason"), "反灰了卻說不出原因"
    assert row[0].get("lock_setup_url") == "/admin/jtlw", "說不出該去哪裡設定"


def test_the_page_says_so_instead_of_showing_a_dead_upload_box(client, unconfigured):
    """**不可以是一個看起來正常、按下去才失敗的上傳區**。"""
    r = client.get("/tools/meeting-transcribe/")
    assert r.status_code == 200
    assert "還沒設定語音服務" in r.text
    assert 'id="mtUp"' not in r.text, "沒設定卻還是畫出了上傳區"


def test_upload_without_configuration_is_503_not_500(client, unconfigured):
    """**部署問題不是使用者送錯東西** —— 500 會讓人以為服務掛了而一直重試。"""
    r = client.post("/tools/meeting-transcribe/upload",
                    files={"file": ("a.m4a", b"xx", "audio/mp4")})
    assert r.status_code == 503, r.status_code


# --------------------------------------------------------------- 端到端

def test_the_whole_flow_against_a_fake_jtlw(client, unconfigured):
    with FakeJtlw() as fake:
        _configure(fake)
        up = _upload(client)
        job = _run(client, up["upload_id"])
        assert job["status"] == "done", job.get("error")
        assert job["has_result"], "完成了卻沒有 result_path —— 我的作業會沒有下載鈕"

        r = client.get(f"/tools/meeting-transcribe/result/{up['upload_id']}")
        assert r.status_code == 200, r.text
        out = r.json()
        segs = out["segments"]
        assert len(segs) == 5
        # final 層的文字勝出、raw 層的時間留著。
        # **時間只在 `raw` 層** —— 語音服務 2026-09-22 特別提醒這個坑
        #（他們自己第一次也寫錯，拿 `speakers` 層去抓 `start_ms` 直接 KeyError）。
        # 這支假伺服器的 `speakers` 層**刻意不帶時間**，所以下面這一行
        # 只有真的從 `raw` 用 `seq` 對回去才會成立。
        assert segs[0]["text"] == "校正後第 1 句。"
        assert segs[0]["start_ms"] == 1000
        # **對不上的不可以硬湊**：第 2 段沒有發言者就是沒有
        assert "speaker" not in segs[1], "把別人的發言者貼到沒有發言者的那一段上了"
        assert segs[0]["speaker"] == "S2"


def test_the_submitted_url_comes_from_the_configured_address_not_the_request(client, unconfigured):
    """**錄音檔網址用寫定的對外位址組** —— 照請求的 Host 組的話，
    從對外網域進來的人送出的作業會被對方的來源白名單擋掉，
    而症狀是「有些人可以、有些人不行」。"""
    with FakeJtlw() as fake:
        _configure(fake, audio_base="http://audio.test:8765")
        up = _upload(client)
        _run(client, up["upload_id"])
        src = fake.seen["body"]["source"]
        assert src["url"].startswith("http://audio.test:8765/api/speech/audio/"), src["url"]
        assert "testserver" not in src["url"], "拿請求的 Host 組了網址"
        # 對方會核對這兩項，對不上就退件
        assert src["sha256"] == up["sha256"]
        assert src["size_bytes"] == up["size_bytes"]
        # 重送不可以變成兩件
        assert fake.seen["idempotency_key"] == f"jtdt-{up['upload_id']}"
        assert fake.seen["auth"] == "Bearer jtlw_test_key"


def test_ack_is_sent_and_only_after_the_transcript_is_on_disk(client, unconfigured):
    """`ack` 的意思是「你可以刪了」—— 順序必須是「寫完 → 才 ACK」。"""
    with FakeJtlw() as fake:
        _configure(fake)
        up = _upload(client)
        _run(client, up["upload_id"])
        assert fake.acked == ["job_fake_1"], "沒有送 ACK，對方會留 72 小時才清"
        # ACK 送出去的時候，檔案一定已經在了
        r = client.get(f"/tools/meeting-transcribe/result/{up['upload_id']}")
        assert r.status_code == 200


def test_a_failure_says_which_field(client, unconfigured):
    """使用者已經傳完才被判定超過上限 —— 一句「處理失敗」等於什麼都沒說。"""
    with FakeJtlw(fail_with={"code": "audio_too_long", "category": "source",
                             "retryable": False,
                             "details": {"field": "source.url"}}) as fake:
        _configure(fake)
        up = _upload(client)
        job = _run(client, up["upload_id"])
        assert job["status"] == "error"
        # **不可以只把代碼原樣吐出來** —— 使用者看到 `audio_too_long`
        # 不知道要怎麼辦。訊息要說得出下一步（「請先分段」）。
        #
        # **判準刻意不驗那個「6 小時」**：上限是對方的，在 `/capabilities`
        # 的 `limits` 裡。寫進我們的訊息就是把同一份事實抄了第二份 ——
        # 他們調整上限的那一天，我們會很有自信地講一個錯的數字。
        assert "錄音太長" in job["error"] and "分段" in job["error"], job["error"]
        assert "audio_too_long" not in job["error"], "把代碼原樣丟給使用者了"
        assert fake.acked == [], "失敗了還去 ACK —— 那是叫對方刪掉一份我們沒有的東西"


def test_an_expired_audio_url_does_not_look_like_a_missing_file(client, unconfigured):
    """對方的 `source_unreachable` **涵蓋「網址過期」**（他們 2026-09-21 講明）。

    我們給的是短效簽章網址，所以 404 十之八九是過期不是檔案不見 ——
    照字面講「找不到檔案」的話，使用者會去找一個其實還在的檔案。
    """
    with FakeJtlw(fail_with={"code": "source_unreachable", "retryable": True,
                             "details": {"http_status": "404"}}) as fake:
        _configure(fake)
        up = _upload(client)
        job = _run(client, up["upload_id"])
        assert "過期" in job["error"], job["error"]
        assert "不見" not in job["error"] and "不存在" not in job["error"], job["error"]


def test_progress_actually_moves_and_the_stage_is_readable(client, unconfigured):
    """接錯欄位的症狀是「進度條從頭到尾不動、階段永遠是『處理中』」。

    **這條要有東西在中間動**才驗得到 —— 假伺服器先回幾次「執行中」。
    """
    with FakeJtlw(running_polls=2) as fake:
        _configure(fake)
        up = _upload(client)
        r = client.post("/tools/meeting-transcribe/start",
                        json={"upload_id": up["upload_id"]})
        job_id = r.json()["job_id"]
        seen_msg, seen_progress = set(), set()
        t0 = time.time()
        while time.time() - t0 < 60:
            j = client.get(f"/api/jobs/{job_id}").json()
            seen_msg.add(j.get("message"))
            seen_progress.add(j.get("progress"))
            if j.get("status") in ("done", "error", "cancelled"):
                break
            time.sleep(0.3)
        assert j["status"] == "done", j.get("error")
        assert "辨識中" in seen_msg, f"階段文字沒有翻出來：{seen_msg}"
        assert "處理中" not in seen_msg, f"階段接錯了，退回預設字樣：{seen_msg}"
        moving = {p for p in seen_progress if isinstance(p, float) and 0.02 < p < 1.0}
        assert moving, f"進度條從頭到尾沒動過：{sorted(seen_progress)}"


def test_unsupported_extension_is_rejected_with_a_useful_message(client, unconfigured):
    with FakeJtlw() as fake:
        _configure(fake)
        r = client.post("/tools/meeting-transcribe/upload",
                        files={"file": ("會議.pdf", b"%PDF-1.4", "application/pdf")})
        assert r.status_code == 400
        assert ".m4a" in r.text, "訊息沒有列出收得下哪些格式"


def test_an_empty_file_leaves_nothing_behind(client, unconfigured):
    from app.web import speech_routes as sr
    with FakeJtlw() as fake:
        _configure(fake)
        before = set(sr.audio_dir().glob("*.bin")) if sr.audio_dir().is_dir() else set()
        r = client.post("/tools/meeting-transcribe/upload",
                        files={"file": ("空的.m4a", b"", "audio/mp4")})
        assert r.status_code == 400
        after = set(sr.audio_dir().glob("*.bin")) if sr.audio_dir().is_dir() else set()
        assert after == before, "空檔案被擋下來了，但半個檔案留在磁碟上"


def test_no_ack_when_writing_the_transcript_fails(client, unconfigured, monkeypatch):
    """**這一條才分得出順序。**

    上面那條只驗「兩件事都發生了」—— 把 `ack()` 搬到寫檔**之前**，它照樣全綠。
    這條讓寫檔失敗：順序對的話 ACK 根本輪不到，順序反了就會送出去，
    而那等於**叫對方刪掉一份我們沒有存到的逐字稿**。
    """
    # **不可以寫 `from app.tools.meeting_transcribe import router as mt`** ——
    # 套件的 `__init__` 做了 `from .router import router`，那個名字被
    # APIRouter **物件**遮住了（同 `tr` 被表格列變數遮蔽那一課）。
    import importlib
    mt = importlib.import_module("app.tools.meeting_transcribe.router")

    def boom(*a, **kw):
        raise OSError("磁碟滿了")

    with FakeJtlw() as fake:
        _configure(fake)
        up = _upload(client)
        monkeypatch.setattr(mt.atomic_json, "write_json", boom)
        job = _run(client, up["upload_id"])
        assert job["status"] == "error", "寫檔失敗了卻回報成功"
        assert fake.acked == [], "逐字稿沒寫進去卻送了 ACK —— 對方會把它刪掉"


# --------------------------------------------------- 對方的自簽憑證

_SELF_SIGNED = """-----BEGIN CERTIFICATE-----
MIIBkTCB+6ADAgECAhQAAAAAAAAAAAAAAAAAAAAAAAAAADAKBggqhkjOPQQDAjAP
-----END CERTIFICATE-----"""


def test_pasting_a_certificate_is_the_recommended_path_not_turning_verification_off(
        client, unconfigured):
    """內部服務常用自簽憑證。**正解是信任那張憑證，不是關掉驗證** ——
    關掉之後任何人都可以冒充對方，而我們每次送件都把 API 金鑰交出去。

    判準是「`httpx` 真的會拿到那個檔案路徑」，不是「設定存起來了」——
    存了但沒接上去的話，連線照樣走系統信任庫而且**完全看不出來**。
    """
    from app.core import jtlw_settings as js
    import importlib
    mt = importlib.import_module("app.tools.meeting_transcribe.router")  # noqa: F401

    # 貼一張真的憑證（用測試自己產的，不要依賴外部檔案）
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    import datetime as _dt
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, "jtlw-test")])
    cert = (x509.CertificateBuilder()
            .subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=1))
            .not_valid_after(_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(days=30))
            .sign(key, hashes.SHA256()))
    pem = cert.public_bytes(serialization.Encoding.PEM).decode("ascii")

    js.save({"ca_cert_pem": pem})
    js.invalidate_cache()

    verify = js.verify_tls()
    assert isinstance(verify, str), f"憑證沒接到 httpx 的 verify 上：{verify!r}"
    assert pathlib.Path(verify).is_file(), "憑證檔沒有落到磁碟上"

    # **指紋要算得出來** —— 管理員得拿它跟對方公布的值核對才敢信任
    fp = js.ca_fingerprint()
    import hashlib
    want = hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest().upper()
    assert fp.replace(":", "") == want, f"指紋算錯了：{fp}"

    # 清掉之後要回到系統信任庫，而且**檔案要跟著消失**
    js.save({"ca_cert_pem": ""})
    js.invalidate_cache()
    assert js.verify_tls() is True
    assert not pathlib.Path(verify).exists(), "清掉設定了，憑證檔卻還留在磁碟上"


def test_a_pasted_non_certificate_is_rejected_at_save_time(client, unconfigured):
    """貼錯的話要**當下**就說 —— 不然連線會在送件那一刻失敗，
    而訊息是 ssl 的內部錯誤，看不出是設定頁貼壞了。"""
    r = client.post("/admin/api/jtlw/settings", json={"ca_cert_pem": "這不是憑證"})
    assert r.status_code == 400
    assert "PEM" in r.text


def test_the_speaker_count_field_asks_who_speaks_not_who_attends():
    """**「與會人數」是會讓結果變差的問法** —— 而且兩個方向都有代價。

    語音服務在完整 37 分鐘的 7 人中文會議上量過（2026-09-22 修正後的版本）：

    | 設定 | 發言者搞錯 | 分出的發言者 |
    |---|---:|---:|
    | 不指定 | 22.90% | 3 / 7 |
    | **指定 7 人（正確答案）** | **30.15%** | 7 / 7 |
    | 指定 5~6 人 | **17.8%** | 5~6 / 7 |

    那 7 個人裡有 3 位分別只講了 28.6 / 56.2 / 94.8 秒 ——
    **聲紋不足以成群的人硬湊一群給他，代價是把主要發言者拆散**
    （最大的兩位正確率 89.8% / 93.2% → 56.5% / 55.8%）。

    **但「不填」也有代價，而且是我們最在意的那一種**：不指定時
    **4/7 位發言者在輸出裡完全不存在**，他們說的每一句都掛在別人名下 ——
    對引用機制來說那不是「少了幾秒」，是「有一條決議、標了一個錯的人」。

    所以問的是「**發言量足以辨認**的人數」，不是「有出過聲的人數」，
    也不是「與會人數」。（語音服務 v1.9 原本說「會發言的人數」，
    v2.2 自己修正成這一版 —— 講一兩句的人算「會發言」，但不該填進去。）

    **⚠ 19.91% → 26.57% 那組舊數字不要再引用**：那是在 3 分鐘節錄上、
    用他們後來修掉的版本量的（2026-09-22 他們主動更正）。
    **結論沒有翻轉**，所以問法維持；變的是「怎麼問才夠精確」。

    **這一條值得用字面守門**：把它改回「與會人數」不會有任何測試變紅，
    而結果會**系統性地變差**，使用者也不會知道為什麼
    （同 `OPS.md` 的 IIS 安裝順序 —— 照著做的人才會踩到）。
    """
    import pathlib as _p
    src = _p.Path("app/tools/meeting_transcribe/templates/meeting_transcribe.html").read_text(
        encoding="utf-8")
    # Jinja 註解裡會引用那個錯誤寫法當反例（use vs mention），先去掉
    import re
    body = re.sub(r"\{#.*?#\}", "", src, flags=re.S)

    assert "會發言的人數" in body, "欄位標題要問「會發言的人數」"
    assert "與會人數" not in body, (
        "欄位標題寫成「與會人數」了 —— 填進不發言的人會讓發言者分辨更差")
    assert "不確定就留 0" in body, "要明講不確定就留 0（系統自己判通常比錯誤的提示好）"
    assert "發言量足以辨認" in body, (
        "說明要講出「發言量足以辨認」—— 只說「會發言」不夠，"
        "講一兩句的人也算會發言，填進去反而會把主要發言者拆散")
    assert "只講一兩句" in body, "要給一個管理員估得出來的界線"
    assert "填了會比較準" not in body, (
        "舊的說明寫著「知道確切人數時填了會比較準」—— 那句話已經被量測推翻了")


# ------------------------------------------- 金鑰要怎麼貼（對方 2026-09-22 指出）

@pytest.mark.parametrize("pasted", [
    "jtlw_jtdt_ABC123",                          # 正常
    "Bearer jtlw_jtdt_ABC123",                   # 照文件複製時最容易發生的
    "bearer  jtlw_jtdt_ABC123",                  # 大小寫與多餘空白
    "Authorization: Bearer jtlw_jtdt_ABC123",    # 整條標頭貼進來
    '"Authorization: Bearer jtlw_jtdt_ABC123"',  # 連 curl 的引號一起貼
    "  jtlw_jtdt_ABC123  ",
])
def test_a_pasted_authorization_prefix_still_leaves_a_working_key(client, unconfigured, pasted):
    """**`Authorization: Bearer <金鑰>` 是我們自己組的，欄位裡只要金鑰。**

    對方的文件寫的是整條標頭，照著複製很自然會連前綴一起貼進來。
    2026-09-22 拿對方正式 API 實測三種貼法：裸金鑰 200、多一層 `Bearer` 401、
    整條標頭 401 —— 而 401 的訊息是「缺少或無效的 API Key」，
    **指向金鑰本身，可是金鑰是對的**。

    所以在存檔那一步收掉前綴（`jtlw_settings.normalise_api_key`）。
    """
    client.post("/admin/api/jtlw/settings", json={"api_key_enc": pasted})
    assert js.api_key() == "jtlw_jtdt_ABC123"


@pytest.mark.parametrize("pasted", ["bearerish_key", "BearerKeyWithoutSpace", "authorization_key"])
def test_a_key_that_merely_starts_with_those_letters_is_left_alone(client, unconfigured, pasted):
    """**反向對照**：只在「`bearer` ＋ 空白」或「`authorization:`」時才剝。
    沒有這一條的話，把剝除寫成 `lstrip('bearer')` 之類也會全綠，
    而那會把真的金鑰吃掉一截 —— 症狀同樣是 401。"""
    client.post("/admin/api/jtlw/settings", json={"api_key_enc": pasted})
    assert js.api_key() == pasted


def test_we_send_the_bearer_header_ourselves(client, unconfigured):
    """驗**送出去的那一條標頭**，不是驗我們存了什麼 ——
    存對了但組錯標頭的話，畫面上完全看不出來（送件才 401）。"""
    with FakeJtlw() as fake:
        js.save({"enabled": True, "base_url": fake.base,
                 "api_key_enc": "Bearer jtlw_jtdt_ABC123",   # 貼錯的那一種
                 "audio_base_url": "http://audio.test:8765"})
        js.invalidate_cache()
        _run(client, _upload(client)["upload_id"])
    assert fake.seen["auth"] == "Bearer jtlw_jtdt_ABC123", (
        "送出去的必須剛好是一層 `Bearer ` ＋ 金鑰本身")


def test_the_key_field_tells_you_what_to_paste():
    """**範例文字要跟著「有沒有存過」走。**

    「留空＝不更動」只有在**已經存過**的時候才說得通，而它原本是無條件的
    placeholder —— 存過之後欄位值是 `__KEPT__`（不是空的），提示永遠被蓋住；
    **沒存過時它卻會出現，而那時候沒有東西可以「不更動」**。
    對方 2026-09-22 看到截圖指出這段文字不對。
    """
    import pathlib as _p
    import re as _re
    src = _p.Path("app/admin/templates/admin_jtlw.html").read_text(encoding="utf-8")
    body = _re.sub(r"\{#.*?#\}", "", src, flags=_re.S)      # 註解裡會引用反例

    row = body.split('id="jl-key"', 1)[1].split(">", 1)[0]
    assert "{% if s.api_key_enc %}" in row, (
        "「留空＝不更動」要在存過金鑰時才出現")
    assert "只貼金鑰本身" in body, "要講出欄位裡只放金鑰本身"
    assert "Authorization: Bearer" in body, "要講出那條標頭是我們自己組的"


def test_a_rejection_at_submit_time_says_what_to_do(client, unconfigured):
    """**送件當下被拒絕走的是例外那條路，不是終態的 `errors[]`。**

    對方 v1.7 起把「拉錄音檔」提前到送件當下，所以來源相關的錯誤
    （`source_not_allowed` / `source_unreachable` …）多半在這裡就回來了。

    2026-09-22 正式機實際踩到：畫面上是
    `jtlw 拒絕了這個請求：source_not_allowed（欄位 source.url）` ——
    **而對照表裡明明就有「請管理員把位址加進允許清單」**，
    只是那份表只有另一條路在用。措辭現在收在
    `jtlw_client.describe_error` 一處，兩條路共用。
    """
    with FakeJtlw(reject_with={"code": "source_not_allowed", "category": "source",
                               "retryable": False,
                               "details": {"field": "source.url"}}) as fake:
        _configure(fake)
        j = _run(client, _upload(client)["upload_id"])
    assert j["status"] == "error", j
    assert "允許清單" in (j.get("error") or ""), (
        f"訊息沒有講出下一步：{j.get('error')!r}")
    assert "source_not_allowed" not in (j.get("error") or ""), (
        "原始錯誤碼直接丟給使用者了 —— 他看不懂，也不知道要找誰")


def test_both_paths_share_one_wording_table():
    """**一份措辭**：送件被拒（例外）與終態失敗（`errors[]`）不可以各寫一份。

    只驗行為的話，有人在 `_raise_for` 裡再寫一份也會全綠 ——
    所以這裡直接看原始碼有沒有委派過去。
    """
    import ast, pathlib as _p
    cli = _p.Path("app/core/jtlw_client.py").read_text(encoding="utf-8")
    rtr = _p.Path("app/tools/meeting_transcribe/router.py").read_text(encoding="utf-8")

    def _calls(src: str, func: str) -> set[str]:
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == func:
                out = set()
                for n in ast.walk(node):
                    if isinstance(n, ast.Call):
                        f = n.func
                        out.add(f.attr if isinstance(f, ast.Attribute)
                                else getattr(f, "id", ""))
                return out
        raise AssertionError(f"找不到 {func} —— 判準要跟著改名")

    assert "describe_error" in _calls(cli, "_raise_for"), (
        "`_raise_for` 自己組了錯誤訊息 —— 要走 `describe_error`")
    assert "describe_error" in _calls(rtr, "_failure_text"), (
        "`_failure_text` 自己組了錯誤訊息 —— 要走 `jtlw_client.describe_error`")
    assert "_ERROR_TEXT" not in rtr, (
        "工具裡又有一份錯誤碼對照表 —— 一份就好（`jtlw_client.ERROR_TEXT`）")


# ------------------------------------------------- 時間不可以在交接時掉到地上

def test_the_saved_transcript_round_trips_into_the_summary_tool():
    """**轉送「會議摘要」時時間要活著。**

    2026-09-22 使用者回報「出來的沒有時間資訊」。語音服務判斷是我們只取了
    `final` 層 —— **但那是錯的**：正式機上存下來的 258 段**每一段都有**
    `start_ms` / `end_ms` / `speaker`，三層的 join 一直是對的。

    掉時間的地方在**交接**：轉送時把逐字稿攤平成純文字，而純文字裡沒有時間，
    於是「會議摘要」那邊的發言者佔比與章節時間軸就變成「這份逐字稿沒有時間戳記」。

    所以改送 JSON。這條驗的是那份 JSON **真的被對面讀得回來** ——
    只驗「有送 JSON」的話，格式不合也會過。
    """
    from app.core import transcript_parse as tp
    saved = {
        "source": {"filename": "會議.m4a"},
        "segments": [
            {"seq": 1, "text": "第一句", "speaker": "S1", "start_ms": 1450, "end_ms": 2650},
            {"seq": 2, "text": "第二句", "speaker": "S2", "start_ms": 6270, "end_ms": 7470},
        ],
    }
    segs = tp.parse_json(json.dumps(saved, ensure_ascii=False).encode("utf-8"))
    assert len(segs) == 2, segs
    assert [s.get("start_ms") for s in segs] == [1450, 6270], "時間掉了"
    assert [s.get("speaker") for s in segs] == ["S1", "S2"], "發言者掉了"


def test_the_handoff_sends_json_not_flattened_text():
    """**判準放在「送出去的是什麼」**。

    只驗 `parse_json` 讀得回來是不夠的 —— 那證明不了我們真的送 JSON
    （攤平成純文字一樣會「成功」，只是對面看不到時間）。
    """
    import pathlib as _p
    import re as _re
    src = _p.Path("app/tools/meeting_transcribe/templates/meeting_transcribe.html").read_text(
        encoding="utf-8")
    body = _re.sub(r"\{#.*?#\}", "", src, flags=_re.S)
    i = body.index("mtToSummary')")
    block = body[i:i + 1200]
    assert "JSON.stringify(data)" in block, (
        "轉送「會議摘要」還是送攤平過的純文字 —— 時間會在這裡掉光")
    assert "application/json" in block and ".json'" in block, (
        "送出去的 blob 要標成 JSON、檔名也要是 .json，不然對面會照純文字解析")


def test_plain_text_copy_keeps_the_timestamps():
    """**複製出去的純文字也要帶時間。**

    我們自己的 `transcript_parse.parse_plain` 讀得懂行首的 `[mm:ss]` ——
    丟掉等於自廢武功（使用者把它貼進「會議摘要」時就沒有時間軸了）。
    """
    import pathlib as _p
    import re as _re
    src = _p.Path("app/tools/meeting_transcribe/templates/meeting_transcribe.html").read_text(
        encoding="utf-8")
    body = _re.sub(r"\{#.*?#\}", "", src, flags=_re.S)
    i = body.index("function asPlainText()")
    block = body[i:i + 500]
    assert "mmss(s.start_ms)" in block, "純文字沒有帶時間"

    # 而且真的解析得回來（格式對不對只有這樣才驗得到）
    #
    # **素材要讓發言者重複出現**：`parse_plain` 判斷「這是發言者還是句子」的方式是
    # 「同一個名字有沒有再出現」，所以每個人只講一句的逐字稿會被判成沒有發言者。
    # 那是啟發式的邊角，不是這條要驗的東西（真的會議一定會重複）。
    from app.core import transcript_parse as tp
    segs, used = tp.parse_plain(
        "[00:01] S1：第一句\n[00:12] S2：第二句\n[00:20] S1：第三句")
    assert used == "inline", used
    assert segs[0].get("start_ms") == 1000, segs
    assert [s.get("speaker") for s in segs] == ["S1", "S2", "S1"], segs


# --------------------------------------------- 播放、改名（v1.16.1 的新功能）

def test_the_audio_is_served_back_for_playback_with_range(client, unconfigured):
    """**播放走 `upload_owner`，不是簽章網址。**

    簽章那條是給**對方的伺服器**拉檔用的（短效、免登入）；這一條是給
    已登入的本人在畫面上播放。混用的話，要嘛把短效網址塞進頁面（會過期），
    要嘛把播放網址做成永久公開（那就漏了）。

    Range 也要能用 —— 不然拖進度列會整檔重拉。
    """
    with FakeJtlw() as fake:
        _configure(fake)                      # 上傳端點在沒設定時是 503
        uid = _upload(client)["upload_id"]
    r = client.get(f"/tools/meeting-transcribe/audio/{uid}")
    assert r.status_code == 200, r.text
    assert r.headers.get("accept-ranges") == "bytes"

    # **HEAD 也要答**。`@router.get` 不會自動加（Starlette 的 `Route` 會，
    # FastAPI 不會）—— 回 405 的話，任何「檔案還在嗎」的探測都會失敗，
    # 而失敗在畫面上跟「沒有錄音檔」長得一模一樣（2026-09-22 踩到：
    # 播放器與波形整組不出現，沒有任何錯誤）。
    assert client.request("HEAD", f"/tools/meeting-transcribe/audio/{uid}"
                          ).status_code == 200, "HEAD 沒被接受"

    part = client.get(f"/tools/meeting-transcribe/audio/{uid}",
                      headers={"Range": "bytes=0-9"})
    assert part.status_code == 206 and len(part.content) == 10

    assert client.get("/tools/meeting-transcribe/audio/not-a-uuid").status_code in (400, 404)


def test_renaming_a_speaker_defaults_to_all_of_them(client, unconfigured):
    """**改一個名字，同一位全部跟著改** —— 一場會議裡同一個人被標幾百次，
    一段一段改沒有意義。但辨識偶爾會把某一段掛錯人，所以**單段**也要能改。

    存回逐字稿本身（不另開一個檔）：下載、複製、轉送「會議摘要」
    走的都是同一份資料，分兩個地方存一定會漂。
    """
    with FakeJtlw() as fake:
        _configure(fake)
        uid = _upload(client)["upload_id"]
        _run(client, uid)

    base = f"/tools/meeting-transcribe/speakers/{uid}"
    r = client.post(base, json={"map": {"S1": "Jason"}, "overrides": {"3": "另一位"}})
    assert r.status_code == 200, r.text

    got = client.get(f"/tools/meeting-transcribe/result/{uid}").json()
    assert got["speaker_names"] == {"S1": "Jason"}
    assert got["speaker_overrides"] == {"3": "另一位"}
    assert got["segments"], "改名字把逐字稿弄不見了"


def test_a_speaker_name_cannot_smuggle_control_characters(client, unconfigured):
    """名字會被寫進逐字稿、下載的檔案與轉送給「會議摘要」的內容 ——
    換行會把一段拆成兩段（而我們自己的純文字剖析器是**逐行**讀的）。"""
    with FakeJtlw() as fake:
        _configure(fake)
        uid = _upload(client)["upload_id"]
        _run(client, uid)

    client.post(f"/tools/meeting-transcribe/speakers/{uid}",
                json={"map": {"S1": "壞\n人\r\x00", "S2": "x" * 200}})
    got = client.get(f"/tools/meeting-transcribe/result/{uid}").json()
    assert "\n" not in got["speaker_names"]["S1"] and "\r" not in got["speaker_names"]["S1"]
    assert "\x00" not in got["speaker_names"]["S1"]
    assert len(got["speaker_names"]["S2"]) <= 40, "名字沒有長度上限"
