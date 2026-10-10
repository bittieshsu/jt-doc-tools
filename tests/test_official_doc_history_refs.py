"""公文撰擬「參考歷史案件」（2026-10-09 使用者：「加入一個勾選 歷史案件 這樣可以從之前的也做查詢或處理
／注意 自己只能查自己的案件」）。

要守的：

* **只查得到自己的案件**：別人的案件內容一個字都不可以進提示、進參考資料 —— 管理員也一樣
  （管理員在歷史案件清單看得到每個人的，那是管理；拿別人的公文當自己草稿的參考不行）。
  認不出是誰（啟用認證但沒有使用者）就一件都沒有，不可以退回「全部」。
* 已刪除的、還沒有草稿的、這一件本身（重新產生時）不算。
* 只有相近的才帶進來（門檻是拿內建範例量的）；帶進來的是那件案件**最新一版**的全文。
* 歷史案件**永遠不算草稿的依據**（去年的金額搬到今年正是要擋的事）；只有知識庫的資料時，
  提示跟加這個功能之前一字不差。
* 勾選框只在這個人有自己的案件時出現。
"""
from __future__ import annotations

import json
import time
import uuid

import pytest

from tests.test_official_doc_tool import (BASE, _run, _user_client, _wait_job,  # noqa: F401
                                          fake_llm)

#: 同一件事的兩種寫法（量過：互相比對 0.4 以上）與一件不相干的
OLD = "我們單位有120台電腦，端點防護軟體的授權今年11月30日到期，想採購一年期的端點防護軟體授權，要能集中管理跟偵測勒索軟體，幫我寫採購簽。"
NEW = "端點防護軟體的授權明年又要到期了，一樣要採購一年期授權，大概120台電腦，要能集中管理跟偵測勒索軟體，幫我寫採購簽。"
UNRELATED = "檔案室天花板漏水，想先請廠商來勘查並估價，看要怎麼修，幫我寫一份簽。"


def _cs():
    from app.core import official_doc_cases
    return official_doc_cases


@pytest.fixture
def cases_root(tmp_path, monkeypatch):
    """核心測試用的案件目錄（不跟其他測試建的案件混在一起）。"""
    root = tmp_path / "cases"
    monkeypatch.setattr(_cs(), "root", lambda: root)
    return root


def _make(owner, narrative, *, text="主旨：舊的草稿。", deleted=False, result=True, revisions=None,
          mode="sign", title="", updated=None) -> str:
    cs = _cs()
    cid = uuid.uuid4().hex
    cs.create(cid, owner_uid=owner, mode=mode)
    cs.ensure(cid)
    inputs = {"mode": mode, "narrative": narrative}
    cs.file(cid, "case.json").write_text(json.dumps({"inputs": inputs}, ensure_ascii=False), "utf-8")
    if result:
        cs.file(cid, "result.json").write_text(
            json.dumps({"inputs": inputs, "draft": {"text": text}}, ensure_ascii=False), "utf-8")
    if revisions is not None:
        cs.file(cid, "revisions.json").write_text(
            json.dumps({"revisions": revisions}, ensure_ascii=False), "utf-8")
    fields = {"title": title}
    if deleted:
        fields["deleted_at"] = time.time()
    cs.update(cid, **fields)
    if updated is not None:
        meta = json.loads(cs.file(cid, "meta.json").read_text("utf-8"))
        meta["updated_at"] = updated
        cs.file(cid, "meta.json").write_text(json.dumps(meta), "utf-8")
    return cid


def _router():
    # `from app.tools.official_doc import router` 拿到的是 APIRouter 物件（套件的 __init__ 把同名子模組遮住）
    import importlib
    return importlib.import_module("app.tools.official_doc.router")


def _bg():
    return _router()._history_background()


# ------------------------------------------------------------------ 核心：誰的、哪幾件

def test_only_the_owners_cases_are_found(cases_root):
    mine = _make(1001, OLD, text="主旨：我的舊草稿。")
    theirs = _make(1002, OLD, text="主旨：別人的舊草稿。")
    got = _cs().search_own(NEW, owner_uid=1001, auth_on=True, background=_bg())
    assert [h["case_id"] for h in got] == [mine], got
    assert all("別人" not in h["text"] for h in got)
    got = _cs().search_own(NEW, owner_uid=1002, auth_on=True, background=_bg())
    assert [h["case_id"] for h in got] == [theirs]
    # 沒有案件的人：什麼都沒有
    assert _cs().search_own(NEW, owner_uid=1003, auth_on=True, background=_bg()) == []


def test_an_unknown_user_gets_nothing_not_everything(cases_root):
    _make(1001, OLD)
    _make(None, OLD)
    assert _cs().search_own(NEW, owner_uid=None, auth_on=True, background=_bg()) == [], \
        "啟用認證卻認不出是誰：不可以退回看得到全部（也不可以拿擁有者是空的那些）"
    assert _cs().has_own(None, True) is False


def test_single_user_mode_only_sees_cases_without_an_owner(cases_root):
    """認證關閉：只看擁有者是空的（單人模式建的）；曾經啟用認證時建的那些有擁有者，是別人的。"""
    solo = _make(None, OLD)
    _make(1001, OLD)
    got = _cs().search_own(NEW, owner_uid=None, auth_on=False, background=_bg())
    assert [h["case_id"] for h in got] == [solo]
    assert _cs().search_own(NEW, owner_uid=1001, auth_on=False, background=_bg())[0]["case_id"] == solo


def test_deleted_draftless_and_the_case_itself_are_skipped(cases_root):
    _make(1001, OLD, deleted=True)
    _make(1001, OLD, result=False)
    me = _make(1001, OLD)
    assert _cs().search_own(NEW, owner_uid=1001, auth_on=True, background=_bg(),
                            exclude=me) == []
    assert [h["case_id"] for h in _cs().search_own(NEW, owner_uid=1001, auth_on=True,
                                                   background=_bg())] == [me]
    assert _cs().has_own(1001, True) is True
    assert _cs().has_own(1002, True) is False
    # 只有還沒產生草稿的案件：勾選框不出現（勾了也查不到）
    _make(1004, OLD, result=False)
    assert _cs().has_own(1004, True) is False


def test_only_similar_cases_come_back_and_the_latest_version_is_used(cases_root):
    near = _make(1001, OLD, text="主旨：模型產生的那一版。", revisions=[
        {"rev": 1, "text": "主旨：模型產生的那一版。"},
        {"rev": 3, "text": "主旨：承辦人改過、存過的那一版。"}])
    _make(1001, UNRELATED)
    got = _cs().search_own(NEW, owner_uid=1001, auth_on=True, background=_bg())
    assert [h["case_id"] for h in got] == [near], [(h["case_id"], h["score"]) for h in got]
    assert got[0]["text"] == "主旨：承辦人改過、存過的那一版。" and got[0]["rev"] == 3
    assert got[0]["score"] >= _cs().HISTORY_MIN_SCORE


def test_at_most_three_and_newest_first_on_ties(cases_root):
    ids = [_make(1001, OLD, updated=1_000_000 + i) for i in range(5)]
    got = _cs().search_own(OLD, owner_uid=1001, auth_on=True, background=_bg())
    assert len(got) == _cs().HISTORY_MAX == 3
    assert [h["case_id"] for h in got] == list(reversed(ids))[:3]


def test_the_directory_name_is_the_case_id_not_what_meta_says(cases_root):
    """`meta.json` 裡的 `case_id` 不算數 —— 不可以讓它指到別人的目錄。"""
    cid = _make(1001, OLD)
    other = _make(1002, OLD)
    p = _cs().file(cid, "meta.json")
    meta = json.loads(p.read_text("utf-8"))
    meta["case_id"] = other
    p.write_text(json.dumps(meta), "utf-8")
    got = _cs().search_own(NEW, owner_uid=1001, auth_on=True, background=_bg())
    assert [h["case_id"] for h in got] == [cid]


def test_the_threshold_separates_the_built_in_examples():
    """門檻的依據：內建範例兩兩之間（不同的事）都比門檻低，同一件事換寫法比門檻高。"""
    import itertools
    from app.core import cjk_fts
    from app.tools.official_doc import examples
    cs = _cs()
    bg = _bg()
    pool = [cjk_fts.compact(b) for b in bg]
    n = len(pool)
    worst = 0.0
    for a, b in itertools.combinations(range(len(bg)), 2):
        terms = cjk_fts.query_terms(bg[a][:cs.HISTORY_MAX_QUERY])
        w = {t: cs._idf(sum(1 for c in pool if t in c), n) for t in terms}
        worst = max(worst, cjk_fts.coverage(terms, pool[b], w))
    assert len(examples.EXAMPLES) >= 50 and worst < cs.HISTORY_MIN_SCORE, worst


# ------------------------------------------------------------------ 核心：參考資料與提示

def test_past_cases_are_never_a_basis_and_kb_only_prompts_are_unchanged():
    from app.core import official_doc as od
    kb_only = od.normalise_references([
        {"purpose": "substantive_basis", "title": "某法", "text": "第一條 內容"},
        {"purpose": "style_example", "title": "範例", "text": "主旨：範例。"}])
    block = od.references_block(kb_only)
    assert "機關知識庫裡檢索到的" in block and "歷史案件" not in block
    refs = od.normalise_references([
        {"purpose": "past_case", "title": "去年的採購簽", "text": "主旨：採購新臺幣24萬元。",
         "case_id": "a" * 32, "case_rev": 2, "case_updated": 1.7e9}])
    assert refs[0]["purpose"] == "past_case" and refs[0]["case_id"] == "a" * 32
    assert od.reference_sources(refs) == [], "歷史案件不可以算依據"
    block = od.references_block(refs)
    assert "〔歷史案件〕" in block and "承辦人自己先前寫過的案件" in block
    assert "這次的需求沒寫的不可以從歷史案件搬過來" in block
    # 畫面會組成網址的編號：只收 32 碼十六進位
    bad = od.normalise_references([{"purpose": "past_case", "title": "x", "text": "y",
                                    "case_id": "../../etc"}])
    assert bad[0]["case_id"] == ""


def test_kb_gives_way_to_past_cases_when_both_are_ticked(cases_root, monkeypatch):
    from app.core import kb
    r = _router()
    seen = {}

    def search(q, *, user_id, k, **kw):
        seen["k"] = k
        return [{"purpose": "substantive_basis", "title": f"法{i}", "text": f"第{i}條"} for i in range(k)]

    monkeypatch.setattr(kb, "search", search)
    monkeypatch.setattr(r._uo, "auth_enabled", lambda: True)
    for _ in range(2):
        _make(1001, OLD)
    refs, kb_note, h_note = r._refs_lookup({"mode": "sign", "narrative": NEW, "use_kb": True,
                                            "use_history": True}, 1001)
    assert seen["k"] == r.KB_MAX_RESULTS - 2
    assert [x["purpose"] for x in refs].count("past_case") == 2 and len(refs) == r.KB_MAX_RESULTS
    assert (kb_note, h_note) == ("", "")
    # 沒有相近的：講出來（不是安靜地沒參考）
    refs, _kb, h_note = r._refs_lookup({"mode": "sign", "narrative": UNRELATED,
                                        "use_history": True}, 1001)
    assert refs == [] and h_note == "none"


# ------------------------------------------------------------------ 端點：真的送一次

def _draft_prompt(fake) -> str:
    got = [p for p in fake.prompts if "擬一份「簽」的內容" in p]
    assert got, "沒有送出撰寫草稿的提示"
    return got[-1]


def _past(res) -> list[dict]:
    return [r for r in (res["draft"].get("references") or []) if r.get("purpose") == "past_case"]


def test_other_peoples_cases_never_reach_the_prompt(admin_session, fake_llm):
    tag = uuid.uuid4().hex[:6]
    a = _user_client(f"odh_a_{tag}")
    b = _user_client(f"odh_b_{tag}")
    # B 寫過一件相近的（內容有一個只屬於 B 的字串）
    _, b_case, b_res = _run(b, narrative=OLD + "（B 的機密備註 ZQX）")
    # A 第一次勾「參考歷史案件」：A 自己沒有案件 → 一件都沒有，B 的那件不可以出現
    fake_llm.prompts.clear()
    _, a1, res = _run(a, narrative=NEW, use_history=True)
    assert _past(res) == [] and res["history_note"] == "none"
    assert "ZQX" not in "".join(fake_llm.prompts) and "〔歷史案件〕" not in _draft_prompt(fake_llm)
    # A 寫過一件之後：參考到的是 A 自己那一件
    _, a2, _ = _run(a, narrative=OLD + "（A 自己的備註 KWP）")
    fake_llm.prompts.clear()
    _, a3, res = _run(a, narrative=NEW, use_history=True)
    ids = [r["case_id"] for r in _past(res)]
    assert a2 in ids and b_case not in ids, ids
    prompt = _draft_prompt(fake_llm)
    assert "〔歷史案件〕" in prompt and "ZQX" not in prompt
    # 管理員：也只查得到自己的（A、B 的都不行）
    adm = admin_session[0] if isinstance(admin_session, tuple) else admin_session
    fake_llm.prompts.clear()
    _, _c, res = _run(adm, narrative=NEW, use_history=True)
    ids = [r["case_id"] for r in _past(res)]
    assert not (set(ids) & {a1, a2, a3, b_case}), ids
    assert "ZQX" not in "".join(fake_llm.prompts) and "KWP" not in "".join(fake_llm.prompts)


def test_regenerating_never_uses_the_case_itself(auth_off, client, fake_llm):
    _, cid, res = _run(client, narrative=OLD)
    r = client.post(f"{BASE}/start", json={"mode": "sign", "narrative": NEW, "case_id": cid,
                                           "use_history": True})
    assert r.status_code == 200, r.text
    assert _wait_job(r.json()["job_id"]).status == "done"
    res = client.get(f"{BASE}/result/{cid}").json()
    assert cid not in [x["case_id"] for x in _past(res)]
    assert res["inputs"]["use_history"] is True


def test_the_checkbox_shows_only_for_people_with_their_own_cases(admin_session, fake_llm):
    tag = uuid.uuid4().hex[:6]
    a = _user_client(f"odh_c_{tag}")
    b = _user_client(f"odh_d_{tag}")
    _run(b, narrative=OLD)
    assert 'id="odUseHistory"' not in a.get(f"{BASE}/").text, "別人有案件不代表我有"
    _run(a, narrative=OLD)
    assert 'id="odUseHistory"' in a.get(f"{BASE}/").text


def test_the_sync_api_also_reports_the_history_note(auth_off, client, fake_llm):
    r = client.post(f"{BASE}/api/official-doc",
                    json={"mode": "sign", "narrative": UNRELATED + "（API）", "use_history": True})
    assert r.status_code == 200, r.text
    assert r.json()["history_note"] in ("", "none")
    assert "history_note" in r.json()
