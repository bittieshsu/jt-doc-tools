"""從逐字稿的自我介紹找出發言者可能的名字（`app/core/self_intro.py`，2026-10-03 使用者要求）。

**判準在「不該建議的不建議」**：判錯會把 A 的話掛到 B 的名字上，而讀的人沒有理由懷疑。
所以反例比正例多，而且反例都是會議裡真的會講的話 ——「我是覺得…」「我是說…」
「我是用 Proxmox 的」「我叫王小明負責這件事」（叫他去負責，不是報名字）。

素材是自己寫的，**正式機兩份真的逐字稿另外量過**（各抓到兩位、八句含「我是」的話零誤判），
真的內容不放進測試（客戶資料）。
"""
from __future__ import annotations

import json
import uuid

import pytest

from app.core import self_intro as si

_SHOULD_FIND = {
    "大家好，我是 Wendy，今天由我來報告": ["Wendy"],
    "那大家好我是就是範例科技這邊的業務Wendy": ["Wendy"],     # 沒有標點、名字放在職稱後面
    "Kevin 我是Bella": ["Bella"],                            # 前面那個 Kevin 是在叫人，不是自己
    "我是王小明。": ["王小明"],
    "我叫林志玲啦": ["林志玲"],
    "我是那個王大明": ["王大明"],
    "我叫做李四": ["李四"],                                  # 「我叫」是明確報名，兩個字也算
    "大家好，我是李四": ["李四"],                             # 有問候語，兩個字也算
    "我是陳總。": ["陳總"],                                  # 姓氏＋稱呼
    "我是林經理，負責專案": ["林經理"],
    "我是小王，請多指教": ["小王"],
    "我是歐陽娜娜。": ["歐陽娜娜"],
    "我是王小明今天要跟大家報告": ["王小明"],                 # 原始層沒有標點
    "我是資訊室的工程師王大明": ["王大明"],
    "我的名字是 Wendy Lin": ["Wendy Lin"],
    "我是業務經理 Kevin": ["Kevin"],
    "Hi, I'm Tom from Contoso.": ["Tom"],
    "This is Amy from the sales team.": ["Amy"],
    "My name is Kevin.": ["Kevin"],
    "田中と申します": ["田中"],
}

_SHOULD_NOT_FIND = [
    # 「我是」後面接的是意見、動作
    "我是覺得這樣不太好", "我是說的那個方案", "我是想說這個", "我是不用做",
    "我是講備份那邊把它restore回來", "那這邊我是請小組幫忙", "我是叫我們下面有一個業務去處理",
    "我是說 Wendy 會負責", "我是跟王小明一起來的", "我是代表王小明來的",
    "我是向您報告", "我是全部都看過", "我是第一次來", "我是周末才有空", "我是何時要交",
    # 姓氏開頭的常見詞
    "我是高興啦", "我是方便的", "我是白做了", "我是石頭", "我是高手", "我是文組的",
    "我是金牌業務", "我是黃色那一組", "我是管理者。", "大家好，我是顧問", "我是曾經做過",
    "我是簡單講一下", "我是高中生。", "我是程式組的", "我是董事會", "我是馬上處理",
    # 英文：產品名不是人名
    "我是用 Proxmox 的", "我是 Proxmox 的愛用者", "我是 OK 的", "我是 Windows 用戶",
    "我是 Apple 那邊的", "所以我是 Mac 派", "我是 Wendy 的主管",
    # 名字後面接的話說不通 / 在講別人
    "我是陳經理那邊的人", "我是王小明的主管", "這位是王小明", "我是林",
    "我叫王小明負責這件事", "我叫王小明來處理",
    "我叫王小明今天過來",                  # 「我叫」後面不認接續句：那是叫別人做事
    # 名字最後一個字不會是語助詞；兩個字的要有「我叫」、問候語或稱呼
    "我是王的", "我是張羅的", "我是游客。",
    # 句尾的英文字前面不是職稱
    "我是用 Proxmox", "我是比較喜歡 Windows",
    # 英文
    "I'm fine, thanks.", "I'm going to share the screen.", "This is Proxmox cluster.",
    "It's fine from here.", "I am Ready.", "I'm Fine, thanks.",
]


@pytest.mark.parametrize("text,want", list(_SHOULD_FIND.items()))
def test_a_self_introduction_is_found(text, want):
    assert si.names_in(text) == want


@pytest.mark.parametrize("text", _SHOULD_NOT_FIND)
def test_ordinary_speech_is_not_taken_for_a_name(text):
    assert si.names_in(text) == [], f"把一般的話當成名字：{text!r} → {si.names_in(text)}"


def test_the_corpus_has_teeth():
    """反例一定要比正例多 —— 這個功能錯的代價在誤判，不在漏抓。"""
    assert len(_SHOULD_NOT_FIND) > len(_SHOULD_FIND)


def test_hints_are_per_speaker_and_name_the_source_segment():
    segs = [
        {"seq": 1, "speaker": "S1", "text": "大家好，我是 Wendy，今天由我報告"},
        {"seq": 2, "speaker": "S2", "text": "我是覺得可以"},
        {"seq": 3, "speaker": "S3", "text": "Kevin 我是Bella"},
        {"seq": 4, "speaker": None, "text": "我是王小明。"},           # 沒有代號：不知道是誰說的
        {"seq": 5, "speaker": "S1", "text": "對，我是 Wendy。"},
    ]
    h = si.hints(segs)
    assert set(h) == {"S1", "S3"}, h
    assert h["S1"]["name"] == "Wendy" and h["S1"]["count"] == 2 and h["S1"]["seq"] == 1
    assert "我是 Wendy" in h["S1"]["text"]
    assert h["S3"]["name"] == "Bella"


def test_two_names_for_one_speaker_keeps_the_more_frequent_and_lists_the_other():
    """辨識把兩個人併成一位時，兩個名字都會出現 —— 不可以安靜丟掉另一個。"""
    segs = [{"seq": 1, "speaker": "S1", "text": "我是 Amy。"},
            {"seq": 2, "speaker": "S1", "text": "我是 Tom。"},
            {"seq": 3, "speaker": "S1", "text": "我是 Tom，剛剛斷線了"}]
    h = si.hints(segs)["S1"]
    assert h["name"] == "Tom" and h["others"] == ["Amy"], h


def test_the_excerpt_is_short():
    segs = [{"seq": 1, "speaker": "S1", "text": "我是 Wendy。" + "後面很長的一段話" * 40}]
    assert len(si.hints(segs)["S1"]["text"]) <= 80


def test_the_result_endpoint_offers_hints_but_does_not_store_them(client):
    """結果端點每次現算 —— 存進檔案的話，下載的 JSON 與轉送給會議摘要的資料都會多一份建議。"""
    from app.config import settings
    uid = uuid.uuid4().hex
    path = settings.temp_dir / f"mt_{uid}_transcript.json"
    settings.temp_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"segments": [
        {"seq": 1, "speaker": "S1", "text": "大家好，我是 Wendy。"},
        {"seq": 2, "speaker": "S2", "text": "我是覺得可以"}],
        "speaker_names": {}, "speaker_overrides": {}}, ensure_ascii=False), encoding="utf-8")
    try:
        got = client.get(f"/tools/meeting-transcribe/result/{uid}").json()
        assert got["name_hints"]["S1"]["name"] == "Wendy", got.get("name_hints")
        assert "S2" not in got["name_hints"]
        assert "name_hints" not in json.loads(path.read_text(encoding="utf-8")), "建議被寫進逐字稿了"
    finally:
        path.unlink(missing_ok=True)
