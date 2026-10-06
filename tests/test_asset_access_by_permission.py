"""資產庫的圖照工具權限給（GitHub issue #54，2026-10-05）。

一般使用者預設沒有「用印與簽名」權限，卻能在 PDF 編輯器的「套印 / 簽名」挑到資產庫裡的
公司印章並蓋進 PDF。只把清單藏起來不夠，三個地方都要擋：

1. 編輯器的資產清單：沒有用印權限的人看不到印章與簽名（Logo 照舊），並且說得出原因；
2. 編輯器存檔：自己組請求帶印章的 id 也蓋不上去（403，不會產出半成品）；
3. 圖檔網址 `/assets/{id}/file|thumb`：拿不到印章的 PNG（不然下載下來再用「上傳新圖片」貼回去）。

另外：有權限的人在編輯器裡蓋資產庫的印章或簽名，跟用印工具一樣寫進用印簽名歷史
（手動存檔才寫、內容沒變不重複寫）。認證關閉時一律照舊。
"""
from __future__ import annotations

from io import BytesIO

import fitz
import pytest
from fastapi.testclient import TestClient

import app.main as app_main
from app.core.asset_manager import asset_manager
from app.core.history_manager import stamp_history

TYPES = ("stamp", "signature", "logo", "watermark")


def _png() -> bytes:
    from PIL import Image, ImageDraw
    im = Image.new("RGBA", (120, 120), (255, 255, 255, 0))
    ImageDraw.Draw(im).ellipse((5, 5, 115, 115), outline=(200, 0, 0, 255), width=8)
    buf = BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def _pdf() -> bytes:
    d = fitz.open()
    d.new_page(width=595, height=842).insert_text((72, 100), "Doc", fontsize=20)
    buf = BytesIO()
    d.save(buf)
    d.close()
    return buf.getvalue()


@pytest.fixture()
def assets():
    made = {t: asset_manager.create_from_bytes(f"權限測試 {t}", t, _png()) for t in TYPES}
    yield {t: a.id for t, a in made.items()}
    for a in made.values():
        asset_manager.delete(a.id)


def _client_for(username: str, role: str | None) -> TestClient:
    from app.core import user_manager, sessions, permissions
    uid = user_manager.create_local(username, username, "UserPass1234")
    if role:
        permissions.set_subject_roles("user", str(uid), [role])
    tok, _ = sessions.issue(uid, remember=False, ip="127.0.0.1", ua="pytest")
    c = TestClient(app_main.app)
    c.cookies.set(sessions.COOKIE_NAME, tok)
    return c


@pytest.fixture()
def people(admin_session):
    """一般使用者（沒有用印）、財務（有用印）、文管（連浮水印都沒有）、管理員。"""
    admin = admin_session[0] if isinstance(admin_session, tuple) else admin_session
    return {
        "plain": _client_for("plain-user", None),
        "finance": _client_for("finance-user", "finance"),
        "clerk": _client_for("clerk-user", "clerk"),
        "admin": admin,
    }


# ---------- 3. 圖檔網址 ----------

@pytest.mark.parametrize("suffix", ["file", "thumb"])
def test_asset_images_follow_the_tool_permission(people, assets, suffix):
    want = {
        # 誰 → 每種資產的回應碼
        "plain":   {"stamp": 403, "signature": 403, "logo": 200, "watermark": 200},
        "finance": {"stamp": 200, "signature": 200, "logo": 200, "watermark": 200},
        "clerk":   {"stamp": 403, "signature": 403, "logo": 200, "watermark": 403},
        "admin":   {"stamp": 200, "signature": 200, "logo": 200, "watermark": 200},
    }
    got = {who: {t: people[who].get(f"/assets/{assets[t]}/{suffix}",
                                    follow_redirects=False).status_code
                 for t in TYPES} for who in want}
    assert got == want, f"/assets/…/{suffix} 的回應跟權限對不上：{got}"


def test_a_denied_image_says_why(people, assets):
    r = people["plain"].get(f"/assets/{assets['stamp']}/file", follow_redirects=False)
    assert r.status_code == 403
    assert "用印與簽名" in r.text, f"擋下時要講出要什麼權限：{r.text}"


@pytest.mark.parametrize("t", TYPES)
def test_auth_off_everyone_still_sees_every_asset(auth_off, assets, t):
    c = TestClient(app_main.app)
    for suffix in ("file", "thumb"):
        assert c.get(f"/assets/{assets[t]}/{suffix}").status_code == 200


# ---------- 1. 編輯器的資產清單 ----------

def _listed(client, assets) -> tuple[set[str], bool]:
    r = client.get("/tools/pdf-editor/assets")
    assert r.status_code == 200, r.text
    data = r.json()
    mine = {t for t, aid in assets.items() if aid in {a["id"] for a in data["assets"]}}
    return mine, data.get("restricted", False)


def test_the_editor_lists_stamps_only_to_people_who_may_stamp(people, assets):
    assert _listed(people["plain"], assets) == ({"logo"}, True), (
        "沒有用印權限的人只該看到 Logo，而且要標出有東西被藏起來")
    assert _listed(people["finance"], assets) == ({"stamp", "signature", "logo"}, False)
    assert _listed(people["admin"], assets) == ({"stamp", "signature", "logo"}, False)


def test_auth_off_editor_lists_everything(auth_off, assets):
    c = TestClient(app_main.app)
    assert _listed(c, assets) == ({"stamp", "signature", "logo"}, False)


# ---------- 2. 編輯器存檔 ----------

def _upload(client) -> str:
    r = client.post("/tools/pdf-editor/load",
                    files={"file": ("doc.pdf", _pdf(), "application/pdf")})
    assert r.status_code == 200, r.text
    return r.json()["upload_id"]


def _save(client, uid: str, asset_id: str, x: float = 100, auto: bool = False):
    body = {"upload_id": uid, "is_auto": auto,
            "pages": [{"page": 0, "objects": [
                {"id": "o1", "type": "image", "asset_id": asset_id,
                 "x": x, "y": 200, "w": 60, "h": 60}]}]}
    return client.post("/tools/pdf-editor/save", json=body)


def _red(pdf_path) -> int:
    with fitz.open(str(pdf_path)) as d:
        pm = d[0].get_pixmap(dpi=50)
    px = pm.samples
    return sum(1 for j in range(0, len(px), pm.n)
               if px[j] > 150 and px[j + 1] < 100 and px[j + 2] < 100)


def _out(uid: str):
    from app.tools.pdf_editor.router import _work_dir
    return _work_dir() / f"pe_{uid}_out.pdf"


@pytest.fixture()
def history_cleanup():
    before = {m["id"] for m in stamp_history.list_all()}
    yield before
    for m in stamp_history.list_all():
        if m["id"] not in before:
            stamp_history.delete(m["id"])


def test_saving_a_library_stamp_without_permission_is_refused(people, assets, history_cleanup):
    c = people["plain"]
    for t in ("stamp", "signature"):
        uid = _upload(c)
        r = _save(c, uid, assets[t])
        assert r.status_code == 403, f"沒有用印權限卻蓋得上資產庫的{t}：{r.status_code}"
        assert not _out(uid).exists(), "被擋下的存檔不可以產出檔案"
    uid = _upload(c)
    r = _save(c, uid, assets["logo"])
    assert r.status_code == 200, f"Logo 不限權限：{r.text}"
    assert _red(_out(uid)) > 20, "Logo 沒有蓋上去"


def test_the_editor_does_not_take_watermark_assets(people, assets, history_cleanup):
    c = people["finance"]
    uid = _upload(c)
    assert _save(c, uid, assets["watermark"]).status_code == 400


def test_stamping_in_the_editor_is_recorded_once_per_version(people, assets, history_cleanup):
    before = history_cleanup
    c = people["finance"]
    uid = _upload(c)

    def new_entries():
        return [m for m in stamp_history.list_all() if m["id"] not in before]

    r = _save(c, uid, assets["stamp"], auto=True)
    assert r.status_code == 200, r.text
    assert _red(_out(uid)) > 20, "有權限卻沒有蓋上章"
    assert new_entries() == [], "自動存檔不該寫進用印歷史（每拖一下就一次）"

    assert _save(c, uid, assets["stamp"]).status_code == 200
    got = new_entries()
    assert len(got) == 1, f"手動存檔蓋了資產庫的章，用印歷史應該多一筆：{len(got)}"
    m = got[0]
    assert m["filename"] == "doc.pdf", m
    assert (m.get("extra") or {}).get("source") == "editor", m
    assert assets["stamp"] in (m.get("extra") or {}).get("asset_ids", []), m
    assert m["username"].startswith("finance-user"), m
    out = stamp_history.file(m["id"], "filled")
    assert out and _red(out) > 20, "歷史裡的成品沒有章"

    assert _save(c, uid, assets["stamp"]).status_code == 200
    assert len(new_entries()) == 1, "內容沒變又按一次儲存，不該多一筆"

    assert _save(c, uid, assets["stamp"], x=300).status_code == 200
    assert len(new_entries()) == 2, "改了位置再存，要記下新的那一版"


def test_a_logo_alone_is_not_a_stamp(people, assets, history_cleanup):
    before = history_cleanup
    c = people["finance"]
    uid = _upload(c)
    assert _save(c, uid, assets["logo"]).status_code == 200
    assert not [m for m in stamp_history.list_all() if m["id"] not in before], (
        "只放 Logo 不是用印，不寫用印歷史")


# ---------- 騎縫章只收印章 ----------

def _two_page_pdf() -> bytes:
    d = fitz.open()
    for _ in range(2):
        d.new_page(width=595, height=842).insert_text((72, 100), "Doc", fontsize=20)
    buf = BytesIO()
    d.save(buf)
    d.close()
    return buf.getvalue()


@pytest.mark.parametrize("t,ok", [("stamp", True), ("signature", False),
                                  ("watermark", False), ("logo", False)])
def test_the_seam_stamp_only_takes_stamps(auth_off, assets, t, ok):
    """畫面上只列印章；簽名要有用印權限才能用，所以只有騎縫章權限的人
    送簽名的編號進來也不可以蓋成騎縫章（判斷看資產種類，認證開關都一樣）。"""
    c = TestClient(app_main.app)
    r = c.post("/tools/pdf-seam-stamp/load",
               files={"file": ("doc.pdf", _two_page_pdf(), "application/pdf")})
    assert r.status_code == 200, r.text
    uid = r.json()["upload_id"]
    r = c.post("/tools/pdf-seam-stamp/stamp-preview",
               data={"upload_id": uid, "source": "asset", "asset_id": assets[t]})
    if ok:
        assert r.status_code == 200, r.text
        assert r.headers["content-type"] == "image/png"
    else:
        assert r.status_code == 400, f"騎縫章收了{t}：{r.status_code}"
