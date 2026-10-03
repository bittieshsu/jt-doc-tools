"""會議背景的專有名詞 → 逐字稿裡可能寫錯的寫法（`app/core/term_fix.py`，v1.16.39）。

2026-10-02 使用者決定：會議背景的專有名詞要拿來修逐字稿的錯字。做法是**建議**，
使用者勾選才換、原文保留。這裡驗兩件事：

* **該抓的抓得到**：英文差一兩個字母（Bianka → Bianca）、中間多了空白或連字號、
  中文同音不同字（王曉明 → 王小明）。
* **不該抓的不抓**（這一半比上一半重要 —— 錯的建議一多，這個功能就沒人看了）：
  讀音不同（王小姐）、兩個字的詞（公事 / 公式撞音）、單複數、只差大小寫、
  寫法不同的字太多、背景自己就這樣寫、同一個寫法像兩個詞。

素材是自己寫的，**判準也自己寫**：每一條「不該抓」都講出它擋的是哪一種誤報。
"""
from __future__ import annotations

import pytest

from app.core import term_fix as tf

CTX = """會議主題：第四季預算
與會者：王小明（財務長）、李美華（行銷經理）、Bianca（PM）
專有名詞：台灣範例、OfflineMirror、Proxmox VE、Acme-Kevin"""

SEGS = [
    {"seq": 1, "speaker": "S1", "text": "王曉明說預算要再看一下，請 Bianka 寄給 Acme Kevin。"},
    {"seq": 2, "speaker": "S2", "text": "王小姐那邊的報價，李美樺會跟進。臺灣範例的案子用 Offline Mirror。"},
    {"seq": 3, "speaker": "S1", "text": "Bianka's draft 好了，meetings 都排好了，proxmox ve 升級。"},
    {"seq": 4, "speaker": "S2", "text": "我們這裡沒滑倒。公事公辦。Bianca 也同意，Biankas 是另一個人。"},
]


def _pairs(res):
    return {(s["from"], s["to"]) for s in res["suggestions"]}


def test_terms_come_from_the_background_in_order():
    terms = tf.extract_terms(CTX)
    for t in ("王小明", "李美華", "Bianca", "台灣範例", "OfflineMirror", "Proxmox VE", "Acme-Kevin"):
        assert t in terms, (t, terms)
    # 全小寫的一般英文字與太短的詞不是候選（`budget`、`PM`、`VE` 單獨一個）
    assert not {"PM", "VE"} & set(terms)
    assert tf.extract_terms("topic: quarterly budget review") == []
    assert terms.index("王小明") < terms.index("李美華")


def test_the_misspellings_are_found():
    got = _pairs(tf.suggest(SEGS, CTX))
    for pair in [("Bianka", "Bianca"), ("王曉明", "王小明"), ("李美樺", "李美華"),
                 ("Offline Mirror", "OfflineMirror"), ("Acme Kevin", "Acme-Kevin"),
                 ("臺灣範例", "台灣範例")]:
        assert pair in got, (pair, got)


@pytest.mark.parametrize("surface,why", [
    ("王小姐", "讀音不同（jie ≠ ming）—— 只比字形的話它跟王小明只差一個字"),
    ("裡沒滑", "三個字的寫法全不同，只是剛好同音 —— 那多半是一句普通的話"),
    ("meetings", "單複數不是錯字"),
    ("proxmox ve", "只差大小寫不是錯字"),
    ("Biankas", "另一個詞（差 1 個字母但是單複數形）"),
])
def test_what_must_not_be_suggested(surface, why):
    got = {s["from"] for s in tf.suggest(SEGS, CTX)["suggestions"]}
    assert surface not in got, why


def test_two_character_words_are_not_compared():
    """兩個字的詞撞音太多（公事 / 公式、會議 / 會意）—— 背景裡寫了「公式」，
    逐字稿的「公事」也不可以被建議換掉。"""
    segs = [{"seq": 1, "text": "公事公辦，這條公式要再算一次。"}]
    assert tf.suggest(segs, "專有名詞：公式")["suggestions"] == []


def test_a_spelling_the_background_itself_uses_is_not_a_typo():
    ctx = CTX + "\n另一位：Bianka（外部顧問）"
    got = {s["from"] for s in tf.suggest(SEGS, ctx)["suggestions"]}
    assert "Bianka" not in got


def test_a_spelling_the_background_uses_in_passing_is_not_a_typo():
    """背景在一句長的說明裡用了這個寫法（不會被當成一個詞取出來）—— 照樣不建議。
    上面那條會被「它自己就是背景裡的一個詞」擋下，驗不到這一條。"""
    ctx = CTX + "\n註：舊文件寫成臺灣範例的也是同一家公司"
    got = {s["from"] for s in tf.suggest(SEGS, ctx)["suggestions"]}
    assert "臺灣範例" not in got


def test_plural_and_tense_forms_are_not_typos():
    """`Hackathons` 跟 `Hackathon` 只差一個字母 —— 但那是複數，不是錯字。"""
    segs = [{"seq": 1, "text": "The Hackathons were fun, and Hackaton was a typo."}]
    got = _pairs(tf.suggest(segs, "活動：Hackathon"))
    assert ("Hackathons", "Hackathon") not in got
    assert ("Hackaton", "Hackathon") in got           # 反向對照：真的錯字照樣抓


def test_a_spelling_close_to_two_terms_is_not_suggested():
    """`Bianc` 跟 `Bianca`、`Bianco` 都只差一個字母 → 不知道該換成哪一個，就不建議。"""
    segs = [{"seq": 1, "text": "請 Bianc 確認。"}]
    got = {s["from"] for s in tf.suggest(segs, "與會者：Bianca、Bianco")["suggestions"]}
    assert "Bianc" not in got
    # 反向對照：只有一個近的時候照樣建議（不然上面那條什麼都沒驗到）
    one = {s["from"] for s in tf.suggest(segs, "與會者：Bianca")["suggestions"]}
    assert "Bianc" in one


def test_the_count_matches_what_applying_will_change():
    """畫面上寫「N 處」，勾了之後就要真的換 N 處 —— 兩邊用同一套比對。"""
    res = tf.suggest(SEGS, CTX)
    pairs = [{"from": s["from"], "to": s["to"]} for s in res["suggestions"]]
    _new, applied = tf.apply(SEGS, pairs)
    want = {(s["from"], s["to"]): s["count"] for s in res["suggestions"]}
    assert {(a["from"], a["to"]): a["count"] for a in applied} == want
    assert want[("Bianka", "Bianca")] == 2          # 含所有格那一次


def test_applying_keeps_the_original_and_respects_word_boundaries():
    new, applied = tf.apply(SEGS, [{"from": "Bianka", "to": "Bianca"},
                                   {"from": "王曉明", "to": "王小明"}])
    assert new[0]["text"] == "王小明說預算要再看一下，請 Bianca 寄給 Acme Kevin。"
    assert new[0]["orig_text"] == SEGS[0]["text"]
    assert new[2]["text"].startswith("Bianca's draft")
    # 沒換到的段落不帶 `orig_text`；`Biankas` 不可以被換掉一截
    assert "orig_text" not in new[1]
    assert "Biankas" in new[3]["text"] and "orig_text" not in new[3]
    assert {a["from"]: a["count"] for a in applied} == {"Bianka": 2, "王曉明": 1}
    # 原本的段落不可以被改到（呼叫端可能還在用它）
    assert SEGS[0]["text"].startswith("王曉明")


def test_reapplying_starts_from_the_original():
    """上一次換過、這次沒勾的要換回原文；什麼都沒勾就是原樣。"""
    first, _ = tf.apply(SEGS, [{"from": "Bianka", "to": "Bianca"},
                               {"from": "王曉明", "to": "王小明"}])
    second, applied = tf.apply(first, [{"from": "王曉明", "to": "王小明"}])
    assert "Bianka" in second[0]["text"] and second[0]["text"].startswith("王小明")
    assert [a["from"] for a in applied] == ["王曉明"]
    back, none = tf.apply(second, [])
    assert none == []
    assert [s["text"] for s in back] == [s["text"] for s in SEGS]
    assert not any("orig_text" in s for s in back)


def test_suggestions_look_at_the_original_text():
    """換過之後再問建議，清單不可以因為已經換掉而消失 —— 使用者要能取消勾選。"""
    first, _ = tf.apply(SEGS, [{"from": "Bianka", "to": "Bianca"}])
    assert ("Bianka", "Bianca") in _pairs(tf.suggest(first, CTX))


@pytest.mark.parametrize("pair", [
    {"from": "J", "to": "K"},                       # 一個字的會把整份逐字稿換爛
    {"from": "Bianka", "to": "Bianka"},             # 沒有變
    {"from": "x" * 101, "to": "y"},                 # 太長
    {"from": "Bianka", "to": "Jo\u0000anna"},       # 控制字元
    {"from": 3, "to": "a"},                         # 不是字串
    "Bianka",                                       # 不是物件
])
def test_bad_pairs_are_ignored(pair):
    new, applied = tf.apply(SEGS, [pair])
    assert applied == []
    assert [s["text"] for s in new] == [s["text"] for s in SEGS]


def test_the_number_of_pairs_is_capped():
    pairs = [{"from": f"Name{i:03d}", "to": f"Nom{i:03d}"} for i in range(tf.MAX_PAIRS + 10)]
    assert len(tf.clean_pairs(pairs)) == tf.MAX_PAIRS


def test_readings_are_available():
    """讀音資料是宣告過的相依 —— 沒裝到的話中文那一半會安靜地只剩「一模一樣才算」。"""
    assert tf.readings_available()
    assert tf._readings("曉") & tf._readings("小")
    assert not (tf._readings("姐") & tf._readings("明"))


def test_saved_replacements_are_normalised():
    rows = [{"from": "Bianka", "to": "Bianca", "count": 2},
            {"from": "", "to": "x", "count": 1},
            {"from": "a", "to": "b", "count": True},
            {"from": "a", "to": {"x": 1}, "count": 1},
            "junk"]
    assert tf.normalise_applied(rows) == [{"from": "Bianka", "to": "Bianca", "count": 2}]
    assert tf.normalise_applied("nope") is None


# ------------------------------------------------------------ 自己加替換（v1.16.45）
# 2026-10-03：辨識聽錯、建議又抓不到的（Groxmoxity → Proxmox、POWPOYNT → PowerPoint），
# 讓使用者自己指定。`find` 回「整份逐字稿出現幾處」—— 那個數字要等於勾了之後實際換掉的處數。

MANUAL = [
    {"seq": 1, "text": "這台 Groxmoxity 叢集要升級，groxmoxity 的備份也一起做。"},
    {"seq": 2, "text": "簡報用 POWPOYNT 做，PPQ 檔寄給我。"},
    {"seq": 3, "text": "MyGroxmoxity 跟 Groxmoxityx 是別的東西，PPQX 也是。"},
    {"seq": 4, "text": "改過的段落", "orig_text": "原本這裡也講了 GROXMOXITY。"},
    {"seq": 5, "text": "波波點的檔案、波波點二號。"},
]


def test_find_counts_the_whole_transcript_ignoring_case():
    """使用者打小寫也要找得到大寫的；回實際出現的每一種寫法（多的排前面）。"""
    d = tf.find(MANUAL, "groxmoxity")
    assert d["count"] == 3, d
    assert set(d["variants"]) == {"Groxmoxity", "groxmoxity", "GROXMOXITY"}, d
    assert "Groxmoxity" in d["example"]


def test_find_matches_whole_words_only():
    """`PPQ` 不可以算進 `PPQX`、`Groxmoxity` 不可以算進 `MyGroxmoxity` —— 換了就把別的詞切壞。"""
    assert tf.find(MANUAL, "PPQ")["count"] == 1
    assert tf.find(MANUAL, "Groxmoxity")["count"] == 3


def test_find_looks_at_the_original_text():
    """換過的段落要照原文找 —— 不然重新打開時，上一次自己加的那幾列會變成「找不到」。"""
    d = tf.find(MANUAL, "GROXMOXITY")
    assert "GROXMOXITY" in d["variants"], d


def test_find_cjk_by_substring():
    assert tf.find(MANUAL, "波波點")["count"] == 2


@pytest.mark.parametrize("bad", ["", "P", " ", "Pro\u0000smos", None, 3, "x" * 101])
def test_find_rejects_what_apply_would_reject(bad):
    assert tf.find(MANUAL, bad) == {"count": 0, "variants": [], "example": ""}


@pytest.mark.parametrize("word,to", [("groxmoxity", "Proxmox"), ("PPQ", "PPT"),
                                     ("波波點", "PowerPoint"), ("POWPOYNT", "PowerPoint")])
def test_the_count_shown_is_the_count_replaced(word, to):
    """畫面上的「N 處」跟勾了之後實際換掉的處數用同一套比對（同建議清單那條）。"""
    d = tf.find(MANUAL, word)
    _, applied = tf.apply(MANUAL, [{"from": v, "to": to} for v in d["variants"]])
    assert sum(a["count"] for a in applied) == d["count"] > 0, (word, d, applied)


def test_saved_manual_rows_keep_the_order_they_were_added_in():
    """套用時長的先換（`clean_pairs`），但存起來的清單要照使用者加入的順序。"""
    rows = [{"from": "ab", "to": "AB"}, {"from": "POWPOYNT", "to": "PowerPoint"},
            {"from": "x", "to": "y"}, {"from": "ab", "to": "dup"}, "junk"]
    assert tf.clean_rows(rows) == [{"from": "ab", "to": "AB"},
                                   {"from": "POWPOYNT", "to": "PowerPoint"}]
    assert [f for f, _ in tf.clean_pairs(rows)] == ["POWPOYNT", "ab"]
